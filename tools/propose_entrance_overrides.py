#!/usr/bin/env python3
"""Generate reviewable entrance-override proposals for broken POIs.

Usage:
    tools/propose_entrance_overrides.py [--content-dir DIR] [--threshold 5]

Read-only reconciliation (never touches the live file): finds every POI
that would fail the release gate — no passable cell within the snap
threshold, or nearest cell on a component unreachable from the park
entrances — and proposes an `entranceCoord` for it:

- POIs on an intentional island (Tom Sawyer Island) get the island's
  authored `entrance` coord from hex_report_config.json (the raft dock),
  so navigation walks users to the crossing, not the far bank.
- Everything else gets the center of the nearest MAINLAND cell — the
  best automatic guess for "where the walkway meets this building".
  The runtime 5 m snap already made this guess implicitly; the override
  makes it explicit, reviewable, and gate-passing.

Existing hand-authored overrides are preserved verbatim and their POIs
are never re-proposed. Output is `poi_entrance_overrides_proposed.json`
next to the live file plus a worst-first review table on stdout.

Adopt after review:
    cp poi_entrance_overrides_proposed.json poi_entrance_overrides.json
    tools/publish.py poi_entrance_overrides.json
    tools/hexgrid_report.py   # should now be much closer to PASS
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hexgrid_format  # noqa: E402
from hexgrid_format import (  # noqa: E402
    FLAT_TOP_DIRECTIONS,
    Projection,
    distance_meters,
)
from hexgrid_report import load_overrides, load_pois, snap_nearest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--content-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="themed-content checkout (default: this script's repo)",
    )
    parser.add_argument(
        "--grid",
        type=Path,
        default=None,
        help="hexgrid to propose against (default: <content-dir>/hexgrid.json)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=5.0,
        help="snap-audit pass distance in meters (default 5, matches the app)",
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

    passable = {(c["q"], c["r"]) for c in cells if c["kind"] != "restricted"}
    centers = {coord: proj.center(*coord) for coord in passable}
    local = {coord: proj.center_local(*coord) for coord in passable}

    # Components + mainland, same rules as the report.
    component: dict[tuple[int, int], int] = {}
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

    mainland_ids: set[int] = set()
    for poi in pois:
        if poi["kind"] != "parkEntrance":
            continue
        coord = overrides.get(poi["id"].lower())
        if coord is None:
            coord = (poi["coord"]["latitude"], poi["coord"]["longitude"])
        cell, _, _ = snap_nearest(proj, centers, coord, args.threshold)
        if cell is not None:
            mainland_ids.add(component[cell])

    intentional: dict[int, dict] = {}
    for island in config.get("intentionalIslands", []):
        seed = (island["seed"]["latitude"], island["seed"]["longitude"])
        cell, _, _ = snap_nearest(proj, centers, seed, args.threshold)
        if cell is not None:
            intentional[component[cell]] = island

    mainland_local = [
        (xy[0], xy[1], coord)
        for coord, xy in local.items()
        if component[coord] in mainland_ids
    ]

    def nearest_mainland(lat: float, lng: float) -> tuple[tuple[int, int], float]:
        """Exact nearest mainland cell, unbounded (orphans can be far out)."""
        px, py = proj.local_offset(lat, lng)
        best = None
        best_sq = float("inf")
        for x, y, coord in mainland_local:
            dx = x - px
            dy = y - py
            d = dx * dx + dy * dy
            if d < best_sq:
                best_sq = d
                best = coord
        return best, best_sq ** 0.5

    proposals = []
    for poi in pois:
        if poi["kind"] == "parkEntrance":
            continue
        if poi["id"].lower() in overrides:
            continue  # hand-authored override wins, never re-propose
        pin = (poi["coord"]["latitude"], poi["coord"]["longitude"])
        cell, dist, ok = snap_nearest(proj, centers, pin, args.threshold)
        comp_id = component[cell] if cell is not None else None
        if ok and comp_id in mainland_ids:
            continue  # healthy POI

        name = poi.get("name") or poi["id"]
        if comp_id is not None and comp_id in intentional:
            island = intentional[comp_id]
            entrance = island.get("entrance")
            if entrance is None:
                print(f"  skip {name}: on {island['name']} which has no "
                      "authored entrance in hex_report_config.json")
                continue
            target = (entrance["latitude"], entrance["longitude"])
            reason = f"on {island['name']}; routed to its authored entrance"
            walk = distance_meters(pin, target)
        else:
            target_cell, walk = nearest_mainland(*pin)
            target = centers[target_cell]
            if comp_id is None:
                reason = "no passable cell anywhere near the pin"
            elif comp_id not in mainland_ids:
                reason = "nearest cell is on a disconnected fragment"
            else:
                reason = "nearest passable cell beyond threshold"
        proposals.append({
            "poi": poi,
            "target": target,
            "meters": walk,
            "reason": reason,
        })

    proposals.sort(key=lambda p: -p["meters"])

    print(f"=== Entrance override proposals ({len(proposals)}) ===")
    print(f"{'meters':>7}  POI")
    for p in proposals:
        poi = p["poi"]
        name = poi.get("name") or poi["id"]
        park = poi.get("park", poi["source"])
        flag = "  <-- far; verify against satellite" if p["meters"] > 25 else ""
        print(f"{p['meters']:7.1f}  {name} [{poi['kind']}, {park}] "
              f"— {p['reason']}{flag}")

    # Merged document: existing overrides verbatim + proposals appended.
    live_path = content_dir / "poi_entrance_overrides.json"
    live = json.loads(live_path.read_text()) if live_path.exists() else {
        "destination": grid.get("destination", ""),
        "formatVersion": 1,
        "overrides": [],
    }
    merged = list(live.get("overrides", []))
    for p in sorted(proposals, key=lambda p: p["poi"].get("name") or ""):
        poi = p["poi"]
        merged.append({
            "entranceCoord": {
                "latitude": p["target"][0],
                "longitude": p["target"][1],
            },
            "id": poi["id"].upper(),
            "note": (f"AUTO-PROPOSED for {poi.get('name') or poi['id']}: "
                     f"{p['reason']} ({p['meters']:.1f} m from pin) — review"),
        })
    out = dict(live)
    out["overrides"] = merged
    out_path = content_dir / "poi_entrance_overrides_proposed.json"
    out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {out_path.name}: {len(live.get('overrides', []))} existing "
          f"+ {len(proposals)} proposed")
    print("review, then: cp poi_entrance_overrides_proposed.json "
          "poi_entrance_overrides.json && tools/publish.py poi_entrance_overrides.json")


if __name__ == "__main__":
    main()
