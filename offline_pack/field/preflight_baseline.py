# -*- coding: utf-8 -*-
"""Фиксирует BASELINE инкремента Field GPS Validation.

Снимок: параметры TriggerConfig (значения по умолчанию из playback.py),
радиусы историй и маршрут из trip_bundle, манифест бандла и существующий
лог синтетической симуляции (как референс, а не полевые данные).

Ничего не меняет: только читает текущие файлы.
"""
import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core import playback
from companion_core.bundle import load_bundle

OUT = Path(__file__).resolve().parent / "baseline_report.json"


def sha256_hex(path):
    import hashlib
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def main():
    bundle_dir = Path(__file__).resolve().parents[1] / "trip_bundle"
    bundle = load_bundle(bundle_dir)

    sim_log = Path(__file__).resolve().parents[1] / "simulation_log.json"
    sim = json.loads(sim_log.read_text(encoding="utf-8"))

    report = {
        "schema": "baseline_report_v1",
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "engine": dataclasses.asdict(playback.TriggerConfig()),
        "engine_module": {
            "path": str(Path(playback.__file__).resolve()),
            "sha256": sha256_hex(Path(playback.__file__)),
            "bundle_module_sha256": sha256_hex(
                Path(__file__).resolve().parents[1] / "companion_core" / "bundle.py"),
        },
        "bundle": {
            "dir": str(bundle_dir.resolve()),
            "manifest": {
                "bundle_id": bundle.manifest.get("bundle_id"),
                "bundle_version": bundle.manifest.get("bundle_version"),
                "route_distance_m": bundle.manifest.get("route", {}).get("distance_m"),
                "corridor_radius_m": bundle.manifest.get("corridor_radius_m"),
                "n_stories": bundle.manifest.get("n_stories"),
            },
            "stories": [
                {
                    "story_id": s["story_id"],
                    "poi_id": s["poi_id"],
                    "route_distance_m": s["route_distance_m"],
                    "trigger_radius_m": s["trigger_radius_m"],
                }
                for s in bundle.stories
            ],
        },
        "reference_simulation_is_synthetic_not_field": True,
        "reference_simulation": {
            "is_real_car_drive": sim.get("is_real_car_drive"),
            "stories_triggered": sim.get("stories_triggered"),
            "status_summary": sim.get("status_summary"),
        },
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"baseline сохранён: {OUT}")
    print(f"  TriggerConfig: {report['engine']}")
    print(f"  историй {len(report['bundle']['stories'])}, "
          f"маршрут {report['bundle']['manifest']['route_distance_m']} м")
    print("  референс-симуляция (SYNTHETIC):",
          f"{sim.get('stories_triggered')} триггеров, {sim.get('status_summary')}")


if __name__ == "__main__":
    main()