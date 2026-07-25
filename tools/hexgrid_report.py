#!/usr/bin/env python3
"""Release-gate report for `hexgrid.json`: POI snap audit + connectivity.

Usage:
    tools/hexgrid_report.py [--content-dir DIR] [--threshold 5]

Read-only. Answers "is the grid done?" with two checks:

1. Snap audit — every POI (graph.json + pois_authored.json, with
   poi_entrance_overrides.json applied) must have a passable cell within
   `--threshold` meters of its routing coord. Mirrors
   `POICatalog.snapAudit` in themed/Routing/POISnapAudit.swift: same
   ring-expansion search, same 80-ring warning cap, same equirectangular
   distance. The in-app 5 m snap stays as a runtime safety net; this
   gate asserts nothing relies on it.

2. Connectivity — flood-fill the passable cells into connected
   components. Components containing a park-entrance POI's cell are the
   mainland; components containing a seed from
   `hex_report_config.json`'s `intentionalIslands` are expected (e.g.
   Tom Sawyer Island) and reported informationally; everything else is
   a stray island, listed worst-first with a centroid you can jump to
   in the painter. POIs whose nearest cell sits on a stray island (or
   on no component at all) are flagged — they'd pass a naive snap audit
   but every route to them would fail.

Exit status: 0 when the gate passes, 1 when anything fails — suitable
for CI next to check_manifest.py.

Geometry note: cell centers / hex(at:) mirror `GridOrigin` in
themed/Routing/HexGrid.swift (same drift hazard as
enrich_hex_elevations.py — changes there must be reflected here).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hexgrid_format  # noqa: E402

# Geometry (Projection, ring, distance_meters) lives in hexgrid_format —
# shared with hexgrid_clip.py and kept in sync with the Swift GridOrigin.
from hexgrid_format import (  # noqa: E402
    FLAT_TOP_DIRECTIONS,
    Projection,
    distance_meters,
    ring,
)

# Mirrors POICatalog.snapAudit's cap: beyond 80 rings (~160 m at 1 m
# apothem) a POI is reported with the best distance found so far.
MAX_WARNING_RINGS = 80

# --- data loading ------------------------------------------------------------

def load_pois(content_dir: Path) -> list[dict]:
    """graph.json POIs + pois_authored.json waypoints, tagged with source."""
    pois = []
    graph = json.loads((content_dir / "graph.json").read_text())
    for p in graph["pois"]:
        pois.append({**p, "source": "graph"})
    authored_path = content_dir / "pois_authored.json"
    if authored_path.exists():
        authored = json.loads(authored_path.read_text())
        for p in authored.get("pois", []):
            pois.append({**p, "source": "authored"})
    return pois


def load_overrides(content_dir: Path) -> dict[str, tuple[float, float]]:
    """POI id (lowercased) → entrance coord."""
    path = content_dir / "poi_entrance_overrides.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    return {
        o["id"].lower(): (o["entranceCoord"]["latitude"], o["entranceCoord"]["longitude"])
        for o in doc.get("overrides", [])
    }


# --- snap audit ---------------------------------------------------------------

def snap_nearest(
    proj: Projection,
    centers: dict[tuple[int, int], tuple[float, float]],
    coord: tuple[float, float],
    threshold_meters: float,
) -> tuple[tuple[int, int] | None, float, bool]:
    """Mirrors POICatalog.snapAudit's per-POI search.

    Returns (nearest passable cell or None, distance in meters,
    within_threshold). Distance is inf when nothing was found inside
    MAX_WARNING_RINGS.
    """
    flat_to_flat = proj.apothem * 2.0
    min_ring_spacing = flat_to_flat * (math.sqrt(3.0) / 2.0)
    threshold_rings = max(1, math.ceil(threshold_meters / min_ring_spacing) + 1)

    p_hex = proj.hex_at(*coord)
    best_cell = None
    best_dist = math.inf
    found_within = False

    for radius in range(threshold_rings + 1):
        for h in ring(p_hex, radius):
            center = centers.get(h)
            if center is None:
                continue
            d = distance_meters(coord, center)
            if d < best_dist:
                best_dist = d
                best_cell = h
            if d <= threshold_meters:
                found_within = True
        if found_within:
            return (best_cell, best_dist, True)

    if best_cell is None:
        for radius in range(threshold_rings + 1, MAX_WARNING_RINGS + 1):
            for h in ring(p_hex, radius):
                center = centers.get(h)
                if center is None:
                    continue
                d = distance_meters(coord, center)
                if d < best_dist:
                    best_dist = d
                    best_cell = h
            if best_cell is not None:
                break
    return (best_cell, best_dist, False)


# --- connectivity --------------------------------------------------------------

def connected_components(passable: set[tuple[int, int]]) -> dict[tuple[int, int], int]:
    """Label each passable cell with a component id via BFS."""
    component = {}
    next_id = 0
    for start in passable:
        if start in component:
            continue
        component[start] = next_id
        queue = deque([start])
        while queue:
            q, r = queue.popleft()
            for dq, dr in FLAT_TOP_DIRECTIONS:
                n = (q + dq, r + dr)
                if n in passable and n not in component:
                    component[n] = next_id
                    queue.append(n)
        next_id += 1
    return component


# --- report -------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--content-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="themed-content checkout (default: this script's repo)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="snap-audit pass distance in meters (default 5, matches the app)",
    )
    parser.add_argument(
        "--grid",
        type=Path,
        default=None,
        help="hexgrid file to audit (default: <content-dir>/hexgrid.json); "
        "use to gate a candidate like hexgrid_clipped.json before adopting it",
    )
    args = parser.parse_args()
    content_dir = args.content_dir

    grid_path = args.grid or (content_dir / "hexgrid.json")
    grid = json.loads(grid_path.read_text())
    cells = hexgrid_format.load_cells(grid)
    proj = Projection(grid["origin"])
    pois = load_pois(content_dir)
    overrides = load_overrides(content_dir)
    config_path = content_dir / "hex_report_config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}

    # Passable = kind != restricted, mirroring HexCell.isPassable.
    passable_cells = [c for c in cells if c["kind"] != "restricted"]
    passable = {(c["q"], c["r"]) for c in passable_cells}
    centers = {(c["q"], c["r"]): proj.center(c["q"], c["r"]) for c in passable_cells}

    kind_counts: dict[str, int] = {}
    for c in cells:
        kind_counts[c["kind"]] = kind_counts.get(c["kind"], 0) + 1
    missing_elevation = sum(1 for c in cells if c["elevationMeters"] is None)

    print("=== Grid summary ===")
    print(f"cells: {len(cells)} total, {len(passable)} passable")
    for kind in sorted(kind_counts, key=kind_counts.get, reverse=True):
        print(f"  {kind}: {kind_counts[kind]}")
    print(f"missing elevation: {missing_elevation}")
    print()

    # --- snap audit ---
    audited = []
    for poi in pois:
        coord = overrides.get(poi["id"].lower())
        overridden = coord is not None
        if coord is None:
            coord = (poi["coord"]["latitude"], poi["coord"]["longitude"])
        cell, dist, ok = snap_nearest(proj, centers, coord, args.threshold)
        audited.append({
            "poi": poi,
            "cell": cell,
            "distance": dist,
            "ok": ok,
            "overridden": overridden,
        })

    failures = sorted(
        (a for a in audited if not a["ok"]),
        key=lambda a: (-a["distance"] if math.isfinite(a["distance"]) else -math.inf,
                       a["poi"].get("name") or a["poi"]["id"]),
    )
    # inf (nothing within the ring cap) sorts first — worst of the worst.
    failures.sort(key=lambda a: not math.isinf(a["distance"]))

    print(f"=== Snap audit (threshold {args.threshold:g} m) ===")
    print(f"POIs audited: {len(audited)} "
          f"({sum(1 for a in audited if a['overridden'])} with entrance overrides)")
    print(f"pass: {len(audited) - len(failures)}, fail: {len(failures)}")
    for a in failures:
        poi = a["poi"]
        name = poi.get("name") or poi["id"]
        park = poi.get("park", poi["source"])
        if math.isinf(a["distance"]):
            where = f"no passable cell within {MAX_WARNING_RINGS} rings"
        else:
            lat, lng = centers[a["cell"]]
            where = (f"nearest {a['distance']:5.1f} m at cell {a['cell']} "
                     f"({lat:.6f}, {lng:.6f})")
        print(f"  FAIL {name} [{poi['kind']}, {park}] — {where}")
    print()

    # --- connectivity ---
    component = connected_components(passable)
    comp_sizes: dict[int, int] = {}
    for comp_id in component.values():
        comp_sizes[comp_id] = comp_sizes.get(comp_id, 0) + 1

    # Mainland = components holding a park entrance's snapped cell.
    entrance_pois = [p for p in pois if p["kind"] == "parkEntrance"]
    mainland_ids = set()
    entrance_components = {}
    for poi in entrance_pois:
        coord = overrides.get(poi["id"].lower())
        if coord is None:
            coord = (poi["coord"]["latitude"], poi["coord"]["longitude"])
        cell, _, _ = snap_nearest(proj, centers, coord, args.threshold)
        if cell is not None:
            comp_id = component[cell]
            mainland_ids.add(comp_id)
            entrance_components[poi.get("name") or poi["id"]] = comp_id

    # Intentional islands from hex_report_config.json.
    intentional_ids: dict[int, str] = {}
    unpainted_islands = []
    bridged_islands: list[str] = []
    for island in config.get("intentionalIslands", []):
        seed = (island["seed"]["latitude"], island["seed"]["longitude"])
        cell, _, _ = snap_nearest(proj, centers, seed, args.threshold)
        if cell is None:
            unpainted_islands.append(island["name"])
            continue
        comp_id = component[cell]
        if comp_id not in mainland_ids:
            intentional_ids[comp_id] = island["name"]
        else:
            # The seed reached the mainland component — either the island
            # is spuriously bridged (a painted crossing to erase) or the
            # seed coordinate isn't actually on the island. Don't record
            # it as intentional (that would exempt the whole mainland);
            # surface it with the snapped cell for jump-to instead.
            lat, lng = centers[cell]
            bridged_islands.append(
                f"{island['name']}: seed snapped to MAINLAND cell {cell} "
                f"({lat:.6f}, {lng:.6f}) — spurious bridge or misplaced seed"
            )

    stray = [
        (comp_id, size) for comp_id, size in comp_sizes.items()
        if comp_id not in mainland_ids and comp_id not in intentional_ids
    ]
    stray.sort(key=lambda t: -t[1])

    print("=== Connectivity ===")
    print(f"components: {len(comp_sizes)} "
          f"(mainland {len(mainland_ids)}, intentional {len(intentional_ids)}, "
          f"stray {len(stray)})")
    if len(mainland_ids) > 1:
        print("  WARNING: park entrances are in DIFFERENT components:")
        for name, comp_id in entrance_components.items():
            print(f"    {name}: component {comp_id} ({comp_sizes[comp_id]} cells)")
    elif entrance_components:
        only = next(iter(mainland_ids))
        print(f"mainland: {comp_sizes[only]} cells "
              f"({', '.join(entrance_components)})")
    else:
        print("  WARNING: no park entrance snapped to any cell — no mainland!")
    for comp_id, name in intentional_ids.items():
        print(f"intentional island: {name} — {comp_sizes[comp_id]} cells")
    for name in unpainted_islands:
        print(f"  note: intentional island {name!r} has no painted cells near its seed")
    for msg in bridged_islands:
        print(f"  FAIL {msg}")
    if stray:
        stray_cells = sum(size for _, size in stray)
        print(f"stray islands: {len(stray)} totaling {stray_cells} cells; largest:")
        # Centroid of each stray component for jump-to (top 20).
        cells_by_comp: dict[int, list[tuple[int, int]]] = {}
        top_ids = {comp_id for comp_id, _ in stray[:20]}
        for cell_coord, comp_id in component.items():
            if comp_id in top_ids:
                cells_by_comp.setdefault(comp_id, []).append(cell_coord)
        for comp_id, size in stray[:20]:
            members = cells_by_comp[comp_id]
            lat = sum(centers[m][0] for m in members) / len(members)
            lng = sum(centers[m][1] for m in members) / len(members)
            print(f"  {size:6d} cells near ({lat:.6f}, {lng:.6f})")
        if len(stray) > 20:
            print(f"  (+ {len(stray) - 20} more)")
    print()

    # --- POIs on the wrong component ---
    on_stray = []
    on_intentional = []
    for a in audited:
        if a["cell"] is None:
            continue
        comp_id = component[a["cell"]]
        if comp_id in mainland_ids:
            continue
        name = a["poi"].get("name") or a["poi"]["id"]
        if comp_id in intentional_ids:
            on_intentional.append((name, intentional_ids[comp_id]))
        else:
            on_stray.append((name, comp_id, comp_sizes[comp_id]))

    print("=== POI component check ===")
    for name, island in on_intentional:
        print(f"  info: {name} is on intentional island {island!r} "
              "(routes to it will fail until it has a mainland entrance override)")
    for name, comp_id, size in sorted(on_stray, key=lambda t: t[0]):
        print(f"  FAIL {name} snapped to a stray island ({size} cells) — "
              "unroutable from the mainland")
    if not on_stray and not on_intentional:
        print("  every snapped POI is on the mainland")
    print()

    # --- gate ---
    problems = []
    if failures:
        problems.append(f"{len(failures)} POIs beyond {args.threshold:g} m")
    if len(mainland_ids) != 1:
        problems.append("park entrances not in exactly one component")
    if stray:
        problems.append(f"{len(stray)} stray islands")
    if bridged_islands:
        problems.append(f"{len(bridged_islands)} intentional islands bridged to mainland")
    if on_stray:
        problems.append(f"{len(on_stray)} POIs on stray islands")
    if problems:
        print(f"RELEASE GATE: FAIL — {'; '.join(problems)}")
        sys.exit(1)
    print("RELEASE GATE: PASS")


if __name__ == "__main__":
    main()
