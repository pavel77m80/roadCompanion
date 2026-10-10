# -*- coding: utf-8 -*-
"""Общие помощники тестов: синтетический бандл и генератор GPS-фиксаций."""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core.bundle import load_bundle  # noqa: E402
from companion_core.playback import Fix, RouteGeometry  # noqa: E402

SCHEMA = "1.0.0"


def make_bundle(tmp_path, stories=None, route_coords=None, manifest_patch=None):
    """Синтетический валидный бандл в каталоге tmp_path/bundle.

    Координаты — нейтральный тестовый участок, не привязан к реальным местам.
    """
    route_coords = route_coords or [(55.901, 37.401), (55.901, 37.501)]
    stories = stories if stories is not None else [
        make_story("st-001-test", "node/1", lat=55.901, lon=37.451,
                   along=5000, radius=300),
    ]
    sources = []
    for s in stories:
        for f in s["facts"]:
            for sid in f.get("source_ids", []):
                sources.append({"source_id": sid, "type": "test",
                                "license": "CC0", "url": "https://example.test/" + sid})
    manifest = {
        "schema_version": SCHEMA, "bundle_id": "test", "bundle_version": "1.0.0",
        "route": {"file": "route.geojson"},
        "n_stories": len(stories), "language": "ru",
    }
    manifest.update(manifest_patch or {})
    d = Path(tmp_path) / "bundle"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "stories.json").write_text(json.dumps(stories), encoding="utf-8")
    (d / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
    (d / "route.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": {"type": "LineString", "coordinates":
                                   [(lon, lat) for lat, lon in route_coords]}}],
    }), encoding="utf-8")
    return load_bundle(d)


def make_story(story_id, poi_id, lat, lon, along, radius=300, facts=None):
    return {
        "story_id": story_id, "poi_id": poi_id, "title": "T " + story_id,
        "short_summary": "s", "spoken_text": "слово " * 40,
        "estimated_duration_seconds": 20,
        "coordinates": {"lat": lat, "lon": lon},
        "route_distance_m": along, "trigger_radius_m": radius,
        "facts": facts or [{"text": "тестовый факт", "kind": "fact",
                            "source_ids": ["osm:node/1"]}],
        "sources": ["osm:node/1"], "uncertainties": [],
        "content_language": "ru", "content_version": SCHEMA,
    }


def point_at_progress(route_coords, progress_m):
    """Точка (lat, lon) на полилинии на заданном расстоянии от старта."""
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(route_coords, route_coords[1:]):
        seg = math.hypot((lat2 - lat1) * 111320.0,
                         (lon2 - lon1) * 111320.0 * math.cos(math.radians(lat1)))
        if total + seg >= progress_m:
            t = 0 if seg == 0 else (progress_m - total) / seg
            return (lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)
        total += seg
    return route_coords[-1]


def drive(player, from_m, to_m, step=25.0, speed=10.0, t0=None,
          accuracy=10.0, offset_m=0.0):
    """Прогоняет по маршруту фикции от from_m до to_m (метрика движка).
    offset_m — перпендикулярное смещение для имитации съезда.
    t0=None продолжает временную шкалу игрока автоматически."""
    events = []
    if t0 is None:
        t0 = (player.last_ts + step / speed) if player.last_ts is not None else 0.0
    t = t0
    d = from_m
    direction = 1 if to_m >= from_m else -1
    geo = player.geometry
    while (to_m - d) * direction > 1e-6:
        lat, lon = geo.point_at(d, offset_m=offset_m)
        events += player.process_fix(
            Fix(lat=lat, lon=lon, accuracy_m=accuracy, timestamp_s=t))
        t += step / speed
        d += step * direction
    return events
