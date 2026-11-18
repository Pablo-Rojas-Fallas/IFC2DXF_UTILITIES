"""Compact IFC summary for the analysis prompt ('Copy prompt' button).

The full IFC does not fit in a chat (tens of MB); a summary of a few KB is sent with what the AI needs to
fill in the schema. Values of properties that hold personal data or URLs are hidden.
"""
from __future__ import annotations

import collections
import os
import re

import numpy as np

from .classify import words
from .ifc_reader import IfcModel, axes

# Properties whose VALUES are not sent (people, owners, links)
_PERSONAL = {"by", "av", "owner", "eier", "bestiller", "approved", "godkjent", "designed", "utarbeidet", "reviewed",
             "kontrollert", "author", "autor", "person", "email", "mail", "phone", "telefon", "url", "link", "uri",
             "produsert", "produced", "ordered", "client", "cliente", "responsable", "responsible"}
_MAX_PROPS_PER_CLASS = 70
_MAX_EXAMPLES = 3
_MAX_ALL_VALUES = 12   # properties with up to this many distinct values are listed in full


def _hidden(prop_name: str) -> bool:
    return bool(words(prop_name) & _PERSONAL)


def _txt(v, n=45) -> str:
    s = " ".join(str(v).split())
    return s if len(s) <= n else s[:n] + "…"


def _pattern(name: str) -> str:
    return re.sub(r"\d+", "#", name or "")[:70]


def generate(path: str, model: IfcModel | None = None, progress=None) -> str:
    m = model or IfcModel(path)
    f = m.f
    L = []
    w = L.append
    hdr = f.header
    w("=== IFC SUMMARY (generated offline by the IFC2DXF app; the full model is not attached) ===")
    w(f"file: {os.path.basename(path)}  |  size: {os.path.getsize(path) / 1e6:.1f} MB  |  schema: {m.schema}")
    w(f"authoring tool (header): {m.origin or 'not stated'}")
    try:
        w(f"view/description: {_txt(' '.join(hdr.file_description.description), 120)}")
    except Exception:
        pass
    units = []
    for u in m.by_type("IfcSIUnit"):
        if u.UnitType in ("LENGTHUNIT", "PLANEANGLEUNIT"):
            units.append(f"{u.UnitType}={(u.Prefix or '')}{u.Name}")
    w(f"declared units: {', '.join(units) or 'none declared'}  |  length → metres factor: {m.scale}")

    # ---------------------------------------------------------------- georeferencing
    w("\n--- GEOREFERENCING ---")
    mc = m.by_type("IfcMapConversion")
    if mc:
        c = mc[0]
        w(f"IfcMapConversion: Eastings={c.Eastings}, Northings={c.Northings}, OrthogonalHeight={c.OrthogonalHeight}, "
          f"XAxisAbscissa={c.XAxisAbscissa}, XAxisOrdinate={c.XAxisOrdinate}, Scale={c.Scale}")
    else:
        w("IfcMapConversion: does NOT exist" + (" (IFC2X3 has no such entity)" if m.schema == "IFC2X3" else ""))
    for c in m.by_type("IfcProjectedCRS"):
        w(f"IfcProjectedCRS: Name={c.Name}, Description={c.Description}, GeodeticDatum={c.GeodeticDatum}, "
          f"MapProjection={getattr(c, 'MapProjection', None)}, MapZone={getattr(c, 'MapZone', None)}")
    import ifcopenshell.util.element as ue
    for t in ("IfcSite", "IfcProject"):
        for o in m.by_type(t):
            for ps in ("ePSet_MapConversion", "ePSet_ProjectedCRS"):
                d = ue.get_pset(o, ps)
                if d:
                    w(f"{t}.{ps}: " + ", ".join(f"{k}={v}" for k, v in d.items() if k != "id"))
    for s in m.by_type("IfcSite"):
        w(f"IfcSite: RefLatitude={s.RefLatitude}, RefLongitude={s.RefLongitude}, RefElevation={s.RefElevation} "
          "(format degrees, minutes, seconds[, millionths])")
    for c in m.by_type("IfcGeometricRepresentationContext"):
        if not c.is_a("IfcGeometricRepresentationSubContext"):
            tn = c.TrueNorth.DirectionRatios if c.TrueNorth else None
            w(f"Context {c.ContextType}: WCS origin={c.WorldCoordinateSystem.Location.Coordinates}, TrueNorth={tn}")

    elems = m.elements()
    if progress:
        progress(0.3, "Summarising properties…")
    # properties about coordinate or height systems
    seen = collections.Counter()
    for e in elems:
        for (ps, k), v in e.props.items():
            if re.search(r"coord|koordinat|crs|epsg|h[oø]e?ydesystem|vertical|datum|ntm|utm", k, re.I):
                seen[(ps, k, _txt(v))] += 1
    for (ps, k, v), n in seen.most_common(8):
        w(f"property {ps}.{k} = '{v}'  (on {n} elements)")

    # coordinate range: segment axes (fast), in metres, model coordinates
    pts = []
    segs = [e for e in elems if e.ent.is_a("IfcFlowSegment") or e.ent.is_a("IfcPipeSegment") or e.ent.is_a("IfcCableSegment")]
    for e in segs[:400]:
        for pl in axes(e):
            pts.extend(pl)
    if not pts:  # no axes: placement origins
        import ifcopenshell.util.placement as up
        for e in elems[:2000]:
            if e.ent.ObjectPlacement:
                mat = up.get_local_placement(e.ent.ObjectPlacement)
                pts.append(tuple(np.asarray(mat)[:3, 3] * m.scale))
    if pts:
        a = np.asarray(pts, dtype=float)
        w(f"model coordinate range in metres ({'segment axes' if segs else 'placement origins'}, "
          f"{len(a)} points): X {a[:, 0].min():.2f}…{a[:, 0].max():.2f}  Y {a[:, 1].min():.2f}…{a[:, 1].max():.2f}  "
          f"Z {a[:, 2].min():.2f}…{a[:, 2].max():.2f}")
    else:
        w("coordinate range: could not be computed")

    # ---------------------------------------------------------------- groups
    w("\n--- GROUPS (IfcSystem / IfcGroup) ---")
    groups = []
    for rel in m.by_type("IfcRelAssignsToGroup"):
        g = rel.RelatingGroup
        groups.append(f"{g.is_a()} '{g.Name}' ({len(rel.RelatedObjects or [])} elements)")
    w("; ".join(groups[:30]) if groups else "none (no IfcSystem or IfcGroup with assigned elements)")

    # ---------------------------------------------------------------- classes
    w("\n--- CLASSES WITH GEOMETRY (n, representation types) ---")
    by_class = collections.defaultdict(list)
    for e in elems:
        by_class[e.ifc_class].append(e)
    for cls, es in sorted(by_class.items(), key=lambda x: -len(x[1])):
        reps = collections.Counter(f"{r.RepresentationIdentifier}:{r.RepresentationType}"
                                   for e in es[:300] for r in e.ent.Representation.Representations)
        w(f"{cls}: {len(es)}  |  representations: {', '.join(f'{k}×{v}' for k, v in reps.most_common(6))}")
        pats = collections.Counter(_pattern(e.name) for e in es)
        w("   Name patterns (digits=#): " + " ; ".join(f"«{p}»×{n}" for p, n in pats.most_common(8)))
        examples = list(dict.fromkeys(e.name for e in es if e.name))[:4]
        w("   Name examples: " + " ; ".join(f"«{_txt(x, 80)}»" for x in examples))

    # ---------------------------------------------------------------- properties
    if progress:
        progress(0.6, "Summarising properties…")
    w("\n--- PROPERTIES ---")
    w("Format: Pset.Property | coverage % (elements of the class with a non-null value) | no. of distinct values | examples")
    w("Null values from the source (e.g. 'IR [N/A]') already excluded. [hidden] = value not sent, for privacy.")
    total = collections.Counter()
    values = collections.defaultdict(set)
    for e in elems:
        for k, v in e.props.items():
            total[k] += 1
            if len(values[k]) < 50:
                values[k].add(_txt(v))
    common = {k for k in total if len(values[k]) == 1 and total[k] >= 0.9 * len(elems)}
    if common:
        w("Common to almost the whole model with a single value:")
        for k in sorted(common):
            v = "[hidden]" if _hidden(k[1]) else next(iter(values[k]))
            w(f"   {k[0]}.{k[1]} = '{v}'")
    for cls, es in sorted(by_class.items(), key=lambda x: -len(x[1])):
        cnt = collections.Counter()
        vals = collections.defaultdict(collections.Counter)
        for e in es:
            for k, v in e.props.items():
                if k in common:
                    continue
                cnt[k] += 1
                vals[k][_txt(v)] += 1
        if not cnt:
            continue
        w(f"[{cls}] ({len(es)} elements)")
        for k, n in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[:_MAX_PROPS_PER_CLASS]:
            dist = len(vals[k])
            if _hidden(k[1]):
                ex = "[hidden]"
            elif dist <= _MAX_ALL_VALUES:   # few values (e.g. a network type): list them ALL, with counts
                ex = "ALL VALUES: " + " | ".join(f"'{v}'×{c}" for v, c in vals[k].most_common())
            else:
                ex = " | ".join(f"'{v}'" for v, _ in vals[k].most_common(_MAX_EXAMPLES))
            w(f"   {k[0]}.{k[1]} | {100 * n / len(es):.0f}% | {dist}{'+' if dist >= 50 else ''} | {ex}")
        if len(cnt) > _MAX_PROPS_PER_CLASS:
            w(f"   … and {len(cnt) - _MAX_PROPS_PER_CLASS} more properties with lower coverage")
    w("=== END OF SUMMARY ===")
    return "\n".join(L)
