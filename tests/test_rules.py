"""Decline rules on synthetic seasonal composites (one array of OSAVI per year)."""
import datetime as dt

import numpy as np
import pytest

import xylella_scanner as xs

H, W = 60, 80
YEARS = range(2017, 2027)


def run(comps, landmask=None, rules=None, seasons=range(2019, 2027)):
    landmask = np.ones((H, W), bool) if landmask is None else landmask
    flagged = np.zeros((H, W), np.uint32)
    recovered = np.zeros((H, W), np.uint32)
    xs.update_history(comps, list(seasons), landmask, flagged, recovered, rules=rules)
    status, first = xs.classify(flagged, recovered, max(seasons))
    return flagged, recovered, status, first


def name(status, r, c):
    return xs.STATUS_NAMES.get(int(status[r, c]), "none")


@pytest.fixture
def world(rng):
    comps = {y: (0.25 + rng.normal(0, 0.005, (H, W))).astype("float32") for y in YEARS}
    for y in range(2021, 2027):                      # A: decline 2021, never recovers
        comps[y][5:9, 5:9] = 0.12 if y == 2021 else 0.09
    comps[2022][5:9, 20:24] = 0.14                   # B: pruning, recovers in 2023
    for y, v in ((2023, 0.08), (2024, 0.40), (2025, 0.08), (2026, 0.40)):
        comps[y][20:24, 5:9] = v                     # C: rotating crop
    comps[2026][20:24, 30:34] = 0.12                 # D: new decline in 2026
    for y in (2024, 2025):
        comps[y][35:39, 30:34] = 0.16                # E: declines 2024 and again 2026
    comps[2026][35:39, 30:34] = 0.10
    comps[2026][40, 60:62] = 0.10                    # F: 2-pixel patch, too small
    for y in YEARS:
        comps[y][50:54, 5:9] = 0.60 if y < 2026 else 0.20   # H: irrigated crop
    comps[2026][50:54, 30:34] = 0.20                 # I: 20 % drop only
    return comps


def test_olive_scenarios(world):
    flagged, recovered, status, first = run(world)
    assert name(status, 6, 6) == "earlier" and first[6, 6] == 2021
    assert name(status, 6, 21) == "none"         # pruning recovered
    assert name(status, 21, 6) == "none"         # rotating crop
    assert name(status, 21, 31) == "new"
    assert name(status, 36, 31) == "continuing"
    assert name(status, 40, 60) == "none"        # too small
    assert name(status, 51, 6) == "none"         # above baseline cap
    assert name(status, 51, 31) == "none"        # drop too small
    assert name(status, 30, 70) == "none"


def test_olive_rules_equal_defaults(world):
    """Passing the olive profile must give exactly the old default behaviour."""
    a = run(world)
    b = run(world, rules=xs.profile_rules("olive"))
    for x, y in zip(a, b):
        assert (x == y).all()


def test_builtup_mask_excludes(world):
    world[2026][50:54, 60:64] = 0.10
    land = np.ones((H, W), bool)
    land[50:54, 60:64] = False
    *_, status, _ = run(world, landmask=land)
    assert name(status, 51, 61) == "none"


def test_incremental_update_matches_full(world):
    flagged, recovered, *_ = run(world)
    f2, r2 = flagged.copy(), recovered.copy()
    xs.update_history({y: world[y] for y in range(2023, 2027)}, [2025, 2026],
                      np.ones((H, W), bool), f2, r2)
    assert (f2 == flagged).all() and (r2 == recovered).all()


def test_season_stats(world):
    flagged, recovered, *_ = run(world)
    stats = xs.season_stats(flagged, recovered, range(2019, 2027))
    assert stats[2021]["new_ha"] == 0.16
    assert stats[2022]["recovered_ha"] == 0.16
    assert stats[2026]["new_ha"] == 0.16 and stats[2026]["again_ha"] == 0.16


def test_almond_profile_catches_gradual_decline(rng):
    """A 27 % drop below a stable three-summer baseline: too small for the olive
    rules, flagged by the almond profile. A drought year that is followed by a
    full rebound is cleared by the recovery rule in both."""
    comps = {y: (0.20 + rng.normal(0, 0.003, (H, W))).astype("float32") for y in YEARS}
    comps[2026][10:14, 10:14] = 0.146                # gradual decline
    comps[2024][30:34, 30:34] = 0.12                 # drought year ...
    comps[2025][30:34, 30:34] = 0.20                 # ... full rebound
    almond = xs.profile_rules("almond")
    *_, olive_status, _ = run(comps)
    *_, almond_status, _ = run(comps, rules=almond)
    assert name(olive_status, 11, 11) == "none"
    assert name(almond_status, 11, 11) == "new"
    assert name(almond_status, 31, 31) == "none"


def test_almond_skips_unsteady_baseline(rng):
    comps = {y: (0.20 + rng.normal(0, 0.003, (H, W))).astype("float32") for y in YEARS}
    for y, v in zip(range(2019, 2027), (0.30, 0.15, 0.31, 0.14, 0.30, 0.16, 0.30, 0.12)):
        comps[y][10:14, 10:14] = v                   # swings like a field crop
    *_, status, _ = run(comps, rules=xs.profile_rules("almond"))
    assert name(status, 11, 11) == "none"


def test_profiles_are_complete():
    for p in xs.PROFILES:
        rules = xs.profile_rules(p)
        assert set(rules) == set(xs.OLIVE_RULES)
        assert xs.PROFILES[p]["reliability"] in ("literature-based", "inferred", "experimental")
    with pytest.raises(ValueError):
        xs.profile_rules("banana")


def test_params_hash_keeps_olive_regions_stable():
    r = {"id": "x", "bbox": [1, 2, 3, 4]}
    assert xs.params_hash(r) == xs.params_hash({**r, "profile": "olive"})
    assert xs.params_hash(r) != xs.params_hash({**r, "profile": "almond"})
    assert xs.params_hash(r) != xs.params_hash({**r, "mask": {"type": "sigpac-ib", "uses": ["OV"]}})


@pytest.mark.parametrize("today, window, expected", [
    (dt.date(2026, 10, 21), xs.SEASON, (2026, True)),
    (dt.date(2026, 8, 1), xs.SEASON, (2026, False)),
    (dt.date(2026, 7, 10), xs.SEASON, (2025, True)),
    (dt.date(2026, 9, 20), ("08-01", "09-30"), (2026, False)),
    (dt.date(2026, 8, 5), ("08-01", "09-30"), (2025, True)),
])
def test_latest_season(today, window, expected):
    assert xs.latest_season(today, window) == expected


def test_polygon_area():
    sq = {"type": "Polygon", "coordinates": [[[0, 0], [20, 0], [20, 10], [0, 10], [0, 0]],
                                             [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]]]}
    assert xs.polygon_area(sq) == 196.0
