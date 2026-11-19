# Differences between `py/ifc2dxf/` (web) and desktop IFC2DXF 0.4.0

`py/ifc2dxf/` is the desktop `ifc2dxf` package of version 0.4.0, copied file by file (the desktop GUI `app.py` is not
part of it). Two files differ; every change is marked in the code with `WEB COPY CHANGE n`.

| # | File / function | Desktop | Web | Why |
|---|---|---|---|---|
| 1 | `ifc_reader.py`, `triangulate()` | several threads | one thread | The browser (Pyodide) has no threads. |
| 2 | `geometry.py`, `footprint()` → `_union()` | `unary_union(triangles)` | same, and only if GEOS raises an error: `union_all(..., grid_size=0.001 mm)`, then 0.1 mm | The browser's shapely 2.0.7 / GEOS 3.12.1 raised "TopologyException: Ring edge missing" on an electrical IFC; outlines that merge without error are identical to the desktop. |

`boq.py` added (web only): reads the BOQ properties (SB_Data KEY-A03/A04/A05, GEN-B03/B04, Novapoint ManholeName) for
the BOQ Dashboard step. It is not part of desktop IFC2DXF 0.4.0 and is not used by the DXF conversion.

The georeferencing code is unchanged and uses the real **pyproj** (3.7.2, PROJ 9.6.2, full EPSG database).

## Why IfcOpenShell 0.8.4

| Build tested in Pyodide 0.29.5 (Edge 157) | Result |
|---|---|
| 0.9.0.post1 | contains no IFC schemas and no geometry kernel: cannot open any IFC |
| 0.9.1 modular (a963c38, 2be812f) | IFC opens; geometry fails ("Incompatible plugin ABI") |
| 0.8.6 modular (b1899b1) | WebAssembly crash on opening the IFC |
| 0.8.5 | pipe bodies (swept disk solids) come out with x and y negated (e.g. –707.8, –551.3 instead of 700.9, 547.7); wrong outlines in 2 of 3 test IFCs |
| **0.8.4** | **matches the desktop**: pipe outlines within about 1 mm on the test IFCs |

## Library versions

| Library | Desktop 0.4.0 | Web | Note |
|---|---|---|---|
| Python | 3.13 | 3.13.2 (Pyodide 0.29.5) | |
| IfcOpenShell | 0.9.0.post1 | **0.8.4** | see "Why IfcOpenShell 0.8.4" above |
| ezdxf | 1.4.5 | 1.4.5 | |
| json-repair | 0.64.9 | 0.64.9 | |
| shapely / GEOS | 2.2.0 | 2.0.7 / 3.12.1 | |
| numpy | 2.5.3 | 2.2.5 | |
| pyproj / PROJ | 3.8.0 | 3.7.2 / 9.6.2 | |

When the desktop package changes, copy it again and re-apply the changes above.
