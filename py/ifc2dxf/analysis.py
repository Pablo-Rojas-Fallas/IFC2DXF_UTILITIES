"""Tolerant reader for the .json analysis produced by an AI.

Stages (each one is recorded in the reading report):
  1. Bytes -> text: BOM, UTF-8, UTF-16, Windows-1252.
  2. Clean-up: ```json code fences, text before/after, stray BOM.
  3. Parsing: standard json; if it fails, json-repair (commas, quotes, comments, True/None, truncation...).
  4. Normalisation against the schema: keys (case, accents, typos, synonyms, Spanish 1.0 keys) and values
     (numbers with units or decimal comma, booleans, nulls, enumerations, IFC classes).
  5. Hints and cross-check against the IFC: the IFC is the source of truth.
It never raises: if nothing can be recovered, the app carries on in automatic mode.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field

from . import schema as S

try:  # json-repair is optional: without it only standard json is tried after clean-up
    import json_repair
except Exception:  # pragma: no cover
    json_repair = None

_NULLS = {"", "nd", "n_d", "n.d.", "n/d", "na", "n_a", "n/a", "null", "none", "nan", "nil", "desconocido", "desconocida",
          "unknown", "-", "--", "?", "sin_dato", "sin_datos", "no_disponible", "no_aplica", "not_available", "tbd",
          "not_applicable", "no_data"}
_TRUE = {"si", "sí", "yes", "y", "s", "true", "verdadero", "cierto", "1", "x", "ja"}
_FALSE = {"no", "n", "false", "falso", "0", "nei"}


@dataclass
class Analysis:
    status: str = "not_selected"   # not_selected | ok | repaired | empty | unrecoverable
    path: str = ""
    encoding: str = ""
    data: dict = field(default_factory=S.template)
    understood: list = field(default_factory=list)
    repaired: list = field(default_factory=list)
    ignored: list = field(default_factory=list)
    discrepancies: list = field(default_factory=list)
    _seen_keys: set = field(default_factory=set, repr=False)
    _legacy: list = field(default_factory=lambda: [0, 0], repr=False)   # Spanish 1.0 keys / values translated

    @property
    def usable(self) -> bool:
        return self.status in ("ok", "repaired")


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


# ============================================================================ 1-3. reading
def read(path: str | None) -> Analysis:
    a = Analysis(path=path or "")
    if not path:
        a.status = "not_selected"
        return a
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception as e:
        a.status = "unrecoverable"
        a.ignored.append(f"The file could not be opened: {e}. Continuing in automatic mode.")
        return a
    return read_text(_decode(raw, a), a)


def read_text(text: str, a: Analysis | None = None) -> Analysis:
    """Also used by the 'Paste response' button: the text comes from the clipboard."""
    a = a or Analysis(path="(pasted text)")
    try:
        obj = _parse(text, a)
        if obj is None:
            return a
        _normalise_root(obj, a)
        if a.status != "empty":
            a.status = "repaired" if a.repaired else "ok"
            summarise_understood(a)
    except Exception as e:  # last safety net: the reader must never break the app
        a.status = "unrecoverable"
        a.ignored.append(f"Unexpected error while interpreting the .json ({type(e).__name__}: {e}). "
                         "Continuing in automatic mode.")
        a.data = S.template()
    return a


def _decode(b: bytes, a: Analysis) -> str:
    if b.startswith(b"\xef\xbb\xbf"):
        a.encoding = "UTF-8 with BOM"
        return b[3:].decode("utf-8", errors="replace")
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        a.encoding = "UTF-16 with BOM"
        return b.decode("utf-16", errors="replace")
    if len(b) >= 4 and b[1::2].count(0) > len(b) // 4:     # UTF-16 LE without BOM: one null byte out of two
        a.encoding = "UTF-16 LE (no BOM)"
        a.repaired.append("UTF-16 encoding without BOM detected and converted.")
        return b.decode("utf-16-le", errors="replace")
    if len(b) >= 4 and b[0::2].count(0) > len(b) // 4:
        a.encoding = "UTF-16 BE (no BOM)"
        a.repaired.append("UTF-16 encoding without BOM detected and converted.")
        return b.decode("utf-16-be", errors="replace")
    try:
        a.encoding = "UTF-8"
        return b.decode("utf-8")
    except UnicodeDecodeError:
        a.encoding = "Windows-1252 / Latin-1"
        a.repaired.append("The file was not UTF-8; it was read as Windows-1252 (Latin-1).")
        return b.decode("cp1252", errors="replace")


def _parse(text: str, a: Analysis):
    t = text.replace("﻿", "").strip()
    if not t:
        a.status = "empty"
        a.ignored.append("The file is empty. Continuing in automatic mode.")
        return None
    odd = sum(1 for c in t if (ord(c) < 32 and c not in "\r\n\t") or c == "�")
    if odd > 0.02 * len(t):
        a.status = "unrecoverable"
        a.repaired.clear()
        a.ignored.append("The file is not text (it looks binary or damaged). Continuing in automatic mode.")
        return None
    # markdown code fences ```json ... ```
    m = re.search(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)(```|$)", t, re.S)
    if m and "{" in m.group(1):
        a.repaired.append("Removed the markdown code fence (```) around the JSON.")
        t = m.group(1).strip()
    # text before/after: from the first '{' to its closing brace (or to the end if truncated)
    start = t.find("{")
    if start < 0:
        a.status = "unrecoverable"
        a.ignored.append("No JSON object found in the file (unrelated text). Continuing in automatic mode.")
        return None
    end = _closing(t, start)
    if start > 0 and t[:start].strip():
        a.repaired.append(f"Ignored the text before the JSON: «{_cut(t[:start])}».")
    if end is not None and t[end + 1:].strip():
        a.repaired.append(f"Ignored the text after the JSON: «{_cut(t[end + 1:])}».")
    t = t[start:(end + 1) if end is not None else len(t)]
    try:
        return json.loads(t)
    except ValueError:
        pass
    if json_repair is None:
        a.status = "unrecoverable"
        a.ignored.append("The JSON has format errors and the json-repair library is not installed.")
        return None
    obj, log = json_repair.repair_json(t, return_objects=True, logging=True)
    for r in _translate_log(log, end is None):
        a.repaired.append(r)
    if isinstance(obj, list):
        obj = next((x for x in obj if isinstance(x, dict)), None)
        if obj is not None:
            a.repaired.append("The JSON was a list; its first object was used.")
    if not isinstance(obj, dict) or not obj:
        a.status = "unrecoverable"
        a.repaired = [r for r in a.repaired if r.startswith(("The file was not", "UTF-16"))]
        a.ignored.append("The content could not be turned into a JSON object. Continuing in automatic mode.")
        return None
    return obj


def _closing(t: str, start: int):
    """Position of the brace that closes the one at 'start', honouring strings; None if never closed."""
    depth, in_str, esc, quote = 0, False, False, ""
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        elif c in "\"'":
            in_str, quote = True, c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _translate_log(log, truncated: bool) -> list[str]:
    """Summarises the repairs reported by json-repair."""
    kinds = []
    for item in log or []:
        s = item.get("text", "").lower()
        if "comment" in s:
            kinds.append("comments removed")
        elif "trailing" in s or "comma" in s:
            kinds.append("extra or missing commas fixed")
        elif "missed the closing" in s or "closing" in s:
            kinds.append("unclosed structures completed")
        elif "quote" in s or "delimiter" in s:
            kinds.append("quotes fixed (single, missing or unclosed)")
        elif "literal" in s or "true" in s or "false" in s or "null" in s:
            kinds.append("literal values converted (True/False/None…)")
        else:
            kinds.append("other format fixes")
    out = [f"JSON format repaired: {k} ({kinds.count(k)})." for k in dict.fromkeys(kinds)]
    if truncated:
        out.insert(0, "The response was cut off (truncated): open structures were closed; "
                      "whatever was missing at the end is treated as unknown.")
    return out or ["JSON format repaired."]


def _cut(s: str, n: int = 60) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


# ============================================================================ 4. normalisation
def _normalise_root(obj: dict, a: Analysis):
    data = S.template()
    data["schema_version"] = None
    _norm_dict(obj, S.SCHEMA, data, "", a)
    v = data.get("schema_version")
    if v is None:
        a.repaired.append(f"schema_version is missing; {S.VERSION} is assumed.")
    elif str(v).strip() != S.VERSION:
        a.repaired.append(f"Schema version '{v}' differs from the expected one ({S.VERSION}); it is accepted and "
                          "everything compatible is read.")
    data["schema_version"] = S.VERSION
    if any(a._legacy):
        a.repaired.append(f"Spanish schema 1.0 file: {a._legacy[0]} keys and {a._legacy[1]} values translated "
                          f"automatically to schema {S.VERSION}.")
    # missing sections (if none arrived at all, the empty-JSON warning is enough)
    for sec in (S.SCHEMA if a._seen_keys else []):
        if sec != "schema_version" and sec not in a._seen_keys:
            a.repaired.append(f"Section «{sec}» is missing: unknown values (null) are used.")
    a.data = data
    if not _has_values(data, skip=("schema_version",)):
        a.status = "empty"
        a.ignored.append("The JSON contains no usable data (everything empty or unknown). Continuing in automatic mode.")


def _has_values(d, skip=()):
    if isinstance(d, dict):
        return any(_has_values(v) for k, v in d.items() if k not in skip)
    if isinstance(d, list):
        return any(_has_values(x) for x in d)
    return d is not None


def _match_key(k: str, expected: list[str], used: set) -> tuple[str | None, str]:
    """Returns (schema_key, how it was matched)."""
    nk = norm(k)
    free = [e for e in expected if e not in used]
    if nk in expected:
        return nk, "exact" if k == nk else "format"
    for cand in S.KEY_SYNONYMS.get(nk, []):
        if cand in free:
            return cand, "synonym"
    nosep = {e.replace("_", ""): e for e in free}
    if nk.replace("_", "") in nosep:
        return nosep[nk.replace("_", "")], "format"
    m = difflib.get_close_matches(nk, free, n=1, cutoff=0.85)
    if m:
        return m[0], "approximate"
    return None, ""


def _norm_dict(obj, node: dict, dest: dict, path: str, a: Analysis):
    if not isinstance(obj, dict):
        a.ignored.append(f"«{path or 'root'}»: an object was expected and a {type(obj).__name__} arrived; ignored.")
        return
    used = set()
    for k, v in obj.items():
        key, how = _match_key(k, list(node), used)
        if key is None:
            a.ignored.append(f"Unknown key «{path + '.' if path else ''}{k}»: ignored.")
            continue
        if key in used:
            a.ignored.append(f"Key «{path + '.' if path else ''}{k}» repeated («{key}» was already read): ignored.")
            continue
        used.add(key)
        p = f"{path}.{key}" if path else key
        a._seen_keys.add(p)
        if how == "synonym" and norm(k) in S.LEGACY_KEYS:
            a._legacy[0] += 1          # Spanish schema 1.0 key: counted, reported once
        elif how in ("synonym", "approximate"):
            a.repaired.append(f"Key «{k}» read as «{p}» ({how}).")
        elif how == "format":
            a.repaired.append(f"Key «{k}» normalised to «{key}» (case, accents or separators).")
        spec = node[key]
        if isinstance(spec, dict):
            if v is None:
                continue
            _norm_dict(v, spec, dest[key], p, a)
        elif isinstance(spec, list):
            dest[key] = _norm_list(v, spec[0], p, a)
        else:
            dest[key] = _norm_value(v, spec, p, a)


def _norm_list(v, spec_item: dict, path: str, a: Analysis) -> list:
    if v is None:
        return []
    first = next(iter(spec_item))
    if isinstance(v, dict):
        # {"OV": {...}, "SP": {...}} -> list with the name as the main key
        new = []
        for k, x in v.items():
            x = dict(x) if isinstance(x, dict) else {first: x}
            x.setdefault(first, k)
            new.append(x)
        a.repaired.append(f"«{path}» arrived as an object and was turned into a list ({len(new)} items).")
        v = new
    if not isinstance(v, list):
        v = [v]
        a.repaired.append(f"«{path}» was not a list; it was turned into a one-item list.")
    out = []
    for i, x in enumerate(v):
        if isinstance(x, str):
            x = {first: x}
            a.repaired.append(f"«{path}[{i}]» was a text; it was taken as «{first}».")
        item = {k: None for k in spec_item}
        _norm_dict(x, spec_item, item, f"{path}[{i}]", a)
        if _has_values(item):
            out.append(item)
        else:
            a.ignored.append(f"«{path}[{i}]» is empty (all null): discarded.")
    return out


def _norm_value(v, spec, path: str, a: Analysis):
    """Converts a value to the schema type; if impossible, None + a note in 'ignored'."""
    if isinstance(v, (dict, list)) and spec not in ("classes",):
        if isinstance(v, list) and len(v) == 1 and not isinstance(v[0], (dict, list)):
            a.repaired.append(f"«{path}»: one-item list turned into a value.")
            v = v[0]
        elif spec == "str":
            a.repaired.append(f"«{path}»: a structure arrived; it is kept as text.")
            return json.dumps(v, ensure_ascii=False)
        else:
            a.ignored.append(f"«{path}»: a simple value was expected and a structure arrived; discarded.")
            return None
    if _is_null(v):
        if spec == "classes":
            return []
        if isinstance(v, str) and v.strip():
            a.repaired.append(f"«{path}» = «{v}» read as unknown (null).")
        elif isinstance(v, float):
            a.repaired.append(f"«{path}» = NaN read as unknown (null).")
        return None
    kind = spec[0] if isinstance(spec, tuple) else spec
    if kind == "str":
        return str(v).strip()
    if kind == "bool":
        if isinstance(v, bool):
            return v
        s = norm(v)
        if s in _TRUE or str(v).strip().lower() in _TRUE:
            a.repaired.append(f"«{path}» = «{v}» converted to true.")
            return True
        if s in _FALSE:
            a.repaired.append(f"«{path}» = «{v}» converted to false.")
            return False
        a.ignored.append(f"«{path}» = «{v}» is not a recognisable yes/no; discarded.")
        return None
    if kind in ("num", "num_m", "int", "pct"):
        return _norm_number(v, kind, path, a)
    if kind == "enum":
        return _norm_enum(v, spec[1], path, a)
    if kind == "unit":
        return _norm_unit(v, path, a)
    if kind == "classes":
        return _norm_classes(v, path, a)
    return v


def _is_null(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and (norm(v) in _NULLS or v.strip().lower() in _NULLS)


_RE_NUM = re.compile(r"[-+−]?\s?\d[\d\s.,'’]*(?:[eE][-+]?\d+)?")
_RE_UNIT = re.compile(r"^\s*(%|mil[ií]metros?|millimet(?:er|re)s?|cent[ií]metros?|centimet(?:er|re)s?|kil[oó]metros?|"
                      r"kilomet(?:er|re)s?|metros?|meters?|metres?|mm|cm|dm|km|m)(?![a-zA-Z])", re.I)
_FACTOR = {"mm": 1e-3, "cm": 1e-2, "dm": 1e-1, "km": 1e3, "m": 1.0}


def parse_number(v):
    """'1 262 000,5 m' -> (1262000.5, 'm', notes). Accepts decimal comma and thousands separators."""
    if isinstance(v, bool):
        raise ValueError("boolean")
    if isinstance(v, (int, float)):
        return float(v), None, []
    s = str(v).strip().replace("−", "-")
    m = _RE_NUM.search(s)
    if not m:
        raise ValueError("no number")
    num = m.group(0).replace(" ", "").replace("'", "").replace("’", "").replace(" ", "")
    notes = []
    if "," in num and "." in num:
        dec = "," if num.rfind(",") > num.rfind(".") else "."
        num = num.replace("." if dec == "," else ",", "").replace(",", ".")
        notes.append("thousands and decimal separators")
    elif num.count(",") > 1:
        num = num.replace(",", "")
        notes.append("commas as thousands separator")
    elif "," in num:
        num = num.replace(",", ".")
        notes.append("decimal comma")
    elif num.count(".") > 1:
        num = num.replace(".", "")
        notes.append("dots as thousands separator")
    val = float(num)
    if m.start() > 0 and s[:m.start()].strip() not in ("", "~", "≈", "aprox", "aprox.", "approx", "approx.", "ca."):
        notes.append(f"leading text «{s[:m.start()].strip()}» ignored")
    rest = s[m.end():]
    u = _RE_UNIT.match(rest)
    unit = None
    if u:
        t = u.group(1).lower()
        unit = "%" if t == "%" else "mm" if t.startswith(("mm", "mil")) else "cm" if t.startswith(("cm", "cent")) \
            else "km" if t.startswith(("km", "kil")) else "dm" if t == "dm" else "m"
        rest = rest[u.end():]
    if rest.strip():
        notes.append(f"trailing text «{_cut(rest.strip(), 30)}» ignored")
    return val, unit, notes


def _norm_number(v, kind, path, a):
    try:
        val, unit, notes = parse_number(v)
    except ValueError:
        a.ignored.append(f"«{path}» = «{v}» contains no number; discarded.")
        return None
    orig = v
    if kind == "num_m" and unit and unit not in ("m", "%"):
        val = round(val * _FACTOR[unit], 6)
        notes.append(f"converted from {unit} to m")
    if kind == "pct":
        if unit != "%" and 0 < val < 1 and not float(val).is_integer():
            val *= 100
            notes.append("fraction converted to percentage")
        if not 0 <= val <= 100:
            a.ignored.append(f"«{path}» = «{orig}» is outside 0–100 %; discarded.")
            return None
    if kind == "int":
        if not float(val).is_integer():
            notes.append("rounded to an integer")
        val = int(round(val))
        if val < 0:
            a.ignored.append(f"«{path}» = «{orig}» is negative; discarded.")
            return None
    if not isinstance(orig, (int, float)) or notes:
        a.repaired.append(f"«{path}» = «{orig}» → {val}" + (f" ({', '.join(notes)})" if notes else ""))
    return val


def _norm_enum(v, name, path, a):
    allowed = S.ENUMS[name]
    s = norm(v)
    for p in allowed:
        if s == norm(p):
            if str(v) != p:
                a.repaired.append(f"«{path}» = «{v}» → «{p}».")
            return p
    for p, syns in S.VALUE_SYNONYMS.get(name, {}).items():
        if s in syns or any(s.startswith(x + "_") or s.endswith("_" + x) for x in syns if len(x) > 3):
            if s in S.LEGACY_VALUES:
                a._legacy[1] += 1      # Spanish schema 1.0 value: counted, reported once
            else:
                a.repaired.append(f"«{path}» = «{v}» → «{p}» (synonym).")
            return p
    m = difflib.get_close_matches(s, [norm(p) for p in allowed], n=1, cutoff=0.8)
    if m:
        p = next(p for p in allowed if norm(p) == m[0])
        a.repaired.append(f"«{path}» = «{v}» → «{p}» (approximate match).")
        return p
    a.ignored.append(f"«{path}» = «{v}» is not an allowed value ({', '.join(allowed)}); discarded.")
    return None


def _norm_unit(v, path, a):
    s = norm(v)
    table = {"m": ["m", "metro", "metros", "metre", "metres", "meter", "meters"],
             "mm": ["mm", "milimetro", "milimetros", "millimetre", "millimetres", "millimeter", "millimeters", "milli_metre"],
             "cm": ["cm", "centimetro", "centimetros", "centimetre", "centimeter", "centimetres", "centimeters"],
             "ft": ["ft", "foot", "feet", "pie", "pies"], "in": ["in", "inch", "inches", "pulgada", "pulgadas"]}
    for u, syns in table.items():
        if s in syns:
            if str(v) != u:
                a.repaired.append(f"«{path}» = «{v}» → «{u}».")
            return u
    a.ignored.append(f"«{path}» = «{v}» is not a recognised length unit; discarded.")
    return None


_IFC_CLASSES = None


def ifc_classes() -> dict:
    """{lower-case name: canonical name} of every class in IFC2X3, IFC4 and IFC4X3."""
    global _IFC_CLASSES
    if _IFC_CLASSES is None:
        import ifcopenshell.ifcopenshell_wrapper as w
        _IFC_CLASSES = {}
        for s in w.schema_names():
            if s.upper().startswith("IFC"):
                for d in w.schema_by_name(s).declarations():
                    if hasattr(d, "is_abstract"):
                        _IFC_CLASSES[d.name().lower()] = d.name()
    return _IFC_CLASSES


def _norm_classes(v, path, a) -> list:
    if isinstance(v, str):
        v = [x for x in re.split(r"[,;/|\s]+", v) if x]
        a.repaired.append(f"«{path}»: text turned into a list of classes.")
    if not isinstance(v, list):
        v = [v]
    out = []
    table = ifc_classes()
    for x in v:
        s = str(x).strip().strip("\"'")
        c = table.get(s.lower()) or table.get("ifc" + s.lower())
        if c:
            if c != s:
                a.repaired.append(f"«{path}»: class «{s}» → «{c}».")
            out.append(c)
        else:
            m = difflib.get_close_matches(s.lower(), list(table), n=1, cutoff=0.9)
            if m:
                a.repaired.append(f"«{path}»: class «{s}» → «{table[m[0]]}» (typo fixed).")
                out.append(table[m[0]])
            else:
                a.ignored.append(f"«{path}»: «{s}» is not an IFC class; discarded.")
    return list(dict.fromkeys(out))


# ============================================================================ summary of what was understood
def summarise_understood(a: Analysis) -> None:
    d = a.data
    a.understood = []
    w = a.understood.append
    f = d["file"]
    if any(f.values()):
        w("File: " + ", ".join(f"{k} = {v}" for k, v in f.items() if v is not None))
    g = d["georeferencing"]
    gv = {k: v for k, v in g.items() if v is not None and k != "xy_range"}
    if gv:
        w("Georeferencing: " + ", ".join(f"{k} = {v}" for k, v in gv.items()))
    if any(v is not None for v in g["xy_range"].values()):
        w("Expected XY range: " + ", ".join(f"{k} = {v}" for k, v in g["xy_range"].items()))
    for s in d["systems"]:
        w(f"System «{s['ifc_name']}»: type {s['suggested_type']}, pipes {s['n_pipes']}, manholes {s['n_manholes']}, "
          f"accessories {s['n_accessories']}")
    for k, r in d["detection_rules"].items():
        if r["ifc_classes"] or r["condition"]:
            w(f"Rule «{k}»: classes {r['ifc_classes'] or '—'}" + (f"; condition: {r['condition']}" if r["condition"] else ""))
    for k, p in d["properties"].items():
        if p["property"]:
            w(f"Property «{k}»: {p['pset'] or '?'}.{p['property']}"
              + (f" ({p['coverage_pct']} %)" if p["coverage_pct"] is not None else ""))
    for x in d["anomalies"]:
        w(f"Anomaly ({x['severity'] or 'no severity'}): {x['description']}")
    r = d["recommendations"]
    if any(r.values()):
        w("Recommendations: " + ", ".join(f"{k} = {v}" for k, v in r.items() if v is not None))


def reading_text(a: Analysis) -> str:
    """Reading report of the .json with the three lists and the discrepancies."""
    st = {"not_selected": "no .json selected: automatic mode",
          "ok": "read correctly", "repaired": "read with automatic repairs",
          "empty": "empty or without data: automatic mode", "unrecoverable": "unrecoverable: automatic mode"}[a.status]
    L = ["JSON ANALYSIS READING REPORT", "=" * 78,
         f"File: {os.path.basename(a.path) if a.path else '—'}   Encoding: {a.encoding or '—'}",
         f"Result: {st}", ""]
    for title, items in (("UNDERSTOOD", a.understood), ("REPAIRED OR CORRECTED AUTOMATICALLY", a.repaired),
                         ("IGNORED OR DISCARDED", a.ignored),
                         ("DISCREPANCIES WITH THE IFC (the IFC is what counts)", a.discrepancies)):
        L.append(title)
        L.append("-" * 78)
        L.extend(f"  • {x}" for x in items) if items else L.append("  (nothing)")
        L.append("")
    return "\n".join(L)


# ============================================================================ 5. hints and cross-check with the IFC
# Which internal role each schema property feeds
JSON_ROLES = {
    "diameter": [("pipe", "diameter"), ("manhole", "diameter")],
    "material": [("pipe", "material"), ("manhole", "material")],
    "pipe_id": [("pipe", "id")],
    "manhole_id": [("manhole", "id")],
}
_PLURAL = {"pipe": "pipes", "manhole": "manholes", "accessory": "accessories"}


def resolve_property(elems, pset, prop):
    """Finds in the IFC the property named by the .json, tolerating case and small typos.
    Returns ((pset, prop), note) or (None, reason)."""
    if not prop:
        return None, "no property name"
    keys = {k for e in elems for k in e.props}
    if (pset, prop) in keys:
        return (pset, prop), ""
    cand = [k for k in keys if k[1].lower() == str(prop).lower() and (not pset or k[0].lower() == str(pset).lower())]
    if not cand:
        cand = [k for k in keys if k[1].lower() == str(prop).lower()]
    if len(cand) == 1:
        return cand[0], f"name adjusted to {cand[0][0]}.{cand[0][1]}"
    names = {f"{k[0]}.{k[1]}".lower(): k for k in keys}
    m = difflib.get_close_matches(f"{pset}.{prop}".lower(), list(names), n=1, cutoff=0.92)
    if m:
        k = names[m[0]]
        return k, f"typo fixed: {k[0]}.{k[1]}"
    return None, "does not exist in the IFC"


def apply_hints(a: Analysis | None, elems_by_kind: dict) -> tuple[dict, int | None]:
    """Forced properties verified against the IFC, and suggested EPSG. The IFC wins: anything unverifiable is dropped."""
    forced, epsg = {}, None
    if a is None or not a.usable:
        return forced, epsg
    from .georef import epsg_from_text
    cs = a.data["georeferencing"]["coordinate_system"]
    if cs:
        epsg = epsg_from_text(cs)
        if epsg is None:
            a.ignored.append(f"coordinate_system «{cs}» could not be translated into an EPSG code; ignored.")
    for json_role, targets in JSON_ROLES.items():
        p = a.data["properties"][json_role]
        if not p["property"]:
            continue
        applied = False
        for kind, role in targets:
            elems = elems_by_kind.get(kind, [])
            key, note = resolve_property(elems, p["pset"], p["property"])
            if key is None:
                continue
            cov = sum(key in e.props for e in elems) / len(elems) if elems else 0
            forced[(kind, role)] = key
            applied = True
            a.understood.append(f"Applied: «{json_role}» → {key[0]}.{key[1]} on {_PLURAL[kind]} (verified in the IFC, "
                                f"actual coverage {cov:.0%})" + (f"; {note}" if note else ""))
            if p["coverage_pct"] is not None and abs(p["coverage_pct"] - 100 * cov) > 15 and kind == targets[0][0]:
                a.discrepancies.append(f"Coverage of «{json_role}»: the .json says {p['coverage_pct']:.0f} %, "
                                       f"the IFC has {cov:.0%} on {_PLURAL[kind]}.")
        if not applied:
            a.discrepancies.append(f"The property for «{json_role}» ({p['pset']}.{p['property']}) does not exist in the IFC; "
                                   "automatic detection is used.")
    return forced, epsg


def cross_check(a: Analysis | None, h: dict) -> None:
    """Compares what the .json says with what was found in the IFC ('h' = conversion facts)."""
    if a is None or not a.usable:
        return
    d, disc = a.data, a.discrepancies
    f = d["file"]
    if f["name"] and norm(os.path.splitext(os.path.basename(f["name"]))[0]) not in norm(h["ifc"]) \
            and norm(h["ifc"]) not in norm(f["name"]):
        disc.append(f"The .json describes the file «{f['name']}» but the loaded IFC is «{h['ifc']}»: "
                    "check that the analysis belongs to this IFC.")
    if f["ifc_schema"] and norm(f["ifc_schema"]).replace("_", "") != norm(h["schema"]).replace("_", ""):
        disc.append(f"IFC schema: the .json says «{f['ifc_schema']}», the IFC is {h['schema']}.")
    if f["length_unit"]:
        fac = {"m": 1.0, "mm": 0.001, "cm": 0.01, "ft": 0.3048, "in": 0.0254}[f["length_unit"]]
        if abs(fac - h["scale"]) > 1e-9:
            disc.append(f"Units: the .json says «{f['length_unit']}», the IFC uses {h['scale']} m per unit.")
    g, gi = d["georeferencing"], h["georef"]
    if g["has_ifcmapconversion"] is not None and g["has_ifcmapconversion"] != h["has_mapconversion"]:
        disc.append(f"IfcMapConversion: the .json says {'yes' if g['has_ifcmapconversion'] else 'no'}, "
                    f"the IFC {'has' if h['has_mapconversion'] else 'does not have'} one.")
    if h.get("mapconversion"):
        for k, kk in (("eastings", "E"), ("northings", "N")):
            if g[k] is not None and abs(g[k] - h["mapconversion"][kk]) > 0.5:
                disc.append(f"{k}: the .json says {g[k]}, IfcMapConversion has {h['mapconversion'][kk]}.")
    if g["coordinate_system"]:
        from .georef import epsg_from_text
        ep = epsg_from_text(g["coordinate_system"])
        if ep and ep != gi.epsg:
            disc.append(f"Coordinate system: the .json says EPSG:{ep}, EPSG:{gi.epsg} was used ({gi.crs_source}).")
    mc = g["model_coordinates"]
    real = "global" if gi.method == "global" else "local"
    if mc and mc != real:
        disc.append(f"Model coordinates: the .json says «{mc}», the IFC has them {real} (method {gi.method}).")
    r = g["xy_range"]
    (x0, y0, _), (x1, y1, _) = h["extent"]
    tol = max(200.0, 0.05 * max(x1 - x0, y1 - y0))
    for k, real_v in (("xmin", x0), ("xmax", x1), ("ymin", y0), ("ymax", y1)):
        if r[k] is not None and abs(r[k] - real_v) > tol:
            disc.append(f"xy_range.{k}: the .json says {r[k]:,.2f}, the drawing has {real_v:,.2f} (difference > {tol:.0f} m).")
    # systems and counts
    det = h["systems"]  # {name: Counter(kind)}
    det_n = {norm(k): k for k in det}
    from .classify import type_from_text
    for s in d["systems"]:
        nm = s["ifc_name"]
        if not nm:
            continue
        k = det_n.get(norm(nm))
        if k is None:
            disc.append(f"System «{nm}» in the .json does not appear in the IFC (detected systems: "
                        f"{', '.join(det) or 'none'}).")
            continue
        for fld, kind in (("n_pipes", "pipe"), ("n_manholes", "manhole"), ("n_accessories", "accessory")):
            if s[fld] is not None and s[fld] != det[k].get(kind, 0):
                disc.append(f"«{nm}»: {fld} = {s[fld]} in the .json, {det[k].get(kind, 0)} in the IFC.")
        t = type_from_text(nm)
        if s["suggested_type"] and t != "other" and s["suggested_type"] != t:
            disc.append(f"«{nm}»: the .json classifies it as «{s['suggested_type']}», the name suggests «{t}» "
                        "(informational only).")
    json_names = {norm(s["ifc_name"]) for s in d["systems"] if s["ifc_name"]}
    if json_names:
        missing = [k for k in det if norm(k) not in json_names]
        if missing:
            disc.append(f"IFC systems not mentioned in the .json: {', '.join(missing)}.")
    for name, rule in d["detection_rules"].items():
        for c in rule["ifc_classes"] or []:
            if c not in h["classes"]:
                disc.append(f"detection_rules.{name}: class {c} does not appear in this IFC.")
