"""Hand-entry sheet for testing how well 1&1 covers OXG's network.

OXG's own checker needs a Friendly Captcha solution per lookup, so it is never crawled. A person
types the sampled addresses into https://mitmachen.oxg.de/address-search instead and fills in the
`oxg` column. The three groups answer different questions:

- build_area: addresses in OXG's announced Frankfurt build districts, whatever 1&1 said. Where OXG
  says yes and 1&1 says no, 1&1 does not cover OXG.
- elsewhere:  1&1 no and Telekom no outside those districts. Finds OXG fiber outside the announced
  area that 1&1 misses.
- precision:  1&1 yes and Telekom no. Tests whether non-Telekom fiber seen through 1&1 is OXG.
"""

from __future__ import annotations

import csv
import random
import sqlite3
from pathlib import Path

OXG_DISTRICTS = ("Höchst", "Unterliederbach", "Sossenheim")
SEED = 20261122
COLUMNS = ("group", "id", "street", "house_number", "plz", "district", "einsundeins", "telekom", "oxg")

_BASE = (
    "SELECT a.id, a.street, a.house_number, a.plz, a.district, e.status AS einsundeins, t.status AS telekom"
    " FROM address a"
    " JOIN measurement e ON e.address_id = a.id AND e.provider = 'einsundeins' AND e.status IN ('yes', 'no')"
    " LEFT JOIN measurement t ON t.address_id = a.id AND t.provider = 'telekom' AND t.status IN ('yes', 'no')"
)
_IN_OXG = "a.district IN (%s)" % ",".join("?" * len(OXG_DISTRICTS))
_GROUPS = (
    ("build_area", _IN_OXG, OXG_DISTRICTS),
    ("elsewhere", f"NOT COALESCE({_IN_OXG}, 0) AND e.status = 'no' AND t.status = 'no'", OXG_DISTRICTS),
    ("precision", "e.status = 'yes' AND t.status = 'no'", ()),
)


def sample(con: sqlite3.Connection, n: int = 50) -> list[dict]:
    """Up to n addresses per group, no address twice, the same draw for the same data."""
    rng = random.Random(SEED)
    taken: set[str] = set()
    rows: list[dict] = []
    for group, where, params in _GROUPS:
        pool = [
            dict(r)
            for r in con.execute(f"{_BASE} WHERE {where} ORDER BY a.id", params)
            if r["id"] not in taken
        ]
        for r in rng.sample(pool, min(n, len(pool))):
            taken.add(r["id"])
            rows.append({"group": group, **r, "oxg": ""})
    rng.shuffle(rows)
    return rows


def write(con: sqlite3.Connection, path: Path, n: int = 50) -> dict[str, int]:
    rows = sample(con, n)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["group"]] = counts.get(r["group"], 0) + 1
    return counts
