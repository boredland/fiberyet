import csv

from conftest import make_con, put

from fiberyet import oxg_check


def build():
    """Per district: 1&1 and Telekom answers chosen so every sampling group has a known pool."""
    rows = []
    for i in range(30):
        rows.append((f"hoechst{i:02d}", i * 10.0, 0.0, "Höchst"))
        rows.append((f"city{i:02d}", i * 10.0, 500.0, "Altstadt"))
        rows.append((f"fringe{i:02d}", i * 10.0, 900.0, "Nordend-West"))
    con = make_con(rows)
    for i in range(30):
        # Build area: 1&1 yes for half, Telekom never measured.
        put(con, f"hoechst{i:02d}", "einsundeins", "coarse", "yes" if i % 2 else "no")
        # Altstadt: 1&1 yes and Telekom no, so these feed the precision group.
        put(con, f"city{i:02d}", "einsundeins", "coarse", "yes")
        put(con, f"city{i:02d}", "telekom", "coarse", "no")
        # Nordend-West: 1&1 no and Telekom no, so these feed the elsewhere group.
        put(con, f"fringe{i:02d}", "einsundeins", "coarse", "no")
        put(con, f"fringe{i:02d}", "telekom", "coarse", "no")
    con.commit()
    return con


def test_each_group_draws_only_from_its_own_pool():
    rows = oxg_check.sample(build(), n=10)
    by_group = {}
    for r in rows:
        by_group.setdefault(r["group"], []).append(r)
    assert {g: len(v) for g, v in by_group.items()} == {"build_area": 10, "elsewhere": 10, "precision": 10}
    assert all(r["district"] == "Höchst" for r in by_group["build_area"])
    assert all(r["district"] == "Nordend-West" and (r["einsundeins"], r["telekom"]) == ("no", "no") for r in by_group["elsewhere"])
    assert all(r["district"] == "Altstadt" and (r["einsundeins"], r["telekom"]) == ("yes", "no") for r in by_group["precision"])


def test_no_address_appears_twice_and_draw_is_repeatable():
    con = build()
    first = oxg_check.sample(con, n=25)
    assert len({r["id"] for r in first}) == len(first)
    assert [r["id"] for r in first] == [r["id"] for r in oxg_check.sample(con, n=25)]


def test_a_build_area_address_is_not_also_sampled_as_elsewhere():
    con = make_con([("h", 0.0, 0.0, "Höchst")])
    put(con, "h", "einsundeins", "coarse", "no")
    put(con, "h", "telekom", "coarse", "no")
    con.commit()
    assert [r["group"] for r in oxg_check.sample(con)] == ["build_area"]


def test_addresses_without_an_einsundeins_answer_are_skipped():
    con = make_con([("a", 0.0, 0.0, "Höchst"), ("b", 10.0, 0.0, "Höchst")])
    put(con, "a", "einsundeins", "coarse", "pending")
    put(con, "b", "einsundeins", "coarse", "unmatched")
    con.commit()
    assert oxg_check.sample(con) == []


def test_sheet_has_an_empty_oxg_column_to_fill_in(tmp_path):
    path = tmp_path / "sheet.csv"
    counts = oxg_check.write(build(), path, n=5)
    assert counts == {"build_area": 5, "elsewhere": 5, "precision": 5}
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == oxg_check.COLUMNS
    assert len(rows) == 15 and all(r["oxg"] == "" for r in rows)
