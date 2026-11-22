"""Telekom's public fiber-coverage polygons as a sampling hint.

The polygons are not ground truth: on a 1000-address hold-out the plain layer found 95.3% of fiber
addresses but also covered 55 addresses where the lookup said no. This module uses the polygons to
rank addresses by how uncertain they are, and to publish a separate map view. It never changes a
measured status.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import shapely
from pyproj import Transformer
from shapely.geometry import shape
from shapely.strtree import STRtree

_TO_3857 = Transformer.from_crs(4326, 3857, always_xy=True)


def load_polygons(data_dir: Path) -> list[shapely.Geometry]:
    path = data_dir / "frame" / "polygons" / "fiber.geojson"
    if not path.exists():
        return []
    fc = json.loads(path.read_text())
    return [shape(f["geometry"]) for f in fc["features"] if f.get("geometry")]


def hinted_addresses(con: sqlite3.Connection, polygons: list[shapely.Geometry]) -> dict[str, bool]:
    """{address id: polygon says fiber} for every address, computed from the saved polygons."""
    if not polygons:
        return {}
    tree = STRtree(polygons)
    out = {}
    for r in con.execute("SELECT id, lon, lat FROM address"):
        x, y = _TO_3857.transform(r["lon"], r["lat"])
        p = shapely.Point(x, y)
        out[r["id"]] = any(polygons[i].contains(p) for i in tree.query(p))
    return out


def polygon_hints_for(con: sqlite3.Connection, todo: dict[str, list[str]], data_dir: Path) -> dict[str, bool]:
    """Hints only for addresses in the given cells."""
    ids = {i for members in todo.values() for i in members}
    polys = load_polygons(data_dir)
    if not polys:
        return {}
    tree = STRtree(polys)
    out = {}
    for r in con.execute("SELECT id, lon, lat FROM address"):
        if r["id"] not in ids:
            continue
        x, y = _TO_3857.transform(r["lon"], r["lat"])
        p = shapely.Point(x, y)
        out[r["id"]] = any(polys[i].contains(p) for i in tree.query(p))
    return out


def polygon_hints(con: sqlite3.Connection, data_dir: Path) -> dict[str, bool]:
    return hinted_addresses(con, load_polygons(data_dir))
