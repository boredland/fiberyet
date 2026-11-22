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
    return subprocess.run(
        ["git", "-C", str(data_dir), "-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def push(data_dir: Path, message: str, branch: str = "data") -> bool:
    """Commit everything and push with a rebase retry loop. False when there was nothing to commit."""
    _git(data_dir, "add", "-A")
    if not _git(data_dir, "status", "--porcelain").stdout.strip():
        return False
    _git(data_dir, "commit", "-m", message)
    for _ in range(PUSH_ATTEMPTS):
        if _git(data_dir, "push", "origin", f"HEAD:{branch}", check=False).returncode == 0:
            return True
        if _git(data_dir, "pull", "--rebase", "origin", branch, check=False).returncode != 0:
            _git(data_dir, "rebase", "--abort", check=False)
    raise RuntimeError("push still failing after retries")
