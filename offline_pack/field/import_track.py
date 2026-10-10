# -*- coding: utf-8 -*-
"""Импорт трека реального проезда в канонический формат field_track_v1.

Адаптеры: GPX (тркпоинты + расширения точности/скорости/курса),
NMEA 0183 ($GPRMC/$GPGGA), а также уже канонический JSON.

Принципы:
- исходные метки времени сохраняются как timestamp_utc, а timestamp_s —
  секунды от первой фиксации (для движка);
- отсутствующие координаты НЕ превращаются в нули: missing=true, lat/lon=null;
- интервалы между фиксациями сохраняются как есть (без интерполяции);
- немонотонное время и некорректные значения помечаются skip_reason и
  отбрасываются движком при replay;
- никаких сетевых запросов (только stdlib: json, sys, datetime,
  pathlib, xml.etree).

Запуск:  python -X utf8 offline_pack/field/import_track.py <input> [-o out.json]
         [--assume-accuracy-m 10]   # только если источник не содержит точности
"""
import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "field_track_v1"

# Приблизительный перевод HDOP в метры (эмпирика для бытовых GPS: ~5 м на 1.0 HDOP)
HDOP_TO_METERS = 5.0
KNOTS_TO_MPS = 0.514444


# ------------------------------- утилиты ------------------------------------

def _localname(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_iso(ts):
    if ts is None:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _empty_fix():
    return {
        "timestamp_utc": None,
        "timestamp_s": None,
        "latitude": None,
        "longitude": None,
        "horizontal_accuracy_m": None,
        "speed_mps": None,
        "bearing_deg": None,
        "missing": False,
        "skip_reason": None,
    }


# --------------------------------- GPX --------------------------------------

def parse_gpx(text, track_id="imported-gpx"):
    root = ET.fromstring(text)
    fixes = []
    for trkpt in root.iter():
        if _localname(trkpt.tag) != "trkpt":
            continue
        fix = _empty_fix()
        lat = trkpt.get("lat")
        lon = trkpt.get("lon")
        if lat is None or lon is None:
            fix["missing"] = True
            fix["skip_reason"] = "no_coords"
        else:
            fix["latitude"] = float(lat)
            fix["longitude"] = float(lon)
        hdop = None
        for child in trkpt.iter():
            name = _localname(child.tag)
            if name == "time":
                fix["timestamp_utc"] = child.text
            elif name == "hdop":
                hdop = child.text
            elif name in ("speed",):
                fix["speed_mps"] = _safe_float(child.text)
            elif name in ("course", "bearing", "heading"):
                fix["bearing_deg"] = _safe_float(child.text)
            elif name in ("accuracy", "eph", "horizontalaccuracy"):
                v = _safe_float(child.text)
                if v is not None:
                    fix["horizontal_accuracy_m"] = v
        if fix["horizontal_accuracy_m"] is None and hdop is not None:
            v = _safe_float(hdop)
            if v is not None:
                fix["horizontal_accuracy_m"] = round(v * HDOP_TO_METERS, 2)
        fixes.append(fix)
    return _assemble(fixes, track_id, "gpx")


# --------------------------------- NMEA -------------------------------------

_NMEA_SIGN = {"N": 1.0, "S": -1.0, "E": 1.0, "W": -1.0}


def _nmea_dm_to_dec(value, hemi):
    """Переводит NNMM.MMMM (или NNNMM.MMMM) + полушарие в десятичные градусы."""
    value = (value or "").strip()
    hemi = ((hemi or "").strip() or value[-1:]).upper()
    if not value or not value[0].isdigit():
        return None
    if not value[-1:].isdigit():
        value = value[:-1]  # значение без суффикса полушария
    try:
        dot = value.index(".")
        deg = int(value[:dot - 2])
        minutes = float(value[dot - 2:])
    except (ValueError, IndexError):
        return None
    sign = _NMEA_SIGN.get(hemi, 1.0)
    return sign * (deg + minutes / 60.0)


def parse_nmea(text, track_id="imported-nmea"):
    fixes_by_time = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("$"):
            continue
        parts = line.split(",")
        kind = parts[0][-3:]
        if kind == "GGA" and len(parts) >= 10:
            t = parts[1]
            quality = _safe_float(parts[6])
            lat = _nmea_dm_to_dec(parts[2], parts[3]) if parts[2] and parts[3] else None
            lon = _nmea_dm_to_dec(parts[4], parts[5]) if parts[4] and parts[5] else None
            if lat is not None and lon is not None and quality not in (None, 0.0):
                fix = fixes_by_time.setdefault(t, _empty_fix())
                fix["latitude"] = lat
                fix["longitude"] = lon
                fix["_nmea_time"] = t
                hdop = _safe_float(parts[8])
                if hdop is not None:
                    fix["horizontal_accuracy_m"] = round(hdop * HDOP_TO_METERS, 2)
        elif kind == "RMC" and len(parts) >= 9:
            t = parts[1]
            date = parts[9] if len(parts) > 9 else None
            if parts[2] != "A":
                continue
            lat = _nmea_dm_to_dec(parts[3], parts[4]) if parts[3] and parts[4] else None
            lon = _nmea_dm_to_dec(parts[5], parts[6]) if parts[5] and parts[6] else None
            if lat is None or lon is None:
                continue
            fix = fixes_by_time.setdefault(t, _empty_fix())
            fix["latitude"] = lat
            fix["longitude"] = lon
            fix["_nmea_time"] = t
            fix["_nmea_date"] = date
            spd = _safe_float(parts[7])
            if spd is not None:
                fix["speed_mps"] = round(spd * KNOTS_TO_MPS, 3)
            brg = _safe_float(parts[8])
            if brg is not None:
                fix["bearing_deg"] = brg
    # Преобразуем в fixes с ISO временем
    fixes = []
    for t in sorted(fixes_by_time):
        fix = fixes_by_time[t]
        date = fix.pop("_nmea_date", None)
        fix.pop("_nmea_time", None)
        dt = _nmea_datetime(t, date)
        fix["timestamp_utc"] = dt.isoformat() if dt else None
        if fix["latitude"] is None:
            fix["missing"] = True
            fix["skip_reason"] = "no_fix"
        fixes.append(fix)
    return _assemble(fixes, track_id, "nmea")


def _nmea_datetime(t, date):
    if not t or len(t) < 6:
        return None
    hh, mm, ss = int(t[0:2]), int(t[2:4]), float(t[4:])
    year = month = day = 1970
    if date and len(date) == 6:
        day, month, year = int(date[0:2]), int(date[2:4]), 2000 + int(date[4:6])
    try:
        return datetime(year, month, day, hh, mm, int(ss),
                        int((ss - int(ss)) * 1e6), tzinfo=timezone.utc)
    except ValueError:
        return None


# ------------------------------- канонизация --------------------------------

def _safe_float(v):
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _assemble(fixes, track_id, original_format):
    """Проставляет timestamp_s от первой валидной метки времени и метаданные."""
    base = None
    for fix in fixes:
        dt = _parse_iso(fix.get("timestamp_utc"))
        if dt is None:
            if fix["latitude"] is not None and fix["timestamp_utc"] is None:
                fix["skip_reason"] = fix["skip_reason"] or "no_timestamp"
            continue
        if base is None:
            base = dt
        fix["timestamp_s"] = (dt - base).total_seconds()
    # немонотонность
    last = None
    for i, fix in enumerate(fixes):
        ts = fix["timestamp_s"]
        if ts is None:
            continue
        if last is not None and ts <= last:
            fix["missing"] = True
            fix["skip_reason"] = "non_monotonic_time"
        else:
            last = ts
    return {
        "schema": SCHEMA,
        "track_id": track_id,
        "is_real_car_drive": True,
        "source": {"tool": None, "original_format": original_format,
                   "original_file": None},
        "privacy": {"contains_personal_data": False,
                    "send_to_external_services": False},
        "route_notes": {"same_route_as_bundle": None, "deviations": None},
        "fixes": fixes,
    }


def import_track(path, assume_accuracy_m=None):
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    track_id = path.stem
    if path.suffix.lower() == ".gpx" or text.lstrip().startswith("<?xml") \
            or "<gpx" in text[:2000]:
        track = parse_gpx(text, track_id)
    elif text.lstrip().startswith("$") or "$GPRMC" in text or "$GPGGA" in text:
        track = parse_nmea(text, track_id)
    else:
        data = json.loads(text)
        if data.get("schema") != SCHEMA:
            raise ValueError(f"неизвестная схема JSON: {data.get('schema')!r}")
        track = data
    if assume_accuracy_m is not None:
        _apply_assumed_accuracy(track, assume_accuracy_m)
    return track


def _apply_assumed_accuracy(track, meters):
    """Заполняет отсутствующую точность явно указанным значением.

    Значение — ЭТО ДОПУЩЕНИЕ (рекордер не сохранил точность), оно явно
    фиксируется в метаданных трека, а не выдумывается как измеренное.
    """
    filled = 0
    for fix in track.get("fixes", []):
        if fix.get("horizontal_accuracy_m") is None and not fix.get("missing"):
            fix["horizontal_accuracy_m"] = float(meters)
            filled += 1
    source = track.setdefault("source", {})
    assumptions = track.setdefault("assumptions", {})
    assumptions["accuracy"] = {
        "assumed_m": float(meters),
        "filled_fixes": filled,
        "reason": "рекордер не сохранил HDOP/accuracy; движку нужна точность "
                  "для отбраковки фиксаций. Значение — допущение, а не измерение.",
    }
    route_notes = track.setdefault("route_notes", {})
    route_notes["accuracy_in_source"] = False
    return track


def usable_fixes(track):
    """Итератор (source_index, fix) по фиксациям, пригодным для движка."""
    for i, fix in enumerate(track.get("fixes", [])):
        if fix.get("missing"):
            continue
        if fix.get("timestamp_s") is None:
            continue
        if fix.get("latitude") is None or fix.get("longitude") is None:
            continue
        yield i, fix


def main():
    ap = argparse.ArgumentParser(description="Импорт GPS-трека в field_track_v1")
    ap.add_argument("input", help="входной файл (.gpx/.nmea/.json)")
    ap.add_argument("-o", "--output", help="канонический JSON (по умолчанию рядом)")
    ap.add_argument("--assume-accuracy-m", type=float, default=None,
                    help="если рекордер не сохранил точность, заполнить этим "
                         "значением (записывается как допущение в метаданные)")
    args = ap.parse_args()
    track = import_track(args.input, assume_accuracy_m=args.assume_accuracy_m)
    out = Path(args.output) if args.output else \
        Path(args.input).with_suffix(".track.json")
    out.write_text(json.dumps(track, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(track["fixes"])
    usable = sum(1 for _ in usable_fixes(track))
    print(f"импортировано: {out}")
    print(f"  формат {track['source']['original_format']}, фиксаций {total}, "
          f"пригодно {usable}, пропущено {total - usable}")


if __name__ == "__main__":
    main()