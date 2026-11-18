"""Prompt that the 'Copy prompt' button puts on the clipboard: instructions + schema + IFC summary."""
from __future__ import annotations

import json

from . import schema as S

CLAUDE_URL = "https://claude.ai/login"

_INSTRUCTIONS = """You are a specialist in IFC models of utility networks (stormwater, sewer, drainage, water supply or electrical networks).
Below is a SUMMARY of an IFC file produced by a local application. You do not have the full IFC: work ONLY from the summary and do not invent data.

TASK
Fill in the JSON with the EXACT SCHEMA shown below. An application will read it automatically to convert the IFC to DXF.

RULES
1. Use exactly the schema keys, in lower case, without adding new keys.
2. Use null whenever a value cannot be deduced from the summary. null is better than a guess.
3. Allowed values:
   - suggested_type: "stormwater", "sewer", "drainage", "water_supply", "electrical", "other"
   - model_coordinates: "global" (real map coordinates inside the model) or "local"
   - confidence and severity: "high", "medium", "low"
   - axis_mode: "2D" or "3D"
   - layers: "per_system" or "single"
4. Numbers as JSON numbers (decimal point, no units, no thousands separators). Lengths and coordinates in metres.
   coverage_pct between 0 and 100, measured over the elements of that kind only (pipes for pipe data, manholes for
   manhole data), NOT over the whole IFC class: exclude 3D texts and other elements that are not of that kind.
5. "pset" and "property" must be copied LITERALLY from the summary (same case, hyphens and brackets).
6. systems: identify the networks. If there are IfcSystem groups, use their names. Otherwise choose the property whose
   value separates the networks and add ONE system per distinct value (the summary lists ALL values of such properties),
   with the literal value in ifc_name. Explain in how_to_identify which Pset.Property you used.
   Fill in n_pipes, n_manholes and n_accessories only if the summary allows you to deduce them; otherwise null.
   Manholes made of several parts (base slab, shaft, cone, cover…) count as a single manhole.
7. detection_rules: real IFC classes (e.g. "IfcFlowSegment") and, in condition, the rule in one sentence.
   3D texts or annotations are not pipes, manholes or accessories.
8. properties: the property that best provides each data item.
   - diameter: the pipe diameter.
   - material: the material.
   - invert_start and invert_end: invert levels at the pipe ends (null if there are only minimum/maximum levels).
   - pipe_id and manhole_id: the identifiers.
   Give the unit if the property name includes it (e.g. "[mm]").
9. georeferencing:
   - State whether IfcMapConversion exists and copy its values if it does.
   - coordinate_system: the declared system (e.g. "EUREF89 NTM5" or "EPSG:5105").
   - xy_range: the coordinate range from the summary.
   - confidence: high only if the system is declared in the IFC and the range is consistent with it.
10. anomalies: problems you see in the summary (e.g. IfcSite latitude/longitude swapped, elements without geometry,
    empty properties, 3D texts), with their severity.
11. recommendations: axis_mode ("2D" unless 3D elevation matters), layers and short notes.

RESPONSE FORMAT
- If you can create files, deliver the result as a downloadable file named analysis.json.
- If you cannot, reply with ONE SINGLE json code block.
- In both cases, no text before or after it.

SCHEMA (fill in this structure; null = unknown)
"""


def build(summary: str) -> str:
    template = json.dumps(S.template_with_examples(), ensure_ascii=False, indent=2)
    return f"{_INSTRUCTIONS}{template}\n\n{summary}\n"
