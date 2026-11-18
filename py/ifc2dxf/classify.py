"""Automatic classification: element kind, systems, data properties and layer names."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .ifc_reader import Element

# ----------------------------------------------------------------------------- element kinds
PIPE_CLASSES = ("IfcPipeSegment", "IfcCableSegment", "IfcCableCarrierSegment", "IfcDuctSegment", "IfcFlowSegment")
MANHOLE_CLASSES = ("IfcDistributionChamberElement",)
ACCESSORY_CLASSES = ("IfcFlowFitting", "IfcFlowController", "IfcFlowTerminal", "IfcFlowMovingDevice",
                     "IfcFlowStorageDevice", "IfcFlowTreatmentDevice", "IfcEnergyConversionDevice",
                     "IfcDistributionControlElement", "IfcJunctionBox", "IfcDiscreteAccessory")

# Hints in names/properties (several languages: Norwegian, English, Spanish, German)
_RE_MANHOLE = re.compile(r"manhole|kum(?!lokk)|chamber|pozo|arqueta|schacht|trekkekum|sandfang|inspection", re.I)
_RE_SLEEVE = re.compile(r"varer[øo]r|isolasjon|insulation|casing|sleeve|camisa|aislamiento|\bxps\b|\beps\b|\bvr\b", re.I)
_RE_TEXT = re.compile(r"text3d|^text\b|annotation|label3d", re.I)
_MANHOLE_PREFIXES = ("manhole", "kum", "chamber", "pozo", "arqueta", "schacht")  # whole words: 'Dokumentkode' is not 'kum'
_NOT_MANHOLE = {"kummer"}  # 'Andre-kummer' appears in object codes of elements that are not manholes


def norm(s: str) -> str:
    """Lower case, no accents, unified separators: for comparing names."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[\s\-\.]+", "_", s.lower()).strip("_")


def words(name: str) -> set[str]:
    """'PipeNetworkUsage' -> {pipe, network, usage}; 'WSS-A03_Type_Type' -> {wss, a03, type}."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    return {w for w in re.split(r"[^a-z0-9]+", norm(s).replace("_", " ")) if w}


def classify_kind(e: Element) -> None:
    """Sets e.kind and e.reason. The IFC class comes first; properties and name refine it."""
    text = " ".join([e.name, str(getattr(e.ent, "ObjectType", "") or "")] +
                    [str(v) for (ps, k), v in e.props.items() if "abbr" in k.lower() or "type" in k.lower()])
    if e.ent.is_a("IfcAnnotation") or _RE_TEXT.search(e.name):
        e.kind, e.reason = "skipped", "3D text / annotation from the authoring tool"
    elif any(e.ent.is_a(c) for c in PIPE_CLASSES):
        if _RE_SLEEVE.search(text):
            e.kind, e.reason = "accessory", f"{e.ifc_class} identified as casing or insulation"
        else:
            e.kind, e.reason = "pipe", f"class {e.ifc_class}"
    elif any(e.ent.is_a(c) for c in MANHOLE_CLASSES):
        e.kind, e.reason = "manhole", f"class {e.ifc_class}"
    elif any(w.startswith(_MANHOLE_PREFIXES) and w not in _NOT_MANHOLE for (_, k) in e.props for w in words(k)) \
            or _RE_MANHOLE.search(e.name):
        e.kind, e.reason = "manhole", "manhole properties or name"
    elif any(e.ent.is_a(c) for c in ACCESSORY_CLASSES):
        e.kind, e.reason = "accessory", f"class {e.ifc_class}"
    else:
        e.kind, e.reason = "unclassified", f"class {e.ifc_class} has no network rule"


# ----------------------------------------------------------------------------- systems
# Whole WORDS of the property name are compared (not substrings: 'red' must not match 'reference')
_SYSTEM_WORDS = {"system", "systems", "network", "nettverk", "nett", "netz", "sistema", "red", "type", "usage", "bruk"}
_STRONG_WORDS = {"system", "systems", "network", "nettverk", "nett", "netz", "sistema", "red"}


@dataclass
class SystemResult:
    mode: str                      # "ifcsystem" | "property" | "name" | "none"
    source: str = ""               # where it came from (e.g. "SB_Construction.WSS-A03_Type_Type")
    reason: str = ""
    coverage: float = 0.0
    candidates: list = field(default_factory=list)  # [(source, coverage, n_values)]


def detect_systems(model, elems: list[Element], threshold: float = 0.8) -> SystemResult:
    """Sets e.system on every network element. Order: IfcSystem -> property -> name."""
    net = [e for e in elems if e.kind in ("pipe", "manhole", "accessory")]
    if not net:
        return SystemResult("none", reason="there are no network elements")
    pipes = [e for e in net if e.kind == "pipe"] or net

    # 1) Explicit IfcSystem groups (IFC4: IfcDistributionSystem, IfcBuildingSystem...)
    by_id = {e.ent.id(): e for e in net}
    assigned = {}
    for rel in model.by_type("IfcRelAssignsToGroup"):
        g = rel.RelatingGroup
        if g and g.is_a("IfcSystem") and not g.is_a("IfcZone"):
            for o in rel.RelatedObjects or []:
                if o.id() in by_id:
                    assigned[o.id()] = g.Name or g.LongName or f"System_{g.id()}"
    cov = sum(e.ent.id() in assigned for e in pipes) / len(pipes)
    if assigned and cov >= threshold:
        for e in net:
            e.system = assigned.get(e.ent.id())
        return SystemResult("ifcsystem", "IfcSystem / IfcRelAssignsToGroup",
                            f"{len(set(assigned.values()))} systems declared in the IFC", cov)

    # 2) A property with few distinct values that covers nearly all network elements
    candidates = []
    keys = {k for e in net for k in e.props}
    for k in keys:
        pw = words(k[1])
        if not pw & _SYSTEM_WORDS:
            continue
        vals = [str(e.props.get(k)) for e in net if k in e.props]
        cov_net = len(vals) / len(net)
        cov_pipes = sum(k in e.props for e in pipes) / len(pipes)
        distinct = len(set(vals))
        if cov_pipes >= threshold and cov_net >= threshold and 1 <= distinct <= max(12, len(net) // 20):
            weight = 2 if pw & _STRONG_WORDS else 1
            if any(re.search(r"network|nett|system|sistema|\bred\b", v, re.I) for v in set(vals)):
                weight += 1  # the values themselves look like network names
            if distinct == 1:
                weight -= 1  # a single value for the whole file is rarely a system
            candidates.append((weight, cov_net, -distinct, k))
    candidates.sort(reverse=True)
    if candidates:
        _, cov_net, nd, k = candidates[0]
        for e in net:
            v = e.props.get(k)
            e.system = str(v) if v is not None else None
        return SystemResult("property", f"{k[0]}.{k[1]}", f"{-nd} distinct values in the property",
                            cov_net, [(f"{c[3][0]}.{c[3][1]}", round(c[1], 3), -c[2]) for c in candidates[:5]])

    # 3) Deduction from the name (usual network codes)
    hits = 0
    for e in net:
        t = type_from_text(e.name)
        e.system = t.upper() if t != "other" else None
        hits += e.system is not None
    cov = hits / len(net)
    if cov >= threshold:
        return SystemResult("name", "patterns in the element name", "no IfcSystem and no reliable property", cov)
    for e in net:
        e.system = None
    return SystemResult("none", reason=f"no IfcSystem, no reliable property and only {cov:.0%} recognisable by name",
                        coverage=cov)


_TYPES = [  # (suggested_type, pattern) — evaluated in order
    ("stormwater", r"storm|overvann|surface.?water|\bov\b|pluvial|regen"),
    ("drainage", r"drain|drens|\bdr\b|drenaje"),
    ("sewer", r"sewer|spillvann|waste|\bsp\b|\baf\b|\bpsp\b|saneamiento|fecal|avl[øo]p|abwasser"),
    ("water_supply", r"water.?pipe|vannledning|\bvl\b|drinking|potable|abastec|^water$|wasser"),
    ("electrical", r"electr|elektr|cable|kabel|power|\bel\b|lighting|belysning|telecom"),
]


def type_from_text(s: str) -> str:
    for t, pat in _TYPES:
        if re.search(pat, s or "", re.I):
            return t
    return "other"


# ----------------------------------------------------------------------------- data properties
# Keywords per data item; the earlier the matching keyword, the higher the priority.
ROLES = {
    "diameter": (["inner_diameter", "innerdiameter", "nominal_diameter", "diameter", "diametro", "dn"],
                 ["height", "taper", "adjusting"]),
    "material": (["material"], ["quality", "kvalitet", "calidad"]),
    "min_level": (["min_elevation", "laveste", "invert", "bottom_internal", "innerbottomz", "bunn"], []),
    "max_level": (["max_elevation", "hoeyeste", "top_internal"], []),
    "id": (["manholename", "kumnavn", "tag", "identifier", "mark", "id"], ["guid", "objektkode", "object_code", "document", "url"]),
    "from": (["fromconnection", "from_connection", "fromnode", "from_node", "start_node", "startnode", "fra_kum"], []),
    "to": (["toconnection", "to_connection", "tonode", "to_node", "end_node", "endnode", "til_kum"], []),
}


def choose_property(elems: list[Element], role: str, forced: tuple | None = None):
    """Chooses the property (pset, name) that best represents a data item, by keywords and coverage."""
    if not elems:
        return None, 0.0
    if forced:
        cov = sum(forced in e.props for e in elems) / len(elems)
        return (forced, cov) if cov > 0 else (None, 0.0)
    incl, excl = ROLES[role]
    best = None
    keys = {k for e in elems for k in e.props}
    for k in keys:
        nk = norm(k[1])
        if any(x in nk for x in excl):
            continue
        pri = next((i for i, w in enumerate(incl) if re.search(rf"(^|_){w}($|_)", nk) or (len(w) > 4 and w in nk)), None)
        if pri is None:
            continue
        cov = sum(k in e.props for e in elems) / len(elems)
        cand = (cov >= 0.5, -pri, cov, k)
        if best is None or cand > best:
            best = cand
    return (best[3], best[2]) if best else (None, 0.0)


# ----------------------------------------------------------------------------- layers
_INVALID = re.compile(r'[<>/\\":;?*|=,`\x00-\x1f]')
PALETTE = [1, 3, 5, 30, 6, 4, 140, 210, 40, 94, 152, 232, 12, 172, 52, 112]  # clearly distinct ACI colours


def layer_name(s: str) -> str:
    s = _INVALID.sub("_", str(s)).strip()
    return (s or "NO_NAME")[:200]
