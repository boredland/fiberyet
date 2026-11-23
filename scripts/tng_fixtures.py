"""Capture TNG availability result pages as test fixtures.

One browser lookup per address, 5 s apart, page saved verbatim to tests/fixtures/tng_<slug>.html.

Usage: python scripts/tng_fixtures.py "Alt-Heddernheim,30" "Römerberg,23" ...
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fiberyet import store
from fiberyet.providers import tng

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def slug(street: str, number: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (street + "_" + number).casefold())
    return s.strip("_")


def main(addrs: list[str]) -> None:
    con = store.load(Path("data"))
    OUT.mkdir(parents=True, exist_ok=True)
    for entry in addrs:
        street, _, number = entry.partition(",")
        if not number:
            raise SystemExit(f"expected Street,Number got {entry!r}")
        row = con.execute(
            "SELECT plz FROM address WHERE street = ? AND house_number = ?", (street, number)
        ).fetchone()
        plz = row["plz"] if row else None
        if not plz:
            print(f"{entry}: no plz in the address table, skipping")
            continue
        tng.pace()
        try:
            html, err = tng.fetch_result_html(street, number, plz)
        except Exception as exc:  # noqa: BLE001
            print(f"{entry}: error playwright:{type(exc).__name__}: {str(exc)[:200]}")
            continue
        if html is None:
            print(f"{entry}: error {err}")
            continue
        path = OUT / f"tng_{slug(street, number)}.html"
        path.write_text(html)
        print(f"{entry}: saved {path.name} ({len(html)} bytes) -> {tng.classify(html)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1:])
