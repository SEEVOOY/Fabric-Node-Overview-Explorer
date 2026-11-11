// From what the server sends to what the layout needs: nodes with their size and ports, and the
// edges between ports. Also the thread of one column: the paths it takes, followed through the
// flows (a flow is one column's path), so a definition shared by many columns doesn't light up all
// of them. No DOM here; text widths come from a measure(text, font) function.

export const SIZE = {
  rowH: 18, dsHead: 44, dsFoot: 8, dsMinW: 190, dsMaxW: 310,
  balloon: 30, opH: 38, labelMax: 250, lineH: 14,
  laneH: 18, mfTop: 46,
  nbHead: 50, nbRowH: 24, nbMinW: 200, nbMaxW: 340,
};
// The fonts text is measured in; the page sets them from its stylesheet (useFonts), so what is measured
// is what is drawn
export const FONT = {
  name: '600 13px "Bahnschrift", "DIN Alternate", "Barlow", "Segoe UI", system-ui, sans-serif',
  sub: '400 11px "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif',
  row: '400 12px "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif',
  code: '400 11.5px "Cascadia Mono", Consolas, "SFMono-Regular", Menlo, monospace',
  small: '400 11px "Cascadia Mono", Consolas, "SFMono-Regular", Menlo, monospace',
  frame: '600 12.5px "Bahnschrift", "DIN Alternate", "Barlow", "Segoe UI", system-ui, sans-serif',
};
export function useFonts({ui, text, code}) {
  if (ui) { FONT.name = `600 13px ${ui}`; FONT.frame = `600 12.5px ${ui}`; }
  if (text) { FONT.sub = `400 11px ${text}`; FONT.row = `400 12px ${text}`; }
  if (code) { FONT.code = `400 11.5px ${code}`; FONT.small = `400 11px ${code}`; }
}

// One line that fits a width, cut with "…" if it has to be
export function fitLine(text, width, measure, font) {
  const t = String(text ?? "");
  if (measure(t, font) <= width) return t;
  let lo = 0, hi = t.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (measure(t.slice(0, mid).trimEnd() + "…", font) <= width) lo = mid; else hi = mid - 1;
  }
  return t.slice(0, lo).trimEnd() + "…";
}

// Code on up to maxLines lines of a width, broken at spaces; a word wider than a line breaks after a
// comma, an opening bracket or a dot. What still doesn't fit ends in "…"
export function wrapLines(text, width, measure, font, maxLines = 2) {
  const t = String(text ?? "").replace(/\s+/g, " ").trim();
  if (!t || measure(t, font) <= width) return [t];
  const parts = [];
  for (const word of t.match(/[^ ]+ ?/g) || [t]) {
    if (measure(word.trimEnd(), font) <= width) parts.push(word);
    else parts.push(...(word.match(/[^,(.]+[,(.]?|[,(.]/g) || [word]));
  }
  const lines = [];
  let cur = "", i = 0;
  for (; i < parts.length; i++) {
    if (!cur || measure((cur + parts[i]).trimEnd(), font) <= width) { cur += parts[i]; continue; }
    lines.push(cur.trimEnd());
    cur = parts[i].trimStart();
    if (lines.length === maxLines - 1) { i++; break; }
  }
  lines.push((cur + parts.slice(i).join("")).trim());
  return lines.map(l => fitLine(l, width, measure, font));
}

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const short = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };

// opts.lanes: a column definition used by several columns is a block with a lane each (they pass
// straight through, as in a manifold); false: the columns gather into one point and spread out again.
export function buildGraph(data, measure, opts = {}) {
  const lanesMode = opts.lanes !== false;
  const nodes = [], byId = new Map();
  const add = n => { nodes.push(n); byId.set(n.id, n); return n; };
  for (const d of data.datasets || []) {
    const rows = d.columns || [];
    const head = Math.max(measure(d.label, FONT.name), measure(d.lakehouseName || "", FONT.row)) + 40;
    const body = Math.max(0, ...rows.map(c => measure(c.name, FONT.row) + measure(c.type || "", FONT.code) + 78));
    const foot = d.hidden ? SIZE.rowH : 0;
    const ports = {[`${d.id}|`]: SIZE.dsHead / 2};
    rows.forEach((c, i) => { ports[c.id] = SIZE.dsHead + i * SIZE.rowH + SIZE.rowH / 2; });
    add({id: d.id, kind: "dataset", data: d, rows, group: "", ports,
      w: clamp(Math.max(head, body), SIZE.dsMinW, SIZE.dsMaxW),
      h: SIZE.dsHead + rows.length * SIZE.rowH + foot + SIZE.dsFoot});
  }
  for (const op of data.ops || []) {
    // the code's box: up to two lines, as wide as the text (it never runs out of the box)
    const lines = wrapLines(op.label, SIZE.labelMax, measure, FONT.code, 2);
    const box = {lines, w: Math.ceil(Math.max(...lines.map(l => measure(l, FONT.code)))) + 20,
      h: lines.length > 1 ? 8 + lines.length * SIZE.lineH : 22};
    const label = lines.join(" ");
    const lw = box.w;
    if (op.scope === "row") {
      const lanes = op.lanes || [];
      const ports = {[`${op.id}|cond`]: SIZE.balloon / 2 + 4};
      lanes.forEach((l, i) => { ports[l.id] = SIZE.mfTop + i * SIZE.laneH + SIZE.laneH / 2; });
      add({id: op.id, kind: "manifold", data: op, label, box, group: op.notebook, ports,
        reorder: {ids: lanes.map(l => l.id), top: SIZE.mfTop, step: SIZE.laneH},
        w: SIZE.balloon + 8 + lw, h: SIZE.mfTop + Math.max(1, lanes.length) * SIZE.laneH + 6});
    } else if (lanesMode && (op.lanes || []).length > 1) {
      const ports = {[`${op.id}|head`]: SIZE.balloon / 2 + 4};
      op.lanes.forEach((l, i) => { ports[l.id] = SIZE.mfTop + i * SIZE.laneH + SIZE.laneH / 2; });
      add({id: op.id, kind: "block", data: op, label, box, group: op.notebook, ports,
        reorder: {ids: op.lanes.map(l => l.id), top: SIZE.mfTop, step: SIZE.laneH},
        w: SIZE.balloon + 8 + lw, h: SIZE.mfTop + op.lanes.length * SIZE.laneH + 6});
    } else {                                           // one point: its lanes all meet in the middle
      const h = Math.max(SIZE.opH, box.h + 8);
      add({id: op.id, kind: "op", data: op, label, box, group: op.notebook, ports: {[op.id]: h / 2},
        w: SIZE.balloon + 8 + lw, h});
    }
  }
  for (const nb of data.cards || []) {             // notebooks, pipelines, semantic models, reports
    const rows = nb.rows || [];
    const head = measure(nb.name, FONT.name) + 64 + (nb.rowOps || []).length * 20;
    const body = Math.max(0, ...rows.map(r => measure(r.label, FONT.small) + 44));
    const ports = {[`${nb.id}|`]: SIZE.nbHead / 2};
    rows.forEach((r, i) => { ports[r.id] = SIZE.nbHead + i * SIZE.nbRowH + SIZE.nbRowH / 2; });
    add({id: nb.id, kind: "card", card: nb.kind || "notebook", data: nb, rows, group: "", ports,
      reorder: {ids: rows.map(r => r.id), top: SIZE.nbHead, step: SIZE.nbRowH},
      w: clamp(Math.max(head, body), SIZE.nbMinW, SIZE.nbMaxW),
      h: SIZE.nbHead + rows.length * SIZE.nbRowH + (rows.length ? 8 : 4)});
  }
  const edges = (data.edges || []).filter(e => byId.has(e.fromNode) && byId.has(e.toNode));
  return {nodes, byId, edges, flows: data.flows || []};
}

// ------------------------------------------------------------------ threads
export function indexFlows(graph) {
  const edgeByIndex = new Map(graph.edges.map(e => [e.i, e]));
  const byPort = new Map(), endsAt = new Map(), startsAt = new Map(), byNode = new Map();
  const push = (m, k, v) => { if (!m.has(k)) m.set(k, []); m.get(k).push(v); };
  graph.flows.forEach((f, i) => {
    push(endsAt, f.to, i); push(startsAt, f.from, i);
    for (const ei of f.edges) {
      const e = edgeByIndex.get(ei);
      if (!e) continue;
      push(byPort, e.from, i); push(byPort, e.to, i); push(byNode, e.fromNode, i); push(byNode, e.toNode, i);
    }
  });
  return {edgeByIndex, byPort, endsAt, startsAt, byNode};
}

// What to light up for a port or a node: the flows through it, followed upstream from where they
// start and downstream from where they end (across datasets, so a chain of notebooks is followed).
export function threadOf(graph, idx, {port = "", node = ""} = {}) {
  const flows = new Set();
  const seeds = port ? idx.byPort.get(port) || [] : idx.byNode.get(node) || [];
  const up = [], down = [];
  for (const f of seeds) { flows.add(f); up.push(graph.flows[f].from); down.push(graph.flows[f].to); }
  const seen = new Set();
  while (up.length) {
    const p = up.pop();
    if (seen.has("u" + p)) continue;
    seen.add("u" + p);
    for (const f of idx.endsAt.get(p) || []) if (!flows.has(f)) { flows.add(f); up.push(graph.flows[f].from); }
  }
  while (down.length) {
    const p = down.pop();
    if (seen.has("d" + p)) continue;
    seen.add("d" + p);
    for (const f of idx.startsAt.get(p) || []) if (!flows.has(f)) { flows.add(f); down.push(graph.flows[f].to); }
  }
  const edges = new Set(), ports = new Set(), nodes = new Set();
  for (const f of flows) {
    for (const ei of graph.flows[f].edges) {
      const e = idx.edgeByIndex.get(ei);
      if (!e) continue;
      edges.add(ei); ports.add(e.from); ports.add(e.to); nodes.add(e.fromNode); nodes.add(e.toNode);
    }
  }
  if (node) {                                      // a node: where it goes and where it comes from, all the way,
    const down = new Map(), up = new Map();         // along the lines between datasets and notebooks too
    for (const e of graph.edges) if (e.kind === "relation") {
      if (!down.has(e.fromNode)) down.set(e.fromNode, []);
      if (!up.has(e.toNode)) up.set(e.toNode, []);
      down.get(e.fromNode).push(e); up.get(e.toNode).push(e);
    }
    for (const [map, next] of [[down, e => e.toNode], [up, e => e.fromNode]]) {
      const queue = [node], seen = new Set([node]);
      while (queue.length) {
        for (const e of map.get(queue.pop()) || []) {
          edges.add(e.i); nodes.add(e.fromNode); nodes.add(e.toNode); ports.add(e.from); ports.add(e.to);
          if (!seen.has(next(e))) { seen.add(next(e)); queue.push(next(e)); }
        }
      }
    }
    nodes.add(node);
  }
  if (port) ports.add(port);
  return {flows, edges, ports, nodes};
}

// The steps a column goes through to get where it is: [{op, from}] per path, for the details panel
export function stepsInto(graph, idx, port, ops) {
  const out = [];
  for (const f of idx.endsAt.get(port) || []) {
    const fl = graph.flows[f], seq = [];
    for (const ei of fl.edges) {
      const e = idx.edgeByIndex.get(ei);
      if (e && ops.has(e.toNode) && !seq.includes(e.toNode)) seq.push(e.toNode);
    }
    out.push({from: fl.from, ops: seq});
  }
  return out;
}

// ------------------------------------------------------------------ filters (a view of the same data)
export const CHANGED = ["new", "altered", "renamed"];

// What is drawn when only some flows are kept: their lines, the rows they touch, and what holds them.
// Row operations keep the lanes in use; datasets keep their changed columns too and count the rest.
export function keepFlows(data, keep, {relations = false, keepChanged = false, keepAll = false, extraEdges = null} = {}) {
  const flows = (data.flows || []).filter((_, i) => keep.has(i));
  const edgeIds = new Set(flows.flatMap(f => f.edges));
  for (const i of extraEdges || []) edgeIds.add(i);
  const edges = (data.edges || []).filter(e => edgeIds.has(e.i));
  const ports = new Set(edges.flatMap(e => [e.from, e.to])), nodes = new Set(edges.flatMap(e => [e.fromNode, e.toNode]));
  if (relations) {
    for (const e of data.edges || []) if (e.kind === "relation" && nodes.has(e.fromNode) && nodes.has(e.toNode)) {
      edges.push(e); ports.add(e.from); ports.add(e.to);
    }
  }
  if (keepAll) {                                     // every dataset and card the view has, and the lines between them
    for (const d of data.datasets || []) nodes.add(d.id);
    for (const c of data.cards || []) if (c.hub) nodes.add(c.id);
    for (const e of data.edges || []) if (e.kind === "relation" && nodes.has(e.fromNode) && nodes.has(e.toNode) && !edges.includes(e)) {
      edges.push(e); ports.add(e.from); ports.add(e.to);
    }
  }
  const datasets = (data.datasets || []).filter(d => nodes.has(d.id) || (keepChanged && d.columns.some(c => CHANGED.includes(c.status))))
    .map(d => {
      const columns = d.columns.filter(c => ports.has(c.id) || (keepChanged && CHANGED.includes(c.status)));
      return {...d, columns, hidden: (d.hidden || 0) + d.columns.length - columns.length};
    });
  const ops = (data.ops || []).filter(o => nodes.has(o.id))
    .map(o => ({...o, lanes: (o.lanes || []).filter(l => ports.has(l.id))}));
  const cards = (data.cards || []).filter(c => nodes.has(c.id)).map(c => ({...c, rows: c.hub ? c.rows : (c.rows || []).filter(r => ports.has(r.id))}));
  return {...data, datasets, ops, cards, edges: edges.sort((a, b) => a.i - b.i), flows};
}

// Notebook view, Changed only: the paths into a changed column, and on from there (into Power BI)
export function onlyChanged(data) {
  const changed = new Set((data.datasets || []).flatMap(d => d.columns.filter(c => CHANGED.includes(c.status)).map(c => c.id)));
  const keep = new Set();
  const starts = new Map();
  (data.flows || []).forEach((f, i) => { if (!starts.has(f.from)) starts.set(f.from, []); starts.get(f.from).push(i); });
  const todo = [];
  (data.flows || []).forEach((f, i) => { if (changed.has(f.to)) { keep.add(i); todo.push(f.to); } });
  while (todo.length) {
    for (const i of starts.get(todo.pop()) || []) if (!keep.has(i)) { keep.add(i); todo.push(data.flows[i].to); }
  }
  const ops = new Set();
  for (const i of keep) for (const ei of data.flows[i].edges) { const e = data.edges.find(x => x.i === ei); if (e) ops.add(e.toNode); }
  (data.flows || []).forEach((f, i) => {                  // what the kept definitions look at
    if (f.kind !== "condition") return;
    const last = data.edges.find(x => x.i === f.edges[f.edges.length - 1]);
    if (last && ops.has(last.toNode)) keep.add(i);
  });
  return keepFlows(data, keep, {keepChanged: true, keepAll: true});
}

// Datasets view, Focus: only the threads of what is selected (one or several), with the relations along them
export function focusData(data, graph, idx, target) {
  const flows = new Set(), rel = new Set();
  for (const t of Array.isArray(target) ? target : [target]) {
    const th = threadOf(graph, idx, t.port ? {port: t.port} : {node: t.node});
    for (const f of th.flows) flows.add(f);
    for (const i of th.edges) { const e = idx.edgeByIndex.get(i); if (e && e.kind === "relation") rel.add(i); }
  }
  return keepFlows(data, flows, {relations: true, extraEdges: rel});
}

// Datasets folded to their heading: their columns are hidden and their lines meet at the heading
export function collapseData(data, collapsed) {
  if (!collapsed || !collapsed.size) return data;
  const remap = new Map();
  const datasets = (data.datasets || []).map(d => {
    if (!collapsed.has(d.id) || !d.columns.length) return collapsed.has(d.id) ? {...d, collapsed: true} : d;
    for (const c of d.columns) remap.set(c.id, `${d.id}|`);
    return {...d, columns: [], hidden: (d.hidden || 0) + d.columns.length, collapsed: true};
  });
  if (!remap.size) return {...data, datasets};
  const to = p => remap.get(p) || p;
  const edges = (data.edges || []).map(e => remap.has(e.from) || remap.has(e.to) ? {...e, from: to(e.from), to: to(e.to)} : e);
  const flows = (data.flows || []).map(f => remap.has(f.from) || remap.has(f.to) ? {...f, from: to(f.from), to: to(f.to)} : f);
  return {...data, datasets, edges, flows};
}
