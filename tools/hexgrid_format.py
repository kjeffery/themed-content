"""Shared read/write for the themed hexgrid wire format.

Mirrors `HexGrid`'s Codable in `themed/Routing/HexGrid.swift`. The format is
*columnar*: parallel arrays `q`/`r`/`kind`/`weight`/`accessibility` (one entry
per cell, index-aligned), an optional `elevationMeters` column (present only
when some cell has elevation; carries explicit nulls for cells that don't), and
a sparse attraction side-table (`attractionIndices` + `attractionEntityIDs`,
present only when some cell references an attraction).

`load_cells` reads it into a list of plain cell dicts; `build_grid` serializes
such a list back out. Accessibility is stored as the integer
`OptionSet.rawValue` per cell (the file is machine-generated at 300k+ cells, so
the int is far more compact than a name array).
"""
from __future__ import annotations

import math

# Keep in sync with `HexGrid.currentFormatVersion`.
CURRENT_FORMAT_VERSION = 1

EARTH_RADIUS_METERS = 6_371_000.0

# Flat-top axial neighbor directions — mirrors HexCoord.flatTopDirections.
FLAT_TOP_DIRECTIONS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]


# --- geometry, mirroring GridOrigin in themed/Routing/HexGrid.swift ---------
# Same drift hazard as the elevation enricher: changes to the Swift
# projection must be reflected here or Python-derived data diverges from
# what the runtime computes for the same cell.

def distance_meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Equirectangular distance — mirrors Coordinate.distance(to:)."""
    phi1 = math.radians(a[0])
    phi2 = math.radians(b[0])
    dlam = math.radians(b[1] - a[1])
    x = dlam * math.cos((phi1 + phi2) / 2.0)
    y = phi2 - phi1
    return EARTH_RADIUS_METERS * math.hypot(x, y)


def cube_round(fq: float, fr: float) -> tuple[int, int]:
    """FractionalHex.rounded() — cube round with largest-residual fixup."""
    fs = -fq - fr
    rq = round(fq)
    rr = round(fr)
    rs = round(fs)
    dq = abs(rq - fq)
    dr = abs(rr - fr)
    ds = abs(rs - fs)
    if dq > dr and dq > ds:
        rq = -rr - rs
    elif dr > ds:
        rr = -rq - rs
    return (int(rq), int(rr))


def ring(center: tuple[int, int], radius: int) -> list[tuple[int, int]]:
    """HexCoord.ring(radius:) — one walk around the ring."""
    if radius < 0:
        return []
    if radius == 0:
        return [center]
    out = []
    sq, sr = FLAT_TOP_DIRECTIONS[4]
    q = center[0] + sq * radius
    r = center[1] + sr * radius
    for edge in range(6):
        dq, dr = FLAT_TOP_DIRECTIONS[edge]
        for _ in range(radius):
            out.append((q, r))
            q += dq
            r += dr
    return out


class Projection:
    """GridOrigin's hex ↔ world mapping (flatTop / pointyTop)."""

    def __init__(self, origin: dict):
        self.lat0 = origin["latitude"]
        self.lng0 = origin["longitude"]
        self.apothem = origin["hexSizeMeters"]
        self.outer_radius = self.apothem * 2.0 / math.sqrt(3.0)
        self.orientation = origin.get("orientation", "flatTop")
        if self.orientation not in ("flatTop", "pointyTop"):
            raise ValueError(f"unknown orientation: {self.orientation!r}")
        self._cos_phi0 = math.cos(math.radians(self.lat0))

    def local_offset(self, lat: float, lng: float) -> tuple[float, float]:
        """World coordinate → east/north meters in the origin's tangent plane."""
        dx = math.radians(lng - self.lng0) * EARTH_RADIUS_METERS * self._cos_phi0
        dy = math.radians(lat - self.lat0) * EARTH_RADIUS_METERS
        return (dx, dy)

    def center(self, q: int, r: int) -> tuple[float, float]:
        R = self.outer_radius
        if self.orientation == "flatTop":
            dx = R * 1.5 * q
            dy = R * math.sqrt(3.0) * (r + q / 2.0)
        else:
            dx = R * math.sqrt(3.0) * (q + r / 2.0)
            dy = R * 1.5 * r
        lat = self.lat0 + math.degrees(dy / EARTH_RADIUS_METERS)
        lng = self.lng0 + math.degrees(dx / (EARTH_RADIUS_METERS * self._cos_phi0))
        return (lat, lng)

    def center_local(self, q: int, r: int) -> tuple[float, float]:
        """Cell center in local meters — cheaper than `center` in bulk loops."""
        R = self.outer_radius
        if self.orientation == "flatTop":
            return (R * 1.5 * q, R * math.sqrt(3.0) * (r + q / 2.0))
        return (R * math.sqrt(3.0) * (q + r / 2.0), R * 1.5 * r)

    def hex_at(self, lat: float, lng: float) -> tuple[int, int]:
        dx, dy = self.local_offset(lat, lng)
        R = self.outer_radius
        if self.orientation == "flatTop":
            fq = (2.0 / 3.0) * dx / R
            fr = (-1.0 / 3.0 * dx + math.sqrt(3.0) / 3.0 * dy) / R
        else:
            fq = (math.sqrt(3.0) / 3.0 * dx - 1.0 / 3.0 * dy) / R
            fr = (2.0 / 3.0) * dy / R
        return cube_round(fq, fr)


def load_cells(grid: dict) -> list[dict]:
    """Read a columnar grid into a list of cell dicts.

    Each returned dict has keys: q, r, kind, weight, accessibility (int),
    elevationMeters (float | None), attractionEntityID (str | None).
    """
    q = grid["q"]
    r = grid["r"]
    kind = grid["kind"]
    weight = grid["weight"]
    accessibility = grid["accessibility"]
    n = len(q)
    if not (len(r) == len(kind) == len(weight) == len(accessibility) == n):
        raise ValueError("columnar arrays have mismatched lengths")

    elevation = grid.get("elevationMeters")
    if elevation is not None and len(elevation) != n:
        raise ValueError("elevationMeters column length != cell count")

    attraction_by_index: dict[int, str] = {}
    indices = grid.get("attractionIndices", [])
    ids = grid.get("attractionEntityIDs", [])
    if len(indices) != len(ids):
        raise ValueError("attractionIndices / attractionEntityIDs length mismatch")
    for slot, cell_index in enumerate(indices):
        attraction_by_index[cell_index] = ids[slot]

    cells = []
    for i in range(n):
        cells.append({
            "q": q[i],
            "r": r[i],
            "kind": kind[i],
            "weight": weight[i],
            "accessibility": accessibility[i],
            "elevationMeters": elevation[i] if elevation is not None else None,
            "attractionEntityID": attraction_by_index.get(i),
        })
    return cells


def build_grid(destination: str, origin: dict, cells: list[dict]) -> dict:
    """Serialize normalized cell dicts into the columnar grid shape."""
    grid = {
        "formatVersion": CURRENT_FORMAT_VERSION,
        "destination": destination,
        "origin": origin,
        "q": [c["q"] for c in cells],
        "r": [c["r"] for c in cells],
        "kind": [c["kind"] for c in cells],
        "weight": [c["weight"] for c in cells],
        "accessibility": [c["accessibility"] for c in cells],
    }
    # Elevation column only when at least one cell carries one (matches
    # Swift, which omits it for a fresh pre-enrichment painter grid).
    if any(c.get("elevationMeters") is not None for c in cells):
        grid["elevationMeters"] = [c.get("elevationMeters") for c in cells]
    # Sparse attraction side-table, only when non-empty.
    indices = []
    ids = []
    for i, c in enumerate(cells):
        attraction = c.get("attractionEntityID")
        if attraction is not None:
            indices.append(i)
            ids.append(attraction)
    if indices:
        grid["attractionIndices"] = indices
        grid["attractionEntityIDs"] = ids
    return grid
