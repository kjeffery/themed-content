#!/usr/bin/env python3
"""Clip `hexgrid.json` to the authored boundary and stamp exclusions.

Usage:
    tools/hexgrid_clip.py [--content-dir DIR] [--out hexgrid_clipped.json]
                          [--in-place]

Two stages, run offline whenever the grid is re-seeded:

1. Clip — keep only cells whose center is inside a `hex_boundary.geojson`
   feature or within that feature's `bufferMeters` of it. The boundary is
   the hand-owned "resort pedestrian area": land polygons (seeded from
   lands.json) plus the esplanade/transit polygon reaching the authored
   hand-off waypoints. Everything else — surrounding Anaheim streets,
   hotels, parking — is dropped; navigation beyond the boundary hands
   off to Apple Maps.

2. Prune — flood-fill the clipped cells and drop stray components not
   reachable from a park entrance. The boundary buffer inevitably
   half-catches backstage slivers along the perimeter; disconnected
   from the mainland, they are dead weight. Safety valve: a component
   is NEVER dropped when any POI's nearest cell is on it (that is real
   guest area with a severed connection — fix the boundary or paint
   the link) or when it holds an intentional island's seed (Tom Sawyer
   Island). `--keep-strays` skips this stage.

3. Exclusions — stamp cells inside any `hex_exclusions.json` polygon as
   `restricted`. This is the durable, hand-authored "never walkable"
   layer (cast-only overflow paths etc.). It runs LAST so nothing
   earlier in the pipeline can re-admit those cells, and it stamps
   rather than deletes so the next OSM seed proposes nothing there
   (erased cells get re-proposed; restricted cells are walls).

Writes a candidate file next to the source by default — adopt it after
`tools/hexgrid_report.py --grid hexgrid_clipped.json` passes, or use
--in-place once you trust the boundary.

Geometry goes through hexgrid_format.Projection (mirrors the Swift
GridOrigin — same drift hazard as the other grid tools).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hexgrid_format  # noqa: E402
from hexgrid_format import FLAT_TOP_DIRECTIONS, Projection  # noqa: E402
# POI loading and the Swift-mirrored nearest-cell search live in the
# report tool; the prune stage reuses them to anchor components.
from hexgrid_report import load_overrides, load_pois, snap_nearest  # noqa: E402

# Edge y-bin height in meters. Bins keep point-in-polygon and
# distance-to-edge probes to a handful of edges instead of the whole
# ring (Fantasyland alone has 444 vertices).
BIN_METERS = 8.0


class PolygonIndex:
    """A polygon in local meters with y-binned edges for fast probes."""

    def __init__(self, ring_local: list[tuple[float, float]], buffer_meters: float):
        self.buffer = buffer_meters
        # Drop an explicit closing vertex; edges wrap implicitly.
        if len(ring_local) > 1 and ring_local[0] == ring_local[-1]:
            ring_local = ring_local[:-1]
        self.pts = ring_local
        xs = [p[0] for p in ring_local]
        ys = [p[1] for p in ring_local]
        self.min_x = min(xs) - buffer_meters
        self.max_x = max(xs) + buffer_meters
        self.min_y = min(ys) - buffer_meters
        self.max_y = max(ys) + buffer_meters
        self.bins: dict[int, list[int]] = {}
        n = len(ring_local)
        for i in range(n):
            y1 = ring_local[i][1]
            y2 = ring_local[(i + 1) % n][1]
            lo = int(min(y1, y2) // BIN_METERS)
            hi = int(max(y1, y2) // BIN_METERS)
            for b in range(lo, hi + 1):
                self.bins.setdefault(b, []).append(i)

    def in_bbox(self, x: float, y: float) -> bool:
        return self.min_x <= x <= self.max_x and self.min_y <= y <= self.max_y

    def contains(self, x: float, y: float) -> bool:
        """Even-odd crossing test over the point's y-bin only."""
        inside = False
        n = len(self.pts)
        for i in self.bins.get(int(y // BIN_METERS), ()):
            x1, y1 = self.pts[i]
            x2, y2 = self.pts[(i + 1) % n]
            if (y1 > y) != (y2 > y):
                if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
        return inside

    def within_buffer(self, x: float, y: float) -> bool:
        """True when the point is within `buffer` meters of any edge."""
        if self.buffer <= 0:
            return False
        n = len(self.pts)
        lo = int((y - self.buffer) // BIN_METERS)
        hi = int((y + self.buffer) // BIN_METERS)
        checked: set[int] = set()
        limit_sq = self.buffer * self.buffer
        for b in range(lo, hi + 1):
            for i in self.bins.get(b, ()):
                if i in checked:
                    continue
                checked.add(i)
                x1, y1 = self.pts[i]
                x2, y2 = self.pts[(i + 1) % n]
                dx = x2 - x1
                dy = y2 - y1
                length_sq = dx * dx + dy * dy
                if length_sq == 0:
                    px, py = x1, y1
                else:
                    t = ((x - x1) * dx + (y - y1) * dy) / length_sq
                    t = max(0.0, min(1.0, t))
                    px = x1 + t * dx
                    py = y1 + t * dy
                ex = x - px
                ey = y - py
                if ex * ex + ey * ey <= limit_sq:
                    return True
        return False

    def covers(self, x: float, y: float) -> bool:
        return self.in_bbox(x, y) and (
            self.contains(x, y) or self.within_buffer(x, y)
        )


def load_boundary(path: Path, proj: Projection) -> list[tuple[str, PolygonIndex]]:
    doc = json.loads(path.read_text())
    out = []
    for feature in doc["features"]:
        name = feature.get("properties", {}).get("name", "unnamed")
        buffer_m = float(feature.get("properties", {}).get("bufferMeters", 0))
        geom = feature["geometry"]
        if geom["type"] == "Polygon":
            polys = [geom["coordinates"]]
        elif geom["type"] == "MultiPolygon":
            polys = geom["coordinates"]
        else:
            raise SystemExit(f"boundary feature {name!r}: unsupported {geom['type']}")
        for rings in polys:
            # Outer ring only; boundary features are simple areas, holes
            # would mean "keep a moat inside the park" which nothing needs.
            local = [proj.local_offset(lat, lng) for lng, lat in rings[0]]
            out.append((name, PolygonIndex(local, buffer_m)))
    return out


def load_exclusions(path: Path, proj: Projection) -> list[tuple[str, PolygonIndex]]:
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    out = []
    for exclusion in doc.get("exclusions", []):
        local = [
            proj.local_offset(p["latitude"], p["longitude"])
            for p in exclusion["polygon"]
        ]
        out.append((exclusion.get("name", "unnamed"), PolygonIndex(local, 0.0)))
    return out


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
        help="source grid (default: <content-dir>/hexgrid.json)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="candidate output (default: <content-dir>/hexgrid_clipped.json)",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="overwrite the source grid instead of writing a candidate",
    )
    parser.add_argument(
        "--keep-strays",
        action="store_true",
        help="skip the prune stage (keep disconnected components)",
    )
    args = parser.parse_args()
    content_dir = args.content_dir

    grid_path = args.grid or (content_dir / "hexgrid.json")
    grid = json.loads(grid_path.read_text())
    cells = hexgrid_format.load_cells(grid)
    proj = Projection(grid["origin"])

    boundary = load_boundary(content_dir / "hex_boundary.geojson", proj)
    exclusions = load_exclusions(content_dir / "hex_exclusions.json", proj)

    before_counts: dict[str, int] = {}
    for c in cells:
        before_counts[c["kind"]] = before_counts.get(c["kind"], 0) + 1

    # --- stage 1: clip ---
    kept = []
    kept_by_feature = {name: 0 for name, _ in boundary}
    for c in cells:
        x, y = proj.center_local(c["q"], c["r"])
        c["_xy"] = (x, y)
        for name, poly in boundary:
            if poly.covers(x, y):
                kept.append(c)
                kept_by_feature[name] += 1
                break

    # --- stage 2: prune strays unreachable from the park entrances ---
    pruned = 0
    pruned_components = 0
    anchored_strays: list[str] = []
    if not args.keep_strays:
        passable = {(c["q"], c["r"]) for c in kept if c["kind"] != "restricted"}
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

        centers = {coord: proj.center(*coord) for coord in passable}
        pois = load_pois(content_dir)
        overrides = load_overrides(content_dir)
        config_path = content_dir / "hex_report_config.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}

        keep_ids: set[int] = set()
        anchor_names: dict[int, list[str]] = {}
        for poi in pois:
            coord = overrides.get(poi["id"].lower())
            if coord is None:
                coord = (poi["coord"]["latitude"], poi["coord"]["longitude"])
            cell, _, _ = snap_nearest(proj, centers, coord, 5.0)
            if cell is None:
                continue
            comp_id = component[cell]
            keep_ids.add(comp_id)
            if poi["kind"] == "parkEntrance":
                anchor_names.setdefault(comp_id, [])  # mainland, not a stray
            else:
                anchor_names.setdefault(comp_id, []).append(
                    poi.get("name") or poi["id"]
                )
        mainland_ids = set()
        for poi in pois:
            if poi["kind"] != "parkEntrance":
                continue
            coord = overrides.get(poi["id"].lower())
            if coord is None:
                coord = (poi["coord"]["latitude"], poi["coord"]["longitude"])
            cell, _, _ = snap_nearest(proj, centers, coord, 5.0)
            if cell is not None:
                mainland_ids.add(component[cell])
        intentional_ids: set[int] = set()
        for island in config.get("intentionalIslands", []):
            seed = (island["seed"]["latitude"], island["seed"]["longitude"])
            cell, _, _ = snap_nearest(proj, centers, seed, 5.0)
            if cell is not None:
                keep_ids.add(component[cell])
                intentional_ids.add(component[cell])

        comp_sizes: dict[int, int] = {}
        for comp_id in component.values():
            comp_sizes[comp_id] = comp_sizes.get(comp_id, 0) + 1
        survivors = []
        for c in kept:
            coord = (c["q"], c["r"])
            comp_id = component.get(coord)
            # Restricted cells carry no component; keep them — they are
            # walls, and dropping them would re-open erased OSM noise.
            if comp_id is None or comp_id in keep_ids:
                survivors.append(c)
            else:
                pruned += 1
        pruned_components = len(
            [cid for cid in comp_sizes if cid not in keep_ids]
        )
        for comp_id, names in anchor_names.items():
            if comp_id not in mainland_ids and comp_id not in intentional_ids and names:
                anchored_strays.append(
                    f"{comp_sizes[comp_id]} cells kept for {', '.join(sorted(set(names)))}"
                )
        kept = survivors

    # --- stage 3: exclusions stamp LAST, so they always win ---
    stamped_by_exclusion = {name: 0 for name, _ in exclusions}
    for c in kept:
        x, y = c["_xy"]
        for name, poly in exclusions:
            if poly.in_bbox(x, y) and poly.contains(x, y):
                if c["kind"] != "restricted":
                    c["kind"] = "restricted"
                    stamped_by_exclusion[name] += 1
                break
    for c in kept:
        del c["_xy"]

    after_counts: dict[str, int] = {}
    for c in kept:
        after_counts[c["kind"]] = after_counts.get(c["kind"], 0) + 1

    print("=== Clip report ===")
    print(f"cells: {len(cells)} -> {len(kept)} "
          f"(dropped {len(cells) - len(kept)}, "
          f"{100.0 * (len(cells) - len(kept)) / len(cells):.1f}%)")
    for kind in sorted(set(before_counts) | set(after_counts)):
        print(f"  {kind}: {before_counts.get(kind, 0)} -> {after_counts.get(kind, 0)}")
    if not args.keep_strays:
        print(f"pruned: {pruned} cells in {pruned_components} stray components "
              "unreachable from a park entrance")
        for line in anchored_strays:
            print(f"  kept disconnected (POI anchor): {line} "
                  "— extend the boundary or paint the missing link")
    print("kept cells by boundary feature (first match wins):")
    for name, count in sorted(kept_by_feature.items(), key=lambda t: -t[1]):
        flag = "  <-- kept NOTHING, check this polygon" if count == 0 else ""
        print(f"  {count:7d}  {name}{flag}")
    if exclusions:
        print("exclusion stamps:")
        for name, count in stamped_by_exclusion.items():
            flag = "  <-- stamped NOTHING, check this polygon" if count == 0 else ""
            print(f"  {count:7d}  {name}{flag}")
    else:
        print("exclusions: none authored yet (hex_exclusions.json is empty)")

    out_path = grid_path if args.in_place else (args.out or content_dir / "hexgrid_clipped.json")
    out = hexgrid_format.build_grid(grid["destination"], grid["origin"], kept)
    out_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {out_path} ({out_path.stat().st_size} bytes)")
    if not args.in_place:
        print("next: python3 tools/hexgrid_report.py --grid "
              f"{out_path.name}   # gate the candidate before adopting it")


if __name__ == "__main__":
    main()
