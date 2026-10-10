# -*- coding: utf-8 -*-
"""Тесты инструментов полевой проверки GPS (Field GPS Validation).

Все треки здесь СИНТЕТИЧЕСКИЕ и помечены is_real_car_drive подконтрольно;
это тесты инструментария импорта/replay/метрик, а не полевые результаты.
"""
import json
import math
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core.bundle import load_bundle
from companion_core.playback import RouteGeometry, TriggerConfig
from field.calibrate import calibrate
from field.import_track import (SCHEMA, import_track, parse_gpx, parse_nmea,
                                usable_fixes)
from field.metrics_report import compute_metrics
from field.replay_track import replay
from tests.helpers import make_bundle, make_story, point_at_progress

FIELD_DIR = Path(__file__).resolve().parents[1] / "field"


_TMP_KEEP = tempfile.TemporaryDirectory(prefix="field-test-bundle-")


def _synthetic_bundle_dir():
    """Валидный синтетический бандл без привязки к личному маршруту.

    Координаты — нейтральный тестовый участок, не связан с реальной поездкой.
    """
    _COS = math.cos(math.radians(55.901))
    line = [(55.901, 37.401 + i * 900.0 / (111320.0 * _COS)) for i in range(12)]
    stories = [
        make_story(f"st-{i:03d}-s", f"node/{i+1}", *point_at_progress(line, 750.0 * i),
                   along=750.0 * i, radius=300,
                   facts=[{"text": "тестовый факт", "kind": "fact",
                           "source_ids": [f"osm:node/{i+1}"]}])
        for i in range(12)
    ]
    return Path(make_bundle(_TMP_KEEP.name, stories, line).path)


BUNDLE_DIR = _synthetic_bundle_dir()


def _route_fixes(step=40.0, speed=10.0, n=None):
    """Трек вдоль синтетического маршрута тестового бандла (монотонные метки)."""
    from datetime import datetime, timedelta, timezone
    bundle = load_bundle(BUNDLE_DIR)
    geo = RouteGeometry(bundle.route_coordinates)
    base = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)
    fixes = []
    d, t = 0.0, 0.0
    while d < geo.total_m:
        lat, lon = geo.point_at(d)
        iso = (base + timedelta(seconds=t)).isoformat()
        fixes.append((iso, lat, lon, 5.0, speed))
        d += step
        t += step / speed
    return geo, fixes


def make_gpx(points, with_acc=True):
    rows = ['<?xml version="1.0"?>',
            '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">',
            '<trk><trkseg>']
    for iso, lat, lon, acc, spd in points:
        rows.append(f'<trkpt lat="{lat}" lon="{lon}">'
                    f'<time>{iso}</time>'
                    + (f'<hdop>{acc / 5.0:.2f}</hdop>' if with_acc else "")
                    + f'<extensions><speed>{spd}</speed></extensions>'
                    f'</trkpt>')
    rows += ['</trkseg></trk></gpx>']
    return "\n".join(rows)


def make_track(points, track_id="t1", real=True):
    base = points[0][0]
    from datetime import datetime
    t0 = datetime.fromisoformat(base)
    fixes = []
    for iso, lat, lon, acc, spd in points:
        ts = (datetime.fromisoformat(iso) - t0).total_seconds()
        fixes.append({
            "timestamp_utc": iso, "timestamp_s": ts,
            "latitude": lat, "longitude": lon,
            "horizontal_accuracy_m": acc, "speed_mps": spd,
            "bearing_deg": None, "missing": False, "skip_reason": None,
        })
    return {
        "schema": SCHEMA, "track_id": track_id, "is_real_car_drive": real,
        "source": {"tool": "test", "original_format": "json", "original_file": None},
        "privacy": {"contains_personal_data": False,
                    "send_to_external_services": False},
        "route_notes": {"same_route_as_bundle": True, "deviations": None},
        "fixes": fixes,
    }


class TestGpxImport(unittest.TestCase):
    def test_gpx_roundtrip_preserves_time_and_accuracy(self):
        _, pts = _route_fixes(step=200.0, speed=10.0)
        gpx = make_gpx(pts)
        track = parse_gpx(gpx, "gpx1")
        self.assertEqual(track["schema"], SCHEMA)
        self.assertEqual(len(track["fixes"]), len(pts))
        first = track["fixes"][0]
        self.assertAlmostEqual(first["latitude"], pts[0][1], places=6)
        self.assertAlmostEqual(first["horizontal_accuracy_m"], 5.0, places=1)
        self.assertEqual(first["timestamp_utc"], pts[0][0])
        self.assertAlmostEqual(first["timestamp_s"], 0.0, places=3)
        # интервалы между фиксациями сохранены (200 м / 10 м/с = 20 с)
        self.assertAlmostEqual(track["fixes"][1]["timestamp_s"], 20.0, places=1)

    def test_gpx_import_from_file(self):
        _, pts = _route_fixes(step=500.0)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "field.gpx"
            p.write_text(make_gpx(pts), encoding="utf-8")
            track = import_track(p)
            self.assertEqual(track["source"]["original_format"], "gpx")
            self.assertGreater(len(track["fixes"]), 0)


class TestNmeaImport(unittest.TestCase):
    def test_gga_rmc_merged_into_fixes(self):
        nmea = (
            "$GPRMC,120000.00,A,5545.0746,N,03737.1054,E,16.0,224.0,111026,,*00\n"
            "$GPGGA,120000.00,5545.0746,N,03737.1054,E,1,08,1.0,250.0,M,,M,,*00\n"
            "$GPRMC,120005.00,A,5545.0800,N,03737.1200,E,16.0,225.0,111026,,*00\n"
            "$GPGGA,120005.00,5545.0800,N,03737.1200,E,1,08,1.5,250.0,M,,M,,*00\n"
        )
        track = parse_nmea(nmea, "nmea1")
        self.assertEqual(len(track["fixes"]), 2)
        f0 = track["fixes"][0]
        self.assertAlmostEqual(f0["latitude"], 55.751243, places=5)
        self.assertAlmostEqual(f0["longitude"], 37.618423, places=5)
        # HDOP 1.0 -> ~5 м
        self.assertAlmostEqual(f0["horizontal_accuracy_m"], 5.0, places=1)
        # 16 узлов -> ~8.23 м/с
        self.assertAlmostEqual(f0["speed_mps"], 8.23, places=1)

    def test_nmea_no_fix_quality_is_missing_not_zero(self):
        nmea = "$GPGGA,120000.00,,,,,0,00,99.9,,,,,,*00\n"
        track = parse_nmea(nmea, "nmea2")
        self.assertTrue(track["fixes"] == [] or
                        all(f["latitude"] is None for f in track["fixes"]))


class TestMissingAndRejection(unittest.TestCase):
    def test_missing_coordinates_not_turned_into_zero(self):
        gpx = ('<?xml version="1.0"?><gpx><trk><trkseg>'
               '<trkpt><time>2026-10-11T07:00:00Z</time></trkpt>'
               '<trkpt lat="56.9" lon="60.4"><time>2026-10-11T07:00:05Z</time></trkpt>'
               '</trkseg></trk></gpx>')
        track = parse_gpx(gpx, "m1")
        self.assertTrue(track["fixes"][0]["missing"])
        self.assertIsNone(track["fixes"][0]["latitude"])
        self.assertNotEqual(track["fixes"][0]["latitude"], 0.0)
        usable = list(usable_fixes(track))
        self.assertEqual(len(usable), 1)

    def test_non_monotonic_time_flagged(self):
        pts = [
            ("2026-10-11T07:00:00+00:00", 56.900, 60.400, 5.0, 10.0),
            ("2026-10-11T07:00:05+00:00", 56.901, 60.401, 5.0, 10.0),
            ("2026-10-11T07:00:02+00:00", 56.902, 60.402, 5.0, 10.0),
        ]
        track = make_track(pts)
        # искусственно портим третий timestamp_s, как это сделал бы парсер
        track["fixes"][2]["timestamp_s"] = 2.0
        from field.import_track import _assemble
        # повторная сборка помечает немонотонность
        rebuilt = _assemble([dict(f) for f in track["fixes"]], "x", "json")
        self.assertTrue(rebuilt["fixes"][2]["missing"])
        self.assertEqual(rebuilt["fixes"][2]["skip_reason"], "non_monotonic_time")


class TestReplay(unittest.TestCase):
    def test_large_gaps_between_fixes_are_tolerated(self):
        # большие интервалы времени (по 5 минут) не должны ломать движок
        pts = []
        _, route = _route_fixes(step=2000.0, speed=10.0)
        for i, (iso, lat, lon, acc, spd) in enumerate(route):
            # переписываем время с шагом 300 с вместо 200 с эквивалента — крупный разрыв
            pass
        geo = RouteGeometry(load_bundle(BUNDLE_DIR).route_coordinates)
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)
        for i in range(0, int(geo.total_m), 3000):
            lat, lon = geo.point_at(i)
            iso = (base + timedelta(seconds=i / 10.0 * 3)).isoformat()
            pts.append((iso, lat, lon, 6.0, 10.0))
        track = make_track(pts)
        result = replay(track, BUNDLE_DIR)
        self.assertGreater(result["summary"]["fixes_accepted"], 0)
        # большие интервалы не превращаются в teleport-отказы при разумной скорости
        self.assertEqual(result["summary"]["fixes_rejected"], 0)

    def test_inaccurate_fixes_are_rejected(self):
        geo = RouteGeometry(load_bundle(BUNDLE_DIR).route_coordinates)
        from datetime import datetime, timedelta, timezone
        base = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)
        pts = []
        for i in range(0, int(geo.total_m), 200):
            lat, lon = geo.point_at(i)
            iso = (base + timedelta(seconds=i / 10.0)).isoformat()
            pts.append((iso, lat, lon, 200.0, 10.0))  # точность хуже порога 50 м
        track = make_track(pts)
        result = replay(track, BUNDLE_DIR)
        self.assertEqual(result["summary"]["fixes_accepted"], 0)
        self.assertGreater(result["summary"]["fixes_rejected"], 0)
        self.assertEqual([e for e in result["events"] if e["type"] == "STORY_TRIGGER"], [])

    def test_event_tied_to_source_fix(self):
        _, pts = _route_fixes(step=40.0, speed=10.0)
        track = make_track(pts)
        result = replay(track, BUNDLE_DIR)
        triggers = [e for e in result["events"] if e["type"] == "STORY_TRIGGER"]
        self.assertGreater(len(triggers), 0)
        for e in triggers:
            self.assertIn("source_index", e)
            self.assertIn("timestamp_utc", e)
            src = track["fixes"][e["source_index"]]
            self.assertEqual(e["timestamp_utc"], src["timestamp_utc"])
            self.assertAlmostEqual(e["latitude"], src["latitude"], places=6)

    def test_replay_is_reproducible(self):
        _, pts = _route_fixes(step=40.0, speed=10.0)
        track = make_track(pts)
        r1 = replay(track, BUNDLE_DIR)
        r2 = replay(track, BUNDLE_DIR)
        self.assertEqual(r1["events"], r2["events"])
        self.assertEqual(r1["status_summary"], r2["status_summary"])
        self.assertEqual(r1["summary"], r2["summary"])

    def test_replay_makes_no_network_calls(self):
        _, pts = _route_fixes(step=200.0)
        track = make_track(pts)
        with mock.patch.object(socket, "socket",
                               side_effect=AssertionError("network call!")):
            result = replay(track, BUNDLE_DIR)
        self.assertGreater(result["summary"]["fixes_accepted"], 0)


class TestMetricsAndCalibration(unittest.TestCase):
    def test_metrics_classify_by_actual_proximity(self):
        _, pts = _route_fixes(step=40.0, speed=10.0)
        track = make_track(pts)
        result = replay(track, BUNDLE_DIR)
        metrics = compute_metrics(track, result, BUNDLE_DIR)
        self.assertIn("per_poi", metrics)
        self.assertEqual(len(metrics["per_poi"]), 12)
        classes = metrics["poi_classes"]
        # трек идёт по самому маршруту: большинство POI должны быть passed_near
        self.assertGreater(classes.get("passed_near", 0), 0)
        # метрика "12 из 12" не подразумевается автоматически
        self.assertLessEqual(metrics["triggered"], 12)

    def test_calibration_preserves_baseline(self):
        _, pts = _route_fixes(step=200.0, speed=10.0)
        track = make_track(pts)
        report = calibrate(track, BUNDLE_DIR)
        names = [v["variant"] for v in report["variants"]]
        self.assertEqual(names[0], "baseline")
        self.assertTrue(report["baseline_preserved"])
        base = next(v for v in report["variants"] if v["variant"] == "baseline")
        self.assertEqual(base["off_route_threshold_m"],
                         TriggerConfig().off_route_threshold_m)

    def test_blocked_report_when_no_track(self):
        # метрики без реального трека не фабрикуются: статус BLOCKED
        import tempfile
        import field.metrics_report as mr
        with tempfile.TemporaryDirectory() as td:
            mr.write_blocked_report(td)
            rep = json.loads((Path(td) / "field_report.json").read_text(encoding="utf-8"))
            self.assertTrue(rep["status"].startswith("BLOCKED"))
            self.assertFalse(rep["field_metrics_present"])


if __name__ == "__main__":
    unittest.main()