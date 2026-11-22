"""Score the map against the hold-out. The hold-out is never used to fill the map."""

from __future__ import annotations

import math
import sqlite3

from .infer import neighbour_table, p_yes_at, state_of


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def report(con: sqlite3.Connection, provider: str) -> dict:
    rows = con.execute(
        "SELECT a.x, a.y, m.status FROM measurement m JOIN address a ON a.id = m.address_id"
        " WHERE m.provider = ? AND m.stage = 'holdout'",
        (provider,),
    ).fetchall()
    usable = [r for r in rows if r["status"] in ("yes", "no")]
    n = len(usable)
    k = sum(1 for r in usable if r["status"] == "yes")
    tree, yes = neighbour_table(con, provider)
    uncertain = false_yes = false_no = decided = 0
    for r in usable:
        p, nn = p_yes_at(tree, yes, r["x"], r["y"])
        s = state_of(p, nn)
        if s == "uncertain":
            uncertain += 1
            continue
        decided += 1
        truth = r["status"] == "yes"
        if s == "inferred yes" and not truth:
            false_yes += 1
        if s == "inferred no" and truth:
            false_no += 1
    errors = false_yes + false_no
    return {
        "holdout_rows": len(rows),
        "holdout_usable": n,
        "unmatched": sum(1 for r in rows if r["status"] == "unmatched"),
        "error": sum(1 for r in rows if r["status"] == "error"),
        "pending": sum(1 for r in rows if r["status"] == "pending"),
        "coverage": k / n if n else None,
        "coverage_ci": wilson(k, n),
        "uncertain_share": uncertain / n if n else None,
        "decided": decided,
        "error_rate": errors / decided if decided else None,
        "error_ci": wilson(errors, decided),
        "false_yes": false_yes,
        "false_no": false_no,
    }


def agreement(con: sqlite3.Connection, a: str, b: str) -> dict[tuple[str, str], int]:
    """2x2 table of two providers' hold-out statuses."""
    table: dict[tuple[str, str], int] = {}
    for r in con.execute(
        "SELECT x.status AS sa, y.status AS sb FROM measurement x JOIN measurement y"
        " ON x.address_id = y.address_id AND y.provider = ? WHERE x.provider = ? AND x.stage = 'holdout'"
        " AND x.status IN ('yes','no') AND y.status IN ('yes','no')",
        (b, a),
    ):
        table[(r["sa"], r["sb"])] = table.get((r["sa"], r["sb"]), 0) + 1
    return table
