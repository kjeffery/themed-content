#!/usr/bin/env python3
"""Shared dietary + allergen signal extraction for Disney menu data.

Used by both `fetch_disney_menus.py` (generation) and
`reconcile_disney_menus.py` (audit) so the two can never drift.

Two signal sources, in increasing order of authority:

  1. Free-text mining of an item's name/description ("Plant-based Burger",
     "Gluten-Friendly Bun"). Low coverage, but all we have for regular menu
     items. This is what the original pipeline used *exclusively* — and why
     coverage was ~4% of items.

  2. Disney's structured group taxonomy. Every menu group carries a `type`
     ("Allergy Friendly|Entree", "Entree", "Kids", …) and a display `name`
     ("Allergy-Friendly Entrées", "Plant-Based"). The old miner threw this
     away; it's high-confidence first-party structure.

  3. Per-item safe-for allergens, in TWO formats Disney uses interchangeably:
       a. Table-service: a "(For X, Y and Z Allergies)" phrase in the
          description (`allergen_free_for`).
       b. Quick-service: the allergen encoded in the item or group NAME —
          "Egg Allergy-Friendly Spaghetti", groups named "Milk Allergy-
          Friendly" (`allergens_from_name`). This is the MORE common format
          (~2.7× the description format in sampling) and was the original
          blind spot.

SEMANTICS OF THE SAFE-FOR LIST — verified against live data and
safety-critical, so spelled out:

    The allergens listed are the ones the dish is prepared to be SAFE FOR
    (free of). Proof: "Crab Cakes … (For Fish, Peanut/Tree Nut and Sesame
    Allergies)" — crab *is* shellfish, and shellfish is pointedly NOT in the
    list. So the list is a free-of/safe-for claim, never a contains-warning.

    Absence of an allergen from the list is NOT a contains-claim — it's
    simply "not claimed," which matches the app's confirmed / couldn't-confirm
    stance (never "incompatible"). Disney's own menus require a cast-member
    conversation, so any UI built on this MUST say "marked safe for", not
    "is", and keep the confirm-with-a-cast-member disclaimer.

We only trust the "(For X)" phrase inside groups Disney itself flags as
allergy-friendly (`group_is_allergy_friendly`); we never mine it out of a
regular group.
"""
from __future__ import annotations

import re

# Canonical, atomic allergen tokens we emit. Aligned to the US FDA "big 9"
# (wheat→gluten, crustacean→shellfish). Stable strings — the Swift side and
# any UI match against these exact values.
CANONICAL_ALLERGENS = (
    "gluten", "egg", "milk", "soy", "sesame",
    "fish", "shellfish", "peanut", "tree-nut",
)

# Word-boundary scanners for each canonical allergen. A combined source token
# like "fish/shellfish" lights BOTH `fish` and `shellfish` (a SAFE broadening:
# prepared-free-of-the-pair entails free-of-each); "peanut/tree nut" lights
# both peanut and tree-nut. We never expand the other direction — a lone
# "peanut" must not imply tree-nut. `\bfish\b` deliberately does NOT fire
# inside "shellfish" (no word boundary), so shellfish never leaks a fish claim.
# Bare "nut" is intentionally unmatched (ambiguous peanut-vs-tree-nut).
#
# CRITICAL: only ever run this over a CONSTRAINED segment — the "(For X)"
# safe-for list, or a name's "<Allergen> Allergy-Friendly" prefix — NEVER the
# free description body, where an allergen word is an *ingredient* ("Crab Cakes
# with fish sauce"), the opposite of a safe-for claim.
_ALLERGEN_SCAN: list[tuple[str, re.Pattern]] = [
    ("gluten", re.compile(r"\b(?:gluten|wheat)\b", re.IGNORECASE)),
    ("egg", re.compile(r"\beggs?\b", re.IGNORECASE)),
    ("milk", re.compile(r"\b(?:milk|dairy)\b", re.IGNORECASE)),
    ("soy", re.compile(r"\bsoy\b", re.IGNORECASE)),
    ("sesame", re.compile(r"\bsesame\b", re.IGNORECASE)),
    ("shellfish", re.compile(r"\bshellfish\b", re.IGNORECASE)),
    ("fish", re.compile(r"\bfish\b", re.IGNORECASE)),
    ("peanut", re.compile(r"\bpeanuts?\b", re.IGNORECASE)),
    ("tree-nut", re.compile(r"\btree\s*nuts?\b", re.IGNORECASE)),
]


def _scan_allergens(segment: str) -> set[str]:
    """Canonical allergens named anywhere in a constrained segment. See the
    CRITICAL note on `_ALLERGEN_SCAN` — never pass a free description body."""
    return {canon for canon, pat in _ALLERGEN_SCAN if pat.search(segment)}


# Anchored to the literal "(For … Allerg…)" parenthetical Disney appends, so a
# stray earlier "for" in the description ("…Rice for two") can't start the
# capture. Non-greedy up to the first "allerg".
_FOR_ALLERGIES = re.compile(r"\(\s*for\b(.*?)allerg", re.IGNORECASE | re.DOTALL)

# Quick-service format: "<Allergen> Allergy-Friendly <Dish>" (item name) or a
# group literally named "<Allergen> Allergy-Friendly". We scan only the prefix
# BEFORE "allergy-friendly" — the dish part after it may name an ingredient.
_ALLERGY_FRIENDLY_NAME = re.compile(r"^(.*?)\ballergy[- ]friendly\b", re.IGNORECASE)
# The generic group header item ("Guests must speak to a Cast Member about
# their allergy-friendly request") carries no allergen and must stay unparsed.
_PLACEHOLDER_ITEM = re.compile(r"speak\s+to\s+a\s+cast\s+member", re.IGNORECASE)


def allergen_free_for(description: str | None) -> list[str] | None:
    """Table-service format: parse Disney's "(For X, Y and Z Allergies)" phrase
    into the sorted canonical allergens the dish is marked SAFE FOR. None when
    the phrase is absent or names nothing recognized."""
    if not description:
        return None
    m = _FOR_ALLERGIES.search(description)
    if not m:
        return None
    found = _scan_allergens(m.group(1))
    return sorted(found) if found else None


def allergens_from_name(name: str | None) -> list[str] | None:
    """Quick-service format: parse the allergen from an item or group name of
    the form "<Allergen> Allergy-Friendly <Dish>". Returns None for the generic
    cast-member placeholder or a prefix that names no allergen."""
    if not name or _PLACEHOLDER_ITEM.search(name):
        return None
    m = _ALLERGY_FRIENDLY_NAME.search(name)
    if not m:
        return None
    found = _scan_allergens(m.group(1))
    return sorted(found) if found else None


def group_is_allergy_friendly(group_type: str | None, group_name: str | None = "") -> bool:
    """True when Disney flags the group as allergy-friendly, via either the
    structured `type` ("Allergy Friendly|…") or the display name."""
    return "allerg" in (group_type or "").lower() or "allerg" in (group_name or "").lower()


def group_is_plant_based(group_name: str | None, group_type: str | None = "") -> bool:
    """True for a dedicated "Plant-Based" group — a first-party vegan signal
    the text miner misses (the dishes rarely repeat the word in their titles)."""
    n = (group_name or "").lower()
    return "plant-based" in n or "plant based" in n


# ---------------------------------------------------------------------------
# Free-text dietary-tag mining (moved here from fetch_disney_menus so the
# generator and the audit share one implementation).
# ---------------------------------------------------------------------------
# We publish lifestyle-diet tags (vegetarian/vegan/gluten-free/dairy-free) but
# deliberately DO NOT mine nut-free/spicy/"contains-X" — a false negative on a
# nut allergy can be fatal, so we don't publish what we can't stand behind.
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

_SUBSTITUTE_CONTEXT = re.compile(
    r"(?:\bsub(?:stitute)?(?:\s+(?:a|an))?"
    r"|\badd(?:\s+(?:a|an))?"
    r"|\boption\s+to"
    r"|\bor(?:\s+(?:a|an))?)\s*$",
    re.IGNORECASE,
)
_NEGATION_PREFIX = re.compile(r"\bnot[- ]so[- ]$", re.IGNORECASE)


def mine_dietary_tags(name: str | None, description: str | None) -> list[str] | None:
    """Scan an item's name + description for Disney's dietary vocabulary.
    Returns sorted raw tag strings (the form `MenuItemDietaryTag(raw:)`
    normalizes on the Swift side), or None when nothing fires."""
    text = f"{name or ''}   {description or ''}"
    if not text.strip():
        return None
    found: set[str] = set()
    for tag, patterns in _TAG_PATTERNS:
        if tag in found:
            continue
        for pattern in patterns:
            for match in pattern.finditer(text):
                pre = text[max(0, match.start() - 24):match.start()]
                if _SUBSTITUTE_CONTEXT.search(pre) or _NEGATION_PREFIX.search(pre):
                    continue
                found.add(tag)
                break
            if tag in found:
                break
    return sorted(found) if found else None


def derive_item_signals(
    name: str | None,
    description: str | None,
    group_name: str | None = "",
    group_type: str | None = "",
) -> dict:
    """Combine every signal for one item. Returns a dict with:
        tags               — lifestyle diet tags (list[str] or None)
        allergenFriendlyFor — canonical allergens marked safe-for (or None),
                              ONLY populated inside allergy-friendly groups.
    """
    tags: set[str] = set(mine_dietary_tags(name, description) or [])
    if group_is_plant_based(group_name, group_type):
        tags.add("vegan")  # dedicated Plant-Based group ⇒ first-party vegan

    allergens = None
    if group_is_allergy_friendly(group_type, group_name):
        # Table-service: full safe-for list in the "(For X)" description.
        allergens = allergen_free_for(description)
        # Quick-service: allergen encoded in the item name ("Egg Allergy-
        # Friendly Spaghetti"), or the group name ("Milk Allergy-Friendly").
        # Item name is most specific; fall back to the group's allergen — but
        # never for the group's "speak to a Cast Member" header row, which is
        # an instruction, not a dish, and must not inherit the group allergen.
        if not allergens and not _PLACEHOLDER_ITEM.search(name or ""):
            allergens = allergens_from_name(name) or allergens_from_name(group_name)

    return {
        "tags": sorted(tags) if tags else None,
        "allergenFriendlyFor": allergens,
    }


# ---------------------------------------------------------------------------
# Self-test — run `python3 dietary_signal.py`. Cases are transcribed from live
# Disneyland menu data; the crab-cakes case guards the safety-critical
# invariant that a contained allergen is never emitted as safe-for.
# ---------------------------------------------------------------------------
def _selftest() -> None:
    cases = [
        # (description, expected allergen_free_for)
        ("Remoulade Sauce and Petite Apple-Arugula Salad "
         "(For Fish, Peanut/Tree Nut and Sesame Allergies)",
         ["fish", "peanut", "sesame", "tree-nut"]),  # NB: no shellfish — it's crab
        ("Mashed Potatoes, Seasonal Vegetables and Peppercorn Demi-glace. "
         "(For Gluten/Wheat, Egg, Peanut/Tree Nut, Sesame, Shellfish and Soy Allergies)",
         ["egg", "gluten", "peanut", "sesame", "shellfish", "soy", "tree-nut"]),
        ("Crab Rice, Pickled Fruit Chow Chow, Sautéed Greens and Citrus Tea Vinaigrette "
         "(For Gluten/Wheat, Egg, Milk, Peanut/Tree Nut, Sesame and Soy Allergies)",
         ["egg", "gluten", "milk", "peanut", "sesame", "soy", "tree-nut"]),
        # No parenthetical → None
        ("Crab Rice, Pickled Fruit Chow Chow, Sautéed Greens", None),
        # Stray "for" earlier in the text must not derail the capture
        ("Steamed Rice for the table. (For Egg and Milk Allergies)", ["egg", "milk"]),
    ]
    ok = True
    for desc, expected in cases:
        got = allergen_free_for(desc)
        status = "ok " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"  [{status}] {got}  (expected {expected})")

    # Quick-service name-encoded format (allergen in the item/group name).
    name_cases = [
        ("Egg Allergy-Friendly Spaghetti with Marinara", ["egg"]),
        ("Gluten/Wheat Allergy-Friendly Pepperoni Pizza", ["gluten"]),
        ("Fish/Shellfish Allergy-Friendly Satellite Salad", ["fish", "shellfish"]),
        ("Peanut/Tree Nut Allergy-Friendly Kids' Pizza", ["peanut", "tree-nut"]),
        ("Gluten / Wheat Allergy-Friendly Bun", ["gluten"]),  # spacing typo
        # Allergen after "Allergy-Friendly" is a dish ingredient, not a claim:
        ("Egg Allergy-Friendly Fish Tacos", ["egg"]),
        # Placeholder header must stay unparsed:
        ("Guests must speak to a Cast Member about their allergy-friendly request", None),
        # No "allergy-friendly" marker → None:
        ("Cheeseburger", None),
    ]
    for name, expected in name_cases:
        got = allergens_from_name(name)
        if got != expected:
            ok = False
        print(f"  [{'ok ' if got == expected else 'FAIL'}] name {name[:42]!r:44} → {got}")

    # group helpers
    assert group_is_allergy_friendly("Allergy Friendly|Entree")
    assert group_is_allergy_friendly("Entree", "Allergy-Friendly Dessert")
    assert not group_is_allergy_friendly("Entree", "Entrées")
    assert group_is_plant_based("Plant-Based")
    assert not group_is_plant_based("Entrées")

    # gating: allergen signal must be ignored outside an allergy-friendly group
    sig = derive_item_signals("X", "(For Egg Allergies)", "Entrées", "Entree")
    assert sig["allergenFriendlyFor"] is None, sig
    sig = derive_item_signals("X", "(For Egg Allergies)", "Allergy-Friendly Entrées", "Allergy Friendly|Entree")
    assert sig["allergenFriendlyFor"] == ["egg"], sig
    # name-encoded item inside its allergen group
    sig = derive_item_signals("Egg Allergy-Friendly Spaghetti", "Spaghetti with Marinara",
                              "Egg Allergy-Friendly", "Allergy Friendly")
    assert sig["allergenFriendlyFor"] == ["egg"], sig
    # plain-named item inheriting the group's allergen
    sig = derive_item_signals("Cheese Pizza", "", "Milk Allergy-Friendly", "Allergy Friendly")
    assert sig["allergenFriendlyFor"] == ["milk"], sig
    # the group's cast-member header row must NOT inherit the group allergen
    sig = derive_item_signals("Guests must speak to a Cast Member about their allergy-friendly request",
                              "", "Gluten/Wheat Allergy-Friendly", "Allergy Friendly")
    assert sig["allergenFriendlyFor"] is None, sig
    # ingredient in a plain description must NOT leak a safe-for claim
    sig = derive_item_signals("Crab Cakes", "made with fish sauce",
                              "Allergy-Friendly Appetizers", "Allergy Friendly|Appetizer")
    assert sig["allergenFriendlyFor"] is None, sig
    sig = derive_item_signals("Tofu Bowl", "Marinated tofu", "Plant-Based", "Entree")
    assert sig["tags"] == ["vegan"], sig

    print("group/gating assertions passed" if ok else "CASES FAILED")
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    _selftest()
