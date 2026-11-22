"""A small, fake data dir for building and checking the site without any network or real addresses.

Usage: python scripts/synthetic_data.py <out-dir>
A grid of fake addresses with 46 district labels, half of them measured with a left/right fiber split.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from fiberyet import db, sampling, store


def main(out: Path) -> None:
    out = Path(out)
    work = out / "work.sqlite"
    if work.exists():
        work.unlink()
    con = db.connect(work)
    names = [f"Stadtteil {i:02d}" for i in range(46)]
    rows = []
    for ix in range(120):
        for iy in range(60):
            x, y = 476000.0 + ix * 25, 5548000.0 + iy * 25
            lon, lat = 8.60 + ix * 0.00035, 50.08 + iy * 0.00022
            rows.append((f"syn{ix:03d}_{iy:03d}", "Teststraße", str(ix * 100 + iy), "60311", names[(ix // 3 + iy // 5) % 46], x, y, lon, lat, f"{math.floor(x / 100)}_{math.floor(y / 100)}"))
    with con:
        con.executemany(
            "INSERT INTO address (id, street, house_number, plz, district, x, y, lon, lat, cell) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    store.save_frame(con, out, "synthetic")
    sampling.mark_holdout(con)
    for r in con.execute("SELECT id, x FROM address ORDER BY id").fetchall()[::3]:
        con.execute(
            "INSERT OR IGNORE INTO measurement (address_id, provider, stage, status, detail, measured_at)"
            " VALUES (?, 'telekom', 'coarse', ?, 'synthetic', '2026-11-22T12:00:00Z')",
            (r["id"], "yes" if r["x"] < 477500 else "no"),
        )
    con.commit()
    store.save(con, out, "telekom")
    squares = [
        [[8.60 + i * 0.01, 50.08], [8.61 + i * 0.01, 50.08], [8.61 + i * 0.01, 50.09], [8.60 + i * 0.01, 50.09], [8.60 + i * 0.01, 50.08]]
        for i in range(46)
    ]
    feats = [
        {"type": "Feature", "properties": {"STT_NAME": n}, "geometry": {"type": "Polygon", "coordinates": [sq]}}
        for n, sq in zip(names, squares)
    ]
    (out / "frame" / "districts.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    con.close()
    work.unlink()
    print(f"synthetic data in {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
