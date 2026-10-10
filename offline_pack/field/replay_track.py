# -*- coding: utf-8 -*-
"""Replay реального трека через существующий GPS-движок (без его изменения).

Каждое событие движка привязывается к исходной фиксации: source_index,
timestamp_utc, координаты и точность. Пропущенные/некорректные фиксации
отражаются в skipped_fixes с причиной.

Никаких сетевых запросов (только stdlib + companion_core).
Движок и его параметры НЕ изменяются.

Запуск:
  python -X utf8 offline_pack/field/replay_track.py <track.json> \
      [--bundle DIR] [-o replay_result.json]
"""
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core.bundle import load_bundle
from companion_core.playback import EV_FIX_REJECTED, Fix, TripPlayer, TriggerConfig
from field.import_track import import_track, usable_fixes

DEFAULT_BUNDLE = Path(__file__).resolve().parents[1] / "trip_bundle"


def replay(track, bundle_dir=DEFAULT_BUNDLE, config=None):
    bundle = load_bundle(Path(bundle_dir))
    player = TripPlayer(bundle, config or TriggerConfig())

    skipped = [
        {
            "source_index": i,
            "timestamp_utc": f.get("timestamp_utc"),
            "reason": f.get("skip_reason") or "missing",
        }
        for i, f in enumerate(track.get("fixes", []))
        if f.get("missing") or f.get("timestamp_s") is None
    ]

    events = []
    for src_i, fix in usable_fixes(track):
        eng_events = player.process_fix(Fix(
            lat=fix["latitude"], lon=fix["longitude"],
            accuracy_m=fix.get("horizontal_accuracy_m")
            if fix.get("horizontal_accuracy_m") is not None else 999.0,
            timestamp_s=fix["timestamp_s"],
        ))
        for ev in eng_events:
            events.append({
                **ev,
                "source_index": src_i,
                "timestamp_utc": fix.get("timestamp_utc"),
                "latitude": fix["latitude"],
                "longitude": fix["longitude"],
                "accuracy_m": fix.get("horizontal_accuracy_m"),
            })

    return {
        "schema": "field_replay_v1",
        "track_id": track.get("track_id"),
        "is_real_car_drive": track.get("is_real_car_drive"),
        "engine_config": asdict(player.config),
        "bundle_dir": str(Path(bundle_dir).resolve()),
        "summary": {
            "fixes_total": len(track.get("fixes", [])),
            "fixes_accepted": player.fix_count,
            "fixes_rejected": player.rejected_count,
            "fixes_skipped": len(skipped),
            "events": len(events),
        },
        "status_summary": player.status_summary(),
        "events": events,
        "skipped_fixes": skipped,
    }


def main():
    ap = argparse.ArgumentParser(description="Replay GPS-трека через движок")
    ap.add_argument("track", help="канонический трек (field_track_v1)")
    ap.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    ap.add_argument("-o", "--output", help="результат replay (JSON)")
    args = ap.parse_args()

    track = import_track(args.track) if not args.track.endswith(".track.json") \
        else json.loads(Path(args.track).read_text(encoding="utf-8-sig"))
    result = replay(track, args.bundle)
    out = Path(args.output) if args.output else \
        Path(args.track).with_suffix(".replay.json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    s = result["summary"]
    print(f"replay: {out}")
    print(f"  фиксаций {s['fixes_total']}: принято {s['fixes_accepted']}, "
          f"отклонено {s['fixes_rejected']}, пропущено {s['fixes_skipped']}")
    print(f"  статусы: {result['status_summary']}")
    trg = [e for e in result["events"] if e["type"] == "STORY_TRIGGER"]
    print(f"  триггеров: {len(trg)}")


if __name__ == "__main__":
    main()