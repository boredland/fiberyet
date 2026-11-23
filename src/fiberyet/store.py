"""Persistence on the `data` branch: sorted text files, so git deltas stay small.

The SQLite file in work/ is a scratch cache rebuilt from these files each run.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import sqlite3
import subprocess
from pathlib import Path

from . import db, sampling

FRAME_COLUMNS = ("id", "street", "house_number", "plz", "district", "x", "y", "lon", "lat")
BOT = ("github-actions[bot]", "github-actions[bot]@users.noreply.github.com")
PUSH_ATTEMPTS = 5


def save_frame(con: sqlite3.Connection, data_dir: Path, version: str) -> None:
    (data_dir / "frame").mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FRAME_COLUMNS)
    for r in con.execute(f"SELECT {','.join(FRAME_COLUMNS)} FROM address ORDER BY id"):
        w.writerow(["" if v is None else v for v in tuple(r)])
    with open(data_dir / "frame" / "addresses.csv.gz", "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            gz.write(buf.getvalue().encode("utf-8"))
    (data_dir / "frame" / "VERSION").write_text(version + "\n")


def load(data_dir: Path, db_path: Path = db.DB_PATH) -> sqlite3.Connection:
    """Fresh work database from the data branch files. The hold-out is re-derived, never stored."""
    if db_path.exists():
        db_path.unlink()
    con = db.connect(db_path)
    frame = data_dir / "frame" / "addresses.csv.gz"
    if not frame.exists():
        raise SystemExit("no frame in data dir; run `fiberyet frame --data-dir ...` first")
    with gzip.open(frame, "rt", encoding="utf-8", newline="") as fh:
        rows = []
        for r in csv.DictReader(fh):
            x, y = float(r["x"]), float(r["y"])
            rows.append(
                (
                    r["id"],
                    r["street"],
                    r["house_number"],
                    r["plz"] or None,
                    r["district"] or None,
                    x,
                    y,
                    float(r["lon"]),
                    float(r["lat"]),
                    f"{math.floor(x / 100)}_{math.floor(y / 100)}",
                )
            )
    with con:
        con.executemany(
            "INSERT INTO address (id, street, house_number, plz, district, x, y, lon, lat, cell)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    sampling.mark_holdout(con)
    for path in sorted((data_dir / "measurements").glob("*.jsonl")):
        provider = path.stem
        with open(path, encoding="utf-8") as fh:
            items = [json.loads(line) for line in fh if line.strip()]
        with con:
            con.executemany(
                "INSERT INTO measurement (address_id, provider, stage, status, detail, raw, measured_at)"
                " VALUES (?,?,?,?,?,?,?)",
                [
                    (i["id"], provider, i["stage"], i["status"], i.get("detail"), i.get("raw"), i.get("t"))
                    for i in items
                ],
            )
    return con


def save(con: sqlite3.Connection, data_dir: Path, provider: str) -> None:
    out = data_dir / "measurements"
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for r in con.execute("SELECT * FROM measurement WHERE provider = ? ORDER BY address_id", (provider,)):
        item = {"id": r["address_id"], "stage": r["stage"], "status": r["status"]}
        if r["detail"] is not None:
            item["detail"] = r["detail"]
        if r["measured_at"] is not None:
            item["t"] = r["measured_at"]
        if r["status"] in ("error", "unmatched") and r["raw"] is not None:
            item["raw"] = r["raw"]
        lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    tmp = out / f".{provider}.jsonl.tmp"
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, out / f"{provider}.jsonl")


def _git(data_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git from inside data_dir, which lives inside the repository's working tree."""
    return subprocess.run(
        ["git", "-C", str(data_dir), "-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _merge_jsonl(local_path: Path, remote_text: str) -> None:
    """Merge remote JSONL into local: union of rows, matched by (id, provider)."""
    local_rows: dict[tuple, dict] = {}
    for line in local_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        local_rows[(obj.get("id"), obj.get("provider", ""))] = obj
    remote_rows: dict[tuple, dict] = {}
    for line in remote_text.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        remote_rows[(obj.get("id"), obj.get("provider", ""))] = obj
    # Union: for rows in both, keep the one with the later measured_at.
    merged = dict(remote_rows)
    for key, local_obj in local_rows.items():
        if key not in merged:
            merged[key] = local_obj
        else:
            remote_t = merged[key].get("t", "")
            local_t = local_obj.get("t", "")
            if local_t >= remote_t:
                merged[key] = local_obj
    lines = [json.dumps(v, ensure_ascii=False, separators=(",", ":")) for v in sorted(merged.values(), key=lambda x: x.get("id", ""))]
    local_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def push(data_dir: Path, message: str) -> bool:
    """Commit only data_dir and push the current branch, rebasing on conflict.

    On rebase conflict, merges JSONL files by taking the union of rows (remote + local),
    so concurrent crawls for different providers or different address sets don't lose work.
    """
    _git(data_dir, "add", "-A", "--", ".")
    if _git(data_dir, "diff", "--cached", "--quiet", "--", ".", check=False).returncode == 0:
        return False
    _git(data_dir, "commit", "-m", message, "--", ".")
    branch = _git(data_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for _ in range(PUSH_ATTEMPTS):
        if _git(data_dir, "push", "origin", f"HEAD:{branch}", check=False).returncode == 0:
            return True
        # Pull failed or push failed: try rebase.
        pull = _git(data_dir, "pull", "--rebase", "--autostash", "origin", branch, check=False)
        if pull.returncode != 0:
            # Rebase conflict: resolve by merging JSONL files.
            _git(data_dir, "rebase", "--abort", check=False)
            # Fetch remote and merge manually.
            _git(data_dir, "fetch", "origin", branch, check=False)
            # For each JSONL file, merge remote into local.
            meas_dir = data_dir / "measurements"
            if meas_dir.exists():
                for local_path in meas_dir.glob("*.jsonl"):
                    rel = local_path.relative_to(data_dir)
                    remote = _git(data_dir, "show", f"origin/{branch}:{rel.as_posix()}", check=False)
                    if remote.returncode == 0:
                        _merge_jsonl(local_path, remote.stdout)
            _git(data_dir, "add", "-A", "--", ".")
            _git(data_dir, "commit", "-m", message, "--", ".", check=False)
    raise RuntimeError("push still failing after retries")
