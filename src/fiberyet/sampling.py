"""What to measure next. All randomness is seeded so a rerun picks the same addresses."""

from __future__ import annotations

import math
import random
import sqlite3

from .infer import neighbour_table, p_yes_at, state_of

SEED = 20261122
HOLDOUT_N = 1000
BUDGET_SHARE = 0.15
REFINE_BATCH = 1000
REFINE_CELL_M = 25


def holdout_ids(ids: list[str]) -> list[str]:
    return random.Random(SEED).sample(sorted(ids), min(HOLDOUT_N, len(ids)))


def mark_holdout(con: sqlite3.Connection) -> int:
    ids = [r[0] for r in con.execute("SELECT id FROM address")]
    chosen = holdout_ids(ids)
    with con:
        con.execute("UPDATE address SET holdout = 0")
        con.executemany("UPDATE address SET holdout = 1 WHERE id = ?", [(i,) for i in chosen])
    return len(chosen)


def _insert(con: sqlite3.Connection, provider: str, stage: str, ids: list[str]) -> int:
    """Insert pending rows; returns how many were new (rows that already exist are ignored)."""
    before = con.total_changes
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO measurement (address_id, provider, stage, status) VALUES (?,?,?,'pending')",
            [(i, provider, stage) for i in ids],
        )
    return con.total_changes - before


def holdout_tasks(con: sqlite3.Connection, provider: str) -> int:
    ids = [r[0] for r in con.execute("SELECT id FROM address WHERE holdout = 1 ORDER BY id")]
    return _insert(con, provider, "holdout", ids)


def _scope(districts: list[str] | None) -> tuple[str, list[str]]:
    if not districts:
        return "", []
    return f" AND district IN ({','.join('?' * len(districts))})", list(districts)


def coarse(con: sqlite3.Connection, provider: str, districts: list[str] | None = None) -> int:
    """One random non-hold-out address per 100 m cell that has none measured or pending yet."""
    clause, args = _scope(districts)
    rng = random.Random(f"{SEED}:{provider}:coarse")
    done = {
        r[0]
        for r in con.execute(
            "SELECT a.cell FROM measurement m JOIN address a ON a.id = m.address_id WHERE m.provider = ?",
            (provider,),
        )
    }
    by_cell: dict[str, list[str]] = {}
    for r in con.execute(f"SELECT id, cell FROM address WHERE holdout = 0{clause} ORDER BY id", args):
        by_cell.setdefault(r["cell"], []).append(r["id"])
    ids = [rng.choice(members) for cell, members in sorted(by_cell.items()) if cell not in done]
    return _insert(con, provider, "coarse", ids)


def retry_unmatched(con: sqlite3.Connection, provider: str, districts: list[str] | None = None) -> int:
    """A new random address in each cell whose measurements are all unmatched; at most 3 tries per cell."""
    clause, args = _scope(districts)
    rng = random.Random(f"{SEED}:{provider}:retry:{con.execute('SELECT COUNT(*) FROM measurement').fetchone()[0]}")
    tried: dict[str, set[str]] = {}
    state: dict[str, list[str]] = {}
    for r in con.execute(
        "SELECT a.cell, m.address_id, m.status FROM measurement m JOIN address a ON a.id = m.address_id"
        " WHERE m.provider = ? AND m.stage != 'holdout'",
        (provider,),
    ):
        tried.setdefault(r["cell"], set()).add(r["address_id"])
        state.setdefault(r["cell"], []).append(r["status"])
    ids = []
    for cell, statuses in sorted(state.items()):
        if len(statuses) >= 3 or any(s != "unmatched" for s in statuses):
            continue
        pool = [
            r["id"]
            for r in con.execute(
                f"SELECT id FROM address WHERE holdout = 0 AND cell = ?{clause} ORDER BY id", [cell, *args]
            )
            if r["id"] not in tried[cell]
        ]
        if pool:
            ids.append(rng.choice(pool))
    return _insert(con, provider, "coarse", ids)


def budget_rows(con: sqlite3.Connection, districts: list[str] | None) -> int:
    clause, args = _scope(districts)
    n = con.execute(f"SELECT COUNT(*) FROM address WHERE 1=1{clause}", args).fetchone()[0]
    return math.floor(BUDGET_SHARE * n) + HOLDOUT_N


def refine(con: sqlite3.Connection, provider: str, districts: list[str] | None = None) -> int:
    """Measure the addresses whose neighbours disagree most, one per 25 m cell, until the budget is spent."""
    have = con.execute("SELECT COUNT(*) FROM measurement WHERE provider = ?", (provider,)).fetchone()[0]
    room = budget_rows(con, districts) - have
    if room <= 0:
        return 0
    tree, yes = neighbour_table(con, provider)
    clause, args = _scope(districts)
    measured = {r[0] for r in con.execute("SELECT address_id FROM measurement WHERE provider = ?", (provider,))}
    scored = []
    for r in con.execute(f"SELECT id, x, y FROM address WHERE holdout = 0{clause}", args):
        if r["id"] in measured:
            continue
        p, n = p_yes_at(tree, yes, r["x"], r["y"])
        if state_of(p, n) != "uncertain":
            continue
        # NULL (no neighbour at all) sorts first: nothing is known there.
        scored.append((-1.0 if p is None else abs(p - 0.5), r["id"], r["x"], r["y"]))
    scored.sort()
    seen, picked = set(), []
    for _, id_, x, y in scored:
        cell = (math.floor(x / REFINE_CELL_M), math.floor(y / REFINE_CELL_M))
        if cell in seen:
            continue
        seen.add(cell)
        picked.append(id_)
        if len(picked) >= min(REFINE_BATCH, room):
            break
    return _insert(con, provider, "refine", picked)
