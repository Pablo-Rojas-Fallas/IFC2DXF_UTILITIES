"""Georeferencing: detection, transformation to map coordinates and checks.

Decision order:
  1. IfcMapConversion (IFC4+)                          -> the Helmert transformation is applied.
  2. ePSet_MapConversion on IfcSite/IfcProject (IFC2X3) -> same.
  3. No conversion: model coordinates must already be global; we check that they fall inside the
     area of use of the coordinate system and cross-check against IfcSite.
  4. If nothing confirms it: warning and no georeference written to the DXF (never assumed).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
from pyproj import CRS, Transformer

DEFAULT_EPSG = 5105  # ETRS89/EUREF89 NTM zone 5


@dataclass
class Georef:
    method: str = "unknown"            # mapconversion | epset_mapconversion | global | local
    epsg: int | None = None
    crs_name: str = ""
    crs_source: str = ""               # where the coordinate system came from
    crs_declared: bool = False         # declared in the IFC itself
    confidence: str = "low"            # high | medium | low
    assured: bool = False              # if False, no georeference is written to the DXF
    params: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    details: list = field(default_factory=list)

    def transform(self, pts: np.ndarray) -> np.ndarray:
        """Project coordinates (m) -> map coordinates (m)."""
        if not self.params:
            return pts
        p = self.params
        x, y = pts[..., 0], pts[..., 1]
        out = pts.copy()
        out[..., 0] = p["s"] * (x * p["a"] - y * p["b"]) + p["E"]
        out[..., 1] = p["s"] * (x * p["b"] + y * p["a"]) + p["N"]
        out[..., 2] = pts[..., 2] * p["sz"] + p["H"]
        return out


def epsg_from_text(s: str) -> int | None:
    """'NTM5', 'EUREF89 NTM sone 5', 'EUREF89.NTM-5', 'UTM33', 'EPSG:25833' -> EPSG code."""
    if not s:
        return None
    s = str(s)
    m = re.search(r"epsg\D{0,3}(\d{4,5})", s, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"ntm\D{0,8}(\d{1,2})", s, re.I)
    if m and 5 <= int(m.group(1)) <= 30:
        return 5100 + int(m.group(1))       # EPSG 5105..5130 = NTM zones 5..30
    m = re.search(r"utm\D{0,8}(\d{1,2})", s, re.I)
    if m and 1 <= int(m.group(1)) <= 60:
        return 25800 + int(m.group(1))      # ETRS89 / UTM zone n
    return None


def _crs_bounds(epsg: int):
    """Area of use of the projected system, in its own coordinates (50 km margin)."""
    crs = CRS.from_epsg(epsg)
    a = crs.area_of_use
    t = Transformer.from_crs(4326, crs, always_xy=True)
    xmin, ymin, xmax, ymax = t.transform_bounds(a.west, a.south, a.east, a.north)
    return xmin - 5e4, ymin - 5e4, xmax + 5e4, ymax + 5e4


def _dms(v) -> float | None:
    """IfcCompoundPlaneAngleMeasure (degrees, minutes, seconds[, millionths]) -> decimal degrees."""
    if not v:
        return None
    v = list(v) + [0] * (4 - len(v))
    sg = -1 if any(x < 0 for x in v[:4]) else 1
    return sg * (abs(v[0]) + abs(v[1]) / 60 + abs(v[2]) / 3600 + abs(v[3]) / 3.6e9)


def _declared_crs(model, elems):
    """Declared coordinate system: IfcProjectedCRS, ePSet or 'Coordinate system'-like properties."""
    for c in model.by_type("IfcProjectedCRS"):
        for s in (c.Name, getattr(c, "Description", None), getattr(c, "MapProjection", None)):
            ep = epsg_from_text(s or "")
            if ep:
                return ep, f"IfcProjectedCRS '{c.Name}'"
    for e in elems[:500]:
        for (ps, k), v in e.props.items():
            if re.search(r"coord|koordinat|crs|epsg", k, re.I):
                ep = epsg_from_text(str(v))
                if ep:
                    return ep, f"property {ps}.{k} = '{v}'"
    return None, ""


def _epset(model, name):
    import ifcopenshell.util.element as ue
    for t in ("IfcSite", "IfcProject"):
        for o in model.by_type(t):
            ps = ue.get_pset(o, name)
            if ps:
                return ps
    return None


def resolve(model, elems, model_bbox: np.ndarray, epsg_hint: int | None = None) -> Georef:
    """Decides how to go from project to map coordinates. model_bbox = [[xmin,ymin,zmin],[xmax,ymax,zmax]] in m."""
    g = Georef()
    epsg, source = _declared_crs(model, elems)
    g.crs_declared = epsg is not None
    if epsg is None and epsg_hint:
        epsg, source = epsg_hint, "hint from the .json analysis (not declared in the IFC)"
    if epsg is None:
        epsg, source = DEFAULT_EPSG, "app default (not declared in the IFC)"
    try:
        crs = CRS.from_epsg(epsg)
        g.epsg, g.crs_name, g.crs_source = epsg, crs.name, source
    except Exception:
        g.warnings.append(f"EPSG:{epsg} is not a valid code; EPSG:{DEFAULT_EPSG} is used.")
        g.epsg, g.crs_name, g.crs_source = DEFAULT_EPSG, CRS.from_epsg(DEFAULT_EPSG).name, "default"
        g.crs_declared = False
    if epsg_hint and epsg_hint != g.epsg:
        g.warnings.append(f"The .json says EPSG:{epsg_hint} but the IFC declares EPSG:{g.epsg}; the IFC's is used.")

    # 1-2) Explicit map conversion
    mc = (model.by_type("IfcMapConversion") or [None])[0]
    if mc is not None:
        g.method = "mapconversion"
        a, b = (mc.XAxisAbscissa or 1.0), (mc.XAxisOrdinate or 0.0)
        s = mc.Scale or 1.0
        map_unit = _unit_scale(mc.TargetCRS.MapUnit) if getattr(mc.TargetCRS, "MapUnit", None) else model.scale
        E, N, H = mc.Eastings, mc.Northings, mc.OrthogonalHeight
    else:
        ps = _epset(model, "ePSet_MapConversion")
        if ps:
            g.method = "epset_mapconversion"
            a, b = float(ps.get("XAxisAbscissa") or 1.0), float(ps.get("XAxisOrdinate") or 0.0)
            s = float(ps.get("Scale") or 1.0)
            map_unit = model.scale
            E, N, H = float(ps.get("Eastings") or 0), float(ps.get("Northings") or 0), float(ps.get("OrthogonalHeight") or 0)
    if g.method in ("mapconversion", "epset_mapconversion"):
        n = math.hypot(a, b) or 1.0
        # Model coordinates are already in metres; the IFC scale works in project units,
        # so the effective metres->metres factor is Scale * map_unit / project_unit.
        s_eff = s * map_unit / model.scale
        g.params = dict(a=a / n, b=b / n, s=s_eff, sz=s_eff, E=E * map_unit, N=N * map_unit, H=H * map_unit)
        rot = math.degrees(math.atan2(b, a))
        g.details.append(f"{'IfcMapConversion' if g.method == 'mapconversion' else 'ePSet_MapConversion'}: "
                         f"E={E}, N={N}, H={H}, rotation={rot:.4f}°, scale={s}")
    else:
        g.method = "global"
        g.details.append("The IFC has no IfcMapConversion or ePSet_MapConversion: checking whether coordinates are already global.")

    # 3) Range check in the coordinate system
    bb = g.transform(np.asarray(model_bbox, dtype=float))
    xmin, ymin, xmax, ymax = _crs_bounds(g.epsg)
    in_range = xmin <= bb[0, 0] and bb[1, 0] <= xmax and ymin <= bb[0, 1] and bb[1, 1] <= ymax
    if not in_range:
        if g.method == "global":
            g.method = "local"
            g.warnings.append("Model coordinates are NOT inside the area of use of "
                              f"{g.crs_name} and the IFC has no map conversion: the DXF keeps the model's local "
                              "coordinates and is NOT marked as georeferenced.")
        else:
            g.warnings.append("The IFC map conversion gives coordinates outside the area of use of "
                              f"{g.crs_name}. Check the model's georeferencing.")
        g.confidence, g.assured = "low", False
        return g
    g.details.append(f"Drawing extent is inside the area of use of {g.crs_name}.")

    # Independent cross-check with IfcSite (lat/lon); swapped lat/lon is also tried
    site = _site_check(model, g.epsg, bb)
    if site:
        g.details.append(site[1])
        if site[2]:
            g.warnings.append(site[2])
    if g.crs_declared and ((site and site[0]) or g.method != "global"):
        g.confidence = "high"   # declared system + (IfcSite matches or explicit map conversion) + correct range
    elif g.crs_declared or (site and site[0]):
        g.confidence = "medium"
    else:
        g.confidence = "low"
        g.warnings.append(f"The coordinate system is not declared in the IFC; {g.crs_name} is assumed only because "
                          "the coordinates fall in its range. Confirm it before using the DXF.")
    g.assured = g.confidence in ("high", "medium")
    return g


def _site_check(model, epsg, bb):
    sites = model.by_type("IfcSite")
    if not sites or not sites[0].RefLatitude or not sites[0].RefLongitude:
        return None
    lat, lon = _dms(sites[0].RefLatitude), _dms(sites[0].RefLongitude)
    t = Transformer.from_crs(4326, epsg, always_xy=True)

    def dist(lo, la):
        x, y = t.transform(lo, la)
        if not (math.isfinite(x) and math.isfinite(y)):
            return float("inf")
        dx = max(bb[0, 0] - x, 0, x - bb[1, 0])
        dy = max(bb[0, 1] - y, 0, y - bb[1, 1])
        return math.hypot(dx, dy)

    d_ok, d_swap = dist(lon, lat), dist(lat, lon)
    if d_ok <= 5000:
        return True, f"IfcSite (lat {lat:.5f}, lon {lon:.5f}) matches the drawing extent ({d_ok:.0f} m).", None
    if d_swap <= 5000:
        return True, f"IfcSite matches the drawing extent ({d_swap:.0f} m) only with latitude and longitude swapped.", \
            "IfcSite has RefLatitude and RefLongitude swapped in the IFC (authoring tool error); it does not affect the DXF."
    return False, f"IfcSite (lat {lat:.5f}, lon {lon:.5f}) is {min(d_ok, d_swap) / 1000:.1f} km away from the drawing.", \
        "The IfcSite position does not match the geometry; georeferencing relies only on the coordinate range."


def _unit_scale(u) -> float:
    pref = {"MILLI": 1e-3, "CENTI": 1e-2, "DECI": 1e-1, "KILO": 1e3, None: 1.0}
    try:
        if u.is_a("IfcSIUnit"):
            return pref.get(u.Prefix, 1.0)
        if u.is_a("IfcConversionBasedUnit"):
            return float(u.ConversionFactor.ValueComponent.wrappedValue) * _unit_scale(u.ConversionFactor.UnitComponent)
    except Exception:
        pass
    return 1.0


# ----------------------------------------------------------------------------- CAD output
def wkt_prj(epsg: int) -> str:
    """WKT1 (ESRI .prj format), readable by Map 3D, QGIS, ArcGIS..."""
    return CRS.from_epsg(epsg).to_wkt(version="WKT1_ESRI")


def autodesk_cs_code(epsg: int) -> str | None:
    """Autodesk (CS-Map) coordinate system code to assign in AutoCAD/Civil 3D, e.g. 5105 -> 'EUREF89.NTM-5'.
    None when there is no safe equivalent (then the EPSG code is given instead)."""
    try:
        name = CRS.from_epsg(epsg).name
    except Exception:
        return None
    m = re.search(r"NTM zone (\d+)", name)
    u = re.search(r"ETRS89.*UTM zone (\d+)N", name)
    return f"EUREF89.NTM-{m.group(1)}" if m else (f"ETRS89.UTM-{u.group(1)}N" if u else None)


def autodesk_xml(epsg: int) -> str | None:
    """CS-Map definition (Autodesk XML) for the DXF GEOGRAPHICDATA object (experimental option only:
    AutoCAD tests showed that a DXF carrying this object is moved far to the south when the coordinate
    system is assigned, while the same DXF without it lands in the right place).

    Only for Transverse Mercator projections on ETRS89 (NTM and ETRS89-UTM), which is what can be
    built safely from the EPSG parameters.
    """
    crs = CRS.from_epsg(epsg)
    op = crs.coordinate_operation
    if op is None or "Transverse Mercator" not in op.method_name:
        return None
    p = {q.name: q.value for q in op.params}
    lat0 = p.get("Latitude of natural origin", 0.0)
    lon0 = p.get("Longitude of natural origin", 0.0)
    k0 = p.get("Scale factor at natural origin", 1.0)
    fe = p.get("False easting", 0.0)
    fn = p.get("False northing", 0.0)
    cs_id = autodesk_cs_code(epsg) or f"EPSG-{epsg}"
    return f'''<?xml version="1.0" encoding="UTF-16" standalone="no" ?>
<Dictionary version="1.0" xmlns="http://www.osgeo.org/mapguide/coordinatesystem">
<ProjectedCoordinateSystem id="{cs_id}">
<Name>{cs_id}</Name>
<Description>{crs.name}</Description>
<Authority>EPSG:{epsg}</Authority>
<DatumId>ETRS89</DatumId>
<Axis uom="Meter">
<CoordinateSystemAxis><AxisOrder>1</AxisOrder><AxisName>Easting</AxisName><AxisAbbreviation>E</AxisAbbreviation><AxisDirection>East</AxisDirection></CoordinateSystemAxis>
<CoordinateSystemAxis><AxisOrder>2</AxisOrder><AxisName>Northing</AxisName><AxisAbbreviation>N</AxisAbbreviation><AxisDirection>North</AxisDirection></CoordinateSystemAxis>
</Axis>
<Conversion>
<Projection>
<OperationMethodId>Transverse Mercator</OperationMethodId>
<ParameterValue><OperationParameterId>Latitude of natural origin</OperationParameterId><Value uom="degree">{lat0}</Value></ParameterValue>
<ParameterValue><OperationParameterId>Longitude of natural origin</OperationParameterId><Value uom="degree">{lon0}</Value></ParameterValue>
<ParameterValue><OperationParameterId>Scale factor at natural origin</OperationParameterId><Value uom="unity">{k0}</Value></ParameterValue>
<ParameterValue><OperationParameterId>False easting</OperationParameterId><Value uom="Meter">{fe}</Value></ParameterValue>
<ParameterValue><OperationParameterId>False northing</OperationParameterId><Value uom="Meter">{fn}</Value></ParameterValue>
</Projection>
</Conversion>
</ProjectedCoordinateSystem>
<Alias id="{epsg}" type="CoordinateSystem"><ObjectId>{cs_id}</ObjectId><Namespace>EPSG Code</Namespace></Alias>
<GeodeticDatum id="ETRS89"><Name>ETRS89</Name><PrimeMeridianId>Greenwich</PrimeMeridianId><EllipsoidId>GRS1980</EllipsoidId></GeodeticDatum>
<Alias id="6258" type="Datum"><ObjectId>ETRS89</ObjectId><Namespace>EPSG Code</Namespace></Alias>
<Ellipsoid id="GRS1980"><Name>GRS1980</Name><SemiMajorAxis uom="meter">6378137</SemiMajorAxis><SecondDefiningParameter><InverseFlattening uom="unity">298.257222101</InverseFlattening></SecondDefiningParameter></Ellipsoid>
<Alias id="7019" type="Ellipsoid"><ObjectId>GRS1980</ObjectId><Namespace>EPSG Code</Namespace></Alias>
</Dictionary>
'''
