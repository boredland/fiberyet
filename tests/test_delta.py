"""The delta-crawl behaviour borrowed from trinkhallen-data: flush often, commit once, stop on signals, TTL retries."""

import datetime as dt
import json
import os
import signal
import subprocess
from collections import Counter
from types import SimpleNamespace

from conftest import make_con, put

from fiberyet import advance, store

T0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC)
ANSWER = SimpleNamespace(status="yes", detail="d", raw="B")


def world(tmp_path, n=2500):
    con = make_con([(f"id{i:05d}", float(i % 50) * 30, float(i // 50) * 30, "D") for i in range(n)])
    store.save_frame(con, tmp_path, "v")
    return store.load(tmp_path, tmp_path / "w.sqlite")


class StepClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def tick(self, s=1.0):
        self.t += s


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


def test_commit_message_has_counts_in_title_and_a_json_block():
    msg = advance.commit_message("telekom", "slice", Counter(yes=3, no=2, unmatched=1), 40, ["holdout+1000"])
    title, _, body = msg.partition("\n\n")
    assert title == "data: telekom slice +6 (yes 3, no 2, unmatched 1), 40 pending"
    stats = json.loads(body.removeprefix("```json\n").removesuffix("\n```"))
    assert stats["measured"] == {"yes": 3, "no": 2, "unmatched": 1} and stats["pending"] == 40
    assert stats["phases"] == ["holdout+1000"]


def test_progress_is_flushed_to_disk_every_n_rows_without_pushing(tmp_path):
    con = world(tmp_path)
    clock = StepClock()
    sizes = []
    real_save = store.save

    def spying_save(c, d, p):
        real_save(c, d, p)
        sizes.append(sum(1 for line in open(d / "measurements" / f"{p}.jsonl") if '"status":"yes"' in line))

    store.save = spying_save
    try:
        def measure(street, number):
            clock.tick()
            return ANSWER

        advance.advance(con, tmp_path, "p", measure, budget_s=260, flush_rows=50, push=False,
                        margin_s=0, clock=clock, now=lambda: T0, log=lambda m: None)
    finally:
        store.save = real_save
    flushes = sizes[:-1]  # the last save is the end-of-run one
    assert flushes[:3] == [50, 100, 150]


def test_one_commit_per_slice_with_stats_in_the_message(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    git(repo, "checkout", "-q", "-b", "main")
    data = repo / "data"
    seed = make_con([(f"id{i:05d}", float(i % 50) * 30, float(i // 50) * 30, "D") for i in range(2500)])
    store.save_frame(seed, data, "v")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "push", "-q", "origin", "main")

    con = store.load(data, tmp_path / "w.sqlite")
    clock = StepClock()

    def measure(street, number):
        clock.tick()
        return ANSWER

    advance.advance(con, data, "p", measure, budget_s=300, flush_rows=50, push=True,
                    margin_s=0, clock=clock, now=lambda: T0, log=lambda m: None)

    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"],
                         check=True, capture_output=True, text=True).stdout.splitlines()
    assert len(log) == 2  # seed + exactly one data commit, however many flushes happened
    assert log[0].startswith("data: p slice +") and "pending" in log[0]


def test_nothing_measured_pushes_nothing(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    git(repo, "checkout", "-q", "-b", "main")
    data = repo / "data"
    store.save_frame(make_con([("a", 0, 0, "D")]), data, "v")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seed")
    git(repo, "push", "-q", "origin", "main")
    # a paused provider does nothing, so there is nothing to commit
    advance.write_state(data, "p", {"blocks": [], "streak": 1, "pause_until": "2099-01-01T00:00:00Z",
                                    "pause_s": 10.0, "disabled": False})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "state")
    git(repo, "push", "-q", "origin", "main")
    con = store.load(data, tmp_path / "w.sqlite")
    advance.advance(con, data, "p", lambda s, n: ANSWER, budget_s=100, push=True, margin_s=0,
                    clock=StepClock(), now=lambda: T0, log=lambda m: None)
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"],
                         check=True, capture_output=True, text=True).stdout.splitlines()
    assert log == ["state", "seed"]


def test_sigterm_finishes_the_row_in_flight_then_stops_and_saves(tmp_path):
    con = world(tmp_path)
    clock = StepClock()
    seen = []

    def measure(street, number):
        clock.tick()
        seen.append(1)
        if len(seen) == 7:
            os.kill(os.getpid(), signal.SIGTERM)  # arrives while this row is in flight
        return ANSWER

    logs = []
    code = advance.advance(con, tmp_path, "p", measure, budget_s=10_000, push=False, margin_s=0,
                           clock=clock, now=lambda: T0, log=logs.append)
    assert code == 0 and len(seen) == 7
    rows = [json.loads(line) for line in (tmp_path / "measurements" / "p.jsonl").read_text().splitlines()]
    assert sum(1 for r in rows if r["status"] == "yes") == 7  # the 7th was finished and kept
    assert "stopped on sigterm" in logs[-1]
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL  # the handler was restored


def test_yes_is_permanent_no_rechecks_at_90_days_unmatched_and_error_at_30(tmp_path):
    con = make_con([(f"a{i}", float(i), 0.0, "D") for i in range(8)])
    for name, status, days in [("a0", "unmatched", 29), ("a1", "unmatched", 31), ("a2", "error", 31),
                               ("a3", "yes", 31), ("a4", "yes", 400), ("a5", "no", 89), ("a6", "no", 91),
                               ("a7", "yes", 3650)]:
        put(con, name, "p", "coarse", status)
        stamp = (T0 - dt.timedelta(days=days)).strftime(advance.TS)
        con.execute("UPDATE measurement SET measured_at = ? WHERE address_id = ?", (stamp, name))
    con.commit()
    assert advance.requeue_stale(con, "p", T0) == 3
    pending = {r[0] for r in con.execute("SELECT address_id FROM measurement WHERE status = 'pending'")}
    assert pending == {"a1", "a2", "a6"}
    still_yes = {r[0] for r in con.execute("SELECT address_id FROM measurement WHERE status = 'yes'")}
    assert still_yes == {"a3", "a4", "a7"}  # however old, a yes is kept and not re-fetched
