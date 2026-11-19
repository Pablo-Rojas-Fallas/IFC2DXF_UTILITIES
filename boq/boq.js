// IFC2DXF web - BOQ Dashboard renderer. Used in two ways:
//  1. inside boq/viewer.html (iframe in the app): data arrives by postMessage {type:"boq-data", data, theme}
//  2. inside the downloaded standalone HTML: window.BOQ_DATA is set before this script runs.
"use strict";
(() => {
const MARKUP = `<div class="wrap">
  <h1>BOQ Dashboard · Quantities by V770 code</h1>
  <div class="sub" id="sub"></div>
  <div class="meta" id="meta"></div>

  <div class="filters">
    <label class="f">Road (KEY-A05)<select id="fRoad"></select></label>
    <label class="f">Category<select id="fCat"><option value="">All</option><option value="pipe">Pipes</option><option value="mh">Manholes / chambers</option></select></label>
    <div class="f">Manhole count
      <div class="seg" id="fMode"><button data-m="phys" class="on">Physical units</button><button data-m="raw">Raw GEN-B03 sum</button></div>
    </div>
    <div class="spacer"></div>
    <button id="csvBtn">Export CSV</button>
    <button id="themeBtn">Dark / light</button>
  </div>

  <div class="kpis" id="kpis"></div>

  <h2>Pipes</h2>
  <div class="grid">
    <div class="card"><h3>Length by diameter and material</h3><div class="cap">Diameter and material parsed from KEY-A04 · quantity GEN-B03 (m)</div><div id="chPipeDia"></div></div>
    <div class="card"><h3>Length by pipe type</h3><div class="cap">Type parsed from KEY-A04</div><div id="chPipeType"></div></div>
  </div>

  <h2>Manholes and chambers</h2>
  <div class="grid">
    <div class="card"><h3>Units by type and diameter</h3><div class="cap" id="capMh"></div><div id="chMh"></div></div>
    <div class="card"><h3>Units by diameter and material</h3><div class="cap">Diameter and material parsed from KEY-A04</div><div id="chMhDia"></div></div>
  </div>

  <h2>By road (KEY-A05)</h2>
  <div class="grid">
    <div class="card"><h3>Pipe length by road</h3><div class="cap">GEN-B03 (m)</div><div id="chRoadM"></div></div>
    <div class="card"><h3>Manholes / chambers by road</h3><div class="cap" id="capRoadU"></div><div id="chRoadU"></div></div>
  </div>

  <h2>Bill of quantities</h2>
  <div class="card wide">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
      <div class="cap" style="margin:0">One line per KEY-A03 code. Click a column header to sort.</div>
      <label class="f" style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" id="fSplit"> Break down by road</label>
    </div>
    <div class="tbl"><table id="boq"></table></div>
  </div>

  <h2>How KEY-A04 was parsed</h2>
  <div class="card wide"><div class="cap">One line per code. Shows how each description is split into fields.</div><div class="tbl"><table class="parse" id="parseTbl"></table></div></div>

  <h2>Data quality</h2>
  <div class="issues" id="issues"></div>

  <footer id="foot"></footer>
</div>
<div id="tip"></div>`;

function setTheme(t){ if (t === "dark" || t === "light") document.documentElement.dataset.theme = t; }

function render(D){
document.body.className = "viz-root";
document.body.innerHTML = MARKUP;

const nf0 = new Intl.NumberFormat("en-GB",{maximumFractionDigits:0});
const nf1 = new Intl.NumberFormat("en-GB",{minimumFractionDigits:1,maximumFractionDigits:1});
const fmtQ = (q,u) => u === "m" ? nf1.format(q) : nf0.format(q);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---------- 1. Normalise IFC strings ----------
   Removes STEP-encoded line breaks (\X\0D\X\0A), collapses whitespace, trims. */
const clean = s => s == null ? null :
  String(s).replace(/\\X\\0D\\X\\0A/g, " ").replace(/\\X\\0[AD]\\/g, " ").replace(/\s+/g, " ").trim();

/* ---------- 2. Parse KEY-A04 ----------
   "Overvannsledning-200mm-Polypropylene(PP)-DV-O-SN8 [Surface-water-pipe-200mm-Polypropylen(PP)-DV-O-SN8]"
   Norwegian part before "[", English part inside "[...]". */
const MAT = {
  "PP":      {name:"Polypropylene (PP)",       grp:"PP"},
  "BTG":     {name:"Concrete (BTG)",           grp:"Concrete"},
  "BTG-IG":  {name:"Concrete IG (BTG-IG)",     grp:"Concrete"},
  "BTG-IG-A":{name:"Concrete IG-A (BTG-IG-A)", grp:"Concrete"}
};
// split on "-" without breaking text inside parentheses: "Betong(BTG-IG)" stays whole
function tokens(s){ const out=[]; let cur="", depth=0;
  for (const ch of s){ if(ch==="(")depth++; if(ch===")")depth--;
    if(ch==="-" && depth===0){ out.push(cur); cur=""; } else cur+=ch; }
  out.push(cur); return out.filter(Boolean);
}
const typeOf = part => { const out=[]; for (const t of tokens(part)){ if (/\(|^\d+\s*mm$/i.test(t)) break; out.push(t); } return out.join("-"); };
const pretty = s => s.replace(/-/g," ").replace(/^./, c=>c.toUpperCase());
function parseA04(s, unit){
  s = s || "";
  let no, en, missingBracket = false;
  const ib = s.indexOf("[");
  if (ib >= 0){ no = s.slice(0, ib).trim(); en = s.slice(ib+1).replace(/\]\s*$/,"").trim(); }
  else { const sp = s.indexOf(" "); no = sp>0 ? s.slice(0,sp) : s; en = sp>0 ? s.slice(sp+1).replace(/\]\s*$/,"").trim() : ""; missingBracket = sp>0; }
  const typeNo = typeOf(no), typeEn = en ? typeOf(en) : "";
  const dia = (no.match(/(\d+)\s*mm/i) || [])[1];
  const matAbbr = (no.match(/\(([^)]+)\)/) || [])[1] || null;
  const cls = (no.match(/\bSN\d+\b/) || [])[0] || null;
  const dw = /(^|-)DV(-|$)/.test(no);
  const isPipe = unit === "m" || /ledning|pipe|rør/i.test(s);
  return {
    no, en, typeNo, type: typeEn ? pretty(typeEn) : pretty(typeNo),
    dia: dia ? +dia : null, matAbbr,
    mat: matAbbr ? (MAT[matAbbr]?.name || matAbbr) : "Not specified",
    matGrp: matAbbr ? (MAT[matAbbr]?.grp || "Other") : "Not specified",
    cls, dw, cat: isPipe ? "pipe" : "mh", missingBracket
  };
}

/* ---------- 3. Normalised records ---------- */
const parseCache = {};
const recs = D.elements.map(e => {
  const a4 = clean(e.a4), u = clean(e.u), code = clean(e.a3);
  const p = parseCache[a4] ||= parseA04(a4, u);
  return { ...p, ifcClass:e.c, code, desc:a4, road:clean(e.a5) || "—", q:parseFloat(e.q) || 0, u,
           mh:clean(e.mh), name:e.n, guid:e.g };
});
// Physical units: the parts of one manhole share ManholeName and are counted once
const physKey = r => r.cat === "mh" ? (r.mh || r.guid) : r.guid;
const groupBy = (rows, f) => { const g = new Map(); rows.forEach(r => { const k=f(r); g.has(k)||g.set(k,[]); g.get(k).push(r); }); return g; };

/* ---------- 4. Code ↔ description consistency ----------
   Rule: one code has exactly one description, and one description has exactly one code. */
const codeToDesc = new Map([...groupBy(recs, r=>r.code)].map(([k,rs]) => [k, groupBy(rs, r=>r.desc)]));
const descToCode = new Map([...groupBy(recs, r=>r.desc)].map(([k,rs]) => [k, groupBy(rs, r=>r.code)]));
const codeMulti = [...codeToDesc].filter(([,m]) => m.size > 1);
const descMulti = [...descToCode].filter(([,m]) => m.size > 1);
const codeWarn = new Set(codeMulti.map(([c])=>c));
const descWarnCodes = new Set(descMulti.flatMap(([,m]) => [...m.keys()]));

/* ---------- State / filters ---------- */
const st = { road:"", cat:"", mode:"phys", split:false, sort:{k:"code",dir:1} };
const roads = [...new Set(recs.map(r=>r.road))].sort();
fRoad.innerHTML = `<option value="">All (${roads.length})</option>` + roads.map(r=>`<option>${esc(r)}</option>`).join("");
fRoad.onchange = e => { st.road = e.target.value; render(); };
fCat.onchange = e => { st.cat = e.target.value; render(); };
fSplit.onchange = e => { st.split = e.target.checked; render(); };
fMode.querySelectorAll("button").forEach(b => b.onclick = () => {
  st.mode = b.dataset.m; fMode.querySelectorAll("button").forEach(x=>x.classList.toggle("on", x===b)); render(); });
themeBtn.onclick = () => { const r=document.documentElement;
  const dark = r.dataset.theme ? r.dataset.theme==="dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  r.dataset.theme = dark ? "light" : "dark"; };

const sumM = rs => rs.reduce((s,r)=>s+r.q,0);
const sumU = rs => st.mode==="phys" ? new Set(rs.map(physKey)).size : rs.reduce((s,r)=>s+r.q,0);

/* ---------- Tooltip ---------- */
const tip = document.getElementById("tip");
const moveTip = e => { tip.style.left = Math.min(e.clientX+14, innerWidth-tip.offsetWidth-8)+"px"; tip.style.top = (e.clientY+14)+"px"; };
function bindTips(root){ root.querySelectorAll("[data-tip]").forEach(el => {
  el.onmouseenter = e => { tip.innerHTML = el.dataset.tip; tip.style.opacity=1; moveTip(e); };
  el.onmousemove = moveTip; el.onmouseleave = () => tip.style.opacity = 0; }); }

/* ---------- Horizontal bars (optionally stacked by material) ---------- */
const SER = { "PP":"var(--series-1)", "Concrete":"var(--series-2)", "Not specified":"var(--series-3)", "Other":"var(--mono)" };
function bars(el, rows, {unit, stacked=false, single="var(--mono)", sort=true}){
  if (!rows.length){ el.innerHTML = `<div class="empty">No data for the current filter.</div>`; return; }
  const fmt = v => unit==="m" ? nf1.format(v)+" m" : nf0.format(v)+" pcs";
  const data = sort ? [...rows].sort((a,b)=>b.total-a.total) : rows;
  const max = Math.max(...data.map(d=>d.total)), tot = data.reduce((s,d)=>s+d.total,0);
  const keys = stacked ? [...new Set(data.flatMap(d=>Object.keys(d.parts)))].sort() : [];
  el.innerHTML = (stacked ? `<div class="legend">${keys.map(k=>`<span><i style="background:${SER[k]||single}"></i>${k}</span>`).join("")}</div>` : "") +
    `<div class="bars">${data.map(d => {
      const segs = stacked ? keys.filter(k=>d.parts[k]).map(k => `<div class="b" style="width:${d.parts[k]/max*100}%;background:${SER[k]||single}"></div>`).join("")
                           : `<div class="b" style="width:${d.total/max*100}%;background:${single}"></div>`;
      const tipH = `<b>${esc(d.label)}</b><br>${fmt(d.total)} · ${(d.total/tot*100).toFixed(1)} %` +
        (stacked ? "<br>" + keys.filter(k=>d.parts[k]).map(k=>`${k}: ${fmt(d.parts[k])}`).join("<br>") : "") + (d.extra ? "<br>"+esc(d.extra) : "");
      return `<div class="row" data-tip="${esc(tipH)}"><div class="l" title="${esc(d.label)}">${esc(d.label)}</div><div class="t">${segs}</div><div class="v">${unit==="m"?nf1.format(d.total):nf0.format(d.total)}</div></div>`;
    }).join("")}</div>`;
  bindTips(el);
}
const byMaterial = (rows, keyF, labF, unitF) => [...groupBy(rows, keyF)].map(([k, rs]) => {
  const parts = {}; for (const [g, gr] of groupBy(rs, r=>r.matGrp)) parts[g] = unitF(gr);
  return { key:k, label:labF(k, rs), total:unitF(rs), parts };
});

function render(){
  const R = recs.filter(r => (!st.road || r.road===st.road) && (!st.cat || r.cat===st.cat));
  const pipes = R.filter(r=>r.cat==="pipe"), mhs = R.filter(r=>r.cat==="mh");
  const m = sumM(pipes), uPhys = new Set(mhs.map(physKey)).size, uRaw = sumM(mhs), u = st.mode==="phys" ? uPhys : uRaw;
  const modeTxt = st.mode==="phys" ? "physical units (parts grouped by ManholeName)" : "raw GEN-B03 sum (1 per IFC part)";
  const nWarn = codeMulti.length + descMulti.length;

  kpis.innerHTML = [
    {lab:"Total pipe length", val:`${nf1.format(m)} <small>m</small>`, note:`${pipes.length} segments · ${new Set(pipes.map(r=>r.code)).size} codes`},
    {lab:"Manholes / chambers", val:`${nf0.format(u)} <small>pcs</small>`, note: st.mode==="phys" ? `${nf0.format(uRaw)} pcs if GEN-B03 is summed per part` : `${nf0.format(uPhys)} actual physical units`},
    {lab:"V770 codes (KEY-A03)", val:nf0.format(new Set(R.map(r=>r.code)).size), note:"distinct BOQ items"},
    {lab:"Roads (KEY-A05)", val:nf0.format(new Set(R.map(r=>r.road)).size), note: st.road ? `Filtered: ${esc(st.road)}` : "with measurable elements"},
    {lab:"Code ↔ description", val: nWarn ? `<span style="color:var(--warn)">${nWarn}</span> <small>warnings</small>` : `<span class="ok">OK</span>`,
     note: nWarn ? "See Data quality" : "1 code = 1 description, both ways"}
  ].map(k=>`<div class="kpi"><div class="lab">${k.lab}</div><div class="val">${k.val}</div><div class="note">${k.note}</div></div>`).join("");

  bars(chPipeDia, byMaterial(pipes, r=>r.dia, k=>`Ø${k} mm`, sumM).sort((a,b)=>a.key-b.key), {unit:"m", stacked:true, sort:false});
  bars(chPipeType, [...groupBy(pipes, r=>r.type)].map(([k,rs])=>({label:k, total:sumM(rs), extra:`${rs.length} segments · ${rs[0].typeNo}`})), {unit:"m"});

  capMh.textContent = "Count: " + modeTxt;
  bars(chMh, [...groupBy(mhs, r=>r.code)].map(([k,rs])=>({label:`${rs[0].type} Ø${rs[0].dia ?? "?"}`, total:sumU(rs), parts:{[rs[0].matGrp]:sumU(rs)},
     extra:`Code ${k} · ${rs.length} IFC parts`})), {unit:"pcs", stacked:true});
  bars(chMhDia, byMaterial(mhs, r=>r.dia, k=>`Ø${k} mm`, sumU).sort((a,b)=>a.key-b.key), {unit:"pcs", stacked:true, sort:false});

  capRoadU.textContent = "Count: " + modeTxt;
  bars(chRoadM, [...groupBy(pipes, r=>r.road)].map(([k,rs])=>({label:k, total:sumM(rs), extra:`${rs.length} segments`})), {unit:"m"});
  bars(chRoadU, [...groupBy(mhs, r=>r.road)].map(([k,rs])=>({label:k, total:sumU(rs)})), {unit:"pcs"});

  /* BOQ table */
  const lines = [...groupBy(R, r=>r.code)].map(([code, rs]) => {
    const r0 = rs[0], unit = r0.u;
    const q = unit==="m" ? sumM(rs) : sumU(rs);
    const subs = st.split ? [...groupBy(rs, r=>r.road)].map(([rd, rr]) => ({road:rd, q: unit==="m"?sumM(rr):sumU(rr), n:rr.length})).sort((a,b)=>a.road.localeCompare(b.road)) : [];
    const warn = [];
    if (codeWarn.has(code)) warn.push(`This code has ${codeToDesc.get(code).size} different descriptions`);
    if (descWarnCodes.has(code)) warn.push("Its description is also used by another code");
    return { code, desc:r0.desc, cat:r0.cat, type:r0.type, dia:r0.dia, mat:r0.mat, matGrp:r0.matGrp, cls:r0.cls || (r0.dw?"DV":""), u:unit, q, n:rs.length, subs, warn };
  });
  const s = st.sort; lines.sort((a,b)=>{ const x=a[s.k], y=b[s.k]; return (typeof x==="number" ? x-y : String(x??"").localeCompare(String(y??""))) * s.dir; });
  const cols = [["code","Code (A03)"],["desc","Description (A04)"],["type","Type"],["dia","Ø mm",1],["mat","Material"],["cls","Class"],["u","Unit (B04)"],["q","Quantity",1],["n","IFC elements",1]];
  const arrow = k => s.k===k ? (s.dir>0?" ▲":" ▼") : "";
  boq.innerHTML = `<thead><tr>${cols.map(([k,l,n])=>`<th data-k="${k}" class="${n?"n":""}">${l}${arrow(k)}</th>`).join("")}</tr></thead><tbody>` +
    lines.map(l => `<tr><td><code>${esc(l.code)}</code>${l.warn.length?`<span class="warnicon" title="${esc(l.warn.join(". "))}">⚠</span>`:""}</td><td class="desc">${esc(l.desc)}</td><td><span class="tag">${l.cat==="pipe"?"Pipe":"Manhole"}</span> ${esc(l.type)}</td>
      <td class="n">${l.dia ?? "—"}</td><td><span class="sw" style="background:${SER[l.matGrp]}"></span>${esc(l.mat)}</td><td>${l.cls||"—"}</td><td>${esc(l.u)}</td>
      <td class="n"><b>${fmtQ(l.q,l.u)}</b></td><td class="n">${l.n}</td></tr>` +
      l.subs.map(sb=>`<tr class="sub"><td colspan="7">Road ${esc(sb.road)}</td><td class="n">${fmtQ(sb.q,l.u)}</td><td class="n">${sb.n}</td></tr>`).join("")).join("") +
    `</tbody><tfoot><tr><td colspan="7">Total</td><td class="n">${nf1.format(m)} m · ${nf0.format(u)} pcs</td><td class="n">${R.length}</td></tr></tfoot>`;
  boq.querySelectorAll("th").forEach(th => th.onclick = () => { const k=th.dataset.k; st.sort = {k, dir: st.sort.k===k ? -st.sort.dir : 1}; render(); });
  window.__lines = lines;
}

/* ---------- Parse table ---------- */
const uniq = [...codeToDesc].flatMap(([c,m]) => [...m.values()].map(rs=>rs[0])).sort((a,b)=>a.code.localeCompare(b.code));
parseTbl.innerHTML = `<thead><tr><th>KEY-A03</th><th>Norwegian text (before “[”)</th><th>Type (EN)</th><th>Category</th><th class="n">Ø mm</th><th>Material</th><th>Class</th><th>Double-walled</th></tr></thead><tbody>` +
  uniq.map(r=>`<tr><td><code>${esc(r.code)}</code></td><td>${esc(r.no)}</td><td>${esc(r.type)}</td><td>${r.cat==="pipe"?"Pipe":"Manhole"}</td><td class="n">${r.dia??"—"}</td><td>${esc(r.matAbbr??"—")}</td><td>${r.cls??"—"}</td><td>${r.dw?"Yes":"—"}</td></tr>`).join("") + `</tbody>`;

/* ---------- Data quality ---------- */
const nCRLF = D.elements.filter(e => /\\X\\0D\\X\\0A|[\r\n]/.test((e.a3||"") + (e.a4||""))).length;
const noBr = [...new Set(recs.filter(r=>r.missingBracket).map(r=>r.code))];
const mhR = recs.filter(r=>r.cat==="mh");
const phys = new Set(mhR.map(physKey)).size;
const noMat = [...new Set(recs.filter(r=>!r.matAbbr).map(r=>r.code))];
const list = items => `<ul>${items.join("")}</ul>`;
const issues = [];
if (codeMulti.length) issues.push({sev:"high", t:`<b>Same code, different descriptions</b> (${codeMulti.length}). Each code must have one fixed description.` +
  list(codeMulti.map(([c,m]) => `<li><code>${esc(c)}</code>: ${[...m].map(([d,rs])=>`“${esc(d)}” (${rs.length} el.)`).join(" · ")}</li>`))});
if (descMulti.length) issues.push({sev:"high", t:`<b>Same description, different codes</b> (${descMulti.length}). Each description must belong to one code.` +
  list(descMulti.map(([d,m]) => `<li>“${esc(d)}”: ${[...m].map(([c,rs])=>`<code>${esc(c)}</code> (${rs.length} el.)`).join(" · ")}</li>`))});
if (!codeMulti.length && !descMulti.length) issues.push({sev:"ok", t:`<b>Code ↔ description check passed.</b> ${codeToDesc.size} codes and ${descToCode.size} descriptions, matched one to one (after normalisation).`});
issues.push({sev:"medium", t:`Manholes are modelled as several IFC parts (cover + body…), and <b>each part carries GEN-B03 = 1 pcs</b> with the same code. Summing GEN-B03 gives ${mhR.length} pcs, but there are <b>${phys} physical units</b>. Parts are grouped by <code>Novapoint UtilityNetwork.ManholeName</code> (or by GlobalId when missing).`});
if (nCRLF) issues.push({sev:"medium", t:`${nCRLF} elements have a trailing line break (CR/LF) in KEY-A03/A04. Values are normalised before grouping, otherwise one code would show up as two BOQ items.`});
if (noBr.length) issues.push({sev:"low", t:`Description without the opening “[” before the English text: ${noBr.map(c=>`<code>${esc(c)}</code>`).join(", ")}. The parser splits at the first space instead.`});
if (noMat.length) issues.push({sev:"low", t:`No material in the description: ${noMat.map(c=>`<code>${esc(c)}</code>`).join(", ")}. Shown as “Not specified”.`});
const excl = D.total_elements - recs.length;
if (excl > 0) issues.push({sev:"low", t:`${nf0.format(excl)} IFC elements have no KEY-A03 (or “IR [N/A]”, e.g. 3D texts) and are excluded from the BOQ.`});
const SEV = {high:"High", medium:"Medium", low:"Low", ok:"OK"};
document.getElementById("issues").innerHTML = issues.map(i=>`<div class="issue"><span class="sev ${i.sev}" ${i.sev==="ok"?'style="color:var(--series-3)"':""}>${SEV[i.sev]}</span><div>${i.t}</div></div>`).join("");

/* ---------- Header ---------- */
sub.textContent = `${D.project || ""} · ${D.file}`;
meta.innerHTML = [D.schema, D.app, D.revision ? `Rev. ${D.revision} (${D.revdate||""})` : null, `${recs.length} measurable elements`, `Generated ${new Date().toISOString().slice(0,10)}`]
  .filter(Boolean).map(t=>`<span class="chip">${esc(t)}</span>`).join("");
foot.textContent = "Quantities: GEN-B03 (quantity) and GEN-B04 (unit). BOQ items: KEY-A03 (code) and KEY-A04 (description). Road: KEY-A05. Type, diameter, material and class are parsed from KEY-A04.";

/* ---------- CSV ---------- */
csvBtn.onclick = () => {
  const rows = [["KEY-A03","KEY-A04","Type","Diameter_mm","Material","Class","Road","Unit","Quantity","IFC_elements","Warning"]];
  (window.__lines||[]).forEach(l => {
    const w = l.warn.join("; ");
    if (l.subs.length) l.subs.forEach(sb => rows.push([l.code,l.desc,l.type,l.dia??"",l.mat,l.cls,sb.road,l.u,sb.q.toFixed(l.u==="m"?2:0),sb.n,w]));
    else rows.push([l.code,l.desc,l.type,l.dia??"",l.mat,l.cls,st.road||"All",l.u,l.q.toFixed(l.u==="m"?2:0),l.n,w]);
  });
  const csv = rows.map(r=>r.map(v=>`"${String(v??"").replace(/"/g,'""')}"`).join(",")).join("\r\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["﻿"+csv],{type:"text/csv"}));
  a.download = "BOQ_" + D.file.replace(/\.ifc$/i,"") + ".csv"; a.click();
};

render();
}

window.addEventListener("message", ev => {
  if (ev.source !== window.parent) return;
  const m = ev.data || {};
  if (m.type === "boq-data") { setTheme(m.theme); render(m.data); }
  else if (m.type === "boq-theme") setTheme(m.theme);
});
if (window.BOQ_DATA) render(window.BOQ_DATA);
else if (window.parent !== window) window.parent.postMessage({ type: "boq-ready" }, "*");
})();
