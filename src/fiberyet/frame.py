"""Build the address frame: every Frankfurt house address with district and postcode.

Sources are cadastre and city geometry only; neither says anything about fiber.
"""

from __future__ import annotations

import math
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from io import BytesIO

import httpx
import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

HAKO_URL = "https://www.gds-srv.hessen.de/cgi-bin/lika-services/ogc-free-data.ows"
POLY_URL = "https://geowebdienste.frankfurt.de/WFS_Stadtgebietsgliederung"
# The service silently truncates at this many features per request.
HAKO_CAP = 10_000
TILE = 2_000
TILE_PAUSE_S = 1.0
FIELDS = {"HA_ID", "HA_LAND", "HA_BEZIRK", "HA_KREIS", "HA_STR", "HA_HAUSNR", "HA_HAUSNRZU"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_hako(content: bytes) -> list[dict]:
    """Every `hako` feature in a WFS response as a flat dict (strings, plus float x/y)."""
    out: list[dict] = []
    for _, el in ET.iterparse(BytesIO(content), events=("end",)):
        if _local(el.tag) != "hako":
            continue
        rec: dict = {}
        for child in el.iter():
            name = _local(child.tag)
            if name in FIELDS:
                rec[name] = (child.text or "").strip()
            elif name == "pos" and child.text:
                # GML order for EPSG:25832 is easting northing.
                e, n = child.text.split()
                rec["x"], rec["y"] = float(e), float(n)
        out.append(rec)
        el.clear()
    return out


def _fetch_bbox(client: httpx.Client, x0: float, y0: float, x1: float, y1: float) -> list[dict]:
    r = client.get(
        HAKO_URL,
        params={
            "SERVICE": "WFS",
            "VERSION": "1.1.0",
            "REQUEST": "GetFeature",
            "TYPENAME": "hako",
            "SRSNAME": "urn:ogc:def:crs:EPSG::25832",
            "BBOX": f"{x0},{y0},{x1},{y1},urn:ogc:def:crs:EPSG::25832",
        },
    )
    r.raise_for_status()
    return parse_hako(r.content)


def _fetch_tile(client: httpx.Client, x0: int, y0: int, size: int) -> list[dict]:
    recs = _fetch_bbox(client, x0, y0, x0 + size, y0 + size)
    if len(recs) < HAKO_CAP:
        return recs
    if size <= TILE // 4:
        raise SystemExit(f"tile {x0},{y0} size {size} still hits the {HAKO_CAP} cap")
    half = size // 2
    out: list[dict] = []
    for dx in (0, half):
        for dy in (0, half):
            time.sleep(TILE_PAUSE_S)
            out += _fetch_tile(client, x0 + dx, y0 + dy, half)
    return out


def _layer(client: httpx.Client, name: str, expected: int) -> list[dict]:
    r = client.get(
        POLY_URL,
        params={
            "SERVICE": "WFS",
            "VERSION": "2.0.0",
            "REQUEST": "GetFeature",
            "TYPENAMES": f"Stadtgebietsgliederung:{name}",
            "OUTPUTFORMAT": "GEOJSON",
        },
    )
    r.raise_for_status()
    feats = r.json()["features"]
    if len(feats) != expected:
        raise SystemExit(f"{name}: expected {expected} features, got {len(feats)}")
    return feats


def _assign(points: shapely.Geometry, feats: list[dict], key: str) -> list[str | None]:
    geoms = [shape(f["geometry"]) for f in feats]
    tree = STRtree(geoms)
    pt_idx, geom_idx = tree.query(points, predicate="within")
    out: list[str | None] = [None] * len(points)
    for p, g in zip(pt_idx, geom_idx):
        out[p] = str(feats[g]["properties"][key])
    return out


def fetch_districts() -> list[dict]:
    """The 46 Stadtteil features (EPSG:4326), for the published map's district layer."""
    with httpx.Client(timeout=120, headers={"User-Agent": "ftth-availability-research/0.1"}) as client:
        return _layer(client, "Stadtteile", 46)


def build(con: sqlite3.Connection) -> int:
    if con.execute("SELECT 1 FROM measurement LIMIT 1").fetchone():
        raise SystemExit("measurements exist; refusing to rebuild the address frame")

    with httpx.Client(timeout=120, headers={"User-Agent": "ftth-availability-research/0.1"}) as client:
        districts = _layer(client, "Stadtteile", 46)
        plz_areas = _layer(client, "Postleitzahlbereiche", 44)

        to_utm = Transformer.from_crs(4326, 25832, always_xy=True)
        city = unary_union([transform(to_utm.transform, shape(f["geometry"])) for f in districts])
        city_prep = prep(city)
        minx, miny, maxx, maxy = city.bounds
        x_start = int(math.floor(minx / TILE) * TILE)
        y_start = int(math.floor(miny / TILE) * TILE)

        found: dict[str, dict] = {}
        scanned = 0
        for x0 in range(x_start, int(maxx) + TILE, TILE):
            for y0 in range(y_start, int(maxy) + TILE, TILE):
                if not city_prep.intersects(box(x0, y0, x0 + TILE, y0 + TILE)):
                    continue
                scanned += 1
                for rec in _fetch_tile(client, x0, y0, TILE):
                    if (rec.get("HA_LAND"), rec.get("HA_BEZIRK"), rec.get("HA_KREIS")) == ("06", "4", "12"):
                        found[rec["HA_ID"]] = rec
                print(f"\rtiles {scanned}, addresses {len(found)}", end="", file=sys.stderr)
                time.sleep(TILE_PAUSE_S)
        print(file=sys.stderr)

    recs = list(found.values())
    xs = np.array([r["x"] for r in recs])
    ys = np.array([r["y"] for r in recs])
    lon, lat = Transformer.from_crs(25832, 4326, always_xy=True).transform(xs, ys)
    points = shapely.points(lon, lat)
    district = _assign(points, districts, "STT_NAME")
    plz = _assign(points, plz_areas, "PLZ_ID")

    rows = [
        (
            r["HA_ID"],
            r["HA_STR"],
            r["HA_HAUSNR"] + r.get("HA_HAUSNRZU", ""),
            plz[i],
            district[i],
            r["x"],
            r["y"],
            float(lon[i]),
            float(lat[i]),
            f"{math.floor(r['x'] / 100)}_{math.floor(r['y'] / 100)}",
        )
        for i, r in enumerate(recs)
    ]
    with con:
        con.execute("DELETE FROM prediction")
        con.execute("DELETE FROM address")
        con.executemany(
            "INSERT INTO address (id, street, house_number, plz, district, x, y, lon, lat, cell)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)
