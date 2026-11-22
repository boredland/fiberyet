"""Fetch Telekom's public fiber-coverage polygons once into the data dir.

One WMS request per layer, 3 s apart. This does not touch the address checker and does not record
a block. Commit the output as versioned input with a date in frame/polygons/VERSION.
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

import httpx

WMS = "https://t-map.telekom.de/tmap2/geoserver/public/tmap/public/wms"
LAYERS = {
    "coverage_fixedline_fiber": "fiber.geojson",
    "coverage_fixedline_fiber_1000_vector": "fiber_1000.geojson",
    "coverage_fixedline_fiber_2000_vector": "fiber_2000.geojson",
}
BBOX = "943000,6445000,980000,6480000"  # Frankfurt, EPSG:3857, with margin
PAUSE_S = 3.0


def main(data_dir: Path) -> None:
    out = data_dir / "frame" / "polygons"
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=180, headers={"User-Agent": "ftth-availability-research/0.1"}) as c:
        for layer, filename in LAYERS.items():
            r = c.get(WMS, params={
                "service": "WMS", "version": "1.3.0", "request": "GetMap", "layers": layer, "styles": "",
                "crs": "EPSG:3857", "bbox": BBOX, "width": 1024, "height": 1024,
                "format": "application/json;type=geojson",
            })
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or "json" not in ctype:
                raise SystemExit(f"{layer}: HTTP {r.status_code} {ctype}; stopping")
            (out / filename).write_bytes(r.content)
            print(f"{layer}: {len(r.content)} bytes")
            time.sleep(PAUSE_S)
    (out / "VERSION").write_text(dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    main(p.parse_args().data_dir)
