#!/usr/bin/env python3
"""Audit committed `menus.json` against Disney's live menu API.

READ-ONLY. Re-fetches Disney's menus, re-derives dietary + allergen signals
with the shared `dietary_signal` logic, and diffs them against the committed
`menus.json`. Emits a human-readable summary plus an optional JSON detail
file for review. It writes NO menu data and makes NO decision — a human reads
the report and decides what (if anything) to regenerate.

Why this exists: the committed data was built by mining item free-text only,
which ignored Disney's structured allergy-friendly / plant-based groups and
the "(For X Allergies)" safe-for lists. This surfaces exactly what that blind
spot costs, per restaurant, before we trust the richer signal in generation.

    tools/reconcile_disney_menus.py [--menus menus.json] [--limit N] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests

from dietary_signal import derive_item_signals, group_is_allergy_friendly
from fetch_disney_menus import fetch_menu, fetch_restaurants

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MENUS = REPO_ROOT / "menus.json"


def item_id(item: dict) -> str:
    """Mirror fetch_disney_menus' id derivation so fresh items line up with
    committed ones."""
    return str(item.get("id") or item.get("title", ""))


def load_committed(path: Path) -> dict[tuple[str, str], list[str] | None]:
    """Map (restaurant_id, item_id) → committed tags, for every item in the
    committed catalog."""
    doc = json.loads(path.read_text())
    out: dict[tuple[str, str], list[str] | None] = {}
    for r in doc.get("restaurants", []):
        rid = r.get("id")
        for mp in r.get("mealPeriods", []) or []:
            for g in mp.get("groups", []) or []:
                for it in g.get("items", []) or []:
                    out[(rid, str(it.get("id") or it.get("name", "")))] = it.get("tags")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--menus", type=Path, default=DEFAULT_MENUS,
                    help=f"committed menus.json to audit (default {DEFAULT_MENUS})")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N restaurants (0 = all) — for quick runs")
    ap.add_argument("--out", type=Path, default=None,
                    help="write per-item detail JSON here for review")
    args = ap.parse_args()

    committed = load_committed(args.menus)
    print(f"committed: {len(committed)} items across {args.menus.name}", file=sys.stderr)

    session = requests.Session()
    restaurants = fetch_restaurants(session, args.date)

    # Aggregate counters
    stat = defaultdict(int)
    per_resto: dict[str, dict] = {}
    detail: list[dict] = []
    tag_mismatches: list[dict] = []

    processed = 0
    for entity in restaurants:
        ufid = entity.get("urlFriendlyId")
        if not ufid:
            continue
        try:
            raw = fetch_menu(session, ufid)
        except requests.HTTPError as e:
            print(f"  [skip] {ufid}: {e}", file=sys.stderr)
            continue
        if not raw:
            continue
        processed += 1

        rstat = per_resto.setdefault(ufid, {
            "name": entity.get("name"), "items": 0, "af_items": 0,
            "gained_allergens": 0, "gained_tags": 0,
        })
        resto_has_af = False

        for mp in raw.get("mealPeriods", []) or []:
            for g in mp.get("groups", []) or []:
                gname, gtype = g.get("name", ""), g.get("type", "")
                af = group_is_allergy_friendly(gtype, gname)
                if af:
                    resto_has_af = True
                for it in g.get("items", []) or []:
                    stat["items"] += 1
                    rstat["items"] += 1
                    if af:
                        stat["af_items"] += 1
                        rstat["af_items"] += 1

                    sig = derive_item_signals(
                        it.get("title"), it.get("description"), gname, gtype)
                    fresh_tags = sig["tags"] or []
                    allergens = sig["allergenFriendlyFor"]

                    old_tags = committed.get((ufid, item_id(it)))
                    old_set = set(old_tags or [])
                    new_set = set(fresh_tags)

                    if allergens:
                        stat["gained_allergens"] += 1
                        rstat["gained_allergens"] += 1
                    if new_set - old_set:
                        stat["gained_tags"] += 1
                        rstat["gained_tags"] += 1
                    if old_set - new_set:
                        # committed tag no longer produced — possible drift or
                        # a false positive worth a human look.
                        tag_mismatches.append({
                            "restaurant": ufid, "item": it.get("title"),
                            "committed": sorted(old_set), "fresh": sorted(new_set),
                        })

                    if allergens or (new_set - old_set):
                        detail.append({
                            "restaurant": ufid, "group": gname, "groupType": gtype,
                            "item": it.get("title"),
                            "allergenFriendlyFor": allergens,
                            "tags_added": sorted(new_set - old_set) or None,
                            "tags_committed": sorted(old_set) or None,
                        })

        if resto_has_af:
            stat["restaurants_with_af"] += 1
        if args.sleep:
            time.sleep(args.sleep)
        if args.limit and processed >= args.limit:
            break

    # ---- report ----
    print("\n" + "=" * 66)
    print("RECONCILIATION SUMMARY (committed menus.json vs live Disney API)")
    print("=" * 66)
    print(f"restaurants with menus fetched : {processed}")
    print(f"  …offering an allergy-friendly menu : {stat['restaurants_with_af']}")
    print(f"menu items scanned             : {stat['items']}")
    print(f"  …in allergy-friendly groups  : {stat['af_items']}")
    print(f"items that GAIN a safe-for allergen list : {stat['gained_allergens']}")
    print(f"items that GAIN a dietary tag  : {stat['gained_tags']}")
    print(f"committed tags no longer produced (review) : {len(tag_mismatches)}")

    top = sorted(per_resto.values(),
                 key=lambda r: r["gained_allergens"], reverse=True)[:15]
    print("\nTop restaurants by allergen coverage gained:")
    for r in top:
        if not r["gained_allergens"]:
            continue
        print(f"  {r['gained_allergens']:4}  {r['name']}  "
              f"({r['af_items']} allergy-friendly items)")

    if tag_mismatches:
        print("\nSample committed-tag drops (committed → fresh):")
        for m in tag_mismatches[:10]:
            print(f"  {m['restaurant']}: {m['item']!r}  {m['committed']} → {m['fresh']}")

    if args.out:
        args.out.write_text(json.dumps({
            "generatedFor": args.date,
            "summary": dict(stat),
            "tagMismatches": tag_mismatches,
            "detail": detail,
        }, indent=2) + "\n")
        print(f"\nwrote detail → {args.out} ({len(detail)} enriched items)")


if __name__ == "__main__":
    main()
