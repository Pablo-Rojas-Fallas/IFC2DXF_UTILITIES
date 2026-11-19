// IFC2DXF web - builds the downloadable, self-contained BOQ Dashboard HTML (CSS + data + renderer inlined).
"use strict";
window.BOQStandalone = {
  async build(data) {
    const base = new URL("boq/", document.baseURI);
    const [css, js] = await Promise.all(["boq.css", "boq.js"].map(async (f) => {
      const r = await fetch(new URL(f, base));
      if (!r.ok) throw new Error("Cannot load boq/" + f);
      return r.text();
    }));
    const json = JSON.stringify(data).replace(/</g, "\\u003c");
    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BOQ Dashboard · ${esc(data.file || "")}</title>
<style>
${css}</style>
</head>
<body class="viz-root">
<script>window.BOQ_DATA = ${json};</script>
<script>
${js.replace(/<\/script/gi, "<\\/script")}</script>
</body>
</html>
`;
  },
};
