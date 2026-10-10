# -*- coding: utf-8 -*-
"""Метрики качества поля (Field GPS Validation) и генерация отчёта.

По каждому POI: минимальное расстояние трека, расстояние в момент срабатывания,
точность GPS в этот момент, отброшенные фиксации рядом, повторные срабатывания,
класс достижимости, раннее/позднее срабатывание.

Без реального трека скрипт генерирует field_report.{json,md} со статусом
BLOCKED: WAITING_FOR_FIELD_TRACK и НЕ выдумывает полевые метрики.

Запуск:
  python -X utf8 offline_pack/field/metrics_report.py            # blocked-отчёт
  python -X utf8 offline_pack/field/metrics_report.py <replay.json> <track.json>
"""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core.bundle import load_bundle
from companion_core.playback import RouteGeometry, haversine_m

FIELD_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = Path(__file__).resolve().parents[1] / "trip_bundle"


def _ev_dist_to_poi(ev, lat, lon):
    if ev.get("latitude") is None or ev.get("longitude") is None:
        return None
    return haversine_m(ev["latitude"], ev["longitude"], lat, lon)


def compute_metrics(track, replay_result, bundle_dir=BUNDLE_DIR):
    bundle = load_bundle(Path(bundle_dir))
    geo = RouteGeometry(bundle.route_coordinates)
    cfg = replay_result["engine_config"]
    radius_default = None

    fixes = [f for f in track.get("fixes", [])
             if not f.get("missing") and f.get("latitude") is not None]
    events = replay_result["events"]
    story_to_route = {}
    for s in bundle.stories:
        d, _ = geo.measure(s["coordinates"]["lat"], s["coordinates"]["lon"])
        story_to_route[s["story_id"]] = d

    per_poi = []
    triggered_count = 0
    repeated_count = 0
    justified = 0
    expected = 0
    indeterminate = 0
    trigger_distances = []
    rejected_events = [e for e in events if e["type"] == "FIX_REJECTED"]
    rejected_total = len(rejected_events)

    for s in bundle.stories:
        sid = s["story_id"]
        lat, lon = s["coordinates"]["lat"], s["coordinates"]["lon"]
        radius = s["trigger_radius_m"]

        # минимальные расстояния по треку
        min_d = float("inf")
        min_route_d = float("inf")
        for f in fixes:
            d = haversine_m(f["latitude"], f["longitude"], lat, lon)
            if d < min_d:
                min_d = d
            dr, _ = geo.measure(f["latitude"], f["longitude"])
            if dr < min_route_d:
                min_route_d = dr
        min_d = None if min_d == float("inf") else round(min_d, 1)
        min_route_d = None if min_route_d == float("inf") else round(min_route_d, 1)

        trg = [e for e in events
               if e["type"] == "STORY_TRIGGER" and e.get("story_id") == sid]
        trig = trg[0] if trg else None
        if len(trg) > 1:
            repeated_count += len(trg) - 1

        rec = {
            "story_id": sid,
            "poi_id": s["poi_id"],
            "title": s.get("title"),
            "trigger_radius_m": radius,
            "story_distance_to_route_m": round(story_to_route[sid], 1),
            "min_track_distance_to_poi_m": min_d,
            "min_track_distance_to_route_m": min_route_d,
            "triggered": trig is not None,
        }

        # отброшенные фиксации рядом (в пределах 2*radius по прямой)
        near_rejected = 0
        for e in rejected_events:
            dd = _ev_dist_to_poi(e, lat, lon)
            if dd is not None and dd <= 2 * radius:
                near_rejected += 1
        rec["rejected_fixes_near"] = near_rejected

        # классификация по фактической близости трека
        if min_d is None:
            rec["class"] = "no_track"
        elif min_d <= radius:
            # был ли ближайший подход вне маршрута (съезд)?
            closest_offroad = False
            for f in fixes:
                if haversine_m(f["latitude"], f["longitude"], lat, lon) <= radius:
                    dr, _ = geo.measure(f["latitude"], f["longitude"])
                    if dr > cfg["off_route_threshold_m"]:
                        closest_offroad = True
                        break
            rec["class"] = "near_crow_flies_but_offroad" if closest_offroad \
                else "passed_near"
            if not closest_offroad:
                expected += 1
        else:
            rec["class"] = "not_passed"

        # неопределённость: трек приближался, но подходящих по точности фиксаций нет
        near_any = any(haversine_m(f["latitude"], f["longitude"], lat, lon) <= radius
                       for f in fixes)
        near_accurate = any(
            haversine_m(f["latitude"], f["longitude"], lat, lon) <= radius
            and (f.get("horizontal_accuracy_m") is None
                 or f["horizontal_accuracy_m"] <= cfg["min_gps_accuracy_m"])
            for f in fixes)
        if near_any and not near_accurate:
            rec["class"] = "indeterminate_gps"
            indeterminate += 1

        if trig is not None:
            triggered_count += 1
            td = trig.get("direct_distance_m")
            rec["trigger"] = {
                "timestamp_utc": trig.get("timestamp_utc"),
                "source_index": trig.get("source_index"),
                "latitude": trig.get("latitude"),
                "longitude": trig.get("longitude"),
                "distance_to_poi_m": td,
                "accuracy_m": trig.get("accuracy_m"),
                "progress_m": trig.get("progress_m"),
            }
            if td is not None:
                trigger_distances.append(td)
                if min_d is not None and min_d <= radius:
                    justified += 1
            # раннее/позднее относительно радиуса
            if td is not None and td > radius:
                rec["timing"] = "too_early_fired_outside_radius"
            elif trig.get("progress_m") is not None \
                    and trig["progress_m"] > s["route_distance_m"]:
                rec["timing"] = "too_late_after_closest"
            else:
                rec["timing"] = "within_radius_approaching"

        per_poi.append(rec)

    def ratio(num, den):
        return round(num / den, 3) if den else None

    metrics = {
        "schema": "field_metrics_v1",
        "expected_triggers": expected,
        "triggered": triggered_count,
        "poi_classes": dict(Counter(r["class"] for r in per_poi)),
        "expected_trigger_share": ratio(
            sum(1 for r in per_poi if r["class"] in ("passed_near",) and r["triggered"]),
            sum(1 for r in per_poi if r["class"] == "passed_near")),
        "justified_trigger_share": ratio(justified, triggered_count),
        "repeated_triggers": repeated_count,
        "trigger_distance_distribution_m": {
            "min": min(trigger_distances) if trigger_distances else None,
            "max": max(trigger_distances) if trigger_distances else None,
            "values": sorted(trigger_distances),
        },
        "rejected_fix_share": ratio(rejected_total,
                                    len(track.get("fixes", [])) or 1),
        "indeterminate_gps_cases": indeterminate,
        "assumptions": track.get("assumptions", {}),
        "per_poi": per_poi,
    }
    return metrics


def render_markdown(metrics, track_meta):
    m = metrics
    lines = [
        "# Field GPS Validation — отчёт",
        "",
        f"- track_id: `{track_meta.get('track_id')}`",
        f"- реальная поездка (is_real_car_drive): `{track_meta.get('is_real_car_drive')}`",
        f"- фиксаций: {track_meta.get('fixes_total')}",
        "",
        "## Допущения (assumptions)",
        "",
    ]
    acc = m.get("assumptions", {}).get("accuracy")
    if acc:
        lines += [
            f"- Точность GPS в исходном треке отсутствовала (рекордер не "
            f"сохранил HDOP/accuracy). Для replay использовано допущение: "
            f"**{acc['assumed_m']} м** (заполнено для {acc['filled_fixes']} "
            f"фиксаций). Это допущение, а не измерение.",
            "",
        ]
    else:
        lines += ["- Точность измерена рекордером, допущений нет.", ""]
    lines += [
        "## Итоговые метрики",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        "| Ожидаемых триггеров (проезд по маршруту в радиусе) | "
        f"{m['expected_triggers']} |",
        f"| Сработало | {m['triggered']} |",
        f"| Доля ожидаемых сработавших | {m['expected_trigger_share']} |",
        f"| Доля обоснованных срабатываний | {m['justified_trigger_share']} |",
        f"| Повторных срабатываний | {m['repeated_triggers']} |",
        f"| Доля отброшенных фиксаций | {m['rejected_fix_share']} |",
        f"| Неопределённых (GPS) случаев | {m['indeterminate_gps_cases']} |",
        "",
        "Классы POI: " + ", ".join(f"{k}={v}" for k, v in m["poi_classes"].items()),
        "",
        "Пояснение классов: `passed_near` — трек был на маршруте в радиусе POI; "
        "`near_crow_flies_but_offroad` — трек приближался по прямой, но в момент "
        "подхода был вне маршрута (движок в режиме съезда не срабатывает); "
        "`not_passed` — трек не проходил в радиусе. Несрабатывание не считается "
        "ошибкой, если трек не был на маршруте в пределах радиуса.",
        "",
        "## По POI",
        "",
        "| POI | Радиус, м | Мин. до POI, м | Класс | Триггер | Дист. триггера, м | Точность, м | Раннее/позднее |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in m["per_poi"]:
        t = r.get("trigger") or {}
        lines.append(
            f"| {r['story_id']} | {r['trigger_radius_m']} | "
            f"{r['min_track_distance_to_poi_m']} | {r['class']} | "
            f"{'да' if r['triggered'] else 'нет'} | {t.get('distance_to_poi_m', '')} | "
            f"{t.get('accuracy_m', '')} | {r.get('timing', '')} |")
    lines.append("")
    lines.append("_Несрабатывание не считается ошибкой, если трек не проходил "
                 "в пределах радиуса. Отчёт описывает только измеренные данные._")
    return "\n".join(lines)


def write_blocked_report(out_dir=None):
    out_dir = Path(out_dir) if out_dir else FIELD_DIR
    status = "BLOCKED: WAITING_FOR_FIELD_TRACK"
    report = {
        "schema": "field_report_v1",
        "status": status,
        "reason": "Реальный GPS-трек полевого проезда ещё не получен.",
        "field_metrics_present": False,
        "needed": [
            "Записать трек проезда по маршруту trip_bundle (GPS-логгер/браузерный recorder).",
            "Экспортировать без сглаживания (.gpx/.nmea/.json).",
            "Импортировать: python -X utf8 offline_pack/field/import_track.py <file>.",
            "Replay: python -X utf8 offline_pack/field/replay_track.py <track.track.json>.",
            "Метрики: python -X utf8 offline_pack/field/metrics_report.py <replay.json> <track.json>.",
        ],
        "baseline_ref": "baseline_report.json",
    }
    (out_dir / "field_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# Field GPS Validation — отчёт",
        "",
        f"**STATUS: {status}**",
        "",
        "Реальный полевой трек ещё не получен и не обработан, поэтому полевых "
        "метрик в отчёте нет. Синтетические результаты за полевые не выдаются.",
        "",
        "## Что нужно для закрытия инкремента",
        "",
    ] + [f"1. {n}" for n in report["needed"]]
    (out_dir / "field_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"отчёт BLOCKED записан: {out_dir / 'field_report.json'}")


def main():
    ap = argparse.ArgumentParser(description="Метрики полевого трека")
    ap.add_argument("replay", nargs="?", help="результат replay_track.py")
    ap.add_argument("track", nargs="?", help="канонический трек")
    ap.add_argument("--bundle", default=str(BUNDLE_DIR))
    args = ap.parse_args()
    if not args.replay or not args.track:
        write_blocked_report()
        return
    replay_result = json.loads(Path(args.replay).read_text(encoding="utf-8-sig"))
    track = json.loads(Path(args.track).read_text(encoding="utf-8-sig"))
    metrics = compute_metrics(track, replay_result, args.bundle)
    meta = {
        "track_id": track.get("track_id"),
        "is_real_car_drive": track.get("is_real_car_drive"),
        "fixes_total": len(track.get("fixes", [])),
    }
    (FIELD_DIR / "field_report.json").write_text(
        json.dumps({"schema": "field_report_v1", "status": "OK",
                    "track_meta": meta, "metrics": metrics},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (FIELD_DIR / "field_report.md").write_text(
        render_markdown(metrics, meta) + "\n", encoding="utf-8")
    print(f"метрики записаны: {FIELD_DIR / 'field_report.json'}")


if __name__ == "__main__":
    main()