from conftest import make_con, put

from fiberyet import infer, validate


def truth(x):
    return "yes" if x < 400 else "no"


def measure_every_third(con):
    rows = con.execute("SELECT id, x FROM address ORDER BY id").fetchall()
    for i, r in enumerate(rows):
        if i % 3 == 0:
            put(con, r["id"], "p", "coarse", truth(r["x"]))


def test_interior_inferred_correctly_and_border_uncertain(grid):
    measure_every_third(grid)
    infer.predict(grid, "p")
    preds = {r["address_id"]: r for r in grid.execute("SELECT * FROM prediction")}
    xs = {r["id"]: r["x"] for r in grid.execute("SELECT id, x FROM address")}
    interior_wrong = border_decided = 0
    for id_, r in preds.items():
        s = infer.state_of(r["p_yes"], r["n_neighbours"])
        x = xs[id_]
        if abs(x - 400) > 60:
            expected = "inferred yes" if x < 400 else "inferred no"
            interior_wrong += s != expected
        if abs(x - 390) < 15 and s != "uncertain":
            border_decided += 1
    assert interior_wrong == 0
    assert border_decided == 0


def test_two_neighbours_are_uncertain():
    con = make_con([("m1", 0, 0, "D"), ("m2", 10, 0, "D"), ("t", 5, 5, "D")])
    put(con, "m1", "p", "coarse", "yes")
    put(con, "m2", "p", "coarse", "yes")
    infer.predict(con, "p")
    row = con.execute("SELECT * FROM prediction WHERE address_id = 't'").fetchone()
    assert row["n_neighbours"] == 2 and row["p_yes"] == 1.0
    assert infer.state_of(row["p_yes"], row["n_neighbours"]) == "uncertain"


def test_no_neighbour_within_radius_has_null_p():
    con = make_con([("m", 0, 0, "D"), ("far", 1000, 0, "D")])
    put(con, "m", "p", "coarse", "yes")
    infer.predict(con, "p")
    row = con.execute("SELECT * FROM prediction WHERE address_id = 'far'").fetchone()
    assert row["p_yes"] is None and row["n_neighbours"] == 0


def test_holdout_rows_are_never_neighbours():
    con = make_con([("h1", 0, 0, "D"), ("h2", 5, 0, "D"), ("h3", 10, 0, "D"), ("t", 5, 5, "D")])
    for i in ("h1", "h2", "h3"):
        put(con, i, "p", "holdout", "yes")
    infer.predict(con, "p")
    row = con.execute("SELECT * FROM prediction WHERE address_id = 't'").fetchone()
    assert row["n_neighbours"] == 0


def test_validate_scores_holdout_without_leaking_it(grid):
    measure_every_third(grid)
    ids = [r["id"] for r in grid.execute("SELECT id, x FROM address WHERE x > 100 AND x < 300 ORDER BY id")]
    holdout = [i for i in ids if i not in {r["address_id"] for r in grid.execute("SELECT address_id FROM measurement")}][:20]
    for i in holdout:
        put(grid, i, "p", "holdout", "yes")
    rep = validate.report(grid, "p")
    assert rep["holdout_usable"] == 20
    assert rep["coverage"] == 1.0
    assert rep["error_rate"] == 0.0 and rep["decided"] == 20


def test_wilson_bounds():
    lo, hi = validate.wilson(0, 100)
    assert lo == 0.0 and 0.0 < hi < 0.05
    lo, hi = validate.wilson(50, 100)
    assert lo < 0.5 < hi
    assert validate.wilson(0, 0) == (0.0, 1.0)
