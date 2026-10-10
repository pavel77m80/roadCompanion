# -*- coding: utf-8 -*-
"""Preflight инкремента 2: проверка происхождения карточек и пула кандидатов.

Только чтение артефактов experiment/. Сетевых запросов нет.
Результат: offline_pack/preflight_report.json.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiment"
OUT = ROOT / "offline_pack"


def load(rel):
    return json.loads((EXP / "data" / "processed" / rel).read_text(encoding="utf-8"))


def main():
    cards = load("story_cards.json")
    src = load("poi_with_sources.json")
    route = load("route.geojson")
    coverage = load("coverage.json")

    pois = {(p["osm_type"], p["osm_id"]): p for p in src["poi"]}
    route_props = route["features"][0]["properties"]

    # 1) Происхождение карточек
    provenance = []
    for c in cards["cards"]:
        key = (c["osm_type"], c["osm_id"])
        p = pois.get(key)
        issues = []
        if not p:
            issues.append("POI отсутствует в poi_with_sources.json")
            provenance.append({"card": c["name"], "ok": False, "issues": issues})
            continue
        if abs(p["lat"] - c["lat"]) > 1e-6 or abs(p["lon"] - c["lon"]) > 1e-6:
            issues.append("координаты карточки не совпадают с POI")
        wd = p["source"].get("wikidata") or {}
        if c.get("wikidata") and wd.get("qid") != c["wikidata"]:
            issues.append("QID карточки != QID POI")
        qids_in_facts = [f["source"] for f in c["confirmed_facts"]
                         if "wikidata.org/wiki/" in f["source"]]
        urls_in_facts = [f["source"] for f in c["confirmed_facts"]
                         if "wikipedia.org" in f["source"]]
        if c["wikidata"] and qids_in_facts and not all(
                f"https://www.wikidata.org/wiki/{c['wikidata']}" == u for u in qids_in_facts):
            issues.append("ссылка факта на чужой QID")
        if urls_in_facts and wd.get("wikipedia_url"):
            import urllib.parse
            wtarget = urllib.parse.unquote(wd["wikipedia_url"].split("/wiki/")[-1])
            fact_targets = {urllib.parse.unquote(u.split("/wiki/")[-1]) for u in urls_in_facts}
            if wtarget not in fact_targets:
                issues.append("wikipedia-ссылка факта не совпадает с POI")
        unsourced = [f["fact"] for f in c["confirmed_facts"] if not f.get("source")]
        if unsourced:
            issues.append(f"факты без источника: {unsourced}")
        provenance.append({
            "card": c["name"], "poi": f"{key[0]}/{key[1]}",
            "match_status": p["source"]["match_status"],
            "qid": wd.get("qid"), "n_facts": len(c["confirmed_facts"]),
            "ok": not issues, "issues": issues,
        })

    # 2) Пул кандидатов (только сохранённые данные)
    def interesting_tags(p):
        tags = p.get("tags", {})
        if tags.get("highway") or tags.get("route") or tags.get("boundary"):
            return False
        # place=village/hamlet с идентификацией в Wikidata — легитимный контент
        if tags.get("place") and tags.get("place") not in ("village", "hamlet"):
            return False
        good = {"historic", "religious_architecture", "protected_area", "has_wiki_link"}
        return any(c in good or c.startswith(("tourism_", "nature_"))
                   for c in p["categories"])

    pool, rejected = [], []
    for p in src["poi"]:
        wd = p["source"].get("wikidata") or {}
        reasons = []
        if p["source"]["match_status"] != "MATCHED":
            reasons.append(f"match_status={p['source']['match_status']}")
        if not p.get("name"):
            reasons.append("нет названия")
        if p["distance_to_route_m"] > 800:
            reasons.append(f"далеко от маршрута ({p['distance_to_route_m']} м)")
        if not interesting_tags(p):
            reasons.append("нет интересной категории")
        if not (wd.get("qid") or p["source"].get("wikipedia_summary")):
            reasons.append("нет идентификации в Wikidata/Wikipedia")
        io = wd.get("instance_of") or []
        if any(q in io for q in ("Q15145593", "Q34442", "Q83620")):
            reasons.append("линейный объект (маршрут/улица), не точка интереса")
        cand = {
            "poi": f"{p['osm_type']}/{p['osm_id']}",
            "name": p.get("name"), "qid": wd.get("qid"),
            "lat": p["lat"], "lon": p["lon"],
            "distance_to_route_m": p["distance_to_route_m"],
            "along_route_m": p["along_route_m"],
            "categories": p["categories"],
            "has_description": bool(wd.get("description_ru")),
            "has_extract_cache": bool(p["source"].get("wikipedia_summary")),
            "instance_of": wd.get("instance_of"),
        }
        (pool if not reasons else rejected).append({**cand, "reject_reasons": reasons})

    pool.sort(key=lambda c: (c["along_route_m"], -c["distance_to_route_m"]))
    report = {
        "route": {"points": route_props["n_points"],
                  "distance_m": route_props["distance_m"],
                  "duration_s": route_props["duration_s"]},
        "coverage_snapshot": coverage,
        "card_provenance": provenance,
        "n_candidate_pool": len(pool),
        "candidate_pool": pool,
        "n_rejected": len(rejected),
        "rejected_sample": rejected[:40],
    }
    OUT.mkdir(exist_ok=True)
    (OUT / "preflight_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for x in provenance if x["ok"])
    print(f"Карточек проверено: {len(provenance)}, без проблем: {ok}")
    for x in provenance:
        if not x["ok"]:
            print("  ПРОБЛЕМА:", x["card"], x["issues"])
    print(f"Кандидатов в пуле (MATCHED+интересные+<=800м): {len(pool)}")
    for c in pool:
        print(f"  {c['along_route_m']/1000:5.1f}км {c['distance_to_route_m']:4}м "
              f"{c['poi']:<16} {c['qid'] or '-':<12} desc={c['has_description']} "
              f"extract={c['has_extract_cache']} {c['name']}")


if __name__ == "__main__":
    main()
