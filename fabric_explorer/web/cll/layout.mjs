// Column lineage layout: a layered (left-to-right) drawing of nodes whose edges attach to ports.
//
// A node is {id, w, h, ports: {portId: y offset from the node's top}, group?}. An edge is
// {from, to, fromNode, toNode} between two ports. The steps are the classic ones for flow diagrams:
//   1. break cycles (a dataset read and written by the same chain) by turning back edges around
//   2. rank by the longest path from the sources, then move sources next to what reads them
//   3. route edges that skip ranks through waypoints (one chain per pair of nodes, shared as a bundle)
//   4. order each rank by the barycentre of the ports it connects to, keeping a group (a notebook) together
//   5. place each rank as close as possible to where its connections pull it, without overlap
//      (isotonic regression: the least-squares positions that keep the order and the gaps)
// No DOM here: the page measures the nodes, this works out where they go.

export const DEFAULTS = {rankGap: 120, nodeGap: 26, sweeps: 6, rounds: 8, laneSpread: 3};

const key = (a, b) => `${a}\u0000${b}`;

export function breakCycles(ids, succ) {
  const back = new Set(), color = new Map();
  for (const start of ids) {
    if (color.get(start)) continue;
    color.set(start, 1);
    const stack = [[start, [...succ.get(start)], 0]];
    while (stack.length) {
      const top = stack[stack.length - 1];
      if (top[2] < top[1].length) {
        const m = top[1][top[2]++];
        const c = color.get(m) || 0;
        if (c === 1) back.add(key(top[0], m));
        else if (c === 0) { color.set(m, 1); stack.push([m, [...succ.get(m)], 0]); }
      } else { color.set(top[0], 2); stack.pop(); }
    }
  }
  return back;
}

export function rank(ids, edges) {
  const succ = new Map(ids.map(id => [id, []])), pred = new Map(ids.map(id => [id, []]));
  for (const [a, b] of edges) { succ.get(a).push(b); pred.get(b).push(a); }
  const indeg = new Map(ids.map(id => [id, pred.get(id).length]));
  const queue = ids.filter(id => !indeg.get(id)), r = new Map(ids.map(id => [id, 0]));
  for (let i = 0; i < queue.length; i++) {
    const a = queue[i];
    for (const b of succ.get(a)) {
      r.set(b, Math.max(r.get(b), r.get(a) + 1));
      indeg.set(b, indeg.get(b) - 1);
      if (!indeg.get(b)) queue.push(b);
    }
  }
  for (const id of ids) {                         // sources sit right before the first thing that reads them
    if (!pred.get(id).length && succ.get(id).length) r.set(id, Math.max(0, Math.min(...succ.get(id).map(b => r.get(b))) - 1));
  }
  return r;
}

// least-squares y positions that keep the order and the gaps (pool adjacent violators)
export function placeLayer(items, gap) {
  const offs = [];
  let acc = 0;
  for (const it of items) { offs.push(acc); acc += it.h + gap; }
  const blocks = [];
  items.forEach((it, i) => {
    blocks.push({sum: (it.want - offs[i]) * it.wt, wt: it.wt, n: 1});
    while (blocks.length > 1) {
      const b = blocks[blocks.length - 1], a = blocks[blocks.length - 2];
      if (a.sum / a.wt <= b.sum / b.wt) break;
      blocks.splice(blocks.length - 2, 2, {sum: a.sum + b.sum, wt: a.wt + b.wt, n: a.n + b.n});
    }
  });
  const out = [];
  let i = 0;
  for (const b of blocks) for (let k = 0; k < b.n; k++, i++) out.push(b.sum / b.wt + offs[i]);
  return out;
}

export function layout(graph, opts = {}) {
  const o = {...DEFAULTS, ...opts};
  const nodes = new Map();
  graph.nodes.forEach((n, i) => nodes.set(n.id, {...n, ports: n.ports || {}, index: i, x: 0, y: 0, dummy: false}));
  const ids = [...nodes.keys()];
  const edges = graph.edges.filter(e => nodes.has(e.fromNode) && nodes.has(e.toNode) && e.fromNode !== e.toNode);
  const succ = new Map(ids.map(id => [id, new Set()]));
  for (const e of edges) succ.get(e.fromNode).add(e.toNode);
  const back = breakCycles(ids, succ);
  const pairs = new Map();                        // node pair -> its edges (a bundle)
  for (const e of edges) {
    const k = key(e.fromNode, e.toNode);
    if (!pairs.has(k)) pairs.set(k, {a: e.fromNode, b: e.toNode, edges: [], back: back.has(k)});
    pairs.get(k).edges.push(e);
  }
  const dag = [...pairs.values()].map(p => p.back ? [p.b, p.a] : [p.a, p.b]);
  const r = rank(ids, dag);
  for (const [id, n] of nodes) n.rank = r.get(id);

  // waypoints for bundles that skip ranks
  for (const p of pairs.values()) {
    const ra = nodes.get(p.a).rank, rb = nodes.get(p.b).rank;
    p.chain = [];
    if (p.back || rb - ra < 2) continue;
    const h = Math.max(6, p.edges.length * o.laneSpread);
    for (let k = ra + 1; k < rb; k++) {
      const id = `\u0001${p.a}>${p.b}#${k}`;
      nodes.set(id, {id, w: 8, h, ports: {}, rank: k, dummy: true, index: nodes.get(p.a).index + 0.5, group: "", x: 0, y: 0});
      p.chain.push(id);
    }
  }

  // ranks, in their first order: as given (datasets, then definitions in code order)
  const maxRank = Math.max(0, ...[...nodes.values()].map(n => n.rank));
  const layers = Array.from({length: maxRank + 1}, () => []);
  for (const n of [...nodes.values()].sort((a, b) => a.index - b.index)) layers[n.rank].push(n);

  // the links each node has to the ranks next to it: [other node, my port offset, its port offset, weight]
  const links = new Map([...nodes.keys()].map(id => [id, []]));
  const portY = (n, port) => n.dummy ? n.h / 2 : (n.ports[port] ?? n.h / 2);
  for (const p of pairs.values()) {
    const hops = [p.a, ...p.chain, p.b];
    for (const e of p.edges) {
      for (let i = 0; i < hops.length - 1; i++) {
        const u = nodes.get(hops[i]), v = nodes.get(hops[i + 1]);
        const pu = i === 0 ? () => portY(u, e.from) : () => u.h / 2;   // read when used: rows may be reordered
        const pv = i === hops.length - 2 ? () => portY(v, e.to) : () => v.h / 2;
        links.get(u.id).push([v, pu, pv, 1]);
        links.get(v.id).push([u, pv, pu, 1]);
      }
    }
  }
  const stack = layer => { let y = 0; for (const n of layer) { n.y = y; y += n.h + o.nodeGap; } };
  layers.forEach(stack);

  // order: barycentre sweeps, groups kept together
  const bary = (n, side) => {
    let s = 0, c = 0;
    for (const [m, mine, theirs] of links.get(n.id)) {
      if (side < 0 ? m.rank !== n.rank - 1 : m.rank !== n.rank + 1) continue;
      s += m.y + theirs() - mine(); c++;
    }
    return c ? s / c : n.y;
  };
  for (let sweep = 0; sweep < o.sweeps; sweep++) {
    const down = sweep % 2 === 0;
    const order = down ? layers.map((_, i) => i) : layers.map((_, i) => layers.length - 1 - i);
    for (const ri of order) {
      const layer = layers[ri];
      const b = new Map(layer.map(n => [n.id, bary(n, down ? -1 : 1)]));
      const gsum = new Map();
      for (const n of layer) { const g = n.group || n.id; const cur = gsum.get(g) || [0, 0]; gsum.set(g, [cur[0] + b.get(n.id), cur[1] + 1]); }
      const gkey = n => { const g = gsum.get(n.group || n.id); return g[0] / g[1]; };
      layer.sort((p, q) => gkey(p) - gkey(q) || (p.group || p.id).localeCompare(q.group || q.id) || b.get(p.id) - b.get(q.id));
      stack(layer);
    }
  }

  // x: each rank as wide as its widest node, with room for the lines in between
  const xs = [];
  let x = 0;
  for (let ri = 0; ri < layers.length; ri++) {
    xs.push(x);
    const w = Math.max(8, ...layers[ri].map(n => n.w));
    const crossing = edges.filter(e => nodes.get(e.fromNode).rank <= ri && nodes.get(e.toNode).rank > ri).length;
    x += w + o.rankGap + Math.min(140, Math.sqrt(crossing) * 6);
  }
  for (const layer of layers) for (const n of layer) {
    const lw = Math.max(8, ...layer.map(m => m.w));
    n.x = xs[n.rank] + (n.dummy ? lw / 2 - n.w / 2 : 0);
  }

  // y: pull each rank towards its neighbours, a few rounds each way
  const place = rounds => {
    for (let round = 0; round < rounds; round++) {
      const fwd = round % 2 === 0;
      const order = fwd ? layers.map((_, i) => i) : layers.map((_, i) => layers.length - 1 - i);
      for (const ri of order) {
        const layer = layers[ri];
        const items = layer.map(n => {
          const vals = [];
          for (const [m, mine, theirs] of links.get(n.id)) {
            if (Math.abs(m.rank - n.rank) !== 1) continue;
            vals.push(m.y + theirs() - mine());
          }
          vals.sort((a, b) => a - b);
          const med = vals.length ? (vals.length % 2 ? vals[(vals.length - 1) / 2] : (vals[vals.length / 2 - 1] + vals[vals.length / 2]) / 2) : n.y;
          return {h: n.h, want: med, wt: n.dummy ? 0.5 : 1 + Math.min(vals.length, 20) * 0.1};
        });
        placeLayer(items, o.nodeGap).forEach((y, i) => { layer[i].y = y; });
      }
    }
  };
  place(o.rounds);

  // rows that may change order (the lanes of a shared definition, a card's rows) take the order their lines
  // come in, rank by rank from the left, so lines run straight in and on without crossing
  const avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  let reordered = false;
  for (const layer of layers) {
    for (const n of layer) {
      if (!n.reorder || !n.reorder.ids.length) continue;
      const into = new Map(), outof = new Map();
      for (const e of edges) {
        if (e.toNode === n.id) {
          const a = nodes.get(e.fromNode);
          if (!into.has(e.to)) into.set(e.to, []);
          into.get(e.to).push(a.y + portY(a, e.from));
        } else if (e.fromNode === n.id) {
          const b = nodes.get(e.toNode);
          if (!outof.has(e.from)) outof.set(e.from, []);
          outof.get(e.from).push(b.y + portY(b, e.to));
        }
      }
      const {ids, top, step} = n.reorder;
      const score = id => avg(into.get(id) || []) ?? avg(outof.get(id) || []) ?? Infinity;
      const order = ids.map((id, i) => [id, score(id), i]).sort((x, y) => (x[1] - y[1]) || (x[2] - y[2]));
      order.forEach(([id], i) => {
        const at = top + i * step + step / 2;
        if (n.ports[id] !== at) { n.ports[id] = at; reordered = true; }
      });
    }
  }
  if (reordered) place(4);

  // long lines: straight when nothing stands in the way; otherwise their waypoints move onto the straight
  // line wherever there is room, and the line is drawn smoothly through what is left
  const layerX = layers.map((layer, ri) => [xs[ri], xs[ri] + Math.max(8, ...layer.map(m => m.w))]);
  for (const p of pairs.values()) {
    if (!p.chain.length) continue;
    const a = nodes.get(p.a), b = nodes.get(p.b);
    const ya = avg(p.edges.map(e => a.y + portY(a, e.from))), yb = avg(p.edges.map(e => b.y + portY(b, e.to)));
    const start = {x: a.x + a.w, y: ya}, end = {x: b.x, y: yb};
    const band = Math.max(4, (p.edges.length - 1) * o.laneSpread / 2) + 6;
    let clear = true;
    for (let k = a.rank + 1; k < b.rank && clear; k++) {
      for (const x of layerX[k]) {
        const y = cubicYAtX(start, end, x);
        if (layers[k].some(m => !m.dummy && y + band > m.y && y - band < m.y + m.h)) { clear = false; break; }
      }
    }
    if (clear) { p.chain = []; continue; }
    for (const id of p.chain) {
      const d = nodes.get(id);
      const want = ya + (yb - ya) * (d.rank - a.rank) / (b.rank - a.rank) - d.h / 2;
      if (!layers[d.rank].some(m => !m.dummy && want + d.h + 4 > m.y && want - 4 < m.y + m.h)) d.y = want;
    }
  }

  let minY = Infinity, maxY = -Infinity, maxX = 0;
  for (const n of nodes.values()) { minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y + n.h); maxX = Math.max(maxX, n.x + n.w); }
  if (!Number.isFinite(minY)) minY = maxY = 0;
  for (const n of nodes.values()) n.y -= minY;

  // routes: from the right side of a port to the left side of the next, through the bundle's waypoints
  const routes = new Map();
  for (const p of pairs.values()) {
    const sorted = [...p.edges].sort((e1, e2) => portY(nodes.get(p.a), e1.from) - portY(nodes.get(p.a), e2.from));
    sorted.forEach((e, idx) => {
      const a = nodes.get(p.a), b = nodes.get(p.b);
      const pts = [{x: a.x + a.w, y: a.y + portY(a, e.from)}];
      const spread = (idx - (sorted.length - 1) / 2) * o.laneSpread;
      for (const id of p.chain) { const d = nodes.get(id); pts.push({x: d.x + d.w / 2, y: d.y + d.h / 2 + spread}); }
      pts.push({x: b.x, y: b.y + portY(b, e.to)});
      routes.set(e, pts);
    });
  }
  const out = new Map();
  for (const [id, n] of nodes) if (!n.dummy) out.set(id, {x: n.x, y: n.y, w: n.w, h: n.h, rank: n.rank});
  return {nodes: out, routes, width: maxX, height: maxY - minY};
}

// The y of the cubic from p to q (level at both ends) where it passes x
export function cubicYAtX(p, q, x) {
  const dx = Math.max(12, (q.x - p.x) * 0.5);
  const c1 = p.x + dx, c2 = q.x - dx;
  let lo = 0, hi = 1;
  for (let i = 0; i < 30; i++) {
    const t = (lo + hi) / 2, u = 1 - t;
    const xt = u * u * u * p.x + 3 * u * u * t * c1 + 3 * u * t * t * c2 + t * t * t * q.x;
    if (xt < x) lo = t; else hi = t;
  }
  const t = (lo + hi) / 2, u = 1 - t;
  return u * u * u * p.y + 3 * u * u * t * p.y + 3 * u * t * t * q.y + t * t * t * q.y;
}

// A smooth path through route points, leaving and arriving level; through the points in between it keeps
// its direction, so there is no flat step at every waypoint
export function pathOf(pts) {
  if (!pts || pts.length < 2) return "";
  const f = v => v.toFixed(1);
  let d = `M${f(pts[0].x)},${f(pts[0].y)}`;
  if (pts.length === 2) {
    const [p, q] = pts, dx = Math.max(12, (q.x - p.x) * 0.5);
    return d + ` C${f(p.x + dx)},${f(p.y)} ${f(q.x - dx)},${f(q.y)} ${f(q.x)},${f(q.y)}`;
  }
  const n = pts.length - 1;
  const slope = i => (i === 0 || i === n) ? 0 : (pts[i + 1].y - pts[i - 1].y) / Math.max(1, pts[i + 1].x - pts[i - 1].x);
  for (let i = 1; i <= n; i++) {
    const p = pts[i - 1], q = pts[i], h = Math.max(6, (q.x - p.x) / 3);
    d += ` C${f(p.x + h)},${f(p.y + slope(i - 1) * h)} ${f(q.x - h)},${f(q.y - slope(i) * h)} ${f(q.x)},${f(q.y)}`;
  }
  return d;
}
