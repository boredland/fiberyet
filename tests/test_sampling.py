from conftest import make_con, put

from fiberyet import sampling


def big(n=3000):
    return make_con([(f"id{i:05d}", float(i % 100) * 7, float(i // 100) * 7, "D") for i in range(n)])


def test_holdout_is_deterministic_and_sized():
    a, b = big(), big()
    assert sampling.mark_holdout(a) == 1000
    sampling.mark_holdout(b)
    ids = lambda c: [r[0] for r in c.execute("SELECT id FROM address WHERE holdout = 1 ORDER BY id")]
    assert ids(a) == ids(b) and len(ids(a)) == 1000


def test_holdout_never_sampled_as_coarse_or_refine():
    con = big()
    sampling.mark_holdout(con)
    sampling.holdout_tasks(con, "p")
    sampling.coarse(con, "p")
    held = {r[0] for r in con.execute("SELECT id FROM address WHERE holdout = 1")}
    stages = {r["address_id"]: r["stage"] for r in con.execute("SELECT * FROM measurement")}
    assert all(stages[i] == "holdout" for i in held)
    assert all(s != "holdout" for i, s in stages.items() if i not in held)


def test_coarse_picks_one_per_cell_and_is_idempotent():
    con = big()
    sampling.mark_holdout(con)
    first = sampling.coarse(con, "p")
    cells = [r[0] for r in con.execute("SELECT a.cell FROM measurement m JOIN address a ON a.id = m.address_id")]
    assert first == len(set(cells)) == len(cells)
    assert sampling.coarse(con, "p") == 0


def test_coarse_respects_district_scope():
    con = make_con([("a", 0, 0, "X"), ("b", 500, 0, "Y"), ("c", 1000, 0, "X")])
    sampling.coarse(con, "p", districts=["X"])
    got = {r[0] for r in con.execute("SELECT address_id FROM measurement")}
    assert got == {"a", "c"}


def test_retry_unmatched_gives_new_address_and_caps_tries():
    con = make_con([(f"i{k}", float(k), 0.0, "D") for k in range(6)])  # one cell
    put(con, "i0", "p", "coarse", "unmatched")
    assert sampling.retry_unmatched(con, "p") == 1
    new = [r[0] for r in con.execute("SELECT address_id FROM measurement WHERE address_id != 'i0'")]
    assert len(new) == 1
    put(con, new[0], "p", "coarse", "unmatched")
    assert sampling.retry_unmatched(con, "p") == 1
    third = [r[0] for r in con.execute("SELECT address_id FROM measurement WHERE address_id NOT IN ('i0', ?)", (new[0],))]
    put(con, third[0], "p", "coarse", "unmatched")
    assert sampling.retry_unmatched(con, "p") == 0


def test_retry_skips_cells_with_a_real_answer():
    con = make_con([("a", 0, 0, "D"), ("b", 1, 0, "D")])
    put(con, "a", "p", "coarse", "unmatched")
    put(con, "b", "p", "coarse", "yes")
    assert sampling.retry_unmatched(con, "p") == 0


def test_refine_targets_disagreement_and_stops_at_budget(grid):
    rows = grid.execute("SELECT id, x FROM address ORDER BY id").fetchall()
    for i, r in enumerate(rows):
        if i % 3 == 0:
            put(grid, r["id"], "p", "coarse", "yes" if r["x"] < 400 else "no")
    added = sampling.refine(grid, "p")
    assert added > 0
    xs = [r["x"] for r in grid.execute(
        "SELECT a.x FROM measurement m JOIN address a ON a.id = m.address_id WHERE m.stage = 'refine'")]
    assert all(abs(x - 400) <= 160 for x in xs)
    # budget: 15% of 800 addresses + 1000 hold-out slots exceeds 800, so fill the table to prove the cap bites
    for r in grid.execute("SELECT id FROM address").fetchall():
        put(grid, r["id"], "p", "coarse", "yes")
    assert sampling.refine(grid, "p") == 0
