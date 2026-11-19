# IFC2DXF web

Convert IFC pipe networks (water, sewer, stormwater, drainage, cable ducts) to a **georeferenced DXF** for AutoCAD /
Civil 3D – entirely in your browser.

**Your files never leave your computer.** The conversion runs inside the browser tab with Python compiled to
WebAssembly ([Pyodide](https://pyodide.org)). Nothing is uploaded, nothing is stored, and the site makes no requests
to other servers: every script, library and icon is served from this repository.

## Using it

1. **IFC file** – drop an `.ifc` file or choose one.
2. **BOQ Dashboard** – reads the quantities by V770 code (KEY-A03) from the IFC properties and shows them in the
   dashboard on the right: pipe length, manholes (parts grouped by ManholeName), codes, roads and a code/description
   check. *Download HTML* saves the dashboard as a single self-contained file.
3. **Options** – the same options and defaults as desktop IFC2DXF 0.4.0. At the top, the optional **AI analysis**:
   *Open Claude*, *Copy prompt* (a summary of the IFC without personal data), paste it into the chat, save the answer
   as `.json` and *Load AI response*. Without it the converter works in automatic mode.
4. **Generate DXF** – then download the DXF, the `.prj` (coordinate system) and the conversion report.

The result shows a georeferencing badge: **green** = real map coordinates confirmed (assign the coordinate system in
AutoCAD without moving anything), **amber** = probably georeferenced, check the report, **red** = local coordinates.

The first visit downloads about 36 MB (Python and the geometry libraries); the browser caches it afterwards.
Edge and Chrome (desktop) are supported.

## Running it locally

```
python -m http.server 8000
```

in this folder, then open http://localhost:8000/. Any static web server works (GitHub Pages included); no build
step is needed.

## Repository layout

| Path | Contents |
|---|---|
| `index.html`, `css/`, `js/` | the app (plain HTML, CSS and JavaScript; Python runs in `js/worker.js`) |
| `boq/` | the BOQ Dashboard viewer (shown in the app in an iframe) and the builder of the downloadable HTML |
| `py/ifc2dxf/` | the conversion engine: copy of desktop IFC2DXF 0.4.0, differences listed in `py/CHANGES.md` |
| `vendor/` | Pyodide 0.29.5 and the Python packages (wheels), unmodified |
| `icons/` | Lucide icons (ISC) |
| `assets/claude.png` | optional: the Claude logo for the *Open Claude* button, supplied by the site owner. If it is missing, a generic chat icon is shown. The logo is never drawn or recreated by this project. |
| `licences.html`, `LICENSES/` | licences of all bundled components |
| `poc/`, `tools/` | Phase 0 proof of concept and test tools (not needed to run the app) |

## Licences

IFC2DXF web bundles open-source components under their own licences (MPL-2.0, LGPL-3.0, LGPL-2.1, MIT,
BSD-3-Clause, PSF, ISC, …); see [licences.html](licences.html) and `LICENSES/`. Coordinate system data: EPSG
Geodetic Parameter Dataset (IOGP), via PROJ.

## Trademarks

Claude is a trademark of Anthropic, PBC. This project is independent and is not affiliated with or endorsed by
Anthropic; the *Open Claude* button only opens claude.ai in a new tab. AutoCAD and Civil 3D are trademarks of
Autodesk, Inc.
