#!/usr/bin/env python3
"""Generate authored-POI proposals for in-park restaurants that have menus
but no routing POI.

Usage:
    tools/propose_authored_pois.py [--content-dir DIR]

Read-only reconciliation (never touches live files): cross-references the
published menus against graph.json + pois_authored.json and, for every
in-park menu restaurant with no POI, emits a proposal using Disney's own
map coordinate (`marker.lat`/`marker.lng` from the dining list — the same
API fetch_disney_menus.py scrapes).

ID policy mirrors the catalog invariant (id == themeParksEntityID):
  - themeparks.wiki has an entity (bridged via externalId == facilityId):
    the POI id IS the wiki UUID.
  - no wiki entity: a UUIDv5 minted from Disney's facilityId, plus a
    MANUAL_BRIDGE_BY_URL_ID line to paste into fetch_disney_menus.py so
    daily menu refreshes keep the join (see check_manual_bridge).

Output: pois_authored_proposed.json (existing authored POIs preserved
verbatim, proposals appended) plus a review table and any bridge lines on
stdout.

Adopt after review:
    cp pois_authored_proposed.json pois_authored.json
    # paste printed MANUAL_BRIDGE_BY_URL_ID entries into fetch_disney_menus.py
    tools/publish.py pois_authored.json
    tools/hexgrid_report.py   # new POIs must pass the snap gate; run
    tools/propose_entrance_overrides.py if any land beyond threshold
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests  # noqa: E402

from fetch_disney_menus import (  # noqa: E402
    MANUAL_BRIDGE_BY_URL_ID,
    PARK_ID_TO_PARK,
    fetch_restaurants,
    fetch_tpw_bridge,
    park_for_park_ids,
)

# Same reasoning as import_osm_pois.OSM_POI_NAMESPACE: a fixed namespace so
# re-runs mint identical ids for the same Disney facility.
DISNEY_POI_NAMESPACE = uuid.UUID("b3a4f0d2-58c7-4c19-9e34-7d2f5a1c6e08")

# Menu entries that must NOT become pins, by urlFriendlyId:
#   - umbrella cart listings: one Disney marker stands in for many physical
#     carts (the individual carts are separate POIs in graph.json); a single
#     pin would misplace all but one of them. Their menus are still real —
#     joining the umbrella menu to each individual cart is a separate,
#     worthwhile follow-up.
#   - packages / dessert parties: ticketed experiences tied to a venue, not
#     locations. Same broken-promise reasoning as the Visa photo op: we
#     can't model the ticket gate, so we don't pin it.
#   - seasonal festival umbrellas and passes: not locations at all.
#   - cozy-cone-motel: parent listing; the individual cone windows already
#     exist as POIs.
#   - tomorrowland-skyline-terrace: event-ticket-only venue.
NON_LOCATION_URL_IDS = {
    # umbrella carts
    "churros", "pretzels", "fruit", "popcorn", "lemonade", "turkey-legs",
    # packages & parties
    "plaza-inn-dining-package", "fantasmic-dinner-packages",
    "oogie-boogie-bash-dessert-party", "world-of-color-dessert-party",
    "world-of-color-dining",
    # festival umbrellas & passes
    "food-and-wine-festival-marketplaces", "lunar-new-year-food-marketplaces",
    "festive-foods-marketplaces", "sip-and-savor",
    # parent / gated venues
    "cozy-cone-motel", "tomorrowland-skyline-terrace",
}


def minted_id(facility_id: str) -> str:
    return str(uuid.uuid5(DISNEY_POI_NAMESPACE, f"disney-dining-{facility_id}")).upper()


def existing_poi_index(content_dir: Path) -> tuple[set[str], set[str]]:
    """(lowercased themeParksEntityIDs, lowercased names) across graph + authored."""
    ids: set[str] = set()
    names: set[str] = set()
    for fname in ("graph.json", "pois_authored.json"):
        doc = json.loads((content_dir / fname).read_text())
        for poi in doc["pois"]:
            if eid := poi.get("themeParksEntityID"):
                ids.add(str(eid).lower())
            if name := poi.get("name"):
                names.add(name.lower())
    return ids, names


def published_menu_ids(content_dir: Path) -> set[str]:
    """urlFriendlyIds with an actual published menu. A dining-list entity
    with no menu (e.g. Boudin Bread Cart) must not become an authored
    restaurant POI: the app-side join gate requires every authored
    restaurant to resolve a menu."""
    man = json.loads((content_dir / "manifest.json").read_text())
    menus_path = next(f["path"] for f in man["files"] if f["role"] == "menus")
    doc = json.loads((content_dir / menus_path).read_text())
    return {r["id"] for r in doc["restaurants"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--content-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    args = parser.parse_args()

    session = requests.Session()
    restaurants = fetch_restaurants(session, date.today().isoformat())
    tpw_bridge = fetch_tpw_bridge()
    poi_ids, poi_names = existing_poi_index(args.content_dir)
    menu_ids = published_menu_ids(args.content_dir)

    authored_path = args.content_dir / "pois_authored.json"
    authored = json.loads(authored_path.read_text())

    proposals: list[dict] = []
    bridge_lines: list[str] = []
    skipped: list[str] = []
    for entity in restaurants:
        ufid = entity.get("urlFriendlyId")
        park = park_for_park_ids(entity.get("parkIds"))
        if not ufid or ufid in NON_LOCATION_URL_IDS:
            continue
        if ufid not in menu_ids:
            skipped.append(f"{entity.get('name') or ufid}: no published menu")
            continue
        if park not in PARK_ID_TO_PARK.values():
            continue  # hotel / Downtown Disney — out of scope by design
        name = entity.get("name") or ufid
        facility_id = str(entity.get("facilityId") or "")
        wiki_id = tpw_bridge.get(facility_id)
        already = (wiki_id and wiki_id.lower() in poi_ids) or name.lower() in poi_names
        if already:
            continue
        marker = entity.get("marker") or {}
        lat, lng = marker.get("lat"), marker.get("lng")
        if lat is None or lng is None:
            skipped.append(f"{name}: Disney marker has no coordinate")
            continue
        pid = (wiki_id or minted_id(facility_id)).upper()
        proposals.append({
            "coord": {"latitude": lat, "longitude": lng},
            "id": pid,
            "kind": "restaurant",
            "name": name,
            "note": (
                f"AUTO-PROPOSED from Disney dining marker ({ufid}); "
                + ("wiki-bridged" if wiki_id else "no wiki entity — needs manual bridge")
                + " — review"
            ),
            "park": park,
            "themeParksEntityID": pid,
        })
        if not wiki_id:
            bridge_lines.append(f'    "{ufid}": "{pid.lower()}",')

    out = dict(authored)
    out["pois"] = authored["pois"] + proposals
    out_path = args.content_dir / "pois_authored_proposed.json"
    out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    print(f"=== Authored-POI proposals ({len(proposals)}) ===")
    for p in proposals:
        bridged = "wiki" if "wiki-bridged" in p["note"] else "manual-bridge"
        print(f"  {p['park']:<20} {p['name']:<40} [{bridged}]")
    if bridge_lines:
        already_present = [
            ln for ln in bridge_lines
            if ln.split('"')[1] in MANUAL_BRIDGE_BY_URL_ID
        ]
        print("\nAdd to MANUAL_BRIDGE_BY_URL_ID in fetch_disney_menus.py:")
        for ln in bridge_lines:
            if ln not in already_present:
                print(ln)
    if skipped:
        print("\nSkipped (no coordinate):")
        for s_ in skipped:
            print(f"  {s_}")
    print(f"\nwrote {out_path.name}: {len(authored['pois'])} existing + {len(proposals)} proposed")


if __name__ == "__main__":
    main()
