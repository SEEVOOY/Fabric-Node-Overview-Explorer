// The Column-Level Lineage tab. Two ways to look at columns:
//   Notebooks       one notebook: its datasets' columns, and what its code does to each of them
//   Across datasets the columns that change (new, altered, renamed) from dataset to dataset
// It talks to the rest of the app only through `host` (see app.js): the API, the workspace's items,
// lakehouse colours, reading items, and showing something on the map.
import {FONT, buildGraph, collapseData, focusData, indexFlows, onlyChanged, short, stepsInto, threadOf, useFonts} from "./graph.mjs";
import {layout, pathOf} from "./layout.mjs";
import {balloon, esc, renderGraph} from "./render.mjs";

const EXT_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 2.5h4.5V7M13.5 2.5 7 9M11 9.5v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h3"/></svg>';
const STATUS_TEXT = {new: "New: made by the notebook", altered: "Altered: computed from itself", renamed: "Renamed",
  unchanged: "Copied as it is", kept: "Not written by this notebook", unknown: "Where it comes from can't be followed",
  feeds: "Used to make a changed column"};
const template = (P, title, note) => `
<aside class="panel tree cll-nav" aria-label="Column lineage: notebooks and datasets">
  <div class="cll-nav-title"><b>${title}</b><span>${note}</span></div>
  <div class="panel-head"><input class="field" type="search" data-c="filter" placeholder="Filter" aria-label="Filter the list" autocomplete="off"></div>
  <div class="tree-body cll-list" data-c="list"></div>
</aside>
<section class="stage cll-stage" data-c="stage">
  <svg class="cll-svg" data-c="svg" tabindex="0" aria-label="Column lineage">
    <defs>
      <pattern id="${P}G1" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M24 0H0V24" class="g1"/></pattern>
      <pattern id="${P}G2" width="120" height="120" patternUnits="userSpaceOnUse"><path d="M120 0H0V120" class="g2"/></pattern>
      <marker id="${P}ArrowRel" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" class="m-rel"/></marker>
      <marker id="${P}ArrowChg" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" class="m-chg"/></marker>
      <marker id="${P}ArrowBi" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" class="m-bi"/></marker>
    </defs>
    <rect class="grid g1r" width="100%" height="100%" fill="url(#${P}G1)"/><rect class="grid" width="100%" height="100%" fill="url(#${P}G2)"/>
    <g data-c="world"></g>
  </svg>
  <div class="tools" data-c="tools"></div>
  <div class="cll-find" role="search"><input class="field" type="search" data-c="search" placeholder="Find a column  /" aria-label="Find a column in the drawing" autocomplete="off"><ul class="cll-hits" data-c="hits" hidden></ul></div>
  <div class="map-bars"><div class="focus-bar cll-bar" data-c="bar" role="status" hidden></div></div>
  <details class="legend cll-legend" data-c="legend" open><summary>Legend</summary><div class="cll-legend-body" data-c="legendBody"></div></details>
  <div class="cll-box" data-c="box" hidden></div>
  <div class="empty" data-c="empty" hidden></div>
  <div class="tip" data-c="tip" role="tooltip" hidden></div>
</section>
<aside class="panel details" aria-label="Column lineage details"><div class="details-body" data-c="info"></div></aside>`;

// One of the two tabs: opts.mode "datasets" (Column-Level Lineage, across datasets) or "notebook"
// (Notebook-Level Lineage, one notebook in depth). Each has its own state, ids and address.
export function createColumnLineageView(root, host, opts = {}) {
  const MODE = opts.mode === "datasets" ? "datasets" : "notebook";
  const VIEW = opts.view || (MODE === "datasets" ? "columns" : "notebooks");
  const P = MODE === "datasets" ? "cllD" : "cllN";             // SVG ids, one set per tab
  root.innerHTML = MODE === "datasets"
    ? template(P, "Column-Level Lineage", "The columns that change, from dataset to dataset, into Power BI. Pick a dataset, model or report to follow its thread.")
    : template(P, "Notebook-Level Lineage", "What one notebook or pipeline does to every column. Pick one.");
  const $ = name => root.querySelector(`[data-c="${name}"]`);
  const svg = $("svg"), world = $("world"), stage = $("stage");
  // the bar under the tools follows them down when they take two rows
  new ResizeObserver(() => stage.style.setProperty("--below-tools", `${$("tools").offsetHeight + 18}px`)).observe($("tools"));
  const saved = host.store.get(`cll:${MODE}`, host.store.get("cll", {}));
  const css = getComputedStyle(document.documentElement), font = v => css.getPropertyValue(v).trim();
  useFonts({ui: font("--font-ui"), text: font("--font-text"), code: font("--font-code")});   // measure what is drawn
  const st = {
    mode: MODE, notebook: saved.notebook || "", scope: "direct",                 // one notebook: just its own datasets
    focus: saved.focus || "", dsScope: saved.dsScope || "full", direction: saved.direction || "both", allColumns: false,
    data: null, graph: null, idx: null, lay: null, sel: null, hover: null, view: {x: 40, y: 70, k: 1},
    loading: false, error: null, req: 0, filter: "", dsList: null, catalog: null, waiting: false, visible: false,
    full: null, changedOnly: !!saved.changedOnly, focusOn: null, pending: null, hits: [], hit: 0,
    lanes: saved.lanes !== false, moved: new Map(), edgesOf: new Map(), collapsed: new Set(saved.collapsed || []),
    sels: [],                                                  // several selected (Shift+click, Shift+drag); st.sel is the last
  };
  const save = () => host.store.set(`cll:${MODE}`, {mode: st.mode, notebook: st.notebook, scope: st.scope, focus: st.focus,
    dsScope: st.dsScope, direction: st.direction, changedOnly: st.changedOnly, lanes: st.lanes,
    collapsed: [...st.collapsed].slice(-200)});
  function fold(ids, on) {                                     // datasets folded to their heading, or opened again
    for (const id of ids) { if (on) st.collapsed.add(id); else st.collapsed.delete(id); }
    save(); draw(); renderChrome();
  }
  const nodeOfPort = port => String(port || "").split("|")[0];

  // ------------------------------------------------------------------ measuring text
  const mctx = document.createElement("canvas").getContext("2d"), mcache = new Map();
  const measure = (t, f) => {
    const k = f + "\u0000" + t;
    let w = mcache.get(k);
    if (w === undefined) { mctx.font = f; w = mctx.measureText(String(t)).width; mcache.set(k, w); }
    return w;
  };
  const colorOf = d => host.colorOf(d.lakehouse, d.lakehouseName);

  // ------------------------------------------------------------------ loading
  async function load(refresh = false) {
    const ws = host.workspaceId();
    if (!ws) { st.data = null; renderAll(); return; }
    if (st.mode === "notebook" && !st.notebook) { st.data = null; renderAll(); return; }
    const req = ++st.req;
    st.loading = true; st.error = null; renderChrome();
    try {
      if (!st.catalog) st.catalog = await host.api("/api/column-lineage/catalog");
      const data = st.mode === "notebook"
        ? await host.api("/api/column-lineage/notebook", {workspaceId: ws, notebookId: st.notebook, scope: st.scope, refresh})
        : await host.api("/api/column-lineage/datasets", {workspaceId: ws, focus: st.focus, scope: st.dsScope,
          direction: st.direction, allColumns: st.allColumns, refresh});
      if (req !== st.req) return;
      const subject = JSON.stringify([st.mode, st.notebook, st.focus, st.scope, st.dsScope, st.direction, st.allColumns]);
      const same = subject === st.subject;                       // Refresh: keep where the user is
      st.subject = subject;
      st.full = data; st.hover = null;
      if (!same) { st.sel = null; st.focusOn = null; st.moved.clear(); }
      if (st.pending) {                                         // opened from a link
        const pd = st.pending;
        st.pending = null;
        if (pd.select) st.sel = {port: pd.select, node: nodeOfPort(pd.select)};
        else if (pd.node) st.sel = {node: pd.node};
        if (pd.focusMode && st.mode === "datasets" && st.sel) st.focusOn = st.sel;
      }
      st.error = data.error ? {message: data.error, hint: data.hint || ""} : null;
      if (st.mode === "notebook") host.read(st.notebook);       // the map shows it as read too
      else if (!st.focus) st.dsList = data.datasets;
      draw();
      if (!same) { st.needsFit = true; fit(false, true); }
      if (st.sel && !same) centerOnPort(st.sel.port, st.sel.node);
      syncHash();
    } catch (err) {
      if (req !== st.req) return;
      st.error = err; st.data = st.full = null; world.innerHTML = "";
    } finally {
      if (req === st.req) { st.loading = false; renderAll(); }
    }
  }

  function draw() {
    if (!st.full) { world.innerHTML = ""; st.graph = st.lay = st.data = null; return; }
    let data = st.full;
    if (st.mode === "notebook" && st.changedOnly) data = onlyChanged(data);
    if (st.mode === "datasets" && st.focusOn) {                 // Focus: the thread of what is selected, laid out alone
      const g0 = buildGraph(st.full, measure);
      data = focusData(st.full, g0, indexFlows(g0), st.focusOn);
    }
    st.data = collapseData(data, st.collapsed);
    st.graph = buildGraph(st.data, measure, {lanes: st.lanes});
    st.idx = indexFlows(st.graph);
    st.lay = layout(st.graph, {rankGap: st.mode === "datasets" ? 140 : 110});
    st.edgesOf = new Map();
    for (const e of st.graph.edges) {
      for (const id of new Set([e.fromNode, e.toNode])) {
        if (!st.edgesOf.has(id)) st.edgesOf.set(id, []);
        st.edgesOf.get(id).push(e);
      }
    }
    for (const [id, p] of st.moved) {                          // nodes the user moved stay where they put them
      const q = st.lay.nodes.get(id);
      if (q) { q.x = p.x; q.y = p.y; reroute(id); }
    }
    world.innerHTML = renderGraph(st.graph, st.lay, st.data, colorOf, measure, P);
    applyView(); highlight();
  }

  // ------------------------------------------------------------------ pan and zoom
  function applyView() {
    const {x, y, k} = st.view;
    world.setAttribute("transform", `translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${k.toFixed(4)})`);
    for (const id of [`${P}G1`, `${P}G2`]) {
      const p = svg.querySelector("#" + id);
      if (p) p.setAttribute("patternTransform", `translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${k.toFixed(4)})`);
    }
    svg.classList.toggle("far", k < 0.45);
  }
  // Fit shows everything; a drawing first opens at a size that can be read (pan to see the rest)
  function fit(animate = true, readable = false) {
    if (!st.lay) return;
    const r = svg.getBoundingClientRect();
    if (!r.width || !r.height) return;
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    for (const p of st.lay.nodes.values()) { x1 = Math.min(x1, p.x); y1 = Math.min(y1, p.y); x2 = Math.max(x2, p.x + p.w); y2 = Math.max(y2, p.y + p.h); }
    if (!Number.isFinite(x1)) { x1 = y1 = 0; x2 = st.lay.width; y2 = st.lay.height; }
    const bar = $("bar");                                       // below the tools and the bar, however tall they are
    const top = $("tools").offsetHeight + (bar.hidden ? 0 : bar.offsetHeight + 8) + 30;
    const w = Math.max(1, x2 - x1), h = Math.max(1, y2 - y1);
    st.needsFit = false;
    let k = Math.max(0.12, Math.min(1.15, (r.width - 70) / w, (r.height - top - 70) / h));
    if (readable) k = Math.max(k, 0.62);
    const target = {k, x: (r.width - w * k) / 2 - x1 * k, y: top + Math.max(10, (r.height - top - 40 - h * k) / 2) - y1 * k};
    if (!animate || matchMedia("(prefers-reduced-motion: reduce)").matches) { st.view = target; applyView(); return; }
    const from = {...st.view}, t0 = performance.now();
    const step = now => {
      const p = Math.min(1, (now - t0) / 260), e = 1 - Math.pow(1 - p, 3);
      st.view = {x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e};
      applyView(); if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function zoomAt(f, cx, cy) {
    const k = Math.max(0.1, Math.min(3, st.view.k * f));
    st.view = {k, x: cx - (cx - st.view.x) * k / st.view.k, y: cy - (cy - st.view.y) * k / st.view.k};
    applyView();
  }
  function centerOnPort(port, nodeId) {
    const id = nodeId || nodeOfPort(port);
    const n = st.graph && st.graph.byId.get(id), p = st.lay && st.lay.nodes.get(id);
    if (!n || !p) return;
    const y = port && n.ports[port] !== undefined ? n.ports[port] : Math.min(p.h, 200) / 2;
    const r = svg.getBoundingClientRect(), k = Math.max(st.view.k, 0.85);
    st.view = {k, x: r.width / 2 - (p.x + p.w / 2) * k, y: r.height / 2 - (p.y + y) * k};
    applyView();
  }
  // Dragging a node moves it; dragging the sheet moves around. Neither may select the drawing's text:
  // browsers would offer to copy it (Edge shows its menu), so selection is off here (see cll.css too).
  let drag = null;
  svg.addEventListener("selectstart", ev => ev.preventDefault());
  svg.addEventListener("dragstart", ev => ev.preventDefault());
  svg.addEventListener("pointerdown", ev => {
    if (ev.button !== 0 && ev.button !== 1) return;
    if (ev.target.closest("a, [data-focus-nb], [data-fold]")) return;   // links and buttons in the drawing do their own thing
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.rangeCount && svg.contains(sel.anchorNode)) sel.removeAllRanges();
    ev.preventDefault();
    const t = ev.button === 0 ? targetOf(ev.target) : null;
    const node = t && st.lay && st.lay.nodes.get(t.node) ? t.node : "";
    const sels = selected();
    const ids = node && sels.length > 1 && sels.some(x => x.node === node) ? [...new Set(sels.map(x => x.node))] : node ? [node] : [];
    drag = {x: ev.clientX, y: ev.clientY, vx: st.view.x, vy: st.view.y, moved: false, target: ev.target, node,
      shift: ev.shiftKey, box: ev.shiftKey && ev.button === 0 && !t,             // Shift+drag on the sheet: a box
      group: ids.filter(id => st.lay.nodes.has(id)).map(id => ({id, x: st.lay.nodes.get(id).x, y: st.lay.nodes.get(id).y}))};
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", ev => {
    if (drag) {
      const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) > (drag.node ? 6 : 3)) {      // a click never moves a node
        drag.moved = true;
        svg.classList.add(drag.box ? "boxing" : drag.node ? "moving" : "panning");
        hideTip();
      }
      if (drag.moved) {
        if (drag.box) drawBox(drag, ev);
        else if (drag.node) for (const g of drag.group) moveNode(g.id, g.x + dx / st.view.k, g.y + dy / st.view.k);
        else { st.view = {...st.view, x: drag.vx + dx, y: drag.vy + dy}; applyView(); }
        return;
      }
    }
    svg.classList.toggle("boxmode", ev.shiftKey);           // Shift over the sheet: a box can be drawn
    hoverAt(ev);
  });
  const endDrag = () => {
    const d = drag;
    drag = null; svg.classList.remove("panning", "moving", "boxing");
    $("box").hidden = true;
    return d;
  };
  svg.addEventListener("pointerup", () => {
    const d = endDrag();
    if (!d) return;
    if (d.moved) { if (d.box) boxSelect(d); else if (d.node) placed(); return; }
    const t = targetOf(d.target);
    if (d.shift && !t) return;                                 // Shift+click on the sheet: the selection stays
    select(t, d.shift && !!t);
  });
  // Shift+drag on the sheet: everything the box touches is added to the selection
  function drawBox(d, ev) {
    const r = stage.getBoundingClientRect(), box = $("box");
    const x1 = Math.min(d.x, ev.clientX) - r.left, y1 = Math.min(d.y, ev.clientY) - r.top;
    Object.assign(box.style, {left: `${x1}px`, top: `${y1}px`, width: `${Math.abs(ev.clientX - d.x)}px`, height: `${Math.abs(ev.clientY - d.y)}px`});
    box.hidden = false;
    d.x2 = ev.clientX; d.y2 = ev.clientY;
  }
  function boxSelect(d) {
    if (d.x2 === undefined || !st.lay) return;
    const r = svg.getBoundingClientRect(), k = st.view.k;
    const wx = sx => (sx - r.left - st.view.x) / k, wy = sy => (sy - r.top - st.view.y) / k;
    const x1 = wx(Math.min(d.x, d.x2)), x2 = wx(Math.max(d.x, d.x2)), y1 = wy(Math.min(d.y, d.y2)), y2 = wy(Math.max(d.y, d.y2));
    const list = selected().slice();
    for (const [id, p] of st.lay.nodes) {
      if (p.x < x2 && p.x + p.w > x1 && p.y < y2 && p.y + p.h > y1 && !list.some(x => !x.port && x.node === id)) list.push({node: id});
    }
    st.sels = list; st.sel = list.length ? list[list.length - 1] : null;
    highlight(); renderInfo(); renderChrome(); syncHash();
  }
  svg.addEventListener("contextmenu", ev => {
    ev.preventDefault();
    if (drag) return;
    const spec = menuFor(targetOf(ev.target), ev.target);
    if (spec && spec.items.length) host.menu(spec, ev.clientX, ev.clientY);
  });
  function menuFor(t, el) {
    const items = [];
    const clean = list => list.filter((it, i, a) => !(it.sep && (i === 0 || i === a.length - 1 || a[i - 1].sep)));
    const link = el.closest && el.closest("[data-nb-item]");
    if (link) {                                              // a notebook's name on its frame
      const id = link.dataset.nbItem;
      if (id && id !== st.notebook) items.push({label: "Focus on its column lineage", run: () => openNotebook(id)});
      items.push({label: "Open in Fabric", run: () => window.open(link.getAttribute("href"), "_blank", "noopener")});
      if (id) items.push({label: "Show on the map", run: () => host.reveal("item:" + id)});
      return {title: link.dataset.linkName, sub: "Notebook", items};
    }
    if (!t || !st.graph) {
      items.push({label: "Fit", run: () => fit()});
      if (st.moved.size) items.push({label: "Re-layout", run: () => { st.moved.clear(); draw(); renderChrome(); }});
      if (st.sel) items.push({label: "Stop highlighting", run: () => select(null)});
      if (st.focusOn) items.push({label: "Leave focus", run: () => act("focus")});
      const all = ((st.full && st.full.datasets) || []).map(d => d.id);
      if (all.some(id => !st.collapsed.has(id))) items.push({label: "Fold every dataset's columns", run: () => fold(all, true)});
      if (st.collapsed.size) items.push({label: "Unfold every dataset", run: () => fold([...st.collapsed], false)});
      return {title: MODE === "datasets" ? "Column-Level Lineage" : "Notebook-Level Lineage", items};
    }
    const n = st.graph.byId.get(t.node);
    if (!n) return null;
    const same = st.sel && st.sel.port === t.port && st.sel.node === t.node;
    const picked = selected().some(x => sameT(x, t));
    const card = n.kind === "card" ? n.card : "";
    items.push({label: same ? "Stop highlighting" : t.port && t.port !== `${t.node}|` ? "Highlight its path" : "Highlight its lines",
      run: () => select(same ? null : t)});
    items.push({label: picked && selected().length > 1 ? "Take it out of the selection" : "Add it to the selection (Shift+click)",
      run: () => select(t, true)});
    if (MODE === "datasets") items.push({label: st.focusOn ? "Leave focus" : "Focus on its thread", run: () => {
      if (st.focusOn) { act("focus"); return; }
      select(t); act("focus");
    }});
    items.push({sep: true});
    if (card === "notebook" || card === "pipeline") {
      if (MODE === "datasets" || n.data.itemId !== st.notebook) items.push({label: "Focus on column lineage", run: () => openNotebook(n.data.itemId)});
    }
    if (n.kind === "dataset") {
      const folded = st.collapsed.has(n.id);
      items.push({label: folded ? "Unfold its columns" : "Fold its columns", run: () => fold([n.id], !folded)});
    }
    if (card === "model" || card === "report" || n.kind === "dataset") {
      const id = n.kind === "dataset" ? n.data.table : n.data.id;
      items.push({label: MODE === "datasets" ? "Follow its thread" : "Show its column changes", run: () => openDatasets(id)});
    }
    if (["op", "manifold", "block"].includes(n.kind)) {
      const nb = ((st.data && st.data.notebooks) || []).find(x => x.id === n.data.notebook);
      if (nb && nb.itemId && nb.itemId !== st.notebook) items.push({label: `Focus on ${nb.name}`, run: () => openNotebook(nb.itemId)});
    }
    items.push({sep: true});
    const mapId = n.kind === "dataset" ? n.data.table : card ? n.data.id : "";
    if (mapId) items.push({label: "Show on the map", run: () => host.reveal(mapId)});
    if (card && n.data.url) items.push({label: "Open in Fabric", run: () => window.open(n.data.url, "_blank", "noopener")});
    if (n.kind === "dataset" && t.port) {
      const c = n.rows.find(r => r.id === t.port);
      if (c && !c.star) items.push({label: "Copy its name", run: () => navigator.clipboard.writeText(`${n.data.fullName}.${c.name}`).then(() => host.toast("Copied."), () => {})});
    }
    const title = n.kind === "dataset" ? (t.port && t.port !== `${t.node}|` ? `${n.data.label}.${(n.rows.find(r => r.id === t.port) || {}).name || ""}` : n.data.fullName) :
      card ? n.data.name : short(n.data.label, 60);
    const sub = n.kind === "dataset" ? (t.port && t.port !== `${t.node}|` ? "Column" : "Dataset") :
      card ? {notebook: "Notebook", pipeline: "Pipeline", model: "Semantic model", report: "Report"}[card] : cats(n.data).join(", ");
    return {title, sub, items: clean(items)};
  }
  svg.addEventListener("pointercancel", () => { const d = endDrag(); if (d && d.moved && d.node) placed(); });

  // a moved node: its lines follow (their middle waypoints stay); the rest is redrawn when it is put down
  const portYOf = (n, port) => (n.ports[port] !== undefined ? n.ports[port] : n.h / 2);
  // its lines go straight to where it is now; lines that share both ends keep their spread (a bundle
  // stays a ribbon instead of merging into one line)
  function reroute(id) {
    const groups = new Map();
    for (const e of st.edgesOf.get(id) || []) {
      const a = st.graph.byId.get(e.fromNode), b = st.graph.byId.get(e.toNode);
      const pa = st.lay.nodes.get(e.fromNode), pb = st.lay.nodes.get(e.toNode);
      if (!a || !b || !pa || !pb) continue;
      const start = {x: pa.x + pa.w, y: pa.y + portYOf(a, e.from)}, end = {x: pb.x, y: pb.y + portYOf(b, e.to)};
      const key = `${Math.round(start.x)},${Math.round(start.y)}>${Math.round(end.x)},${Math.round(end.y)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push([e, start, end]);
    }
    for (const list of groups.values()) {
      list.forEach(([e, start, end], i) => {
        if (list.length === 1) { st.lay.routes.set(e, [start, end]); return; }
        const off = (i - (list.length - 1) / 2) * 3;
        st.lay.routes.set(e, [start, {x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 + off}, end]);
      });
    }
  }
  function moveNode(id, x, y) {
    const p = st.lay.nodes.get(id);
    p.x = x; p.y = y;
    st.moved.set(id, {x, y});
    reroute(id);
    const el = world.querySelector(`.nodes > [data-node="${CSS.escape(id)}"]`);
    if (el) el.setAttribute("transform", `translate(${x.toFixed(1)},${y.toFixed(1)})`);
    for (const e of st.edgesOf.get(id) || []) {
      const path = world.querySelector(`[data-e="${e.i}"]`);
      if (path) path.setAttribute("d", pathOf(st.lay.routes.get(e)));
    }
  }
  function placed() {                                      // frames and everything else, where they are now
    world.innerHTML = renderGraph(st.graph, st.lay, st.data, colorOf, measure, P);
    highlight();
    renderChrome();
  }
  svg.addEventListener("pointerleave", () => { if (!drag) { setHover(null); hideTip(); } });
  svg.addEventListener("wheel", ev => {
    ev.preventDefault();
    const r = svg.getBoundingClientRect();
    zoomAt(Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0016)), ev.clientX - r.left, ev.clientY - r.top);
  }, {passive: false});

  // ------------------------------------------------------------------ hover, selection, highlight
  function targetOf(el) {
    const portEl = el.closest && el.closest("[data-port]"), nodeEl = el.closest && el.closest("[data-node]");
    if (!nodeEl) return null;
    const node = nodeEl.dataset.node, port = portEl && nodeEl.contains(portEl) ? portEl.dataset.port : "";
    const n = st.graph && st.graph.byId.get(node);
    if (n && (n.kind === "op" || port.endsWith("|cond") || port.endsWith("|head") || port === `${node}|`)) return {node};
    return port ? {port, node} : {node};
  }
  function hoverAt(ev) {
    const link = ev.target.closest && ev.target.closest("[data-link-name], [data-focus-nb]");
    if (link) {
      setHover(null);
      showTipHtml(ev, link.dataset.focusNb ? `<strong>Focus on its column lineage</strong>` :
        `<strong>Open ${esc(link.dataset.linkName)} in Fabric</strong><span>Opens the notebook in a new tab</span>`);
      return;
    }
    const t = targetOf(ev.target);
    setHover(t);
    if (t) showTip(ev, t); else hideTip();
  }
  function setHover(t) {
    const same = (a, b) => (a && b) ? a.port === b.port && a.node === b.node : a === b;
    if (same(t, st.hover)) return;
    st.hover = t; highlight();
  }
  const sameT = (a, b) => !!a && !!b && a.node === b.node && (a.port || "") === (b.port || "");
  // what is selected: the list, if the last selection is in it (st.sel can be set or cleared elsewhere)
  const selected = () => st.sel ? (st.sels.some(x => sameT(x, st.sel)) ? st.sels : [st.sel]) : [];
  function select(t, add = false) {                          // add: Shift+click, one more (or one less)
    if (add && t) {
      const list = selected().slice(), i = list.findIndex(x => sameT(x, t));
      if (i >= 0) list.splice(i, 1); else list.push(t);
      st.sels = list; st.sel = list.length ? list[list.length - 1] : null;
    } else {
      st.sels = t ? [t] : []; st.sel = t;
    }
    highlight(); renderInfo(); renderChrome(); syncHash();
  }
  function highlight() {
    const sels = selected();                                    // what is clicked stays lit while the mouse moves on
    const targets = sels.length ? sels : st.hover ? [st.hover] : [];
    const els = world.querySelectorAll(".hl, .sel");
    for (const el of els) el.classList.remove("hl", "sel");
    world.classList.toggle("dim", targets.length > 0);
    if (!targets.length || !st.graph) return;
    const th = {edges: new Set(), ports: new Set(), nodes: new Set()};
    for (const t of targets) {                                  // several selected: their threads together
      const one = threadOf(st.graph, st.idx, t.port ? {port: t.port} : {node: t.node});
      for (const k of ["edges", "ports", "nodes"]) for (const v of one[k]) th[k].add(v);
    }
    for (const el of world.querySelectorAll("[data-e]")) if (th.edges.has(+el.dataset.e)) el.classList.add("hl");
    for (const el of world.querySelectorAll("[data-port]")) if (th.ports.has(el.dataset.port)) el.classList.add("hl");
    for (const el of world.querySelectorAll("[data-node]")) if (th.nodes.has(el.dataset.node)) el.classList.add("hl");
    for (const t of sels) {
      const el = t.port ? world.querySelector(`[data-port="${CSS.escape(t.port)}"]`) : world.querySelector(`[data-node="${CSS.escape(t.node)}"]`);
      if (el) el.classList.add("sel");
    }
  }
  const tip = $("tip");
  function showTip(ev, t) { showTipHtml(ev, tipFor(t)); }
  function showTipHtml(ev, html) {
    if (!html) { hideTip(); return; }
    tip.innerHTML = html; tip.hidden = false;
    const r = stage.getBoundingClientRect();
    tip.style.left = `${Math.min(ev.clientX - r.left + 14, r.width - tip.offsetWidth - 8)}px`;
    tip.style.top = `${Math.min(ev.clientY - r.top + 14, r.height - tip.offsetHeight - 8)}px`;
  }
  function hideTip() { tip.hidden = true; }
  const colOf = port => { for (const n of st.graph.nodes) if (n.kind === "dataset") for (const c of n.rows) if (c.id === port) return [n, c]; return [null, null]; };
  function tipFor(t) {
    if (!st.graph) return "";
    const n = st.graph.byId.get(t.node);
    if (!n) return "";
    if (n.kind === "dataset" && t.port) {
      const [, c] = colOf(t.port);
      if (!c) return "";
      return `<strong>${esc(c.star ? "The other columns" : c.name)}</strong><span>${esc(STATUS_TEXT[c.status] || (c.used === false ? "Not used by the notebook" : "Read by the notebook"))}${c.type ? `, ${esc(c.type)}` : ""}</span>`;
    }
    if (n.kind === "dataset") return `<strong>${esc(n.data.fullName)}</strong><span>${n.rows.length} column${n.rows.length === 1 ? "" : "s"} shown</span>`;
    if (n.kind === "op" || n.kind === "manifold" || n.kind === "block") {
      const o = n.data;
      const lane = t.port && (o.lanes || []).find(l => l.id === t.port);
      return `<strong>${esc(cats(o).join(", "))}</strong><span>${esc(short(o.label, 90))}${lane ? `<br>Column: ${esc(lane.label)}` : ""}<br>${esc(o.where || `Cell ${o.cell}, line ${o.line}`)}</span>`;
    }
    if (n.kind === "card") {
      const row = t.port && n.rows.find(r => r.id === t.port);
      const word = {notebook: "Notebook", pipeline: "Pipeline", model: "Semantic model", report: "Report"}[n.card] || "";
      if (!row) return `<strong>${esc(n.data.name)}</strong><span>${word}</span>`;
      const sub = row.kind === "column" ? "Column of the model" : row.kind === "visual" ? `Visual: ${row.type}` :
        row.kind === "copy" ? "Copied as it is" : (row.where || "");
      return `<strong>${esc(row.letters ? row.letters + " " : "")}${esc(short(row.label, 70))}</strong><span>${esc(sub)}</span>`;
    }
    return "";
  }
  const catName = k => ((st.catalog && st.catalog.categories.find(c => c.key === k)) || {name: k}).name;
  const cats = o => (o.categories || [o.category]).map(catName);

  // ------------------------------------------------------------------ chrome: tools, bar, legend, list
  function renderAll() { renderChrome(); renderNav(); renderInfo(); renderEmpty(); }
  function seg(attr, options, cur, label) {
    return `<div class="seg" role="group" aria-label="${esc(label)}">${options.map(([v, text, title]) =>
      `<button data-${attr}="${v}" aria-pressed="${v === cur}" title="${esc(title || "")}">${esc(text)}</button>`).join("")}</div>`;
  }
  function renderChrome() {
    const t = [`<button class="tool" data-act="fit" title="Fit the drawing to the window (F)">Fit</button>`];
    const theme = `<button class="tool" data-act="theme" title="Switch between the sheet and blueprint colours">${document.documentElement.dataset.theme === "blueprint" ? "Sheet" : "Blueprint"}</button>`;
    if (st.collapsed.size && st.data) t.push(`<button class="tool" data-act="unfold" title="Show every dataset's columns again">Unfold all</button>`);
    if (!st.data && !st.loading) {                                  // nothing drawn: nothing to fit or refresh
      $("tools").innerHTML = theme;
      $("legend").hidden = true;
    } else {
    $("legend").hidden = false;
    if (st.mode === "notebook") {
      t.push(seg("only", [["all", "All columns", "Every column of its datasets"], ["changed", "Changed only", "Only the columns it changes, and where they come from"]], st.changedOnly ? "changed" : "all", "Columns"));
      t.push(seg("lanes", [["lanes", "Lanes", "A definition used by several columns is a block they pass straight through, one lane each"], ["bundled", "Bundled", "The columns gather into one point at a shared definition"]], st.lanes ? "lanes" : "bundled", "Shared definitions"));
    } else {
      t.push(`<button class="tool${st.focusOn ? " on" : ""}" data-act="focus" ${!st.sel && !st.focusOn ? "disabled" : ""} title="${st.focusOn ? "Show everything again" : "Show only the thread of what is selected"}">${st.focusOn ? "Leave focus" : "Focus"}</button>`);
      if (st.focus) {
        t.push(seg("dsscope", [["direct", "Direct links", "Only the notebooks right next to the dataset"], ["full", "Full lineage", "The whole thread"]], st.dsScope, "Scope"));
        if (!st.focus.startsWith("item:")) t.push(seg("dir", [["in", "Inputs", "Where it comes from"], ["both", "Both ways", ""], ["out", "Outputs", "Where it goes"]], st.direction, "Follow"));
        t.push(`<button class="tool" data-act="whole" title="Show every dataset the read notebooks touch">Whole workspace</button>`);
      }
      t.push(seg("cols", [["all", "All columns", "Every column, and its whole path (copies too): click one to follow it all the way"], ["changed", "Changed only", "Only the columns that change (new, altered, renamed), and where they come from"]], st.allColumns ? "all" : "changed", "Columns"));
    }
    t.push(`<button class="tool" data-act="refresh" title="Read the columns again from OneLake${st.mode === "notebook" ? " and the notebook from Fabric" : ""}">Refresh</button>`);
    if (st.moved.size) t.push(`<button class="tool" data-act="relayout" title="Put the nodes you moved back where the layout puts them">Re-layout</button>`);
    if (st.data) t.push(`<button class="tool" data-act="onmap" title="Show it on the workspace map">Show on the map</button>`);
    if (st.data) t.push(`<details class="cll-menu"><summary class="tool" title="Save the column lineage as a file">Export</summary><div class="cll-menu-list" role="menu">` +
      `<button role="menuitem" data-export="view-csv">This view as CSV</button><button role="menuitem" data-export="view-json">This view as JSON</button>` +
      `<button role="menuitem" data-export="ws-csv">Everything read, as CSV</button><button role="menuitem" data-export="ws-json">Everything read, as JSON with OpenLineage</button></div></details>`);
    if (st.data) t.push(`<button class="tool" data-act="link" title="Copy a link that opens this view">Copy link</button>`);
    t.push(theme);
    $("tools").innerHTML = t.join("");
    }
    const bar = $("bar");
    const d = st.data;
    let text = "";
    if (st.loading) text = st.mode === "notebook" ? "Reading the notebook and its datasets' columns…" : "Working out the column changes…";
    else if (d && st.mode === "notebook") {
      const ins = d.datasets.filter(x => x.role !== "target").length, outs = d.datasets.filter(x => x.role !== "source").length;
      text = `Column lineage of ${d.focus ? d.focus.name : "the notebook"}: ${ins} dataset${ins === 1 ? "" : "s"} in, ${outs} out, ${d.ops.length} definition${d.ops.length === 1 ? "" : "s"}${d.scope === "full" ? `, ${d.notebooks.length} notebook${d.notebooks.length === 1 ? "" : "s"}` : ""}`;
    } else if (d && st.focusOn) {
      text = `Focus on the thread of ${focusLabel()}`;
    } else if (d) {
      const f = st.focus && d.datasets.find(x => x.id === st.focus);
      const item = st.focus.startsWith("item:") && (d.cards || []).find(c => c.id === st.focus);
      text = item ? `The columns reaching ${item.name}, and the changes on their way` :
        f ? `Column changes along the thread of ${f.fullName}` : `Column changes across ${plural(d.notebooks.length, "notebook or pipeline", "notebooks and pipelines")} read`;
    }
    if (d && st.mode === "notebook" && st.changedOnly) text += ", changed columns only";
    const unread = d && (d.mode === "datasets" || d.scope === "full") ? d.unread : 0;
    bar.innerHTML = `<span>${st.loading ? '<i class="pulse"></i>' : ""}${esc(text)}</span>${st.focusOn ? '<button data-act="focus">Leave focus</button>' : ""}${unread && !st.focusOn ? `<button data-act="readall" title="Read the notebooks, pipelines, semantic models and reports not read yet, then work this out again">Read the other ${unread}</button>` : ""}`;
    bar.hidden = !text;
    renderLegend();
  }
  function renderLegend() {
    const c = st.catalog;
    const lines = st.mode === "notebook"
      ? `<div class="lg-line"><svg viewBox="0 0 34 12"><path d="M1 6H33" class="e e-data"/></svg>A column's path</div>
         <div class="lg-line"><svg viewBox="0 0 34 12"><path d="M1 6H33" class="e e-cond"/></svg>Looked at, not passed on</div>`
      : `<div class="lg-line"><svg viewBox="0 0 34 12"><path d="M1 6H33" class="e e-rel"/></svg>Dataset relation</div>
         <div class="lg-line"><svg viewBox="0 0 34 12"><path d="M1 6H33" class="e e-chg"/></svg>Changed column</div>
         <div class="lg-line"><svg viewBox="0 0 34 12"><path d="M1 6H33" class="e e-copy"/></svg>Carried on by a copy</div>`;
    const bi = st.data && st.data.edges.some(e => e.kind === "bi") ? `<div class="lg-line"><svg viewBox="0 0 34 12"><path d="M1 6H33" class="e e-bi"/></svg>Into Power BI: model, measures, visuals</div>` : "";
    const items = c ? c.categories.map(k => `<div class="lg-cat cat-${esc(k.key)}" title="${esc(k.description)}: ${esc(k.examples)}"><svg viewBox="0 0 20 20">${balloon(k.letter, "", 10, 10, 8.5)}</svg>${esc(k.name)}${k.scope === "row" ? '<small>rows</small>' : ""}</div>`).join("") : "";
    $("legendBody").innerHTML = `${lines}${bi}<div class="lg-cats">${items}</div><p class="lg-note">Under the line in a balloon: where the code is (cell:line, a pipeline activity, or DAX).</p>`;
  }
  function renderNav() {
    const list = $("list"), q = st.filter.toLowerCase();
    if (!host.workspaceId()) { list.innerHTML = '<p class="sub-note">Open a workspace first.</p>'; return; }
    if (st.mode === "notebook") {
      const item = n => {
        const s = host.readState(n.itemId);
        return `<li><button class="row${n.itemId === st.notebook ? " sel" : ""}" data-nb="${esc(n.itemId)}" title="${esc(n.label)}"><span class="st ${s === "ok" ? "ok" : s === "error" ? "err" : s === "busy" ? "busy" : ""}"></span><span class="nm">${esc(n.label)}</span></button></li>`;
      };
      const nbs = host.items("Notebook").filter(n => !q || n.label.toLowerCase().includes(q));
      const pls = host.items("DataPipeline").filter(n => !q || n.label.toLowerCase().includes(q));
      list.innerHTML = (nbs.length ? `<ul class="cll-items">${nbs.map(item).join("")}</ul>` : "") +
        (pls.length ? `<div class="ds-head"><span>Pipelines (Copy activities)</span></div><ul class="cll-items">${pls.map(item).join("")}</ul>` : "") ||
        '<p class="sub-note">Nothing matches.</p>';
      return;
    }
    const ds = st.dsList || (st.data && !st.focus ? st.data.datasets : null);
    if (!ds) { list.innerHTML = '<p class="sub-note">Working out which datasets the notebooks touch…</p>'; return; }
    const groups = new Map();
    for (const d of ds) {
      if (q && !d.fullName.toLowerCase().includes(q)) continue;
      const g = d.lakehouseName || "Other";
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(d);
    }
    let html = `<button class="row cll-whole${!st.focus ? " sel" : ""}" data-ds="">Whole workspace</button>`;
    for (const [type, head] of [["SemanticModel", "Semantic models"], ["Report", "Reports"]]) {
      const items = host.items(type).filter(n => !q || n.label.toLowerCase().includes(q));
      if (items.length) html += `<div class="ds-head"><span>${head}</span></div><ul class="cll-items">` + items.map(n =>
        `<li><button class="row${st.focus === "item:" + n.itemId ? " sel" : ""}" data-ds="item:${esc(n.itemId)}" title="${esc(n.label)}"><span class="nm">${esc(n.label)}</span></button></li>`).join("") + "</ul>";
    }
    for (const [g, items] of [...groups].sort((a, b) => a[0].localeCompare(b[0]))) {
      html += `<div class="ds-head"><i style="background:${esc(colorOf(items[0]))}"></i><span>${esc(g)}</span></div><ul class="cll-items">` +
        items.sort((a, b) => a.label.localeCompare(b.label)).map(d => {
          const n = (d.columns || []).filter(c => ["new", "altered", "renamed"].includes(c.status)).length;
          return `<li><button class="row${d.id === st.focus ? " sel" : ""}" data-ds="${esc(d.id)}" title="${esc(d.fullName)}"><span class="nm">${esc(d.label)}</span>${n ? `<span class="hint chg-count">${n} changed</span>` : ""}</button></li>`;
        }).join("") + "</ul>";
    }
    list.innerHTML = html;
  }
  function renderEmpty() {
    const el = $("empty");
    let html = "";
    if (!host.workspaceId()) html = `<h1>Column-Level Lineage</h1><p>Open a workspace on the map first.</p>`;
    else if (st.error && !st.data) html = `<div class="error-box"><b>${esc(st.error.message || String(st.error))}</b><p>${esc(st.error.hint || "")}</p></div><p><button class="btn" data-act="retry">Try again</button></p>`;
    else if (st.mode === "notebook" && !st.notebook) html = `<h1>Pick a notebook</h1><p>Choose a notebook on the left, or right-click a notebook on the map and choose <b>Focus on column lineage</b>. You'll see the datasets it reads and writes, every column, and what the code does to each one.</p>`;
    else if (st.data && st.mode === "datasets" && !st.data.notebooks.length) html = `<h1>No notebooks read yet</h1><p>Column changes come from the notebooks' code. Read the notebooks first.</p><p><button class="btn primary" data-act="readall">Read all notebooks</button></p>`;
    el.innerHTML = html ? `<div class="empty-card">${html}</div>` : "";
    el.hidden = !html;
    $("legend").hidden = !!html || !st.data;                    // nothing drawn: nothing to explain
  }

  // ------------------------------------------------------------------ details panel
  function renderInfo() {
    const box = $("info"), d = st.data;
    if (!d) { box.innerHTML = st.loading ? '<div class="spinner"></div>' : ""; return; }
    if (selected().length > 1) { box.innerHTML = selectionInfo(); return; }
    const s = st.sel;
    const n = s && st.graph && st.graph.byId.get(s.node);
    if (s && s.port && n && n.kind === "dataset") box.innerHTML = columnInfo(n, s.port);
    else if (n && (n.kind === "op" || n.kind === "manifold" || n.kind === "block")) box.innerHTML = opInfo(n.data, s.port);
    else if (n && n.kind === "dataset") box.innerHTML = datasetInfo(n.data);
    else if (n && n.kind === "card") box.innerHTML = s.port && s.port !== `${n.id}|` ? rowInfo(n, s.port) : notebookInfo(n.data);
    else box.innerHTML = overviewInfo();
  }
  const codeBlock = (o, title = "") => `<div class="ev">${title ? `<div>${esc(title)}</div>` : ""}<code>${esc(o.code || o.label)}</code></div>`;
  const opLine = (o, extra = "") => `<li class="cll-step cat-${esc(o.category)}"><button class="ref" data-sel-node="${esc(o.id)}"><svg viewBox="0 0 22 22" class="mini">${balloon(o.letters, "", 11, 11, 9.5)}</svg><span>${esc(short(o.label, 60))}</span></button><span class="chip">${esc(o.where || `cell ${o.cell}, line ${o.line}`)}</span>${extra}</li>`;
  const colLabel = port => {
    if (!st.graph) return port;
    const [n, c] = colOf(port);
    if (c) return `${n.data.label}.${c.star ? "*" : c.name}`;
    const node = st.graph.nodes.find(x => x.kind === "card" && x.rows.some(r => r.id === port));
    return node ? `${node.data.name}: ${node.rows.find(r => r.id === port).label}` : "(made by the code)";
  };
  function overviewInfo() {
    const d = st.data;
    let h = "";
    if (st.mode === "notebook") {
      const f = d.focus || {};
      h += `<div class="d-type">Notebook column lineage</div><h2 class="d-title">${esc(f.name || "")}</h2><div class="d-actions">${f.url ? `<a class="btn" href="${esc(f.url)}" target="_blank" rel="noopener">${EXT_SVG}Open in Fabric</a>` : ""}<button class="btn" data-act="onmap">Show on the map</button></div>`;
      if (st.error) h += `<div class="d-error"><b>${esc(st.error.message)}</b> ${esc(st.error.hint || "")}</div>`;
      const rows = d.ops.filter(o => o.scope === "row").length;
      h += `<p class="d-summary">Reads ${plural(d.datasets.filter(x => x.role !== "target").length, "dataset")} and writes ${plural(d.datasets.filter(x => x.role !== "source").length, "dataset")}. ${plural(d.ops.length, "definition")}: ${rows} shared by every column passing through, ${d.ops.length - rows} on single columns.</p>`;
      h += `<div class="d-sec">How to read it</div><p class="d-note">Hover a column to follow it; click to keep it lit. A balloon is a piece of the notebook's code: its letters say what it does, the number under the line is its cell and line. A bar with lanes is a shared definition (rows filtered, joined, grouped or merged): every column on a lane passes through it. Green marks a column the notebook changes.</p>`;
      const changed = d.datasets.flatMap(x => x.columns.filter(c => ["new", "altered", "renamed"].includes(c.status)).map(c => [x, c]));
      if (changed.length) h += `<div class="d-sec">Columns it changes <small>${changed.length}</small></div><ul class="refs">${changed.map(([x, c]) => `<li><div class="ref-line"><button class="ref" data-sel-port="${esc(c.id)}" data-sel-node="${esc(x.id)}"><span>${esc(x.label)}.${esc(c.name)}</span></button><span class="chip st-${esc(c.status)}">${esc(c.status)}</span></div></li>`).join("")}</ul>`;
    } else {
      h += `<div class="d-type">Across datasets</div><h2 class="d-title">${esc(st.focus ? (d.datasets.find(x => x.id === st.focus) || {}).fullName || "Thread" : host.workspaceName())}</h2>`;
      h += `<p class="d-summary">${plural(d.notebooks.length, "notebook")} read, ${plural(d.datasets.length, "dataset")}. Black lines: a notebook reads one dataset and writes another. Green lines: columns that are new, altered or renamed on the way; columns copied as they are aren't shown.</p>`;
      if (d.unread) h += `<p class="d-note">${plural(d.unread, "notebook")} haven't been read yet, so their changes aren't here. <button class="btn small" data-act="readall">Read them</button></p>`;
      h += `<div class="d-sec">How to read it</div><p class="d-note">Hover a column to follow its thread through the notebooks; click to keep it lit. Each notebook lists the definitions that change columns, with the letters of what they do; the letters in its heading are what it does to rows.</p>`;
    }
    const notes = st.mode === "notebook" ? d.notes || [] : [];
    if (notes.length) h += `<div class="d-sec">What couldn't be followed <small>${notes.length}</small></div>${notes.map(t => `<p class="d-note">${esc(t)}</p>`).join("")}`;
    return h;
  }
  function columnInfo(n, port) {
    const d = n.data, c = n.rows.find(x => x.id === port);
    if (!c) return overviewInfo();
    let h = `<div class="d-type"><i style="background:${esc(colorOf(d))}"></i>Column of ${esc(d.fullName)}</div><h2 class="d-title">${esc(c.star ? "The other columns" : c.name)}</h2>`;
    h += `<dl class="kv">${c.type ? `<dt>Type</dt><dd>${esc(c.type)}</dd>` : ""}<dt>Here</dt><dd>${esc(STATUS_TEXT[c.status] || (c.used === false ? "Not used by the code shown" : "Read by the code"))}</dd>${c.inferred ? "<dt>Note</dt><dd>Named by the code; the table's columns couldn't be read</dd>" : ""}${c.inTable === false ? "<dt>Note</dt><dd>Written by the code, but the table as read from OneLake doesn't have it</dd>" : ""}</dl>`;
    const ops = new Map(st.graph.nodes.filter(x => ["op", "manifold", "block"].includes(x.kind)).map(x => [x.id, x.data]));
    if (st.mode === "notebook") {
      const steps = stepsInto(st.graph, st.idx, port, ops);
      if (steps.length) {
        h += `<div class="d-sec">How it is made <small>${plural(steps.length, "path")}</small></div>`;
        h += steps.slice(0, 12).map(sp => `<p class="cll-from">From <button class="linkish" data-sel-port="${esc(sp.from)}" data-sel-node="${esc(sp.from.split("|")[0])}">${esc(colLabel(sp.from))}</button></p>${sp.ops.length ? `<ul class="refs cll-steps">${sp.ops.map(id => opLine(ops.get(id))).join("")}</ul>` : '<p class="d-note">Copied as it is.</p>'}`).join("");
      }
    } else {
      const into = (st.idx.endsAt.get(port) || []).map(f => st.graph.flows[f]);
      if (into.length) h += `<div class="d-sec">How it changes</div><ul class="refs">${into.map(f => `<li><div class="ref-line"><span>${esc(colLabel(f.from))}</span></div><div class="ev"><div>${esc(f.edges.length > 1 ? "via" : "made by")} ${esc(colLabel(st.idx.edgeByIndex.get(f.edges[f.edges.length - 1]).from))}</div></div></li>`).join("")}</ul>`;
    }
    const out = (st.idx.startsAt.get(port) || []).map(f => st.graph.flows[f].to);
    if (out.length) h += `<div class="d-sec">Goes to <small>${out.length}</small></div><ul class="refs">${[...new Set(out)].map(p => `<li><div class="ref-line"><button class="ref" data-sel-port="${esc(p)}" data-sel-node="${esc(p.split("|")[0])}"><span>${esc(colLabel(p))}</span></button></div></li>`).join("")}</ul>`;
    h += `<div class="d-actions"><button class="btn" data-act="reveal" data-id="${esc(d.table)}">Show the table on the map</button>${st.mode === "notebook" ? `<button class="btn" data-act="dsfocus" data-id="${esc(d.table)}">Its column changes across datasets</button>` : ""}</div>`;
    return h;
  }
  function opInfo(o, port) {
    let h = `<div class="d-type cat-${esc(o.category)}"><svg viewBox="0 0 22 22" class="mini">${balloon(o.letters, "", 11, 11, 9.5)}</svg>${esc(cats(o).join(", "))}</div><h2 class="d-title cll-code-title">${esc(o.label)}</h2>`;
    h += `<dl class="kv"><dt>Where</dt><dd>${esc(o.where || `Cell ${o.cell}, line ${o.line}`)}</dd><dt>Applies to</dt><dd>${o.scope === "row" ? "Rows: every column passing through goes through it" : "The values of the columns on its lines"}</dd>${o.detail ? `<dt>Note</dt><dd>${esc(o.detail)}</dd>` : ""}${(o.functions || []).length ? `<dt>Functions</dt><dd>${esc(o.functions.join(", "))}</dd>` : ""}</dl>`;
    h += codeBlock(o, "The code");
    const thr = threadOf(st.graph, st.idx, {node: o.id});
    const ins = [...new Set([...thr.flows].map(f => st.graph.flows[f].from))].filter(p => !p.startsWith("op:"));
    const outs = [...new Set([...thr.flows].map(f => st.graph.flows[f].to))];
    if (ins.length) h += `<div class="d-sec">Columns in <small>${ins.length}</small></div><ul class="refs">${ins.slice(0, 60).map(p => `<li><div class="ref-line"><button class="ref" data-sel-port="${esc(p)}" data-sel-node="${esc(p.split("|")[0])}"><span>${esc(colLabel(p))}</span></button></div></li>`).join("")}</ul>`;
    if (outs.length) h += `<div class="d-sec">Columns out <small>${outs.length}</small></div><ul class="refs">${outs.slice(0, 60).map(p => `<li><div class="ref-line"><button class="ref" data-sel-port="${esc(p)}" data-sel-node="${esc(p.split("|")[0])}"><span>${esc(colLabel(p))}</span></button></div></li>`).join("")}</ul>`;
    return h;
  }
  function datasetInfo(d) {
    const counts = {};
    for (const c of d.columns) counts[c.status || "read"] = (counts[c.status || "read"] || 0) + 1;
    let h = `<div class="d-type"><i style="background:${esc(colorOf(d))}"></i>${d.kind === "file" ? "Folder" : "Dataset"}${d.lakehouseName ? ` in ${esc(d.lakehouseName)}` : ""}</div><h2 class="d-title">${esc(d.fullName)}</h2>`;
    h += `<div class="d-actions"><button class="btn" data-act="reveal" data-id="${esc(d.table)}">Show on the map</button>${st.mode === "notebook" ? `<button class="btn" data-act="dsfocus" data-id="${esc(d.table)}">Its column changes across datasets</button>` : `<button class="btn" data-act="dsfocus" data-id="${esc(d.table)}">Follow its thread</button>`}</div>`;
    h += `<dl class="kv">${Object.entries(counts).map(([k, v]) => `<dt>${esc(STATUS_TEXT[k] || (k === "read" ? "Read" : k))}</dt><dd>${v}</dd>`).join("")}${d.schema && d.schema.via ? `<dt>Columns from</dt><dd>${esc(d.schema.via)}</dd>` : ""}</dl>`;
    if (d.schema && (d.schema.hint || d.schema.error)) h += `<p class="d-note">Its columns couldn't be read: ${esc(d.schema.hint || d.schema.error)} The columns shown are the ones the code names.</p>`;
    if ((d.writes || []).length) h += `<div class="d-sec">Written by</div><ul class="refs">${d.writes.map(w => `<li><div class="ref-line"><span>${esc(nbName(w.notebook))}</span><span class="chip">${esc(w.verb)}${w.mode ? `, ${esc(w.mode)}` : ""}</span><span class="chip">cell ${w.cell}, line ${w.line}</span></div></li>`).join("")}</ul>`;
    return h;
  }
  const nbName = id => ((st.data.notebooks || []).find(n => n.id === id) || {name: id}).name;
  function notebookInfo(nb) {
    const word = {notebook: "Notebook", pipeline: "Pipeline", model: "Semantic model", report: "Report"}[nb.kind] || "Notebook";
    const stage = !nb.kind || nb.kind === "notebook" || nb.kind === "pipeline";
    let h = `<div class="d-type">${word}</div><h2 class="d-title">${esc(nb.name)}</h2><div class="d-actions">${stage ? `<button class="btn primary" data-act="nbfocus" data-id="${esc(nb.itemId)}">Focus on column lineage</button>` : ""}${nb.url ? `<a class="btn" href="${esc(nb.url)}" target="_blank" rel="noopener">${EXT_SVG}Open in Fabric</a>` : ""}</div>`;
    if (nb.kind === "model") h += `<p class="d-note">The model's columns that read the columns drawn here, the measures and calculated columns built on them (DAX balloons), and the report visuals that show any of them.</p>`;
    if (nb.kind === "report") h += `<p class="d-note">The visuals that show a field reached by the columns drawn here.</p>`;
    if ((nb.rowOps || []).length) h += `<div class="d-sec">What it does to rows</div><ul class="refs">${nb.rowOps.map(r => `<li class="cll-step cat-${esc(r.category)}"><div class="ref-line"><svg viewBox="0 0 22 22" class="mini">${balloon(r.letters, "", 11, 11, 9.5)}</svg><span>${esc(short(r.label, 70))}</span></div></li>`).join("")}</ul>`;
    if ((nb.rows || []).length) h += `<div class="d-sec">${stage ? "Definitions that change columns" : nb.kind === "model" ? "Its columns reached" : "Its visuals reached"} <small>${nb.rows.length}</small></div><ul class="refs">${nb.rows.map(r => `<li class="cll-step cat-${esc(r.category)}"><button class="ref" data-sel-port="${esc(r.id)}" data-sel-node="${esc(nb.id)}"><svg viewBox="0 0 22 22" class="mini">${balloon(r.letters, "", 11, 11, 9.5)}</svg><span>${esc(short(r.label, 60))}</span></button></li>`).join("")}</ul>`;
    if ((nb.notes || []).length) h += `<div class="d-sec">What couldn't be followed</div>${nb.notes.map(t => `<p class="d-note">${esc(t)}</p>`).join("")}`;
    return h;
  }
  function rowInfo(n, port) {
    const r = n.rows.find(x => x.id === port);
    if (!r) return notebookInfo(n.data);
    const bi = r.kind === "column" || r.kind === "visual";
    let h = `<div class="d-type cat-${esc(r.category)}">${bi ? "" : `<svg viewBox="0 0 22 22" class="mini">${balloon(r.letters, "", 11, 11, 9.5)}</svg>`}In ${esc(n.data.name)}</div><h2 class="d-title cll-code-title">${esc(r.label)}</h2>` +
      (bi ? `<dl class="kv"><dt>Kind</dt><dd>${r.kind === "column" ? "A column of the model" : `A visual (${esc(r.type)})`}</dd>${r.type && r.kind === "column" ? `<dt>Type</dt><dd>${esc(r.type)}</dd>` : ""}</dl>` :
        r.kind === "copy" ? `<p class="d-note">Copied as it is.</p>` : `<dl class="kv"><dt>Where</dt><dd>${esc(r.where || `Cell ${r.cell}, line ${r.line}`)}</dd></dl>${codeBlock(r, "The code")}`);
    const fl = st.idx.byPort.get(port) || [];
    const ins = [...new Set(fl.map(f => st.graph.flows[f].from).filter(p => p !== port))], outs = [...new Set(fl.map(f => st.graph.flows[f].to))];
    if (ins.length) h += `<div class="d-sec">From</div><ul class="refs">${ins.map(p => `<li><div class="ref-line"><span>${esc(colLabel(p))}</span></div></li>`).join("")}</ul>`;
    if (outs.length) h += `<div class="d-sec">Makes</div><ul class="refs">${outs.map(p => `<li><div class="ref-line"><span>${esc(colLabel(p))}</span></div></li>`).join("")}</ul>`;
    return h + (n.card === "notebook" || n.card === "pipeline" ? `<div class="d-actions"><button class="btn" data-act="nbfocus" data-id="${esc(n.data.itemId)}">Focus on column lineage</button></div>` : "");
  }
  const plural = (n, w, many) => `${n} ${n === 1 ? w : many || w + "s"}`;
  function nameOf(t) {
    const n = st.graph && st.graph.byId.get(t.node);
    if (!n) return t.node;
    if (n.kind === "dataset") {
      const c = t.port && n.rows.find(r => r.id === t.port);
      return c ? `${n.data.label}.${c.star ? "*" : c.name}` : n.data.fullName;
    }
    if (n.kind === "card") { const r = t.port && n.rows.find(x => x.id === t.port); return r ? r.label : n.data.name; }
    return short(n.data.label, 60);
  }
  function selectionInfo() {
    const sels = selected();
    return `<div class="d-type">Selection</div><h2 class="d-title">${sels.length} selected</h2>` +
      `<p class="d-note">Their lines are lit together. Shift+click adds or removes one, Shift+drag on the sheet draws a box around more, and dragging one of them moves them all.</p>` +
      `<div class="d-actions">${MODE === "datasets" ? `<button class="btn" data-act="focus">${st.focusOn ? "Leave focus" : "Focus on their threads"}</button>` : ""}<button class="btn" data-act="clearsel">Clear the selection</button></div>` +
      `<ul class="refs">${sels.map(t => `<li><div class="ref-line"><button class="ref" data-sel-port="${esc(t.port || "")}" data-sel-node="${esc(t.node)}" title="Select only this"><span>${esc(nameOf(t))}</span></button></div></li>`).join("")}</ul>`;
  }
  function focusLabel() {
    const t = st.focusOn;
    if (!t || !st.full) return "";
    if (Array.isArray(t)) return `${t.length} selected`;
    if (t.port) {
      const n = (st.full.datasets || []).find(d => d.id === nodeOfPort(t.port));
      const c = n && n.columns.find(x => x.id === t.port);
      if (c) return `${n.label}.${c.name}`;
      const card = (st.full.cards || []).find(x => x.id === nodeOfPort(t.port));
      const r = card && card.rows.find(x => x.id === t.port);
      if (r) return r.label;
    }
    const d = (st.full.datasets || []).find(x => x.id === t.node) || (st.full.cards || []).find(x => x.id === t.node);
    const o = (st.full.ops || []).find(x => x.id === t.node);
    return d ? (d.fullName || d.name) : o ? o.label : "the selection";
  }

  // ------------------------------------------------------------------ links (the address bar follows the view)
  function linkParams() {
    const p = {v: VIEW};
    if (st.mode === "notebook") { p.nb = st.notebook; if (st.changedOnly) p.o = 1; if (!st.lanes) p.b = 1; }
    else {
      if (st.focus) { p.f = st.focus; p.ds = st.dsScope; p.d = st.direction; }
      if (st.allColumns) p.a = 1;
      if (st.focusOn) p.x = 1;
    }
    if (st.sel && st.sel.port) p.c = st.sel.port; else if (st.sel && st.sel.node) p.n = st.sel.node;
    return p;
  }
  function syncHash() { if (st.visible && st.full) host.setHash(linkParams()); }
  async function copyLink() {
    const url = host.link(linkParams());
    try {
      await navigator.clipboard.writeText(url);
      host.toast("Link copied. It opens this view in Fabric Explorer (click it, or paste it on the page with Ctrl+V).");
    } catch { window.prompt("Copy this link: it opens this view in Fabric Explorer", url); }
  }

  // ------------------------------------------------------------------ find a column
  const search = $("search"), hitsBox = $("hits");
  function findables() {
    const out = [];
    if (!st.graph) return out;
    for (const n of st.graph.nodes) {
      if (n.kind === "dataset") for (const c of n.rows) out.push({port: c.id, node: n.id, label: `${n.data.label}.${c.star ? "*" : c.name}`, sub: n.data.lakehouseName || ""});
      else if (n.kind === "card") for (const r of n.rows) out.push({port: r.id, node: n.id, label: r.label, sub: n.data.name});
      else out.push({node: n.id, label: n.data.label, sub: cats(n.data).join(", ")});
    }
    return out;
  }
  function renderHits() {
    const q = search.value.trim().toLowerCase();
    if (!q) { hitsBox.hidden = true; st.hits = []; return; }
    const all = findables(), name = x => x.label.toLowerCase().split(/[.[\]]/).filter(Boolean).pop() || "";
    st.hits = all.filter(x => x.label.toLowerCase().includes(q))
      .sort((a, b) => (name(b) === q) - (name(a) === q) || (name(b).startsWith(q)) - (name(a).startsWith(q)) || a.label.localeCompare(b.label))
      .slice(0, 40);
    st.hit = 0;
    hitsBox.innerHTML = st.hits.length ? st.hits.map((x, i) => `<li><button data-hit="${i}" class="${i === 0 ? "on" : ""}"><span>${esc(short(x.label, 60))}</span><small>${esc(short(x.sub, 40))}</small></button></li>`).join("") :
      `<li class="none">Nothing in the drawing matches${st.changedOnly || st.focusOn ? " (some columns are hidden)" : ""}.</li>`;
    hitsBox.hidden = false;
  }
  function pickHit(i) {
    const x = st.hits[i];
    if (!x) return;
    hitsBox.hidden = true; search.value = ""; search.blur();
    select(x.port ? {port: x.port, node: x.node} : {node: x.node});
    centerOnPort(x.port, x.node);
  }
  search.addEventListener("input", renderHits);
  search.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      st.hit = Math.max(0, Math.min(st.hits.length - 1, st.hit + (ev.key === "ArrowDown" ? 1 : -1)));
      hitsBox.querySelectorAll("[data-hit]").forEach((b, i) => b.classList.toggle("on", i === st.hit));
    } else if (ev.key === "Enter") { ev.preventDefault(); pickHit(st.hit); }
    else if (ev.key === "Escape") { search.value = ""; hitsBox.hidden = true; search.blur(); }
  });
  search.addEventListener("blur", () => setTimeout(() => { hitsBox.hidden = true; }, 150));

  // ------------------------------------------------------------------ export
  const COLUMNS = ["target", "target_column", "status", "source", "source_column", "via", "via_type", "column_definitions",
    "row_definitions", "where", "write", "via_url"];
  function viewRows() {
    const g = st.graph, idx = st.idx, rows = [];
    if (!g) return rows;
    const ops = new Map(g.nodes.filter(n => ["op", "manifold", "block"].includes(n.kind)).map(n => [n.id, n.data]));
    const stageName = new Map(((st.data && st.data.notebooks) || []).map(nb => [nb.id, nb.name]));
    const place = port => {
      const id = nodeOfPort(port), n = g.byId.get(id);
      if (n && n.kind === "dataset") { const c = n.rows.find(r => r.id === port); return [n.data.fullName, c ? (c.star ? "*" : c.name) : "", c ? c.status : ""]; }
      if (n && n.kind === "card") { const r = n.rows.find(x => x.id === port); return [n.data.name, r ? r.label : "", r ? r.kind : ""]; }
      if (ops.has(id)) return ["", ops.get(id).label, "definition"];
      return ["", "", ""];
    };
    for (const f of g.flows) {
      if (f.kind === "condition") continue;
      const steps = [], via = new Set();
      for (const ei of f.edges) {
        const e = idx.edgeByIndex.get(ei);
        if (!e) continue;
        for (const id of [e.fromNode, e.toNode]) {
          const n = g.byId.get(id);
          if (n && n.kind === "card") via.add(n.data.name);
          if (ops.has(id) && !steps.includes(id)) steps.push(id);
        }
      }
      for (const id of steps) { const nb = ops.get(id).notebook; if (stageName.has(nb)) via.add(stageName.get(nb)); }
      const defs = row => steps.map(id => ops.get(id)).filter(o => (o.scope === "row") === row).map(o => `${o.letters} ${o.label}`).join(" ▸ ");
      const [source, sourceCol] = place(f.from), [target, targetCol, status] = place(f.to);
      rows.push({target, target_column: targetCol, status, source, source_column: sourceCol, via: [...via].join(", "),
        via_type: f.kind, column_definitions: defs(false), row_definitions: defs(true),
        where: [...new Set(steps.map(id => ops.get(id).where || `Cell ${ops.get(id).cell}, line ${ops.get(id).line}`))].join("; "),
        write: "", via_url: ""});
    }
    return rows;
  }
  const csv = (rows, cols) => "\ufeff" + [cols.join(","), ...rows.map(r => cols.map(c => {
    const v = String(r[c] ?? "");
    return /[",\r\n;]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  }).join(","))].join("\r\n");
  function download(name, text, type) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], {type}));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }
  const slug = s => String(s || "column-lineage").replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "column-lineage";
  async function exportAs(kind) {
    for (const d of root.querySelectorAll(".cll-menu[open]")) d.open = false;
    if (kind.startsWith("view")) {
      const name = st.mode === "notebook" ? (st.full.focus || {}).name : focusLabel() || (st.focus ? st.focus : host.workspaceName());
      const base = `column-lineage-${slug(name)}`, rows = viewRows();
      if (kind === "view-csv") download(`${base}.csv`, csv(rows, COLUMNS), "text/csv;charset=utf-8");
      else download(`${base}.json`, JSON.stringify({workspace: host.workspaceName(), view: linkParams(), link: host.link(linkParams()),
        generated: new Date().toISOString(), columns: COLUMNS, rows}, null, 2), "application/json");
      host.toast(`Saved ${plural(rows.length, "column path")}.`);
      return;
    }
    try {
      host.toast("Working out every column path read…");
      const x = await host.api("/api/column-lineage/export", {workspaceId: host.workspaceId()});
      const base = `column-lineage-${slug(x.workspace.name)}`;
      if (kind === "ws-csv") download(`${base}.csv`, csv(x.rows, x.columns), "text/csv;charset=utf-8");
      else download(`${base}.json`, JSON.stringify(x, null, 2), "application/json");
      host.toast(`Saved ${plural(x.rows.length, "column path")} from ${plural(x.openlineage.length, "notebook or pipeline", "notebooks and pipelines")}.`);
    } catch (err) { host.toast(err.message || String(err)); }
  }

  // ------------------------------------------------------------------ actions
  function openNotebook(itemId) {                           // the Notebook-Level Lineage tab
    if (MODE !== "notebook") { host.showView("notebooks", {mode: "notebook", notebookId: itemId}); return; }
    st.notebook = itemId; st.sel = null; save(); renderAll(); load();
  }
  function openDatasets(focus = "") {                       // the Column-Level Lineage tab
    if (MODE !== "datasets") { host.showView("columns", {mode: "datasets", focus}); return; }
    st.focus = focus; st.sel = null; save(); renderAll(); load();
  }
  function act(a, el) {
    if (a === "fit") fit();
    else if (a === "refresh") {
      if (st.mode === "notebook" && st.notebook) { host.refresh(st.notebook); setTimeout(() => load(true), 400); }
      else load(true);
    } else if (a === "onmap") host.reveal(st.mode === "notebook" ? "item:" + st.notebook : (st.focus || ""));
    else if (a === "reveal") host.reveal(el.dataset.id);
    else if (a === "dsfocus") openDatasets(el.dataset.id);
    else if (a === "nbfocus") openNotebook(el.dataset.id);
    else if (a === "whole") openDatasets("");
    else if (a === "readall") { st.waiting = host.readAll(["Notebook", "DataPipeline", "SemanticModel", "Report"]) > 0; if (!st.waiting) load(); }
    else if (a === "retry") load();
    else if (a === "theme") { host.toggleTheme(); renderChrome(); }
    else if (a === "relayout") { st.moved.clear(); draw(); renderChrome(); }
    else if (a === "unfold") fold([...st.collapsed], false);
    else if (a === "clearsel") select(null);
    else if (a === "focus") {
      if (st.focusOn) { st.focusOn = null; draw(); fit(false, true); if (st.sel) centerOnPort(st.sel.port, st.sel.node); }
      else if (st.sel) { const sels = selected(); st.focusOn = sels.length > 1 ? sels.slice() : st.sel; draw(); fit(); }
      renderChrome(); renderInfo(); syncHash();
    } else if (a === "link") copyLink();
  }
  root.addEventListener("click", ev => {
    const a = ev.target.closest("[data-act]");
    if (a && root.contains(a)) { act(a.dataset.act, a); return; }
    const ex = ev.target.closest("[data-export]");
    if (ex) { exportAs(ex.dataset.export); return; }
    const hit = ev.target.closest("[data-hit]");
    if (hit) { pickHit(+hit.dataset.hit); return; }
    const lanesBtn = ev.target.closest("[data-lanes]");
    if (lanesBtn) {
      st.lanes = lanesBtn.dataset.lanes === "lanes"; save();
      draw(); fit(false, true); renderChrome(); syncHash();
      return;
    }
    const only = ev.target.closest("[data-only]");
    if (only) {
      st.changedOnly = only.dataset.only === "changed"; save();
      draw(); fit(false, true); renderChrome(); renderInfo(); syncHash();
      return;
    }
    const b = ev.target.closest("[data-scope], [data-dsscope], [data-dir], [data-cols], [data-ctab], [data-nb], [data-ds]");
    if (b) {
      if (b.dataset.scope) { st.scope = b.dataset.scope; save(); load(); }
      else if (b.dataset.dsscope) { st.dsScope = b.dataset.dsscope; save(); load(); }
      else if (b.dataset.dir) { st.direction = b.dataset.dir; save(); load(); }
      else if (b.dataset.cols) { st.allColumns = b.dataset.cols === "all"; load(); }
      else if (b.dataset.nb !== undefined) openNotebook(b.dataset.nb);
      else if (b.dataset.ds !== undefined) openDatasets(b.dataset.ds);
      return;
    }
    const foldBtn = ev.target.closest("[data-fold]");
    if (foldBtn) { fold([foldBtn.dataset.fold], !st.collapsed.has(foldBtn.dataset.fold)); return; }
    const fnb = ev.target.closest("[data-focus-nb]");
    if (fnb) { openNotebook(fnb.dataset.focusNb); return; }
    const s = ev.target.closest("[data-sel-port], [data-sel-node]");
    if (s) {
      const node = s.dataset.selNode, port = s.dataset.selPort || "";
      select(port ? {port, node} : {node});
      centerOnPort(port, node);
    }
  });
  $("filter").addEventListener("input", ev => { st.filter = ev.target.value.trim(); renderNav(); });
  host.on("workspace", () => { st.data = st.full = null; st.dsList = null; st.sel = st.focusOn = null; world.innerHTML = ""; if (st.visible) { renderAll(); load(); } });
  host.on("scan", () => {
    if (!st.visible) { st.stale = true; return; }               // worked out again when the tab is shown
    renderNav();
    if (st.waiting && !host.busyCount()) { st.waiting = false; load(); }
  });
  // the drawing is fitted once it has a size to fit into (the tab may have been hidden while it loaded)
  new ResizeObserver(() => { if (st.visible && st.lay && st.needsFit) fit(false, true); }).observe(svg);

  return {
    show(args = {}) {
      root.hidden = false; st.visible = true;
      if (args.link) {                                       // opened from a link: everything it says
        st.pending = {select: args.select || "", node: args.node || "", focusMode: !!args.focusMode};
        if (MODE === "datasets") {
          Object.assign(st, {mode: "datasets", focus: args.focus || "", dsScope: args.dsScope || "full",
            direction: args.direction || "both", allColumns: !!args.allColumns});
        } else {
          Object.assign(st, {mode: "notebook", notebook: args.notebookId || st.notebook, scope: "direct",
            changedOnly: !!args.changedOnly, lanes: !args.bundled});
        }
        st.subject = ""; st.sel = st.focusOn = null; save(); renderAll(); load();
        return;
      }
      if (MODE === "notebook" && args.notebookId) { openNotebook(args.notebookId); return; }
      if (MODE === "datasets" && args.focus !== undefined) { openDatasets(args.focus || ""); return; }
      renderAll();
      if ((!st.full || st.stale) && host.workspaceId()) { st.stale = false; load(); }
      else if (st.needsFit) requestAnimationFrame(() => fit(false, true));     // otherwise: where the user left it
    },
    hide() { root.hidden = true; st.visible = false; hideTip(); },
    onKey(ev) {
      if (ev.key === "Escape") { if (st.sel || st.hover) { st.hover = null; select(null); return true; } return false; }
      if (ev.key === "f" || ev.key === "F") { fit(); return true; }
      if (ev.key === "/") { search.focus(); return true; }
      if (ev.key === "+" || ev.key === "=" || ev.key === "-") {
        const r = svg.getBoundingClientRect();
        zoomAt(ev.key === "-" ? 1 / 1.2 : 1.2, r.width / 2, r.height / 2); return true;
      }
      return false;
    },
    get state() { return st; },
    select(t) { select(t); centerOnPort(t.port, t.node); },
    linkParams,
  };
}
