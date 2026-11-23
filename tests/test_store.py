import gzip
import json

import pytest
from conftest import make_con, put

from fiberyet import sampling, store


def frame_con(n=2000):
    con = make_con([(f"id{i:05d}", float(i % 50) * 30, float(i // 50) * 30, "D") for i in range(n)])
    con.execute("UPDATE address SET plz = NULL WHERE id = 'id00007'")
    return con


def test_frame_roundtrip_and_deterministic_gzip(tmp_path):
    con = frame_con(200)
    store.save_frame(con, tmp_path, "2026-11-22")
    first = (tmp_path / "frame" / "addresses.csv.gz").read_bytes()
    store.save_frame(con, tmp_path, "2026-11-22")
    assert first == (tmp_path / "frame" / "addresses.csv.gz").read_bytes()
    lines = gzip.decompress(first).decode().splitlines()
    assert lines[0] == "id,street,house_number,plz,district,x,y,lon,lat"
    assert [l.split(",")[0] for l in lines[1:]] == sorted(l.split(",")[0] for l in lines[1:])

    work = store.load(tmp_path, tmp_path / "w.sqlite")
    assert work.execute("SELECT COUNT(*) FROM address").fetchone()[0] == 200
    assert work.execute("SELECT plz FROM address WHERE id = 'id00007'").fetchone()[0] is None
    assert work.execute("SELECT cell FROM address WHERE id = 'id00051'").fetchone()[0] == "0_0"


def test_holdout_rederived_identically_on_every_load(tmp_path):
    store.save_frame(frame_con(2000), tmp_path, "v")
    ids = lambda c: [r[0] for r in c.execute("SELECT id FROM address WHERE holdout = 1 ORDER BY id")]
    a = store.load(tmp_path, tmp_path / "a.sqlite")
    b = store.load(tmp_path, tmp_path / "b.sqlite")
    assert len(ids(a)) == 1000 and ids(a) == ids(b)


def test_measurement_roundtrip_is_sorted_compact_and_byte_stable(tmp_path):
    store.save_frame(frame_con(200), tmp_path, "v")
    con = store.load(tmp_path, tmp_path / "w.sqlite")
    put(con, "id00003", "telekom", "coarse", "yes")
    con.execute("UPDATE measurement SET detail='AVAILABLE_FIBER', measured_at='2026-11-22T13:04:17Z', raw='BODY'"
                " WHERE address_id='id00003'")
    put(con, "id00001", "telekom", "coarse", "unmatched")
    con.execute("UPDATE measurement SET raw='SEARCH', measured_at='2026-11-22T13:05:00Z' WHERE address_id='id00001'")
    put(con, "id00002", "telekom", "refine", "pending")
    con.commit()

    store.save(con, tmp_path, "telekom")
    path = tmp_path / "measurements" / "telekom.jsonl"
    first = path.read_bytes()
    rows = [json.loads(l) for l in first.decode().splitlines()]
    assert [r["id"] for r in rows] == ["id00001", "id00002", "id00003"]
    assert "raw" in rows[0] and rows[0]["status"] == "unmatched"
    assert rows[1] == {"id": "id00002", "stage": "refine", "status": "pending"}
    assert "raw" in rows[2] and rows[2]["detail"] == "AVAILABLE_FIBER"
    assert b" " not in first.split(b"\n")[2]

    store.save(con, tmp_path, "telekom")
    assert path.read_bytes() == first

    again = store.load(tmp_path, tmp_path / "w2.sqlite")
    got = {r["address_id"]: dict(r) for r in again.execute("SELECT * FROM measurement")}
    assert got["id00003"]["status"] == "yes" and got["id00003"]["raw"] == "BODY"
    assert got["id00001"]["raw"] == "SEARCH"
    assert got["id00002"]["measured_at"] is None


def test_load_without_frame_fails(tmp_path):
    with pytest.raises(SystemExit):
        store.load(tmp_path, tmp_path / "w.sqlite")


def test_holdout_marking_matches_sampling_helper():
    ids = [f"id{i:05d}" for i in range(2000)]
    assert len(sampling.holdout_ids(ids)) == 1000
