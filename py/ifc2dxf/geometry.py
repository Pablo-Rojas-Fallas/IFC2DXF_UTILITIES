"""Plan geometry: footprints (outlines) from meshes, and derived measures."""
from __future__ import annotations

import math

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union


def footprint(verts: np.ndarray, faces: np.ndarray, origin: tuple[float, float]):
    """Plan outline of a mesh: union of its triangles projected onto XY.

    Works relative to 'origin' so 7-digit coordinates do not lose precision.
    Returns a Polygon/MultiPolygon in absolute coordinates, or None.
    """
    if len(faces) == 0:
        return None
    xy = verts[:, :2] - np.asarray(origin)
    tri = xy[faces]                                     # (n, 3, 2)
    area = 0.5 * np.abs((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) -
                        (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1]))
    tri = tri[area > 1e-9]                              # drop vertical triangles (zero area in plan)
    if len(tri) == 0:
        return None
    polys = shapely.polygons(np.concatenate([tri, tri[:, :1]], axis=1))
    u = _union(polys)
    u = u.buffer(0.0005, join_style="mitre").buffer(-0.0005, join_style="mitre").simplify(0.001)  # closes 1 mm gaps
    if u.is_empty:
        return None
    return affinity.translate(u, *origin)


def _union(polys):
    """WEB COPY CHANGE 2: union with a fallback. The browser's shapely 2.0.7 / GEOS 3.12.1 can raise
    'TopologyException: Ring edge missing' on triangles that almost touch (the desktop's shapely 2.2 does not).
    Only then the union is repeated with snap-rounding to a 0.001 mm grid, then 0.1 mm; results that succeed at the
    first try are exactly the desktop ones."""
    try:
        return unary_union(polys)
    except shapely.errors.GEOSException:
        pass
    try:
        return shapely.union_all(polys, grid_size=1e-6)
    except shapely.errors.GEOSException:
        return shapely.union_all(polys, grid_size=1e-4)


def rings(g):
    """Rings (exterior and interior) of a polygon or multipolygon as lists of (x, y)."""
    if g is None or g.is_empty:
        return []
    polys = g.geoms if isinstance(g, MultiPolygon) else [g] if isinstance(g, Polygon) else \
        [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
    out = []
    for p in polys:
        for r in [p.exterior, *p.interiors]:
            c = list(r.coords)[:-1]
            if len(c) >= 3:
                out.append(c)
    return out


def length_2d(pl) -> float:
    return sum(math.dist(pl[i][:2], pl[i + 1][:2]) for i in range(len(pl) - 1))


def longest_segment(pl):
    segs = [(pl[i], pl[i + 1]) for i in range(len(pl) - 1)]
    return max(segs, key=lambda s: math.dist(s[0][:2], s[1][:2]))


def readable_angle(rad: float) -> float:
    """Angle in degrees between -90 and 90 so text is never upside down."""
    a = math.degrees(rad)
    while a > 90:
        a -= 180
    while a <= -90:
        a += 180
    return a
