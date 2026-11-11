// SVG for the column lineage graph, as markup strings. Drawn like the rest of the app: a process
// flow sheet. Datasets are cards with a lakehouse-coloured edge; a definition is an instrument tag
// (a balloon with the category letters above the line and the cell:line where the code is below
// it); a row operation is a manifold every column pipe runs through; a notebook in the datasets view
// is a card listing the definitions that change columns. Colours come from the stylesheet.
import {FONT, SIZE, fitLine, short} from "./graph.mjs";
import {pathOf} from "./layout.mjs";

export const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const STATUS = {new: "new", altered: "altered", renamed: "renamed", kept: "not written here", unknown: "source unknown", carried: "copied", bi: "Power BI"};

let fit = (t, w) => short(t, Math.max(4, Math.floor(w / 7)));   // replaced by real measuring (renderGraph)
let fitFont = (t, w, font) => fit(t, w);

export function balloon(letters, tag, cx, cy, r = SIZE.balloon / 2) {
  const big = String(letters || "?");
  tag = String(tag || "");
  const tagSize = tag.length <= 4 ? 8.5 : tag.length === 5 ? 7.5 : 6.5;      // always inside the circle
  return `<g class="balloon"><circle cx="${cx}" cy="${cy}" r="${r}"/>` +
    (tag ? `<line x1="${cx - r}" x2="${cx + r}" y1="${cy}" y2="${cy}"/>` : "") +
    `<text class="b-letters${big.length > 2 ? " b-small" : ""}" x="${cx}" y="${tag ? cy - 3.5 : cy + 4}">${esc(big)}</text>` +
    (tag ? `<text class="b-tag" x="${cx}" y="${cy + 10.5}" style="font-size:${tagSize}px">${esc(tag.length > 7 ? tag.slice(0, 6) + "…" : tag)}</text>` : "") + `</g>`;
}

function dataset(n, pos, colorOf) {
  const d = n.data, w = pos.w;
  const sub = d.notFollowed ? "not followed column by column" : d.split === "before" ? "as it is read, before it is written" :
    [d.lakehouseName && `in ${d.lakehouseName}`, d.kind === "file" ? "files" : ""].filter(Boolean).join(", ");
  let s = `<g class="n ds role-${esc(d.role)}${d.kind !== "table" ? " ds-" + esc(d.kind) : ""}" data-node="${esc(d.id)}" transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})">` +
    `<rect class="card" width="${w}" height="${pos.h}" rx="4"/>` +
    `<rect class="stripe" width="5" height="${pos.h}" rx="2" style="fill:${esc(colorOf(d))}"/>` +
    `<g class="ds-top" data-port="${esc(d.id)}|"><rect class="hit" width="${w}" height="${SIZE.dsHead - 4}"/>` +
    `<text class="ds-name" x="14" y="19">${esc(fitFont(d.label, w - 24, FONT.name))}</text>` +
    `<text class="ds-sub" x="14" y="34">${esc(fitFont(sub, w - 44, FONT.sub))}</text></g>` +
    `<g class="ds-fold" data-fold="${esc(d.id)}"><rect class="hit" x="${w - 26}" y="6" width="20" height="20" rx="3"/>` +
    `<path class="fold-glyph" d="${d.collapsed ? `M${w - 19} 11l5 5-5 5` : `M${w - 21} 13l5 5 5-5`}"/></g>` +
    `<line class="rule" x1="5" x2="${w}" y1="${SIZE.dsHead - 3}" y2="${SIZE.dsHead - 3}"/>`;
  n.rows.forEach((c, i) => {
    const y = SIZE.dsHead + i * SIZE.rowH;
    const cls = ["row", c.status ? "st-" + c.status : "", c.used === false ? "unused" : "", c.inferred ? "inferred" : "",
      c.inTable === false ? "not-in-table" : "", c.star ? "star" : ""].filter(Boolean).join(" ");
    const mark = STATUS[c.status] && ["new", "altered", "renamed", "carried", "bi"].includes(c.status) ? STATUS[c.status] : "";
    s += `<g class="${cls}" data-port="${esc(c.id)}" transform="translate(0,${y})">` +
      `<rect class="hit" x="5" width="${w - 5}" height="${SIZE.rowH}"/>` +
      (mark ? `<rect class="chg" x="${w - 4}" y="2" width="4" height="${SIZE.rowH - 4}"/>` : "") +
      `<text class="col-name" x="14" y="${SIZE.rowH - 5}">${esc(fitFont(c.star ? "* the other columns" : c.name, w - 34 - typeW(mark || c.type), FONT.row))}</text>` +
      `<text class="col-type" x="${w - 10}" y="${SIZE.rowH - 5}">${esc(mark || fitFont(c.type || "", 90, FONT.small))}</text></g>`;
  });
  if (d.hidden) s += `<text class="ds-more" x="14" y="${SIZE.dsHead + n.rows.length * SIZE.rowH + SIZE.rowH - 5}">${d.collapsed ?
    `${d.hidden} column${d.hidden === 1 ? "" : "s"} folded away` : `${d.hidden} more column${d.hidden === 1 ? "" : "s"} unchanged`}</text>`;
  return s + `</g>`;
}

const typeW = t => Math.min(90, fitFont(String(t || ""), 90, FONT.small).length * 6.6);

// The code of a definition: a box with a stripe in its category's colour, the text on one or two lines,
// the box as big as the text (graph.mjs measured it)
function codeBox(n, x, cy) {
  const b = n.box, y = cy - b.h / 2;
  let s = `<rect class="pill" x="${x}" y="${y.toFixed(1)}" width="${b.w}" height="${b.h}" rx="4"/>` +
    `<rect class="accent" x="${x}" y="${y.toFixed(1)}" width="3" height="${b.h}" rx="1.5"/>`;
  b.lines.forEach((line, i) => {
    s += `<text class="code" x="${x + 11}" y="${(y + 15 + i * SIZE.lineH).toFixed(1)}">${esc(line)}</text>`;
  });
  return s;
}

function op(n, pos) {
  const o = n.data, tag = o.tag || `${o.cell}:${o.line}`, r = SIZE.balloon / 2;
  if (n.kind === "op") {
    return `<g class="n op cat-${esc(o.category)}" data-node="${esc(o.id)}" data-port="${esc(o.id)}" transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})">` +
      `<rect class="hit" width="${pos.w}" height="${pos.h}"/>` + balloon(o.letters, tag, r, pos.h / 2) +
      codeBox(n, SIZE.balloon + 8, pos.h / 2) + `</g>`;
  }
  if (n.kind === "block") {                       // the columns pass straight through, one lane each
    const lanes = o.lanes || [], top = SIZE.mfTop;
    let s = `<g class="n blk cat-${esc(o.category)}" data-node="${esc(o.id)}" transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})">` +
      `<g data-port="${esc(o.id)}|head"><rect class="hit" width="${pos.w}" height="${top - 6}"/>` + balloon(o.letters, tag, r, r + 4) +
      codeBox(n, SIZE.balloon + 8, r + 4) + `</g>` +
      `<line class="stem" x1="${r}" x2="${r}" y1="${SIZE.balloon + 4}" y2="${top - 4}"/>` +
      `<rect class="block" x="0" y="${top - 4}" width="${pos.w}" height="${lanes.length * SIZE.laneH + 8}" rx="3"/>` +
      `<line class="side" x1="0.75" x2="0.75" y1="${top - 4}" y2="${top + lanes.length * SIZE.laneH + 4}"/>` +
      `<line class="side" x1="${pos.w - 0.75}" x2="${pos.w - 0.75}" y1="${top - 4}" y2="${top + lanes.length * SIZE.laneH + 4}"/>`;
    lanes.forEach((l, i) => {
      const y = n.ports[l.id] ?? top + i * SIZE.laneH + SIZE.laneH / 2;
      s += `<g class="lane" data-port="${esc(l.id)}"><rect class="hit" y="${y - SIZE.laneH / 2}" width="${pos.w}" height="${SIZE.laneH}"/>` +
        `<line class="pipe" x1="0" x2="${pos.w}" y1="${y}" y2="${y}"/></g>`;
    });
    return s + `</g>`;
  }
  const lanes = o.lanes || [], top = SIZE.mfTop, barX = r - 8;
  let s = `<g class="n mf cat-${esc(o.category)}" data-node="${esc(o.id)}" transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})">` +
    `<g data-port="${esc(o.id)}|cond"><rect class="hit" width="${pos.w}" height="${top - 6}"/>` + balloon(o.letters, tag, r, r + 4) +
    codeBox(n, SIZE.balloon + 8, r + 4) + `</g>` +
    `<line class="stem" x1="${r}" x2="${r}" y1="${SIZE.balloon + 4}" y2="${top - 4}"/>` +
    `<rect class="mf-bar" x="${barX}" y="${top - 4}" width="16" height="${Math.max(1, lanes.length) * SIZE.laneH + 8}" rx="3"/>`;
  lanes.forEach((l, i) => {
    const y = n.ports[l.id] ?? top + i * SIZE.laneH + SIZE.laneH / 2;
    s += `<g class="lane" data-port="${esc(l.id)}"><rect class="hit" y="${y - SIZE.laneH / 2}" width="${pos.w}" height="${SIZE.laneH}"/>` +
      `<line class="mf-tick" x1="${barX + 3}" x2="${barX + 13}" y1="${y}" y2="${y}"/>` +
      `<line class="pipe" x1="${barX + 16}" x2="${pos.w}" y1="${y}" y2="${y}"/></g>`;
  });
  return s + `</g>`;
}

const GLYPH = {
  notebook: '<path class="nb-glyph" d="M12 11h10l4 4v14H12z M22 11v4h4"/>',
  pipeline: '<path class="nb-glyph" d="M11 20h5m6 0h5 M16 15h6v10h-6z M11 16v8 M27 16v8"/>',
  model: '<path class="nb-glyph" d="M11 13h16v14H11z M11 18h16 M11 22.5h16 M17 13v14"/>',
  report: '<path class="nb-glyph" d="M11 28h17 M13 27v-6h3v6 M18 27V14h3v13 M23 27v-9h3v9"/>',
};
const KIND_WORD = {notebook: "Notebook", pipeline: "Pipeline", model: "Semantic model", report: "Report"};
const EXT = 'd="M6 1.5h4.5V6M10.5 1.5 5 7M8.5 7v2.5a.8.8 0 0 1-.8.8H2.3a.8.8 0 0 1-.8-.8V4.1a.8.8 0 0 1 .8-.8h2.5"';

// a notebook, pipeline, semantic model or report: a card with a row per definition, field or visual
function card(n, pos) {
  const c = n.data, kind = n.card;
  let s = `<g class="n nb card-${esc(kind)}${c.error ? " nb-error" : ""}" data-node="${esc(c.id)}" transform="translate(${pos.x.toFixed(1)},${pos.y.toFixed(1)})">` +
    `<rect class="card" width="${pos.w}" height="${pos.h}" rx="6"/>` +
    `<g data-port="${esc(c.id)}|"><rect class="hit" width="${pos.w}" height="${SIZE.nbHead - 4}"/>` + (GLYPH[kind] || GLYPH.notebook) +
    `<text class="nb-name" x="34" y="22">${esc(fitFont(c.name, pos.w - 64, FONT.name))}</text>`;
  if (!(c.rowOps || []).length) s += `<text class="nb-kind" x="34" y="38">${esc(KIND_WORD[kind] || "")}</text>`;
  (c.rowOps || []).slice(0, 8).forEach((r, i) => {
    s += `<g class="chip cat-${esc(r.category)}"><circle cx="${42 + i * 19}" cy="37" r="7.5"/><text x="${42 + i * 19}" y="40.5">${esc(r.letters)}</text></g>`;
  });
  s += `</g>` + (c.url ? `<a class="nb-link corner" href="${esc(c.url)}" target="_blank" rel="noopener" data-link-name="${esc(c.name)}"><rect class="hit" x="${pos.w - 26}" y="6" width="20" height="20"/><path class="lnk-ext" transform="translate(${pos.w - 22},10)" ${EXT}/></a>` : "") +
    `<line class="rule" x1="0" x2="${pos.w}" y1="${SIZE.nbHead - 4}" y2="${SIZE.nbHead - 4}"/>`;
  n.rows.forEach((r, i) => {
    const y = (n.ports[r.id] ?? SIZE.nbHead + i * SIZE.nbRowH + SIZE.nbRowH / 2) - SIZE.nbRowH / 2;
    const mark = r.kind === "column" ? `<path class="row-glyph" d="M10 ${SIZE.nbRowH / 2 - 5}h12v10H10z M14 ${SIZE.nbRowH / 2 - 5}v10"/>` :
      r.kind === "visual" ? `<path class="row-glyph" d="M10 ${SIZE.nbRowH / 2 + 5}h13 M12 ${SIZE.nbRowH / 2 + 4}v-4h2.5v4 M16.5 ${SIZE.nbRowH / 2 + 4}v-8h2.5v8"/>` :
      r.kind === "copy" ? `<path class="row-glyph" d="M9 ${SIZE.nbRowH / 2}h13 M18 ${SIZE.nbRowH / 2 - 4}l4 4-4 4"/>` :
      balloon(r.letters, "", 16, SIZE.nbRowH / 2, 9);
    s += `<g class="row nb-row kind-${esc(r.kind || "definition")} cat-${esc(r.category || "")}" data-port="${esc(r.id)}" transform="translate(0,${y})">` +
      `<rect class="hit" width="${pos.w}" height="${SIZE.nbRowH}"/>` + mark +
      `<text class="code" x="32" y="${SIZE.nbRowH / 2 + 4}">${esc(fitFont(r.label, pos.w - 42, FONT.small))}</text></g>`;
  });
  return s + `</g>`;
}

// notebook mode: a light frame around each notebook's definitions, named after it
function frames(graph, lay, data) {
  const by = new Map();
  for (const n of graph.nodes) {
    if (n.kind !== "op" && n.kind !== "manifold" && n.kind !== "block") continue;
    const p = lay.nodes.get(n.id);
    if (!p) continue;
    const b = by.get(n.group) || {x1: Infinity, y1: Infinity, x2: -Infinity, y2: -Infinity};
    b.x1 = Math.min(b.x1, p.x); b.y1 = Math.min(b.y1, p.y); b.x2 = Math.max(b.x2, p.x + p.w); b.y2 = Math.max(b.y2, p.y + p.h);
    by.set(n.group, b);
  }
  const names = new Map((data.notebooks || []).map(nb => [nb.id, nb]));
  let s = "";
  for (const [id, b] of by) {
    if (!names.has(id)) continue;                           // DAX belongs to its model's card, not a frame
    const nb = names.get(id);
    const x = b.x1 - 10, y = b.y1 - 18;
    const name = `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}">${esc(nb.name)}</text>` +
      `<path class="lnk-ext" transform="translate(${(x + measureName(nb.name) + 6).toFixed(1)},${(y - 10).toFixed(1)})" d="M6 1.5h4.5V6M10.5 1.5 5 7M8.5 7v2.5a.8.8 0 0 1-.8.8H2.3a.8.8 0 0 1-.8-.8V4.1a.8.8 0 0 1 .8-.8h2.5"/>`;
    // background only: the frame doesn't take the mouse; its name opens the notebook in Fabric
    s += `<g class="nb-frame${nb.focus ? " focus" : ""}"><rect x="${(b.x1 - 22).toFixed(1)}" y="${(b.y1 - 38).toFixed(1)}" width="${(b.x2 - b.x1 + 44).toFixed(1)}" height="${(b.y2 - b.y1 + 56).toFixed(1)}" rx="10"/>` +
      (nb.url ? `<a class="nb-link" href="${esc(nb.url)}" target="_blank" rel="noopener" data-link-name="${esc(nb.name)}" data-nb-item="${esc(nb.itemId || "")}">${name}</a>` : name) +
      (!nb.focus && nb.itemId ? `<text class="nb-focus" x="${(x + measureName(nb.name) + 26).toFixed(1)}" y="${y.toFixed(1)}" data-focus-nb="${esc(nb.itemId)}">Focus on it</text>` : "") +
      `</g>`;
  }
  return s;
}

let measureName = t => String(t).length * 7;

export function renderGraph(graph, lay, data, colorOf, measure, ids = "cll") {
  if (measure) {
    measureName = t => measure(t, FONT.frame);
    fitFont = (t, w, font) => fitLine(t, w, measure, font);
  }
  let edges = "";
  for (const e of graph.edges) {
    const pts = lay.routes.get(e);
    if (!pts) continue;
    const cls = {data: "e-data", condition: "e-cond", relation: "e-rel", change: "e-chg", copy: "e-copy", bi: "e-bi"}[e.kind] || "e-data";
    const marker = {relation: `${ids}ArrowRel`, change: `${ids}ArrowChg`, bi: `${ids}ArrowBi`}[e.kind];
    edges += `<path class="e ${cls}" data-e="${e.i}" d="${pathOf(pts)}"${marker ? ` marker-end="url(#${marker})"` : ""}/>`;
  }
  let nodes = "";
  for (const n of graph.nodes) {
    const pos = lay.nodes.get(n.id);
    if (!pos) continue;
    nodes += n.kind === "dataset" ? dataset(n, pos, colorOf) : n.kind === "card" ? card(n, pos) : op(n, pos);
  }
  return `<g class="frames">${data.mode === "notebook" ? frames(graph, lay, data) : ""}</g><g class="edges">${edges}</g><g class="nodes">${nodes}</g>`;
}
