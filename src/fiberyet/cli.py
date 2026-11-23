from __future__ import annotations

import datetime as dt
import argparse
import sys
from pathlib import Path

from . import db
from .fetch import Blocked, Fetcher, ProxyConfigError, proxy_transport_from_env

PROVIDERS = ("telekom", "einsundeins", "tng")


def _districts(value: str | None) -> list[str] | None:
    return [d.strip() for d in value.split(",") if d.strip()] if value else None


def make_fetcher(provider: str, pause_s: float | None = None) -> Fetcher:
    return Fetcher(pause_s=pause_s, transport=proxy_transport_from_env(provider))


def measure_with(provider: str, fetcher: Fetcher):
    if provider == "telekom":
        from .providers import telekom

        return lambda street, number, plz: telekom.measure(fetcher, street, number)
    if provider == "einsundeins":
        from .providers import einsundeins

        return lambda street, number, plz: einsundeins.measure(fetcher, street, number, plz)
    if provider == "tng":
        from .providers import tng

        return lambda street, number, plz: tng.measure(fetcher, street, number, plz)
    raise SystemExit(f"unknown provider {provider}")


def advance_pause(data_dir: Path, provider: str) -> float | None:
    from . import advance

    return advance.read_state(data_dir, provider)["pause_s"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fiberyet")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("frame", help="download the Frankfurt address list into the data dir")

    p = sub.add_parser("advance", help="run one incremental crawl slice")
    p.add_argument("--provider", required=True, choices=PROVIDERS)
    p.add_argument("--budget-minutes", type=float, default=320)
    p.add_argument("--checkpoint-minutes", type=float, default=None, help="also commit and push this often (default: once, at the end)")
    p.add_argument("--flush-rows", type=int, default=100, help="write progress to disk every N rows")
    p.add_argument("--districts")
    p.add_argument("--no-push", action="store_true")

    p = sub.add_parser("save-and-push", help="commit and push whatever is in the data dir")
    p.add_argument("--provider", required=True, choices=PROVIDERS)

    p = sub.add_parser("sample", help="plan what to measure (works on the loaded data dir)")
    p.add_argument("what", choices=["holdout-tasks", "coarse", "refine"])
    p.add_argument("--provider", required=True, choices=PROVIDERS)
    p.add_argument("--districts")
    p.add_argument("--retry-unmatched", action="store_true")

    p = sub.add_parser("infer", help="predict unmeasured addresses")
    p.add_argument("--provider", required=True, choices=PROVIDERS)

    p = sub.add_parser("validate", help="score against the hold-out")
    p.add_argument("--provider", required=True, choices=PROVIDERS)
    p.add_argument("--against", choices=PROVIDERS)

    p = sub.add_parser("probe", help="measure one address, write nothing")
    p.add_argument("--provider", required=True, choices=PROVIDERS)
    p.add_argument("--street", required=True)
    p.add_argument("--number", required=True)

    p = sub.add_parser("export", help="write the published cell and district files")
    p.add_argument("--out", type=Path, default=Path("dist/data"))
    return parser


def run_probe(args, con) -> None:
    try:
        fetcher = make_fetcher(args.provider)
    except ProxyConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    row = con.execute(
        "SELECT plz FROM address WHERE street = ? AND house_number = ?", (args.street, args.number)
    ).fetchone()
    try:
        r = measure_with(args.provider, fetcher)(args.street, args.number, row["plz"] if row else None)
    except Blocked as exc:
        print(f"{args.provider} BLOCKED status={exc.status}")
        sys.exit(3)
    finally:
        fetcher.close()
    print(args.provider, r.status, r.detail or "-")


def run_advance(args, con) -> None:
    from . import advance

    try:
        fetcher = make_fetcher(args.provider, advance_pause(args.data_dir, args.provider))
    except ProxyConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    try:
        code = advance.advance(
            con,
            args.data_dir,
            args.provider,
            measure_with(args.provider, fetcher),
            budget_s=args.budget_minutes * 60,
            checkpoint_s=None if args.checkpoint_minutes is None else args.checkpoint_minutes * 60,
            flush_rows=args.flush_rows,
            districts=_districts(args.districts),
            push=not args.no_push,
        )
    finally:
        fetcher.close()
    sys.exit(code)


def run_export(args, con) -> None:
    import json

    from . import export, frame, infer

    for name in PROVIDERS:
        infer.predict(con, name)
    states = export.per_address(con, list(PROVIDERS))
    cells = export.build_cells(con, states)
    cache = args.data_dir / "frame" / "districts.geojson"
    polygons = json.loads(cache.read_text())["features"] if cache.exists() else frame.fetch_districts()
    districts = export.build_districts(con, states, polygons)
    version = (args.data_dir / "frame" / "VERSION").read_text().strip()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    poly_layer = export.build_polygons(args.data_dir)
    export.write(args.out, cells, districts, export.build_summary(con, list(PROVIDERS), stamp, version), poly_layer)
    print(f"{len(cells['features'])} cells, {len(districts['features'])} districts")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.cmd == "frame":
        import json

        from . import frame, store

        con = db.connect()
        n = frame.build(con)
        store.save_frame(con, args.data_dir, dt.date.today().isoformat())
        (args.data_dir / "frame" / "districts.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": frame.fetch_districts()}, separators=(",", ":"))
        )
        print(f"{n} addresses")
        return
    from . import store

    con = store.load(args.data_dir)

    if args.cmd == "probe":
        run_probe(args, con)
        return

    if args.cmd == "save-and-push":
        pushed = store.push(args.data_dir, f"data: {args.provider} final save")
        print("pushed" if pushed else "nothing to push")
        return

    if args.cmd == "advance":
        run_advance(args, con)
    elif args.cmd == "export":
        run_export(args, con)
    elif args.cmd == "sample":
        from . import sampling

        districts = _districts(args.districts)
        if args.what == "holdout-tasks":
            n = sampling.holdout_tasks(con, args.provider)
        elif args.what == "coarse" and args.retry_unmatched:
            n = sampling.retry_unmatched(con, args.provider, districts)
        elif args.what == "coarse":
            n = sampling.coarse(con, args.provider, districts)
        else:
            n = sampling.refine(con, args.provider, districts)
        store.save(con, args.data_dir, args.provider)
        print(f"{n} rows planned")
    elif args.cmd == "infer":
        from . import infer

        print(f"{infer.predict(con, args.provider)} predictions")
    elif args.cmd == "validate":
        from . import validate

        for k, v in validate.report(con, args.provider).items():
            print(f"{k}: {v}")
        if args.against:
            print("agreement", args.provider, "x", args.against)
            for (a, b), n in sorted(validate.agreement(con, args.provider, args.against).items()):
                print(f"  {args.provider}={a} {args.against}={b}: {n}")
