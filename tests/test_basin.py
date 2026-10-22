"""Basin screening rules and merge bookkeeping on a synthetic 15 x 15 km cell."""
import datetime as dt
import json
import types

import numpy as np
import pytest

import basin_scanner as bs
import xylella_scanner as xs

H = W = 150                                  # 3 x 3 squares of 50 px (5 km at 100 m)


@pytest.fixture
def basin(tmp_path, monkeypatch, rng):
    monkeypatch.setattr(bs, "OUT", tmp_path / "data/basin")
    monkeypatch.setattr(bs, "CELLS", tmp_path / "data/basin/cells")
    cfg = tmp_path / "basin.json"
    cfg.write_text(json.dumps({"domains": [{"name": "t", "bbox": [16.0, 40.0, 17.0, 41.0]}]}))
    monkeypatch.setattr(bs, "CONFIG", cfg)

    years = range(2018, 2027)
    world = {y: (0.25 + rng.normal(0, 0.003, (H, W))).astype("float32") for y in years}
    world[2026][5:11, 5:15] = 0.10                                    # (0,0) collapse, 60 ha
    for y, v in zip(years, [0.30, 0.18, 0.32, 0.17, 0.33, 0.19, 0.31, 0.30, 0.10]):
        world[y][0:50, 50:100] = v                                    # (0,1) unsteady crop
    world[2025][0:50, 100:150] = 0.36                                 # (0,2) one green year
    for k in range(8):
        world[2026][55 + 5 * k, 5:8] = 0.10                           # (1,0) scattered noise
    for y in years:
        world[y][50:100, 50:100] = 0.50                               # (1,1) dense forest
    world[2026][60:80, 60:80] = 0.25
    land = np.ones((H, W), bool)
    land[100:150, 100:150] = False
    land[100:110, 100:120] = True                                     # (2,2) mostly sea
    world[2026][100:105, 100:103] = 0.10                              # 15 ha flagged there

    loaded = []
    monkeypatch.setattr(xs, "region_geobox", lambda bbox, resolution=10: types.SimpleNamespace(
        shape=(H, W), crs="EPSG:32633", transform=(100.0, 0.0, 600000.0, 0.0, -100.0, 4500000.0)))
    monkeypatch.setattr(xs, "open_catalog", lambda: None)
    monkeypatch.setattr(xs, "build_landmask", lambda c, g, bbox, cid, **kw: (land.copy(), {"worldcover": "stub"}))
    monkeypatch.setattr(xs, "load_burned", lambda c, g, b, seasons, **kw: ({s: None for s in seasons}, "stub"))
    monkeypatch.setattr(xs, "load_season",
                        lambda c, g, b, y, resampling=None, **kw: (loaded.append(y) or world[y].copy(), 20))
    return types.SimpleNamespace(loaded=loaded)


def squares(cells_dir, cid):
    out = {}
    for f in json.loads((cells_dir / f"{cid}.hotspots.json").read_text()):
        x0, y0 = f["geometry"]["coordinates"][0][0]
        key = (round((4500000 - y0 * 1e5) / 5000), round((x0 * 1e5 - 600000) / 5000))
        out[key] = f["properties"]
    return out


def test_basin_rules(basin):
    ph = bs.params_hash()
    bs.process_cell("N40E016", [16, 40, 17, 41], 2026, ph, bs.CELLS)
    assert min(basin.loaded) == 2019                     # three baseline summers
    sq = squares(bs.CELLS, "N40E016")
    assert sq[(0, 0)]["status"] == "new" and sq[(0, 0)]["new_ha"] == 60.0
    assert (0, 1) not in sq                              # unsteady baseline skipped
    assert (0, 2) not in sq                              # one green year doesn't count
    assert (1, 0) not in sq                              # 24 ha in 2,500 ha: below 2 %
    assert (1, 1) not in sq                              # forest above 0.40
    assert sq[(2, 2)]["land_ha"] == 200 and sq[(2, 2)]["share"] == 0.075


def test_merge_keeps_rule_sets_apart(basin):
    ph = bs.params_hash()
    bs.process_cell("N40E016", [16, 40, 17, 41], 2026, ph, bs.CELLS)
    meta_path = bs.CELLS / "N40E016.json"
    meta = json.loads(meta_path.read_text())
    meta_path.write_text(json.dumps({**meta, "params_hash": "older-rules"}))
    bs.cmd_merge(dt.date(2026, 10, 22))
    s = json.loads((bs.OUT / "summary.json").read_text())
    assert s["squares"] == 0 and s["cells_pending"] == 1
    meta_path.write_text(json.dumps(meta))
    bs.cmd_merge(dt.date(2026, 10, 22))
    s = json.loads((bs.OUT / "summary.json").read_text())
    assert s["squares"] == 2 and s["cells_done"] == 1 and s["provisional"] and "rules" in s


def test_pending_cells(basin):
    today = dt.date(2026, 10, 22)
    ph = bs.params_hash()
    assert [c for c, _ in bs.pending_cells(today, ph)] == ["N40E016"]
    bs.process_cell("N40E016", [16, 40, 17, 41], 2026, ph, bs.CELLS)
    assert bs.pending_cells(today, ph) == []
    assert len(bs.pending_cells(dt.date(2027, 9, 10), ph)) == 1     # next season due
