"""Label placement (de-cluttering) of the free-standing data texts: pipe labels and invert-level texts.

Used when Options.declutter is on (the default). The pipeline hands over the movable texts as requests instead of
writing them; place_and_write() then places them all together, deterministically, and writes them on layer DATA.

What moves: pipe labels (MTEXT) and invert-level texts at pipe ends (TEXT). Nothing else.

Cost-based placement:
  FORBIDDEN  text over another text, or over a visible manhole attribute / any other text already in the drawing.
  PENALTY    each OTHER pipe the text crosses (its own pipe is free), each manhole symbol it covers, each text
             its leader would cross, each leader it would cover.
  COST       distance from the text to what it labels + the penalties. The lowest-cost free spot wins.
Candidates: preferred slots (pipe labels slide along their pipe on both sides; level texts are staggered along
their pipe at shared manholes), then rings around the anchor from SEARCH_MIN to SEARCH_MAX metres (independent of
the text size), SEARCH_DIRECTIONS directions, at the pipe angle and horizontal (0°).
If no free spot exists within SEARCH_MAX, the text is UNRESOLVED: it goes to the spot with the least overlap area
and is listed in the report so it can be fixed by hand.

Text sizes are ESTIMATED (characters x height x CHAR_WIDTH), not measured with the real font.
TIME_BUDGET: after this many seconds the texts not yet placed stay at their original position (status "skipped").
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

from ezdxf.enums import MTextEntityAlignment, TextEntityAlignment
from shapely import STRtree
from shapely.affinity import rotate
from shapely.geometry import LineString, Point, box
from shapely.ops import nearest_points, unary_union

CHAR_WIDTH = 0.8        # estimated character width / text height (same estimate as tests/compare_dxf.py)
LINE_PITCH = 5 / 3      # MTEXT line distance / char height at line spacing 1.0
AREA_EPS = 1e-9         # m²: boxes that only touch do not collide
PIPE_FRACTIONS = (0.5, 0.35, 0.65, 0.2, 0.8)
LEVEL_DISTANCES = (2.5, 4.0, 5.5, 7.0)   # x base text height, from the pipe end along the pipe
NODE_TOL = 0.5                           # m: pipe ends closer than this share a node (manhole)
GRID = 5.0                               # m: cell size of the index of placed texts

# search area, in metres (independent of the text scale)
SEARCH_MIN = 0.5
SEARCH_MAX = 10.0
SEARCH_STEP = 0.5
SEARCH_DIRECTIONS = 24

# cost terms (metre-equivalents)
PIPE_PENALTY = 5.0              # per other pipe crossed
MANHOLE_PENALTY = 3.0           # per manhole symbol covered
LEADER_CROSS_PENALTY = 4.0      # per text the leader would cross
LEADER_OVER_PENALTY = 1.0       # per existing leader the text would cover
HORIZONTAL_PENALTY = 0.5        # horizontal (0°) instead of the pipe angle
SLIDE_WEIGHT = 0.05             # per metre a pipe label slides away from its preferred slot
GOOD_ENOUGH = SEARCH_MIN        # a free preferred slot cheaper than this is taken without searching the rings

# time budget of the whole placement, in seconds: when it runs out, the texts not yet placed stay at their original
# position (status "skipped") and the report says so. The conversion never hangs because of labels.
TIME_BUDGET = 60.0

# leaders
LEADER_MIN_GAP = 1.0            # x text height: a leader only if the text is farther than this from its pipe
LEADER_MIN_LEN = 1.5            # x text height: never draw a shorter leader


@dataclass
class Request:
    kind: str                 # "pipe" (MTEXT label) or "level" (TEXT at a pipe end)
    idx: int                  # order of creation: used for ties and for writing order
    lines: list
    h: float
    pos: tuple                # preferred insertion point
    rot: float                # degrees (readable pipe angle)
    top: bool                 # True: insertion point at the top centre of the text; False: bottom centre
    anchor: tuple             # pipe label: point of its pipe nearest the preferred slot; level: the pipe end
    pipe_id: int
    axis: LineString          # own pipe axis (2D)
    cands: list = field(default_factory=list)   # (pos, rot, top, slide) in preference order; cands[0] = original
    node_dir: tuple | None = None
    n_free: int = 0
    result: tuple | None = None                 # chosen (pos, rot, top)
    status: str = "unresolved"                  # stayed | moved | leader | unresolved
    leader: tuple | None = None                 # ((x0, y0), (x1, y1))
    overlap: float = 0.0                        # unresolved: remaining overlap area (m²)
    geo: tuple | None = None                    # level texts: data to build candidates once nodes are known


# ----------------------------------------------------------------------------- boxes
def _dims(lines, h):
    n_chars = max((len(s) for s in lines), default=0)
    return n_chars * h * CHAR_WIDTH, h + (len(lines) - 1) * h * LINE_PITCH


def request_box(r: Request, pos, rot, top):
    w, ht = _dims(r.lines, r.h)
    y0 = -ht if top else 0.0
    return rotate(box(pos[0] - w / 2, pos[1] + y0, pos[0] + w / 2, pos[1] + y0 + ht), rot, origin=(pos[0], pos[1]))


def entity_box(e):
    """Estimated rotated box of an existing TEXT / ATTRIB / MTEXT entity, or None (empty or invisible)."""
    t = e.dxftype()
    if t == "MTEXT":
        lines = e.plain_text(split=True) or [""]
        w, ht = _dims(lines, e.dxf.char_height)
        ap = e.dxf.get("attachment_point", 1)
        col, row = (ap - 1) % 3, (ap - 1) // 3
        x0, y0 = (0, -w / 2, -w)[col], (-ht, -ht / 2, 0)[row]
        p, rot = e.dxf.insert, e.get_rotation()
    elif t in ("TEXT", "ATTRIB"):
        if t == "ATTRIB" and e.is_invisible:
            return None
        lines = [e.dxf.text]
        w, ht = _dims(lines, e.dxf.height)
        w *= e.dxf.get("width", 1.0)
        ha, va = e.dxf.get("halign", 0), e.dxf.get("valign", 0)
        if ha == 4:
            ha, va = 1, 2
        x0 = {0: 0, 1: -w / 2, 2: -w}.get(ha, 0)
        y0 = {0: 0, 1: 0, 2: -ht / 2, 3: -ht}.get(va, 0)
        p = e.dxf.insert if (ha == 0 and va == 0) else e.dxf.get("align_point", e.dxf.insert)
        rot = e.dxf.get("rotation", 0.0)
    else:
        return None
    if w <= 0:
        return None
    return rotate(box(p.x + x0, p.y + y0, p.x + x0 + w, p.y + y0 + ht), rot, origin=(p.x, p.y))


def fixed_label_boxes(msp) -> list:
    """Boxes of every label already in the modelspace: visible block attributes and TEXT/MTEXT."""
    out = []
    for e in msp:
        if e.dxftype() == "INSERT":
            out += [b for b in (entity_box(a) for a in e.attribs) if b is not None]
        elif e.dxftype() in ("TEXT", "MTEXT"):
            b = entity_box(e)
            if b is not None:
                out.append(b)
    return out


def _readable(deg):
    while deg > 90:
        deg -= 180
    while deg <= -90:
        deg += 180
    return deg


def count_overlaps(boxes) -> int:
    """Pairs of boxes whose intersection has a non-zero area."""
    if len(boxes) < 2:
        return 0
    tree = STRtree(boxes)
    a, b = tree.query(boxes, predicate="intersects")
    return sum(1 for i, j in zip(a.tolist(), b.tolist())
               if i < j and boxes[i].intersection(boxes[j]).area > AREA_EPS)


def measure(text_boxes, fixed_labels, pipe_geoms, manhole_geoms) -> dict:
    """Quality counts for a set of movable text boxes [(box, own pipe_id)]:
    text-text overlapping pairs (movable + fixed labels), movable texts over another pipe, over a manhole symbol."""
    pipe_tree = STRtree([g for g, _ in pipe_geoms]) if pipe_geoms else None
    mh_tree = STRtree(manhole_geoms) if manhole_geoms else None
    over_pipe = over_mh = 0
    for b, own in text_boxes:
        if pipe_tree is not None and any(pipe_geoms[i][1] != own and b.intersection(pipe_geoms[i][0]).area > AREA_EPS
                                         for i in pipe_tree.query(b, predicate="intersects").tolist()):
            over_pipe += 1
        if mh_tree is not None and any(b.intersection(manhole_geoms[i]).area > AREA_EPS
                                       for i in mh_tree.query(b, predicate="intersects").tolist()):
            over_mh += 1
    return dict(text_text=count_overlaps(fixed_labels + [b for b, _ in text_boxes]),
                over_pipes=over_pipe, over_manholes=over_mh)


class _Grid:
    """Growing spatial index (STRtree is static) for the texts and leaders placed so far."""

    def __init__(self):
        self.cells: dict = {}
        self.n = 0

    def _cells(self, g):
        x0, y0, x1, y1 = g.bounds
        for i in range(math.floor(x0 / GRID), math.floor(x1 / GRID) + 1):
            for j in range(math.floor(y0 / GRID), math.floor(y1 / GRID) + 1):
                yield i, j

    def add(self, g):
        self.n += 1
        for c in self._cells(g):
            self.cells.setdefault(c, []).append((self.n, g))

    def near(self, g):
        seen = set()
        for c in self._cells(g):
            for k, q in self.cells.get(c, ()):
                if k not in seen:
                    seen.add(k)
                    yield q


# ----------------------------------------------------------------------------- layout
class LabelLayout:
    def __init__(self):
        self.requests: list[Request] = []
        self.pipe_geoms: list = []        # (geometry, pipe_id)
        self.fixed_geoms: list = []       # manhole symbols (outlines / reference circles)

    # ---- collected by the pipeline
    def add_pipe(self, pipe_id, outline, axis_lines):
        """A pipe as an obstacle: its plan outline (if any) and its axes (thin buffer)."""
        if outline is not None and not outline.is_empty:
            self.pipe_geoms.append((outline, pipe_id))
        for pl in axis_lines:
            xy = [(p[0], p[1]) for p in pl]
            if len(xy) >= 2:
                self.pipe_geoms.append((LineString(xy).buffer(0.02), pipe_id))

    def add_fixed(self, geom):
        if geom is not None and not geom.is_empty:
            self.fixed_geoms.append(geom)

    def pipe_label(self, lines, pos, rot, h, pipe_id, axis, off):
        xy = [(p[0], p[1]) for p in axis]
        ls = LineString(xy)
        anchor = tuple(nearest_points(ls, Point(pos))[0].coords[0])
        r = Request("pipe", len(self.requests), list(lines), h, tuple(pos), rot, False, anchor, pipe_id, ls)
        r.cands = [(r.pos, rot, False, 0.0)]
        cum = [0.0]
        for i in range(len(xy) - 1):
            cum.append(cum[-1] + math.dist(xy[i], xy[i + 1]))
        for f in PIPE_FRACTIONS:
            d = f * cum[-1]
            k = next((i for i in range(len(xy) - 1) if cum[i + 1] >= d), len(xy) - 2)
            (x1, y1), (x2, y2) = xy[k], xy[k + 1]
            if math.dist((x1, y1), (x2, y2)) < 1e-9:
                continue
            p = ls.interpolate(d)
            slide = math.dist((p.x, p.y), anchor)
            ra = _readable(math.degrees(math.atan2(y2 - y1, x2 - x1)))
            nx, ny = -math.sin(math.radians(ra)), math.cos(math.radians(ra))
            for side in (1, -1):
                r.cands.append(((p.x + side * nx * off, p.y + side * ny * off), ra, side < 0, slide))
        self.requests.append(r)

    def level_text(self, text, pos, rot, h, pipe_id, end, direction, normal, off, step, axis):
        ls = LineString([(p[0], p[1]) for p in axis])
        r = Request("level", len(self.requests), [text], h, tuple(pos), rot, True, tuple(end), pipe_id, ls,
                    node_dir=tuple(direction))
        r.cands = [(r.pos, rot, True, 0.0)]
        r.geo = (end, direction, normal, off, step)    # candidates are built once the nodes are known
        self.requests.append(r)

    # ---- placement
    def _build_level_candidates(self):
        nodes: list[list] = []                          # [[(x, y), [requests]]]
        for r in (q for q in self.requests if q.kind == "level"):
            for nd in nodes:
                if math.dist(nd[0], r.anchor) <= NODE_TOL:
                    nd[1].append(r)
                    break
            else:
                nodes.append([r.anchor, [r]])
        for _, members in nodes:
            members.sort(key=lambda q: (math.atan2(q.node_dir[1], q.node_dir[0]), q.idx))
            for rank, r in enumerate(members):
                (ex, ey), (ux, uy), (nx, ny), off, step = r.geo
                k = rank % len(LEVEL_DISTANCES) if len(members) > 1 else 0   # stagger texts sharing a manhole
                for d in LEVEL_DISTANCES[k:] + LEVEL_DISTANCES[:k]:
                    for side in (-1, 1):
                        p = (ex + ux * step * d + side * nx * off, ey + uy * step * d + side * ny * off)
                        r.cands.append((p, r.rot, side < 0, 0.0))

    def place_and_write(self, W, data_layer: str, budget: float = TIME_BUDGET) -> dict:
        t0 = time.time()
        deadline = t0 + budget
        msp = W.msp
        self._build_level_candidates()

        # own pipe body (outline + axis) per pipe: the gap to it decides leaders and the cost of pipe labels
        body = {}
        for g, pid in self.pipe_geoms:
            body.setdefault(pid, []).append(g)
        body = {pid: unary_union(gs) for pid, gs in body.items()}
        own = lambda r: body.get(r.pipe_id, r.axis)

        fixed_labels = fixed_label_boxes(msp)
        label_tree = STRtree(fixed_labels) if fixed_labels else None
        pipe_tree = STRtree([g for g, _ in self.pipe_geoms]) if self.pipe_geoms else None
        mh_tree = STRtree(self.fixed_geoms) if self.fixed_geoms else None
        texts, leaders = _Grid(), _Grid()

        def hard_area(b, full=True):
            """Overlap area of b with the fixed labels and the texts placed so far (0.0 if below AREA_EPS).
            full=False: only 'is it blocked?' is needed, so stop as soon as the sum passes AREA_EPS."""
            a = 0.0
            if label_tree is not None:
                for i in label_tree.query(b, predicate="intersects"):
                    a += b.intersection(fixed_labels[i]).area
                    if not full and a > AREA_EPS:
                        return a
            for q in texts.near(b):
                if b.intersects(q):
                    a += b.intersection(q).area
                    if not full and a > AREA_EPS:
                        return a
            return a if a > AREA_EPS else 0.0

        def soft(r, b):
            n_pipes = len({self.pipe_geoms[i][1] for i in (pipe_tree.query(b, predicate="intersects").tolist()
                                                          if pipe_tree is not None else [])
                           if self.pipe_geoms[i][1] != r.pipe_id
                           and b.intersection(self.pipe_geoms[i][0]).area > AREA_EPS})
            n_mh = sum(1 for i in (mh_tree.query(b, predicate="intersects").tolist() if mh_tree is not None else [])
                       if b.intersection(self.fixed_geoms[i]).area > AREA_EPS)
            n_lead = sum(1 for q in leaders.near(b) if b.intersects(q))
            return PIPE_PENALTY * n_pipes + MANHOLE_PENALTY * n_mh + LEADER_OVER_PENALTY * n_lead

        def leader_for(r, b):
            """Leader (start, end) if the text is far enough from its pipe body, else None."""
            if b.distance(own(r)) <= LEADER_MIN_GAP * r.h:
                return None
            target = nearest_points(b, r.axis)[1] if r.kind == "pipe" else Point(r.anchor)
            start = nearest_points(b, target)[0]
            if start.distance(target) < LEADER_MIN_LEN * r.h:
                return None
            return (start.x, start.y), (target.x, target.y)

        def leader_crossings(ld, own_box):
            line = LineString(ld)
            n = 0
            if label_tree is not None:
                n += sum(1 for i in label_tree.query(line, predicate="intersects"))
            n += sum(1 for q in texts.near(line) if q is not own_box and line.intersects(q))
            return n

        def cost_below(r, b, rot, slide, ring, bound):
            """(cost, leader) of box b, or None as soon as the cost cannot be lower than 'bound'.
            Every cost term is >= 0, so stopping early never changes which candidate wins."""
            if ring or r.kind == "level":
                cost = b.distance(Point(r.anchor))
            else:
                cost = b.distance(own(r)) + SLIDE_WEIGHT * slide
            cost += HORIZONTAL_PENALTY if abs(rot - r.rot) > 1e-6 else 0.0
            if cost >= bound:
                return None
            cost += soft(r, b)
            if cost >= bound:
                return None
            ld = leader_for(r, b)
            if ld is not None:
                cost += LEADER_CROSS_PENALTY * leader_crossings(ld, b)
            return (cost, ld) if cost < bound else None

        def consider(r, best, n, pos, rot, top, slide, ring):
            """Update best = [best_free, best_any] with one candidate (same choice as a full evaluation)."""
            b = request_box(r, pos, rot, top)
            area = hard_area(b, full=best[0] is None)     # the exact area only matters while no free spot is known
            if area == 0.0:
                bf = best[0]
                res = cost_below(r, b, rot, slide, ring, bf[0] if bf is not None else math.inf)
                if res is not None:
                    best[0] = (res[0], n, (pos, rot, top), b, res[1])
            elif best[0] is None:            # best_any only matters while no free spot is known
                ba = best[1]
                if ba is None or area < ba[0]:
                    bound = math.inf
                elif area == ba[0]:
                    bound = ba[1]
                else:
                    return
                res = cost_below(r, b, rot, slide, ring, bound)
                if res is not None:
                    best[1] = (area, res[0], n, (pos, rot, top), b, res[1])

        before = measure([(request_box(r, r.pos, r.rot, r.top), r.pipe_id) for r in self.requests],
                         fixed_labels, self.pipe_geoms, self.fixed_geoms)

        # most constrained first: number of preferred slots that are free and cost nothing extra
        for r in self.requests:
            r.n_free = 0
            for (p, rot, top, _) in r.cands:
                b = request_box(r, p, rot, top)
                r.n_free += hard_area(b, full=False) == 0.0 and soft(r, b) == 0.0
        order = sorted(self.requests, key=lambda r: (r.kind != "pipe", r.n_free, r.idx))

        for r in order:
            if time.time() > deadline:                   # time budget used up: leave the rest where they were
                r.result, r.leader, r.status = (r.pos, r.rot, r.top), None, "skipped"
                continue
            best = [None, None]  # best_free (cost, i, cand, box, leader), best_any (area, cost, i, cand, box, leader)
            n = 0
            for (p, rot, top, slide) in r.cands:
                consider(r, best, n, p, rot, top, slide, False)
                n += 1
            if best[0] is None or best[0][0] >= GOOD_ENOUGH:
                w, ht = _dims(r.lines, r.h)
                half_diag = math.hypot(w, ht) / 2
                rots = (r.rot,) if abs(r.rot) < 1e-6 else (r.rot, 0.0)
                ax, ay = r.anchor
                k = 0
                while True:
                    rad = SEARCH_MIN + k * SEARCH_STEP
                    if rad > SEARCH_MAX + 1e-9 or (best[0] is not None and rad - half_diag > best[0][0]):
                        break
                    if time.time() > deadline:           # time budget: keep the best spot found so far
                        break
                    for j in range(SEARCH_DIRECTIONS):
                        a = 2 * math.pi * j / SEARCH_DIRECTIONS
                        cx, cy = ax + rad * math.cos(a), ay + rad * math.sin(a)
                        for rot in rots:
                            dy = -ht / 2 if r.top else ht / 2          # box centre relative to the insertion point
                            s, c = math.sin(math.radians(rot)), math.cos(math.radians(rot))
                            pos = (cx + dy * s, cy - dy * c)
                            consider(r, best, n, pos, rot, r.top, 0.0, True)
                            n += 1
                    k += 1
            if best[0] is not None:
                _, i, cand, b, ld = best[0]
                r.result, r.leader = cand, ld
                r.status = "leader" if ld else ("stayed" if i == 0 else "moved")
            else:                                        # least overlap area within SEARCH_MAX
                area, _, _, cand, b, ld = best[1]
                r.result, r.leader, r.status, r.overlap = cand, ld, "unresolved", area
            texts.add(b)
            if r.leader:
                leaders.add(LineString(r.leader).buffer(0.02))

        # write in creation order, so the output does not depend on the placement order
        for r in self.requests:
            pos, rot, top = r.result
            if r.kind == "pipe":
                W.mtext(r.lines, pos, rot, attach=MTextEntityAlignment.TOP_CENTER if top
                        else MTextEntityAlignment.BOTTOM_CENTER, h=r.h)
            else:
                W.text(r.lines[0], pos, rot, r.h, align=TextEntityAlignment.TOP_CENTER if top
                       else TextEntityAlignment.BOTTOM_CENTER)
            if r.leader:
                msp.add_lwpolyline(list(r.leader), dxfattribs={"layer": data_layer})

        final_boxes = [request_box(r, *r.result) for r in self.requests]
        after = measure(list(zip(final_boxes, (r.pipe_id for r in self.requests))),
                        fixed_labels, self.pipe_geoms, self.fixed_geoms)
        lens = [math.dist(*r.leader) for r in self.requests if r.leader]
        stats = {s: sum(r.status == s for r in self.requests)
                 for s in ("stayed", "moved", "leader", "unresolved", "skipped")}
        # extent of everything written (texts + leaders), so the drawing extents can include moved texts
        xs = [c for b in final_boxes for c in (b.bounds[0], b.bounds[2])] + \
             [p[0] for r in self.requests if r.leader for p in r.leader]
        ys = [c for b in final_boxes for c in (b.bounds[1], b.bounds[3])] + \
             [p[1] for r in self.requests if r.leader for p in r.leader]
        bounds = ((min(xs), min(ys)), (max(xs), max(ys))) if xs else None
        return dict(
            version=2, n=len(self.requests), seconds=round(time.time() - t0, 2), budget=budget, bounds=bounds,
            n_pipe=sum(r.kind == "pipe" for r in self.requests), n_level=sum(r.kind == "level" for r in self.requests),
            before=before, after=after, leaders=len(lens),
            leader_min=round(min(lens), 2) if lens else None,
            leader_median=round(statistics.median(lens), 2) if lens else None,
            unresolved_list=[(" / ".join(r.lines), round(r.result[0][0], 2), round(r.result[0][1], 2),
                              round(r.overlap, 3)) for r in self.requests if r.status == "unresolved"],
            **stats)
