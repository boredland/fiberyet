import json

from conftest import make_con, put

from fiberyet import export, infer


def cell_world():
    """Cell 0_0 has 6 addresses (shown), cell 1_0 has 2 (suppressed), cell 2_0 has 5 (boundary, shown)."""
    addrs = [(f"a{i}", 10.0 + i, 10.0, "Altstadt") for i in range(6)]
    addrs += [(f"b{i}", 110.0 + i, 10.0, "Altstadt") for i in range(2)]
    addrs += [(f"c{i}", 210.0 + i, 10.0, "Heddernheim") for i in range(5)]
    con = make_con(addrs)
    for i in range(6):
        put(con, f"a{i}", "telekom", "coarse", "yes" if i < 3 else "no")
    for i in range(2):
        put(con, f"b{i}", "telekom", "coarse", "yes")
    for i in range(5):
        put(con, f"c{i}", "telekom", "coarse", "no")
    infer.predict(con, "telekom")
    return con


def build(con):
    states = export.per_address(con, ["telekom"])
    return states, export.build_cells(con, states)


def test_small_cells_carry_no_shares():
    _, cells = build(cell_world())
    by_n = {f["properties"]["n"]: f["properties"] for f in cells["features"]}
    assert by_n[2] == {"n": 2, "s": 1}
    assert export.MIN_CELL_ADDRESSES == 5
    assert "a" in by_n[5] and "s" not in by_n[5]
    assert "a" in by_n[6]


def test_shares_are_tens_and_sum_to_100():
    _, cells = build(cell_world())
    for f in cells["features"]:
        for k in ("a", "t", "e", "g"):
            if k in f["properties"]:
                v = f["properties"][k]
                assert sum(v) == 100 and all(x % 10 == 0 for x in v), (k, v)


def test_values_for_half_yes_cell():
    _, cells = build(cell_world())
    six = next(f["properties"] for f in cells["features"] if f["properties"]["n"] == 6)
    assert six["t"] == [50, 50, 0]
    assert six["e"] == [0, 0, 100] and six["g"] == [0, 0, 100]  # nobody measured 1&1 or TNG
    assert six["a"] == [50, 50, 0]


def test_only_allowed_property_keys():
    _, cells = build(cell_world())
    keys = {k for f in cells["features"] for k in f["properties"]}
    assert keys <= {"n", "s", "a", "t", "e", "g"}


def test_no_street_or_id_leaks_into_any_output(tmp_path):
    con = cell_world()
    con.execute("UPDATE address SET street = 'Geheimweg', house_number = '99z'")
    states = export.per_address(con, ["telekom"])
    polys = [{"properties": {"STT_NAME": "Altstadt"},
              "geometry": {"type": "Polygon", "coordinates": [[[8.0, 50.0], [8.1, 50.0], [8.1, 50.1], [8.0, 50.0]]]}}]
    export.write(tmp_path, export.build_cells(con, states), export.build_districts(con, states, polys),
                 export.build_summary(con, ["telekom"], "2026-11-22T00:00:00Z", "v"))
    blob = "".join((tmp_path / n).read_text() for n in ("cells.geojson", "districts.geojson", "summary.json"))
    assert "Geheimweg" not in blob and "99z" not in blob and '"a0"' not in blob and "a0" not in json.dumps(
        [f["properties"] for f in json.loads((tmp_path / "cells.geojson").read_text())["features"]])


def test_tng_is_unknown_outside_its_districts():
    con = make_con([(f"o{i}", 10.0 + i, 10.0, "Altstadt") for i in range(5)])
    for i in range(5):
        put(con, f"o{i}", "tng", "coarse", "yes")
    states = export.per_address(con, ["tng"])
    assert set(states["g"].values()) == {"x"}
    assert set(states["a"].values()) == {"x"}


def test_any_state_rules():
    assert export.any_state(["x", "x", "x"]) == "x"
    assert export.any_state(["mn", "x", "x"]) == "mn"
    assert export.any_state(["mn", "in", "x"]) == "in"
    assert export.any_state(["mn", "my", "x"]) == "my"
    assert export.any_state(["mn", "iy", "x"]) == "iy"
    assert export.any_state(["mn", "u", "x"]) == "u"


def test_tens_largest_remainder():
    assert export.tens([1, 1, 1]) == [40, 30, 30]
    assert export.tens([5, 0, 0]) == [100, 0, 0]
    assert export.tens([0, 0, 0]) == [0, 0, 100]
    assert sum(export.tens([7, 3, 11])) == 100


def test_oversize_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "MAX_FILE_BYTES", 10)
    import pytest

    with pytest.raises(SystemExit):
        export.write(tmp_path, {"type": "FeatureCollection", "features": [{"x": "y" * 100}]}, {}, {})


def test_summary_has_no_address_level_data():
    s = export.build_summary(cell_world(), ["telekom"], "t", "v")
    assert set(s) == {"updated_at", "frame_version", "addresses", "providers"}
    assert s["providers"]["telekom"]["measured"] == 13
