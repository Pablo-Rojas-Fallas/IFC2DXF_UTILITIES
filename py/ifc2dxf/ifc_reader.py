"""IFC reading: opening, units, properties and geometry (axes and meshes)."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field

import ifcopenshell
import ifcopenshell.geom as geom
import ifcopenshell.ifcopenshell_wrapper as wrapper
import ifcopenshell.util.element as ue
import ifcopenshell.util.unit as uu
import numpy as np

# In a PyInstaller build, ifcopenshell would look for its geometry plugins next to its main library, which PyInstaller
# places outside the ifcopenshell folder ("No geometry mapping registered for ifc2x3"). Point it at the right folder.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    ifcopenshell.set_plugin_search_paths([os.path.join(sys._MEIPASS, "ifcopenshell")])

# Values that authoring tools use for "no data" (Novapoint: "IR [N/A]")
_NULLS = re.compile(r"^\s*(|-+|n/?a|n/?d|none|null|nan|ir\s*\[n/a\]|\[n/a\]|ikke relevant)\s*$", re.I)


def is_null(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:  # NaN
        return True
    return isinstance(v, str) and bool(_NULLS.match(v))


@dataclass
class Element:
    """An IFC product with a geometric representation and its flattened properties."""
    ent: ifcopenshell.entity_instance
    ifc_class: str
    name: str
    props: dict = field(default_factory=dict)   # {(pset, property): value}, nulls removed
    kind: str = ""                              # pipe | manhole | accessory | skipped | unclassified
    reason: str = ""                            # why it was classified that way
    system: str | None = None


class IfcModel:
    def __init__(self, path: str):
        # The file is opened read-only; ifcopenshell never changes it unless write() is called.
        self.path = path
        self.f = ifcopenshell.open(path)
        self.schema = self.f.schema
        self.scale = uu.calculate_unit_scale(self.f)  # project length unit -> metres
        fn = self.f.header.file_name
        self.origin = " | ".join(x for x in (fn.originating_system, fn.preprocessor_version) if x)
        self._elements: list[Element] | None = None

    def elements(self) -> list[Element]:
        """Products with geometry (excluding the spatial structure: project, site, building...)."""
        if self._elements is None:
            out = []
            for p in self.f.by_type("IfcProduct"):
                if p.is_a("IfcSpatialStructureElement") or p.is_a("IfcSpatialElement") or not p.Representation:
                    continue
                out.append(Element(p, p.is_a(), p.Name or "", _props(p)))
            self._elements = out
        return self._elements

    def by_type(self, t: str):
        try:
            return self.f.by_type(t)
        except RuntimeError:  # class does not exist in this schema (e.g. IfcMapConversion in IFC2X3)
            return []


def _props(p) -> dict:
    d = {}
    for pset, vals in ue.get_psets(p).items():
        for k, v in vals.items():
            if k == "id" or is_null(v):
                continue
            # names with stray whitespace (Novapoint: 'WSS-A03_Type_Type\t\t\t') are merged with the clean name;
            # if both exist, the clean one keeps its value
            key = (pset.strip(), k.strip())
            if key not in d or k == key[1]:
                d[key] = v.strip() if isinstance(v, str) else v
    return d


# --------------------------------------------------------------------------- geometry
GEOMETRY_RETRIES = 2   # single-element retries when the multi-thread pass returns no shape for an element

# TEST ONLY (off by default): simulate a missing 3D body for one element, to test the retry path.
#   IFC2DXF_TEST_DROP_SHAPE=<GlobalId>         the multi-thread pass "loses" it; the first retry recovers it
#   IFC2DXF_TEST_DROP_SHAPE=<GlobalId>:always  every retry fails as well (the axis fallback is used, if any)
TEST_DROP_ENV = "IFC2DXF_TEST_DROP_SHAPE"


def _test_drop() -> tuple[str | None, bool]:
    v = os.environ.get(TEST_DROP_ENV, "").strip()
    if not v:
        return None, False
    gid, _, mode = v.partition(":")
    return gid, mode == "always"


def global_id(elem: Element) -> str:
    return str(getattr(elem.ent, "GlobalId", None) or f"#{elem.ent.id()}")


def _mesh(sh):
    return (np.asarray(sh.geometry.verts, dtype=float).reshape(-1, 3),
            np.asarray(sh.geometry.faces, dtype=np.int64).reshape(-1, 3))


def _settings():
    s = geom.settings()
    s.set("use-world-coords", True)
    return s


def triangulate(model: IfcModel, elements: list[Element], progress=None, cancel=None) -> dict:
    """Triangulates only the requested elements, in project coordinates (metres).

    Returns {ifc_id: (vertices Nx3, faces Mx3)}. Web copy: one thread (see below). Elements without a shape, or with
    an empty one (no faces), are not in the result: the caller retries them with retry_shape().
    """
    if not elements:
        return {}
    s = _settings()
    threads = 1   # WEB COPY CHANGE 1: the browser (Pyodide) has no threads; desktop: max(1, (os.cpu_count() or 2) - 1)
    it = geom.iterator(s, model.f, threads, include=[e.ent for e in elements])
    drop_gid, _ = _test_drop()
    drop = {e.ent.id() for e in elements if drop_gid and global_id(e) == drop_gid}
    out = {}
    total = len(elements)
    n = 0
    if it.initialize():
        while True:
            sh = it.get()
            n += 1
            v, f = _mesh(sh)
            if len(f) and sh.id not in drop:
                out[sh.id] = (v, f)
            if progress and n % 25 == 0:
                progress(n / total)
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Cancelled by the user")
            if not it.next():
                break
    return out


def retry_shape(elem: Element, tries: int = GEOMETRY_RETRIES):
    """Retries ONE element alone (single thread, geom.create_shape, same settings as triangulate()).
    Returns ((vertices, faces), attempt number) on success, or (None, tries)."""
    s = _settings()
    drop_gid, always = _test_drop()
    for k in range(1, tries + 1):
        try:
            sh = geom.create_shape(s, elem.ent)
        except Exception:      # the geometry kernel raises RuntimeError for bodies it cannot process
            continue
        if always and global_id(elem) == drop_gid:
            continue
        v, f = _mesh(sh)
        if len(f):
            return (v, f), k
    return None, tries


_CURVES = None


def axes(elem: Element) -> list[list[tuple[float, float, float]]]:
    """Element axes as 3D polylines in metres (placement and units applied).

    Prefers the 'Axis' representation; otherwise uses curves inside 'Body'
    (Novapoint uses one or the other depending on the export version).
    """
    global _CURVES
    if _CURVES is None:
        _CURVES = geom.settings()
        _CURVES.set("use-world-coords", True)
        _CURVES.set("dimensionality", wrapper.CURVES)
    reps = [r for r in elem.ent.Representation.Representations
            if r.RepresentationType in ("Curve", "Curve2D", "Curve3D", "GeometricCurveSet", "GeometricSet")]
    pref = [r for r in reps if r.RepresentationIdentifier == "Axis"] or \
           [r for r in reps if r.RepresentationIdentifier in ("Body", "Reference", None)]
    out = []
    for rep in pref:
        try:
            sh = geom.create_shape(_CURVES, elem.ent, rep)
        except RuntimeError:
            continue
        g = getattr(sh, "geometry", sh)
        v = np.asarray(g.verts, dtype=float).reshape(-1, 3)
        e = np.asarray(g.edges, dtype=np.int64).reshape(-1, 2)
        out.extend(_chain(v, e))
    return out


def _chain(v: np.ndarray, edges: np.ndarray) -> list[list[tuple]]:
    """Joins loose edges into continuous polylines (vertices matched to 1 mm)."""
    if len(edges) == 0:
        return []
    key = lambda i: tuple(np.round(v[i], 3))
    remaining = [tuple(a) for a in edges]
    lines = []
    while remaining:
        a, b = remaining.pop(0)
        chain = [a, b]
        changed = True
        while changed:
            changed = False
            for k, (c, d) in enumerate(remaining):
                if key(c) == key(chain[-1]):
                    chain.append(d)
                elif key(d) == key(chain[-1]):
                    chain.append(c)
                elif key(d) == key(chain[0]):
                    chain.insert(0, c)
                elif key(c) == key(chain[0]):
                    chain.insert(0, d)
                else:
                    continue
                remaining.pop(k)
                changed = True
                break
        lines.append([tuple(map(float, v[i])) for i in chain])
    return lines


def swept_radius(elem: Element, scale: float) -> float | None:
    """Outer radius if the body is an IfcSweptDiskSolid (circular pipes), in metres."""
    for r in elem.ent.Representation.Representations:
        for it in r.Items:
            if it.is_a("IfcSweptDiskSolid"):
                return float(it.Radius) * scale
    return None
