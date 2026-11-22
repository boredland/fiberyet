"""One slice of the incremental crawl: plan, measure, flush, stop cleanly, commit once.

The shape follows trinkhallen-data's enrichment runs: work is bounded by a wall-clock budget and by
SIGTERM/SIGINT (in-flight work finishes, nothing new starts), progress is flushed to disk every
few rows, everything is committed once at the end with stats in the message, and stale entries are
re-queued under a TTL so settled ones are not asked again.

Output is counts only. Actions logs of a public repository are public, so nothing here may print an
address, a URL or a response body.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

from . import infer, sampling, store
from .fetch import Blocked, Unavailable

TS = "%Y-%m-%dT%H:%M:%SZ"
RECHECK_NO_AFTER_DAYS = 90
REFRESH_DAILY_QUOTA = 2000
DEADLINE_MARGIN_S = 60.0
BLOCK_PAUSE_HOURS = 24
BLOCK_PAUSE_MAX_HOURS = 24 * 7
BLOCK_PAUSE_S = 10.0
# Rows that came back unmatched or errored are not asked again for this long (cf. trinkhallen's *_attempted TTL).
RETRY_AFTER_DAYS = 30
# Written to data/ on disk this often, so a killed runner loses at most this many rows.
FLUSH_EVERY_ROWS = 100

Measure = Callable[[str, str], "object"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def read_state(data_dir: Path, provider: str) -> dict:
    path = data_dir / "state" / f"{provider}.json"
    if path.exists():
        state = json.loads(path.read_text())
        state.setdefault("streak", 0)
        return state
    return {"blocks": [], "streak": 0, "pause_until": None, "pause_s": None, "disabled": False}


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
    on_row: Callable[[], None],
    now: Callable[[], dt.datetime] = utcnow,
) -> Counter:
    """Measure pending rows in rowid order, committing each, so any stop resumes cleanly.

    Returns how many rows ended in each status.
    """
    rows = con.execute(
        "SELECT m.rowid AS rid, a.street, a.house_number FROM measurement m"
        " JOIN address a ON a.id = m.address_id WHERE m.provider = ? AND m.status = 'pending'"
        " ORDER BY m.rowid",
        (provider,),
    ).fetchall()
    done: Counter = Counter()
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
        done[result.status] += 1
        on_row()
    return done


def requeue_stale(con: sqlite3.Connection, provider: str, now: dt.datetime) -> int:
    """Re-measure what may have changed, oldest first, within a daily quota.

    A `yes` is never asked again: fiber that is orderable does not go away. A `no` is rechecked after
    RECHECK_NO_AFTER_DAYS, because a rollout can turn it into a `yes`. An `error` (a transient 4xx or
    5xx) is retried after RETRY_AFTER_DAYS.

    An `unmatched` row is not retried here, except in the hold-out: sampling.retry_unmatched already
    swaps in a different address for the same cell, so asking a permanently unmatched address again
    (a range like 109-111) would repeat itself every month. A hold-out address cannot be swapped
    without biasing the sample, so an unmatched one is retried.
    """
    today = now.strftime("%Y-%m-%d")
    used = con.execute(
        "SELECT COUNT(*) FROM measurement WHERE provider = ? AND measured_at LIKE ?", (provider, today + "%")
    ).fetchone()[0]
    quota = REFRESH_DAILY_QUOTA - used
    if quota <= 0:
        return 0
    no_cutoff = (now - dt.timedelta(days=RECHECK_NO_AFTER_DAYS)).strftime(TS)
    retry_cutoff = (now - dt.timedelta(days=RETRY_AFTER_DAYS)).strftime(TS)
    ids = [
        r[0]
        for r in con.execute(
            "SELECT address_id FROM measurement WHERE provider = ? AND measured_at IS NOT NULL AND ("
            " (status = 'no' AND measured_at < ?)"
            " OR (status = 'error' AND measured_at < ?)"
            " OR (status = 'unmatched' AND stage = 'holdout' AND measured_at < ?))"
            " ORDER BY (stage = 'holdout') DESC, measured_at, address_id LIMIT ?",
            (provider, no_cutoff, retry_cutoff, retry_cutoff, quota),
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
    """A block pauses the provider, longer for each consecutive one (24 h, 48 h, ... up to 7 days).

    Runner addresses change from run to run, so a single refusal says little about us; nothing here
    disables a provider for good. `disabled` is a switch a person sets by hand in the state file.
    """
    state["blocks"].append({"at": now.strftime(TS), "status": status})
    state["streak"] = state.get("streak", 0) + 1
    hours = min(BLOCK_PAUSE_HOURS * 2 ** (state["streak"] - 1), BLOCK_PAUSE_MAX_HOURS)
    state["pause_until"] = (now + dt.timedelta(hours=hours)).strftime(TS)
    state["pause_s"] = BLOCK_PAUSE_S


@contextlib.contextmanager
def _stop_on_signals(request: dict) -> Iterator[None]:
    """SIGTERM/SIGINT ask for a clean stop: the row in flight finishes, nothing new starts."""
    previous: dict[int, object] = {}

    def handler(signum: int, frame: object) -> None:
        request.setdefault("reason", signal.Signals(signum).name.lower())

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, handler)
    except ValueError:  # not the main thread: no handlers, the budget still bounds the run
        previous = {}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def commit_message(provider: str, outcome: str, counts: Counter, pending: int, phases: list[str]) -> str:
    """A title with the counts, then the same numbers as a JSON block (the trinkhallen commit style)."""
    total = sum(counts.values())
    parts = ", ".join(f"{k} {counts[k]}" for k in ("yes", "no", "unmatched", "error") if counts[k])
    title = f"data: {provider} {outcome} +{total}" + (f" ({parts})" if parts else "") + f", {pending} pending"
    stats = {"provider": provider, "outcome": outcome, "measured": dict(counts), "pending": pending, "phases": phases}
    return f"{title}\n\n```json\n{json.dumps(stats, sort_keys=True)}\n```"


def advance(
    con: sqlite3.Connection,
    data_dir: Path,
    provider: str,
    measure: Measure,
    *,
    budget_s: float,
    checkpoint_s: float | None = None,
    flush_rows: int = FLUSH_EVERY_ROWS,
    districts: list[str] | None = None,
    push: bool = True,
    margin_s: float = DEADLINE_MARGIN_S,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], dt.datetime] = utcnow,
    log: Callable[[str], None] = print,
) -> int:
    """Run one slice. Exit code 0 normally, 3 when the provider blocked us.

    Rows are flushed to the data dir every `flush_rows`; the commit and push happen once, at the end
    of the slice (and once more every `checkpoint_s` seconds if that is set). Each push is a Workers
    Build, so an unchanged slice commits nothing.
    """
    state = read_state(data_dir, provider)
    if state["disabled"]:
        log(f"{provider}: disabled in the state file")
        return 0
    if state["pause_until"] and now().strftime(TS) < state["pause_until"]:
        log(f"{provider}: paused until {state['pause_until']}")
        return 0

    deadline = clock() + budget_s
    last_push = [clock()]
    rows_since_flush = [0]
    counts: Counter = Counter()
    phases: list[str] = []
    request: dict = {}

    def should_stop() -> bool:
        return "reason" in request or clock() + margin_s >= deadline

    def pending_count() -> int:
        return con.execute(
            "SELECT COUNT(*) FROM measurement WHERE provider = ? AND status = 'pending'", (provider,)
        ).fetchone()[0]

    def finish(outcome: str) -> None:
        store.save(con, data_dir, provider)
        if push:
            store.push(data_dir, commit_message(provider, outcome, counts, pending_count(), phases))

    def on_row() -> None:
        rows_since_flush[0] += 1
        if rows_since_flush[0] >= flush_rows:
            store.save(con, data_dir, provider)
            rows_since_flush[0] = 0
        if checkpoint_s is not None and clock() - last_push[0] >= checkpoint_s:
            finish("checkpoint")
            last_push[0] = clock()

    with _stop_on_signals(request):
        try:
            while not should_stop():
                counts.update(crawl_pending(con, provider, measure, should_stop=should_stop, on_row=on_row, now=now))
                if should_stop():
                    break
                phase, planned = plan_next(con, provider, districts, now())
                phases.append(f"{phase}+{planned}")
                log(f"{provider}: planned {phase} +{planned}")
                if phase == "idle":
                    break
        except Blocked as exc:
            _record_block(state, exc.status, now())
            write_state(data_dir, provider, state)
            finish(f"blocked {exc.status}")
            log(f"{provider}: BLOCKED status={exc.status}; paused, {sum(counts.values())} rows measured this run")
            return 3
        except Unavailable:
            finish("unavailable")
            log(f"{provider}: unavailable after retries; {sum(counts.values())} rows measured this run")
            return 0

    if sum(counts.values()) and state["streak"]:
        state["streak"] = 0
        write_state(data_dir, provider, state)
    stopped = request.get("reason")
    finish(f"stopped on {stopped}" if stopped else "slice")
    log(f"{provider}: {sum(counts.values())} rows measured this run, {pending_count()} pending"
        + (f", stopped on {stopped}" if stopped else ""))
    return 0
