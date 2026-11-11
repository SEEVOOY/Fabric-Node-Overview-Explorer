// The column lineage page's pure parts: layout and threads. Run: node --test tests/js/*.test.mjs
import {test} from "node:test";
import assert from "node:assert/strict";
import {breakCycles, layout, placeLayer, rank} from "../../fabric_explorer/web/cll/layout.mjs";
import {buildGraph, indexFlows, threadOf} from "../../fabric_explorer/web/cll/graph.mjs";

const measure = (t) => String(t).length * 7;

test("ranks follow the data, sources sit next to what reads them", () => {
  const r = rank(["a", "b", "c", "d"], [["a", "b"], ["b", "c"], ["d", "c"]]);
  assert.deepEqual([r.get("a"), r.get("b"), r.get("c"), r.get("d")], [0, 1, 2, 1]);
});

test("cycles are broken where they are found", () => {
  const succ = new Map([["a", new Set(["b"])], ["b", new Set(["a"])]]);
  assert.equal(breakCycles(["a", "b"], succ).size, 1);
});

test("a layer keeps its order and gaps, as close as it can to where it is pulled", () => {
  const ys = placeLayer([{h: 10, want: 50, wt: 1}, {h: 10, want: 50, wt: 1}], 4);
  assert.deepEqual(ys, [43, 57]);                         // pulled to the same place, pushed apart evenly
  assert.deepEqual(placeLayer([{h: 10, want: 0, wt: 1}, {h: 10, want: 100, wt: 1}], 4), [0, 100]);
});

const notebook = {
  mode: "notebook",
  datasets: [
    {id: "in", label: "raw", fullName: "bronze.raw", columns: [{id: "in|a", name: "a"}, {id: "in|b", name: "b"}]},
    {id: "out", label: "clean", fullName: "silver.clean", columns: [{id: "out|a", name: "a", status: "altered"}, {id: "out|b", name: "b"}]},
  ],
  ops: [
    {id: "f", scope: "row", label: "filter a > 0", letters: "F", category: "filter", cell: 1, line: 2, notebook: "nb",
     lanes: [{id: "f|0", label: "a"}, {id: "f|1", label: "b"}]},
    {id: "u", scope: "column", label: "upper(a)", letters: "S", category: "text", cell: 1, line: 3, notebook: "nb"},
  ],
  edges: [
    {i: 0, from: "in|a", to: "f|0", fromNode: "in", toNode: "f", kind: "data"},
    {i: 1, from: "f|0", to: "u", fromNode: "f", toNode: "u", kind: "data"},
    {i: 2, from: "u", to: "out|a", fromNode: "u", toNode: "out", kind: "data"},
    {i: 3, from: "in|b", to: "f|1", fromNode: "in", toNode: "f", kind: "data"},
    {i: 4, from: "f|1", to: "out|b", fromNode: "f", toNode: "out", kind: "data"},
  ],
  flows: [{edges: [0, 1, 2], from: "in|a", to: "out|a"}, {edges: [3, 4], from: "in|b", to: "out|b"}],
};

test("layout: left to right, ports lined up, long lines routed past the definitions", () => {
  const g = buildGraph(notebook, measure);
  const lay = layout(g);
  const x = id => lay.nodes.get(id).x;
  assert.ok(x("in") < x("f") && x("f") < x("u") && x("u") < x("out"));
  const route = lay.routes.get(g.edges.find(e => e.i === 4));   // b skips the upper(a) rank: one waypoint
  assert.equal(route.length, 3);
  for (const n of g.nodes) {                               // nothing overlaps within a rank
    for (const m of g.nodes) {
      if (n === m) continue;
      const a = lay.nodes.get(n.id), b = lay.nodes.get(m.id);
      if (a.rank !== b.rank) continue;
      assert.ok(a.y + a.h <= b.y || b.y + b.h <= a.y, `${n.id} overlaps ${m.id}`);
    }
  }
});

test("a column's thread follows its own path through a shared definition", () => {
  const g = buildGraph(notebook, measure), idx = indexFlows(g);
  const th = threadOf(g, idx, {port: "out|a"});
  assert.deepEqual([...th.edges].sort(), [0, 1, 2]);       // not b's lane through the same filter
  const all = threadOf(g, idx, {node: "f"});
  assert.equal(all.edges.size, 5);
});

test("Changed only keeps the paths into a changed column and counts what it hides", async () => {
  const {onlyChanged} = await import("../../fabric_explorer/web/cll/graph.mjs");
  const shown = onlyChanged(notebook);
  assert.deepEqual(shown.edges.map(e => e.i), [0, 1, 2]);              // a's path, not b's copy
  assert.deepEqual(shown.datasets.map(d => [d.id, d.columns.map(c => c.name), d.hidden]), [["in", ["a"], 1], ["out", ["a"], 1]]);
  assert.deepEqual(shown.ops.find(o => o.id === "f").lanes.map(l => l.id), ["f|0"]);
});

test("Focus keeps a thread and the relations along it", async () => {
  const {focusData} = await import("../../fabric_explorer/web/cll/graph.mjs");
  const data = {...notebook, edges: [...notebook.edges, {i: 9, from: "in|", to: "out|", fromNode: "in", toNode: "out", kind: "relation"}]};
  const g = buildGraph(data, measure);
  const shown = focusData(data, g, indexFlows(g), {port: "out|b", node: "out"});
  assert.deepEqual(shown.edges.map(e => e.i), [3, 4, 9]);
});

test("a shared definition's lanes take the order their lines come in (no crossing)", () => {
  const data = {mode: "notebook",
    datasets: [{id: "in", label: "in", fullName: "in", columns: [{id: "in|a", name: "a"}, {id: "in|b", name: "b"}]},
               {id: "out", label: "out", fullName: "out", columns: [{id: "out|a", name: "a"}, {id: "out|b", name: "b"}]}],
    ops: [{id: "m", scope: "row", label: "filter", letters: "F", category: "filter", cell: 1, line: 1, notebook: "nb",
           lanes: [{id: "m|0", label: "b"}, {id: "m|1", label: "a"}]}],           // made b first
    edges: [{i: 0, from: "in|b", to: "m|0", fromNode: "in", toNode: "m", kind: "data"},
            {i: 1, from: "in|a", to: "m|1", fromNode: "in", toNode: "m", kind: "data"},
            {i: 2, from: "m|0", to: "out|b", fromNode: "m", toNode: "out", kind: "data"},
            {i: 3, from: "m|1", to: "out|a", fromNode: "m", toNode: "out", kind: "data"}],
    flows: [{edges: [0, 2], from: "in|b", to: "out|b"}, {edges: [1, 3], from: "in|a", to: "out|a"}]};
  const g = buildGraph(data, measure);
  layout(g);
  const m = g.byId.get("m");
  assert.ok(m.ports["m|1"] < m.ports["m|0"], "a's lane sits above b's, like the rows they come from");
});

test("a long line goes straight when nothing stands in the way", () => {
  const rows = ["a", "x1", "x2", "x3", "x4", "x5", "b"];
  const data = {mode: "notebook",
    datasets: [{id: "in", label: "in", fullName: "in", columns: rows.map(r => ({id: `in|${r}`, name: r}))},
               {id: "out", label: "out", fullName: "out", columns: [{id: "out|a", name: "a"}, {id: "out|b", name: "b"}]}],
    ops: [{id: "u", scope: "column", label: "upper(a)", letters: "S", category: "text", cell: 1, line: 2, notebook: "nb", lanes: []}],
    edges: [{i: 0, from: "in|a", to: "u", fromNode: "in", toNode: "u", kind: "data"},
            {i: 1, from: "u", to: "out|a", fromNode: "u", toNode: "out", kind: "data"},
            {i: 2, from: "in|b", to: "out|b", fromNode: "in", toNode: "out", kind: "data"}],
    flows: [{edges: [0, 1], from: "in|a", to: "out|a"}, {edges: [2], from: "in|b", to: "out|b"}]};
  const g = buildGraph(data, measure);
  const lay = layout(g);
  assert.equal(lay.routes.get(g.edges.find(e => e.i === 2)).length, 2);   // b passes below upper(a): no waypoint
});

test("a definition's text fits its box: two lines broken where code breaks, then …", async () => {
  const {wrapLines, fitLine} = await import("../../fabric_explorer/web/cll/graph.mjs");
  const m = t => String(t).length * 7;
  const lines = wrapLines("left join ism.pro_nomenclature on d.Discipline = n.Code", 140, m, "", 2);
  assert.equal(lines.length, 2);
  for (const l of lines) assert.ok(m(l) <= 140, l);
  assert.ok(lines[1].endsWith("…"));
  assert.deepEqual(wrapLines("upper(trim(TagNo))", 200, m, "", 2), ["upper(trim(TagNo))"]);
  assert.equal(fitLine("abcdefghij", 50, m, ""), "abcdef…");
});

test("Focus on several selected keeps each of their threads", async () => {
  const {focusData} = await import("../../fabric_explorer/web/cll/graph.mjs");
  const data = {mode: "datasets",
    datasets: [{id: "a", label: "a", fullName: "a", columns: [{id: "a|x", name: "x", status: "unchanged"}]},
               {id: "b", label: "b", fullName: "b", columns: [{id: "b|x", name: "x", status: "new"}]},
               {id: "c", label: "c", fullName: "c", columns: [{id: "c|y", name: "y", status: "unchanged"}]},
               {id: "d", label: "d", fullName: "d", columns: [{id: "d|y", name: "y", status: "new"}]}],
    ops: [], cards: [],
    edges: [{i: 0, from: "a|x", to: "b|x", fromNode: "a", toNode: "b", kind: "change"},
            {i: 1, from: "c|y", to: "d|y", fromNode: "c", toNode: "d", kind: "change"}],
    flows: [{edges: [0], from: "a|x", to: "b|x", kind: "change"}, {edges: [1], from: "c|y", to: "d|y", kind: "change"}]};
  const g = buildGraph(data, measure), idx = indexFlows(g);
  const one = focusData(data, g, idx, {port: "b|x", node: "b"});
  assert.deepEqual(one.datasets.map(d => d.id).sort(), ["a", "b"]);
  const both = focusData(data, g, idx, [{port: "b|x", node: "b"}, {port: "d|y", node: "d"}]);
  assert.deepEqual(both.datasets.map(d => d.id).sort(), ["a", "b", "c", "d"]);
});
