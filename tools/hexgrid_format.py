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

# Keep in sync with `HexGrid.currentFormatVersion`.
CURRENT_FORMAT_VERSION = 1


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
