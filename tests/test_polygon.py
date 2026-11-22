"""The polygon hint is a ranking input for sampling and a separate map view, never a measured status."""

import json

from conftest import make_con, put
from pyproj import Transformer

from fiberyet import export, polygon, sampling

_TO_3857 = Transformer.from_crs(4326, 3857, always_xy=True)
BASE_LON, BASE_LAT = 8.0, 50.0
BASE_X, BASE_Y = _TO_3857.transform(BASE_LON, BASE_LAT)


def data_dir_with_polygons(tmp_path):
    frame = tmp_path / "frame" / "polygons"
    frame.mkdir(parents=True)
    x0, x1, y0, y1 = BASE_X - 30, BASE_X + 30, BASE_Y - 20, BASE_Y + 20
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [
            [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}}]}
    (frame / "fiber.geojson").write_text(json.dumps(fc))
    (frame / "VERSION").write_text("2026-11-23T00:00:00Z\n")
    return tmp_path


def con_for_polygons(tmp_path):
    con = make_con([("a0", 10.0, 10.0, "D"), ("a1", 20.0, 20.0, "D"),
                    ("b0", 200.0, 200.0, "D"), ("b1", 210.0, 210.0, "D")])
    # a0 inside the box, a1 just outside but same cell -> boundary cell
    con.execute("UPDATE address SET lon=?, lat=? WHERE id='a0'", (BASE_LON, BASE_LAT))
    con.execute("UPDATE address SET lon=?, lat=? WHERE id='a1'", (BASE_LON + 0.001, BASE_LAT))
    # b0, b1 outside the box -> uniform cell
    con.execute("UPDATE address SET lon=?, lat=? WHERE id='b0'", (BASE_LON + 0.01, BASE_LAT + 0.01))
    con.execute("UPDATE address SET lon=?, lat=? WHERE id='b1'", (BASE_LON + 0.02, BASE_LAT + 0.01))
    return con


def test_hint_marks_addresses_inside_the_polygon(tmp_path):
    con = con_for_polygons(tmp_path)
    hints = polygon.polygon_hints(con, data_dir_with_polygons(tmp_path))
    assert hints["a0"] is True
    assert hints["a1"] is False
    assert hints["b0"] is False
    assert hints["b1"] is False


def test_coarse_orders_mixed_cells_before_uniform_cells(tmp_path):
    con = con_for_polygons(tmp_path)
    data_dir = data_dir_with_polygons(tmp_path)
    assert sampling.coarse(con, "p", data_dir=data_dir) == 2
    rows = list(con.execute("SELECT address_id FROM measurement ORDER BY rowid"))
    assert rows[0]["address_id"] in {"a0", "a1"}  # boundary cell first
    assert rows[1]["address_id"] in {"b0", "b1"}


def test_coarse_without_polygons_keeps_the_old_behaviour(tmp_path):
    con = con_for_polygons(tmp_path)
    assert sampling.coarse(con, "p", data_dir=tmp_path) == 2


def test_export_adds_a_polygon_layer_with_a_version(tmp_path):
    con = con_for_polygons(tmp_path)
    data_dir = data_dir_with_polygons(tmp_path)
    layer = export.build_polygons(data_dir, con)
    assert len(layer["features"]) == 2
    assert layer["features"][0]["geometry"]["type"] == "MultiPolygon"
    assert layer["features"][1]["properties"] == {"i": -1, "version": "2026-11-23T00:00:00Z"}
    export.write(tmp_path / "dist", {"features": []}, {"features": []}, {}, layer)
    assert (tmp_path / "dist" / "polygons.geojson").exists()


def test_measured_statuses_are_not_overwritten_by_the_hint(tmp_path):
    con = con_for_polygons(tmp_path)
    put(con, "b0", "telekom", "coarse", "yes")
    data_dir_with_polygons(tmp_path)
    states = export.per_address(con, ["telekom"])
    assert states["t"]["b0"] == "my"
    assert states["t"]["a0"] == "x"
