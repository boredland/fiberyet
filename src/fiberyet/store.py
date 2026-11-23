"""Persistence on the `data` branch: sorted text files, so git deltas stay small.

The SQLite file in work/ is a scratch cache rebuilt from these files each run.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from . import db

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
    from . import sampling

    if db_path.exists():
        db_path.unlink()
    con = db.connect(db_path)
    frame_dir = data_dir / "frame"
    if not (frame_dir / "addresses.csv.gz").exists():
        raise SystemExit("no frame in data dir; run `fiberyet frame --data-dir ...` first")
    with gzip.open(frame_dir / "addresses.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        rows = []
        for r in csv.DictReader(fh):
            x, y = float(r["x"]), float(r["y"])
            rows.append(
                (
                    r["id"], r["street"], r["house_number"], r["plz"] or None,
                    r["district"] or None, x, y, float(r["lon"]), float(r["lat"]),
                    f"{int(x // 100)}_{int(y // 100)}",
                )
            )
    with con:
        con.executemany(
            "INSERT INTO address (id, street, house_number, plz, district, x, y, lon, lat, cell)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    sampling.mark_holdout(con)
    meas_dir = data_dir / "measurements"
    if meas_dir.exists():
        for path in sorted(meas_dir.glob("*.jsonl")):
            provider = path.stem
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                con.execute(
                    "INSERT OR REPLACE INTO measurement (address_id, provider, stage, status, detail, raw, measured_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        item["id"], provider, item["stage"], item["status"],
                        item.get("detail"), item.get("raw"), item.get("t"),
                    ),
                )
    con.commit()
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
        if r["raw"] is not None:
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


def _merge_jsonl(local_path: Path, local_text: str, remote_text: str) -> None:
    """Merge remote JSONL into the file at local_path: union of rows, matched by (id, provider).

    For rows present in both, keeps the one with the later measured_at (t).
    Falls back to line-based union for non-JSON content.
    """
    local_lines = [ln for ln in local_text.splitlines() if ln.strip()]
    remote_lines = [ln for ln in remote_text.splitlines() if ln.strip()]
    try:
        local_rows: dict[tuple, dict] = {}
        for line in local_lines:
            obj = json.loads(line)
            local_rows[(obj.get("id"), obj.get("provider", ""))] = obj
        remote_rows: dict[tuple, dict] = {}
        for line in remote_lines:
            obj = json.loads(line)
            remote_rows[(obj.get("id"), obj.get("provider", ""))] = obj
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
        return
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    seen: set[str] = set()
    out: list[str] = []
    for line in remote_lines + local_lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    local_path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")


def push(data_dir: Path, message: str) -> bool:
    """Commit only data_dir and push the current branch.

    On push failure (e.g. concurrent crawl), fetches remote, resets to it, merges
    local file changes on top, re-commits and pushes. Stashes uncommitted changes
    outside data_dir (e.g. README edits) so they survive the reset.
    """
    _git(data_dir, "add", "-A", "--", ".")
    if _git(data_dir, "diff", "--cached", "--quiet", "--", ".", check=False).returncode == 0:
        return False
    _git(data_dir, "commit", "-m", message, "--", ".")
    branch = _git(data_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for _ in range(PUSH_ATTEMPTS):
        if _git(data_dir, "push", "origin", f"HEAD:{branch}", check=False).returncode == 0:
            return True
        # Push failed: stash, fetch remote, reset, merge, re-commit, push, unstash.
        _git(data_dir, "stash", "push", "--include-untracked", "-m", "push-stash", check=False)
        _git(data_dir, "fetch", "origin", branch, check=False)
        # Save ALL local files before reset.
        local_files: dict[str, str] = {}
        for fpath in data_dir.rglob("*"):
            if fpath.is_file():
                rel = fpath.relative_to(data_dir).as_posix()
                local_files[rel] = fpath.read_text(encoding="utf-8")
        # Reset to remote (discards local commit, but we saved file contents).
        _git(data_dir, "reset", "--hard", f"origin/{branch}", check=False)
        # Merge: for JSONL files, union rows; for other files, restore if missing.
        for rel, local_text in local_files.items():
            local_path = data_dir / rel
            local_path.parent.mkdir(parents=True, exist_ok=True)
            if rel.endswith(".jsonl"):
                remote_text = ""
                if local_path.exists():
                    remote_text = local_path.read_text(encoding="utf-8")
                _merge_jsonl(local_path, local_text, remote_text)
            elif not local_path.exists():
                local_path.write_text(local_text, encoding="utf-8")
        _git(data_dir, "add", "-A", "--", ".")
        if _git(data_dir, "diff", "--cached", "--quiet", "--", ".", check=False).returncode != 0:
            _git(data_dir, "commit", "-m", message, "--", ".", check=False)
        # Restore stashed changes.
        _git(data_dir, "stash", "pop", check=False)
    raise RuntimeError("push still failing after retries")
