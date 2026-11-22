"""One slice of the incremental crawl: plan, measure, checkpoint, stop on a block.

Output is counts only. Actions logs of a public repository are public, so
nothing here may print an address, a URL or a response body.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from . import infer, sampling, store
from .fetch import Blocked, Unavailable

TS = "%Y-%m-%dT%H:%M:%SZ"
REFRESH_AFTER_DAYS = 90
REFRESH_DAILY_QUOTA = 2000
DEADLINE_MARGIN_S = 60.0
BLOCK_PAUSE_HOURS = 24
BLOCK_PAUSE_S = 10.0

Measure = Callable[[str, str], "object"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def read_state(data_dir: Path, provider: str) -> dict:
    path = data_dir / "state" / f"{provider}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"blocks": [], "pause_until": None, "pause_s": None, "disabled": False}


def write_state(data_dir: Path, provider: str, state: dict) -> None:
    path = data_dir / "state" / f"{provider}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def crawl_pending(
    con: sqlite3.Connection,
    provider: str,
    measure: Measure,
    *,
    should_stop: Callable[[], bool],
    tick: Callable[[], None],
    now: Callable[[], dt.datetime] = utcnow,
) -> int:
    """Measure pending rows in rowid order, committing each, so any stop resumes cleanly."""
    rows = con.execute(
        "SELECT m.rowid AS rid, a.street, a.house_number FROM measurement m"
        " JOIN address a ON a.id = m.address_id WHERE m.provider = ? AND m.status = 'pending'"
        " ORDER BY m.rowid",
        (provider,),
    ).fetchall()
    done = 0
    for row in rows:
        if should_stop():
            break
        result = measure(row["street"], row["house_number"])
        with con:
            con.execute(
                "UPDATE measurement SET status = ?, detail = ?, raw = ?, measured_at = ? WHERE rowid = ?",
                (
                    result.status,
                    result.detail,
                    None if result.status in ("yes", "no") else result.raw,
                    now().strftime(TS),
                    row["rid"],
                ),
            )
        done += 1
        tick()
    return done


def requeue_stale(con: sqlite3.Connection, provider: str, now: dt.datetime) -> int:
    """Rollouts change answers, so old measurements expire. Re-measure the oldest, within a daily quota."""
    today = now.strftime("%Y-%m-%d")
    used = con.execute(
        "SELECT COUNT(*) FROM measurement WHERE provider = ? AND measured_at LIKE ?", (provider, today + "%")
    ).fetchone()[0]
    quota = REFRESH_DAILY_QUOTA - used
    if quota <= 0:
        return 0
    cutoff = (now - dt.timedelta(days=REFRESH_AFTER_DAYS)).strftime(TS)
    ids = [
        r[0]
        for r in con.execute(
            "SELECT address_id FROM measurement WHERE provider = ? AND status IN ('yes','no')"
            " AND measured_at < ? ORDER BY (stage = 'holdout') DESC, measured_at, address_id LIMIT ?",
            (provider, cutoff, quota),
        )
    ]
    with con:
        con.executemany(
            "UPDATE measurement SET status = 'pending', measured_at = NULL, raw = NULL"
            " WHERE address_id = ? AND provider = ?",
            [(i, provider) for i in ids],
        )
    return len(ids)


def plan_next(
    con: sqlite3.Connection, provider: str, districts: list[str] | None, now: dt.datetime
) -> tuple[str, int]:
    n = sampling.holdout_tasks(con, provider)
    if n:
        return "holdout", n
    n = sampling.coarse(con, provider, districts)
    if n:
        return "coarse", n
    n = sampling.retry_unmatched(con, provider, districts)
    if n:
        return "retry", n
    infer.predict(con, provider)
    n = sampling.refine(con, provider, districts)
    if n:
        return "refine", n
    n = requeue_stale(con, provider, now)
    if n:
        return "refresh", n
    return "idle", 0


def _record_block(state: dict, status: int, now: dt.datetime) -> None:
    state["blocks"].append({"at": now.strftime(TS), "status": status})
    if len(state["blocks"]) == 1:
        state["pause_until"] = (now + dt.timedelta(hours=BLOCK_PAUSE_HOURS)).strftime(TS)
        state["pause_s"] = BLOCK_PAUSE_S
    else:
        state["disabled"] = True


def advance(
    con: sqlite3.Connection,
    data_dir: Path,
    provider: str,
    measure: Measure,
    *,
    budget_s: float,
    checkpoint_s: float,
    districts: list[str] | None = None,
    push: bool = True,
    margin_s: float = DEADLINE_MARGIN_S,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], dt.datetime] = utcnow,
    log: Callable[[str], None] = print,
) -> int:
    """Run one slice. Exit code 0 normally, 3 when the provider blocked us."""
    state = read_state(data_dir, provider)
    if state["disabled"]:
        log(f"{provider}: disabled after repeated blocks")
        return 0
    if state["pause_until"] and now().strftime(TS) < state["pause_until"]:
        log(f"{provider}: paused until {state['pause_until']}")
        return 0

    deadline = clock() + budget_s
    last_checkpoint = [clock()]
    measured = 0

    def should_stop() -> bool:
        return clock() + margin_s >= deadline

    def checkpoint(message: str) -> None:
        store.save(con, data_dir, provider)
        if push:
            store.push(data_dir, message)
        last_checkpoint[0] = clock()

    def tick() -> None:
        if clock() - last_checkpoint[0] >= checkpoint_s:
            checkpoint(f"data: {provider} checkpoint {now().strftime(TS)}")

    stamp = lambda: now().strftime("%Y-%m-%dT%H:%MZ")
    try:
        while not should_stop():
            measured += crawl_pending(con, provider, measure, should_stop=should_stop, tick=tick, now=now)
            if should_stop():
                break
            phase, planned = plan_next(con, provider, districts, now())
            log(f"{provider}: planned {phase} +{planned}")
            if phase == "idle":
                break
    except Blocked as exc:
        _record_block(state, exc.status, now())
        write_state(data_dir, provider, state)
        checkpoint(f"data: {provider} blocked {stamp()}")
        log(f"{provider}: BLOCKED status={exc.status}; paused, {measured} rows measured this run")
        return 3
    except Unavailable:
        checkpoint(f"data: {provider} unavailable {stamp()}")
        log(f"{provider}: unavailable after retries; {measured} rows measured this run")
        return 0

    checkpoint(f"data: {provider} slice {stamp()}")
    pending = con.execute(
        "SELECT COUNT(*) FROM measurement WHERE provider = ? AND status = 'pending'", (provider,)
    ).fetchone()[0]
    log(f"{provider}: {measured} rows measured this run, {pending} pending")
    return 0
