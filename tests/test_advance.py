import datetime as dt
import json
import subprocess
from types import SimpleNamespace

from conftest import make_con, put

from fiberyet import advance, sampling, store
from fiberyet.fetch import Blocked, Unavailable

T0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.UTC)


class Clock:
    """Fake monotonic clock; every measurement 'costs' `step` seconds."""

    def __init__(self, step=1.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        return self.t

    def tick(self):
        self.t += self.step


def world(tmp_path, n=2500):
    con = make_con([(f"id{i:05d}", float(i % 50) * 30, float(i // 50) * 30, "D") for i in range(n)])
    store.save_frame(con, tmp_path, "v")
    return store.load(tmp_path, tmp_path / "w.sqlite")


def fiber_if_left(con):
    x = {r["street"] + r["house_number"]: r["x"] for r in con.execute("SELECT street, house_number, x FROM address")}
    return lambda street, number: SimpleNamespace(
        status="yes" if x[street + number] < 700 else "no", detail="d", raw="BODY"
    )


def run(con, tmp_path, measure, **kw):
    clock = kw.pop("clock", Clock())
    logs = []
    code = advance.advance(
        con, tmp_path, "p", measure,
        budget_s=kw.pop("budget_s", 10_000), checkpoint_s=kw.pop("checkpoint_s", 10_000),
        push=False, margin_s=0, clock=clock, now=lambda: T0, log=logs.append, **kw,
    )
    return code, logs


def counting(measure, clock):
    def wrapped(street, number):
        clock.tick()
        return measure(street, number)

    return wrapped


def test_slice_runs_holdout_then_coarse_then_refine_and_saves(tmp_path):
    con = world(tmp_path)
    clock = Clock()
    code, logs = run(con, tmp_path, counting(lambda s, n: SimpleNamespace(status="yes", detail="d", raw="B"), clock),
                     clock=clock, budget_s=5000)
    assert code == 0
    phases = [l.split("planned ")[1].split(" ")[0] for l in logs if "planned" in l]
    assert phases[:2] == ["holdout", "coarse"]
    assert (tmp_path / "measurements" / "p.jsonl").exists()
    stages = {r[0] for r in con.execute("SELECT DISTINCT stage FROM measurement")}
    assert {"holdout", "coarse"} <= stages


def test_budget_stops_midway_leaving_pending_saved(tmp_path):
    con = world(tmp_path)
    clock = Clock()
    code, logs = run(con, tmp_path, counting(lambda s, n: SimpleNamespace(status="yes", detail="d", raw="B"), clock),
                     clock=clock, budget_s=0.05 * 60 + 40)
    assert code == 0
    saved = (tmp_path / "measurements" / "p.jsonl").read_text().splitlines()
    statuses = [json.loads(l)["status"] for l in saved]
    assert "pending" in statuses and "yes" in statuses
    assert statuses.count("yes") <= 43


def test_resumes_from_saved_pending_rows(tmp_path):
    con = world(tmp_path)
    clock = Clock()
    m = counting(lambda s, n: SimpleNamespace(status="no", detail="d", raw="B"), clock)
    run(con, tmp_path, m, clock=clock, budget_s=100)
    first = sum(1 for l in (tmp_path / "measurements" / "p.jsonl").read_text().splitlines()
                if json.loads(l)["status"] == "no")
    con2 = store.load(tmp_path, tmp_path / "w2.sqlite")
    clock2 = Clock()
    run(con2, tmp_path, counting(lambda s, n: SimpleNamespace(status="no", detail="d", raw="B"), clock2),
        clock=clock2, budget_s=100)
    total = sum(1 for l in (tmp_path / "measurements" / "p.jsonl").read_text().splitlines()
                if json.loads(l)["status"] == "no")
    assert total > first


def blocker(status):
    def measure(street, number):
        raise Blocked(status)

    return measure


def test_block_stops_pauses_for_24h_and_next_run_makes_no_request(tmp_path):
    con = world(tmp_path)
    code, logs = run(con, tmp_path, blocker(403))
    assert code == 3
    state = json.loads((tmp_path / "state" / "p.json").read_text())
    assert state["blocks"] == [{"at": "2026-11-22T12:00:00Z", "status": 403}]
    assert state["pause_until"] == "2026-11-23T12:00:00Z" and state["pause_s"] == 10.0
    assert state["disabled"] is False

    calls = []
    code, logs = run(con, tmp_path, lambda s, n: calls.append(1))
    assert code == 0 and calls == [] and "paused until" in logs[0]


def test_second_block_disables_provider(tmp_path):
    con = world(tmp_path)
    run(con, tmp_path, blocker(429))
    later = T0 + dt.timedelta(hours=25)
    logs = []
    code = advance.advance(con, tmp_path, "p", blocker(429), budget_s=1000, checkpoint_s=1000, push=False,
                           margin_s=0, clock=Clock(), now=lambda: later, log=logs.append)
    assert code == 3
    state = json.loads((tmp_path / "state" / "p.json").read_text())
    assert state["disabled"] is True and len(state["blocks"]) == 2
    calls = []
    code, logs = run(con, tmp_path, lambda s, n: calls.append(1))
    assert code == 0 and calls == [] and "disabled" in logs[0]


def test_blocked_status_zero_for_non_json_is_recorded(tmp_path):
    con = world(tmp_path)
    assert run(con, tmp_path, blocker(0))[0] == 3
    assert json.loads((tmp_path / "state" / "p.json").read_text())["blocks"][0]["status"] == 0


def test_unavailable_exits_zero_without_recording_a_block(tmp_path):
    con = world(tmp_path)

    def down(street, number):
        raise Unavailable("x")

    code, logs = run(con, tmp_path, down)
    assert code == 0
    assert not (tmp_path / "state" / "p.json").exists()


def test_logs_never_contain_addresses_or_urls(tmp_path):
    con = world(tmp_path)
    clock = Clock()
    _, logs = run(con, tmp_path, counting(lambda s, n: SimpleNamespace(status="yes", detail="d", raw="B"), clock),
                  clock=clock, budget_s=300)
    text = "\n".join(logs)
    assert "://" not in text and "Gärtner" not in text and "id0" not in text


def test_refresh_requeues_old_rows_within_daily_quota(tmp_path):
    con = world(tmp_path, n=300)
    old = (T0 - dt.timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(300):
        put(con, f"id{i:05d}", "p", "coarse", "yes")
        con.execute("UPDATE measurement SET measured_at = ? WHERE address_id = ?", (old, f"id{i:05d}"))
    assert advance.requeue_stale(con, "p", T0) == 300
    assert con.execute("SELECT COUNT(*) FROM measurement WHERE status = 'pending'").fetchone()[0] == 300
    # a row measured today counts against the quota
    quota_left = advance.REFRESH_DAILY_QUOTA
    con.execute("UPDATE measurement SET status='yes', measured_at=?", (T0.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    assert advance.requeue_stale(con, "p", T0) == 0
    assert quota_left == 2000


def test_idle_when_everything_is_fresh_and_nothing_to_plan(tmp_path):
    con = world(tmp_path, n=1100)
    clock = Clock()
    m = counting(lambda s, n: SimpleNamespace(status="yes", detail="d", raw="B"), clock)
    code, logs = run(con, tmp_path, m, clock=clock, budget_s=10**7)
    assert code == 0
    assert any("planned idle" in l for l in logs)


def test_push_commits_and_pushes_to_a_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "data", str(remote)], check=True, capture_output=True)
    work = tmp_path / "data"
    work.mkdir()
    subprocess.run(["git", "-C", str(work), "init", "-b", "data"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True)
    (work / "a.txt").write_text("1")
    assert store.push(work, "first") is True
    assert store.push(work, "nothing") is False
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%an|%s", "data"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert log == "github-actions[bot]|first"


def test_push_rebases_over_a_concurrent_push(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "data", str(remote)], check=True, capture_output=True)
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        d.mkdir()
        subprocess.run(["git", "-C", str(d), "init", "-b", "data"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", str(remote)], check=True)
    (a / "tel.txt").write_text("1")
    store.push(a, "a1")
    subprocess.run(["git", "-C", str(b), "pull", "origin", "data"], check=True, capture_output=True)
    (a / "tel.txt").write_text("2")
    store.push(a, "a2")
    (b / "eins.txt").write_text("x")  # b is now behind remote: its first push is rejected
    assert store.push(b, "b1") is True
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "data"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert sorted(files) == ["eins.txt", "tel.txt"]


def test_holdout_rows_are_planned_once(tmp_path):
    con = world(tmp_path)
    assert sampling.holdout_tasks(con, "p") == 1000
    assert sampling.holdout_tasks(con, "p") == 0
