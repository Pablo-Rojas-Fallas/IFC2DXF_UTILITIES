"""BOQ data for the 'BOQ Dashboard' step.

Reads only property values (no geometry) and returns the JSON consumed by boq/boq.js.
Values are sent raw: normalisation, KEY-A04 parsing, manhole grouping and code/description
checks are done in boq.js, so the logic lives in one place.
"""
from __future__ import annotations

import collections
import json
import os

import ifcopenshell

# Property names are matched by prefix (full names are long, e.g. "KEY-A03_V770-Objektkode_V770-Object-code").
FIELDS = {  # key: (pset name, property name prefix)
    "a3": ("SB_Data", "KEY-A03"),   # BOQ code
    "a4": ("SB_Data", "KEY-A04"),   # description
    "a5": ("SB_Data", "KEY-A05"),   # road number
    "q":  ("SB_Data", "GEN-B03"),   # quantity
    "u":  ("SB_Data", "GEN-B04"),   # unit
    "mh": ("Novapoint UtilityNetwork", "ManholeName"),
}
META = {"project": "GEN-A01", "revision": "GEN-A12", "revdate": "GEN-A13"}
EXCLUDED_CODES = {"", "IR [N/A]"}
_SHORT = {"IfcFlowSegment": "Seg", "IfcFlowTerminal": "Term"}


def _val(p):
    v = getattr(p, "NominalValue", None)
    return None if v is None else v.wrappedValue


def generate(path: str, progress=None) -> str:
    f = ifcopenshell.open(path)
    if progress:
        progress("Reading properties…")
    props = collections.defaultdict(dict)          # element id -> {"Pset.Prop": value}
    for rel in f.by_type("IfcRelDefinesByProperties"):
        ps = rel.RelatingPropertyDefinition
        if not ps.is_a("IfcPropertySet"):
            continue
        vals = {f"{ps.Name}.{p.Name}": _val(p) for p in (ps.HasProperties or ()) if p.is_a("IfcPropertySingleValue")}
        for o in rel.RelatedObjects:
            props[o.id()].update(vals)

    def get(d, pset, prefix):
        for k, v in d.items():
            ps, _, name = k.partition(".")
            if ps == pset and name.startswith(prefix):
                return v
        return None

    products = [e for e in f.by_type("IfcProduct")
                if getattr(e, "Representation", None) is not None and not e.is_a("IfcSpatialStructureElement")]
    elements, meta = [], {k: collections.Counter() for k in META}
    for e in products:
        d = props.get(e.id(), {})
        for k, pre in META.items():
            v = get(d, "SB_Data", pre)
            if v not in (None, "") and str(v).strip() not in EXCLUDED_CODES:
                meta[k][str(v)] += 1
        a3 = get(d, *FIELDS["a3"])
        if a3 is None or str(a3).strip() in EXCLUDED_CODES:
            continue
        rec = {"c": _SHORT.get(e.is_a(), e.is_a()), "g": e.GlobalId, "n": e.Name}
        for k, (ps, pre) in FIELDS.items():
            v = get(d, ps, pre)
            rec[k] = None if v is None else str(v)
        elements.append(rec)

    top = lambda c: c.most_common(1)[0][0] if c else None
    try:
        app = f.header.file_name.originating_system
    except Exception:
        app = None
    return json.dumps({
        "file": os.path.basename(path), "schema": f.schema, "app": app,
        "project": top(meta["project"]), "revision": top(meta["revision"]), "revdate": top(meta["revdate"]),
        "total_elements": len(products), "elements": elements,
    }, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    print(generate(sys.argv[1]))
