# -*- coding: utf-8 -*-
"""Тесты GPS playback engine на синтетическом и реальном бандлах."""
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core.bundle import load_bundle
from companion_core.playback import (EV_FIX_REJECTED, EV_MISSED, EV_OFF_ROUTE,
                                     EV_RESUME, EV_TRIGGER, EV_TRIP_END,
                                     Fix, RouteGeometry, TripPlayer, TriggerConfig)
from tests.helpers import drive, make_bundle, make_story, point_at_progress

# Прямой синтетический маршрут ~9.9 км (12 точек через 900 м).
# Координаты — нейтральный тестовый участок, не привязан к реальным местам.
_COS = math.cos(math.radians(55.901))
LINE = [(55.901, 37.401 + i * 900.0 / (111320.0 * _COS)) for i in range(12)]


def cfg(**kw):
    base = dict(off_route_threshold_m=250, off_route_fixes=3,
                retrigger_cooldown_s=600)
    base.update(kw)
    return TriggerConfig(**base)


def story_at(progress, radius=300, along=None, offset_m=0.0):
    lat, lon = point_at_progress(LINE, progress)
    if offset_m:
        lat += offset_m / 111320.0
    return make_story("st-001-x", "node/1", lat, lon,
                      along if along is not None else progress, radius=radius)


class TestTriggerBasics(unittest.TestCase):
    def test_entering_radius_triggers_exactly_one_event(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            events = drive(player, 0, 7000, step=50)
            triggers = [e for e in events if e["type"] == EV_TRIGGER]
            self.assertEqual(len(triggers), 1)
            self.assertEqual(triggers[0]["story_id"], "st-001-x")

    def test_passing_by_triggers_at_most_once(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            drive(player, 0, 7000, step=50)       # туда
            drive(player, 7000, 3000, step=50)    # назад через радиус
            drive(player, 3000, 7000, step=50)    # снова вперёд
            triggers = [e for e in player.events_log if e["type"] == EV_TRIGGER]
            self.assertEqual(len(triggers), 1)

    def test_loitering_in_radius_creates_no_storm(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            drive(player, 4700, 5100, step=20)    # вход в радиус и остановка
            lat, lon = point_at_progress(LINE, 5100)
            for i in range(20):                          # 20 повторных фиксаций
                player.process_fix(Fix(lat, lon, 8, 600 + i))
            triggers = [e for e in player.events_log if e["type"] == EV_TRIGGER]
            self.assertEqual(len(triggers), 1)

    def test_inaccurate_fixes_rejected_without_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            events = drive(player, 0, 5000, step=50, accuracy=150)
            self.assertTrue(events)
            self.assertEqual(len([e for e in events if e["type"] == EV_FIX_REJECTED]),
                             len(events))
            self.assertEqual([e for e in events if e["type"] == EV_TRIGGER], [])
            lat, lon = point_at_progress(LINE, 5000)
            events2 = player.process_fix(Fix(lat, lon, 10, 600))
            self.assertTrue(any(e["type"] == EV_TRIGGER for e in events2))

    def test_teleport_jump_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            drive(player, 0, 1000, step=50)
            far = point_at_progress(LINE, 9500)
            # dt = 10 c, прыжок ~8.5 км -> 850 м/с >> порога 120 м/с
            events = player.process_fix(Fix(far[0], far[1], 10, 110))
            self.assertTrue(any(e["type"] == EV_FIX_REJECTED and
                                e.get("reason") == "teleport_jump" for e in events))


class TestMissedAndCorridor(unittest.TestCase):
    def test_story_missed_when_passed_outside_radius(self):
        # POI в 250 м от дороги при радиусе 100 м: не срабатывает, метится MISSED
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000, radius=100,
                                                          offset_m=250)], LINE), cfg())
            events = drive(player, 4000, 6200, step=200)
            self.assertEqual(len([e for e in events if e["type"] == EV_MISSED]), 1)
            # позднее возвращение в радиус не срабатывает
            drive(player, 6200, 4900, step=100)
            self.assertEqual([e for e in player.events_log
                              if e["type"] == EV_TRIGGER], [])

    def test_poi_outside_corridor_never_triggers(self):
        # озеро в 1745 м к северу от маршрута — вне коридора и радиуса
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000, radius=300,
                                                          offset_m=1745)], LINE), cfg())
            drive(player, 0, 9900, step=50)
            self.assertEqual([e for e in player.events_log
                              if e["type"] == EV_TRIGGER], [])
            self.assertEqual(player.status_summary().get("MISSED"), 1)


class TestOffRoute(unittest.TestCase):
    def test_detour_past_story_marks_missed_predictably(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            drive(player, 0, 4000, step=50)
            events = drive(player, 4000, 6000, step=50, offset_m=400)
            off = [e for e in events if e["type"] == EV_OFF_ROUTE]
            self.assertEqual(len(off), 1, "съезд должен объявляться один раз")
            # в съезде триггеры и решения заморожены
            self.assertEqual([e for e in events if e["type"] == EV_TRIGGER], [])
            self.assertEqual(player.status_summary().get("PENDING"), 1)
            # возвращение на линию: история осталась позади -> MISSED
            events2 = drive(player, 6000, 6300, step=50, offset_m=0)
            self.assertTrue(any(e["type"] == EV_RESUME for e in events2))
            self.assertEqual(player.status_summary().get("MISSED"), 1)
            # поздний триггер при движении назад невозможен
            events3 = drive(player, 6300, 5200, step=50)
            self.assertEqual([e for e in events3 if e["type"] == EV_TRIGGER], [])

    def test_detour_before_story_allows_trigger_after_rejoin(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            drive(player, 0, 4000, step=50)
            events = drive(player, 4000, 4600, step=50, offset_m=400)
            self.assertTrue(any(e["type"] == EV_OFF_ROUTE for e in events))
            self.assertEqual(player.status_summary().get("PENDING"), 1)
            events2 = drive(player, 4600, 4900, step=50, offset_m=0)
            self.assertTrue(any(e["type"] == EV_RESUME for e in events2))
            events3 = drive(player, 4900, 5100, step=50)
            # после возврата на линию история снова достижима и срабатывает ровно раз
            triggers = [e for e in (events2 + events3) if e["type"] == EV_TRIGGER]
            self.assertEqual(len(triggers), 1)
            self.assertEqual(player.status_summary().get("TRIGGERED"), 1)

    def test_short_deviation_does_not_trip_offroute(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            events = drive(player, 0, 4600, step=50, offset_m=120)
            self.assertEqual([e for e in events if e["type"] == EV_OFF_ROUTE], [])


class TestStatePersistence(unittest.TestCase):
    def test_reopen_preserves_state(self):
        with tempfile.TemporaryDirectory() as td:
            b = make_bundle(td, [story_at(5000)], LINE)
            p1 = TripPlayer(b, cfg())
            drive(p1, 0, 5200, step=50)
            self.assertEqual(p1.status_summary().get("TRIGGERED"), 1)
            state = p1.export_state()
            p2 = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            p2.restore_state(state)
            self.assertEqual(p2.status_summary().get("TRIGGERED"), 1)
            drive(p2, 5200, 5600, step=50)
            drive(p2, 5600, 4900, step=50)   # снова через радиус
            self.assertEqual([e for e in p2.events_log
                              if e["type"] == EV_TRIGGER], [])

    def test_mark_completed_and_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            player = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            drive(player, 0, 5200, step=50)
            player.mark_completed("st-001-x")
            self.assertEqual(player.status_summary().get("COMPLETED"), 1)
            player2 = TripPlayer(make_bundle(td, [story_at(5000)], LINE), cfg())
            drive(player2, 0, 5200, step=50)
            player2.mark_skipped("st-001-x")
            self.assertEqual(player2.status_summary().get("SKIPPED"), 1)


_BUNDLE_EXISTS = (Path(__file__).resolve().parents[1] / "trip_bundle").is_dir()


@unittest.skipUnless(_BUNDLE_EXISTS,
                     "trip_bundle не входит в git-репозиторий (личные данные); "
                     "бандл собирается локально")
class TestRealBundleEndToEnd(unittest.TestCase):
    """Сквозная СИМУЛЯЦИЯ поездки по реальному маршруту trip_bundle.

    Это тест на синтетических GPS-фиксациях, а не поездка на реальном
    автомобиле; проверяет детерминированность ядра.
    """

    def test_full_drive_triggers_each_story_once(self):
        b = load_bundle(Path(__file__).resolve().parents[1] / "trip_bundle")
        player = TripPlayer(b, cfg())
        total = player.geometry.total_m
        events = drive(player, 0, total, step=25, speed=10.0)
        triggers = [e for e in events if e["type"] == EV_TRIGGER]
        self.assertEqual(len(triggers), len(b.stories),
                         f"ожидался триггер для каждой истории, получено {len(triggers)}")
        ids = [t["story_id"] for t in triggers]
        self.assertEqual(len(ids), len(set(ids)))
        pos = {s["story_id"]: s["route_distance_m"] for s in b.stories}
        alongs = [pos[i] for i in ids]
        self.assertEqual(alongs, sorted(alongs), "порядок триггеров вдоль маршрута")
        self.assertTrue(any(e["type"] == EV_TRIP_END for e in events))


if __name__ == "__main__":
    unittest.main()
