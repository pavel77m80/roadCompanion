# -*- coding: utf-8 -*-
"""Калибровка параметров на одном и том же треке (без изменения движка и
исходного бандла).

Сравнивает ограниченный набор вариантов:
- масштаб радиусов триггера (через временную копию бандла);
- порог съезда off_route_threshold_m (через TriggerConfig).
Baseline (исходные параметры) всегда первый и не меняется. Выбирается
минимальное изменение, устраняющее измеренную проблему; при недостатке
данных baseline сохраняется.

Запуск:
  python -X utf8 offline_pack/field/calibrate.py <track.json> [-o calibration_report.json]
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core.playback import TriggerConfig
from field.import_track import import_track
from field.metrics_report import compute_metrics
from field.replay_track import replay

FIELD_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = Path(__file__).resolve().parents[1] / "trip_bundle"

VARIANTS = [
    {"name": "baseline", "note": "исходные параметры (не менять)"},
    {"name": "radius_x0.8", "radius_factor": 0.8},
    {"name": "radius_x1.3", "radius_factor": 1.3},
    {"name": "off_route_200", "off_route_threshold_m": 200.0},
    {"name": "off_route_300", "off_route_threshold_m": 300.0},
]


def _bundle_with_scaled_radii(bundle_dir, factor, variant_name, tmp_root):
    dst = Path(tmp_root) / ("bundle_" + variant_name)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(bundle_dir, dst)
    stories = json.loads((dst / "stories.json").read_text(encoding="utf-8"))
    for s in stories:
        s["trigger_radius_m"] = round(s["trigger_radius_m"] * factor, 1)
    (dst / "stories.json").write_text(
        json.dumps(stories, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst


def run_variant(track, bundle_dir, variant, tmp_root):
    cfg = TriggerConfig()
    used_bundle = Path(bundle_dir)
    if "off_route_threshold_m" in variant:
        cfg.off_route_threshold_m = variant["off_route_threshold_m"]
    if variant.get("radius_factor") not in (None, 1.0):
        used_bundle = _bundle_with_scaled_radii(
            bundle_dir, variant["radius_factor"], variant["name"], tmp_root)
    result = replay(track, used_bundle, config=cfg)
    return result, cfg, used_bundle


def calibrate(track, bundle_dir=BUNDLE_DIR):
    if isinstance(track, (str, Path)):
        track = import_track(track)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for var in VARIANTS:
            result, cfg, used_bundle = run_variant(track, bundle_dir, var, tmp)
            metrics = compute_metrics(track, result, used_bundle)
            justified = metrics["justified_trigger_share"]
            rows.append({
                "variant": var["name"],
                "note": var.get("note", ""),
                "radius_factor": var.get("radius_factor"),
                "off_route_threshold_m": cfg.off_route_threshold_m,
                "expected_trigger_share": metrics["expected_trigger_share"],
                "justified_trigger_share": justified,
                "repeated_triggers": metrics["repeated_triggers"],
                "triggered": metrics["triggered"],
                "score": round(justified if justified is not None else 0.0, 3),
            })
    by_name = {r["variant"]: r for r in rows}
    baseline = by_name["baseline"]
    better = [r for r in rows if r["score"] > baseline["score"] + 0.05]
    recommendation = max(better, key=lambda r: r["score"])["variant"] \
        if better else "baseline (без изменений)"
    return {
        "schema": "field_calibration_v1",
        "track_id": track.get("track_id"),
        "is_real_car_drive": track.get("is_real_car_drive"),
        "rule": "минимальное изменение; baseline сохраняется; параметры не "
                "подгоняются под единственный трек любой ценой",
        "variants": rows,
        "recommendation": recommendation,
        "baseline_preserved": True,
    }


def main():
    ap = argparse.ArgumentParser(description="Калибровка на одном треке")
    ap.add_argument("track", help="канонический трек (field_track_v1)")
    ap.add_argument("--bundle", default=str(BUNDLE_DIR))
    ap.add_argument("-o", "--output", default=str(FIELD_DIR / "calibration_report.json"))
    args = ap.parse_args()
    report = calibrate(import_track(args.track), args.bundle)
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"калибровка: {args.output}")
    for r in report["variants"]:
        print(f"  {r['variant']:<14} score={r['score']} "
              f"expected={r['expected_trigger_share']} "
              f"justified={r['justified_trigger_share']}")
    print(f"  рекомендация: {report['recommendation']}")


if __name__ == "__main__":
    main()