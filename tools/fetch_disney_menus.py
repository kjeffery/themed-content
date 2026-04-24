#!/usr/bin/env python3
"""Fetch Disneyland Resort menus and emit a normalized `menus.json`.

Two-step Disney API flow, fully unauthenticated:

    1. GET /finder/api/v1/explorer-service/…/dining → restaurant list
    2. GET /dining/dinemenu/api/menu?searchTerm=…   → per-restaurant menu

We also pull the themeparks.wiki entity list for DLR so we can bridge Disney's
`facilityId` → themeparks.wiki UUID (their `externalId` is Disney's facility
id). The bridge is what lets the app cross-reference menus with the routing
graph and live schedule.

Output:    a single `menus.json` matching the Swift MenuCatalog schema.
Pipe into `tools/publish.py menus.json` to content-address + manifest it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Iterable

import requests

DISNEY_BASE = "https://disneyland.disney.go.com"
DLR_DESTINATION_ID = "80008297"
TPW_DLR_ENTITY_ID = "bfc89fd6-314d-44b4-b89e-df1a89cf991e"  # Disneyland Resort

# Disney's parkIds come back as "<id>;entityType=<kind>". We only set our
# `Park` enum for the two theme parks; hotel and Downtown Disney venues stay
# `park = nil` (still browsable, just not park-scoped).
PARK_ID_TO_PARK = {
    "330339": "disneyland",            # Disneyland Park
    "336894": "california-adventure",  # Disney California Adventure
}


def http_headers() -> dict:
    return {
        "Host": "disneyland.disney.go.com",
        "Accept": "application/json",
        "Accept-Language": "en_US",
        "Content-Type": "application/json",
        "Origin": DISNEY_BASE,
        "Referer": f"{DISNEY_BASE}/dining/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }


def fetch_restaurants(session: requests.Session, api_date: str) -> list[dict]:
    """Step 1: list all dining entities under the DLR destination."""
    url = (
        f"{DISNEY_BASE}/finder/api/v1/explorer-service/list-ancestor-entities/"
        f"dlr/{DLR_DESTINATION_ID};entityType=destination/{api_date}/dining"
    )
    r = session.get(url, headers=http_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])


def fetch_menu(session: requests.Session, url_friendly_id: str) -> dict | None:
    """Step 2: per-restaurant menu. Returns None when Disney has no menu listed
    (404, empty body, or non-JSON response — all observed in practice)."""
    r = session.get(
        f"{DISNEY_BASE}/dining/dinemenu/api/menu",
        headers=http_headers(),
        params={"searchTerm": url_friendly_id, "language": "en-us"},
        timeout=30,
    )
    if r.status_code == 404 or not r.text.strip():
        return None
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return None


def fetch_tpw_bridge() -> dict[str, str]:
    """Return a dict mapping Disney facilityId → themeparks.wiki UUID.

    themeparks.wiki stores Disney's facility id in `externalId` on entity docs.
    We hit the DLR children endpoint once, filter to RESTAURANTs, and build the
    dict. One round trip; no auth needed.
    """
    r = requests.get(
        f"https://api.themeparks.wiki/v1/entity/{TPW_DLR_ENTITY_ID}/children",
        timeout=30,
    )
    r.raise_for_status()
    bridge: dict[str, str] = {}
    for child in r.json().get("children", []):
        if child.get("entityType") != "RESTAURANT":
            continue
        ext = child.get("externalId")
        eid = child.get("id")
        if ext and eid:
            # themeparks.wiki appends ";entityType=restaurant" — strip it so
            # the key is the bare facility id Disney's API gives us.
            bare = str(ext).split(";", 1)[0]
            bridge[bare] = str(eid).lower()
    return bridge


def price_tier_from_facets(facets: Iterable[str]) -> str | None:
    """Disney's priceRange facet comes in as "$", "$$", "$$$", "$$$$"."""
    mapping = {"$": "low", "$$": "medium", "$$$": "high", "$$$$": "luxury"}
    for f in facets or []:
        if f in mapping:
            return mapping[f]
    return None


def cents(price) -> int | None:
    """Disney prices come back as numbers (sometimes strings, sometimes "N/A").
    Return integer cents, or None if the value is missing/unparseable."""
    if price is None or price == "" or price == "N/A":
        return None
    try:
        return int(round(float(price) * 100))
    except (TypeError, ValueError):
        return None


def park_for_park_ids(park_ids: list[str] | None) -> str | None:
    """`parkIds` entries look like "330339;entityType=theme-park"."""
    for pid in park_ids or []:
        bare = pid.split(";", 1)[0]
        if bare in PARK_ID_TO_PARK:
            return PARK_ID_TO_PARK[bare]
    return None


# ---------------------------------------------------------------------------
# Dietary-tag mining
# ---------------------------------------------------------------------------
# Disney's dining API doesn't expose structured dietary flags — the flags they
# *do* publish live in the free-text `title` + `description` fields. Mining
# recovers a useful subset: Disney uses a fairly consistent vocabulary
# ("Plant-based X", "[Vegetarian]", "Gluten-Friendly Bun", "Dairy-free
# Cheese"), so pattern matching produces high-precision tags for the items
# that bother to carry them (~4% of items / 59 of 117 restaurants at current
# writing).
#
# What we explicitly DO NOT mine:
#   • nut-free — allergen severity asymmetry. A false negative on gluten
#     or dairy causes discomfort; a false negative on a nut allergy can
#     be fatal. We don't publish what we can't stand behind.
#   • spicy — too noisy ("not-so-spicy Hot Dog" would false-positive).
#   • "contains-X" warnings — Disney's allergen callouts are incomplete;
#     an incomplete "safe for you" list is worse than none.

_TAG_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    ("vegan", [
        re.compile(r"\bvegan\b", re.IGNORECASE),
        re.compile(r"\bplant[- ]based\b", re.IGNORECASE),
    ]),
    ("vegetarian", [
        re.compile(r"\bvegetarian\b", re.IGNORECASE),
    ]),
    ("gluten-free", [
        re.compile(r"\bmade\s+without\s+gluten\b", re.IGNORECASE),
        re.compile(r"\bgluten[- ](?:free|friendly)\b", re.IGNORECASE),
    ]),
    ("dairy-free", [
        re.compile(r"\bdairy[- ]free\b", re.IGNORECASE),
    ]),
]

# Rejection contexts: match in the ~24 chars *before* a hit. If any of these
# fire, the hit describes an upsell or substitute — not a property of the
# base item. "Sub a Gluten Free crust" on a regular pizza would otherwise
# mistag the pizza. The patterns anchor to end-of-window so they only fire
# when the suspect phrasing immediately precedes the keyword.
_SUBSTITUTE_CONTEXT = re.compile(
    r"(?:\bsub(?:stitute)?(?:\s+(?:a|an))?"
    r"|\badd(?:\s+(?:a|an))?"
    r"|\boption\s+to"
    r"|\bor(?:\s+(?:a|an))?)\s*$",
    re.IGNORECASE,
)
# "not-so-spicy" would have lit the spicy pattern; defensively apply the
# same guard to everything so a future tag addition doesn't re-introduce
# the bug.
_NEGATION_PREFIX = re.compile(r"\bnot[- ]so[- ]$", re.IGNORECASE)


def mine_dietary_tags(name: str | None, description: str | None) -> list[str] | None:
    """Scan an item's name + description for Disney's dietary vocabulary.

    Returns the sorted list of tag strings found (in the same raw form
    `MenuItemDietaryTag(raw:)` on the Swift side normalizes), or `None`
    when no tag fires — callers serialize that as `tags: null` and old
    clients without a dietary reader stay happy.
    """
    text = f"{name or ''}   {description or ''}"  # gap keeps word boundaries clean
    if not text.strip():
        return None
    found: set[str] = set()
    for tag, patterns in _TAG_PATTERNS:
        if tag in found:
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                pre = text[max(0, match.start() - 24):match.start()]
                if _SUBSTITUTE_CONTEXT.search(pre):
                    continue
                if _NEGATION_PREFIX.search(pre):
                    continue
                found.add(tag)
                break  # one confirming hit is enough
            if tag in found:
                break
    return sorted(found) if found else None


def normalize_restaurant(entity: dict, raw_menu: dict | None, tpw_bridge: dict[str, str]) -> dict | None:
    url_friendly_id = entity.get("urlFriendlyId")
    if not url_friendly_id:
        return None

    facility_id = entity.get("facilityId") or ""
    land = entity.get("locationName") or None
    facets = entity.get("facets", {}) or {}

    meal_periods_out: list[dict] = []
    for period in (raw_menu or {}).get("mealPeriods", []) or []:
        groups_out = []
        for group in period.get("groups", []) or []:
            items_out = []
            for item in group.get("items", []) or []:
                prices = item.get("prices") or []
                price_cents = cents(prices[0].get("withoutTax")) if prices else None
                name = item.get("title") or "Unknown"
                description = item.get("description") or None
                items_out.append({
                    "id": str(item.get("id") or item.get("title", "")),
                    "name": name,
                    "description": description,
                    "priceCents": price_cents,
                    # Disney's public menu API omits `calories` in every
                    # sample we've inspected, but leave the read in place
                    # in case it ever surfaces — renders as `null` when
                    # absent (Swift decodes to Int?).
                    "calories": item.get("calories"),
                    # `dietaryTags` is also absent from Disney's feed — we
                    # mine the name/description instead. See
                    # `mine_dietary_tags` above for the vocabulary and
                    # safeguards.
                    "tags": mine_dietary_tags(name, description),
                })
            if items_out:
                groups_out.append({
                    "name": group.get("name") or "Items",
                    "items": items_out,
                })
        if groups_out:
            meal_periods_out.append({
                "name": period.get("name") or "All Day",
                "groups": groups_out,
            })

    return {
        "id": url_friendly_id,
        "name": entity.get("name") or "Unknown",
        "land": land,
        "park": park_for_park_ids(entity.get("parkIds")),
        "themeParksEntityID": tpw_bridge.get(str(facility_id)),
        "isQuickService": entity.get("quickServiceAvailable"),
        "cuisineTypes": facets.get("cuisine") or None,
        "priceTier": price_tier_from_facets(facets.get("priceRange") or []),
        "mealPeriods": meal_periods_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("menus.json"))
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--sleep", type=float, default=0.2,
                        help="seconds to sleep between menu fetches (be polite)")
    args = parser.parse_args()

    session = requests.Session()
    restaurants = fetch_restaurants(session, args.date)
    tpw_bridge = fetch_tpw_bridge()

    normalized: list[dict] = []
    for i, entity in enumerate(restaurants):
        ufid = entity.get("urlFriendlyId")
        if not ufid:
            continue
        try:
            raw = fetch_menu(session, ufid)
        except requests.HTTPError as e:
            print(f"  [skip] {ufid}: {e}", file=sys.stderr)
            continue
        norm = normalize_restaurant(entity, raw, tpw_bridge)
        if norm and norm["mealPeriods"]:
            normalized.append(norm)
        if args.sleep:
            time.sleep(args.sleep)
        if (i + 1) % 10 == 0:
            print(f"  fetched {i + 1}/{len(restaurants)}", file=sys.stderr)

    doc = {
        "formatVersion": 1,
        "destination": "disneyland-resort",
        "restaurants": sorted(normalized, key=lambda r: r["id"]),
    }
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({len(normalized)} restaurants with menus)")


if __name__ == "__main__":
    main()
