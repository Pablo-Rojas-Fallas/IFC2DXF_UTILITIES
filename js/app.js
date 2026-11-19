// IFC2DXF web - page logic (main thread). The conversion itself runs in js/worker.js.
"use strict";

const APP_VERSION = "0.1.0";
const $ = (id) => document.getElementById(id);
const OPTION_IDS = ["axes_3d", "data_texts", "manholes_as_blocks", "layers_per_system", "declutter", "short_labels",
                    "write_geodata", "text_height", "data_text_scale"];

let worker = null, ready = false, busy = false, defaults = null, versions = null;
let fileLoaded = false;     // the chosen IFC has reached the worker (its "fileSet" reply arrived)
let fileAnnounce = false;   // show the "IFC ready" messages when that reply arrives (not after a silent re-send)
let file = null;            // the chosen IFC (File)
let ai = null;              // {name, bytes} of the loaded AI response, re-sent after a restart
let pending = null;         // action requested before the converter was ready
let running = null;         // command the user is waiting for: "prompt" | "loadAI" | "convert" | "boq"
let engineError = null;     // why the converter could not start (shown instead of waiting forever)
let outputs = {};           // name -> Blob of the last conversion
let boqData = null;         // parsed BOQ data of the current IFC (shown in the iframe, used for the HTML download)
let boqFrameReady = false;  // the dashboard iframe has posted boq-ready
const blobUrls = [];

// ------------------------------------------------------------------ helpers
const mb = (n) => (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + " MB";
function setStatus(msg) { $("status").textContent = msg; }
function setEngine(state, msg) {
  const chip = $("engine");
  chip.classList.toggle("ready", state === "ready");
  chip.classList.toggle("failed", state === "failed");
  chip.querySelector(".i").className = "i " + (state === "ready" ? "i-ok" : state === "failed" ? "i-bad" : "i-loader spin");
  $("engine-text").textContent = msg;
}
function updateButtons() {
  const canUse = !!file && fileLoaded && !busy;
  $("convert").disabled = !canUse;
  $("boq-run").disabled = !canUse || !ready;
  $("boq-download").disabled = busy;
  $("copy-prompt").disabled = !canUse;
  $("ai-input").disabled = busy;
  $("ai-label").setAttribute("aria-disabled", String(busy));
  $("cancel").disabled = !busy;
  $("ai-remove").disabled = busy;
  $("file-input").disabled = busy;
  $("drop").classList.toggle("locked", busy);
}
function setBusy(b) {
  busy = b;
  if (!b) running = null;
  $("run-card").setAttribute("aria-busy", String(b));
  updateButtons();
}
function icon(name) { const s = document.createElement("span"); s.className = "i i-" + name; s.setAttribute("aria-hidden", "true"); return s; }

// ------------------------------------------------------------------ worker
function engineFailed(msg) {
  engineError = msg;
  pending = null;
  setEngine("failed", "The converter could not start");
  setBusy(false); $("bar").value = 0; boqProgressEnd();
  setStatus(`The converter could not start: ${msg}. Reload the page to try again; if it keeps failing, ` +
            "your browser or network may be blocking part of the site.");
}

function startWorker() {
  ready = false; engineError = null; fileLoaded = false;
  setEngine("loading", "Preparing the converter… (first visit: about 36 MB)");
  worker = new Worker("js/worker.js");
  worker.onmessage = (ev) => onMessage(ev.data);
  worker.onerror = (ev) => engineFailed(ev.message || "the converter script did not load");
  worker.postMessage({ cmd: "init" });
}

async function sendFile() {
  const buffer = await file.arrayBuffer();
  worker.postMessage({ cmd: "setFile", name: file.name, buffer }, [buffer]);
}
function sendAI() {
  const copy = ai.bytes.slice(0);
  worker.postMessage({ cmd: "loadAI", name: ai.name, buffer: copy.buffer }, [copy.buffer]);
}

function onMessage(m) {
  switch (m.type) {
    case "loading": setEngine("loading", m.msg); break;
    case "ready": {
      ready = true; defaults = m.defaults; versions = m.versions;
      setEngine("ready", "Converter ready");
      updateButtons();
      if (!$("o-data_texts").dataset.init) applyOptions(defaults);
      (async () => {
        if (file) await sendFile();
        if (ai) sendAI();
        if (pending) { const p = pending; pending = null; p(); }
      })();
      break;
    }
    case "fileSet":
      if (!file || m.name !== file.name) break;     // reply for a file that has since been replaced
      fileLoaded = true;
      if (fileAnnounce) {
        fileAnnounce = false;
        setBoqStatus("Press BOQ Dashboard to read the quantities.");
        setStatus("IFC selected. Optionally run the AI analysis, check the options and press Generate DXF.");
      }
      updateButtons();
      break;
    case "progress":
      if (running === "boq") { $("boq-bar").removeAttribute("value"); setBoqStatus(m.msg); break; }
      if (m.frac != null) $("bar").value = Math.round(m.frac * 100);
      else $("bar").removeAttribute("value");
      setStatus(m.msg);
      break;
    case "boq": onBoq(m.json); break;
    case "prompt": onPrompt(m.text); break;
    case "ai":
      if (running === "loadAI") { setBusy(false); onAI(m); }   // re-sent silently after a restart: no message
      break;
    case "aiCleared": break;
    case "done": onDone(m); break;
    case "error":
      if (m.cmd === "init") { engineFailed(m.msg); break; }
      if (m.cmd === "setFile") setBoqStatus("The IFC could not be loaded: " + m.msg);
      if (m.cmd === "boq") { setBusy(false); boqProgressEnd(); setBoqStatus("The BOQ could not be built: " + m.msg); break; }
      setBusy(false); $("bar").value = 0;
      setStatus("Error: " + m.msg);
      break;
  }
}

// run an action now, or as soon as the converter is ready
function whenReady(action, waitingMsg) {
  if (ready) return action();
  if (engineError) {
    setStatus(`Not possible: the converter could not start (${engineError}). Reload the page to try again.`);
    return;
  }
  pending = action;
  setBusy(true);
  setStatus(waitingMsg + " It will start as soon as the converter is ready.");
}

// ------------------------------------------------------------------ 1. IFC
async function pickFile(f) {
  if (!f) return;
  if (!/\.ifc$/i.test(f.name)) { setStatus(`"${f.name}" is not an .ifc file.`); return; }
  file = f;
  $("file-name").textContent = `${f.name} – ${mb(f.size)}`;
  $("file-info").hidden = false;
  $("out-name").value = f.name.replace(/\.ifc$/i, "") + ".dxf";
  $("result").hidden = true; $("bar").value = 0;
  clearBoq();
  fileLoaded = false; fileAnnounce = true;     // the buttons stay grey until the worker has the file
  const waitMsg = ready ? "Loading the IFC…" : "Loading the IFC… It is passed to the converter as soon as it is ready.";
  setBoqStatus(waitMsg);
  setStatus(waitMsg);
  updateButtons();
  if (ready) await sendFile();
}

// ------------------------------------------------------------------ 2. AI
function copyPrompt() {
  whenReady(() => {
    setBusy(true); running = "prompt"; $("prompt-box").hidden = true;
    setStatus("Reading the IFC to build the prompt…");
    worker.postMessage({ cmd: "prompt", name: file.name });
  }, "Preparing the prompt.");
}
async function onPrompt(text) {
  setBusy(false); $("bar").value = 0;
  $("prompt-text").value = text;
  try {
    await navigator.clipboard.writeText(text);
    setStatus(`Prompt copied (${text.length.toLocaleString("en")} characters). Paste it into the Claude chat and send it.`);
  } catch (e) {
    $("prompt-box").hidden = false;
    $("prompt-text").focus(); $("prompt-text").select();
    setStatus("The prompt is ready below. Copy it and paste it into the Claude chat.");
  }
}
async function loadAI(f) {
  if (!f) return;
  ai = { name: f.name, bytes: new Uint8Array(await f.arrayBuffer()) };
  whenReady(() => { setBusy(true); running = "loadAI"; setStatus("Reading the AI response…"); sendAI(); },
            "Reading the AI response.");
}
function onAI(m) {
  $("ai-reading").textContent = m.reading;
  $("ai-details").hidden = false;
  if (!m.usable) {
    ai = null;
    $("ai-state").textContent = `Could not use ${m.name} (${m.status}) – automatic mode. The reason is shown below.`;
    $("ai-remove").hidden = true;
    setStatus("The AI response could not be used; the converter will work in automatic mode.");
    return;
  }
  const st = m.status === "ok" ? "read correctly" : `read with ${m.repaired} automatic repair${m.repaired !== 1 ? "s" : ""}`;
  $("ai-state").textContent = `Loaded: ${m.name} – ${st}, ${m.ignored} item${m.ignored !== 1 ? "s" : ""} discarded. ` +
    "It will be cross-checked against the IFC during conversion.";
  $("ai-remove").hidden = false;
  const changes = [];     // pre-tick the options the AI recommends; the user can still change them
  if (m.axis_mode === "2D" || m.axis_mode === "3D") { $("o-axes_3d").checked = m.axis_mode === "3D"; changes.push(`axes ${m.axis_mode}`); }
  if (m.layers === "per_system" || m.layers === "single") {
    $("o-layers_per_system").checked = m.layers === "per_system";
    changes.push(m.layers === "per_system" ? "layers per system" : "single set of layers");
  }
  setStatus("AI analysis loaded." + (changes.length ? ` Options set from its recommendations: ${changes.join(", ")}.` : "") +
            (m.notes ? ` AI notes: ${m.notes}` : ""));
}
function removeAI() {
  ai = null;
  if (ready) worker.postMessage({ cmd: "clearAI" });
  $("ai-state").textContent = "No AI analysis loaded – automatic mode.";
  $("ai-remove").hidden = true; $("ai-details").hidden = true; $("ai-input").value = "";
  setStatus("AI analysis removed: automatic mode.");
}

// ------------------------------------------------------------------ 3. options
function applyOptions(d) {
  for (const k of OPTION_IDS) {
    const el = $("o-" + k);
    if (el.type === "checkbox") el.checked = !!d[k];
    else el.value = [...el.options].find((o) => Number(o.value) === Number(d[k]))?.value ?? el.options[0].value;
  }
  $("o-data_texts").dataset.init = "1";
}
function readOptions() {
  const o = { overwrite: true };    // the browser always writes into its own empty temporary folder
  for (const k of OPTION_IDS) {
    const el = $("o-" + k);
    o[k] = el.type === "checkbox" ? el.checked : Number(el.value);
  }
  return o;
}
function outName() {
  let n = ($("out-name").value || "").trim().replace(/[\\/:*?"<>|]+/g, "_");
  if (!n) n = file.name.replace(/\.ifc$/i, "");
  if (!/\.dxf$/i.test(n)) n += ".dxf";
  $("out-name").value = n;
  return n;
}

// ------------------------------------------------------------------ 4. convert
function convert() {
  const options = readOptions(), name = outName();
  whenReady(() => {
    setBusy(true); running = "convert"; $("result").hidden = true; $("bar").value = 0;
    setStatus("Converting…");
    worker.postMessage({ cmd: "convert", name: file.name, outName: name, options });
  }, "Conversion requested.");
}
function cancel() {
  const wasBoq = running === "boq";
  worker.terminate();
  pending = null;
  setBusy(false); $("bar").value = 0; boqProgressEnd();
  if (wasBoq) setBoqStatus("Cancelled. Press BOQ Dashboard to try again.");
  setStatus(wasBoq ? "Cancelled. Restarting the converter…" : "Cancelled. No DXF was written. Restarting the converter…");
  startWorker();
}

function onDone(m) {
  setBusy(false);
  for (const u of blobUrls.splice(0)) URL.revokeObjectURL(u);
  const r = m.result;
  if (!r.ok) {
    $("bar").value = 0;
    setStatus("The conversion failed: " + r.error);
    return;
  }
  $("bar").value = 100;
  renderBadge(r.georef);
  renderSummary(r, m.seconds);
  const dl = $("downloads"); dl.replaceChildren();
  outputs = {};
  if (window.showDirectoryPicker) {     // Edge / Chrome: write all files into a folder the user chooses
    const all = document.createElement("button");
    all.type = "button"; all.className = "btn primary";
    all.append(icon("download"), document.createTextNode("Save all to a folder…"));
    all.addEventListener("click", saveAllToFolder);
    dl.append(all);
  }
  for (const [name, bytes] of Object.entries(m.files)) {
    const isDxf = /\.dxf$/i.test(name);
    const blob = new Blob([bytes], { type: isDxf ? "application/dxf" : "text/plain" });
    outputs[name] = blob;
    const url = URL.createObjectURL(blob);
    blobUrls.push(url);
    const a = document.createElement("a");
    a.className = "btn " + (isDxf && !window.showDirectoryPicker ? "primary" : "secondary");
    a.href = url; a.download = name;
    a.append(icon("download"), document.createTextNode(isDxf ? `Save DXF (${name})` : `Save ${name}`));
    if (window.showSaveFilePicker) {    // ask where to save; plain download (Downloads folder) as fallback
      a.addEventListener("click", (e) => { e.preventDefault(); saveOne(name); });
    }
    dl.append(a);
  }
  const hint = document.createElement("p");
  hint.className = "help";
  hint.textContent = window.showSaveFilePicker
    ? "You choose the folder and name in the Save window."
    : "This browser saves to your Downloads folder (change it in the browser's download settings).";
  dl.append(hint);
  $("report").textContent = r.report;
  $("result").hidden = false;
  const miss = r.missing.length ? ` Warning: ${r.missing.length} element(s) missing or drawn from their axis only.` : "";
  setStatus(`Done in ${m.seconds.toFixed(0)} s. The DXF is ready to save.` + miss);
  $("result").focus();
}

async function writeBlob(handle, blob) {
  const w = await handle.createWritable();
  await w.write(blob);
  await w.close();
}
async function saveOne(name) {
  const ext = name.slice(name.lastIndexOf("."));
  try {
    const handle = await window.showSaveFilePicker({ suggestedName: name, id: "ifc2dxf-out",
      types: [{ description: ext === ".dxf" ? "DXF drawing" : "Text file", accept: { [ext === ".dxf" ? "application/dxf" : "text/plain"]: [ext] } }] });
    await writeBlob(handle, outputs[name]);
    setStatus(`Saved ${handle.name}.`);
  } catch (e) {
    if (e.name !== "AbortError") setStatus(`Could not save ${name}: ${e.message}`);
  }
}
async function saveAllToFolder() {
  let dir;
  try { dir = await window.showDirectoryPicker({ id: "ifc2dxf-out", mode: "readwrite" }); }
  catch (e) { if (e.name !== "AbortError") setStatus("Could not open the folder: " + e.message); return; }
  const names = Object.keys(outputs), existing = [];
  for (const n of names) {
    try { await dir.getFileHandle(n); existing.push(n); } catch (e) { /* not there: fine */ }
  }
  if (existing.length && !confirm(`These files already exist in "${dir.name}":\n\n${existing.join("\n")}\n\nOverwrite them?`)) {
    setStatus("Nothing saved: the existing files were kept.");
    return;
  }
  try {
    for (const n of names) await writeBlob(await dir.getFileHandle(n, { create: true }), outputs[n]);
    setStatus(`Saved ${names.length} files to the folder "${dir.name}".`);
  } catch (e) {
    setStatus("Could not save to that folder: " + e.message);
  }
}

function renderBadge(g) {
  const b = $("badge"); b.replaceChildren(); b.className = "badge";
  let cls, ic, title, note;
  if (!g) {
    cls = "bad"; ic = "bad"; title = "Georeferencing could not be checked.";
    note = "See the report for details.";
  } else if (g.assured && g.confidence === "high") {
    cls = "ok"; ic = "ok"; title = `Georeferenced: ${g.crs_name || "EPSG:" + g.epsg} (EPSG:${g.epsg})`;
    note = `The coordinates are real map coordinates. In AutoCAD assign ${g.autodesk || "EPSG:" + g.epsg} without ` +
           "moving or scaling anything. A .prj file is included.";
  } else if (g.assured) {
    cls = "warn"; ic = "warn"; title = `Probably georeferenced: ${g.crs_name || ""} (EPSG:${g.epsg}), confidence ${g.confidence}`;
    note = "Check the georeferencing section and the warnings of the report before using the drawing.";
  } else {
    cls = "bad"; ic = "bad"; title = "Not georeferenced: the DXF keeps the local coordinates of the IFC.";
    note = "The IFC did not confirm real map coordinates, so no .prj file was written.";
  }
  b.classList.add(cls);
  const txt = document.createElement("span");
  txt.append(document.createTextNode(title));
  const small = document.createElement("small"); small.textContent = note; txt.append(small);
  b.append(icon(ic), txt);
}

function renderSummary(r, seconds) {
  const ul = $("summary"); ul.replaceChildren();
  const li = (text, warn) => { const e = document.createElement("li"); e.textContent = text; if (warn) e.className = "warnline"; ul.append(e); };
  const k = r.kinds || {};
  const names = { pipe: "pipes", manhole: "manholes", accessory: "accessories", unclassified: "unclassified elements" };
  const parts = Object.entries(names).filter(([key]) => k[key]).map(([key, n]) => `${k[key]} ${n}`);
  li("Converted: " + (parts.join(", ") || "nothing") + ".");
  const lp = r.label_placement;
  if (!r.data_texts) li("Data texts: off (no labels written).");
  else if (lp) {
    li(`Labels: ${lp.n} placed – kept ${lp.stayed}, moved ${lp.moved}, with a leader line ${lp.leader}, ` +
       `not resolved ${lp.unresolved}.`, lp.unresolved > 0);
    if (lp.skipped) li(`Label placement stopped after ${lp.budget} s: ${lp.skipped} texts left at their original ` +
                       "position (they may overlap). Try a smaller data text size or short pipe labels.", true);
  } else li("Label placement: off.");
  if (r.missing.length) {
    li(`${r.missing.length} element(s) missing from the DXF or drawn from their axis only (no 3D body in the IFC):`, true);
    for (const [what, who] of r.missing.slice(0, 10)) li(`${who}: ${what}`, true);
    if (r.missing.length > 10) li(`… and ${r.missing.length - 10} more – see MISSING DATA in the report.`, true);
  }
  if (r.recovered) li(`${r.recovered} element(s) needed a geometry retry and were drawn normally.`);
  li(r.warnings ? `${r.warnings} warning(s): read them in the report below.` : "No warnings.", r.warnings > 0);
  li(`Time: ${seconds.toFixed(0)} s.`);
}

// ------------------------------------------------------------------ 2. BOQ Dashboard
const boqFrame = $("boq-frame");
function setBoqStatus(msg) { $("boq-status").textContent = msg; }
function boqProgressEnd() { $("boq-bar").hidden = true; $("boq-bar").value = 0; }
function frameOrigin() { return location.origin && location.origin !== "null" ? location.origin : "*"; }
function postToFrame(msg) { if (boqFrame.contentWindow) boqFrame.contentWindow.postMessage(msg, frameOrigin()); }
function sendBoqToFrame() {   // only once the viewer has said it is ready; otherwise boq-ready triggers this again
  if (!boqData || !boqFrameReady) return;
  postToFrame({ type: "boq-data", data: boqData, theme: currentTheme() });
  boqFrame.hidden = false; $("boq-empty").hidden = true;
}
function clearBoq() {         // new IFC: forget the data and reload an empty viewer
  boqData = null; boqFrameReady = false;
  boqFrame.hidden = true; $("boq-empty").hidden = false;
  boqFrame.src = "boq/viewer.html";
  $("boq-summary").hidden = true; $("boq-summary").textContent = "";
  $("boq-download").hidden = true;
  boqProgressEnd();
}
window.addEventListener("message", (ev) => {
  if (ev.source !== boqFrame.contentWindow || !ev.data || ev.data.type !== "boq-ready") return;
  boqFrameReady = true;
  sendBoqToFrame();
});

function runBoq() {
  whenReady(() => {
    setBusy(true); running = "boq";
    $("boq-bar").hidden = false; $("boq-bar").removeAttribute("value");
    setBoqStatus("Reading the IFC properties…");
    worker.postMessage({ cmd: "boq", name: file.name });
  }, "BOQ requested.");
}

// Same reading as boq/boq.js: pipes have unit "m" or a pipe word in KEY-A04; manhole parts sharing a ManholeName
// count once (GlobalId when it is missing); codes are the distinct KEY-A03 values.
function boqSummary(d) {
  const clean = (v) => v == null ? null :
    String(v).replace(/\\X\\0D\\X\\0A/g, " ").replace(/\\X\\0[AD]\\/g, " ").replace(/\s+/g, " ").trim();
  let pipeM = 0, segments = 0;
  const manholes = new Set(), codes = new Set();
  for (const e of d.elements) {
    const u = clean(e.u), a4 = clean(e.a4) || "";
    codes.add(clean(e.a3));
    if (u === "m" || /ledning|pipe|rør/i.test(a4)) { pipeM += parseFloat(e.q) || 0; segments++; }
    else manholes.add(clean(e.mh) || e.g);
  }
  return { pipeM, segments, manholes: manholes.size, codes: codes.size };
}

function onBoq(json) {
  setBusy(false); boqProgressEnd();
  try { boqData = JSON.parse(json); }
  catch (e) { boqData = null; setBoqStatus("The BOQ data could not be read: " + e.message); return; }
  const s = boqSummary(boqData);
  const nf1 = new Intl.NumberFormat("en-GB", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  $("boq-summary").textContent = `${nf1.format(s.pipeM)} m of pipe (${s.segments} segments) · ${s.manholes} manholes · ` +
                                 `${s.codes} codes`;
  $("boq-summary").hidden = false;
  $("boq-download").hidden = false;
  setBoqStatus(boqData.elements.length ? "BOQ ready: see the dashboard." :
               "No elements with a BOQ code (KEY-A03) were found in this IFC.");
  sendBoqToFrame();
}

async function downloadBoq() {
  if (!boqData) return;
  try {
    const html = await window.BOQStandalone.build(boqData);
    const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    const a = document.createElement("a");
    a.href = url; a.download = "BOQ_" + (file ? file.name : boqData.file || "model").replace(/\.ifc$/i, "") + ".html";
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    setBoqStatus(`Saved ${a.download}.`);
  } catch (e) {
    setBoqStatus("Could not build the HTML file: " + e.message);
  }
}

// ------------------------------------------------------------------ theme
function currentTheme() {
  return document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function showThemeButton() {
  const dark = currentTheme() === "dark";
  const btn = $("theme");
  btn.setAttribute("aria-pressed", String(dark));
  btn.querySelector(".i").className = "i " + (dark ? "i-sun" : "i-moon");
  btn.querySelector(".sr-only").textContent = "Dark mode";
  btn.title = dark ? "Switch to light mode" : "Switch to dark mode";
}
$("theme").addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("ifc2dxf-theme", next); } catch (e) { /* not stored: fine */ }
  showThemeButton();
  postToFrame({ type: "boq-theme", theme: next });
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  showThemeButton();
  postToFrame({ type: "boq-theme", theme: currentTheme() });
});

// ------------------------------------------------------------------ wiring
$("file-input").addEventListener("change", (e) => pickFile(e.target.files[0]));
const drop = $("drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); pickFile(e.dataTransfer.files[0]); });
document.addEventListener("dragover", (e) => e.preventDefault());           // a drop outside the zone must not open the file
document.addEventListener("drop", (e) => { if (!drop.contains(e.target)) e.preventDefault(); });
$("copy-prompt").addEventListener("click", copyPrompt);
$("prompt-copy-again").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("prompt-text").value); setStatus("Prompt copied."); }
  catch (e) { $("prompt-text").select(); setStatus("Press Ctrl+C to copy the selected prompt."); }
});
$("ai-input").addEventListener("change", (e) => loadAI(e.target.files[0]));
$("ai-remove").addEventListener("click", removeAI);
$("reset-options").addEventListener("click", () => { if (defaults) { applyOptions(defaults); setStatus("Options restored to the defaults."); } });
$("convert").addEventListener("click", convert);
$("boq-run").addEventListener("click", runBoq);
$("boq-download").addEventListener("click", downloadBoq);
$("cancel").addEventListener("click", cancel);
// the Claude logo is shown only if the site owner placed assets/claude.png; otherwise the chat icon stays
const logo = $("claude-logo");
logo.addEventListener("load", () => { logo.hidden = false; $("claude-fallback").hidden = true; });
logo.addEventListener("error", () => { logo.remove(); });
if (logo.complete && logo.naturalWidth) { logo.hidden = false; $("claude-fallback").hidden = true; }

$("app-version").textContent = APP_VERSION;
showThemeButton();
updateButtons();
startWorker();
