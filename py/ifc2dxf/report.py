"""Plain-text conversion report (shown in the app and saved next to the DXF)."""
from __future__ import annotations

import collections
import datetime

from . import __version__

_KINDS = {"pipe": "pipes", "manhole": "manholes", "accessory": "accessories",
          "unclassified": "unclassified", "skipped": "skipped"}


def report_text(rep: dict) -> str:
    L = []
    w = L.append
    w(f"IFC → DXF CONVERSION REPORT   (IFC2DXF {__version__}, {datetime.datetime.now():%Y-%m-%d %H:%M})")
    w("=" * 78)
    w(f"IFC:   {rep.get('ifc')}   [{rep.get('schema')}]  authoring tool: {rep.get('origin') or 'unknown'}")
    w(f"DXF:   {rep.get('dxf')}   ({rep.get('n_entities', 0)} entities, {rep.get('seconds', '?')} s)")
    w(f"IFC length unit: {rep.get('unit_m')} m per unit")
    o = rep.get("options", {})
    w(f"Options: {'3D axes with real elevation' if o.get('axes_3d') else '2D axes (Z = 0)'} | data texts: "
      f"{'yes' if o.get('data_texts') else 'no'} | manholes as {'blocks' if o.get('manholes_as_blocks') else 'outlines'} | "
      f"separate systems into layers: {'yes' if o.get('layers_per_system') else 'no'}")

    an = rep.get("analysis")
    w("")
    if an is None or an.status == "not_selected":
        w("JSON ANALYSIS: none loaded; layers and data detected in automatic mode.")
    else:
        st = {"ok": "read correctly", "repaired": "read with repairs", "empty": "empty → automatic mode",
              "unrecoverable": "unrecoverable → automatic mode"}[an.status]
        w(f"JSON ANALYSIS: {st}. Repairs: {len(an.repaired)}, discarded: {len(an.ignored)}, "
          f"discrepancies with the IFC: {len(an.discrepancies)} (details in *_json_reading.txt).")
        for x in an.understood:
            if x.startswith("Applied"):
                w(f"  {x}")
    if rep.get("warnings"):
        w("\nWARNINGS")
        w("-" * 78)
        for a in dict.fromkeys(rep["warnings"]):
            w(f"  ! {a}")

    g = rep.get("georef")
    if g:
        w("\nGEOREFERENCING")
        w("-" * 78)
        w(f"  Method: {g.method}   System: {g.crs_name} (EPSG:{g.epsg})   Confidence: {g.confidence}")
        w(f"  System taken from: {g.crs_source}")
        for d in g.details:
            w(f"  - {d}")
        for line in rep.get("geodata", "").splitlines():
            w(f"  {line}")
    if rep.get("extent"):
        (x0, y0, z0), (x1, y1, z1) = rep["extent"]
        w(f"  Drawing extent:  X {x0:,.2f} … {x1:,.2f}   Y {y0:,.2f} … {y1:,.2f}   Z {z0:,.2f} … {z1:,.2f}")

    lp = rep.get("label_placement")
    if lp:
        w("\nLABEL PLACEMENT")
        w("-" * 78)
        w(f"  Pipe labels and level texts placed: {lp['n']} ({lp['n_pipe']} pipe labels, {lp['n_level']} level "
          f"texts); data text size factor {o.get('data_text_scale', 1.0)}")
        w(f"  Kept in place: {lp['stayed']}   moved: {lp['moved']}   moved with a leader line: {lp['leader']}   "
          f"not resolved: {lp['unresolved']}")
        if lp.get("skipped"):
            w(f"  Label placement stopped after {lp['budget']:.0f} s (time limit): {lp['skipped']} texts not processed. "
              f"They were left at their original position and may overlap; try a smaller data text size or short "
              f"pipe labels.")
        b, a = lp["before"], lp["after"]
        w("  Overlaps (text sizes are estimated)          before -> after")
        w(f"    texts overlapping other texts (pairs)    {b['text_text']:>6} -> {a['text_text']}")
        w(f"    texts lying over another pipe            {b['over_pipes']:>6} -> {a['over_pipes']}")
        w(f"    texts lying over a manhole symbol        {b['over_manholes']:>6} -> {a['over_manholes']}")
        if lp["leaders"]:
            w(f"  Leader lines: {lp['leaders']} (shortest {lp['leader_min']} m, median {lp['leader_median']} m)")
        else:
            w("  Leader lines: 0")
        if lp["unresolved_list"]:
            w("  Not resolved - no free spot within 10 m; placed where they overlap least. "
              "Check these by hand in AutoCAD:")
            for txt, x, y, area in lp["unresolved_list"]:
                w(f"    X {x:,.2f}  Y {y:,.2f}   " + (f"overlap {area:.3f} m²   " if area is not None else "") + txt)
        w(f"  Manhole blocks and their attributes are never moved. Time: {lp['seconds']} s.")
    elif o.get("data_texts", True):
        w("\nLabel placement: off")

    w("\nLAYERS")
    w("-" * 78)
    w(f"  {rep.get('layers', '')}")
    sr = rep.get("system_source")
    if sr is not None and getattr(sr, "candidates", None):
        w("  Candidate properties evaluated (source, coverage, no. of values):")
        for c in sr.candidates:
            w(f"    · {c[0]}  {c[1]:.0%}  {c[2]}")
    w(f"  Layers created: {', '.join(rep.get('dxf_layers', []))}")
    if rep.get("colours"):
        w("  Colour per system (ACI index): " + ", ".join(f"{s} = {c}" for s, c in rep["colours"].items()))

    w("\nELEMENTS READ (by kind and IFC class)")
    w("-" * 78)
    for (kind, cls), n in sorted(rep.get("read", {}).items()):
        w(f"  {_KINDS.get(kind, kind):15s} {cls:32s} {n:6d}")
    w("\nELEMENTS CONVERTED (by system and kind; a manhole counts once even if it has several parts)")
    w("-" * 78)
    per_sys = collections.defaultdict(dict)
    for (sys_, kind), n in rep.get("converted", {}).items():
        per_sys[sys_ or "UNCLASSIFIED"][kind] = n
    for sys_ in sorted(per_sys):
        d = per_sys[sys_]
        w(f"  {sys_:34s} " + "  ".join(f"{_KINDS.get(t, t)}: {n}" for t, n in sorted(d.items())))

    w("\nDATA (property used for each data item, and coverage)")
    w("-" * 78)
    for (kind, role), (prop, cov) in sorted(rep.get("roles", {}).items()):
        w(f"  {kind:8s} {role:10s} " + (f"{prop[0]}.{prop[1]}  ({cov:.0%})" if prop else "— not found"))
    w(f"  Pipe identifier: {rep.get('pipe_id_source', '')}")
    if rep.get("axis_level"):
        w(f"  {rep['axis_level']}")
    w("  Ø = outer diameter from the pipe geometry (Ø≈ when estimated from the plan of a Brep solid); "
      "ID = inner diameter from the property. TL = top level, IL = invert level.")

    w("\nSKIPPED ELEMENTS")
    w("-" * 78)
    if rep.get("skipped"):
        for k, n in rep["skipped"].most_common():
            w(f"  {n:6d}  {k}")
    else:
        w("  none")
    w("\nMISSING DATA")
    w("-" * 78)
    if rep.get("missing"):
        for k, n in rep["missing"].most_common():
            w(f"  {n:6d}  {k}")
    elif not rep.get("missing_elements"):
        w("  none")
    if rep.get("missing_elements"):
        w("  Elements whose 3D body was missing from the geometry step:")
        for what, who in rep["missing_elements"]:
            w(f"    - {who}: {what}")
    if rep.get("traceback"):
        w("\nTECHNICAL ERROR DETAILS")
        w(rep["traceback"])
    return "\n".join(L) + "\n"
