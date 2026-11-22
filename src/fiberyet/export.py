"""The only code allowed to produce the published files. Cell and district shares, never addresses.

Everything here is derived from per-address states, then collapsed: shares are
rounded to tens, and cells with fewer than MIN_CELL_ADDRESSES addresses carry no
shares at all.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import mapping, shape

from . import infer, validate

MIN_CELL_ADDRESSES = 5
MAX_FILE_BYTES = 20 * 1024 * 1024
PROVIDER_KEYS = {"telekom": "t", "einsundeins": "e", "tng": "g"}
TNG_DISTRICTS = frozenset(
    {"Berkersheim", "Eckenheim", "Eschersheim", "Ginnheim", "Heddernheim", "Preungesheim", "Seckbach", "Frankfurter Berg"}
)


def tens(counts: list[int]) -> list[int]:
    """Shares as multiples of 10 summing to 100 (largest-remainder), or [0,0,100] when there is nothing."""
    total = sum(counts)
    if total == 0:
        return [0, 0, 100]
    raw = [c * 10 / total for c in counts]
    floors = [math.floor(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))[: 10 - sum(floors)]:
        floors[i] += 1
    return [f * 10 for f in floors]


def address_states(con: sqlite3.Connection, provider: str) -> dict[str, str]:
    """my | mn | iy | in | u for every address that has any information; others are absent (= x)."""
    out: dict[str, str] = {}
    for r in con.execute(
        "SELECT address_id, status FROM measurement WHERE provider = ? AND status IN ('yes','no')", (provider,)
    ):
        out[r["address_id"]] = "my" if r["status"] == "yes" else "mn"
    for r in con.execute("SELECT address_id, p_yes, n_neighbours FROM prediction WHERE provider = ?", (provider,)):
        if r["address_id"] in out:
            continue
        s = infer.state_of(r["p_yes"], r["n_neighbours"])
        out[r["address_id"]] = {"inferred yes": "iy", "inferred no": "in", "uncertain": "u"}[s]
    return out


def any_state(states: list[str]) -> str:
    known = [s for s in states if s != "x"]
    if "my" in states:
        return "my"
    if "iy" in states:
        return "iy"
    if "u" in states:
        return "u"
    if known and all(s == "mn" for s in known):
        return "mn"
    if known and all(s in ("mn", "in") for s in known):
        return "in"
    return "x"


def bucket(state: str) -> int:
    return {"my": 0, "iy": 0, "mn": 1, "in": 1}.get(state, 2)


def per_address(con: sqlite3.Connection, providers: list[str]) -> dict[str, dict[str, str]]:
    """{provider_key: {address_id: state}} for t, e, g and 'a' (any), with absent addresses as 'x'.

    Polygon hints are accepted for callers that want them, but they never alter a measured state.
    """
    ids = [r[0] for r in con.execute("SELECT id FROM address")]
    district = {r["id"]: r["district"] for r in con.execute("SELECT id, district FROM address")}
    by_key: dict[str, dict[str, str]] = {}
    for name, key in PROVIDER_KEYS.items():
        states = address_states(con, name) if name in providers else {}
        if name == "tng":
            states = {i: s for i, s in states.items() if district[i] in TNG_DISTRICTS}
        by_key[key] = {i: states.get(i, "x") for i in ids}
    by_key["a"] = {i: any_state([by_key[k][i] for k in ("t", "e", "g")]) for i in ids}
    return by_key


def cell_polygon(cell: str, to_ll: Transformer) -> dict:
    cx, cy = (int(v) for v in cell.split("_"))
    corners = [(cx * 100, cy * 100), (cx * 100 + 100, cy * 100), (cx * 100 + 100, cy * 100 + 100), (cx * 100, cy * 100 + 100)]
    ring = [[round(v, 5) for v in to_ll.transform(x, y)] for x, y in corners]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def build_cells(con: sqlite3.Connection, states: dict[str, dict[str, str]]) -> dict:
    to_ll = Transformer.from_crs(25832, 4326, always_xy=True)
    members: dict[str, list[str]] = {}
    for r in con.execute("SELECT id, cell FROM address ORDER BY id"):
        members.setdefault(r["cell"], []).append(r["id"])
    feats = []
    for cell, ids in sorted(members.items()):
        props: dict = {"n": len(ids)}
        if len(ids) < MIN_CELL_ADDRESSES:
            props["s"] = 1
        else:
            for key in ("a", "t", "e", "g"):
                counts = [0, 0, 0]
                for i in ids:
                    counts[bucket(states[key][i])] += 1
                props[key] = tens(counts)
        feats.append({"type": "Feature", "properties": props, "geometry": cell_polygon(cell, to_ll)})
    return {"type": "FeatureCollection", "features": feats}


def build_districts(con: sqlite3.Connection, states: dict[str, dict[str, str]], polygons: list[dict]) -> dict:
    by_district: dict[str, list[str]] = {}
    for r in con.execute("SELECT id, district FROM address"):
        by_district.setdefault(r["district"], []).append(r["id"])
    feats = []
    for f in polygons:
        name = f["properties"]["STT_NAME"]
        ids = by_district.get(name, [])
        props: dict = {"name": name, "n": len(ids)}
        for key in ("a", "t", "e", "g"):
            counts = [0, 0, 0]
            for i in ids:
                counts[bucket(states[key][i])] += 1
            props[key] = tens(counts)
        geom = shape(f["geometry"]).simplify(0.00005)
        feats.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": json.loads(json.dumps(mapping(geom), separators=(",", ":"))),
            }
        )
    feats.sort(key=lambda f: f["properties"]["name"])
    return {"type": "FeatureCollection", "features": feats}


def _round_coords(obj):
    if isinstance(obj, (list, tuple)):
        return [_round_coords(o) for o in obj]
    if isinstance(obj, float):
        return round(obj, 5)
    return obj


def build_summary(con: sqlite3.Connection, providers: list[str], updated_at: str, frame_version: str) -> dict:
    out: dict = {
        "updated_at": updated_at,
        "frame_version": frame_version,
        "addresses": con.execute("SELECT COUNT(*) FROM address").fetchone()[0],
        "providers": {},
    }
    for name in providers:
        rep = validate.report(con, name)
        measured = con.execute(
            "SELECT COUNT(*) FROM measurement WHERE provider = ? AND status IN ('yes','no')", (name,)
        ).fetchone()[0]
        pending = con.execute(
            "SELECT COUNT(*) FROM measurement WHERE provider = ? AND status = 'pending'", (name,)
        ).fetchone()[0]
        last = con.execute("SELECT MAX(measured_at) FROM measurement WHERE provider = ?", (name,)).fetchone()[0]
        out["providers"][name] = {
            "measured": measured,
            "pending": pending,
            "last_measured": last,
            "coverage": rep["coverage"],
            "coverage_ci": list(rep["coverage_ci"]),
            "error_rate": rep["error_rate"],
            "error_ci": list(rep["error_ci"]),
            "uncertain_share": rep["uncertain_share"],
            "unmatched_share": (rep["unmatched"] / rep["holdout_rows"]) if rep["holdout_rows"] else None,
        }
    return out


def build_polygons(data_dir: Path) -> dict | None:
    """Telekom's public fiber polygons, reprojected to WGS84, holes preserved."""
    from shapely.ops import transform

    from . import polygon

    feats = polygon.load_polygons(data_dir)
    if not feats:
        return None
    to_ll = Transformer.from_crs(3857, 4326, always_xy=True)
    out = []
    for geom in sorted(feats, key=lambda g: g.area, reverse=True):
        projected = transform(to_ll.transform, geom)
        out.append({"type": "Feature", "properties": {}, "geometry": mapping(projected)})
    version_path = data_dir / "frame" / "polygons" / "VERSION"
    version = version_path.read_text().strip() if version_path.exists() else ""
    return {"type": "FeatureCollection", "version": version, "features": out}


def write(out_dir: Path, cells: dict, districts: dict, summary: dict, polygons: dict | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [("cells.geojson", cells), ("districts.geojson", districts), ("summary.json", summary)]
    if polygons is None:
        polygons = {"type": "FeatureCollection", "features": []}
    files.append(("polygons.geojson", polygons))
    for name, obj in files:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        data = text.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise SystemExit(f"{name} is {len(data)} bytes, above the {MAX_FILE_BYTES} limit")
        (out_dir / name).write_bytes(data)
