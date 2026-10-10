# -*- coding: utf-8 -*-
import sys

sys.path.insert(0, r"offline_pack\companion_core")
import bundle

try:
    b = bundle.load_bundle(r"offline_pack\trip_bundle")
    print("Валидация: PASS")
    print("Историй:", len(b.stories), "| источников:", len(b.sources),
          "| точек маршрута:", len(b.route_coordinates))
    for s in b.stories:
        print(f"  {s['story_id']} {s['route_distance_m']/1000:5.1f}км "
              f"r={s['trigger_radius_m']}м {s['estimated_duration_seconds']}с {s['title']}")
except bundle.BundleValidationError as e:
    print("FAIL:")
    for x in e.errors:
        print(" -", x)
