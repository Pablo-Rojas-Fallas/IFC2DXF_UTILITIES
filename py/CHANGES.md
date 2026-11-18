# Differences between `py/ifc2dxf/` (web) and desktop IFC2DXF 0.4.0

`py/ifc2dxf/` is the desktop `ifc2dxf` package of version 0.4.0, copied file by file (the desktop GUI `app.py` is not
part of it). Only `ifc_reader.py` differs (one change, marked in the code with `WEB COPY CHANGE 1`).

| # | File / function | Desktop | Web | Why |
|---|---|---|---|---|
| 1 | `ifc_reader.py`, `triangulate()` | several threads | one thread | The browser (Pyodide) has no threads. |

The georeferencing code is unchanged and uses the real **pyproj** (3.7.2, PROJ 9.6.2, full EPSG database).

## Known limitation (IfcOpenShell 0.8.5 in the browser)

IFCs whose 3D model context has a shifted origin (`IfcGeometricRepresentationContext.WorldCoordinateSystem` not at
0,0 – e.g. files re-saved by Bonsai with a false origin) are not handled like the desktop: the 3D bodies (pipe
outlines, manholes) can come out misplaced (in the Phase 0 test, rotated 180° about the origin) while the axes and
texts are right. Tested without success: one `create_shape` per element instead of the iterator, and passing the
solid representation explicitly (0 vertices). The app (`js/worker.js`, `web_context_offset`) detects such files
and shows a red warning in the result. To be solved with a newer IfcOpenShell WebAssembly build (0.9).

## Library versions

| Library | Desktop 0.4.0 | Web | Note |
|---|---|---|---|
| Python | 3.13 | 3.13.2 (Pyodide 0.29.5) | |
| IfcOpenShell | 0.9.0.post1 | **0.8.5** | The 0.9 WebAssembly builds do not work in Pyodide yet (0.9.0.post1 has no schemas or geometry kernel inside; the modular 0.9.1 builds fail in the geometry plugins). |
| ezdxf | 1.4.5 | 1.4.5 | |
| json-repair | 0.64.9 | 0.64.9 | |
| shapely / GEOS | 2.2.0 | 2.0.7 / 3.12.1 | |
| numpy | 2.5.3 | 2.2.5 | |
| pyproj / PROJ | 3.8.0 | 3.7.2 / 9.6.2 | |

When the desktop package changes, copy it again and re-apply the changes above.
