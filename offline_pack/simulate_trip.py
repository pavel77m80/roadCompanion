# -*- coding: utf-8 -*-
"""Воспроизводимый сценарий поездки по реальному бандлу trip_bundle.

ПРОИЗВОДИТ СИНТЕТИЧЕСКИЕ GPS-ФИКСАЦИИ — это НЕ поездка на реальном автомобиле.
Сценарий: подъезд к первой истории и остановка, проезд вперёд, съезд с
маршрута с возвращением, пачка неточных фиксаций (отбраковка), финиш.

Запуск:  python -X utf8 offline_pack/simulate_trip.py
Результат: offline_pack/simulation_log.json + сводка в stdout.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from companion_core.bundle import load_bundle
from companion_core.playback import (EV_FIX_REJECTED, EV_MISSED, EV_OFF_ROUTE,
                                     EV_RESUME, EV_TRIP_END, EV_TRIGGER,
                                     Fix, TripPlayer, TriggerConfig)

BUNDLE_DIR = Path(__file__).resolve().parent / "trip_bundle"


def config():
    return TriggerConfig()


class FixStream:
    """Накапливает синтетические фиксации и общее время сценария."""

    def __init__(self, geo):
        self.geo = geo
        self.t = 1000.0
        self.fixes = []

    def drive(self, to_m, step=25.0, speed=10.0, offset_m=0.0, accuracy=10.0,
              from_m=None):
        """Проезд от текущего прогресса (или from_m) до to_m."""
        d = self.geo.total_m if from_m is None else min(float(from_m), self.geo.total_m)
        while d is None:
            pass
        if from_m is None:
            # расстояние считаем от последней добавленной фиксации
            if not self.fixes:
                raise ValueError("нет начальной позиции: укажите from_m")
            last = self.fixes[-1]
            d = min(self.geo.measure(last.lat, last.lon)[1], self.geo.total_m)
        to_m = min(float(to_m), self.geo.total_m)
        direction = 1 if to_m >= d else -1
        while (to_m - d) * direction > 1e-6:
            lat, lon = self.geo.point_at(d, offset_m=offset_m)
            self.fixes.append(Fix(lat, lon, accuracy, self.t))
            self.t += step / speed
            d += step * direction
        lat, lon = self.geo.point_at(d, offset_m=offset_m)
        self.fixes.append(Fix(lat, lon, accuracy, self.t))
        self.t += step / speed

    def loiter(self, at_m, n=10, period=0.8, accuracy=10.0):
        lat, lon = self.geo.point_at(at_m)
        for i in range(n):
            self.fixes.append(Fix(lat, lon, accuracy, self.t + i * period))
        self.t += n * period

    def noisy(self, at_m, n=6, accuracy=120.0, period=1.0):
        """Пачка неточных фиксаций — должны отбраковываться."""
        lat, lon = self.geo.point_at(at_m)
        for i in range(n):
            self.fixes.append(
                Fix(lat + i * 2e-4, lon, accuracy, self.t + i * period))
        self.t += n * period


def run():
    bundle = load_bundle(BUNDLE_DIR)
    player = TripPlayer(bundle, config())
    geo = player.geometry
    stream = FixStream(geo)

    first = bundle.stories[0]
    stop_at = max(first["route_distance_m"] - 120.0, 25.0)

    # 1) подъезд к первой истории и "остановка событием"
    stream.drive(stop_at, from_m=0.0)
    stream.loiter(stop_at, n=10)

    # 2) основная дорога вперёд
    detour_from, detour_to = 24200.0, 25100.0
    stream.drive(detour_from - 100.0)

    # 3) съезд с маршрута и возвращение на линию
    stream.drive(detour_to, offset_m=300.0)
    stream.drive(geo.total_m)

    # 4) пачка неточных фиксаций (после финиша — в отброшенные)
    stream.noisy(geo.total_m * 0.98)

    events = []
    for f in stream.fixes:
        events += player.process_fix(f)

    triggers = [e for e in events if e["type"] == EV_TRIGGER]
    rejected = [e for e in events if e["type"] == EV_FIX_REJECTED]
    offroute = [e for e in events if e["type"] == EV_OFF_ROUTE]
    resume = [e for e in events if e["type"] == EV_RESUME]
    missed = [e for e in events if e["type"] == EV_MISSED]
    end = [e for e in events if e["type"] == EV_TRIP_END]

    log = {
        "schema": "simulation_log_v1",
        "is_real_car_drive": False,
        "bundle": BUNDLE_DIR.name,
        "route_distance_m": round(geo.total_m, 1),
        "fix_count": player.fix_count,
        "rejected_count": player.rejected_count,
        "stories_total": len(bundle.stories),
        "stories_triggered": len(triggers),
        "stories_missed": len(missed),
        "triggered_ids": [e["story_id"] for e in triggers],
        "offroute_events": len(offroute),
        "resume_events": len(resume),
        "trip_end_events": len(end),
        "rejected_events": len(rejected),
        "status_summary": player.status_summary(),
        "events": [e for e in events if e["type"] != EV_FIX_REJECTED],
    }
    (Path(__file__).resolve().parent / "simulation_log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"СИМУЛЯЦИЯ ({BUNDLE_DIR.name}) — синтетические GPS-фиксации, не реальная поездка")
    print(f"  маршрут: {log['route_distance_m']} м, принято {player.fix_count}, "
          f"отброшено {player.rejected_count}")
    print(f"  истории: {log['stories_triggered']}/{log['stories_total']} триггер, "
          f"{log['stories_missed']} MISSED, съездов {log['offroute_events']}, "
          f"возвратов {log['resume_events']}, финишей {log['trip_end_events']}")
    print(f"  статусы: {log['status_summary']}")
    print("  лог: simulation_log.json")


if __name__ == "__main__":
    run()