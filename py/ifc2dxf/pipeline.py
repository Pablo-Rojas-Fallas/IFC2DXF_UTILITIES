"""IFC -> DXF conversion pipeline. Never raises towards the user interface: it returns a Result."""
from __future__ import annotations

import collections
import copy
import math
import os
import re
import statistics
import time
import traceback
from dataclasses import dataclass, field

import numpy as np
from ezdxf.enums import MTextEntityAlignment, TextEntityAlignment
from shapely.geometry import Point
from shapely.ops import unary_union

from . import analysis as an
from . import classify as cl
from . import georef as gr
from . import geometry as ge
from .dxf_writer import DATA_LAYER, UNCLASSIFIED_LAYER, DxfWriter
from .label_layout import LabelLayout
from .ifc_reader import GEOMETRY_RETRIES, IfcModel, axes, global_id, retry_shape, swept_radius, triangulate


@dataclass
class Options:
    axes_3d: bool = False            # False: 2D axes (Z=0) with levels as text; True: real elevation
    data_texts: bool = True          # data texts/attributes on the DATA layer
    manholes_as_blocks: bool = True  # True: block with attributes; False: outline + text
    layers_per_system: bool = True
    text_height: float = 0.5         # m (≈ 1:500 scale)
    overwrite: bool = False
    # Experimental: write the AutoCAD GEOGRAPHICDATA object. Off by default because AutoCAD moved drawings
    # carrying it far to the south when the coordinate system was assigned; the .prj is always written.
    write_geodata: bool = False
    # Move pipe labels and level texts to avoid overlaps, with leader lines where needed (ifc2dxf/label_layout.py).
    declutter: bool = True
    # Size factor for pipe labels and level texts only (manhole attributes keep text_height).
    data_text_scale: float = 1.0
    # Abbreviated materials in pipe labels (SHORT_MATERIALS). Pipe IDs already use the 'A→B' connection
    # form whenever the IFC has the connections (_pipe_ids).
    short_labels: bool = False


@dataclass
class Result:
    ok: bool = False
    error: str = ""
    dxf_path: str = ""
    extra_paths: list = field(default_factory=list)
    report: dict = field(default_factory=dict)


def convert(ifc_path: str, dxf_path: str, opt: Options, analysis=None, progress=None, cancel=None) -> Result:
    """Entry point. 'analysis' = result of analysis.read() (or None); 'progress(fraction, message)'."""
    res = Result(dxf_path=dxf_path)
    notify = progress or (lambda f, m: None)
    try:
        if not os.path.isfile(ifc_path):
            raise ValueError(f"The IFC file does not exist: {ifc_path}")
        if os.path.exists(dxf_path) and not opt.overwrite:
            raise FileExistsError(f"{dxf_path} already exists and overwriting was not allowed.")
        # copy: the same analysis can be reused for several conversions without accumulating notes
        _convert(ifc_path, dxf_path, opt, copy.deepcopy(analysis), notify, cancel, res)
        res.ok = True
    except (InterruptedError, FileExistsError, ValueError) as e:  # expected errors: message only
        res.error = str(e)
    except Exception as e:  # any other failure becomes a readable message
        res.error = f"{type(e).__name__}: {e}"
        res.report.setdefault("traceback", traceback.format_exc())
    return res


def _convert(ifc_path, dxf_path, opt, analysis, notify, cancel, res):
    t0 = time.time()
    rep = res.report
    rep.update(ifc=os.path.basename(ifc_path), dxf=os.path.basename(dxf_path), options=opt.__dict__.copy(),
               warnings=[], skipped=collections.Counter(), missing=collections.Counter())

    # ---------------------------------------------------------------- 1. reading
    notify(0.02, "Opening the IFC (this can take a while for large files)…")
    m = IfcModel(ifc_path)
    rep.update(schema=m.schema, origin=m.origin, unit_m=m.scale)
    elems = m.elements()
    notify(0.08, f"{len(elems)} elements with geometry. Classifying…")
    for e in elems:
        cl.classify_kind(e)
    rep["read"] = collections.Counter((e.kind, e.ifc_class) for e in elems)
    for e in elems:
        if e.kind == "skipped":
            rep["skipped"][e.reason] += 1
    rep["analysis"] = analysis
    by_kind = collections.defaultdict(list)
    for e in elems:
        by_kind[e.kind].append(e)
    try:
        forced, epsg_hint = an.apply_hints(analysis, by_kind)   # only hints verified against the IFC
    except Exception as ex:  # the analysis must never stop the conversion
        forced, epsg_hint = {}, None
        analysis.ignored.append(f"Internal error while applying the hints ({type(ex).__name__}: {ex}); automatic mode is used.")

    # ---------------------------------------------------------------- 2. systems
    sr = cl.detect_systems(m, elems)   # always: also used for the report and the .json cross-check
    if not opt.layers_per_system:
        rep["layers"] = "Single layer set: option 'Separate systems into layers' is off."
    elif sr.mode == "none":
        rep["layers"] = f"Single layer set: no reliable system was detected ({sr.reason})."
    per_system = opt.layers_per_system and sr.mode in ("ifcsystem", "property", "name")
    if per_system:
        rep["layers"] = (f"One layer set per system. Source: {sr.source} ({sr.reason}; coverage {sr.coverage:.0%}). "
                         f"Elements without a value go to {UNCLASSIFIED_LAYER}.")
    rep["system_source"] = sr

    # ---------------------------------------------------------------- 3. triangulation
    net = [e for e in elems if e.kind in ("pipe", "manhole", "accessory", "unclassified")]
    notify(0.10, f"Computing geometry of {len(net)} elements (3D texts are skipped)…")
    meshes = triangulate(m, net, progress=lambda f: notify(0.10 + 0.55 * f, "Computing geometry…"), cancel=cancel)
    axis_only = {}
    rep["missing_elements"] = []      # (what, element label): listed under MISSING DATA and shown in the app
    no_shape = [e for e in net if e.ent.id() not in meshes]
    if no_shape:
        notify(0.65, f"Retrying the geometry of {len(no_shape)} element(s) one by one…")
    for e in no_shape:
        who = f"{e.name or e.ifc_class} (GlobalId {global_id(e)})"
        mesh, k = retry_shape(e, GEOMETRY_RETRIES)   # the multi-thread pass can miss a shape now and then
        if mesh is not None:
            meshes[e.ent.id()] = mesh
            rep["missing_elements"].append((f"recovered on geometry retry {k} (complete in the DXF)", who))
            rep["warnings"].append(f"{who}: no 3D body in the first geometry pass; recovered on retry {k} "
                                   f"(drawn normally).")
            continue
        ls = axes(e)  # empty or unprocessable body, also after the retries: use the axis if there is one
        tried = f"no 3D body (also after {GEOMETRY_RETRIES} single-element retries)"
        if ls:
            axis_only[e.ent.id()] = ls
            if e.kind == "pipe":
                drawn = "axis, label and level texts" if opt.data_texts else "axis"
                rep["warnings"].append(f"{who}: {tried}; drawn from its axis only ({drawn} drawn, pipe outline "
                                       f"missing).")
                rep["missing_elements"].append(("pipe outline missing, drawn from its axis", who))
            else:
                rep["warnings"].append(f"{who}: {tried}; drawn from its axis only (outline missing).")
                rep["missing_elements"].append((f"{e.kind} outline missing, drawn from its axis", who))
        else:
            rep["skipped"][f"{e.ifc_class}: no processable geometry (neither body nor axis)"] += 1
            rep["warnings"].append(f"{who}: {tried} and no axis; NOT drawn in the DXF.")
            rep["missing_elements"].append((f"{e.kind} NOT drawn (no 3D body, no axis)", who))
    net = [e for e in net if e.ent.id() in meshes or e.ent.id() in axis_only]
    if not net:
        raise ValueError("The IFC contains no network elements with processable geometry.")

    # ---------------------------------------------------------------- 4. georeferencing
    notify(0.67, "Checking georeferencing…")
    allpts = np.concatenate([meshes[k][0] for k in meshes] + [np.asarray(l) for ls in axis_only.values() for l in ls])
    bbox = np.array([allpts.min(0), allpts.max(0)])
    g = gr.resolve(m, net, bbox, epsg_hint=epsg_hint)
    rep["georef"] = g
    rep["warnings"].extend(g.warnings)
    if g.params:  # map conversion: transform every mesh once
        meshes = {k: (g.transform(v), f) for k, (v, f) in meshes.items()}
        axis_only = {k: [g.transform(np.asarray(l)).tolist() for l in ls] for k, ls in axis_only.items()}
        allpts = g.transform(allpts)
    origin = (float(np.floor(allpts[:, 0].min())), float(np.floor(allpts[:, 1].min())))

    # ---------------------------------------------------------------- 5. data (properties)
    pipes = [e for e in net if e.kind == "pipe"]
    mhs = [e for e in net if e.kind == "manhole"]
    roles = {}
    for kind, group in (("pipe", pipes), ("manhole", mhs)):
        for role in ("diameter", "material", "min_level", "max_level", "id", "from", "to"):
            roles[(kind, role)] = cl.choose_property(group, role, forced.get((kind, role)))
    rep["roles"] = roles
    pipe_ids = _pipe_ids(pipes, roles[("pipe", "id")][0], roles[("pipe", "from")][0], roles[("pipe", "to")][0])
    rep["pipe_id_source"] = pipe_ids.pop("__source__")

    # ---------------------------------------------------------------- 6. writing
    W = DxfWriter(opt.text_height)
    LL = LabelLayout() if opt.declutter else None   # None: texts are written directly, as before
    hs = W.h * opt.data_text_scale                  # pipe label / level text height (= W.h at scale 1.0)

    def layer(e, suffix):
        if e.kind == "unclassified":
            return W.layer(UNCLASSIFIED_LAYER, 8)
        if not per_system:
            return W.layer(f"IFC_{suffix}", {"PIPES": 3, "AXES": 3, "MANHOLES": 5, "ACCESSORIES": 30}[suffix])
        if e.system is None:
            return W.layer(UNCLASSIFIED_LAYER, 8)
        return W.layer(f"{e.system}_{suffix}", W.colour_for(e.system), "DASHED" if suffix == "AXES" else None)

    conv = collections.Counter()
    per_sys = collections.defaultdict(collections.Counter)   # detected system -> kind -> n (for the cross-check)
    level_check = [0, 0]
    total = len(net)
    for i, e in enumerate(net):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Cancelled by the user")
        if i % 50 == 0:
            notify(0.70 + 0.25 * i / total, "Writing the DXF…")
        if e.kind == "manhole":
            continue  # grouped per manhole below
        if e.ent.id() in axis_only:
            for pl in axis_only[e.ent.id()]:
                W.axis(pl, layer(e, "AXES" if e.kind == "pipe" else "ACCESSORIES"), opt.axes_3d)
            if LL is not None and e.kind == "pipe":
                LL.add_pipe(e.ent.id(), None, axis_only[e.ent.id()])
            if e.kind == "pipe" and opt.data_texts:   # outline missing, but the label and level texts are still written
                _pipe_texts(W, e, axis_only[e.ent.id()], None, roles, pipe_ids.get(e.ent.id()), m.scale, level_check,
                            rep, hs, LL, opt.short_labels)
            conv[(e.system if per_system else "(single layer set)", e.kind)] += 1
            per_sys[e.system][e.kind] += 1
            continue
        v, f = meshes[e.ent.id()]
        fp = ge.footprint(v, f, origin)
        if e.kind == "pipe":
            W.outline(fp, layer(e, "PIPES"))
            lines = [g.transform(np.asarray(pl)).tolist() if g.params else pl for pl in axes(e)]
            if not lines:
                rep["missing"]["pipe without axis (outline only)"] += 1
            for pl in lines:
                W.axis(pl, layer(e, "AXES"), opt.axes_3d)
            if LL is not None:
                LL.add_pipe(e.ent.id(), fp, lines)
            if opt.data_texts and lines:
                _pipe_texts(W, e, lines, fp, roles, pipe_ids.get(e.ent.id()), m.scale, level_check, rep, hs, LL,
                            opt.short_labels)
        else:
            W.outline(fp, layer(e, "ACCESSORIES"))
        conv[(e.system if per_system else "(single layer set)", e.kind)] += 1
        per_sys[e.system][e.kind] += 1

    # manholes: parts grouped by identifier within each system
    mh_id = roles[("manhole", "id")][0]
    groups = collections.defaultdict(list)
    for e in mhs:
        key = e.props.get(mh_id) if mh_id else None
        groups[(e.system, str(key) if key is not None else f"#{e.ent.id()}")].append(e)
    if not mh_id and mhs:
        rep["missing"]["manholes without an identifier property (each part treated as a manhole)"] += len(mhs)
    notify(0.95, f"Writing {len(groups)} manholes…")
    for (sys_, ident), parts in groups.items():
        fps = [ge.footprint(*meshes[p.ent.id()], origin) for p in parts if p.ent.id() in meshes]
        fps = [h for h in fps if h is not None]
        axis_pts = np.array([q for p in parts for l in axis_only.get(p.ent.id(), []) for q in l]).reshape(-1, 3)
        zs = [float(meshes[p.ent.id()][0][:, 2].max()) for p in parts if p.ent.id() in meshes] + axis_pts[:, 2].tolist()
        if fps:
            union = unary_union(fps)
            c = union.centroid
            cx, cy, r = c.x, c.y, math.sqrt(union.area / math.pi)
        elif len(axis_pts):
            cx, cy, r = float(axis_pts[:, 0].mean()), float(axis_pts[:, 1].mean()), 0.5  # axis only: point
        else:
            rep["skipped"]["manhole without a plan footprint"] += 1
            continue
        tl = max(zs)
        il = _min_num(p.props.get(roles[("manhole", "min_level")][0]) for p in parts)
        diam = _mode(p.props.get(roles[("manhole", "diameter")][0]) for p in parts)
        mat = _mode(p.props.get(roles[("manhole", "material")][0]) for p in parts)
        if il is None:
            rep["missing"]["manhole without invert level (IL)"] += 1
        if diam is None:
            rep["missing"]["manhole without diameter"] += 1
        if mat is None:
            rep["missing"]["manhole without material"] += 1
        name = ident if not ident.startswith("#") else (parts[0].name or ident)
        lay = layer(parts[0], "MANHOLES")
        if LL is not None:   # manholes are fixed obstacles for the movable texts
            LL.add_fixed(unary_union(fps) if fps else Point(cx, cy).buffer(0.5))
        if opt.manholes_as_blocks:
            att = {"ID": name, "TL": f"TL {tl:.2f}", "IL": f"IL {il:.2f}" if il is not None else "",
                   "DIAMETER": _fmt_diam(diam), "MATERIAL": _short(mat), "SYSTEM": sys_ or ""} if opt.data_texts else {}
            W.insert_manhole((cx, cy), fps, lay, att, visible=("ID", "TL", "IL"))
        else:
            for h in fps:
                W.outline(h, lay)
            if not fps:
                W.msp.add_point((cx, cy), dxfattribs={"layer": lay})
            if opt.data_texts:
                lines = [name, f"TL {tl:.2f}"] + ([f"IL {il:.2f}"] if il is not None else []) + \
                        ([f"Ø{_fmt_diam(diam)}"] if diam is not None else [])
                W.mtext(lines, (cx + r + W.h * 0.6, cy + r + W.h * 0.6))
        conv[(sys_ if per_system else "(single layer set)", "manhole")] += 1
        per_sys[sys_]["manhole"] += 1

    if LL is not None:
        notify(0.96, "Placing labels (avoiding overlaps)…")
        rep["label_placement"] = LL.place_and_write(W, DATA_LAYER)

    if level_check[1]:
        rep["axis_level"] = (f"Axis level compared with the minimum-level property: matches (±2 cm) in "
                             f"{level_check[0]} of {level_check[1]} pipes.")

    # ---------------------------------------------------------------- 7. georeference in the DXF and saving
    ext2 = ((float(allpts[:, 0].min()), float(allpts[:, 1].min())), (float(allpts[:, 0].max()), float(allpts[:, 1].max())))
    lb = (rep.get("label_placement") or {}).get("bounds")
    if lb:   # moved texts and leader lines can lie outside the network: include them in the drawing extents
        ext2 = ((min(ext2[0][0], lb[0][0]), min(ext2[0][1], lb[0][1])), (max(ext2[1][0], lb[1][0]), max(ext2[1][1], lb[1][1])))
    rep["extent"] = (allpts.min(0).tolist(), allpts.max(0).tolist())
    base = os.path.splitext(dxf_path)[0]
    if g.assured:
        code = gr.autodesk_cs_code(g.epsg)
        lines = [f"The DXF coordinates are real {g.crs_name} (EPSG:{g.epsg}) map coordinates in metres.",
                 "In AutoCAD / Civil 3D, assign the coordinate system with Geolocation (GEOGRAPHICLOCATION) or "
                 "MAPCSASSIGN",
                 f"(Civil 3D: Drawing Settings > Units and Zone), code {code or f'matching EPSG:{g.epsg}'}, "
                 "without moving or scaling anything.",
                 f"The coordinate system is also written as {os.path.basename(base)}.prj next to the DXF."]
        xml = gr.autodesk_xml(g.epsg) if opt.write_geodata else None
        if xml:
            W.georeference(xml, ((ext2[0][0] + ext2[1][0]) / 2, (ext2[0][1] + ext2[1][1]) / 2))
            lines.append("EXPERIMENTAL: a GEOGRAPHICDATA object was written to the DXF (option on). AutoCAD may "
                         "misplace the drawing with it; if so, convert again without this option.")
        elif opt.write_geodata:
            lines.append("Projection not supported for the experimental GEOGRAPHICDATA object; none was written.")
        else:
            lines.append("No GEOGRAPHICDATA object is written (default): AutoCAD misplaced drawings that carried it.")
        rep["geodata"] = "\n".join(lines)
        with open(base + ".prj", "w", encoding="utf-8") as fh:
            fh.write(gr.wkt_prj(g.epsg))
        res.extra_paths.append(base + ".prj")
    else:
        rep["geodata"] = "NO georeference written to the DXF because it could not be confirmed from the IFC data."
    notify(0.98, "Saving the DXF…")
    W.save(dxf_path, ext2)
    rep["converted"] = conv
    rep["dxf_layers"] = sorted(l.dxf.name for l in W.doc.layers)
    rep["colours"] = W.colours
    rep["n_entities"] = len(W.msp)
    rep["seconds"] = round(time.time() - t0, 1)

    # cross-check of the .json against what was found in the IFC (the IFC wins)
    mc = (m.by_type("IfcMapConversion") or [None])[0]
    rep["facts"] = dict(
        ifc=os.path.basename(ifc_path), schema=m.schema, scale=m.scale, georef=g, extent=rep["extent"],
        has_mapconversion=mc is not None,
        mapconversion=dict(E=mc.Eastings, N=mc.Northings) if mc is not None else None,
        systems={k: v for k, v in per_sys.items() if k is not None},
        classes=set().union(*(_ancestors(e.ent) for e in {e.ifc_class: e for e in elems}.values())),
        by_kind=by_kind)
    if analysis is not None and analysis.status != "not_selected":
        try:
            an.cross_check(analysis, rep["facts"])
        except Exception as ex:
            analysis.ignored.append(f"Internal error while cross-checking with the IFC ({type(ex).__name__}: {ex}).")
        reading_path = base + "_json_reading.txt"
        with open(reading_path, "w", encoding="utf-8") as fh:
            fh.write(an.reading_text(analysis))
        res.extra_paths.append(reading_path)

    from .report import report_text
    report_path = base + "_report.txt"
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_text(rep))
    res.extra_paths.append(report_path)
    notify(1.0, "Done.")


# ------------------------------------------------------------------------------------- helpers
def _pipe_ids(pipes, prop, frm, to):
    """Pipe identifier: Tag, an id property, or the most distinctive fragment of the name (e.g. the
    Novapoint line), replaced by 'from→to' when the IFC has the pipe connections."""
    out, source = {}, ""
    if pipes and sum(bool(e.ent.Tag) for e in pipes) / len(pipes) >= 0.8:
        out, source = {e.ent.id(): e.ent.Tag for e in pipes if e.ent.Tag}, "IFC Tag attribute"
    elif prop:
        out, source = {e.ent.id(): str(e.props[prop]) for e in pipes if prop in e.props}, f"property {prop[0]}.{prop[1]}"
    else:
        chunks = [[t.strip() for t in re.split(r"[,;|]", e.name)] for e in pipes]
        best, uniq = None, 0.0
        for i in range(max((len(t) for t in chunks), default=0)):
            vals = [t[i] for t in chunks if len(t) > i and t[i]]
            u = len(set(vals)) / len(pipes) if pipes else 0
            if u > uniq:
                best, uniq = i, u
        if best is not None and uniq >= 0.3:
            out = {e.ent.id(): t[best] for e, t in zip(pipes, chunks) if len(t) > best}
            source = (f"name fragment {best + 1} ({len(set(out.values()))} values for {len(pipes)} pipes: "
                      "several pipes can share a line)")
        else:
            source = "not found (no Tag, no id property and names not distinctive)"
    if frm or to:
        last = lambda v: re.split(r"\s*,\s*", str(v))[-1] if v is not None else "?"
        n = 0
        for e in pipes:
            a, b = e.props.get(frm), e.props.get(to)
            if a is None and b is None:
                continue
            n += 1
            out[e.ent.id()] = f"{last(a)}→{last(b)}"   # shorter and more useful in plan than the line name
        source = (f"from→to connections ({frm[1] if frm else '?'} / {to[1] if to else '?'}) on {n} pipes; "
                  f"for the rest: {source}")
    out["__source__"] = source
    return out


def _pipe_texts(W, e, lines, fp, roles, ident, scale, check, rep, hs=None, LL=None, short=False):
    """Pipe label + invert levels. hs = their text height (default W.h); LL = LabelLayout to defer them to;
    short = abbreviated materials (SHORT_MATERIALS)."""
    hs = W.h if hs is None else hs
    pl = max(lines, key=ge.length_2d)
    (x1, y1, z1), (x2, y2, z2) = ge.longest_segment(pl)
    ang = math.atan2(y2 - y1, x2 - x1)
    ra = ge.readable_angle(ang)
    nx, ny = -math.sin(math.radians(ra)), math.cos(math.radians(ra))
    length = ge.length_2d(pl)
    # outer diameter: from the swept solid or, for a Brep, plan area / length
    r = swept_radius(e, scale)
    od = f"{round(r * 2000)}" if r else (f"≈{round(fp.area / length * 1000)}" if fp is not None and length > 0.05 else None)
    inner = _num(e.props.get(roles[("pipe", "diameter")][0]))
    mat = e.props.get(roles[("pipe", "material")][0])
    if inner is None:
        rep["missing"]["pipe without inner diameter (property)"] += 1
    if mat is None:
        rep["missing"]["pipe without material"] += 1
    if ident is None:
        rep["missing"]["pipe without identifier"] += 1
    za, zb = pl[0][2], pl[-1][2]
    slope = abs(zb - za) / length * 1000 if length > 0.05 else None
    mat_txt = _short(mat)
    if short:
        mat_txt = SHORT_MATERIALS.get(mat_txt.lower(), mat_txt)
    l2 = " ".join(x for x in (f"Ø{od}" if od else "", f"ID{_fmt_diam(inner)}" if inner is not None else "",
                              mat_txt, f"i={slope:.1f}‰" if slope is not None else "") if x)
    off = (_num(od) or 200) / 2000 + hs * 0.4
    label = [x for x in (ident, l2) if x]
    label_pos = ((x1 + x2) / 2 + nx * off, (y1 + y2) / 2 + ny * off)
    if LL is None:
        W.mtext(label, label_pos, ra, attach=MTextEntityAlignment.BOTTOM_CENTER, h=hs)
    else:
        LL.pipe_label(label, label_pos, ra, hs, e.ent.id(), pl, off)
    # invert levels at both ends
    ux, uy = math.cos(ang), math.sin(ang)
    for (x, y, z), sg in ((pl[0], 1), (pl[-1], -1)):
        pos = (x + sg * ux * hs * 2.5 - nx * off, y + sg * uy * hs * 2.5 - ny * off)
        if LL is None:
            W.text(f"{z:.2f}", pos, ra, hs * 0.7, align=TextEntityAlignment.TOP_CENTER)
        else:
            LL.level_text(f"{z:.2f}", pos, ra, hs * 0.7, e.ent.id(), (x, y), (sg * ux, sg * uy), (nx, ny), off, hs,
                          pl)
    lmin = _num(e.props.get(roles[("pipe", "min_level")][0]))
    if lmin is not None:
        check[1] += 1
        check[0] += abs(min(za, zb) - lmin) <= 0.02


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    mm = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(mm.group(0).replace(",", ".")) if mm else None


def _min_num(vals):
    ns = [n for n in (_num(v) for v in vals) if n is not None]
    return min(ns) if ns else None


def _mode(vals):
    vs = [v for v in vals if v is not None]
    return statistics.mode(vs) if vs else None


def _fmt_diam(d):
    """Diameter in mm without decimals; values < 5 are read as metres."""
    n = _num(d)
    if n is None:
        return ""
    return f"{n * 1000:.0f}" if n < 5 else f"{n:.0f}"


# Material abbreviations for Options.short_labels (keys in lower case; materials not listed stay unchanged)
SHORT_MATERIALS = {
    "polyethylene": "PE",
    "polyvinyl chloride": "PVC",
    "ductile cast iron": "DI",
    "double-walled storm water pipes": "DW",
}


def _short(s):
    """Material text: the English translation in brackets if present (Novapoint: 'Betong [Concrete]' -> 'Concrete')."""
    if s is None:
        return ""
    m = re.search(r"\[([^\]]+)\]", str(s))
    return (m.group(1) if m else str(s)).strip()


def _ancestors(ent) -> set:
    """Parent classes of an entity (IfcFlowSegment -> IfcDistributionFlowElement...), to validate .json classes."""
    import ifcopenshell.ifcopenshell_wrapper as w
    out, d = set(), w.schema_by_name(ent.is_a(True).split(".")[0]).declaration_by_name(ent.is_a())
    while d is not None:
        out.add(d.name())
        d = d.supertype()
    return out
