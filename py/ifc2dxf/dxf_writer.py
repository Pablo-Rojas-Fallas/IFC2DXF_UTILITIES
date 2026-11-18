"""DXF writing (AutoCAD 2018): layers, outlines, 2D/3D axes, manhole blocks, texts and georeference."""
from __future__ import annotations

import hashlib
import math

import ezdxf
from ezdxf.enums import MTextEntityAlignment, TextEntityAlignment

from .classify import PALETTE, layer_name, type_from_text
from .geometry import rings

# Fixed ACI colours per network type (usual convention: water blue, sewer red, stormwater green...)
TYPE_COLOUR = {"water_supply": 5, "sewer": 1, "stormwater": 3, "drainage": 30, "electrical": 6}

DATA_LAYER = "DATA"
UNCLASSIFIED_LAYER = "UNCLASSIFIED"


class DxfWriter:
    def __init__(self, text_height: float = 0.5):
        self.doc = ezdxf.new("R2018", setup=True)
        self.doc.units = ezdxf.units.M
        self.doc.header["$INSUNITS"] = 6          # metres
        self.doc.header["$MEASUREMENT"] = 1       # metric
        self.doc.header["$LUNITS"] = 2
        self.doc.header["$LUPREC"] = 3
        self.msp = self.doc.modelspace()
        self.h = text_height
        self._system_colour: dict[str, int] = {}
        self.layer(DATA_LAYER, colour=7)
        self._blocks: dict[str, str] = {}

    # ------------------------------------------------------------------ layers
    def colour_for(self, system: str | None) -> int:
        """Stable colour per system: fixed by recognised network type (same in every DXF);
        if not recognised or already used, the next free one in the palette."""
        if system is None:
            return 8
        if system not in self._system_colour:
            used = set(self._system_colour.values())
            pref = TYPE_COLOUR.get(type_from_text(system))
            free = [c for c in PALETTE if c not in used] or PALETTE
            self._system_colour[system] = pref if pref and pref not in used else free[0]
        return self._system_colour[system]

    @property
    def colours(self) -> dict:
        return dict(self._system_colour)

    def layer(self, name: str, colour: int = 7, linetype: str | None = None) -> str:
        name = layer_name(name)
        if name not in self.doc.layers:
            lay = self.doc.layers.add(name, color=colour)
            if linetype and linetype in self.doc.linetypes:
                lay.dxf.linetype = linetype
        return name

    # ------------------------------------------------------------------ geometry
    def outline(self, geom, layer: str) -> int:
        n = 0
        for ring in rings(geom):
            self.msp.add_lwpolyline(ring, close=True, dxfattribs={"layer": layer})
            n += 1
        return n

    def axis(self, pts, layer: str, in_3d: bool):
        if in_3d:
            self.msp.add_polyline3d(pts, dxfattribs={"layer": layer})
        else:
            self.msp.add_lwpolyline([(x, y) for x, y, _ in pts], dxfattribs={"layer": layer})

    # ------------------------------------------------------------------ texts (always on DATA)
    def text(self, s: str, pos, rot: float = 0.0, h: float | None = None, align=TextEntityAlignment.BOTTOM_CENTER):
        t = self.msp.add_text(s, height=h or self.h, rotation=rot, dxfattribs={"layer": DATA_LAYER})
        t.set_placement(pos, align=align)
        return t

    def mtext(self, lines: list[str], pos, rot: float = 0.0, attach=MTextEntityAlignment.BOTTOM_LEFT,
              h: float | None = None):
        m = self.msp.add_mtext("\\P".join(lines), dxfattribs={"layer": DATA_LAYER, "char_height": h or self.h,
                                                              "rotation": rot})
        m.set_location(pos, attachment_point=attach)
        return m

    # ------------------------------------------------------------------ manholes as blocks
    def insert_manhole(self, centre, outlines, layer: str, attributes: dict, visible: tuple):
        """One block per distinct geometry (outlines relative to the centre) + attributes on the DATA layer."""
        cx, cy = centre
        rel = [[(round(x - cx, 3), round(y - cy, 3)) for x, y in a] for g in outlines for a in rings(g)]
        signature = hashlib.md5(repr(sorted(rel)).encode()).hexdigest()[:8]
        name = self._blocks.get(signature)
        if name is None:
            name = f"MANHOLE_{len(self._blocks) + 1:03d}"
            blk = self.doc.blocks.new(name)
            for a in rel:
                blk.add_lwpolyline(a, close=True, dxfattribs={"layer": "0", "color": 0})  # colour BYBLOCK
            if not rel:  # manhole without 3D body: point and 0.5 m reference circle
                blk.add_point((0, 0), dxfattribs={"layer": "0", "color": 0})
                blk.add_circle((0, 0), 0.5, dxfattribs={"layer": "0", "color": 0, "linetype": "DASHED"})
            radius = max((math.hypot(x, y) for a in rel for x, y in a), default=0.5)
            for i, tag in enumerate(attributes):
                blk.add_attdef(tag, insert=(radius + self.h * 0.6, radius + self.h * (0.6 + 1.3 * (len(visible) - 1 - i))),
                               height=self.h, dxfattribs={"layer": DATA_LAYER,
                                                          "flags": 0 if tag in visible else ezdxf.const.ATTRIB_INVISIBLE})
            self._blocks[signature] = name
        ref = self.msp.add_blockref(name, (cx, cy), dxfattribs={"layer": layer})
        ref.add_auto_attribs({k: ("" if v is None else str(v)) for k, v in attributes.items()})
        for a in ref.attribs:
            a.dxf.layer = DATA_LAYER
        return name

    # ------------------------------------------------------------------ georeference and saving
    def georeference(self, xml: str, centre) -> None:
        """GEOGRAPHICDATA object (experimental, only with Options.write_geodata): the drawing is already in map
        coordinates, so design point = reference point. AutoCAD misplaced drawings carrying this object."""
        gd = self.msp.new_geodata()
        gd.setup_local_grid(design_point=(centre[0], centre[1], 0), reference_point=(centre[0], centre[1]), crs=xml)
        # ezdxf leaves the owner handle (330) at "0"; AutoCAD expects it to point to *Model_Space.
        gd.dxf.block_record_handle = self.msp.block_record.dxf.handle

    def save(self, path: str, extent) -> None:
        (x0, y0), (x1, y1) = extent
        self.doc.set_modelspace_vport(height=max(y1 - y0, 50) * 1.1, center=((x0 + x1) / 2, (y0 + y1) / 2))
        # saveas() writes $EXTMIN/$EXTMAX from the modelspace layout extents (default ±1e20), so set those;
        # the header values are kept as well for clarity.
        self.msp.reset_extents((x0, y0, 0), (x1, y1, 0))
        self.doc.header["$EXTMIN"] = (x0, y0, 0)
        self.doc.header["$EXTMAX"] = (x1, y1, 0)
        self.doc.saveas(path, encoding="utf-8")
