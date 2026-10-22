"""Shared test setup. The tests need only numpy, scipy and pytest: the satellite
and GIS libraries (rasterio, odc-stac, Planetary Computer) are replaced by small
stand-ins, so the detection logic can be checked without network access."""
import sys
import types
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_rasterio_stub():
    """Minimal rasterio: features.shapes yields one square per masked pixel, and
    coordinate transforms divide metres by 1e5 (enough for bookkeeping tests)."""
    if "rasterio" in sys.modules and not getattr(sys.modules["rasterio"], "_is_stub", False):
        return
    rio = types.ModuleType("rasterio")
    rio._is_stub = True
    feat = types.ModuleType("rasterio.features")
    warp = types.ModuleType("rasterio.warp")

    def shapes(src, mask=None, transform=None):
        a, _, c, _, e, f = transform
        for r, col in zip(*np.nonzero(mask)):
            x0, y0 = c + col * a, f + r * e
            ring = [[x0, y0], [x0 + a, y0], [x0 + a, y0 + e], [x0, y0 + e], [x0, y0]]
            yield {"type": "Polygon", "coordinates": [ring]}, float(src[r, col])

    feat.shapes = shapes
    warp.transform = lambda s, d, xs, ys: ([x / 1e5 for x in xs], [y / 1e5 for y in ys])
    warp.transform_geom = lambda s, d, g: {"type": g["type"],
                                           "coordinates": [[[x / 1e5, y / 1e5] for x, y in g["coordinates"][0]]]}
    rio.features, rio.warp = feat, warp
    sys.modules.update({"rasterio": rio, "rasterio.features": feat, "rasterio.warp": warp})


_install_rasterio_stub()


@pytest.fixture
def rng():
    return np.random.default_rng(0)
