# -*- coding: utf-8 -*-
"""Загрузка и валидация офлайн-бандла поездки.

МОДУЛЬ БЕЗ СЕТИ: импортирует только json/math/pathlib/re.
Никаких urllib/requests/socket — проверяется тестом no_network.
"""
import json
import re
from pathlib import Path

SUPPORTED_SCHEMA = "1.0.0"

STORY_REQUIRED_FIELDS = [
    "story_id", "poi_id", "title", "short_summary", "spoken_text",
    "estimated_duration_seconds", "coordinates", "route_distance_m",
    "trigger_radius_m", "facts", "sources", "uncertainties",
    "content_language", "content_version",
]
FACT_KINDS = ("fact", "interpretation", "legend")

# Явно секретоподобные конструкции, запрещённые в артефактах бандла
SECRET_PATTERNS = (
    re.compile(r"ORS_API_KEY\s*=\s*\S"),
    re.compile(r"Authorization\s*[:=]"),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9]{16,}", re.IGNORECASE),
)


class BundleValidationError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Бандл не прошёл валидацию: " + "; ".join(errors))


def _read_json(path, errors):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"файл отсутствует: {path.name}")
    except json.JSONDecodeError as e:
        errors.append(f"файл повреждён (JSON): {path.name}: {e}")
    return None


def validate_bundle(path):
    """Полная проверка целостности. Возвращает (manifest, stories, sources, route)
    или бросает BundleValidationError со списком всех проблем."""
    path = Path(path)
    errors = []

    manifest = _read_json(path / "manifest.json", errors)
    stories = _read_json(path / "stories.json", errors)
    sources = _read_json(path / "sources.json", errors)
    route = _read_json(path / "route.geojson", errors)
    if errors:
        raise BundleValidationError(errors)

    if manifest.get("schema_version") != SUPPORTED_SCHEMA:
        errors.append(f"schema_version {manifest.get('schema_version')!r} "
                      f"не поддерживается (ожидалось {SUPPORTED_SCHEMA!r})")

    # --- маршрут ---
    if not isinstance(route, dict) or not route.get("features"):
        errors.append("route.geojson: нет features")
    else:
        geom = route["features"][0].get("geometry", {})
        coords = geom.get("coordinates") or []
        if geom.get("type") != "LineString" or len(coords) < 2:
            errors.append("route.geojson: ожидается LineString с >=2 точками")
        for lon, lat in coords:
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                errors.append(f"route.geojson: координата вне диапазона {lon},{lat}")
                break

    # --- источники ---
    if not isinstance(sources, list) or not sources:
        errors.append("sources.json: ожидается непустой массив")
        sources = []
    known_sources = set()
    for s in sources:
        sid = s.get("source_id")
        if not sid:
            errors.append("sources.json: запись без source_id")
            continue
        if sid in known_sources:
            errors.append(f"sources.json: дубликат source_id {sid}")
        known_sources.add(sid)
        for field in ("type", "license", "url"):
            if not s.get(field):
                errors.append(f"sources.json: {sid} без {field}")

    # --- истории ---
    if not isinstance(stories, list) or not stories:
        errors.append("stories.json: ожидается непустой массив")
        stories = []

    seen_ids = set()
    last_along = -1.0
    for st in stories:
        sid = st.get("story_id", "<нет story_id>")
        for field in STORY_REQUIRED_FIELDS:
            if field not in st:
                errors.append(f"{sid}: отсутствует поле {field}")
        if sid in seen_ids:
            errors.append(f"дубликат story_id: {sid}")
        seen_ids.add(sid)

        poi_id = st.get("poi_id", "")
        if not re.fullmatch(r"(node|way|relation)/\d+", str(poi_id)):
            errors.append(f"{sid}: некорректный poi_id {poi_id!r}")

        c = st.get("coordinates") or {}
        if not (-90 <= c.get("lat", 999) <= 90 and -180 <= c.get("lon", 999) <= 180):
            errors.append(f"{sid}: координаты вне диапазона")

        along = st.get("route_distance_m")
        if not isinstance(along, (int, float)) or along < 0:
            errors.append(f"{sid}: route_distance_m должен быть числом >= 0")
        elif along < last_along:
            errors.append(f"{sid}: истории должны быть отсортированы по route_distance_m")
        else:
            last_along = along

        tr = st.get("trigger_radius_m")
        if not isinstance(tr, (int, float)) or not (50 <= tr <= 2000):
            errors.append(f"{sid}: trigger_radius_m вне диапазона 50..2000")

        dur = st.get("estimated_duration_seconds")
        if not isinstance(dur, (int, float)) or not (10 <= dur <= 120):
            errors.append(f"{sid}: estimated_duration_seconds вне 10..120 с")

        words = len((st.get("spoken_text") or "").split())
        if words == 0:
            errors.append(f"{sid}: пустой spoken_text")
        elif dur and abs(dur - words / 2.0) > max(6.0, 0.35 * dur):
            errors.append(f"{sid}: длительность {dur} с не согласована с объёмом "
                          f"текста ({words} слов; ожидалось ~{round(words/2.0/5)*5} с)")

        for f in st.get("facts") or []:
            if f.get("kind") not in FACT_KINDS:
                errors.append(f"{sid}: недопустимый kind факта {f.get('kind')!r}")
            if not (f.get("text") or "").strip():
                errors.append(f"{sid}: пустой текст факта")
            if f.get("kind") == "fact" and not f.get("source_ids"):
                errors.append(f"{sid}: подтверждённый факт без источника: "
                              f"{f.get('text', '')[:50]}...")
            for s in f.get("source_ids") or []:
                if s not in known_sources:
                    errors.append(f"{sid}: факт ссылается на неизвестный "
                                  f"источник {s}")

        for s in st.get("sources") or []:
            if s not in known_sources:
                errors.append(f"{sid}: история ссылается на неизвестный источник {s}")

    # --- секреты ---
    for f in ("manifest.json", "stories.json", "sources.json", "route.geojson"):
        text = (path / f).read_text(encoding="utf-8")
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                errors.append(f"обнаружен секретоподобный шаблон в {f}")

    if errors:
        raise BundleValidationError(errors)
    return manifest, stories, sources, route


def load_bundle(path):
    """Валидирует и возвращает Bundle-объект. Только локальные файлы."""
    manifest, stories, sources, route = validate_bundle(path)
    return Bundle(manifest, stories, sources, route, Path(path))


class Bundle:
    def __init__(self, manifest, stories, sources, route, path):
        self.manifest = manifest
        self.stories = stories
        self.sources = {s["source_id"]: s for s in sources}
        self.route = route
        self.path = path

    @property
    def route_coordinates(self):
        """[(lat, lon)] полилиния маршрута."""
        return [(lat, lon)
                for lon, lat in self.route["features"][0]["geometry"]["coordinates"]]

    def source_urls(self):
        return [s.get("url") for s in self.sources.values() if s.get("url")]
