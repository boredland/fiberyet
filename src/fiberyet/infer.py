"""Fill unmeasured addresses from their nearest measured neighbours.

Hold-out measurements are never neighbours: they exist only to score the map.
"""

from __future__ import annotations

import sqlite3

import numpy as np
from scipy.spatial import cKDTree

K = 5
RADIUS_M = 150.0
MIN_NEIGHBOURS = 3
YES_AT = 0.8
NO_AT = 0.2
MIN_DIST_M = 5.0


def state_of(p_yes: float | None, n_neighbours: int) -> str:
    """'inferred yes' | 'inferred no' | 'uncertain' for an unmeasured address."""
    if p_yes is None or n_neighbours < MIN_NEIGHBOURS:
        return "uncertain"
    if p_yes >= YES_AT:
        return "inferred yes"
    if p_yes <= NO_AT:
        return "inferred no"
    return "uncertain"


def neighbour_table(con: sqlite3.Connection, provider: str):
    rows = con.execute(
        "SELECT a.x, a.y, m.status FROM measurement m JOIN address a ON a.id = m.address_id"
        " WHERE m.provider = ? AND m.status IN ('yes','no') AND m.stage != 'holdout'",
        (provider,),
    ).fetchall()
    if not rows:
        return None, None
    pts = np.array([[r["x"], r["y"]] for r in rows])
    yes = np.array([1.0 if r["status"] == "yes" else 0.0 for r in rows])
    return cKDTree(pts), yes


def p_yes_at(tree: cKDTree | None, yes: np.ndarray | None, x: float, y: float) -> tuple[float | None, int]:
    if tree is None:
        return None, 0
    dist, idx = tree.query([x, y], k=K, distance_upper_bound=RADIUS_M)
    dist = np.atleast_1d(dist)
    idx = np.atleast_1d(idx)
    keep = np.isfinite(dist)
    if not keep.any():
        return None, 0
    d = dist[keep]
    w = 1.0 / np.maximum(d, MIN_DIST_M)
    return float((w * yes[idx[keep]]).sum() / w.sum()), int(keep.sum())


def predict(con: sqlite3.Connection, provider: str) -> int:
    """Rewrite this provider's prediction rows for every address that has no yes/no measurement."""
    tree, yes = neighbour_table(con, provider)
    measured = {
        r[0]
        for r in con.execute(
            "SELECT address_id FROM measurement WHERE provider = ? AND status IN ('yes','no')"
            " AND stage != 'holdout'",
            (provider,),
        )
    }
    out = []
    for r in con.execute("SELECT id, x, y FROM address"):
        if r["id"] in measured:
            continue
        p, n = p_yes_at(tree, yes, r["x"], r["y"])
        out.append((r["id"], provider, p, n))
    with con:
        con.execute("DELETE FROM prediction WHERE provider = ?", (provider,))
        con.executemany(
            "INSERT INTO prediction (address_id, provider, p_yes, n_neighbours) VALUES (?,?,?,?)", out
        )
    return len(out)
