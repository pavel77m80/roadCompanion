# -*- coding: utf-8 -*-
"""Тесты офлайн-бандла: целостность, источники, секреты, работа без сети."""
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_core import bundle as bundle_mod
from companion_core.bundle import BundleValidationError, load_bundle, validate_bundle

REAL_BUNDLE = Path(__file__).resolve().parents[1] / "trip_bundle"
BUNDLE_EXISTS = REAL_BUNDLE.is_dir()

FORBIDDEN_MODULES = ("requests", "urllib", "socket", "http.client", "urllib3")


@unittest.skipUnless(BUNDLE_EXISTS,
                     "trip_bundle не входит в git-репозиторий (личные данные); "
                     "бандл собирается локально")
class TestValidBundle(unittest.TestCase):
    def test_real_bundle_loads(self):
        b = load_bundle(REAL_BUNDLE)
        self.assertEqual(b.manifest["schema_version"], bundle_mod.SUPPORTED_SCHEMA)
        self.assertGreaterEqual(len(b.stories), 10)
        self.assertGreaterEqual(len(b.sources), 10)
        self.assertGreaterEqual(len(b.route_coordinates), 2)

    def test_required_fields_and_order(self):
        b = load_bundle(REAL_BUNDLE)
        along = -1
        for s in b.stories:
            for field in bundle_mod.STORY_REQUIRED_FIELDS:
                self.assertIn(field, s, s["story_id"])
            self.assertGreaterEqual(s["route_distance_m"], along)
            along = s["route_distance_m"]
            kinds = {f["kind"] for f in s["facts"]}
            self.assertTrue(kinds <= set(bundle_mod.FACT_KINDS))

    def test_every_confirmed_fact_has_known_source(self):
        b = load_bundle(REAL_BUNDLE)
        for s in b.stories:
            for f in s["facts"]:
                if f["kind"] == "fact":
                    self.assertTrue(f["source_ids"], s["story_id"])
                    for sid in f["source_ids"]:
                        self.assertIn(sid, b.sources)

    def test_wikipedia_attribution_present(self):
        b = load_bundle(REAL_BUNDLE)
        wp = [s for s in b.sources.values() if s["type"] == "wikipedia-article"]
        self.assertTrue(wp, "нет Wikipedia-источников в бандле")
        for s in wp:
            self.assertIn("CC BY-SA", s["license"])
            self.assertTrue(s.get("authors"))
            self.assertTrue(s["url"].startswith("https://"))

    def test_no_secrets_in_bundle(self):
        # валидатор сам сканирует секретоподобные шаблоны
        manifest, stories, sources, route = validate_bundle(REAL_BUNDLE)
        self.assertTrue(manifest and stories and sources and route)


@unittest.skipUnless(BUNDLE_EXISTS,
                     "trip_bundle не входит в git-репозиторий (личные данные); "
                     "бандл собирается локально")
class TestCorruptedBundle(unittest.TestCase):
    def _copy_and(self, edit):
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "bundle"
            dst.mkdir()
            for f in ("manifest.json", "stories.json", "sources.json", "route.geojson"):
                (dst / f).write_text((REAL_BUNDLE / f).read_text(encoding="utf-8"),
                                     encoding="utf-8")
            edit(dst)
            with self.assertRaises(BundleValidationError) as ctx:
                load_bundle(dst)
            return ctx.exception.errors

    def test_truncated_manifest_rejected(self):
        def edit(d):
            (d / "manifest.json").write_text("{broken json", encoding="utf-8")
        errors = self._copy_and(edit)
        self.assertTrue(any("повреждён" in e or "manifest" in e for e in errors))

    def test_bad_schema_version_rejected(self):
        def edit(d):
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            m["schema_version"] = "9.9.9"
            (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        errors = self._copy_and(edit)
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_missing_required_file_rejected(self):
        def edit(d):
            (d / "stories.json").unlink()
        errors = self._copy_and(edit)
        self.assertTrue(any("stories.json" in e for e in errors))

    def test_missing_source_reference_detected(self):
        def edit(d):
            st = json.loads((d / "stories.json").read_text(encoding="utf-8"))
            st[0]["facts"][0]["source_ids"] = ["osm:nonexistent/0"]
            (d / "stories.json").write_text(json.dumps(st), encoding="utf-8")
        errors = self._copy_and(edit)
        self.assertTrue(any("неизвестный источник" in e for e in errors))

    def test_unsourced_confirmed_fact_detected(self):
        def edit(d):
            st = json.loads((d / "stories.json").read_text(encoding="utf-8"))
            st[0]["facts"][0]["source_ids"] = []
            (d / "stories.json").write_text(json.dumps(st), encoding="utf-8")
        errors = self._copy_and(edit)
        self.assertTrue(any("без источника" in e for e in errors))

    def test_secret_pattern_detected(self):
        def edit(d):
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            m["injected"] = "ORS_API_KEY=sk-secret123"
            (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        errors = self._copy_and(edit)
        self.assertTrue(any("секретоподобный" in e for e in errors))


class TestNoNetwork(unittest.TestCase):
    def test_core_modules_do_not_import_network(self):
        for mod_name in ("companion_core.bundle", "companion_core.playback"):
            mod = sys.modules.get(mod_name) or __import__(mod_name, fromlist=["x"])
            src = Path(mod.__file__).read_text(encoding="utf-8")
            for bad in FORBIDDEN_MODULES:
                self.assertNotIn(f"import {bad}", src)
                self.assertNotIn(f"from {bad}", src)

    def test_load_and_validate_with_socket_disabled(self):
        real_bundle = str(REAL_BUNDLE)
        if not BUNDLE_EXISTS:
            self.skipTest("trip_bundle не входит в git-репозиторий (личные данные)")
        with mock.patch.object(socket, "socket", side_effect=OSError("no network")):
            b = load_bundle(real_bundle)
            self.assertEqual(len(b.stories), 12)


if __name__ == "__main__":
    unittest.main()
