import sqlite3

import pytest

from fiberyet import db


def make_con(addresses):
    """In-memory database; addresses are (id, x, y, district)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.executemany(
        "INSERT INTO address (id, street, house_number, plz, district, x, y, lon, lat, cell)"
        " VALUES (?, 's', '1', '60311', ?, ?, ?, 8.0, 50.0, ?)",
        [(i, d, x, y, f"{int(x // 100)}_{int(y // 100)}") for i, x, y, d in addresses],
    )
    return con


@pytest.fixture
def grid():
    """40 x 20 addresses on a 20 m raster: x in [0, 800), y in [0, 400). Left half (x < 400) has fiber."""
    addrs = [(f"a{ix:02d}_{iy:02d}", ix * 20.0, iy * 20.0, "D") for ix in range(40) for iy in range(20)]
    return make_con(addrs)


def put(con, addr_id, provider, stage, status):
    con.execute(
        "INSERT OR REPLACE INTO measurement (address_id, provider, stage, status) VALUES (?,?,?,?)",
        (addr_id, provider, stage, status),
    )
