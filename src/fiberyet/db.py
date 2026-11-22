from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("work/fiberyet.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS address (
  id TEXT PRIMARY KEY,
  street TEXT NOT NULL,
  house_number TEXT NOT NULL,
  plz TEXT,
  district TEXT,
  x REAL NOT NULL, y REAL NOT NULL,
  lon REAL NOT NULL, lat REAL NOT NULL,
  cell TEXT NOT NULL,
  holdout INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS measurement (
  address_id TEXT NOT NULL REFERENCES address(id),
  provider TEXT NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT,
  raw TEXT,
  measured_at TEXT,
  PRIMARY KEY (address_id, provider)
);
CREATE TABLE IF NOT EXISTS prediction (
  address_id TEXT NOT NULL REFERENCES address(id),
  provider TEXT NOT NULL,
  p_yes REAL,
  n_neighbours INTEGER NOT NULL,
  PRIMARY KEY (address_id, provider)
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con
