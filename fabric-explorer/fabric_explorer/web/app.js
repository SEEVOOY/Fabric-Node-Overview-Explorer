/* Fabric Explorer page.
   Open a workspace -> the map shows its structure (groups and items, nothing read yet).
   Select an item -> its content is read once (cached by the app) -> the map grows with what it
   reads, writes and runs. Refresh reads it again. */
"use strict";
(() => {
  // ============================================================ basics
  const $ = (s, r = document) => r.querySelector(s);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const KEY = new URLSearchParams(location.hash.slice(1)).get("k") || "";
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const EXT_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M9 2.5h4.5V7M13.5 2.5 7 9M11 9.5v3a1 1 0 0 1-1 1H3.5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h3"/></svg>';
  const PIN_SVG = '<svg class="pin" viewBox="0 0 16 16" aria-label="pinned highlight"><circle cx="9.5" cy="5.5" r="3.5" fill="currentColor"/><path d="M8 8 3.5 13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
  const CHEV = '<svg class="chev" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M3.5 2 7 5 3.5 8"/></svg>';
  const store = {
    get(k, d) { try { const v = localStorage.getItem("fx." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("fx." + k, JSON.stringify(v)); } catch { /* private mode */ } },
  };

  async function api(path, body) {
    let res;
    try {
      res = await fetch(path, body === undefined ? {headers: {"X-Session": KEY}} : {
        method: "POST", headers: {"X-Session": KEY, "Content-Type": "application/json"}, body: JSON.stringify(body)});
    } catch {
      throw Object.assign(new Error("Fabric Explorer isn't running any more."), {hint: "Start it again with python run.py, then open the address it prints."});
    }
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    if (!res.ok) throw Object.assign(new Error((data && data.error) || `Request failed (${res.status})`), {hint: (data && data.hint) || "", status: res.status});
    return data;
  }

  // ============================================================ vocabulary
  const TYPES = {
    Lakehouse: ["Lakehouse", "Lakehouses", 1], Notebook: ["Notebook", "Notebooks", 2],
    DataPipeline: ["Data pipeline", "Data pipelines", 3], SemanticModel: ["Semantic model", "Semantic models", 4],
    Report: ["Report", "Reports", 5], SparkJobDefinition: ["Spark job definition", "Spark job definitions", 6],
    Dataflow: ["Dataflow", "Dataflows", 7], Environment: ["Environment", "Environments", 8],
    Warehouse: ["Warehouse", "Warehouses", 9], SQLEndpoint: ["SQL analytics endpoint", "SQL analytics endpoints", 30],
    Table: ["Table", "Tables", 40], Folder: ["Folder", "Folders", 41], External: ["Outside Fabric", "Outside Fabric", 42],
  };
  const typeName = (t, many) => TYPES[t] ? TYPES[t][many ? 1 : 0] : (s => many ? s + "s" : s)(String(t || "Item").replace(/([a-z])([A-Z])/g, "$1 $2"));
  const typeOrder = t => TYPES[t] ? TYPES[t][2] : 20;
  // in / out: how the link reads from the item at its target / source end
  const EDGE = {
    read:              {color: "read", w: 1.7, dash: [], arrow: true, flow: true, verb: "is read by", in: "Reads", out: "Read by"},
    write:             {color: "write", w: 1.7, dash: [], arrow: true, flow: true, verb: "writes", in: "Written by", out: "Writes"},
    maintain:          {color: "write", w: 1.2, dash: [2, 4], arrow: true, flow: false, verb: "maintains", in: "Maintained by", out: "Maintains"},
    run:               {color: "run", w: 1.4, dash: [8, 4], arrow: true, flow: true, verb: "runs", in: "Run by", out: "Runs"},
    refresh:           {color: "run", w: 1.3, dash: [8, 4], arrow: true, flow: true, verb: "refreshes", in: "Refreshed by", out: "Refreshes"},
    model_source:      {color: "serve", w: 1.6, dash: [], arrow: true, flow: true, verb: "feeds", in: "Tables it uses", out: "Used by semantic models"},
    report_model:      {color: "serve", w: 1.4, dash: [], arrow: true, flow: true, verb: "feeds", in: "Built on", out: "Reports built on it"},
    shortcut:          {color: "shortcut", w: 1.5, dash: [10, 3, 2, 3], arrow: true, flow: true, verb: "is the source of shortcut", in: "Shortcut to", out: "Shortcuts pointing here"},
    default_lakehouse: {color: "muted", w: 1.1, dash: [3, 3], arrow: false, flow: false, settings: true, verb: "uses as default lakehouse", in: "Default lakehouse of", out: "Default lakehouse"},
    attached:          {color: "muted", w: 1, dash: [1, 4], arrow: false, flow: false, settings: true, verb: "has attached", in: "Attached to", out: "Also attached"},
    environment:       {color: "muted", w: 1, dash: [1, 4], arrow: false, flow: false, settings: true, verb: "runs in", in: "Used by", out: "Environment"},
    references:        {color: "muted", w: 1, dash: [], arrow: true, flow: false, verb: "mentions", in: "Mentioned by", out: "Mentions"},
    structure:         {color: "gridStrong", w: 1, dash: [], arrow: false, flow: false},
  };
  const LINEAGE = new Set(["read", "write", "run", "refresh", "model_source", "report_model", "shortcut"]);
  const DATA_KINDS = new Set(["read", "write", "maintain", "model_source", "shortcut", "report_model", "run", "refresh", "references"]);

  // ============================================================ state
  const S = {
    hello: {}, ws: null, wsData: null, folders: new Map(), lakeInfo: {}, wsList: null,
    groupMode: store.get("groupMode", "type"),
    opts: Object.assign({tables: true, areas: true, structure: true, settings: true, flow: true}, store.get("opts", {})),
    hl: store.get("hl", "full"),
    nodes: new Map(), edges: new Map(), scans: new Map(), busy: new Map(),
    queue: [], running: 0, batch: {total: 0, done: 0}, concurrency: 4,
    shown: new Set(), hidden: new Set(), expanded: new Set(), contents: new Set(), treeOpen: new Set(), subState: new Map(),
    leftTab: "structure", colCache: new Map(), colLoading: new Set(), colTarget: null,
    layout: store.get("layout", "free"), tween: null, multi: new Set(), offsets: new Map(), space: false, focusOpen: true, ddOpen: new Set(), ddSec: new Map(), ddSecIds: new Map(), treeDD: new Set(), quietReads: new Set(), folderMode: new Set(), folderVis: new Map(), focusIds: null, focusSig: "", bigCascade: 300, threadDir: store.get("threadDir", "both"), focusMode: null, addedBy: new Map(), pendingExpand: new Map(), pinned: null, colsOnMap: new Map(), modelColsOpen: new Map(),
    modelColsVisibleOnly: store.get("modelColsVisibleOnly", false), colStale: new Set(), dsScope: store.get("dsScope", "map"), dsFilter: "all",
    sel: null, selEdge: null, active: null, hover: null, hoverEdge: null, press: null,
    view: {x: 0, y: 0, k: 1}, alpha: 0, logLast: 0, logRows: [], treeFilter: "", loadedAt: "",
  };
  let V = {nodes: [], edges: [], vis: new Set(), out: new Map(), inn: new Map(), adj: new Map()};

  // ============================================================ theme and colour
  let C = {}, FONT = "sans-serif";
  const MEDALLION = [["bronze", "#A96B33"], ["silver", "#7D8B98"], ["gold", "#B8901A"], ["platinum", "#5E8C8A"]];
  const PALETTE = ["#1E7F86", "#6A4C93", "#3F7F3F", "#2F5D8A", "#A23B72", "#8A5A00", "#4F6D7A", "#9C4F2E"];
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    for (const k of ["sheet", "grid", "gridStrong", "ink", "ink2", "muted", "card", "line", "read", "write", "run", "serve", "shortcut", "error", "halo"])
      C[k] = cs.getPropertyValue("--c-" + k).trim();
    C.areaFill = parseFloat(cs.getPropertyValue("--area-fill")) || 0.1;
    FONT = cs.getPropertyValue("--font-ui").trim() || "sans-serif";
  }
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    $("#theme").textContent = t === "blueprint" ? "Sheet" : "Blueprint";
    store.set("theme", t);
    readColors();
    measureCache.clear();
    for (const n of S.nodes.values()) n.w = 0;
    if (S.ws) rebuildView(false);
    requestDraw();
  }
  function rgba(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return hex;
    const v = parseInt(m[1], 16);
    return `rgba(${v >> 16 & 255},${v >> 8 & 255},${v & 255},${a})`;
  }
  function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function lakeColor(n) {
    if (!n || n.kind === "missing" || n.kind === "external") return C.muted;
    if (n.color) return n.color;
    const name = String(n.label || "").toLowerCase();
    for (const [k, c] of MEDALLION) if (name.includes(k)) return (n.color = c);
    return (n.color = PALETTE[hash(n.id) % PALETTE.length]);
  }
  const colorOf = n => (n.kind === "table" || n.kind === "file") ? lakeColor(S.nodes.get(n.parent)) :
    (n.type === "Lakehouse" ? lakeColor(n) : ({DataPipeline: C.run, SemanticModel: C.serve, Report: C.serve}[n.type] || C.ink2));

  // ============================================================ workspace and links
  async function openWorkspace(id, itemId = "", refresh = false) {
    closePops();
    const same = S.ws && S.ws.id === id;
    const keep = same ? new Map([...S.nodes.values()].filter(n => Number.isFinite(n.x)).map(n => [n.id, {x: n.x, y: n.y, fixed: n.fixed}])) : null;
    const keepView = same ? {...S.view} : null;
    const keepSel = same ? S.sel : null;
    if (!same) showEmpty("loading");
    setActivity(refresh ? "Reading the workspace again" : "Reading the workspace", true);
    try {
      const data = await api(`/api/workspace?id=${encodeURIComponent(id)}${refresh ? "&refresh=1" : ""}`);
      if (!same) { S.queue = []; S.batch = {total: 0, done: 0}; }
      resetModel(same);
      S.ws = data.workspace; S.wsData = data; S.loadedAt = data.loadedAt;
      S.folders = new Map(data.folders.map(f => [f.id, f]));
      S.lakeInfo = data.lakehouses || {};
      for (const it of data.items) S.nodes.set("item:" + it.id, {
        id: "item:" + it.id, kind: "item", type: it.type, label: it.name, itemId: it.id, url: it.url,
        scannable: it.scannable, folderId: it.folderId, workspaceId: data.workspace.id, x: NaN, y: NaN, vx: 0, vy: 0, w: 0, h: 0});
      rebuildGroups();
      if (!same) {
        S.expanded.clear(); S.treeOpen.clear(); S.shown.clear(); S.hidden.clear(); S.contents.clear(); S.colTarget = null;
        for (const n of S.nodes.values()) if (n.kind === "group" && defaultOpen(n)) { S.expanded.add(n.id); S.treeOpen.add(n.id); }
      }
      for (const sc of data.scans || []) applyScan(sc, true);
      if (keep) for (const [nid, p] of keep) { const n = S.nodes.get(nid); if (n) Object.assign(n, p); }
      placeInitial();
      rebuildView(false);
      if (!same) { S.alpha = 1; for (let i = 0; i < 280; i++) tick(); S.alpha = 0.05; }
      hideEmpty();
      $("#wsName").textContent = S.ws.name;
      document.title = `${S.ws.name} | Fabric Explorer`;
      if (keepView) S.view = keepView; else fit(false);
      S.sel = keepSel && S.nodes.has(keepSel) ? keepSel : null;
      computeActive();
      renderAll();
      setActivity("");
      for (const note of data.notes || []) console.info(note);
      if (itemId) reveal("item:" + itemId);
      refreshLog();
    } catch (err) {
      setActivity("");
      if (same) toast(err.message); else showEmpty("error", err);
    }
  }
  function defaultOpen(g) {
    if (S.groupMode === "type") return (S.hello.openGroups || ["Lakehouse", "Notebook"]).includes(g.key);
    return g.members.length <= 40;
  }
  async function openLink(text, msg) {
    const t = String(text || "").trim();
    if (!t) return;
    const say = (m, bad) => { if (msg) { msg.textContent = m; msg.classList.toggle("bad", !!bad); } else if (bad) toast(m); };
    say("Looking it up…");
    try {
      const r = await api(`/api/link?text=${encodeURIComponent(t)}`);
      if (r.error) { say(r.error, true); return; }
      say("");
      if (S.ws && S.ws.id === r.workspaceId) { closePops(); if (r.itemId) reveal("item:" + r.itemId); return; }
      await openWorkspace(r.workspaceId, r.itemId || "");
    } catch (err) { say(err.message, true); }
  }
  async function loadWorkspaces() {
    if (S.wsList) return S.wsList;
    try { S.wsList = (await api("/api/workspaces")).value; } catch (err) { S.wsList = null; throw err; }
    return S.wsList;
  }
  async function renderWsList(list, filterInput) {
    list.innerHTML = '<li class="muted" style="padding:6px 8px">Loading your workspaces…</li>';
    let all;
    try { all = await loadWorkspaces(); } catch (err) { list.innerHTML = `<li class="msg bad" style="padding:6px 8px">${esc(err.message)}</li>`; return; }
    const draw = () => {
      const q = (filterInput.value || "").trim().toLowerCase();
      const hits = all.filter(w => !q || w.name.toLowerCase().includes(q)).slice(0, 200);
      list.innerHTML = hits.length ? hits.map(w => `<li><button data-ws="${esc(w.id)}" class="${S.ws && S.ws.id === w.id ? "on" : ""}"><span>${esc(w.type === "Personal" ? "My workspace" : w.name)}</span>${S.ws && S.ws.id === w.id ? "<small>open now</small>" : ""}</button></li>`).join("")
        : '<li class="muted" style="padding:6px 8px">No workspace matches.</li>';
    };
    filterInput.oninput = draw;
    draw();
  }

  // ============================================================ model
  function resetModel(keepScans) {
    const scans = keepScans ? [...S.scans.values()] : [];
    S.nodes.clear(); S.edges.clear(); S.scans.clear();
    if (!keepScans) { S.busy.clear(); S.sel = null; S.selEdge = null; S.active = null; S.hover = null; S.multi = new Set(); S.offsets = new Map(); }
    return scans;
  }
  function folderPath(fid) {
    const parts = [];
    for (let f = S.folders.get(fid), guard = 0; f && guard < 12; f = S.folders.get(f.parentId), guard++) parts.unshift(f.name);
    return parts.join(" / ") || "Folder";
  }
  function rebuildGroups() {
    for (const [id, n] of S.nodes) if (n.kind === "group") S.nodes.delete(id);
    const groups = new Map();
    for (const n of S.nodes.values()) {
      if (n.kind !== "item" || n.external) continue;
      const key = S.groupMode === "folder" ? (n.folderId || "root") : n.type;
      const gid = `grp:${S.groupMode}:${key}`;
      n.group = gid;
      if (!groups.has(gid)) groups.set(gid, {key, members: []});
      groups.get(gid).members.push(n.id);
    }
    for (const [gid, g] of groups) {
      const label = S.groupMode === "folder" ? (g.key === "root" ? "Not in a folder" : folderPath(g.key)) : typeName(g.key, true);
      const order = S.groupMode === "folder" ? (g.key === "root" ? 999 : 0) : typeOrder(g.key);
      S.nodes.set(gid, {id: gid, kind: "group", key: g.key, label, members: g.members, order, x: NaN, y: NaN, vx: 0, vy: 0, w: 0, h: 0});
    }
  }
  function applyScan(res, quiet) {
    const itemId = res.itemId;
    for (const [id, e] of S.edges) if (e.from === itemId) S.edges.delete(id);
    S.scans.set(itemId, res);
    const anchor = "item:" + itemId, added = [];
    for (const nd of res.nodes || []) {
      let n = S.nodes.get(nd.id);
      if (!n) { n = {...nd, x: NaN, y: NaN, vx: 0, vy: 0, w: 0, h: 0, anchor}; S.nodes.set(nd.id, n); }
      else {
        for (const [k, v] of Object.entries(nd)) {
          if (v === "" || v === null || v === undefined) continue;
          if (n.kind === "item" && (k === "label" || k === "kind")) continue;
          if (k === "listed" && !v) continue;
          n[k] = v;
        }
      }
      if (quiet !== "silent" && (!quiet || !S.hidden.has(n.id)) && n.kind !== "table" && n.kind !== "file" && !S.hidden.has(n.id) && !S.shown.has(n.id)) {
        S.shown.add(n.id); if (n.id !== anchor) added.push(n.id);
      }
    }
    S.addedBy.set(itemId, [...new Set([...(S.addedBy.get(itemId) || []), ...added])]);
    for (const e of res.edges || []) S.edges.set(`${e.id}@${itemId}`, {...e, from: itemId});
    if (!quiet) { S.shown.add(anchor); }
  }

  // Unload: forget what was read for these items (here and in the app's memory). They go back to
  // "not read", their links leave the map, and whatever only those reads knew about goes too.
  async function unload(itemIds) {
    const ids = [...new Set(itemIds)].filter(id => S.scans.has(id) && !S.busy.has(id));
    if (!ids.length) { toast("Nothing to unload: none of these has been read."); return; }
    try { await api("/api/unload", {workspaceId: S.ws.id, itemIds: ids}); }
    catch (err) { toast(err.message); return; }
    forgetLocally(ids, false);
    rebuildView(); renderAll(); renderColumns(); refreshLog();
    toast(`Unloaded ${ids.length} item${ids.length === 1 ? "" : "s"}: back to not read.`);
  }
  // Drop what was read for these items from the page. keep (a refresh): the choices made on the map
  // stay (tickboxes, Folders, Contents, columns shown), since the same items come back with the new read.
  function forgetLocally(ids, keep) {
    const added = new Set();
    for (const id of ids) {
      const nid = "item:" + id, n = S.nodes.get(nid);
      if (n && n.type === "Lakehouse") {
        if (keep) for (const t of lakeData(nid)) { const k = colKey(t); if (k) { S.colCache.delete(k); S.colStale.add(k); } }
        else { resetLake(nid); S.treeDD.delete(nid); for (const k of [...S.ddOpen]) if (k.endsWith("|" + nid)) S.ddOpen.delete(k); }
      }
      S.scans.delete(id);
      for (const [eid, e] of S.edges) if (e.from === id) S.edges.delete(eid);
      if (!keep) { for (const a of S.addedBy.get(id) || []) added.add(a); S.addedBy.delete(id); }
    }
    const known = new Set(), listed = new Set();
    for (const sc of S.scans.values()) for (const nd of sc.nodes || []) { known.add(nd.id); if (nd.listed) listed.add(nd.id); }
    for (const [id, n] of [...S.nodes]) {
      if ((n.kind === "item" && !n.external) || n.kind === "group" || n.kind === "schema") continue;
      if (!known.has(id)) { S.nodes.delete(id); S.multi.delete(id); if (!keep) { S.shown.delete(id); S.hidden.delete(id); } continue; }
      if (isTable(n) && !listed.has(id)) { delete n.listed; delete n.format; delete n.tableType; delete n.shortcut; }
    }
    if (keep) return;
    for (const a of added) if (!known.has(a)) S.shown.delete(a);   // put on the map by those reads only
    if (S.sel && !S.nodes.has(S.sel)) S.sel = null;
    if (S.colTarget && !S.nodes.has(S.colTarget)) S.colTarget = null;
    if (S.focusMode) for (const id of [...S.focusMode]) if (!S.nodes.has(id)) S.focusMode.delete(id);
  }
  // Refresh: read the items again from Fabric, dropping what was read before (so removed tables and
  // links go), while everything chosen on the map stays as it is
  function refreshItems(itemIds) {
    const ids = [...new Set(itemIds)].filter(id => !S.busy.has(id));
    if (!ids.length) return;
    forgetLocally(ids.filter(id => S.scans.has(id)), true);
    for (const id of ids) enqueue(id, true);
    toast(`Reading ${ids.length === 1 ? "it" : ids.length + " items"} again from Fabric…`);
  }
  const readIds = nodes => nodes.filter(n => n && n.kind === "item" && !n.external && S.scans.has(n.itemId)).map(n => n.itemId);

  // ============================================================ what is on the map
  function hasDataEdge(id) {
    for (const e of S.edges.values()) if ((e.source === id || e.target === id) && DATA_KINDS.has(e.kind)) return true;
    return false;
  }
  const isTable = n => !!n && (n.kind === "table" || n.kind === "file");
  // the folder a table or Files folder sits in: "t:<schema>", "t:Tables" (no schemas) or "files"
  const secKeyOf = t => t.kind === "file" ? "files" : "t:" + (t.schema || "Tables");
  const folderId = (lid, key) => `fold:${lid}|${key}`;
  function itemOnMap(n) {
    if (S.hidden.has(n.id)) return false;
    return S.shown.has(n.id) || (S.expanded.has(n.group) && (n.type !== "SQLEndpoint" || S.groupMode === "folder"));
  }
  // On the map: ticked items; tables and folders linked to something on the map (or shown as a
  // lakehouse's contents); items elsewhere only while something on the map links to them. So taking
  // a notebook off the map also takes away the tables only it touches.
  function computeVisible() {
    const vis = new Set();
    for (const n of S.nodes.values()) {
      if (n.kind === "group") { if (S.opts.structure) vis.add(n.id); }
      else if (n.kind === "item" && !n.external && itemOnMap(n)) vis.add(n.id);
    }
    const touches = new Map();
    const touch = (a, b) => { let l = touches.get(a); if (!l) touches.set(a, l = []); l.push(b); };
    for (const e of S.edges.values()) { touch(e.source, e.target); touch(e.target, e.source); }
    const cand = new Set();
    for (const n of S.nodes.values()) {
      if (!isTable(n) || S.hidden.has(n.id)) continue;
      if ((S.contents.has(n.parent) && n.listed) || S.shown.has(n.id)) { cand.add(n.id); continue; }
      for (const o of touches.get(n.id) || []) { const m = S.nodes.get(o); if (m && !isTable(m) && vis.has(o)) { cand.add(n.id); break; } }
    }
    for (const n of S.nodes.values()) {             // the source of a shortcut that is on the map
      if (!isTable(n) || cand.has(n.id) || S.hidden.has(n.id)) continue;
      for (const o of touches.get(n.id) || []) if (cand.has(o)) { cand.add(n.id); break; }
    }
    const candParents = new Set([...cand].map(id => S.nodes.get(id).parent));
    for (const n of S.nodes.values()) {
      if (vis.has(n.id) || n.kind === "group" || n.kind === "schema" || isTable(n) || S.hidden.has(n.id)) continue;
      if (n.kind === "item" && !n.external) continue;
      if (candParents.has(n.id) || (touches.get(n.id) || []).some(o => vis.has(o) || cand.has(o))) vis.add(n.id);
    }
    if (S.opts.tables) for (const id of cand) if (vis.has(S.nodes.get(id).parent)) vis.add(id);
    // Folders (schemas, Tables, Files) of the lakehouses whose Folders option is on. A folder shows
    // when its own tickbox says so; until then, when one of its datasets is on the map. The datasets
    // on the map hang under their folder.
    for (const n of S.nodes.values()) if (isTable(n)) n.schemaNode = undefined;
    const counts = new Map();
    for (const lid of S.folderMode) {
      const lh = S.nodes.get(lid);
      if (!lh || !vis.has(lid)) continue;
      const {tables, folders} = lakeLists(lh), secs = new Map();
      for (const t of [...tables, ...folders]) { const k = secKeyOf(t); if (!secs.has(k)) secs.set(k, []); secs.get(k).push(t); }
      for (const [k, list] of secs) {
        const fid = folderId(lid, k), shown = list.filter(t => vis.has(t.id));
        if (!(S.folderVis.has(fid) ? S.folderVis.get(fid) : shown.length > 0)) continue;
        if (!S.nodes.has(fid)) S.nodes.set(fid, {id: fid, kind: "schema", type: "Folder", sec: k, label: k === "files" ? "Files" : k.slice(2),
          parent: lid, x: NaN, y: NaN, vx: 0, vy: 0, w: 0, h: 0});
        vis.add(fid);
        for (const t of shown) t.schemaNode = fid;
        counts.set(fid, shown.length);
      }
    }
    for (const n of S.nodes.values()) if (n.kind === "schema") n.count = counts.get(n.id) || 0;
    // focus mode: only what was highlighted when it started, and whatever has been ticked since
    if (S.focusMode) {
      const folders = new Set();
      for (const id of S.focusMode) { const t = S.nodes.get(id); if (t && t.schemaNode) folders.add(t.schemaNode); }
      for (const id of [...vis]) if (!S.focusMode.has(id) && !folders.has(id)) vis.delete(id);
    }
    return vis;
  }
  function rebuildView(heat = true) {
    const linked = new Set();
    for (const e of S.edges.values()) { linked.add(e.source); linked.add(e.target); }
    for (const id of [...linked]) { const n = S.nodes.get(id); if (n && n.parent) linked.add(n.parent); }
    const vis = computeVisible();
    const disp = id => {
      if (vis.has(id)) return id;
      const n = S.nodes.get(id);
      return n && (n.kind === "table" || n.kind === "file") && n.parent && vis.has(n.parent) ? n.parent : null;
    };
    const agg = new Map();
    for (const e of S.edges.values()) {
      const st = EDGE[e.kind];
      if (!st || (st.settings && !S.opts.settings)) continue;
      const a = disp(e.source), b = disp(e.target);
      if (!a || !b || a === b) continue;
      const key = `${e.kind}|${a}|${b}`;
      let v = agg.get(key);
      if (!v) agg.set(key, v = {id: key, kind: e.kind, source: a, target: b, members: []});
      v.members.push(e);
    }
    if (S.opts.structure) for (const id of vis) {
      const n = S.nodes.get(id);
      if (n.kind === "item" && n.group && vis.has(n.group) && !linked.has(id)) agg.set(`structure|${n.group}|${id}`, {id: `structure|${n.group}|${id}`, kind: "structure", source: n.group, target: id, members: []});
    }
    for (const id of vis) {                         // a lakehouse's contents: lakehouse -> schema -> table
      const n = S.nodes.get(id);
      if (n.kind === "schema") agg.set(`structure|${n.parent}|${id}`, {id: `structure|${n.parent}|${id}`, kind: "structure", source: n.parent, target: id, members: []});
      else if (isTable(n) && n.schemaNode && vis.has(n.schemaNode)) agg.set(`structure|${n.schemaNode}|${id}`, {id: `structure|${n.schemaNode}|${id}`, kind: "structure", source: n.schemaNode, target: id, members: []});
    }
    const rank = n => n.kind === "group" ? 0 : isTable(n) ? 2 : n.kind === "schema" ? 1.5 : 1;
    const nodes = [...vis].map(id => S.nodes.get(id)).sort((a, b) => rank(a) - rank(b));
    const edges = [...agg.values()];
    const out = new Map(), inn = new Map(), adj = new Map();
    const push = (m, k, e) => { let l = m.get(k); if (!l) m.set(k, l = []); l.push(e); };
    const pairs = new Map();
    for (const e of edges) {
      push(out, e.source, e); push(inn, e.target, e); push(adj, e.source, e); push(adj, e.target, e);
      if (e.kind === "structure") { e.curve = 0; continue; }
      const pk = e.source < e.target ? e.source + "|" + e.target : e.target + "|" + e.source;
      push(pairs, pk, e);
    }
    for (const list of pairs.values()) list.forEach((e, i) => {
      const flip = e.source > e.target ? -1 : 1;
      e.curve = list.length > 1 ? (i - (list.length - 1) / 2) * 28 * flip : 0;
    });
    for (const n of nodes) { n.dataLinked = linked.has(n.id); if (!n.w || (n.kind === "table" && (n._cols || S.colsOnMap.has(n.id)))) size(n); }
    rankNodes(nodes, edges);
    V = {nodes, edges, vis, out, inn, adj};
    for (const n of nodes) if (!Number.isFinite(n.x)) placeNew(n);
    if (S.sel && !vis.has(S.sel) && S.nodes.get(S.sel)?.kind !== "group") { /* keep the selection in the panel */ }
    computeActive();
    if (S.layout === "struct") {
      if (S.active && S.active.size > 1) { S.focusIds = new Set(S.active); S.focusSig = [...S.active].sort().join("|"); }
      layoutStructured(heat, S.sel);
    }
    else if (heat) reheat(0.45);
  }

  // ============================================================ layout
  // Data-flow columns: longest path from the sources, so every link points left to right where it can.
  // Cycles (a notebook reading and writing the same table) are broken where they are found.
  let COLX = [], GAP = 84;
  function rankNodes(nodes, edges) {
    for (const n of nodes) n.rank = undefined;
    // shortcuts aren't processing steps: a shortcut stays with its own lakehouse
    const flow = edges.filter(e => EDGE[e.kind] && EDGE[e.kind].flow && e.kind !== "shortcut");
    const indeg = new Map(), outs = new Map(), ids = new Set();
    for (const e of flow) {
      ids.add(e.source); ids.add(e.target);
      indeg.set(e.target, (indeg.get(e.target) || 0) + 1);
      let l = outs.get(e.source); if (!l) outs.set(e.source, l = []); l.push(e.target);
    }
    const rank = new Map(), left = new Set(ids), queue = [];
    for (const id of ids) { rank.set(id, 0); if (!indeg.get(id)) queue.push(id); }
    while (left.size) {
      if (!queue.length) {                       // a cycle: release the node with the fewest open inputs
        let best = null;
        for (const id of left) if (best === null || (indeg.get(id) || 0) < (indeg.get(best) || 0)) best = id;
        queue.push(best);
      }
      const u = queue.shift();
      if (!left.has(u)) continue;
      left.delete(u);
      const r = rank.get(u) || 0;
      for (const v of outs.get(u) || []) {
        if (!left.has(v)) continue;
        rank.set(v, Math.max(rank.get(v) || 0, r + 1));
        indeg.set(v, (indeg.get(v) || 1) - 1);
        if (!indeg.get(v)) queue.push(v);
      }
    }
    let max = 0;
    for (const n of nodes) if (rank.has(n.id)) { n.rank = rank.get(n.id); max = Math.max(max, n.rank); }
    for (const n of nodes) {                     // a lakehouse sits among its tables
      if (n.rank !== undefined) continue;
      const kids = nodes.filter(m => m.parent === n.id && m.rank !== undefined);
      if (kids.length) n.rank = kids.reduce((t, m) => t + m.rank, 0) / kids.length;
    }
    // each column as wide as its widest item, centred around x = 0
    const widths = new Array(max + 1).fill(110);
    for (const n of nodes) if (Number.isInteger(n.rank)) widths[n.rank] = Math.max(widths[n.rank], n.w || 0);
    let x = 0;
    COLX = widths.map(w => { const c = x + w / 2; x += w + GAP; return c; });
    const mid = (x - GAP) / 2;
    COLX = COLX.map(c => c - mid);
  }
  function colX(rank) {
    if (!COLX.length) return 0;
    const lo = Math.max(0, Math.floor(rank)), hi = Math.min(COLX.length - 1, Math.ceil(rank));
    return lo === hi ? COLX[lo] : COLX[lo] + (COLX[hi] - COLX[lo]) * (rank - lo);
  }
  function placeInitial() {
    const groups = [...S.nodes.values()].filter(n => n.kind === "group").sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));
    const R = 220 + groups.length * 40;
    groups.forEach((g, i) => {
      if (Number.isFinite(g.x)) return;
      const placed = g.members.map(id => S.nodes.get(id)).filter(m => m && Number.isFinite(m.x));
      if (placed.length) { g.x = placed.reduce((s, m) => s + m.x, 0) / placed.length; g.y = placed.reduce((s, m) => s + m.y, 0) / placed.length - 60; return; }
      const a = (i / Math.max(groups.length, 1)) * Math.PI * 2 - Math.PI / 2;
      g.x = Math.cos(a) * R * 1.4; g.y = Math.sin(a) * R;
    });
    for (const g of groups) g.members.forEach((id, j) => {
      const m = S.nodes.get(id);
      if (!m || Number.isFinite(m.x)) return;
      const ring = 1 + Math.floor(j / 10), a = (j % 10) / 10 * Math.PI * 2 + ring * 0.4;
      m.x = g.x + Math.cos(a) * (110 + ring * 70); m.y = g.y + Math.sin(a) * (80 + ring * 55);
    });
  }
  function placeNew(n) {
    let a = n.parent && S.nodes.get(n.parent);
    if (!a || !Number.isFinite(a.x)) a = n.anchor && S.nodes.get(n.anchor);
    if ((!a || !Number.isFinite(a.x)) && n.group) a = S.nodes.get(n.group);
    const ang = Math.random() * Math.PI * 2, r = 70 + Math.random() * 90;
    if (a && Number.isFinite(a.x)) { n.x = a.x + Math.cos(ang) * r; n.y = a.y + Math.sin(ang) * r; }
    else { n.x = (Math.random() - 0.5) * 400; n.y = (Math.random() - 0.5) * 300; }
    n.vx = n.vy = 0;
  }
  function spring(a, b, L, k) {
    let dx = b.x - a.x, dy = b.y - a.y;
    const l = Math.hypot(dx, dy) || 1, f = (l - L) / l * k;
    dx *= f; dy *= f;
    b.vx -= dx * 0.5; b.vy -= dy * 0.5; a.vx += dx * 0.5; a.vy += dy * 0.5;
  }
  // pull two nodes to the same height only (columns decide x)
  function level(a, b, k) { const d = (b.y - a.y) * k; a.vy += d * 0.5; b.vy -= d * 0.5; }
  function tick() {
    const nodes = V.nodes, a = S.alpha, cols = S.opts.flow;
    if (!nodes.length || S.layout === "struct") { S.alpha = 0; return; }
    for (const e of V.edges) {
      const s = S.nodes.get(e.source), t = S.nodes.get(e.target);
      if (e.kind === "structure") {
        const fold = s.kind === "schema" || t.kind === "schema";          // folders stay close to their lakehouse and tables
        spring(s, t, (fold ? 30 : 90) + (s.w + t.w) / 4, (fold ? 0.35 : 0.12) * a);
      }
      else if (EDGE[e.kind].settings) continue;
      else if (cols && s.rank !== undefined && t.rank !== undefined) level(s, t, 0.05 * a);
      else if (e.kind === "references") spring(s, t, 160 + (s.w + t.w) / 4, 0.05 * a);
      else spring(s, t, 110 + (s.w + t.w) / 3, 0.18 * a);
    }
    for (const n of nodes) if ((n.kind === "table" || n.kind === "file") && V.vis.has(n.parent) && !(n.schemaNode && V.vis.has(n.schemaNode))) {
      const p = S.nodes.get(n.parent);
      if (cols && n.rank !== undefined) level(p, n, 0.12 * a);
      else spring(p, n, 50 + (p.w + n.w) / 4, 0.35 * a);
    }
    let top = Infinity, ranked = 0;
    if (S.opts.flow) for (const n of nodes) {
      if (n.rank === undefined) continue;
      ranked++; top = Math.min(top, n.y - n.h / 2);
      n.vx += (colX(n.rank) - n.x) * 0.16 * a;
    }
    if (ranked) for (const n of nodes) {          // groups and items not linked yet wait in a band above
      if (!(n.kind === "group" || (n.kind === "item" && !n.dataLinked))) continue;
      const want = top - 170;
      if (n.y > want) n.vy += (want - n.y) * 0.08 * a;
    }
    // repulsion, bucketed
    const cell = 320, grid = new Map();
    for (const n of nodes) { const k = `${Math.floor(n.x / cell)},${Math.floor(n.y / cell)}`; let l = grid.get(k); if (!l) grid.set(k, l = []); l.push(n); }
    for (const n of nodes) {
      const cx = Math.floor(n.x / cell), cy = Math.floor(n.y / cell);
      const mn = n.kind === "group" ? 1.6 : (n.kind === "table" || n.kind === "file") ? 0.55 : 1;
      for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
        const l = grid.get(`${cx + i},${cy + j}`);
        if (!l) continue;
        for (const m of l) {
          if (m === n) continue;
          let dx = n.x - m.x, dy = n.y - m.y, d2 = dx * dx + dy * dy;
          if (d2 > cell * cell) continue;
          if (d2 < 1) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 1; }
          const mm = m.kind === "group" ? 1.6 : (m.kind === "table" || m.kind === "file") ? 0.55 : 1;
          const f = 1100 * a * mn * mm / d2;
          n.vx += dx * f * 0.5; n.vy += dy * f * 0.5;
        }
      }
    }
    for (const n of nodes) { const g = (n.kind === "group" ? 0.012 : 0.006) * a; if (n.rank === undefined || !S.opts.flow) n.vx -= n.x * g; n.vy -= n.y * g * 1.6; }
    for (const n of nodes) {
      if (n.fixed) { n.vx = n.vy = 0; continue; }
      n.vx *= 0.6; n.vy *= 0.6;
      n.x += clamp(n.vx, -60, 60); n.y += clamp(n.vy, -60, 60);
    }
    collide(nodes);
    S.alpha *= 0.986;
  }
  function collide(nodes) {
    const cell = 260, grid = new Map(), pad = 14;
    for (const n of nodes) { const k = `${Math.floor(n.x / cell)},${Math.floor(n.y / cell)}`; let l = grid.get(k); if (!l) grid.set(k, l = []); l.push(n); }
    for (const n of nodes) {
      const cx = Math.floor(n.x / cell), cy = Math.floor(n.y / cell);
      for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
        const l = grid.get(`${cx + i},${cy + j}`);
        if (!l) continue;
        for (const m of l) {
          if (m === n || m.id < n.id) continue;
          const ox = (n.w + m.w) / 2 + pad - Math.abs(n.x - m.x), oy = (n.h + m.h) / 2 + pad - Math.abs(n.y - m.y);
          if (ox <= 0 || oy <= 0) continue;
          const wn = n.fixed ? 0 : m.fixed ? 1 : 0.5, wm = 1 - wn;
          if (ox < oy * 2.2) { const s = (n.x < m.x ? -1 : 1) * ox; n.x += s * wn; m.x -= s * wm; }
          else { const s = (n.y < m.y ? -1 : 1) * oy; n.y += s * wn; m.y -= s * wm; }
        }
      }
    }
  }
  function reheat(a) { if (S.layout === "struct") { requestDraw(); return; } S.alpha = Math.max(S.alpha, a); requestDraw(); }

  // ============================================================ structured view
  // Lakehouses (and other data stores) become columns: the store at the top, then its schemas (or
  // Tables) and Files, with their tables and folders listed underneath in name order. Columns follow
  // the data flow. Notebooks and the other items that move data sit in the gaps between the stores
  // they read and write, ordered by the height of what they connect to, so links cross as little as
  // possible. Items without links wait above, grouped like the left panel.
  let LANES = [], BOXSET = null;
  const STORE_TYPES = new Set(["Lakehouse", "Warehouse", "SQLEndpoint", "MirroredDatabase", "KQLDatabase", "SQLDatabase"]);
  function layoutStructured(animate = true, anchor = null) {
    const LIT = S.focusIds && S.focusIds.size > 1 ? S.focusIds : null;   // the chain to line up, if any
    const nodes = V.nodes, vis = V.vis, target = new Map(), adj = new Map();
    const link = (a, b, e) => { let l = adj.get(a); if (!l) adj.set(a, l = []); l.push({o: b, e}); };
    for (const e of V.edges) if (e.kind !== "structure" && EDGE[e.kind] && !EDGE[e.kind].settings) { link(e.source, e.target, e); link(e.target, e.source, e); }
    const rowsOf = new Map();
    for (const n of nodes) if (isTable(n) && vis.has(n.parent)) { let l = rowsOf.get(n.parent); if (!l) rowsOf.set(n.parent, l = []); l.push(n); }
    const withFolders = new Set(nodes.filter(f => f.kind === "schema").map(f => f.parent));
    const isStore = n => rowsOf.has(n.id) || withFolders.has(n.id) || ((n.kind === "external" || n.kind === "missing") && adj.has(n.id)) ||
      (n.kind === "item" && STORE_TYPES.has(n.type) && adj.has(n.id));
    const stores = nodes.filter(n => n.kind !== "group" && n.kind !== "schema" && !isTable(n) && isStore(n));
    const storeSet = new Set(stores.map(n => n.id));
    const procs = nodes.filter(n => n.kind === "item" && !storeSet.has(n.id) && adj.has(n.id));
    const procSet = new Set(procs.map(n => n.id));
    const medal = n => { const t = String(n.label).toLowerCase(); return t.includes("bronze") ? 0 : t.includes("silver") ? 1 : t.includes("gold") ? 2 : 3; };
    for (const n of stores) {
      const rs = (rowsOf.get(n.id) || []).map(m => m.rank).filter(r => r !== undefined);
      if (n.rank !== undefined) rs.push(n.rank);
      n._stage = rs.length ? rs.reduce((a, b) => a + b, 0) / rs.length : 1000 + medal(n);
    }
    const keys = [...new Set(stores.map(n => Math.round(n._stage)))].sort((a, b) => a - b);
    const laneLit = n => LIT && (LIT.has(n.id) || (rowsOf.get(n.id) || []).some(r => LIT.has(r.id))) ? 0 : 1;
    const cols = keys.map(k => stores.filter(n => Math.round(n._stage) === k)
      .sort((a, b) => laneLit(a) - laneLit(b) || a._stage - b._stage || medal(a) - medal(b) || String(a.label).localeCompare(String(b.label))));
    const colOf = new Map();
    cols.forEach((list, i) => list.forEach(n => colOf.set(n.id, i)));
    const storeOf = id => { if (storeSet.has(id)) return id; const n = S.nodes.get(id); return n && isTable(n) && storeSet.has(n.parent) ? n.parent : null; };
    // gutter g lies left of data column g; the last one right of everything
    const gut = new Map(), sub = new Map();
    for (const p of procs) {
      const ins = [], outs = [];
      for (const x of adj.get(p.id) || []) {
        const st = storeOf(x.o);
        if (st === null) continue;
        (x.e.target === p.id ? ins : outs).push(colOf.get(st));
      }
      if (!ins.length && !outs.length) continue;
      let best = null;
      for (let g = 0; g <= cols.length; g++) {
        let cost = 0, dist = 0;
        for (const c of ins) { if (c >= g) cost += c - g + 1; dist += Math.abs(g - (c + 0.5)); }
        for (const c of outs) { if (c < g) cost += g - c; dist += Math.abs(g - (c + 0.5)); }
        if (!best || cost < best.cost || (cost === best.cost && (dist < best.dist - 1e-9 || (Math.abs(dist - best.dist) < 1e-9 && g > best.g)))) best = {g, cost, dist};
      }
      gut.set(p.id, best.g); sub.set(p.id, 0);
    }
    for (let pass = 0; pass < 8; pass++) for (const p of procs) {   // orchestrators before what they run, reports after their model
      if (gut.has(p.id)) continue;
      let g = null, sb = 0;
      for (const x of adj.get(p.id) || []) {
        if (!procSet.has(x.o) || !gut.has(x.o)) continue;
        const gg = gut.get(x.o), ss = sub.get(x.o) + (x.e.source === p.id ? -1 : 1);
        if (g === null || (x.e.source === p.id ? (gg < g || (gg === g && ss < sb)) : (gg > g || (gg === g && ss > sb)))) { g = gg; sb = ss; }
      }
      if (g !== null) { gut.set(p.id, g); sub.set(p.id, sb); }
    }
    for (const p of procs) if (!gut.has(p.id)) { gut.set(p.id, cols.length); sub.set(p.id, 0); }
    const subsIn = [];
    for (let g = 0; g <= cols.length; g++) {
      const list = procs.filter(p => gut.get(p.id) === g);
      const lo = list.length ? Math.min(...list.map(p => sub.get(p.id))) : 0;
      for (const p of list) sub.set(p.id, sub.get(p.id) - lo);
      subsIn.push(list.length ? Math.max(...list.map(p => sub.get(p.id))) + 1 : 0);
    }
    // widths
    const storeW = n => clamp(Math.max(n.kind === "item" && n.type === "Lakehouse" ? measure(n.label, F.lake()) + 70 : measure(n.label, F.item()) + 60,
      ...(rowsOf.get(n.id) || []).map(r => r._cols ? r.w + 20 : measure(r.label, F.small()) + 52)), 190, 380);
    const colW = cols.map(list => Math.max(...list.map(storeW)));
    const procW = Math.max(140, ...procs.map(p => p.w || 140)), SUBW = procW + 70;
    let x = 0;
    const gutX = [], colX = [];
    for (let g = 0; g <= cols.length; g++) {
      const w = subsIn[g] ? subsIn[g] * SUBW + 40 : (g === 0 || g === cols.length ? 0 : 90);
      gutX.push(x + 40); x += w;
      if (g < cols.length) { colX.push(x + colW[g] / 2); x += colW[g]; }
    }
    // data columns: store, then sections (schemas or Tables, then Files) with their rows
    LANES = [];
    cols.forEach((list, c) => {
      let y = 0;
      for (const n of list) {
        const cw = colW[c];
        n.w = cw;
        target.set(n.id, [colX[c], y + n.h / 2]); y += n.h + 16;
        const lane = {id: n.id, rows: [], sections: []}, secs = new Map();
        for (const r of rowsOf.get(n.id) || []) { const k = r.kind === "file" ? "~Files" : (r.schema || "Tables"); if (!secs.has(k)) secs.set(k, []); secs.get(k).push(r); }
        const fnode = k => { const f = S.nodes.get(folderId(n.id, k === "~Files" ? "files" : "t:" + k)); return f && vis.has(f.id) ? f : null; };
        for (const f of nodes) if (f.kind === "schema" && f.parent === n.id) { const k = f.sec === "files" ? "~Files" : f.sec.slice(2); if (!secs.has(k)) secs.set(k, []); }
        const secLit = k => LIT && secs.get(k).some(r => LIT.has(r.id)) ? 0 : 1;
        const order = [...secs.keys()].sort((a, b) => secLit(a) - secLit(b) || (a === "~Files") - (b === "~Files") || a.localeCompare(b));
        const heads = order.length > 1 || (order.length === 1 && order[0] !== "Tables") || order.some(fnode);
        for (const k of order) {
          const rows = secs.get(k).sort((a, b) => (LIT ? LIT.has(b.id) - LIT.has(a.id) : 0) || byLabel(a, b)), sn = fnode(k);
          if (sn) { sn.w = cw - 20; target.set(sn.id, [colX[c], y + sn.h / 2]); y += sn.h + 8; lane.sections.push({node: sn.id}); }
          else if (heads && rows.length) { lane.sections.push({label: k === "~Files" ? "Files" : k, first: rows[0].id}); y += 22; }
          for (const r of rows) { const h = r._cols ? r.h : 24; r.w = cw - 20; r.h = h; target.set(r.id, [colX[c], y + h / 2]); lane.rows.push(r.id); y += h + 6; }
        }
        LANES.push(lane);
        y += 44;
      }
    });
    // items between the columns, in the order of what they connect to
    const bary = new Map();
    for (const p of procs) {
      const ys = (adj.get(p.id) || []).map(x => target.get(x.o)).filter(Boolean).map(v => v[1]);
      if (ys.length) bary.set(p.id, ys.reduce((a, b) => a + b, 0) / ys.length);
    }
    for (let pass = 0; pass < 4; pass++) for (const p of procs) {
      if (bary.has(p.id) && pass === 0) continue;
      const ys = (adj.get(p.id) || []).map(x => bary.get(x.o)).filter(v => v !== undefined);
      if (ys.length && !(adj.get(p.id) || []).some(x => target.has(x.o))) bary.set(p.id, ys.reduce((a, b) => a + b, 0) / ys.length);
    }
    for (let g = 0; g <= cols.length; g++) for (let sb = 0; sb < subsIn[g]; sb++) {
      const list = procs.filter(p => gut.get(p.id) === g && sub.get(p.id) === sb)
        .sort((a, b) => (LIT ? LIT.has(b.id) - LIT.has(a.id) : 0) || (bary.get(a.id) || 0) - (bary.get(b.id) || 0) || String(a.label).localeCompare(String(b.label)));
      let last = -Infinity;
      for (const p of list) {
        const y = Math.max(bary.get(p.id) || 0, last + p.h / 2 + 18);
        target.set(p.id, [gutX[g] + sb * SUBW + procW / 2, y]); last = y + p.h / 2;
      }
    }
    // what isn't linked: grouped (like the left panel) in a band above
    const rest = nodes.filter(n => !target.has(n.id) && n.kind !== "group" && n.kind !== "schema" && !isTable(n));
    const blocks = new Map();
    for (const g of nodes.filter(n => n.kind === "group").sort((a, b) => a.order - b.order || a.label.localeCompare(b.label))) blocks.set(g.id, {head: g, list: []});
    for (const n of rest) { const k = n.group && blocks.has(n.group) ? n.group : "~other"; if (!blocks.has(k)) blocks.set(k, {head: null, list: []}); blocks.get(k).list.push(n); }
    const width = Math.max(x, 900);
    let bx = 0, by = 0, rowH = 0;
    const placed = [];
    for (const b of blocks.values()) {
      b.list.sort(byLabel);
      const bw = Math.max(b.head ? b.head.w : 0, ...b.list.map(n => n.w || 120));
      const bh = (b.head ? b.head.h + 12 : 0) + b.list.reduce((t, n) => t + n.h + 10, 0);
      if (bx > 0 && bx + bw > width) { bx = 0; by += rowH + 36; rowH = 0; }
      placed.push({b, bx, by, bw});
      bx += bw + 36; rowH = Math.max(rowH, bh);
    }
    const bandH = by + rowH;
    for (const {b, bx: x0, by: y0, bw} of placed) {
      let y = y0 - bandH - 110;
      if (b.head) { target.set(b.head.id, [x0 + bw / 2, y + b.head.h / 2]); y += b.head.h + 12; }
      for (const n of b.list) { target.set(n.id, [x0 + bw / 2, y + n.h / 2]); y += n.h + 10; }
    }
    // the user's own moves, kept relative to this arrangement (moving a lakehouse carries its column)
    if (S.offsets.size) {
      const laneOf = new Map();
      for (const lane of LANES) { for (const id of lane.rows) laneOf.set(id, lane.id); for (const sc of lane.sections) if (sc.node) laneOf.set(sc.node, lane.id); }
      for (const [id, pos] of target) {
        const own = S.offsets.get(id), lane = laneOf.has(id) ? S.offsets.get(laneOf.get(id)) : null;
        const dx = (own ? own.dx : 0) + (lane ? lane.dx : 0), dy = (own ? own.dy : 0) + (lane ? lane.dy : 0);
        if (dx || dy) target.set(id, [pos[0] + dx, pos[1] + dy]);
      }
    }
    moveTo(target, animate, anchor);
  }
  function moveTo(target, animate, anchor = null) {
    // keep the anchor (the item just clicked) where it is on screen while everything else moves
    const a = anchor && S.nodes.get(anchor);
    if (a && target.has(anchor) && Number.isFinite(a.x)) {
      const [tx, ty] = target.get(anchor), k = S.view.k;
      if (Math.abs(tx - a.x) + Math.abs(ty - a.y) > 1) goTo({k, x: S.view.x - (tx - a.x) * k, y: S.view.y - (ty - a.y) * k}, animate && !REDUCED);
    }
    S.alpha = 0;
    if (!animate || REDUCED) {
      for (const [id, [x, y]] of target) { const n = S.nodes.get(id); if (n) { n.x = x; n.y = y; n.vx = n.vy = 0; } }
      S.tween = null; requestDraw(); return;
    }
    const items = [];
    for (const [id, [x, y]] of target) {
      const n = S.nodes.get(id);
      if (!n) continue;
      if (!Number.isFinite(n.x)) { n.x = x; n.y = y; continue; }
      items.push({n, x0: n.x, y0: n.y, x1: x, y1: y});
    }
    S.tween = {t0: performance.now(), dur: 420, items};
    requestDraw();
  }
  function stepTween(now) {
    const tw = S.tween, p = Math.min(1, Math.max(0, (now - tw.t0) / tw.dur)), e = 1 - Math.pow(1 - p, 3);
    for (const it of tw.items) { it.n.x = it.x0 + (it.x1 - it.x0) * e; it.n.y = it.y0 + (it.y1 - it.y0) * e; }
    if (p >= 1) S.tween = null;
  }
  function setLayout(mode) {
    if (mode === S.layout || !S.ws) { S.layout = mode; markLayout(); return; }
    if (mode === "struct") {
      for (const n of S.nodes.values()) if (Number.isFinite(n.x)) n._free = {x: n.x, y: n.y};
      S.layout = "struct";
      rebuildView(true);
    } else {
      S.layout = "free"; S.tween = null;
      for (const n of S.nodes.values()) {
        if (n._free) { n.x = n._free.x; n.y = n._free.y; }
        if (isTable(n) || n.kind === "schema" || n.kind === "missing" || n.kind === "external" || (n.kind === "item" && STORE_TYPES.has(n.type))) n.w = 0;
      }
      rebuildView(false); S.alpha = 0.3; requestDraw();
    }
    store.set("layout", S.layout); markLayout();
    setTimeout(() => fit(true), mode === "struct" ? 460 : 60);
  }
  function markLayout() {
    for (const b of document.querySelectorAll("[data-layout]")) b.setAttribute("aria-pressed", String(b.dataset.layout === S.layout));
    canvas.classList.toggle("struct", S.layout === "struct");
    const hint = $("#mapHint");
    if (hint) hint.textContent = S.layout === "struct"
      ? "Drag to pan, drag items to move them (Re-layout tidies up). Shift+drag selects several. + on a lakehouse shows its datasets; right-click an item for more."
      : "Drag to pan, drag items to move them. Shift+drag selects several, Shift+click adds or removes one. + on a lakehouse shows its datasets; right-click an item for more.";
  }
  function drawLanes(act) {
    for (const lane of LANES) {
      const head = S.nodes.get(lane.id);
      if (!head || !V.vis.has(lane.id)) continue;
      const parts = [head, ...lane.rows.map(id => S.nodes.get(id)), ...lane.sections.filter(sc => sc.node).map(sc => S.nodes.get(sc.node))]
        .filter(n => n && V.vis.has(n.id));
      let x0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of parts) { x0 = Math.min(x0, n.x - n.w / 2); x1 = Math.max(x1, n.x + n.w / 2); y1 = Math.max(y1, n.y + n.h / 2); }
      const y0 = head.y - head.h / 2, col = lakeColor(head), lit = !act || parts.some(n => act.has(n.id));
      ctx.beginPath(); rr(x0 - 12, y0 - 12, x1 - x0 + 24, y1 - y0 + 26, 10);
      ctx.fillStyle = rgba(col, S.opts.areas ? (lit ? C.areaFill : C.areaFill * 0.35) : 0.02); ctx.fill();
      ctx.lineWidth = 1.2; ctx.strokeStyle = rgba(col, lit ? 0.6 : 0.2); ctx.stroke();
      if (S.view.k >= 0.42) {
        ctx.font = `600 11px ${FONT}`; ctx.fillStyle = C.ink2; ctx.textAlign = "left"; ctx.textBaseline = "middle";
        for (const sc of lane.sections) {
          const f = sc.first && S.nodes.get(sc.first);
          if (f && V.vis.has(f.id)) ctx.fillText(sc.label, f.x - f.w / 2 + 2, f.y - f.h / 2 - 9);
        }
      }
    }
  }

  // ============================================================ drawing
  const canvas = $("#sheet"), ctx = canvas.getContext("2d");
  let W = 0, H = 0, DPR = 1, frameReq = 0;
  const measureCache = new Map();
  const F = {item: () => `500 12.5px ${FONT}`, lake: () => `600 13.5px ${FONT}`, small: () => `500 11.5px ${FONT}`,
    group: () => `600 12.5px ${FONT}`, tiny: () => `400 10.5px ${FONT}`};
  function measure(text, font) {
    const key = font + "\u0000" + text;
    let w = measureCache.get(key);
    if (w === undefined) { ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.font = font; w = ctx.measureText(text).width; ctx.restore(); measureCache.set(key, w); }
    return w;
  }
  function fitText(text, max, font) {
    text = String(text);
    if (measure(text, font) <= max) return text;
    let lo = 0, hi = text.length;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (measure(text.slice(0, mid) + "…", font) <= max) lo = mid; else hi = mid - 1; }
    return text.slice(0, Math.max(1, lo)) + "…";
  }
  const groupCount = n => String(n.members.length);
  function size(n) {
    if (n.kind === "group") { n.h = 28; n.w = clamp(measure(n.label, F.group()) + measure(groupCount(n), F.group()) + 52, 100, 320); }
    else if (n.kind === "schema") { n.h = 24; n.w = clamp(measure(n.label, F.small()) + 40, 70, 200); }
    else if (n.kind === "table" || n.kind === "file") {
      n.h = 24; n.w = clamp(measure(n.label, F.small()) + 32, 72, 230);
      n._cols = n.kind === "table" && S.colsOnMap.has(n.id) ? mapCols(n) : null;      // its columns, shown on the map
      if (n._cols) {
        const rows = Math.max(1, Math.min(n._cols.length, COLS_MAX) + (n._cols.length > COLS_MAX ? 1 : 0));
        n.h = 30 + rows * 15;
        n.w = clamp(Math.max(n.w, ...n._cols.slice(0, COLS_MAX).map(c => measure(c.name, F.small()) + measure(c.type, F.tiny()) + 44)), 160, 340);
      }
    }
    else if (n.kind === "item" && n.type === "Lakehouse") { n.h = 44; n.w = clamp(measure(n.label, F.lake()) + 70, 150, 290); }
    else { n.h = twoLine(n) ? 40 : 34; n.w = clamp(Math.max(measure(n.label, F.item()), twoLine(n) ? measure(subLine(n), F.tiny()) : 0) + 46, 120, 260); }
  }
  const twoLine = n => !!(n.external || n.kind === "external" || n.kind === "missing");
  const subLine = n => n.external ? `in ${n.workspaceName || "another workspace"}` : n.kind === "missing" ? "not found" : "outside Fabric";
  function rr(x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.arcTo(x + w, y, x + w, y + r, r);
    ctx.lineTo(x + w, y + h - r); ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h); ctx.arcTo(x, y + h, x, y + h - r, r);
    ctx.lineTo(x, y + r); ctx.arcTo(x, y, x + r, y, r); ctx.closePath();
  }
  function resize() {
    const r = canvas.getBoundingClientRect();
    DPR = window.devicePixelRatio || 1; W = r.width; H = r.height;
    canvas.width = Math.round(W * DPR); canvas.height = Math.round(H * DPR);
    requestDraw();
  }
  function requestDraw() { if (!frameReq) frameReq = requestAnimationFrame(frame); }
  function frame(t) {
    frameReq = 0;
    if (S.tween) stepTween(t);
    if (S.alpha > 0.004) { tick(); tick(); }
    draw(t);
    const scanning = [...S.busy.values()].some(v => v === "reading");
    if (S.alpha > 0.004 || S.tween || (scanning && !REDUCED)) requestDraw();
  }
  function draw(t = performance.now()) {
    const {x: ox, y: oy, k} = S.view;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.fillStyle = C.sheet; ctx.fillRect(0, 0, W, H);
    for (const [step, color, min] of [[24, C.grid, 10], [120, C.gridStrong, 16]]) {
      const s = step * k;
      if (s < min) continue;
      ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath();
      for (let x = ((ox % s) + s) % s; x < W; x += s) { const px = Math.round(x) + 0.5; ctx.moveTo(px, 0); ctx.lineTo(px, H); }
      for (let y = ((oy % s) + s) % s; y < H; y += s) { const py = Math.round(y) + 0.5; ctx.moveTo(0, py); ctx.lineTo(W, py); }
      ctx.stroke();
    }
    if (!S.ws) return;
    ctx.setTransform(DPR * k, 0, 0, DPR * k, DPR * ox, DPR * oy);
    const act = S.active;
    if (S.layout === "struct") drawLanes(act); else if (S.opts.areas) drawAreas(act);
    for (const e of V.edges) drawEdge(e, act, k);
    for (const n of V.nodes) drawNode(n, act, k, t);
    if (k < 0.5) farLabels(act, k);
    drawBadge(k);
    const pr = S.press;
    if (pr && pr.type === "box" && pr.moved) {
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      const bx = Math.min(pr.x0, pr.x1), by = Math.min(pr.y0, pr.y1), bw = Math.abs(pr.x1 - pr.x0), bh = Math.abs(pr.y1 - pr.y0);
      ctx.fillStyle = rgba(C.read, 0.08); ctx.fillRect(bx, by, bw, bh);
      ctx.setLineDash([5, 4]); ctx.lineWidth = 1.2; ctx.strokeStyle = C.read; ctx.strokeRect(bx + 0.5, by + 0.5, bw, bh); ctx.setLineDash([]);
    }
  }
  // Zoomed out, the names inside the boxes are too small to read: write the ones that matter
  // (the highlighted chain, lakehouses, groups, the hovered item) above their boxes at a readable size.
  function farLabels(act, k) {
    const size = 11.5 / k, placed = [];
    ctx.font = `600 ${size}px ${FONT}`; ctx.textAlign = "center"; ctx.textBaseline = "bottom";
    ctx.lineJoin = "round"; ctx.lineWidth = 4 / k; ctx.strokeStyle = C.sheet;
    const want = n => n.id === S.hover || n.id === S.sel || (act ? act.has(n.id) && n.kind !== "group" :
      n.kind === "group" || n.kind === "schema" || (n.kind === "item" && n.type === "Lakehouse") || n.kind === "missing");
    const list = V.nodes.filter(want).sort((a, b) => (b.id === S.sel) - (a.id === S.sel) || (a.kind === "table") - (b.kind === "table"));
    for (const n of list) {
      const text = n.kind === "group" ? `${n.label} ${n.members.length}` : n.label;
      const w = measure(text, `600 11.5px ${FONT}`) / k, x = n.x, y = n.y - n.h / 2 - 3 / k;
      const box = [x - w / 2, y - size, x + w / 2, y];
      if (n.id !== S.sel && placed.some(b => b[0] < box[2] && box[0] < b[2] && b[1] < box[3] && box[1] < b[3])) continue;
      placed.push(box);
      ctx.strokeText(text, x, y); ctx.fillStyle = n.kind === "missing" ? C.error : C.ink; ctx.fillText(text, x, y);
    }
  }
  function outline(n, pad, out) {
    const hw = n.w / 2 + pad, hh = n.h / 2 + pad, r = Math.min(pad + 4, hh);
    for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      const cx = n.x + sx * (hw - r), cy = n.y + sy * (hh - r);
      for (let i = 0; i <= 3; i++) { const a = Math.atan2(sy, sx) + (i - 1.5) * Math.PI / 6; out.push([cx + Math.cos(a) * r, cy + Math.sin(a) * r]); }
    }
  }
  function hull(pts) {
    pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lo = [], up = [];
    for (const p of pts) { while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], p) <= 0) lo.pop(); lo.push(p); }
    for (let i = pts.length - 1; i >= 0; i--) { const p = pts[i]; while (up.length >= 2 && cross(up[up.length - 2], up[up.length - 1], p) <= 0) up.pop(); up.push(p); }
    return lo.slice(0, -1).concat(up.slice(0, -1));
  }
  function drawAreas(act) {
    const kids = new Map();
    for (const n of V.nodes) if ((n.kind === "table" || n.kind === "file" || n.kind === "schema") && V.vis.has(n.parent)) {
      let l = kids.get(n.parent); if (!l) kids.set(n.parent, l = []); l.push(n);
    }
    for (const [pid, list] of kids) {
      const p = S.nodes.get(pid), col = lakeColor(p), pts = [];
      for (const m of [p, ...list]) outline(m, 14, pts);
      const h = hull(pts);
      if (h.length < 3) continue;
      const lit = !act || act.has(pid) || list.some(m => act.has(m.id));
      ctx.beginPath(); h.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)); ctx.closePath();
      ctx.fillStyle = rgba(col, lit ? C.areaFill : C.areaFill * 0.35); ctx.fill();
      ctx.lineJoin = "round"; ctx.lineWidth = 1.2; ctx.strokeStyle = rgba(col, lit ? 0.55 : 0.15); ctx.stroke();
    }
  }
  function clip(n, px, py) {
    const dx = px - n.x, dy = py - n.y;
    if (!dx && !dy) return [n.x, n.y];
    const hw = n.w / 2 + 3, hh = n.h / 2 + 3;
    const s = Math.min(Math.abs(hw / (dx || 1e-9)), Math.abs(hh / (dy || 1e-9)), 1);
    return [n.x + dx * s, n.y + dy * s];
  }
  function curveAt(g, u) {
    const v = 1 - u;
    if (g.cubic) return [v * v * v * g.sx + 3 * v * v * u * g.c1x + 3 * v * u * u * g.c2x + u * u * u * g.tx,
      v * v * v * g.sy + 3 * v * v * u * g.c1y + 3 * v * u * u * g.c2y + u * u * u * g.ty];
    return [v * v * g.sx + 2 * v * u * g.cx + u * u * g.tx, v * v * g.sy + 2 * v * u * g.cy + u * u * g.ty];
  }
  const anchorY = n => n._cols ? n.y - n.h / 2 + 12 : n.y;
  function geom(e, s, t) {
    if (S.layout === "struct") {
      const gap = Math.abs(t.x - s.x) - (s.w + t.w) / 2, sy = anchorY(s), ty = anchorY(t);
      if (gap > 8) {
        const dir = t.x > s.x ? 1 : -1, sx = s.x + dir * s.w / 2, tx = t.x - dir * t.w / 2, d = Math.max(30, Math.abs(tx - sx) * 0.45) * dir;
        return {cubic: true, sx, sy, c1x: sx + d, c1y: sy, c2x: tx - d, c2y: ty, tx, ty, cx: tx - d, cy: ty};
      }
      const sx = s.x + s.w / 2, tx = t.x + t.w / 2, d = 46 + Math.abs(ty - sy) * 0.12;     // same column: loop out right
      return {cubic: true, sx, sy, c1x: sx + d, c1y: sy, c2x: tx + d, c2y: ty, tx, ty, cx: tx + d, cy: ty};
    }
    const dx = t.x - s.x, dy = t.y - s.y, len = Math.hypot(dx, dy) || 1, off = e.curve || 0;
    const cx = (s.x + t.x) / 2 - dy / len * off, cy = (s.y + t.y) / 2 + dx / len * off;
    const [sx, sy] = clip(s, cx, cy), [tx, ty] = clip(t, cx, cy);
    return {sx, sy, cx, cy, tx, ty};
  }
  function drawEdge(e, act, k) {
    const s = S.nodes.get(e.source), t = S.nodes.get(e.target);
    if (!s || !t) return;
    const st = EDGE[e.kind] || EDGE.references;
    const on = !act || (act.has(s.id) && act.has(t.id));
    const sel = S.selEdge === e.id, hov = S.hoverEdge === e.id;
    ctx.globalAlpha = on ? (e.kind === "structure" ? (act ? 0.3 : 0.6) : st.settings && !sel && !hov ? (act ? 0.75 : 0.45) : 1) : 0.07;
    const g = geom(e, s, t), color = C[st.color] || C.muted;
    if (S.layout === "struct" && e.kind === "structure") { ctx.globalAlpha = 1; return; }
    ctx.beginPath(); ctx.moveTo(g.sx, g.sy);
    if (g.cubic) ctx.bezierCurveTo(g.c1x, g.c1y, g.c2x, g.c2y, g.tx, g.ty); else ctx.quadraticCurveTo(g.cx, g.cy, g.tx, g.ty);
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(st.w * (sel || hov ? 2.1 : act && on && e.kind !== "structure" ? 1.35 : 1), 0.9 / k);
    ctx.setLineDash(st.dash); ctx.stroke(); ctx.setLineDash([]);
    if (st.arrow) {
      const ang = Math.atan2(g.ty - g.cy, g.tx - g.cx), sz = Math.max(8, 6 / k);
      ctx.beginPath(); ctx.moveTo(g.tx, g.ty);
      ctx.lineTo(g.tx - sz * Math.cos(ang - 0.42), g.ty - sz * Math.sin(ang - 0.42));
      ctx.lineTo(g.tx - sz * Math.cos(ang + 0.42), g.ty - sz * Math.sin(ang + 0.42));
      ctx.closePath(); ctx.fillStyle = color; ctx.fill();
    }
    if (e.members.length > 1 && k > 0.45 && on) {
      const [mx, my] = curveAt(g, 0.5);
      const label = String(e.members.length);
      ctx.font = F.tiny(); const w = measure(label, F.tiny()) + 8;
      ctx.beginPath(); rr(mx - w / 2, my - 7, w, 14, 7); ctx.fillStyle = C.card; ctx.fill(); ctx.lineWidth = 1; ctx.strokeStyle = color; ctx.stroke();
      ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(label, mx, my + 0.5);
    }
    ctx.globalAlpha = 1;
  }
  function state(n) {
    if (n.kind !== "item") return n.kind;
    if (n.external) return "ext";
    const b = S.busy.get(n.itemId);
    if (b) return "busy";
    const sc = S.scans.get(n.itemId);
    if (!sc) return n.scannable ? "none" : "na";
    return sc.status === "ok" ? "ok" : sc.status === "unsupported" ? "na" : "err";
  }
  function glyph(type, x, y, color) {
    ctx.save(); ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.3; ctx.beginPath();
    switch (type) {
      case "Notebook": rr(x - 5, y - 6.5, 10, 13, 1.5); ctx.moveTo(x - 2.5, y - 2.5); ctx.lineTo(x + 2.5, y - 2.5); ctx.moveTo(x - 2.5, y + 0.5); ctx.lineTo(x + 2.5, y + 0.5); ctx.moveTo(x - 2.5, y + 3.5); ctx.lineTo(x + 1, y + 3.5); ctx.stroke(); break;
      case "DataPipeline": ctx.arc(x - 5, y, 2.3, 0, 7); ctx.moveTo(x + 7.3, y); ctx.arc(x + 5, y, 2.3, 0, 7); ctx.moveTo(x - 2.5, y); ctx.lineTo(x + 2.7, y); ctx.stroke(); break;
      case "SemanticModel": for (let i = 0; i < 6; i++) { const a = Math.PI / 3 * i + Math.PI / 6; i ? ctx.lineTo(x + Math.cos(a) * 6.5, y + Math.sin(a) * 6.5) : ctx.moveTo(x + Math.cos(a) * 6.5, y + Math.sin(a) * 6.5); } ctx.closePath(); ctx.stroke(); ctx.beginPath(); ctx.arc(x, y, 1.8, 0, 7); ctx.fill(); break;
      case "Report": ctx.rect(x - 5.5, y + 1, 3, 5); ctx.rect(x - 1.5, y - 3, 3, 9); ctx.rect(x + 2.5, y - 6, 3, 12); ctx.fill(); break;
      case "Environment": ctx.arc(x, y, 4, 0, 7); for (let i = 0; i < 6; i++) { const a = Math.PI / 3 * i; ctx.moveTo(x + Math.cos(a) * 4.5, y + Math.sin(a) * 4.5); ctx.lineTo(x + Math.cos(a) * 7, y + Math.sin(a) * 7); } ctx.stroke(); break;
      case "Warehouse": case "SQLEndpoint": ctx.rect(x - 6, y - 6, 12, 4); ctx.rect(x - 6, y - 1, 12, 4); ctx.rect(x - 6, y + 4, 12, 3); ctx.stroke(); break;
      case "SparkJobDefinition": ctx.moveTo(x + 1.5, y - 7); ctx.lineTo(x - 4, y + 1); ctx.lineTo(x, y + 1); ctx.lineTo(x - 1.5, y + 7); ctx.lineTo(x + 4.5, y - 1.5); ctx.lineTo(x + 0.5, y - 1.5); ctx.closePath(); ctx.fill(); break;
      case "Dataflow": ctx.moveTo(x - 7, y - 2); ctx.bezierCurveTo(x - 3, y - 7, x + 1, y + 3, x + 7, y - 2); ctx.moveTo(x - 7, y + 3); ctx.bezierCurveTo(x - 3, y - 2, x + 1, y + 8, x + 7, y + 3); ctx.stroke(); break;
      case "Lakehouse": rr(x - 7, y - 4.5, 14, 9, 4.5); ctx.stroke(); break;
      case "External": ctx.arc(x, y, 6, 0, 7); ctx.moveTo(x - 6, y); ctx.lineTo(x + 6, y); ctx.moveTo(x, y - 6); ctx.bezierCurveTo(x + 4, y - 3, x + 4, y + 3, x, y + 6); ctx.bezierCurveTo(x - 4, y + 3, x - 4, y - 3, x, y - 6); ctx.stroke(); break;
      default: ctx.arc(x, y, 4.5, 0, 7); ctx.stroke();
    }
    ctx.restore();
  }
  function drawNode(n, act, k, t) {
    const dim = act && !act.has(n.id);
    ctx.globalAlpha = dim ? 0.2 : 1;
    const x = n.x - n.w / 2, y = n.y - n.h / 2, st = state(n);
    const selected = isSel(n.id), hovered = S.hover === n.id || (BOXSET !== null && BOXSET.has(n.id));
    const pinned = S.pinned === n.id;
    if (pinned) {
      ctx.beginPath(); rr(x - 9, y - 9, n.w + 18, n.h + 18, n.kind === "group" || (n.type === "Lakehouse" && n.kind === "item") ? (n.h + 18) / 2 : 10);
      ctx.setLineDash([]); ctx.lineWidth = 2.5; ctx.strokeStyle = C.ink; ctx.stroke();
    }
    if (selected || hovered) {
      ctx.beginPath(); rr(x - 5, y - 5, n.w + 10, n.h + 10, n.kind === "group" || (n.type === "Lakehouse" && n.kind === "item") ? (n.h + 10) / 2 : 7);
      ctx.fillStyle = C.halo; ctx.fill();
      if (selected) { ctx.lineWidth = 2; ctx.strokeStyle = C.read; ctx.stroke(); }
    }
    const dashed = st === "none" || st === "busy";
    ctx.setLineDash(dashed ? [5, 4] : st === "ext" || n.kind === "external" ? [2, 3] : n.kind === "missing" ? [4, 3] : []);
    ctx.lineDashOffset = st === "busy" && !REDUCED ? -((t / 45) % 9) : 0;
    if (n.kind === "group") {
      ctx.beginPath(); rr(x, y, n.w, n.h, n.h / 2);
      ctx.fillStyle = C.sheet; ctx.fill(); ctx.setLineDash([]); ctx.lineWidth = 1.2; ctx.strokeStyle = C.ink2; ctx.stroke();
      const open = groupAllOn(n);
      ctx.font = F.group(); ctx.fillStyle = C.ink; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      const label = fitText(n.label, n.w - 46 - measure(groupCount(n), F.group()), F.group());
      if (k >= 0.5) {
        ctx.fillText(label, x + 13, n.y + 0.5);
        ctx.fillStyle = C.muted; ctx.fillText(groupCount(n), x + 19 + measure(label, F.group()), n.y + 0.5);
      }
      ctx.strokeStyle = C.ink2; ctx.lineWidth = 1.4; ctx.beginPath();
      ctx.moveTo(x + n.w - 18, n.y); ctx.lineTo(x + n.w - 10, n.y);
      if (!open) { ctx.moveTo(x + n.w - 14, n.y - 4); ctx.lineTo(x + n.w - 14, n.y + 4); }
      ctx.stroke();
    } else if (n.kind === "item" && n.type === "Lakehouse") {
      const col = lakeColor(n), r = n.h / 2;
      ctx.beginPath(); rr(x, y, n.w, n.h, r);
      ctx.fillStyle = C.card; ctx.fill(); ctx.fillStyle = rgba(col, 0.24); ctx.fill();
      ctx.lineWidth = 1.5; ctx.strokeStyle = st === "err" ? C.error : C.ink; ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.moveTo(x + r * 0.85, y + 3); ctx.lineTo(x + r * 0.85, y + n.h - 3);
      ctx.moveTo(x + n.w - r * 0.85, y + 3); ctx.lineTo(x + n.w - r * 0.85, y + n.h - 3);
      ctx.moveTo(x - 7, n.y); ctx.lineTo(x, n.y); ctx.moveTo(x + n.w, n.y); ctx.lineTo(x + n.w + 7, n.y);
      ctx.strokeStyle = rgba(C.ink, 0.5); ctx.lineWidth = 1; ctx.stroke();
      ctx.font = F.lake(); ctx.fillStyle = C.ink; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      if (k >= 0.5) ctx.fillText(fitText(n.label, n.w - r * 2 - 8, F.lake()), n.x, n.y + 0.5);
      drawToggle(n);
    } else if (n.kind === "schema") {
      const col = lakeColor(S.nodes.get(n.parent));
      ctx.beginPath(); rr(x, y, n.w, n.h, 4);
      ctx.fillStyle = C.card; ctx.fill(); ctx.fillStyle = rgba(col, 0.22); ctx.fill();
      ctx.setLineDash([]); ctx.lineWidth = 1.2; ctx.strokeStyle = col; ctx.stroke();
      ctx.font = `700 11px ${FONT}`; ctx.fillStyle = col; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      if (n.sec === "files" || n.sec === "t:Tables") {
        ctx.beginPath(); ctx.moveTo(x + 7, y + 7); ctx.lineTo(x + 11, y + 7); ctx.lineTo(x + 12.5, y + 8.5); ctx.lineTo(x + 19, y + 8.5);
        ctx.lineTo(x + 19, y + 17); ctx.lineTo(x + 7, y + 17); ctx.closePath(); ctx.strokeStyle = col; ctx.lineWidth = 1.2; ctx.stroke();
      } else ctx.fillText("{ }", x + 7, n.y + 0.5);
      if (k >= 0.42) { ctx.font = F.small(); ctx.fillStyle = C.ink; ctx.fillText(fitText(n.label, n.w - 48, F.small()), x + 27, n.y + 0.5); }
      drawToggle(n);
    } else if (n.kind === "table" || n.kind === "file") {
      const col = colorOf(n);
      const parentRead = S.scans.has((S.nodes.get(n.parent) || {}).itemId || "");
      ctx.beginPath(); rr(x, y, n.w, n.h, 3);
      ctx.fillStyle = C.card; ctx.fill(); ctx.fillStyle = rgba(col, n.kind === "file" ? 0.07 : 0.13); ctx.fill();
      ctx.setLineDash(n.kind === "table" && parentRead && !n.listed ? [4, 3] : []);
      ctx.lineWidth = 1.1; ctx.strokeStyle = col; ctx.stroke(); ctx.setLineDash([]);
      ctx.strokeStyle = col; ctx.lineWidth = 1.1; ctx.beginPath();
      if (n.kind === "table") { ctx.rect(x + 7, y + 7, 10, 10); ctx.moveTo(x + 7, y + 10.5); ctx.lineTo(x + 17, y + 10.5); ctx.moveTo(x + 7, y + 14); ctx.lineTo(x + 17, y + 14); ctx.moveTo(x + 11, y + 10.5); ctx.lineTo(x + 11, y + 17); }
      else { ctx.moveTo(x + 7, y + 8); ctx.lineTo(x + 11, y + 8); ctx.lineTo(x + 12.5, y + 9.5); ctx.lineTo(x + 18, y + 9.5); ctx.lineTo(x + 18, y + 17); ctx.lineTo(x + 7, y + 17); ctx.closePath(); }
      ctx.stroke();
      if (k >= 0.42) {
        ctx.font = F.small(); ctx.fillStyle = C.ink; ctx.textAlign = "left"; ctx.textBaseline = "middle";
        const underFolder = S.layout === "struct" || (n.schemaNode && V.vis.has(n.schemaNode));
        const shown = underFolder && n.kind === "file" ? n.label.replace(/^Files\//, "") : underFolder && n.kind === "table" && n.schema ? n.table : n.label;
        ctx.fillText(fitText(shown, n.w - 30, F.small()), x + 23, (n._cols ? y + 12 : n.y) + 0.5);
        if (n._cols) drawCols(n, x, y, col);
      }
    } else {
      ctx.beginPath(); rr(x, y, n.w, n.h, 4);
      ctx.fillStyle = C.card; ctx.fill();
      ctx.lineWidth = 1.25;
      ctx.strokeStyle = st === "err" || n.kind === "missing" ? C.error : st === "na" ? C.muted : C.ink;
      ctx.stroke(); ctx.setLineDash([]);
      const gtype = n.kind === "external" ? "External" : n.type;
      glyph(gtype, x + 15, n.y, n.kind === "missing" ? C.error : colorOf(n));
      ctx.font = F.item(); ctx.fillStyle = st === "na" ? C.ink2 : C.ink; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      if (k < 0.42) { /* the far label carries the name */ } else if (twoLine(n)) {
        ctx.fillText(fitText(n.label, n.w - 38, F.item()), x + 28, n.y - 6);
        ctx.font = F.tiny(); ctx.fillStyle = n.kind === "missing" ? C.error : C.muted;
        ctx.fillText(fitText(subLine(n), n.w - 38, F.tiny()), x + 28, n.y + 8);
      } else ctx.fillText(fitText(n.label, n.w - 38, F.item()), x + 28, n.y + 0.5);
      if (st === "err") {
        ctx.beginPath(); ctx.arc(x + n.w - 2, y + 2, 7, 0, 7); ctx.fillStyle = C.error; ctx.fill();
        ctx.fillStyle = C.card; ctx.font = `700 10px ${FONT}`; ctx.textAlign = "center"; ctx.fillText("!", x + n.w - 2, y + 2.5);
      }
    }
    if (pinned) drawPin(x - 7, y - 7);
    ctx.setLineDash([]); ctx.lineDashOffset = 0; ctx.globalAlpha = 1;
  }
  function badgeNode() {
    const id = S.hover || S.sel, n = id && S.nodes.get(id);
    return n && n.url && V.vis.has(n.id) && n.kind !== "group" ? n : null;
  }
  function badgeRect(n, k) { const s = 20 / k; return {x: n.x + n.w / 2 - s * 0.45, y: n.y - n.h / 2 - s * 0.6, s}; }
  function drawBadge(k) {
    const n = badgeNode();
    if (!n) return;
    const b = badgeRect(n, k), p = b.s / 20;
    ctx.beginPath(); rr(b.x, b.y, b.s, b.s, 4 * p); ctx.fillStyle = C.ink; ctx.fill();
    ctx.strokeStyle = C.card; ctx.lineWidth = 1.5 * p; ctx.beginPath();
    ctx.moveTo(b.x + 11 * p, b.y + 5 * p); ctx.lineTo(b.x + 15 * p, b.y + 5 * p); ctx.lineTo(b.x + 15 * p, b.y + 9 * p);
    ctx.moveTo(b.x + 15 * p, b.y + 5 * p); ctx.lineTo(b.x + 9 * p, b.y + 11 * p);
    ctx.moveTo(b.x + 13 * p, b.y + 11.5 * p); ctx.lineTo(b.x + 13 * p, b.y + 15 * p); ctx.lineTo(b.x + 5 * p, b.y + 15 * p); ctx.lineTo(b.x + 5 * p, b.y + 7 * p); ctx.lineTo(b.x + 8.5 * p, b.y + 7 * p);
    ctx.stroke();
  }

  // ============================================================ view control
  // Fit everything, or a set of nodes. A set too wide to read when fitted (a long lineage) is shown
  // at a readable zoom, centred on the selected item, so the rest of the chain can be followed by panning.
  function fit(animate = true, ids = null) {
    const list = ids ? V.nodes.filter(n => ids.has(n.id)) : V.nodes;
    if (!list.length || !W) return;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const n of list) { x0 = Math.min(x0, n.x - n.w / 2); x1 = Math.max(x1, n.x + n.w / 2); y0 = Math.min(y0, n.y - n.h / 2); y1 = Math.max(y1, n.y + n.h / 2); }
    const k = clamp(Math.min((W - 80) / (x1 - x0 + 1), (H - 150) / (y1 - y0 + 1)), 0.15, 1.3);
    const sel = ids && S.sel && S.nodes.get(S.sel);
    if (ids && k < 0.6 && sel && Number.isFinite(sel.x)) {
      goTo({k: 0.6, x: W / 2 - sel.x * 0.6, y: (H - 40) / 2 - (y0 + y1) / 2 * 0.6}, animate);
      return;
    }
    goTo({k, x: W / 2 - (x0 + x1) / 2 * k, y: (H - 40) / 2 - (y0 + y1) / 2 * k}, animate);
  }
  function goTo(target, animate = true) {
    if (!animate || REDUCED) { S.view = target; requestDraw(); return; }
    const from = {...S.view}, t0 = performance.now(), dur = 280;
    const step = now => {
      const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      S.view = {x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e};
      draw(); if (p < 1) requestAnimationFrame(step); else requestDraw();
    };
    requestAnimationFrame(step);
  }
  function centerOn(n) {
    if (!n || !Number.isFinite(n.x)) return;
    const k = Math.max(S.view.k, 0.75);
    goTo({k, x: W / 2 - n.x * k, y: H / 2 - n.y * k});
  }
  function reveal(id) {
    const n = S.nodes.get(id);
    if (!n) { toast("That item isn't in this workspace."); return; }
    S.hidden.delete(id);
    if (n.kind === "table" || n.kind === "file") { S.shown.add(id); if (n.parent) { S.shown.add(n.parent); S.hidden.delete(n.parent); } if (!S.opts.tables) setOpt("tables", true); }
    else if (n.kind === "group") { S.expanded.add(id); }
    else S.shown.add(id);
    rebuildView();
    select(id);
    setTimeout(() => centerOn(S.nodes.get(id)), 60);
  }

  // ============================================================ selection and lineage
  function select(id, scan = true) {
    S.sel = id; S.selEdge = null; S.multi = new Set(id ? [id] : []);
    computeActive();
    const n = id && S.nodes.get(id);
    if (n && n.kind === "table") S.colTarget = id;
    if (scan && n && n.kind === "item" && !n.external && n.scannable && !S.scans.has(n.itemId) && !S.busy.has(n.itemId)) enqueue(n.itemId);
    renderDetails(); renderTree(); renderColumns(); requestDraw(); refocus();
  }
  function selectEdge(e) { S.selEdge = e.id; S.sel = null; S.multi = new Set(); computeActive(); renderDetails(); renderTree(); requestDraw(); refocus(); }
  // Structured view: when the highlighted chain changes, line it up (it stays lined up after the
  // selection is cleared, until something else is highlighted or Re-layout tidies up)
  function refocus() {
    if (S.layout !== "struct" || !S.active || S.active.size < 2) return;
    const sig = [...S.active].sort().join("|");
    if (sig === S.focusSig) return;
    S.focusSig = sig; S.focusIds = new Set(S.active);
    layoutStructured(true, S.sel && S.active.has(S.sel) ? S.sel : null);
  }
  function selectMany(ids, add = false) {
    const set = add ? new Set(S.multi) : new Set();
    for (const id of ids) set.add(id);
    if (set.size <= 1) { select(set.size ? [...set][0] : null, false); return; }
    S.multi = set; S.sel = null; S.selEdge = null;
    computeActive(); renderDetails(); renderTree(); requestDraw(); refocus();
  }
  function toggleInSelection(id) {
    const set = new Set(S.multi);
    if (set.has(id)) set.delete(id); else set.add(id);
    selectMany([...set]);
  }
  function clearSelection() { S.sel = null; S.selEdge = null; S.multi = new Set(); computeActive(); renderDetails(); renderTree(); requestDraw(); }
  const isSel = id => S.sel === id || S.multi.has(id);
  function computeActive() {
    if (S.pinned) {                                           // a pinned highlight stays, whatever is selected
      if (!S.nodes.has(S.pinned)) S.pinned = null;
      else { S.active = activeFor(S.nodes.get(S.pinned)); return; }
    }
    if (S.selEdge) { const e = V.edges.find(x => x.id === S.selEdge); S.active = e ? new Set([e.source, e.target]) : null; return; }
    if (S.multi.size > 1) {                                  // several selected: what they share, upstream and downstream
      const start = [];
      for (const id of S.multi) {
        if (!V.vis.has(id)) continue;
        start.push(id);
        const n = S.nodes.get(id);
        if (n && ((n.kind === "item" && n.type === "Lakehouse") || n.kind === "missing" || n.kind === "external" || n.external))
          for (const m of V.nodes) if (m.parent === id) start.push(m.id);
      }
      S.active = start.length ? lineage(start, S.hl === "full") : null;
      return;
    }
    S.active = activeFor(S.sel && S.nodes.get(S.sel));
  }
  // what one node lights up: a dataset its thread (in the chosen direction), anything else its lineage
  function activeFor(n) {
    if (!n || !V.vis.has(n.id)) return null;
    if (isTable(n)) {
      const a = thread(n.id, S.threadDir, S.hl === "full");
      return a.size <= 2 ? null : a;                         // only itself (and its lakehouse): nothing to light up
    }
    let start = [n.id];
    if (n.kind === "group") start = [n.id, ...n.members.filter(m => V.vis.has(m))];
    else if (n.kind === "schema") start = [n.id, ...V.nodes.filter(m => m.schemaNode === n.id).map(m => m.id)];
    else if ((n.kind === "item" && n.type === "Lakehouse") || n.kind === "missing" || n.kind === "external" || n.external)
      start = [n.id, ...V.nodes.filter(m => m.parent === n.id).map(m => m.id)];
    const a = lineage(start, S.hl === "full");
    return a.size <= start.length && n.kind !== "group" ? null : a;   // nothing linked yet: don't dim the map
  }
  // Pin: keep this node's lineage highlighted while other items are clicked and selected
  function pinHighlight(id) {
    if (!S.nodes.has(id)) return;
    S.pinned = id;
    select(id, false);
    renderTitle();
  }
  function unpinHighlight() {
    if (!S.pinned) return;
    S.pinned = null;
    computeActive(); renderDetails(); renderTree(); renderTitle(); requestDraw(); refocus();
  }
  // The thread of a dataset. Inputs: the items that write it, everything those read and write, then
  // upstream from what they read. Outputs: the items that read it, everything those read and write,
  // then downstream from what they write. Both: the two together. Only the items right next to the
  // dataset get their full context; further along only the chosen direction is followed.
  function thread(id, dir, full, G = V) {
    const set = new Set([id]);
    const ins = x => (G.inn.get(x) || []).filter(e => LINEAGE.has(e.kind)).map(e => e.source);
    const outs = x => (G.out.get(x) || []).filter(e => LINEAGE.has(e.kind)).map(e => e.target);
    const walk = (start, next) => {
      const seen = new Set(start), stack = [...start];
      while (stack.length) for (const y of next(stack.pop())) if (!seen.has(y)) { seen.add(y); set.add(y); stack.push(y); }
    };
    const side = (near, onward, next) => {
      for (const p of near) {
        set.add(p);
        if (!full) continue;
        for (const x of ins(p)) set.add(x);
        for (const x of outs(p)) set.add(x);
      }
      if (full) walk(near.flatMap(onward), next);
    };
    if (dir !== "out") side(ins(id), ins, ins);       // where it comes from, and on upstream
    if (dir !== "in") side(outs(id), outs, outs);     // where it goes, and on downstream
    return finishActive(set, [id], G);
  }
  // what is lit with the lineage: direct settings links of the start, lakehouses and folders of lit tables
  function finishActive(set, start, G = V) {
    for (const id of start) for (const e of G.adj.get(id) || []) if (!LINEAGE.has(e.kind) && e.kind !== "structure") set.add(e.source === id ? e.target : e.source);
    for (const id of [...set]) {
      const m = S.nodes.get(id);
      if (m && m.parent && (G !== V || V.vis.has(m.parent))) set.add(m.parent);
      if (m && m.schemaNode && V.vis.has(m.schemaNode)) set.add(m.schemaNode);
    }
    return set;
  }
  function setThreadDir(dir) {
    S.threadDir = dir; store.set("threadDir", dir);
    const n = S.sel && S.nodes.get(S.sel);
    if (n && isTable(n) && !S.focusMode) { revealLineage(n.id); return; }
    computeActive(); renderDetails(); renderTree(); requestDraw(); refocus();
  }
  function lineage(start, full, G = V) {
    const set = new Set(start);
    for (const dir of ["down", "up"]) {
      const seen = new Set(start), stack = [...start];
      while (stack.length) {
        const id = stack.pop();
        for (const e of (dir === "down" ? G.out.get(id) : G.inn.get(id)) || []) {
          if (!LINEAGE.has(e.kind)) continue;
          const nb = dir === "down" ? e.target : e.source;
          set.add(nb);
          if (full && !seen.has(nb)) { seen.add(nb); stack.push(nb); }
        }
      }
    }
    return finishActive(set, start, G);
  }
  // every relation the app knows, also between items that aren't on the map
  function fullGraph() {
    const out = new Map(), inn = new Map(), adj = new Map();
    const push = (m, k, e) => { let l = m.get(k); if (!l) m.set(k, l = []); l.push(e); };
    for (const e of S.edges.values()) { push(out, e.source, e); push(inn, e.target, e); push(adj, e.source, e); push(adj, e.target, e); }
    return {out, inn, adj};
  }
  // Show all relations of an item or dataset: its lineage (a dataset's thread, in the chosen
  // direction) is worked out on everything known and put on the map, ticking those items, then
  // highlighted. Focus mode keeps to what is focused.
  function revealLineage(id) {
    const n = S.nodes.get(id);
    if (!n) return;
    if (S.pinned && S.pinned !== id) S.pinned = id;
    if (S.focusMode) { select(id); return; }
    const G = fullGraph(), full = S.hl === "full";
    let set;
    if (isTable(n)) set = thread(id, S.threadDir, full, G);
    else {
      let start = [id];
      if (n.kind === "item" && n.type === "Lakehouse") start = [id, ...[...S.nodes.values()].filter(m => m.parent === id && isTable(m) && G.adj.has(m.id)).map(m => m.id)];
      set = lineage(start, full, G);
    }
    let tables = false;
    for (const x of set) {
      const m = S.nodes.get(x);
      if (!m || m.kind === "group" || m.kind === "schema") continue;
      onMap(x, true);
      if (m.parent && S.nodes.has(m.parent)) onMap(m.parent, true);
      if (isTable(m)) tables = true;
    }
    if (tables) tablesOn();
    afterVisibility();
    select(id);
  }

  // ============================================================ reading items (lazy, cached by the app)
  function enqueue(itemId, refresh = false) {
    if (S.busy.has(itemId)) return;
    S.busy.set(itemId, "queued");
    S.queue.push({itemId, refresh});
    S.batch.total++;
    pump(); renderTreeSoon(); renderTitle(); requestDraw();
  }
  function pump() {
    while (S.running < S.concurrency && S.queue.length) {
      const job = S.queue.shift();
      S.running++; S.busy.set(job.itemId, "reading");
      if (S.sel === "item:" + job.itemId) renderDetails();
      requestDraw();
      runScan(job).finally(() => {
        S.running--; S.batch.done++;
        if (!S.queue.length && !S.running) S.batch = {total: 0, done: 0};
        pump(); renderTitle(); updateActivity();
      });
    }
    updateActivity();
  }
  async function runScan(job) {
    const wsId = S.ws && S.ws.id;
    try {
      const res = await api("/api/scan", {workspaceId: wsId, itemId: job.itemId, refresh: job.refresh});
      if (!S.ws || S.ws.id !== wsId) return;
      S.busy.delete(job.itemId);
      applyScan(res, S.quietReads.delete(job.itemId) ? "silent" : false);
      const pid = "item:" + job.itemId;
      if (S.pendingExpand.has(pid)) { const fo = S.pendingExpand.get(pid); S.pendingExpand.delete(pid); setTimeout(() => expandLake(pid, fo), 0); }
      for (const extra of res.alsoScanned || []) {
        if (S.scans.has(extra)) continue;
        try { applyScan(await api("/api/scan", {workspaceId: wsId, itemId: extra})); } catch { /* shown when selected */ }
      }
    } catch (err) {
      S.busy.delete(job.itemId);
      S.pendingExpand.delete("item:" + job.itemId);
      applyScan({itemId: job.itemId, status: "error", error: err.message, hint: err.hint || "", nodes: [], edges: [], unresolved: [], details: {}, summary: ""});
    } finally {
      S.busy.delete(job.itemId);
      if (S.ws && S.ws.id === wsId) {
        rebuildView();
        renderDetails();
        renderTreeSoon(); renderTitle(); refreshLog();
      }
    }
  }
  function readAll(ids, refresh = false) {
    let n = 0;
    for (const itemId of ids) { if (refresh || !S.scans.has(itemId)) { enqueue(itemId, refresh); n++; } }
    if (!n) toast("Those have all been read already.");
  }
  function stopReading() {
    for (const job of S.queue) S.busy.delete(job.itemId);
    S.batch.total -= S.queue.length; S.queue = [];
    renderTreeSoon(); renderTitle(); updateActivity(); requestDraw();
  }

  // ============================================================ interaction
  function toWorld(ev) {
    const r = canvas.getBoundingClientRect(), sx = ev.clientX - r.left, sy = ev.clientY - r.top;
    return {sx, sy, x: (sx - S.view.x) / S.view.k, y: (sy - S.view.y) / S.view.k};
  }
  function hitNode(p) {
    for (let i = V.nodes.length - 1; i >= 0; i--) {
      const n = V.nodes[i];
      if (Math.abs(p.x - n.x) <= n.w / 2 + 3 && Math.abs(p.y - n.y) <= n.h / 2 + 3) return n;
    }
    return null;
  }
  function hitBadge(p) {
    const n = badgeNode();
    if (!n) return null;
    const b = badgeRect(n, S.view.k);
    return p.x >= b.x && p.x <= b.x + b.s && p.y >= b.y && p.y <= b.y + b.s ? n : null;
  }
  function hitEdge(p) {
    const tol = 6 / S.view.k;
    let best = null, bestD = tol;
    for (const e of V.edges) {
      if (e.kind === "structure") continue;
      const s = S.nodes.get(e.source), t = S.nodes.get(e.target), g = geom(e, s, t);
      for (let i = 1; i < 16; i++) {
        const [qx, qy] = curveAt(g, i / 16), d = Math.hypot(qx - p.x, qy - p.y);
        if (d < bestD) { bestD = d; best = e; }
      }
    }
    return best;
  }
  function openUrl(n) { if (n && n.url) window.open(n.url, "_blank", "noopener"); }

  canvas.addEventListener("contextmenu", ev => ev.preventDefault());
  // What moves when a node is dragged: the whole selection when the node is part of it, otherwise
  // the node. In the structured view a lakehouse brings its column (tables, schema headings).
  function dragSet(n) {
    const set = new Set((S.multi.has(n.id) && S.multi.size > 1 ? [...S.multi] : [n.id]).filter(id => V.vis.has(id)));
    const followers = new Set();
    if (S.layout === "struct") for (const id of [...set]) {
      const lane = LANES.find(l => l.id === id);
      if (!lane) continue;
      for (const r of [...lane.rows, ...lane.sections.filter(sc => sc.node).map(sc => sc.node)])
        if (V.vis.has(r)) { followers.add(r); set.delete(r); }
    }
    const pick = id => { const m = S.nodes.get(id); return {m, ox: m.x, oy: m.y}; };
    return {movers: [...set].map(pick), followers: [...followers].filter(id => !set.has(id)).map(pick)};
  }
  function nodesInBox(pr) {
    const k = S.view.k, x0 = (Math.min(pr.x0, pr.x1) - S.view.x) / k, x1 = (Math.max(pr.x0, pr.x1) - S.view.x) / k;
    const y0 = (Math.min(pr.y0, pr.y1) - S.view.y) / k, y1 = (Math.max(pr.y0, pr.y1) - S.view.y) / k;
    return V.nodes.filter(n => n.x >= x0 && n.x <= x1 && n.y >= y0 && n.y <= y1).map(n => n.id);
  }
  canvas.addEventListener("pointerdown", ev => {
    const panButton = ev.button === 1 || ev.button === 2 || (ev.button === 0 && S.space);
    if (ev.button !== 0 && !panButton) return;
    canvas.focus({preventScroll: true});
    canvas.setPointerCapture(ev.pointerId);
    const p = toWorld(ev);
    if (panButton) { S.press = {type: "pan", sx: ev.clientX, sy: ev.clientY, vx: S.view.x, vy: S.view.y, moved: false, quiet: true, rnode: ev.button === 2 ? hitNode(p) : null}; return; }
    const badge = hitBadge(p);
    if (badge) { S.press = {type: "badge", node: badge}; return; }
    const n = hitNode(p);
    const tg = n && toggleOf(n);
    if (tg && Math.hypot(p.x - tg.x, p.y - tg.y) <= tg.r + 3) { S.press = {type: "toggle", node: n}; return; }
    if (n) { S.press = {type: "node", node: n, sx: ev.clientX, sy: ev.clientY, moved: false, ctrl: ev.ctrlKey || ev.metaKey, shift: ev.shiftKey}; return; }
    const edge = hitEdge(p);
    if (ev.shiftKey)
      S.press = {type: "box", x0: p.sx, y0: p.sy, x1: p.sx, y1: p.sy, add: ev.ctrlKey || ev.metaKey, edge, moved: false};
    else S.press = {type: "pan", edge, sx: ev.clientX, sy: ev.clientY, vx: S.view.x, vy: S.view.y, moved: false};
  });
  canvas.addEventListener("pointermove", ev => {
    const pr = S.press, p = toWorld(ev);
    canvas.classList.toggle("boxmode", ev.shiftKey);
    if (pr && pr.type === "node") {
      const dx = ev.clientX - pr.sx, dy = ev.clientY - pr.sy;
      if (!pr.moved && Math.hypot(dx, dy) < 4) return;
      if (!pr.moved) { Object.assign(pr, dragSet(pr.node)); S.tween = null; }
      pr.moved = true; canvas.classList.add("dragging");
      const kx = dx / S.view.k, ky = dy / S.view.k;
      for (const g of [...pr.movers, ...pr.followers]) {
        g.m.x = g.ox + kx; g.m.y = g.oy + ky; g.m.vx = g.m.vy = 0;
        if (S.layout !== "struct") g.m.fixed = true;
      }
      if (S.layout === "struct") requestDraw(); else reheat(0.12);
      hideTip(); return;
    }
    if (pr && pr.type === "box") {
      pr.x1 = p.sx; pr.y1 = p.sy;
      if (!pr.moved && Math.hypot(pr.x1 - pr.x0, pr.y1 - pr.y0) < 4) return;
      pr.moved = true; BOXSET = new Set(nodesInBox(pr)); requestDraw(); hideTip(); return;
    }
    if (pr && pr.type === "pan") {
      const dx = ev.clientX - pr.sx, dy = ev.clientY - pr.sy;
      if (!pr.moved && Math.hypot(dx, dy) < 4) return;
      pr.moved = true; canvas.classList.add("dragging");
      S.view.x = pr.vx + dx; S.view.y = pr.vy + dy; requestDraw(); hideTip(); return;
    }
    const n = hitNode(p), e = n ? null : hitEdge(p), b = hitBadge(p);
    const hid = n ? n.id : (b ? b.id : null);
    if (hid !== S.hover || (e && e.id) !== S.hoverEdge) { S.hover = hid; S.hoverEdge = e ? e.id : null; markHover(); requestDraw(); }
    canvas.classList.toggle("pointing", !!(n || e || b));
    if (b) showTip(ev, `<strong>Open in Fabric</strong><span>${esc(b.label)}</span>`);
    else if (n) {
      const tg = toggleOf(n);
      if (tg && Math.hypot(p.x - tg.x, p.y - tg.y) <= tg.r + 3)
        showTip(ev, tg.open ? "<strong>Collapse</strong><span>Take its datasets off the map</span>" : "<strong>Expand</strong><span>Show its datasets on the map</span>");
      else showTip(ev, tipFor(n));
    }
    else if (e) showTip(ev, tipForEdge(e));
    else hideTip();
  });
  const endPress = ev => {
    const pr = S.press; S.press = null; BOXSET = null; canvas.classList.remove("dragging");
    if (!pr) return;
    if (pr.type === "badge") { openUrl(pr.node); return; }
    if (pr.type === "toggle") { toggleNode(pr.node); return; }
    if (pr.type === "box") {
      if (pr.moved) { selectMany(nodesInBox(pr), pr.add); return; }
      if (pr.edge) { selectEdge(pr.edge); return; }
      requestDraw(); return;
    }
    if (pr.moved) {
      if (pr.type === "node") {
        if (S.layout === "struct") {                 // remember the move relative to the tidy arrangement
          const dx = (ev.clientX - pr.sx) / S.view.k, dy = (ev.clientY - pr.sy) / S.view.k;
          for (const g of pr.movers) { const o = S.offsets.get(g.m.id) || {dx: 0, dy: 0}; S.offsets.set(g.m.id, {dx: o.dx + dx, dy: o.dy + dy}); }
          requestDraw();
        } else reheat(0.1);
      }
      return;
    }
    if (pr.quiet) { if (pr.rnode) openMenu(pr.rnode, ev.clientX, ev.clientY); return; }
    if (pr.type === "node") {
      const n = pr.node;
      if (pr.ctrl) { openUrl(n); return; }
      if (pr.shift) { toggleInSelection(n.id); return; }
      if (n.kind === "group") { toggleGroupOnMap(n.id); select(n.id, false); return; }
      select(n.id);
      return;
    }
    if (pr.edge) { selectEdge(pr.edge); return; }
    if (ev.type === "pointerup") clearSelection();
  };
  canvas.addEventListener("pointerup", endPress);
  canvas.addEventListener("pointercancel", () => { S.press = null; BOXSET = null; canvas.classList.remove("dragging"); requestDraw(); });
  canvas.addEventListener("pointerleave", () => { if (!S.press) { S.hover = null; S.hoverEdge = null; hideTip(); markHover(); requestDraw(); } });
  canvas.addEventListener("dblclick", ev => {
    const n = hitNode(toWorld(ev));
    if (n && S.active) fit(true, S.active);
  });
  canvas.addEventListener("wheel", ev => {
    ev.preventDefault(); closeMenu();
    const p = toWorld(ev), f = Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0016));
    const k = clamp(S.view.k * f, 0.12, 3);
    S.view = {k, x: p.sx - p.x * k, y: p.sy - p.y * k};
    requestDraw();
  }, {passive: false});
  canvas.addEventListener("keydown", ev => {
    if (ev.key === "+" || ev.key === "=") zoomBy(1.2);
    else if (ev.key === "-") zoomBy(1 / 1.2);
  });
  function zoomBy(f) { const k = clamp(S.view.k * f, 0.12, 3); S.view = {k, x: W / 2 - (W / 2 - S.view.x) / S.view.k * k, y: H / 2 - (H / 2 - S.view.y) / S.view.k * k}; requestDraw(); }

  const tip = $("#tip");
  function showTip(ev, html) {
    const r = $("#stage").getBoundingClientRect();
    tip.innerHTML = html; tip.hidden = false;
    const x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 16;
    tip.style.left = Math.min(x, r.width - tip.offsetWidth - 8) + "px";
    tip.style.top = Math.min(y, r.height - tip.offsetHeight - 8) + "px";
  }
  function hideTip() { tip.hidden = true; }
  function tipFor(n) {
    if (n.kind === "group") return `<strong>${esc(n.label)}</strong><span>${n.members.length} items. Click to ${groupAllOn(n) ? "take them off the map" : "put them all on the map"}.</span>`;
    if (n.kind === "table" || n.kind === "file") return `<strong>${esc(n.fullName || n.label)}</strong><span>${n.kind === "table" ? "Table" : "Folder"}. Click to see what writes and reads it.</span>`;
    if (n.kind === "missing" || n.kind === "external") return `<strong>${esc(n.label)}</strong><span>${esc(n.note || "")}</span>`;
    const st = state(n), sc = S.scans.get(n.itemId);
    const what = st === "none" ? "Not read yet. Click to read it." : st === "busy" ? "Reading…" : st === "err" ? "Couldn't be read. Click for the reason." : sc ? esc(sc.summary || "") : "";
    return `<strong>${esc(n.label)}</strong><span>${esc(typeName(n.type))}${n.external ? ` in ${esc(n.workspaceName)}` : ""}. ${what}<br>Ctrl+click opens it in Fabric.</span>`;
  }
  function linkSentence(kind, s, t) {
    const a = s.label || "?", b = t.label || "?";
    return kind === "read" ? `${b} reads ${a}` : kind === "model_source" ? `${b} uses ${a}` : kind === "report_model" ? `${b} is built on ${a}`
      : kind === "shortcut" ? `${b} is a shortcut to ${a}` : `${a} ${EDGE[kind] ? EDGE[kind].verb : "links to"} ${b}`;
  }
  function tipForEdge(e) {
    const s = S.nodes.get(e.source), t = S.nodes.get(e.target);
    const ev = e.members.flatMap(m => m.evidence || []);
    return `<strong>${esc(linkSentence(e.kind, s, t))}</strong><span>${ev.length ? esc(ev[0].where) + (ev.length > 1 ? ` and ${ev.length - 1} more` : "") : ""}. Click for details.</span>`;
  }

  // ============================================================ map controls
  // the group's items that a tickbox counts (SQL endpoints only when grouping by folder)
  const groupItems = g => g.members.filter(m => { const n = S.nodes.get(m); return n && (n.type !== "SQLEndpoint" || S.groupMode === "folder"); });
  const groupAllOn = g => { const list = groupItems(g); return list.length > 0 && list.every(m => V.vis.has(m)); };
  function toggleGroupOnMap(gid) { const g = S.nodes.get(gid); if (g) setGroupOnMap(gid, !groupAllOn(g)); }
  function onMap(id, on) {
    if (on) { S.hidden.delete(id); S.shown.add(id); } else { S.hidden.add(id); S.shown.delete(id); }
    if (on && S.focusMode) { S.focusMode.add(id); const n = S.nodes.get(id); if (n && n.parent) S.focusMode.add(n.parent); }
  }
  function enterFocus(set) {
    const src = set || S.active;
    if (!src || !src.size) { toast("Select something first: focus mode keeps what is highlighted."); return; }
    S.focusMode = new Set(src);
    for (const id of src) { const n = S.nodes.get(id); if (n && n.parent) S.focusMode.add(n.parent); }
    afterVisibility();
    setTimeout(() => fit(true), S.layout === "struct" ? 480 : 60);   // after the Structured view has rearranged
  }
  function leaveFocus() { if (!S.focusMode) return; S.focusMode = null; afterVisibility(); }
  // the lakehouses (in this workspace) holding the tables and folders an item reads or writes
  function lakesOf(id) {
    const out = new Set();
    for (const e of S.edges.values()) {
      const other = e.source === id ? e.target : e.target === id ? e.source : null;
      const t = other && S.nodes.get(other);
      const p = t && isTable(t) && S.nodes.get(t.parent);
      if (p && p.kind === "item" && !p.external) out.add(p.id);
    }
    return out;
  }
  // putting an item on the map also brings back the lakehouses its tables live in, so its links show
  function showWithLakes(id) {
    onMap(id, true);
    for (const l of lakesOf(id)) onMap(l, true);
    for (const e of S.edges.values()) {                 // and the tables it reads and writes, even if taken off before
      const o = e.source === id ? e.target : e.target === id ? e.source : null;
      const t = o && S.nodes.get(o);
      if (!isTable(t)) continue;
      S.hidden.delete(o);
      if (S.focusMode) { S.focusMode.add(o); if (t.parent) S.focusMode.add(t.parent); }
    }
  }
  function tablesOn() { if (!S.opts.tables) { S.opts.tables = true; store.set("opts", S.opts); const box = document.querySelector('[data-opt="tables"]'); if (box) box.checked = true; } }
  function lakeData(lid) { const lh = S.nodes.get(lid); if (!lh) return []; const {tables, folders} = lakeLists(lh); return [...tables, ...folders]; }
  function readQuietly(n) { if (n && n.kind === "item" && !n.external && n.scannable && !S.scans.has(n.itemId) && !S.busy.has(n.itemId)) { S.quietReads.add(n.itemId); enqueue(n.itemId); } }
  // forget every choice made under a lakehouse: Folders, Contents, folder names, single datasets
  function resetLake(lid) {
    S.contents.delete(lid); S.folderMode.delete(lid);
    for (const k of [...S.folderVis.keys()]) if (k.startsWith(`fold:${lid}|`)) S.folderVis.delete(k);
    for (const d of lakeData(lid)) { S.shown.delete(d.id); S.hidden.delete(d.id); }
  }
  // The lakehouse's own tickbox overrides everything under it. On: the lakehouse, Folders, Contents,
  // every folder and every dataset. Off: all of it off. A very big lakehouse asks first.
  function cascadeLake(lid, on) {
    const lh = S.nodes.get(lid);
    if (!lh) return;
    const data = lakeData(lid);
    if (!on) { resetLake(lid); onMap(lid, false); afterVisibility(); return; }
    if (data.length > S.bigCascade && !window.confirm(`Put all ${data.length} tables and folders of ${lh.label} on the map?\n\nCancel puts only ${lh.label} itself on the map.`)) {
      onMap(lid, true); afterVisibility(); return;
    }
    resetLake(lid);
    onMap(lid, true); S.contents.add(lid); S.folderMode.add(lid);
    for (const d of data) onMap(d.id, true);
    for (const k of new Set(data.map(secKeyOf))) S.folderVis.set(folderId(lid, k), true);
    tablesOn(); readQuietly(lh);
    afterVisibility();
  }
  function afterVisibility() { rebuildView(); renderTree(); renderDetails(); renderTitle(); renderColumns(); }
  // tables and folders: ticking one puts it (and its lakehouse) on the map, unticking takes it off
  function setDataOnMap(ids, on) {
    for (const id of ids) {
      const n = S.nodes.get(id);
      if (!n) continue;
      onMap(id, on);
      if (on && n.parent && S.nodes.has(n.parent)) onMap(n.parent, true);
    }
    if (on && !S.opts.tables) { S.opts.tables = true; store.set("opts", S.opts); const box = document.querySelector('[data-opt="tables"]'); if (box) box.checked = true; }
    afterVisibility();
  }
  function setNodeOnMap(id, on) {
    const n = S.nodes.get(id);
    if (!n) return;
    if (n.kind === "schema") { S.folderVis.set(id, on); afterVisibility(); }
    else if (isTable(n)) setDataOnMap([id], on);
    else if (n.kind === "item" && n.type === "Lakehouse" && !n.external) cascadeLake(id, on);
    else setItemOnMap(id, on);
  }
  // ---- + / − on the map: a lakehouse opens into its folders and datasets, a folder into its datasets
  // open: all its datasets are on the map. A lakehouse that hasn't been read never is: its datasets aren't loaded yet.
  const lakeOpen = lid => {
    const lh = S.nodes.get(lid), d = lakeData(lid);
    if (!lh || (!lh.external && lh.scannable && !S.scans.has(lh.itemId))) return false;
    return S.contents.has(lid) || (d.length > 0 && d.every(x => V.vis.has(x.id)));
  };
  function folderIds(fid) { const f = S.nodes.get(fid); return f ? lakeData(f.parent).filter(t => secKeyOf(t) === f.sec).map(t => t.id) : []; }
  const folderOpen = fid => { const ids = folderIds(fid); return ids.length > 0 && ids.every(id => V.vis.has(id)); };
  // + on a lakehouse: read it if needed, then its folders with all their datasets (a very big one
  // asks first; "no" shows just the folders, each with its own +). foldersOnly: just the folders.
  function expandLake(lid, foldersOnly = false) {
    const lh = S.nodes.get(lid);
    if (!lh) return;
    onMap(lid, true);
    if (lh.external) { setDataOnMap(lakeData(lid).map(d => d.id), true); return; }
    if (!S.scans.has(lh.itemId)) {                          // load it first; this continues when the read is done
      S.pendingExpand.set(lid, foldersOnly);
      if (!S.busy.has(lh.itemId) && lh.scannable) { S.quietReads.add(lh.itemId); enqueue(lh.itemId); }
      afterVisibility(); return;
    }
    const data = lakeData(lid);
    S.folderMode.add(lid);
    for (const k of [...S.folderVis.keys()]) if (k.startsWith(`fold:${lid}|`)) S.folderVis.delete(k);
    for (const k of new Set(data.map(secKeyOf))) S.folderVis.set(folderId(lid, k), true);
    if (!foldersOnly && !(data.length > S.bigCascade && !window.confirm(`Show all ${data.length} tables and folders of ${lh.label}?\n\nCancel shows only its folders; use + on a folder to open it.`))) {
      S.contents.add(lid);
      for (const d of data) onMap(d.id, true);
      tablesOn();
    }
    afterVisibility();
  }
  // − on a lakehouse: its folders and every dataset off the map (links then point at the lakehouse)
  function collapseLake(lid) {
    const data = lakeData(lid);
    resetLake(lid);
    for (const d of data) onMap(d.id, false);
    afterVisibility();
  }
  function toggleNode(n) {
    if (n.kind === "schema") { const open = folderOpen(n.id); S.folderVis.set(n.id, true); setDataOnMap(folderIds(n.id), !open); return; }
    if (lakeOpen(n.id)) collapseLake(n.id); else expandLake(n.id);
  }
  // where the + / − sits on a node, in map coordinates (only when readable)
  function toggleOf(n) {
    if (S.view.k < 0.42 || !V.vis.has(n.id)) return null;
    if (n.kind === "item" && n.type === "Lakehouse") return {x: n.x + n.w / 2 - n.h * 0.21, y: n.y, r: 7.5, open: lakeOpen(n.id)};
    if (n.kind === "schema") return {x: n.x + n.w / 2 - 11, y: n.y, r: 6.5, open: folderOpen(n.id)};
    return null;
  }
  // a table's columns under its name: used by a model = dot and bold, hidden in the model = italic
  function drawCols(n, x, y, col) {
    ctx.save(); ctx.setLineDash([]);
    ctx.strokeStyle = rgba(col, 0.5); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x + 4, y + 24.5); ctx.lineTo(x + n.w - 4, y + 24.5); ctx.stroke();
    ctx.textBaseline = "middle";
    const list = n._cols, loaded = S.colCache.has(colKey(n)), rowY = i => y + 30 + i * 15 + 7;
    if (!loaded || !list.length) {
      ctx.font = F.tiny(); ctx.fillStyle = C.muted; ctx.textAlign = "left";
      ctx.fillText(!loaded ? "Reading its columns…" : S.colsOnMap.get(n.id) === "used" ? "No column shown by a model read so far" : "No columns found", x + 10, rowY(0));
    }
    list.slice(0, COLS_MAX).forEach((c, i) => {
      const ry = rowY(i), font = `${c.hidden ? "italic " : ""}${c.used ? 600 : 400} 11px ${FONT}`;
      if (c.used) { ctx.beginPath(); ctx.arc(x + 8, ry, 2.4, 0, 7); ctx.fillStyle = C.serve; ctx.fill(); }
      ctx.textAlign = "left"; ctx.font = font; ctx.fillStyle = c.hidden ? C.muted : C.ink;
      ctx.fillText(fitText(c.name, n.w - measure(c.type, F.tiny()) - 34, font), x + 15, ry);
      ctx.textAlign = "right"; ctx.font = F.tiny(); ctx.fillStyle = C.muted; ctx.fillText(c.type, x + n.w - 8, ry);
    });
    if (list.length > COLS_MAX) { ctx.textAlign = "left"; ctx.font = F.tiny(); ctx.fillStyle = C.muted; ctx.fillText(`+ ${list.length - COLS_MAX} more in the Table content tab`, x + 15, rowY(COLS_MAX)); }
    ctx.restore();
  }
  // the pin badge of the pinned highlight: a dark disc with a pin
  function drawPin(cx, cy) {
    ctx.save(); ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(cx, cy, 10, 0, 7); ctx.fillStyle = C.ink; ctx.fill();
    ctx.strokeStyle = C.card; ctx.fillStyle = C.card; ctx.lineWidth = 1.6; ctx.lineCap = "round";
    ctx.beginPath(); ctx.arc(cx + 1.5, cy - 2.5, 3.2, 0, 7); ctx.fill();
    ctx.beginPath(); ctx.moveTo(cx + 0.5, cy - 0.5); ctx.lineTo(cx - 4, cy + 5); ctx.stroke();
    ctx.restore();
  }
  function drawToggle(n) {
    const t = toggleOf(n);
    if (!t) return;
    ctx.beginPath(); ctx.arc(t.x, t.y, t.r, 0, 7); ctx.fillStyle = C.card; ctx.fill();
    ctx.lineWidth = 1.2; ctx.strokeStyle = C.ink; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(t.x - t.r * 0.5, t.y); ctx.lineTo(t.x + t.r * 0.5, t.y);
    if (!t.open) { ctx.moveTo(t.x, t.y - t.r * 0.5); ctx.lineTo(t.x, t.y + t.r * 0.5); }
    ctx.lineWidth = 1.5; ctx.stroke();
  }
  // the Folders option of a lakehouse: its schemas / Tables / Files as nodes on the map
  function setFolders(id, on) {
    const n = S.nodes.get(id);
    if (!n) return;
    for (const k of [...S.folderVis.keys()]) if (k.startsWith(`fold:${id}|`)) S.folderVis.delete(k);
    if (on) {
      S.folderMode.add(id); S.treeDD.add(id); onMap(id, true);
      for (const k of new Set(lakeData(id).map(secKeyOf))) S.folderVis.set(folderId(id, k), true);
      readQuietly(n);
    } else S.folderMode.delete(id);
    afterVisibility();
  }
  const visBox = (id, label) => `<input type="checkbox" class="vis" data-vis="${esc(id)}" ${V.vis.has(id) ? "checked" : ""} title="On the map" aria-label="${esc(label)} on the map">`;
  const mixBox = (attr, key, ids, label) => { const on = ids.filter(id => V.vis.has(id)).length;
    return `<input type="checkbox" class="vis" data-${attr}="${esc(key)}" ${ids.length && on === ids.length ? "checked" : ""} data-mixed="${on > 0 && on < ids.length}" title="Put all of them on the map, or take them all off" aria-label="${esc(label)} on the map">`; };
  // the tables or folders of one section of a lakehouse's contents ("<lakehouse id>|t:<schema>" or "|files")
  function sectionIds(key) {
    const [lid, part] = [key.slice(0, key.lastIndexOf("|")), key.slice(key.lastIndexOf("|") + 1)];
    const lh = S.nodes.get(lid);
    if (!lh) return [];
    const {tables, folders} = lakeLists(lh);
    if (part === "files") return folders.map(f => f.id);
    const schema = part.slice(2);
    return tables.filter(t => schema === "Tables" || (t.schema || "dbo") === schema).map(t => t.id);
  }
  function setItemOnMap(id, on) { if (on) showWithLakes(id); else onMap(id, false); afterVisibility(); }
  function setGroupOnMap(gid, on) {
    const g = S.nodes.get(gid);
    if (!g) return;
    for (const m of g.members) {
      const n = S.nodes.get(m);
      if (!n || (on && n.type === "SQLEndpoint" && S.groupMode !== "folder")) continue;
      if (on) showWithLakes(m); else { if (n.type === "Lakehouse") resetLake(m); onMap(m, false); }
    }
    if (on) S.expanded.add(gid); else S.expanded.delete(gid);
    afterVisibility();
  }
  function showAllItems(on) {
    for (const n of S.nodes.values()) if (n.kind === "item" && !n.external && (!on || n.type !== "SQLEndpoint")) { if (!on && n.type === "Lakehouse") resetLake(n.id); onMap(n.id, on); }
    if (!on) {                                   // and everything that was put on the map on its own
      S.expanded.clear();
      for (const n of S.nodes.values()) if (isTable(n) || n.kind === "external" || n.kind === "missing" || n.external) S.shown.delete(n.id);
    }
    afterVisibility();
  }
  function setContents(id, on) {
    const n = S.nodes.get(id);
    if (!n) return;
    const data = lakeData(id);
    if (on) {
      if (data.length > S.bigCascade && !window.confirm(`Put all ${data.length} tables and folders of ${n.label} on the map?`)) { renderTree(); return; }
      S.contents.add(id); S.treeDD.add(id); onMap(id, true);
      for (const d of data) onMap(d.id, true);
      tablesOn();
      if (n.kind === "item" && !n.external && !S.scans.has(n.itemId)) enqueue(n.itemId);
    } else {
      S.contents.delete(id);
      for (const d of data) onMap(d.id, false);
    }
    afterVisibility();
  }
  function focusLineage() {
    if (!S.active) return;
    const keep = new Set(S.active);
    for (const n of S.nodes.values()) if (n.kind === "item" && !n.external) onMap(n.id, keep.has(n.id));
    afterVisibility();
    setTimeout(() => fit(true, keep), 60);
    toast("Showing only this lineage. Show all, in the left panel, puts everything back.");
  }

  // ---- Table content tab: a table's column names, read when asked for
  function setLeftTab(tab) {
    S.leftTab = tab;
    for (const b of document.querySelectorAll("[data-tab]")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    $("#paneStructure").hidden = tab !== "structure"; $("#paneColumns").hidden = tab !== "columns"; $("#paneDatasets").hidden = tab !== "datasets";
    renderDatasets();
    if (document.body.classList.contains("no-left")) { document.body.classList.remove("no-left"); $("#toggleLeft").setAttribute("aria-pressed", "true"); requestAnimationFrame(resize); }
    renderColumns();
  }
  function showColumns(id) { if (!S.nodes.get(id)) return; S.colTarget = id; setLeftTab("columns"); }

  // ---- Datasets tab: every known table and folder, and which notebooks use it
  // Scope "map": the notebooks on the map now; "all": every notebook read so far. Tables written,
  // read or maintained by a notebook in scope are in use; the rest aren't (by those notebooks).
  function datasetView() {
    const nbs = new Set();
    for (const n of S.nodes.values()) if (n.kind === "item" && !n.external && n.type === "Notebook" && (S.dsScope === "all" || V.vis.has(n.id))) nbs.add(n.id);
    const use = new Map();
    const get = id => { let u = use.get(id); if (!u) use.set(id, u = {w: new Set(), r: new Set(), m: new Set(), o: new Set()}); return u; };
    for (const e of S.edges.values()) {
      const pick = {read: ["source", "target", "r"], write: ["target", "source", "w"], maintain: ["target", "source", "m"], model_source: ["source", "target", "o"]}[e.kind];
      if (!pick) continue;
      const d = S.nodes.get(e[pick[0]]), a = S.nodes.get(e[pick[1]]);
      if (!d || !a || !isTable(d)) continue;
      if (a.type === "Notebook") { if (nbs.has(a.id)) get(d.id)[pick[2] === "o" ? "r" : pick[2]].add(a.id); }
      else get(d.id).o.add(a.id);
    }
    const all = [...S.nodes.values()].filter(isTable);
    const direct = n => { const u = use.get(n.id); return !!u && (u.w.size + u.r.size + u.m.size) > 0; };
    const usedFiles = all.filter(n => n.kind === "file" && direct(n));
    const inside = n => n.kind === "file" ? usedFiles.filter(f => f !== n && f.parent === n.parent && f.label.toLowerCase().startsWith(n.label.toLowerCase() + "/")) : [];
    const used = n => direct(n) || inside(n).length > 0;
    const listed = all.filter(n => S.dsFilter === "all" || (S.dsFilter === "used") === used(n));
    const groups = new Map();
    for (const n of listed.sort(byLabel)) { const k = n.parent || "?"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(n); }
    return {use, used, direct, inside, all, listed, groups, nbs};
  }
  function renderDatasets() {
    if (S.leftTab !== "datasets") return;
    const box = $("#dsBody");
    if (!S.ws) { box.innerHTML = ""; return; }
    const dv = datasetView();
    const items = [...S.nodes.values()].filter(n => n.kind === "item" && !n.external);
    const nbs = items.filter(n => n.type === "Notebook"), lakes = items.filter(n => n.type === "Lakehouse");
    const readNb = nbs.filter(n => S.scans.has(n.itemId)).length, readLh = lakes.filter(n => S.scans.has(n.itemId)).length;
    const nUsed = dv.all.filter(dv.used).length, nUnused = dv.all.length - nUsed;
    const scopeTxt = S.dsScope === "all" ? "any notebook read so far" : "the notebooks on the map";
    const names = set => [...set].map(id => (S.nodes.get(id) || {}).label || id).sort().join(", ");
    const medal = n => { const t = String((n && n.label) || "").toLowerCase(); return t.includes("bronze") ? 0 : t.includes("silver") ? 1 : t.includes("gold") ? 2 : 3; };
    const order = [...dv.groups.keys()].sort((a, b) => {
      const pa = S.nodes.get(a), pb = S.nodes.get(b), xa = !pa || pa.kind !== "item" || pa.external, xb = !pb || pb.kind !== "item" || pb.external;
      return xa - xb || medal(pa) - medal(pb) || String((pa || {}).label).localeCompare(String((pb || {}).label));
    });
    const chip = (cls, n, word, set) => n ? `<span class="u ${cls}" title="${esc(word)}: ${esc(names(set))}">${{w: "writes", r: "reads", m: "maint."}[cls]} ${n}</span>` : "";
    const row = n => {
      const u = dv.use.get(n.id) || {w: new Set(), r: new Set(), m: new Set(), o: new Set()}, isUsed = dv.used(n);
      const short = t => ({SemanticModel: "model", DataPipeline: "pipeline", SparkJobDefinition: "Spark job", Dataflow: "dataflow"}[t] || typeName(t).toLowerCase());
      const other = u.o.size ? `<span class="u o" title="Used by ${esc(names(u.o))}">${u.o.size === 1 ? esc(short((S.nodes.get([...u.o][0]) || {}).type)) : `${u.o.size} other items`}</span>` : "";
      const sub = !dv.direct(n) && isUsed ? `<span class="u r" title="In use inside: ${esc(dv.inside(n).map(f => f.label.replace(/^Files\//, "")).join(", "))}">used inside</span>` : "";
      return `<li class="ds-row${isUsed ? "" : " unused"}">${visBox(n.id, n.label)}<button class="row" data-node="${esc(n.id)}" title="${esc(n.fullName || n.label)}">${n.kind === "file" ? FOLDER_SVG : TABLE_SVG}<span class="nm">${esc(n.kind === "file" ? n.label.replace(/^Files\//, "") : n.label)}</span></button>
        <span class="uses">${chip("w", u.w.size, "Written by", u.w)}${chip("r", u.r.size, "Read by", u.r)}${chip("m", u.m.size, "Maintained by", u.m)}${sub}${isUsed ? "" : `<span class="u none">not used</span>`}${other}</span></li>`;
    };
    let html = `<div class="d-type">Datasets</div><h2 class="cols-title">Tables and folders in use</h2>
      <div class="ds-ctrl"><div class="seg" role="group" aria-label="Which notebooks count">
        <button data-dsscope="map" aria-pressed="${S.dsScope === "map"}" title="Only the notebooks on the map now">On the map (${dv.nbs.size && S.dsScope === "map" ? dv.nbs.size : nbs.filter(n => V.vis.has(n.id)).length})</button>
        <button data-dsscope="all" aria-pressed="${S.dsScope === "all"}" title="Every notebook read so far">All notebooks (${nbs.length})</button></div>
      <div class="seg" role="group" aria-label="Show">
        <button data-dsfilter="all" aria-pressed="${S.dsFilter === "all"}">All ${dv.all.length}</button>
        <button data-dsfilter="used" aria-pressed="${S.dsFilter === "used"}">In use ${nUsed}</button>
        <button data-dsfilter="unused" aria-pressed="${S.dsFilter === "unused"}">Not used ${nUnused}</button></div></div>
      <p class="ds-sum">${nUsed} of ${dv.all.length} are written, read or maintained by ${esc(scopeTxt)}.</p>`;
    if (readNb < nbs.length || readLh < lakes.length)
      html += `<p class="d-note">Based on ${readNb} of ${nbs.length} notebooks and ${readLh} of ${lakes.length} lakehouses read. A lakehouse's unused tables only show up once it has been read.
        ${readNb < nbs.length ? `<button class="btn small" data-readall="Notebook">Read the other ${nbs.length - readNb} notebooks</button>` : ""}
        ${readLh < lakes.length ? `<button class="btn small" data-readall="Lakehouse">Read the other ${lakes.length - readLh} lakehouses</button>` : ""}</p>`;
    if (dv.listed.length) html += `<div class="d-actions"><button class="btn small" data-dsall="1" title="Put every table and folder listed here on the map">Put listed on the map</button><button class="btn small" data-dsall="0" title="Take every table and folder listed here off the map">Take listed off</button></div>`;
    html += order.map(k => {
      const p = S.nodes.get(k), list = dv.groups.get(k), inUse = list.filter(dv.used).length;
      return `<section class="ds-group"><div class="ds-head">${mixBox("dsgroup", k, list.map(n => n.id), p ? p.label : "?")}<i style="background:${esc(lakeColor(p))}"></i><button class="ref" data-node="${esc(k)}"><span>${esc(p ? p.label : "Unknown")}</span></button><small>${inUse} of ${list.length} in use</small></div>
        <ul class="ds-list">${list.map(row).join("")}</ul></section>`;
    }).join("") || `<p class="muted">${dv.all.length ? "Nothing matches this filter." : "No tables known yet. Read some notebooks or lakehouses."}</p>`;
    box.innerHTML = html;
    for (const el of box.querySelectorAll('[data-mixed="true"]')) el.indeterminate = true;
  }
  // the columns of a table that the semantic models read so far use: Map(lower-case column -> {models, hidden})
  function modelUse(tableId) {
    const use = new Map();
    for (const sc of S.scans.values()) {
      const model = S.nodes.get("item:" + sc.itemId);
      if (sc.status !== "ok" || !model || model.type !== "SemanticModel" || !sc.details || !Array.isArray(sc.details.tables)) continue;
      for (const t of sc.details.tables) {
        if (t.source !== tableId) continue;
        for (const c of t.columns || []) {
          if (!c.source) continue;
          const k = c.source.toLowerCase(), u = use.get(k) || {models: new Set(), hidden: true};
          u.models.add(model.label); u.hidden = u.hidden && c.hidden; use.set(k, u);
        }
      }
    }
    return use;
  }
  const modelSourceTables = mid => { const m = S.nodes.get(mid), sc = m && S.scans.get(m.itemId); return ((sc && sc.details && sc.details.tables) || []).map(t => t.source).filter(id => (S.nodes.get(id) || {}).kind === "table"); };
  // the column rows a table shows on the map (All, or In use = used by a model); [] while loading
  const COLS_MAX = 40;
  function mapCols(n) {
    const mode = S.colsOnMap.get(n.id), res = S.colCache.get(colKey(n));
    if (!mode || !res) return [];
    const use = modelUse(n.id);
    const cols = (res.columns || []).map(c => { const u = use.get(String(c.name).toLowerCase()); return {name: String(c.name), type: c.type || "", used: !!u, hidden: !!(u && u.hidden)}; });
    return mode === "used" ? cols.filter(c => c.used && !c.hidden) : cols;   // In use: what a model shows, hidden ones left out
  }
  function setColsOnMap(ids, mode) {
    for (const id of ids) {
      const n = S.nodes.get(id);
      if (!n || n.kind !== "table") continue;
      if (mode === "off") S.colsOnMap.delete(id);
      else { S.colsOnMap.set(id, mode); if (!S.colCache.has(colKey(n))) loadColumns(n); }
    }
    rebuildView(false); renderDetails(); requestDraw();
  }
  const colKey = n => { const p = n && S.nodes.get(n.parent); return p && p.kind === "item" ? [p.workspaceId, p.itemId, n.schema || "", n.table || n.label].join("|").toLowerCase() : ""; };
  async function loadColumns(n, refresh = false) {
    const key = colKey(n), p = S.nodes.get(n.parent);
    if (!key || S.colLoading.has(key)) return;
    S.colLoading.add(key);
    if (S.colStale.delete(key)) refresh = true;              // its lakehouse was refreshed since
    if (refresh) S.colCache.delete(key);
    renderColumns();
    try { S.colCache.set(key, await api("/api/columns", {workspaceId: p.workspaceId, lakehouseId: p.itemId, schema: n.schema || "", table: n.table || n.label, refresh})); }
    catch (err) { S.colCache.set(key, {columns: [], error: err.message, hint: err.hint || ""}); }
    finally {
      S.colLoading.delete(key); renderColumns(); refreshLog(); renderDetails();
      if (S.colsOnMap.has(n.id)) rebuildView(false);
    }
  }
  function tablesNear() {
    const n = S.sel && S.nodes.get(S.sel);
    if (!n) return {title: "", list: []};
    let ids = [];
    if (n.kind === "item" && n.type === "Lakehouse") { const sc = S.scans.get(n.itemId); ids = (sc && sc.details && sc.details.tables) || childrenOf(n.id); }
    else if (n.kind === "schema") ids = [...S.nodes.values()].filter(m => m.schemaNode === n.id).map(m => m.id);
    else if (n.kind === "item") { const r = relations([n.id]); ids = [...(r["in:read"] || new Map()).keys(), ...(r["out:write"] || new Map()).keys(), ...(r["out:maintain"] || new Map()).keys()]; }
    else return {title: "", list: []};
    const list = [...new Set(ids)].map(id => S.nodes.get(id)).filter(m => m && m.kind === "table").sort(byLabel);
    return {title: n.kind === "item" && n.type !== "Lakehouse" ? `Tables ${n.label} uses` : `Tables in ${n.label}`, list};
  }
  function renderColumns() {
    if (S.leftTab !== "columns") return;
    const box = $("#colsBody");
    if (!S.ws) { box.innerHTML = ""; return; }
    const near = tablesNear();
    const pick = near.list.length ? `<div class="d-sec">${esc(near.title)} <small>${near.list.length}</small></div><div class="cols-pick">${near.list.map(t => `<button data-cols="${esc(t.id)}" class="${S.colTarget === t.id ? "on" : ""}" title="${esc(t.fullName || t.label)}">${esc(t.table || t.label)}</button>`).join("")}</div>` : "";
    const n = S.colTarget && S.nodes.get(S.colTarget), use = n ? modelUse(n.id) : new Map();
    if (!n) { box.innerHTML = `<div class="empty-hint"><h2>Table content</h2><p>Select a table (on the map, under a lakehouse's Contents, or in the details panel) to see its column names here.</p></div>${pick}`; return; }
    const p = S.nodes.get(n.parent), key = colKey(n), res = key && S.colCache.get(key);
    let html = `<div class="d-type"><i style="background:${esc(colorOf(n))}"></i>Table</div><h2 class="cols-title">${esc(n.table || n.label)}</h2><div class="cols-meta">${esc(p ? p.label : "?")}${n.schema ? `, schema ${esc(n.schema)}` : ""}</div>`;
    if (!key) html += `<p class="d-note">This table's lakehouse isn't known, so its columns can't be read.</p>`;
    else if (!res || S.colLoading.has(key)) { html += `<p class="muted">Reading the column names…</p>`; if (!res && !S.colLoading.has(key)) setTimeout(() => loadColumns(n), 0); }
    else if (res.error) html += `<div class="d-error"><b>Couldn't read the columns.</b> ${esc(res.hint)}<div class="muted" style="margin-top:4px">${esc(res.error)}</div></div><div class="d-actions"><button class="btn small" data-cact="refresh">Try again</button></div>`;
    else html += `<div class="d-actions"><button class="btn small" data-cact="copy" title="One name per line, ready to paste into Excel">Copy names</button><button class="btn small" data-cact="refresh" title="Read the columns again from Fabric">Read again</button></div>
        <input class="field" id="colFilter" type="search" placeholder="Filter ${res.columns.length} columns" autocomplete="off" style="width:100%">
        <ol class="cols" id="colList">${res.columns.map(c => { const u = use.get(String(c.name).toLowerCase()); return `<li${u ? ' class="in-model"' : ""} title="${esc(c.type)}${c.nullable === false ? ", not null" : ""}${u ? `, used by ${esc([...u.models].join(", "))}${u.hidden ? " (hidden)" : ""}` : ""}">${esc(c.name)}${u ? '<span class="cm">model</span>' : ""}</li>`; }).join("")}</ol>
        <div class="cols-foot">${res.columns.length} columns, read at ${esc(fmtTime(res.readAt))} with the ${esc(res.via)}.</div>`;
    box.innerHTML = html + pick;
  }
  async function copyColumns() {
    const n = S.colTarget && S.nodes.get(S.colTarget), res = n && S.colCache.get(colKey(n));
    if (!res || !res.columns) return;
    const text = res.columns.map(c => c.name).join("\n");
    try { await navigator.clipboard.writeText(text); }
    catch { const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select(); document.execCommand("copy"); t.remove(); }
    toast(`Copied ${res.columns.length} column names.`);
  }
  function setOpt(k, v) {
    S.opts[k] = v; store.set("opts", S.opts);
    const box = document.querySelector(`[data-opt="${k}"]`); if (box) box.checked = v;
    rebuildView(); renderAll();
  }
  function setGroupMode(mode) {
    if (mode === S.groupMode) return;
    for (const n of V.nodes) if (n.kind === "item" && !n.external) S.shown.add(n.id);
    S.groupMode = mode; store.set("groupMode", mode);
    rebuildGroups();
    S.expanded.clear(); S.treeOpen.clear();
    for (const n of S.nodes.values()) if (n.kind === "group" && defaultOpen(n)) S.treeOpen.add(n.id);
    placeInitial(); rebuildView(); renderAll();
  }

  // ============================================================ panels
  let treeTimer = 0;
  function renderTreeSoon() { if (!treeTimer) treeTimer = requestAnimationFrame(() => { treeTimer = 0; renderTree(); }); }
  const stClass = n => ({none: "", busy: "busy", ok: "ok", err: "err", na: "na"}[state(n)] || "");
  const TABLE_SVG = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.1" aria-hidden="true"><rect x="1.5" y="2" width="9" height="8" rx="1"/><path d="M1.5 5h9M1.5 7.5h9M5 5v5"/></svg>';
  const FOLDER_SVG = '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.1" aria-hidden="true"><path d="M1.5 3h3l1 1.2h5v5.3h-9z"/></svg>';
  const byLabel = (a, b) => String(a.table || a.label).localeCompare(String(b.table || b.label), undefined, {numeric: true});
  function renderTree() {
    const body = $("#treeBody");
    if (!S.ws) { body.innerHTML = ""; return; }
    const q = S.treeFilter;
    const groups = [...S.nodes.values()].filter(n => n.kind === "group").sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));
    body.innerHTML = groups.map(g => {
      const all = g.members.map(id => S.nodes.get(id)).filter(Boolean);
      const members = all.filter(m => !q || m.label.toLowerCase().includes(q)).sort(byLabel);
      if (q && !members.length) return "";
      const open = !!q || S.treeOpen.has(g.id);
      const on = all.filter(m => V.vis.has(m.id)).length;
      const readable = all.filter(m => m.scannable), read = readable.filter(m => S.scans.has(m.itemId)).length;
      return `<section class="tg"><div class="tg-head">
        <input type="checkbox" class="vis" data-gvis="${esc(g.id)}" ${all.length && on === all.length ? "checked" : ""} data-mixed="${on > 0 && on < all.length}" title="Put all of them on the map, or take them all off" aria-label="${esc(g.label)} on the map">
        <button class="tg-toggle" data-tgroup="${esc(g.id)}" aria-expanded="${open}">${CHEV}<span>${esc(g.label)}</span><span class="count">${readable.length && read ? `${read}/` : ""}${all.length}</span></button>${open && all.some(m => m.type === "Lakehouse") ? '<span class="opt-heads" aria-hidden="true"><span class="f">Folders</span><span class="c">Contents</span></span><span class="ext-gap"></span>' : ""}</div>
        ${open ? `<ul>${members.map(itemRow).join("")}</ul>` : ""}
      </section>`;
    }).join("") || '<p class="muted" style="padding:8px">Nothing matches the filter.</p>';
    for (const box of body.querySelectorAll('[data-mixed="true"]')) box.indeterminate = true;
    $("#treeWs").textContent = S.ws.name;
    const link = $("#treeWsLink"); link.href = S.ws.url; link.hidden = false;
    const items = [...S.nodes.values()].filter(n => n.kind === "item" && !n.external);
    const onMap = items.filter(n => V.vis.has(n.id)).length;
    $("#treeSub").textContent = `${items.length} items, ${onMap} on the map` + (S.loadedAt ? `, list read ${fmtTime(S.loadedAt)}` : "");
    renderDatasets();
  }
  const SHORT_TYPE = {SQLEndpoint: "SQL endpoint", DataPipeline: "Pipeline", SemanticModel: "Semantic model", SparkJobDefinition: "Spark job",
    MLExperiment: "ML experiment", MLModel: "ML model", KQLDatabase: "KQL database", KQLQueryset: "KQL queryset", MirroredDatabase: "Mirrored DB"};
  const shortType = t => SHORT_TYPE[t] || typeName(t);
  function itemRow(m) {
    const on = V.vis.has(m.id), lake = m.type === "Lakehouse", contents = lake && S.contents.has(m.id), open = lake && S.treeDD.has(m.id);
    return `<li><div class="it">
      ${lake ? `<button class="tdd" data-tdd="${esc(m.id)}" aria-expanded="${open}" title="${open ? "Hide" : "Show"} its tables and folders here (the map doesn't change)" aria-label="${open ? "Hide" : "Show"} the tables and folders of ${esc(m.label)}">${CHEV}</button>` : '<span class="tdd-gap"></span>'}
      <input type="checkbox" class="vis" data-vis="${esc(m.id)}" ${on ? "checked" : ""} title="On the map" aria-label="${esc(m.label)} on the map">
      <button class="row${isSel(m.id) ? " sel" : ""}" data-node="${esc(m.id)}"><span class="st ${stClass(m)}"></span><span class="nm">${esc(m.label)}</span>${S.pinned === m.id ? PIN_SVG : ""}${S.groupMode === "folder" && !lake ? `<span class="hint">${esc(shortType(m.type))}</span>` : ""}</button>
      ${lake ? `<span class="opt f"><input type="checkbox" data-folders="${esc(m.id)}" ${S.folderMode.has(m.id) ? "checked" : ""} title="Folders: show its schemas and folders as nodes on the map; each one then gets its own tickbox in the list" aria-label="Folders of ${esc(m.label)} on the map"></span><span class="opt c"><input type="checkbox" data-contents="${esc(m.id)}" ${contents ? "checked" : ""} title="Contents: put all its schemas, tables and folders on the map" aria-label="All contents of ${esc(m.label)} on the map"></span>` : ""}
      ${m.url ? `<a class="ext" href="${esc(m.url)}" target="_blank" rel="noopener" title="Open in Fabric" aria-label="Open ${esc(m.label)} in Fabric">${EXT_SVG}</a>` : ""}</div>
      ${open ? `<ul class="sub">${contentsHtml(m)}</ul>` : ""}</li>`;
  }
  function lakeLists(lh) {
    const sc = S.scans.get(lh.itemId), d = (sc && sc.status === "ok" && sc.details) || {};
    const tables = (d.tables || []).map(id => S.nodes.get(id)).filter(Boolean);
    const folders = (d.folders || []).map(id => S.nodes.get(id)).filter(Boolean);
    for (const m of S.nodes.values()) if (m.parent === lh.id && isTable(m)) { const l = m.kind === "file" ? folders : tables; if (!l.includes(m)) l.push(m); }
    return {tables: tables.sort(byLabel), folders: folders.sort(byLabel)};
  }
  // A lakehouse's contents: schemas (or Tables) with their tables, then the folders under Files
  function contentsHtml(lh) {
    const sc = S.scans.get(lh.itemId);
    if (S.busy.has(lh.itemId) || !sc) return '<li class="sub-note">Reading its tables and folders…</li>';
    if (sc.status !== "ok") return `<li class="sub-note bad">${esc(sc.hint || sc.error || "It couldn't be read.")}</li>`;
    const d = sc.details || {};
    const {tables, folders} = lakeLists(lh);
    const many = tables.length > 40;
    const section = (key, title, rows, count) => {
      const sk = `${lh.id}|${key}`, open = S.subState.has(sk) ? S.subState.get(sk) : !many, fid = folderId(lh.id, key);
      const fbox = S.folderMode.has(lh.id) ? `<input type="checkbox" class="vis fold" data-foldvis="${esc(fid)}" ${V.vis.has(fid) ? "checked" : ""} title="The folder ${esc(title)} on the map (its name only)" aria-label="Folder ${esc(title)} on the map">` : "";
      return `<li><div class="it">${fbox}${mixBox("secvis", sk, sectionIds(sk), title)}<button class="sub-head" data-sub="${esc(sk)}" data-open="${open}" aria-expanded="${open}">${CHEV}${esc(title)} <small>${count}</small></button></div>${open ? `<ul>${rows}</ul>` : ""}</li>`;
    };
    const tableRow = t => `<li><div class="it sub-it">${visBox(t.id, t.label)}<button class="row${isSel(t.id) ? " sel" : ""}" data-node="${esc(t.id)}">${TABLE_SVG}<span class="nm">${esc(t.table || t.label)}</span>${t.shortcut ? '<span class="hint">shortcut</span>' : ""}</button><button class="cols-btn" data-cols="${esc(t.id)}" title="Show its column names">Columns</button></div></li>`;
    const folderRow = f => `<li><div class="it sub-it">${visBox(f.id, f.label)}<button class="row${isSel(f.id) ? " sel" : ""}" data-node="${esc(f.id)}">${FOLDER_SVG}<span class="nm">${esc(f.label.replace(/^Files\//, ""))}</span></button></div></li>`;
    const bySchema = new Map();
    for (const t of tables) { const k = t.schema || "Tables"; if (!bySchema.has(k)) bySchema.set(k, []); bySchema.get(k).push(t); }
    void d;
    let html = S.folderMode.has(lh.id) ? '<li class="sub-note">First box: the folder\'s name on the map. Second box: all its datasets.</li>' : "";
    html += [...bySchema.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([k, list]) => section("t:" + k, k, list.map(tableRow).join(""), list.length)).join("");
    if (!tables.length) html += '<li class="sub-note">No tables.</li>';
    html += section("files", "Files", folders.length ? folders.map(folderRow).join("") : '<li class="sub-note">No folders.</li>', folders.length);
    return html;
  }
  function fmtTime(iso) { try { return new Date(iso).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); } catch { return ""; } }

  function renderFocus() {
    const bar = $("#focusBar"), hint = $("#mapHint"), btn = $("#focusBtn");
    if (btn) { btn.setAttribute("aria-pressed", String(!!S.focusMode)); btn.textContent = S.focusMode ? "Leave focus" : "Focus"; }
    if (hint) hint.hidden = !!S.focusMode || !!S.pinned;
    const pb = $("#pinBar");
    if (pb) {
      pb.hidden = !S.pinned;
      const pn = S.pinned && S.nodes.get(S.pinned);
      if (pn) $("#pinText").innerHTML = `Highlight pinned on <b>${esc(pn.fullName || pn.label)}</b>${V.vis.has(pn.id) ? "" : " (not on the map)"}. Clicking other items keeps it.`;
    }
    if (!bar) return;
    bar.hidden = !S.focusMode;
    if (S.focusMode) $("#focusText").textContent = `Focus mode: the map shows only the ${V.nodes.filter(n => n.kind !== "group").length} items that were highlighted, and anything you tick.`;
  }
  function renderTitle() {
    renderFocus();
    const tb = $("#titleblock");
    if (!S.ws) { tb.hidden = true; return; }
    tb.hidden = false;
    $("#tbName").textContent = S.ws.name; $("#tbLink").href = S.ws.url;
    const items = [...S.nodes.values()].filter(n => n.kind === "item" && !n.external);
    const read = items.filter(n => S.scans.has(n.itemId) && S.scans.get(n.itemId).status === "ok").length;
    let links = 0; for (const e of S.edges.values()) if (DATA_KINDS.has(e.kind) && e.kind !== "references") links++;
    $("#tbItems").textContent = items.length; $("#tbRead").textContent = read; $("#tbLinks").textContent = links;
    const foot = $("#tbFoot");
    if (S.batch.total > 1) {
      const pct = Math.round(S.batch.done / S.batch.total * 100);
      foot.innerHTML = `<span>Reading ${S.batch.done + S.running} of ${S.batch.total}</span><span class="meter"><i style="width:${pct}%"></i></span><button data-act="stop">Stop</button>`;
    } else foot.innerHTML = `<span>Select an item to read it. Selecting it again uses the copy already read.</span>`;
  }
  function updateActivity() {
    const n = S.running + S.queue.length;
    setActivity(n ? `Reading ${n} item${n === 1 ? "" : "s"}` : "", n > 0);
  }
  function setActivity(text, busy) {
    $("#activity").innerHTML = text ? `${busy ? '<span class="pulse"></span>' : ""}<span>${esc(text)}</span>` : "";
  }
  function toast(msg) { setActivity(msg, false); setTimeout(() => updateActivity(), 5000); }

  // ---- details
  function relations(ids) {
    const out = {};
    const set = new Set(ids);
    for (const e of S.edges.values()) {
      let key, other;
      if (set.has(e.source) && !set.has(e.target)) { key = "out:" + e.kind; other = e.target; }
      else if (set.has(e.target) && !set.has(e.source)) { key = "in:" + e.kind; other = e.source; }
      else continue;
      const m = out[key] || (out[key] = new Map());
      const list = m.get(other) || [];
      for (const ev of e.evidence || []) list.push({...ev, via: set.has(e.source) ? e.source : e.target});
      m.set(other, list);
    }
    return out;
  }
  let rowSeq = 0;
  function refRow(id, evidence = [], extra = "", ctx = "") {
    const n = S.nodes.get(id);
    const lake = !!n && n.kind === "item" && n.type === "Lakehouse", ddKey = `${ctx}|${id}`, ddOpen = lake && S.ddOpen.has(ddKey);
    const dd = lake ? `<button class="dd" data-dd="${esc(ddKey)}" data-lh="${esc(id)}" aria-expanded="${ddOpen}" title="${ddOpen ? "Hide" : "Show"} its tables and folders" aria-label="${ddOpen ? "Hide" : "Show"} the tables and folders of ${esc(n.label)}">${CHEV}</button>` : "";
    const label = n ? (n.kind === "table" || n.kind === "file" ? n.label : n.label) : id;
    let chip = "";
    if (n && (n.kind === "table" || n.kind === "file")) { const p = S.nodes.get(n.parent); chip = `<span class="chip"><i style="background:${esc(lakeColor(p))}"></i>${esc(p ? p.label : "?")}</span>`; }
    else if (n && n.kind === "item") chip = `<span class="chip">${esc(typeName(n.type))}${n.external ? " elsewhere" : ""}</span>`;
    else if (n) chip = `<span class="chip">${esc(n.kind === "missing" ? "not found" : "outside Fabric")}</span>`;
    const evs = evidence.filter(e => e && (e.where || e.code));
    const rid = "ev" + (++rowSeq);
    const first = evs[0] ? evs[0].where + (evs.length > 1 ? ` +${evs.length - 1}` : "") : "";
    return `<li><div class="ref-line">${n && n.kind !== "group" ? visBox(id, label) : ""}${chip}<button class="ref" data-node="${esc(id)}"><span>${esc(label)}</span></button>${dd}${extra}${first ? `<button class="ev-toggle" data-ev="${rid}" aria-expanded="false">${esc(first)}</button>` : ""}</div>${evs.length ? `<div class="ev" id="${rid}" hidden>${evs.map(e => `<div>${esc(e.where || "")}${e.verb ? `, ${esc(e.verb)}` : ""}${e.note ? `, ${esc(e.note)}` : ""}</div>${e.code ? `<code>${esc(e.code)}</code>` : ""}`).join("")}</div>` : ""}${ddOpen ? lakeDropdown(n, ddKey) : ""}</li>`;
  }
  // A lakehouse's contents for its dropdown: schemas (or Tables) with their tables, then Files.
  // Tables the code uses but the lakehouse doesn't list are included; for a lakehouse that hasn't
  // been read (or is in another workspace) only those are known.
  function lakeSections(lh) {
    const sc = S.scans.get(lh.itemId), d = sc && sc.status === "ok" ? (sc.details || {}) : null;
    const known = [...S.nodes.values()].filter(m => m.parent === lh.id && isTable(m));
    const tables = d ? (d.tables || []).map(id => S.nodes.get(id)).filter(Boolean) : [];
    const folders = d ? (d.folders || []).map(id => S.nodes.get(id)).filter(Boolean) : [];
    for (const m of known) { const list = m.kind === "file" ? folders : tables; if (!list.includes(m)) list.push(m); }
    const secs = new Map();
    for (const t of tables.sort(byLabel)) { const k = t.schema || "Tables"; if (!secs.has(k)) secs.set(k, []); secs.get(k).push(t); }
    const out = [...secs.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([k, rows]) => ({key: "t:" + k, title: k, rows}));
    if (folders.length) out.push({key: "files", title: "Files", rows: folders.sort(byLabel)});
    const state = sc ? (sc.status === "ok" ? "ok" : "error") : S.busy.has(lh.itemId) || (!lh.external && lh.scannable) ? "reading" : "unread";
    return {state, sections: out, error: sc && sc.status !== "ok" ? (sc.hint || sc.error || "") : ""};
  }
  function lakeDropdown(lh, key) {
    const info = lakeSections(lh);
    let html = "";
    if (info.state === "reading") html += '<p class="dd-note">Reading its tables and folders…</p>';
    else if (info.state === "unread") html += '<p class="dd-note">In another workspace: only the tables that what you have read uses are known.</p>';
    else if (info.state === "error") html += `<p class="dd-note bad">${esc(info.error || "It couldn't be read.")}</p>`;
    for (const sec of info.sections) {
      const sk = `${key}|${sec.key}`, open = S.ddSec.has(sk) ? S.ddSec.get(sk) : sec.rows.length <= 12;
      S.ddSecIds.set(sk, sec.rows.map(r => r.id));
      html += `<div class="dd-sec"><div class="dd-head">${mixBox("ddsecvis", sk, sec.rows.map(r => r.id), sec.title)}<button class="dd-toggle" data-ddsec="${esc(sk)}" data-open="${open}" aria-expanded="${open}">${CHEV}${esc(sec.title)} <small>${sec.rows.length}</small></button></div>
        ${open ? `<ul class="dd-list">${sec.rows.map(r => `<li><div class="ref-line">${visBox(r.id, r.label)}<button class="ref" data-node="${esc(r.id)}" title="${esc(r.fullName || r.label)}">${r.kind === "file" ? FOLDER_SVG : TABLE_SVG}<span>${esc(r.kind === "file" ? r.label.replace(/^Files\//, "") : (r.table || r.label))}</span></button>${r.kind === "table" ? `<button class="ev-toggle" data-act="columns" data-id="${esc(r.id)}" title="Show its column names">Columns</button>` : ""}</div></li>`).join("")}</ul>` : ""}</div>`;
    }
    if (info.state === "ok" && !info.sections.length) html += '<p class="dd-note">No tables or folders.</p>';
    return `<div class="lh-dd">${html}</div>`;
  }
  function refList(title, map, note = "") {
    if (!map || !map.size) return "";
    const rows = [...map.entries()].sort((a, b) => sortKey(a[0]).localeCompare(sortKey(b[0]), undefined, {numeric: true}));
    return `<div class="d-sec">${esc(title)} <small>${rows.length}</small></div>${note}<ul class="refs">${rows.map(([id, ev]) => refRow(id, ev, "", title)).join("")}</ul>`;
  }
  const sortKey = id => { const n = S.nodes.get(id); if (!n) return id; const p = n.parent && S.nodes.get(n.parent); return (p ? p.label + "/" : "") + n.label; };
  function actions(n) {
    const bits = [];
    if (n.url) bits.push(`<a class="btn" href="${esc(n.url)}" target="_blank" rel="noopener">${EXT_SVG}Open in Fabric</a>`);
    if (n.kind === "item" && !n.external && n.scannable) {
      const busy = S.busy.has(n.itemId);
      bits.push(S.scans.has(n.itemId)
        ? `<button class="btn" data-act="refresh" data-item="${esc(n.itemId)}" ${busy ? "disabled" : ""} title="Read it again from Fabric; what was read before is dropped, so your latest changes show">Refresh</button>`
        : `<button class="btn" data-act="rescan" data-item="${esc(n.itemId)}" ${busy ? "disabled" : ""} title="Read it from Fabric">Read now</button>`);
      if (S.scans.has(n.itemId)) bits.push(`<button class="btn" data-act="unload" data-item="${esc(n.itemId)}" ${busy ? "disabled" : ""} title="Forget what was read: it goes back to not read and its links leave the map. Selecting it reads it again.">Unload</button>`);
    }
    if (n.kind === "table") bits.push(`<button class="btn" data-act="columns" data-id="${esc(n.id)}" title="Show its column names in the Table content tab">Show columns</button>`);
    if (n.kind === "item" && n.type === "Lakehouse" && !n.external) bits.push(`<button class="btn" data-act="contents" data-id="${esc(n.id)}" title="Its schemas, folders and tables, on the map and in the left panel">${S.contents.has(n.id) ? "Hide its contents" : "Show its contents"}</button>`);
    bits.push(S.pinned === n.id
      ? `<button class="btn" data-act="unpin" title="Let the highlight follow what you select again">Unpin highlight</button>`
      : `<button class="btn" data-act="pin" data-id="${esc(n.id)}" title="Keep its lineage highlighted while you click other items">Pin its highlight</button>`);
    if (!S.focusMode && n.kind !== "group" && n.kind !== "schema" && (isTable(n) || n.kind === "item") && fullGraph().adj.has(n.id))
      bits.push(`<button class="btn" data-act="reveal" data-id="${esc(n.id)}" title="Put everything related to it on the map (also what you took off) and highlight it">Show all its relations</button>`);
    if (S.sel === n.id && S.active && S.active.size > 1) bits.push(`<button class="btn" data-act="zoomchain" title="Fit the map to everything highlighted">Zoom to its lineage</button>`);
    if (S.sel === n.id && S.active && S.active.size > 1 && !isTable(n)) bits.push(`<button class="btn" data-act="focusnow" title="Show only what is highlighted; your tickboxes don't change">${S.focusMode ? "Leave focus" : "Focus on its lineage"}</button>`);
    if (n.kind !== "group") bits.push(`<button class="btn" data-act="${V.vis.has(n.id) ? "hide" : "show"}" data-id="${esc(n.id)}">${V.vis.has(n.id) ? "Hide from map" : "Show on map"}</button>`);
    return `<div class="d-actions">${bits.join("")}</div>`;
  }
  function head(n, typeText) {
    const col = colorOf(n);
    return `<div class="d-type"><i style="background:${esc(col)}"></i>${esc(typeText)}${S.pinned === n.id ? `<span class="pin-chip">${PIN_SVG}Pinned highlight</span>` : ""}</div><h2 class="d-title">${esc(n.fullName || n.label)}</h2>${actions(n)}`;
  }
  function statusLine(n) {
    if (n.kind !== "item" || n.external) return "";
    const b = S.busy.get(n.itemId), sc = S.scans.get(n.itemId);
    if (b) return `<div class="d-status">${b === "queued" ? "Waiting to be read…" : "Reading it from Fabric…"}</div>`;
    if (!sc) return n.scannable ? `<div class="d-status">Not read yet.</div>` : `<div class="d-status">Fabric doesn't provide the content of this kind of item.</div>`;
    let html = `<div class="d-status">Read at ${esc(fmtTime(sc.scannedAt))}${sc.ms ? ` in ${(sc.ms / 1000).toFixed(1)} s` : ""}. Selecting it again uses this copy.</div>`;
    if (sc.status === "error") html += `<div class="d-error"><b>Couldn't read it.</b> ${esc(sc.hint || "")}<div class="muted" style="margin-top:4px">${esc(sc.error || "")}</div></div>`;
    else if (sc.summary) html += `<p class="d-summary">${esc(sc.summary)}</p>`;
    return html;
  }
  function renderDetailsInner() {
    const box = $("#detailsBody");
    rowSeq = 0;
    if (!S.ws) { box.innerHTML = ""; return; }
    if (S.selEdge) { box.innerHTML = edgeDetails(); return; }
    if (S.multi.size > 1) { box.innerHTML = multiDetails(); return; }
    const n = S.sel && S.nodes.get(S.sel);
    if (!n) { box.innerHTML = overview(); return; }
    if (n.kind === "group") box.innerHTML = groupDetails(n);
    else if (n.kind === "schema") box.innerHTML = schemaDetails(n);
    else if (n.kind === "table" || n.kind === "file") box.innerHTML = tableDetails(n);
    else if (n.kind === "missing" || n.kind === "external") box.innerHTML = head(n, n.kind === "missing" ? "Not found" : "Outside Fabric") + `<p class="d-note">${esc(n.note || "")}</p>${n.detail ? `<div class="ev"><code>${esc(n.detail)}</code></div>` : ""}` + relationsHtml([n.id, ...childrenOf(n.id)]);
    else if (n.type === "Lakehouse") box.innerHTML = lakehouseDetails(n);
    else box.innerHTML = itemDetails(n);
  }
  const childrenOf = id => [...S.nodes.values()].filter(m => m.parent === id).map(m => m.id);
  function relationsHtml(ids, order) {
    const rel = relations(ids);
    const keys = order || ["in:read", "out:write", "out:maintain", "out:run", "in:run", "out:refresh", "in:refresh", "in:model_source", "out:model_source", "in:report_model", "out:report_model", "in:shortcut", "out:shortcut", "out:default_lakehouse", "in:default_lakehouse", "out:attached", "in:attached", "out:environment", "in:environment", "out:references", "in:references"];
    return keys.map(k => { const [dir, kind] = k.split(":"); const st = EDGE[kind]; return st ? refList(st[dir], rel[k]) : ""; }).join("");
  }
  function overview() {
    const items = [...S.nodes.values()].filter(n => n.kind === "item" && !n.external);
    const nbs = items.filter(n => n.type === "Notebook"), lakes = items.filter(n => n.type === "Lakehouse");
    const unreadNb = nbs.filter(n => !S.scans.has(n.itemId)).length, unreadLh = lakes.filter(n => !S.scans.has(n.itemId)).length;
    return `<div class="empty-hint"><h2>${esc(S.ws.name)}</h2>
      <p>The map shows what's in the workspace. Nothing inside an item is read until you select it.</p>
      <ol><li>Select a notebook: its code is read and the map draws the tables it reads (blue) and writes (orange).</li>
      <li>Select a lakehouse: its tables and shortcuts are listed.</li>
      <li>Select a table: see every notebook that writes or reads it.</li></ol>
      <p>Ctrl+click an item, or the arrow at its corner, to open it in Fabric.</p>
      <div class="d-actions">
        ${unreadNb ? `<button class="btn primary" data-act="readall" data-type="Notebook">Read all ${unreadNb} notebooks</button>` : ""}
        ${unreadLh ? `<button class="btn" data-act="readall" data-type="Lakehouse">Read all ${unreadLh} lakehouses</button>` : ""}
        ${S.scans.size ? `<button class="btn" data-act="unloadall" title="Forget everything that was read: every item goes back to not read">Unload everything read (${readIds(items).length})</button>` : ""}
      </div></div>`;
  }
  function itemDetails(n) {
    const sc = S.scans.get(n.itemId) || {};
    const d = sc.details || {};
    let html = head(n, typeName(n.type) + (n.external ? ` in ${n.workspaceName || "another workspace"}` : "")) + statusLine(n);
    if (n.type === "Notebook" && sc.status === "ok") {
      const facts = [["Language", d.language], ["Cells", d.cells ? `${d.cells} (${d.codeCells} with code)` : ""]];
      html += `<dl class="kv">${facts.filter(f => f[1]).map(f => `<dt>${esc(f[0])}</dt><dd>${esc(f[1])}</dd>`).join("")}</dl>`;
      if ((d.includedFrom || []).length) html += `<p class="d-note">Uses names defined in ${esc(d.includedFrom.join(", "))} (pulled in with %run).</p>`;
      if ((d.regexCells || []).length) html += `<p class="d-note">Cell ${esc(d.regexCells.join(", "))} isn't valid Python, so it was read with simpler pattern matching.</p>`;
    }
    if (n.type === "SemanticModel" && (d.tables || []).length) html += modelTablesHtml(n, d);
    if (n.type === "DataPipeline" && (d.activities || []).length) {
      html += `<div class="d-sec">Activities that touch other items <small>${d.activities.length}</small></div><table class="tbl">${d.activities.map(a => `<tr><td>${esc(a.name)}</td><td class="num">${esc(a.type)}</td></tr>`).join("")}</table>`;
    }
    html += relationsHtml([n.id]);
    const un = sc.unresolved || [];
    if (un.length) html += `<div class="d-sec">Names built at run time <small>${un.length}</small></div><p class="d-note">The code builds these names while it runs, so they can't be placed on the map.</p><ul class="refs">${un.map(u => { const rid = "un" + (++rowSeq); return `<li><div class="ref-line"><span class="chip">${esc(u.op || "")}</span><span>${esc(u.shown)}</span><button class="ev-toggle" data-ev="${rid}">${esc(u.where || "")}</button></div><div class="ev" id="${rid}" hidden>${u.code ? `<code>${esc(u.code)}</code>` : ""}</div></li>`; }).join("")}</ul>`;
    const ex = d.exports || {};
    const names = Object.keys(ex);
    if (n.type === "Notebook" && names.length && names.length <= 30 && relations([n.id])["in:run"]) html += `<div class="d-sec">Names it defines for %run <small>${names.length}</small></div><dl class="kv">${names.map(k => `<dt>${esc(k)}</dt><dd>${esc(ex[k].join(", "))}</dd>`).join("")}</dl>`;
    return html;
  }
  function lakehouseDetails(n) {
    const sc = S.scans.get(n.itemId) || {};
    const d = sc.details || {};
    let html = head(n, n.external ? `Lakehouse in ${n.workspaceName || "another workspace"}` : "Lakehouse") + statusLine(n);
    const kids = childrenOf(n.id);
    const rel = relations(kids);
    const nbs = [...S.nodes.values()].filter(m => m.kind === "item" && !m.external && m.type === "Notebook");
    const readNb = nbs.filter(m => S.scans.has(m.itemId)).length;
    // seen from the lakehouse's tables: writers arrive as in:write, readers leave as out:read
    const w2 = new Map(), r2 = new Map(), m2 = new Map(), models = new Map(), sc2 = new Map();
    for (const [k, map] of Object.entries(rel)) for (const [id, ev] of map) {
      const target = k === "in:write" ? w2 : k === "out:read" ? r2 : k === "in:maintain" ? m2 : k === "out:model_source" ? models : k === "in:shortcut" ? sc2 : null;
      if (target) target.set(id, (target.get(id) || []).concat(ev));
    }
    const coverage = nbs.length ? `<p class="d-note">Based on the ${readNb} of ${nbs.length} notebooks read so far.${readNb < nbs.length ? ` <button class="btn small" data-act="readall" data-type="Notebook">Read the other ${nbs.length - readNb}</button>` : ""}</p>` : "";
    html += refList("Written by", w2, coverage) || (nbs.length ? `<div class="d-sec">Written by</div>${coverage}<p class="muted">Nothing found yet.</p>` : "");
    html += refList("Read by", r2) + refList("Maintained by", m2) + refList("Used by semantic models", models) + refList("Shortcut sources", sc2);
    html += relationsHtml([n.id], ["in:default_lakehouse", "in:attached", "in:references", "out:references"]);
    if (sc.status === "ok") {
      const tables = (d.tables || []).map(id => S.nodes.get(id)).filter(Boolean);
      const shown = S.contents.has(n.id);
      html += `<div class="d-sec">Tables <small>${tables.length}${d.tablesVia ? `, listed with the ${esc(d.tablesVia)}` : ""}</small><button class="btn small" data-act="contents" data-id="${esc(n.id)}">${shown ? "Hide contents" : "Show contents"}</button></div>`;
      if (tables.length) {
        const ioOf = id => { const r = relations([id]); return {in: (r["in:write"] || new Map()).size, out: ((r["out:read"] || new Map()).size + (r["out:model_source"] || new Map()).size)}; };
        html += `<table class="tbl">${tables.sort((a, b) => a.label.localeCompare(b.label)).map(t => { const io = ioOf(t.id); return `<tr><td>${visBox(t.id, t.label)}<button class="ref" data-node="${esc(t.id)}"><span>${esc(t.label)}</span></button>${t.shortcut ? ' <span class="chip">shortcut</span>' : ""}</td><td class="num"><span class="io" title="written by, read by"><span class="in">${io.in ? `<b>${io.in}</b> in` : ""}</span><span class="out">${io.out ? `<b>${io.out}</b> out` : ""}</span></span></td></tr>`; }).join("")}</table>`;
      }
      if (d.sqlEndpoint && d.sqlEndpoint.connectionString) html += `<div class="d-sec">SQL analytics endpoint</div><dl class="kv"><dt>Server</dt><dd>${esc(d.sqlEndpoint.connectionString)}</dd><dt>Schemas</dt><dd>${d.schemas ? `on (default ${esc(d.defaultSchema)})` : "off"}</dd></dl>`;
      for (const note of d.notes || []) html += `<p class="d-note">${esc(note)}</p>`;
    }
    return html;
  }
  // A model's tables, each opening onto its columns: by default all the source table's columns,
  // marked in the model / hidden in the model / not in the model; or only the model's visible ones.
  function modelTablesHtml(n, d) {
    const only = S.modelColsVisibleOnly, src = modelSourceTables(n.id);
    let html = `<div class="d-sec">Model tables <small>${d.tables.length}</small></div>
      <label class="tick-line"><input type="checkbox" data-modelonly ${only ? "checked" : ""}> Only the model's visible columns</label>
      ${src.length ? colsMapCtl(src, n.id) : ""}<ul class="mt-list">`;
    for (const t of d.tables) {
      const key = `${n.id}|${t.table}`, open = S.modelColsOpen.has(key) ? S.modelColsOpen.get(key) : d.tables.length <= 8;
      const s = t.source && S.nodes.get(t.source), cols = t.columns || [];
      html += `<li class="mt"><div class="mt-head"><button class="dd-toggle" data-mcols="${esc(key)}" aria-expanded="${open}">${CHEV}<b>${esc(t.table)}</b></button>
        <small class="muted">${esc(t.kind || t.mode)}${cols.length ? `, ${cols.length} columns` : ""}</small>
        ${s ? `<button class="ref" data-node="${esc(s.id)}"><span>${esc(sortKey(s.id))}</span></button>` : '<span class="muted">no lakehouse source</span>'}</div>
        ${open ? `<div class="mt-cols">${modelColsHtml(t, s, only)}</div>` : ""}</li>`;
    }
    return html + "</ul>";
  }
  function modelColsHtml(t, src, only) {
    const mcols = t.columns || [], bySource = new Map(mcols.filter(c => c.source).map(c => [c.source.toLowerCase(), c]));
    let rows;
    if (only) rows = mcols.filter(c => !c.hidden).map(c => ({name: c.name, type: c.dataType, cls: "c-used", note: c.calculated ? "calculated" : c.source && c.source !== c.name ? `from ${c.source}` : ""}));
    else {
      const table = src && src.kind === "table" ? src : null, res = table && S.colCache.get(colKey(table));
      if (table && !res) { loadColumns(table); return '<p class="dd-note">Reading the source table\'s columns…</p>'; }
      if (res && (res.columns || []).length) {
        const names = new Set(res.columns.map(c => String(c.name).toLowerCase()));
        rows = res.columns.map(c => {
          const m = bySource.get(String(c.name).toLowerCase());
          return {name: c.name, type: c.type || "", cls: m ? (m.hidden ? "c-hidden" : "c-used") : "c-unused",
                  note: m ? (m.hidden ? "hidden in the model" : m.name !== c.name ? `in the model as ${m.name}` : "in the model") : "not in the model"};
        });
        for (const c of mcols) if (c.calculated || (c.source && !names.has(c.source.toLowerCase())))
          rows.push({name: c.name, type: c.dataType, cls: c.hidden ? "c-hidden" : "c-used", note: c.calculated ? "calculated in the model" : `reads ${c.source}, not in the table`});
      } else rows = mcols.map(c => ({name: c.name, type: c.dataType, cls: c.hidden ? "c-hidden" : "c-used", note: c.hidden ? "hidden in the model" : c.calculated ? "calculated" : ""}));
    }
    if (!rows.length) return `<p class="dd-note">${only ? "No visible columns in the model." : "No columns found."}</p>`;
    const visible = rows.filter(r => r.cls === "c-used").length;
    return `<p class="dd-note">${rows.length} column${rows.length === 1 ? "" : "s"}${only ? "" : `, ${visible} visible in the model`}.</p>
      <table class="tbl cols">${rows.map(r => `<tr class="${r.cls}"><td>${esc(r.name)}</td><td class="num">${esc(r.type)}</td><td>${esc(r.note)}</td></tr>`).join("")}</table>`;
  }
  // Columns on the map for one table, or for all the tables of a model: Off / All / In use
  function colsMapCtl(ids, modelId = "") {
    const modes = ids.map(id => S.colsOnMap.get(id) || "off"), mode = modes.every(m => m === modes[0]) ? modes[0] : "";
    const used = ids.some(id => [...modelUse(id).values()].some(u => !u.hidden));
    const b = (m, label, title, dis = false) => `<button data-colsmap="${m}" ${modelId ? `data-model="${esc(modelId)}"` : ""} aria-pressed="${mode === m}" ${dis ? "disabled" : ""} title="${title}">${label}</button>`;
    return `<div class="thread-ctl"><span>${modelId ? "Columns of its tables on the map" : "Columns on the map"}</span><div class="seg" role="group" aria-label="Columns on the map">
      ${b("off", "Off", "Don't show columns on the map")}${b("all", "All", "Show all the columns on the map")}
      ${b("used", "In use", used ? "Only the columns a semantic model shows (columns it hides are left out)" : "No semantic model read so far shows these columns", !used)}</div></div>`;
  }
  function threadCtl() {
    const b = (dir, label, title) => `<button data-thread="${dir}" aria-pressed="${S.threadDir === dir}" title="${title}">${label}</button>`;
    return `<div class="thread-ctl"><span>Follow</span><div class="seg" role="group" aria-label="Which way to follow it">
      ${b("in", "Inputs", "Where it comes from: what writes it, everything those read and write, and on upstream")}
      ${b("both", "Both ways", "What writes and reads it, everything those read and write, then upstream and downstream")}
      ${b("out", "Outputs", "Where it goes: what reads it, everything those read and write, and on downstream")}</div>
      <button class="btn small" data-act="focusnow" title="Show only what is highlighted; your tickboxes don't change">${S.focusMode ? "Leave focus" : "Focus"}</button></div>`;
  }
  function tableDetails(n) {
    const p = S.nodes.get(n.parent);
    let html = head(n, n.kind === "table" ? "Table" : "Folder") + (S.sel === n.id ? threadCtl() : "") + (n.kind === "table" ? colsMapCtl([n.id]) : "");
    const pRead = p && p.itemId && S.scans.has(p.itemId);
    if (n.kind === "table") {
      const listed = n.listed ? "Yes" : pRead ? "No. The code names it, but the lakehouse doesn't list it (it may be created at run time, or be misspelled)." : "Unknown until the lakehouse is read.";
      html += `<dl class="kv"><dt>Lakehouse</dt><dd>${p ? `<button class="ref" data-node="${esc(p.id)}"><span>${esc(p.label)}</span></button>` : "?"}</dd>${n.schema ? `<dt>Schema</dt><dd>${esc(n.schema)}</dd>` : ""}<dt>In the lakehouse list</dt><dd>${esc(listed)}</dd>${n.format ? `<dt>Format</dt><dd>${esc(n.format)}</dd>` : ""}</dl>`;
      if (p && p.itemId && !pRead && !p.external) html += `<div class="d-actions"><button class="btn small" data-act="rescan" data-item="${esc(p.itemId)}">Read ${esc(p.label)}</button></div>`;
    } else if (p) html += `<dl class="kv"><dt>Lakehouse</dt><dd><button class="ref" data-node="${esc(p.id)}"><span>${esc(p.label)}</span></button></dd></dl>`;
    const nbs = [...S.nodes.values()].filter(m => m.kind === "item" && !m.external && m.type === "Notebook");
    const readNb = nbs.filter(m => S.scans.has(m.itemId)).length;
    if (readNb < nbs.length) html += `<p class="d-note">Based on the ${readNb} of ${nbs.length} notebooks read so far. <button class="btn small" data-act="readall" data-type="Notebook">Read the other ${nbs.length - readNb}</button></p>`;
    html += relationsHtml([n.id], ["in:write", "out:read", "in:maintain", "out:model_source", "in:shortcut", "out:shortcut", "in:references"]);
    return html;
  }
  function schemaDetails(n) {
    const lh = S.nodes.get(n.parent);
    let list = [];
    if (lh) { const {tables, folders} = lakeLists(lh); list = [...tables, ...folders].filter(t => secKeyOf(t) === n.sec); }
    const onMap = list.filter(t => V.vis.has(t.id)).length, files = n.sec === "files";
    return `<div class="d-type"><i style="background:${esc(lakeColor(lh))}"></i>${files ? "Files folder" : n.sec === "t:Tables" ? "Tables folder" : "Schema"}</div><h2 class="d-title">${esc((lh ? lh.label + "." : "") + n.label)}</h2>
      <div class="d-actions"><button class="btn" data-act="hide" data-id="${esc(n.id)}">Hide from map</button></div>
      <p class="d-status">${list.length} ${files ? "folders" : "tables"}, ${onMap} on the map.</p>
      <ul class="refs">${list.map(t => `<li><div class="ref-line">${visBox(t.id, t.label)}<button class="ref" data-node="${esc(t.id)}"><span>${esc(t.kind === "file" ? t.label.replace(/^Files\//, "") : (t.table || t.label))}</span></button>${t.kind === "table" ? `<button class="ev-toggle" data-act="columns" data-id="${esc(t.id)}">Columns</button>` : ""}</div></li>`).join("")}</ul>`;
  }
  function groupDetails(g) {
    const members = g.members.map(id => S.nodes.get(id)).filter(Boolean).sort((a, b) => a.label.localeCompare(b.label, undefined, {numeric: true}));
    const unread = members.filter(m => m.scannable && !S.scans.has(m.itemId));
    const allOn = groupAllOn(g);
    return `<div class="d-type">${S.groupMode === "folder" ? "Folder" : "Item type"}</div><h2 class="d-title">${esc(g.label)}</h2>
      <div class="d-actions">${unread.length ? `<button class="btn primary" data-act="readgroup" data-id="${esc(g.id)}">Read all ${unread.length}</button>` : ""}
      ${readIds(members).length ? `<button class="btn" data-act="refreshgroup" data-id="${esc(g.id)}" title="Read them all again from Fabric">Refresh all ${readIds(members).length}</button>` : ""}
      ${readIds(members).length ? `<button class="btn" data-act="unloadgroup" data-id="${esc(g.id)}" title="Forget what was read for all of them">Unload all ${readIds(members).length}</button>` : ""}
      <button class="btn" data-act="groupmap" data-id="${esc(g.id)}">${allOn ? "Take all off the map" : "Put all on the map"}</button></div>
      <p class="d-status">${members.length} items, ${members.filter(m => S.scans.has(m.itemId)).length} read.</p>
      <ul class="refs">${members.map(m => { const lake = m.type === "Lakehouse", k = `group|${m.id}`, open = lake && S.ddOpen.has(k); return `<li><div class="ref-line">${visBox(m.id, m.label)}<span class="st ${stClass(m)}"></span><button class="ref" data-node="${esc(m.id)}"><span>${esc(m.label)}</span></button>${lake ? `<button class="dd" data-dd="${esc(k)}" data-lh="${esc(m.id)}" aria-expanded="${open}" title="${open ? "Hide" : "Show"} its tables and folders">${CHEV}</button>` : ""}${S.scans.get(m.itemId) ? `<span class="muted" style="margin-left:auto;font-size:11.5px">${esc((S.scans.get(m.itemId).summary || "").slice(0, 60))}</span>` : ""}</div>${open ? lakeDropdown(m, k) : ""}</li>`; }).join("")}</ul>`;
  }
  function edgeDetails() {
    const e = V.edges.find(x => x.id === S.selEdge);
    if (!e) return overview();
    const s = S.nodes.get(e.source), t = S.nodes.get(e.target), st = EDGE[e.kind];
    const ev = e.members.flatMap(m => (m.evidence || []).map(x => ({...x, pair: linkSentence(m.kind, S.nodes.get(m.source) || {}, S.nodes.get(m.target) || {})})));
    void st;
    return `<div class="d-type">Link</div><h2 class="d-title">${esc(linkSentence(e.kind, s, t))}</h2>
      <div class="d-actions"><button class="btn" data-node="${esc(s.id)}">Select ${esc(s.label)}</button><button class="btn" data-node="${esc(t.id)}">Select ${esc(t.label)}</button></div>
      <div class="d-sec">Where it was found <small>${ev.length}</small></div>
      <div class="ev">${ev.map(x => `<div>${esc(x.where || "")}${x.verb ? `, ${esc(x.verb)}` : ""}${e.members.length > 1 ? `, ${esc(x.pair)}` : ""}</div>${x.code ? `<code>${esc(x.code)}</code>` : ""}`).join("") || '<div>Notebook settings.</div>'}</div>`;
  }
  function renderDetails() {
    S.ddSecIds = new Map();
    renderDetailsInner();
    if (S.ws && !S.selEdge && (S.multi.size > 1 || (S.sel && S.nodes.has(S.sel)))) {
      const html = focusSection(S.multi.size > 1 ? new Set(S.multi) : new Set([S.sel]));
      if (html) $("#detailsBody").insertAdjacentHTML("beforeend", html);
    }
    const pn = S.pinned && S.nodes.get(S.pinned);
    if (pn && S.ws && S.sel !== S.pinned)                      // the highlight belongs to another item
      $("#detailsBody").insertAdjacentHTML("afterbegin", `<div class="pin-note">${PIN_SVG}<span>The highlight is pinned on <button class="ref" data-node="${esc(pn.id)}"><span>${esc(pn.fullName || pn.label)}</span></button></span><button class="btn small" data-act="unpin">Unpin</button></div>`);
    markFocus(); markHover();
    for (const el of $("#detailsBody").querySelectorAll('[data-mixed="true"]')) el.indeterminate = true;
  }
  // Everything lit on the map by the current selection that the lists above don't already cover:
  // the whole upstream / downstream chain, items first, then tables and folders.
  function focusSection(exclude) {
    const act = S.active;
    if (!act || act.size < 2) return "";
    const order = n => n.kind === "item" ? 0 : isTable(n) ? 1 : 2;
    const list = [...act].filter(id => !exclude.has(id)).map(id => S.nodes.get(id))
      .filter(n => n && n.kind !== "group" && V.vis.has(n.id))
      .sort((a, b) => order(a) - order(b) || String(a.type).localeCompare(String(b.type)) || byLabel(a, b));
    if (!list.length) return "";
    return `<details class="focus-list"${S.focusOpen ? " open" : ""}><summary class="d-sec">Highlighted on the map <small>${list.length}${S.pinned ? `, pinned on ${esc((S.nodes.get(S.pinned) || {}).label || "")}` : ""}</small></summary>
      <p class="focus-note">Names in <b><i>bold italic</i></b> anywhere in this panel are highlighted on the map.</p>
      <ul class="refs">${list.map(n => refRow(n.id, [], "", "focus")).join("")}</ul></details>`;
  }
  // Rows whose item is highlighted on the map: one size larger, bold and italic (see app.css)
  function markFocus() {
    const box = $("#detailsBody"), act = S.active;
    for (const el of box.querySelectorAll(".focus")) el.classList.remove("focus");
    if (!act) return;
    for (const el of box.querySelectorAll("[data-node]")) {
      const id = el.dataset.node;
      if (act.has(id) && V.vis.has(id)) (el.closest("li, tr") || el).classList.add("focus");
    }
  }
  // The row of the item under the mouse, whether the mouse is on the map or in the panel
  function markHover() {
    const box = $("#detailsBody");
    for (const el of box.querySelectorAll(".hl")) el.classList.remove("hl");
    if (!S.hover) return;
    for (const el of box.querySelectorAll("[data-node]")) if (el.dataset.node === S.hover) (el.closest("li, tr") || el).classList.add("hl");
  }
  // several nodes selected (selection box, or Shift+click)
  function multiDetails() {
    const list = [...S.multi].map(id => S.nodes.get(id)).filter(Boolean)
      .sort((a, b) => String(a.kind).localeCompare(String(b.kind)) || String(a.type).localeCompare(String(b.type)) || byLabel(a, b));
    const counts = new Map();
    for (const n of list) { const t = isTable(n) ? (n.kind === "table" ? "table" : "folder") : n.kind === "group" ? "group" : n.kind === "schema" ? "schema" : typeName(n.type).toLowerCase(); counts.set(t, (counts.get(t) || 0) + 1); }
    const what = [...counts.entries()].map(([t, c]) => `${c} ${t}${c === 1 ? "" : "s"}`).join(", ");
    const unread = list.filter(n => n.kind === "item" && !n.external && n.scannable && !S.scans.has(n.itemId)).length;
    return `<div class="d-type">Selection</div><h2 class="d-title">${list.length} selected</h2>
      <p class="d-status">${esc(what)}. Drag any of them to move them together; Shift+click adds or removes one.</p>
      <div class="d-actions"><button class="btn" data-act="zoomsel">Zoom to them</button><button class="btn" data-act="focusnow" title="Show only what is highlighted; your tickboxes don't change">${S.focusMode ? "Leave focus" : "Focus on the highlighted"}</button>
        <button class="btn" data-act="hidesel">Take off the map</button>${unread ? `<button class="btn" data-act="readsel">Read the ${unread} not read yet</button>` : ""}
        ${readIds(list).length ? `<button class="btn" data-act="unloadsel">Unload the ${readIds(list).length} read</button>` : ""}
        <button class="btn" data-act="clearsel">Clear selection</button></div>
      <ul class="refs">${list.map(n => refRow(n.id, [], "", "sel")).join("")}</ul>`;
  }
  function onlySelection() {
    const keep = new Set(S.multi);
    for (const n of S.nodes.values()) if (n.kind === "item" && !n.external) onMap(n.id, false);
    for (const id of keep) {
      const n = S.nodes.get(id);
      if (!n) continue;
      if (isTable(n)) { onMap(id, true); if (n.parent) onMap(n.parent, true); }
      else if (n.kind === "item") showWithLakes(id);
      else onMap(id, true);
    }
    afterVisibility();
    setTimeout(() => fit(true, keep), 60);
  }
  function renderAll() { renderTree(); renderDetails(); renderTitle(); renderColumns(); requestDraw(); }

  // ---- empty states
  function showEmpty(mode, err) {
    const box = $("#empty");
    box.hidden = false;
    $("#titleblock").hidden = true;
    if (mode === "loading") { box.innerHTML = `<div class="empty-card"><div class="spinner"></div><h1>Reading the workspace</h1><p>Fetching its list of items. Nothing inside them is read until you select one.</p></div>`; return; }
    if (mode === "nokey") { box.innerHTML = `<div class="empty-card"><h1>Open the address the app printed</h1><p>This page only works at the address Fabric Explorer prints when it starts (it ends with <code>#k=…</code>). Start it with <code>python run.py</code>.</p></div>`; return; }
    const errHtml = mode === "error" ? `<div class="error-box"><strong>${esc(err.message)}</strong>${err.hint ? `<div>${esc(err.hint)}</div>` : ""}</div>` : "";
    box.innerHTML = `<div class="empty-card">
      <h1>${mode === "error" ? "That workspace couldn't be opened" : "Open a workspace"}</h1>${errHtml}
      <p>Open the workspace in Fabric or Power BI, copy the address from the browser, and paste it here. It's read with your own sign-in.</p>
      <div class="paste-row"><input id="emptyLink" type="text" spellcheck="false" autocomplete="off" placeholder="https://app.fabric.microsoft.com/groups/…" aria-label="Workspace address"><button id="emptyGo" class="btn primary">Open</button></div>
      <p id="emptyMsg" class="msg" role="status"></p>
      <label class="pop-label" for="emptyFilter">Or choose one you can access</label>
      <input id="emptyFilter" class="field" type="search" placeholder="Filter workspaces" autocomplete="off" style="width:100%">
      <ul id="emptyList" class="ws-list"></ul></div>`;
    const input = $("#emptyLink"), msg = $("#emptyMsg");
    $("#emptyGo").onclick = () => openLink(input.value, msg);
    input.onkeydown = ev => { if (ev.key === "Enter") openLink(input.value, msg); };
    input.onpaste = () => setTimeout(() => openLink(input.value, msg), 0);
    renderWsList($("#emptyList"), $("#emptyFilter"));
    setTimeout(() => input.focus(), 30);
  }
  function hideEmpty() { $("#empty").hidden = true; $("#empty").innerHTML = ""; }

  // ---- legend
  function buildLegend() {
    const line = (kind) => { const st = EDGE[kind]; return `<svg viewBox="0 0 34 12"><line x1="1" y1="6" x2="${st.arrow ? 26 : 33}" y2="6" stroke="var(--c-${st.color})" stroke-width="${st.w + 0.3}" stroke-dasharray="${st.dash.join(" ")}"/>${st.arrow ? `<path d="M33 6l-8-4v8z" fill="var(--c-${st.color})"/>` : ""}</svg>`; };
    const box = (dash, stroke) => `<svg viewBox="0 0 34 12"><rect x="3" y="1" width="28" height="10" rx="2" fill="var(--c-card)" stroke="var(--c-${stroke})" stroke-width="1.2" stroke-dasharray="${dash}"/></svg>`;
    $("#legendGrid").innerHTML = [
      [line("read"), "Reads: data in"], [line("write"), "Writes: data out"], [line("run"), "Runs or refreshes"],
      [line("model_source"), "Feeds a model or report"], [line("shortcut"), "Shortcut"], [line("default_lakehouse"), "Default lakehouse"],
      [box("4 3", "ink"), "Not read yet"], [box("", "ink"), "Read"], [box("", "error"), "Couldn't be read"],
    ].map(([g, t]) => `${g}<span>${esc(t)}</span>`).join("");
  }

  // ---- requests log
  let logTimer = 0;
  async function refreshLog() {
    try {
      const r = await api(`/api/log?since=${S.logLast}`);
      if (r.entries.length) { S.logRows = r.entries.reverse().concat(S.logRows).slice(0, 300); S.logLast = r.last; }
      $("#logCount").textContent = r.last;
      if (!$("#logBody").hidden) renderLog();
    } catch { /* the page shows errors where they matter */ }
  }
  function renderLog() {
    $("#logRows").innerHTML = S.logRows.map(e => `<tr><td>${esc(new Date(e.t * 1000).toLocaleTimeString())}</td><td>${esc(e.api)}</td><td>${esc(e.method)}</td><td class="${e.status >= 400 || e.status === 0 ? "bad" : ""}">${esc(e.status || "failed")}</td><td>${esc(e.ms)} ms</td><td class="path">${esc(e.path)}</td></tr>`).join("");
  }

  // ---- search
  let searchHits = [], searchOn = -1;
  function runSearch() {
    const q = $("#search").value.trim().toLowerCase(), list = $("#searchList");
    if (!q || !S.ws) { list.hidden = true; return; }
    const pool = [...S.nodes.values()].filter(n => n.kind !== "group");
    searchHits = pool.filter(n => (n.fullName || n.label).toLowerCase().includes(q))
      .sort((a, b) => (a.label.toLowerCase().startsWith(q) ? 0 : 1) - (b.label.toLowerCase().startsWith(q) ? 0 : 1) || a.label.localeCompare(b.label)).slice(0, 14);
    searchOn = searchHits.length ? 0 : -1;
    list.innerHTML = searchHits.length ? searchHits.map((n, i) => `<li><button data-hit="${i}" class="${i === searchOn ? "on" : ""}"><span class="st ${n.kind === "item" ? stClass(n) : "ok"}" style="${n.kind !== "item" ? `background:${esc(colorOf(n))}` : ""}"></span><span>${esc(n.fullName || n.label)}</span><small>${esc(n.kind === "item" ? typeName(n.type) : n.kind === "table" ? "Table" : n.kind === "file" ? "Folder" : "")}</small></button></li>`).join("")
      : '<li class="muted" style="padding:8px">Nothing found. Tables appear once something that uses them has been read.</li>';
    list.hidden = false;
  }
  function pickHit(i) { const n = searchHits[i]; if (!n) return; $("#searchList").hidden = true; $("#search").value = ""; reveal(n.id); }

  // ---- export
  function download(name, text, type) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], {type}));
    a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }
  function exportMap(kind) {
    if (!S.ws) return;
    const safe = S.ws.name.replace(/[^\w.-]+/g, "_");
    const nm = id => { const n = S.nodes.get(id); return n ? (n.fullName || n.label) : id; };
    const tp = id => { const n = S.nodes.get(id); return n ? (n.kind === "item" ? n.type : n.kind) : ""; };
    if (kind === "csv") {
      const q = v => `"${String(v ?? "").replace(/"/g, '""')}"`;
      const rows = [["from_type", "from", "relationship", "to_type", "to", "found_in", "how", "code"]];
      for (const e of S.edges.values()) {
        const evs = e.evidence && e.evidence.length ? e.evidence : [{}];
        for (const ev of evs) rows.push([tp(e.source), nm(e.source), e.kind, tp(e.target), nm(e.target), ev.where || "", ev.verb || "", ev.code || ""]);
      }
      download(`${safe}_relationships.csv`, "\ufeff" + rows.map(r => r.map(q).join(",")).join("\r\n"), "text/csv");
    } else {
      const nodes = [...S.nodes.values()].filter(n => n.kind !== "group").map(({x, y, vx, vy, w, h, fixed, dataLinked, anchor, color, ...rest}) => rest);
      download(`${safe}_map.json`, JSON.stringify({workspace: S.ws, exportedAt: new Date().toISOString(), nodes, edges: [...S.edges.values()].map(({from, ...e}) => ({...e, readFrom: from}))}, null, 2), "application/json");
    }
  }

  // ---- pop-overs
  // ---- right-click menu on map items. To add an action, add an entry to menuItems().
  const menu = $("#ctxMenu");
  function menuItems(n) {
    const many = S.multi.size > 1 && S.multi.has(n.id), items = [];
    if (!many && n.kind === "item" && n.type === "Lakehouse") {
      if (!lakeOpen(n.id)) items.push({label: "Expand: show its datasets", run: () => expandLake(n.id)});
      if (!n.external) items.push({label: "Show its folders only", run: () => expandLake(n.id, true)});
      if (lakeData(n.id).some(d => V.vis.has(d.id)) || S.folderMode.has(n.id)) items.push({label: "Collapse: hide its datasets", run: () => collapseLake(n.id)});
      items.push({sep: true});
    }
    if (!many && n.kind === "schema") {
      items.push({label: folderOpen(n.id) ? "Collapse: hide its datasets" : "Expand: show its datasets", run: () => toggleNode(n)});
      items.push({sep: true});
    }
    if (!many && isTable(n)) {
      const cur = S.sel === n.id ? S.threadDir : null;
      for (const [dir, label] of [["both", "Show its thread: inputs and outputs"], ["in", "Show where it comes from (inputs)"], ["out", "Show where it goes (outputs)"]])
        items.push({label, on: cur === dir, run: () => { S.threadDir = dir; store.set("threadDir", dir); revealLineage(n.id); }});
      items.push({sep: true});
    } else if (!many && n.kind !== "group") { items.push({label: "Show its lineage", run: () => revealLineage(n.id)}); items.push({sep: true}); }
    if (!many && n.kind !== "group") {
      if (S.pinned === n.id) items.push({label: "Unpin its highlight", run: unpinHighlight});
      else items.push({label: "Pin its highlight (stays while you click others)", run: () => pinHighlight(n.id)});
    }
    if (S.pinned && S.pinned !== n.id) items.push({label: `Unpin the highlight on ${(S.nodes.get(S.pinned) || {}).label || "?"}`, run: unpinHighlight});
    if (S.focusMode) items.push({label: "Leave focus mode", run: leaveFocus});
    else items.push({label: "Focus on what's highlighted", run: () => { if (!S.active || !S.active.has(n.id)) select(n.id, false); enterFocus(); }});
    items.push({sep: true});
    items.push({label: many ? `Remove the ${S.multi.size} selected from view` : "Remove from view", run: () => removeFromView(many ? [...S.multi] : [n.id])});
    const toUnload = readIds(many ? [...S.multi].map(id => S.nodes.get(id)) : [n]);
    if (toUnload.length) items.push({label: many ? `Refresh the ${toUnload.length} read ones` : "Refresh (read it again from Fabric)", run: () => refreshItems(toUnload)});
    else if (!many && isTable(n)) {
      const p = S.nodes.get(n.parent);
      if (p && p.kind === "item" && !p.external && S.scans.has(p.itemId)) items.push({label: `Refresh its lakehouse (${p.label})`, run: () => refreshItems([p.itemId])});
    }
    if (toUnload.length) items.push({label: many ? `Unload the ${toUnload.length} read ones` : "Unload (back to not read)", run: () => unload(toUnload)});
    if (!many && n.url) items.push({label: "Open in Fabric", run: () => openUrl(n)});
    return items;
  }
  function removeFromView(ids) {
    for (const id of ids) {
      const n = S.nodes.get(id);
      if (!n) continue;
      if (n.kind === "group") { for (const m of n.members) { const it = S.nodes.get(m); if (it && it.type === "Lakehouse") resetLake(m); onMap(m, false); } S.expanded.delete(id); }
      else if (n.kind === "schema") S.folderVis.set(id, false);
      else if (n.kind === "item" && n.type === "Lakehouse" && !n.external) { resetLake(id); onMap(id, false); }
      else onMap(id, false);
    }
    if (ids.some(isSel)) { S.sel = null; S.multi = new Set(); S.selEdge = null; }
    afterVisibility();
  }
  function openMenu(n, x, y) {
    const items = menuItems(n);
    menu._items = items;
    menu.innerHTML = `<div class="ctx-title">${esc(n.fullName || n.label)}</div>` +
      items.map((it, i) => it.sep ? '<hr class="ctx-sep">' : `<button role="${it.on === undefined ? "menuitem" : "menuitemradio"}" ${it.on !== undefined ? `aria-checked="${it.on}"` : ""} data-mi="${i}"><span class="ctx-check">${it.on ? "\u2713" : ""}</span>${esc(it.label)}</button>`).join("");
    menu.hidden = false;
    const r = $("#stage").getBoundingClientRect();
    menu.style.left = `${Math.max(4, Math.min(x - r.left, r.width - menu.offsetWidth - 6))}px`;
    menu.style.top = `${Math.max(4, Math.min(y - r.top, r.height - menu.offsetHeight - 6))}px`;
    hideTip();
    const first = menu.querySelector("button");
    if (first) first.focus();
  }
  function closeMenu() { if (!menu.hidden) { menu.hidden = true; menu.innerHTML = ""; } }
  menu.addEventListener("click", ev => {
    const b = ev.target.closest("[data-mi]");
    if (!b) return;
    const it = menu._items[+b.dataset.mi];
    closeMenu(); it.run();
  });
  menu.addEventListener("keydown", ev => {
    const list = [...menu.querySelectorAll("button")], i = list.indexOf(document.activeElement);
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); list[(i + (ev.key === "ArrowDown" ? 1 : -1) + list.length) % list.length].focus(); }
    else if (ev.key === "Escape") { closeMenu(); canvas.focus(); }
  });
  document.addEventListener("pointerdown", ev => { if (!menu.hidden && !ev.target.closest("#ctxMenu")) closeMenu(); }, true);
  window.addEventListener("blur", closeMenu);
  function closePops(except) {
    closeMenu();
    for (const [btn, pop] of [["#wsButton", "#wsPop"], ["#viewBtn", "#viewMenu"], ["#exportBtn", "#exportMenu"]]) {
      if (pop === except) continue;
      $(pop).hidden = true; $(btn).setAttribute("aria-expanded", "false");
    }
    if (except !== "#searchList") $("#searchList").hidden = true;
  }
  function togglePop(btn, pop) {
    const open = $(pop).hidden;
    closePops(pop);
    $(pop).hidden = !open; $(btn).setAttribute("aria-expanded", String(open));
    return open;
  }

  // ============================================================ wiring
  function bind() {
    $("#wsButton").onclick = () => {
      if (togglePop("#wsButton", "#wsPop")) { $("#wsLinkMsg").textContent = ""; setTimeout(() => $("#wsLink").focus(), 20); renderWsList($("#wsList"), $("#wsFilter")); }
    };
    $("#wsLinkGo").onclick = () => openLink($("#wsLink").value, $("#wsLinkMsg"));
    $("#wsLink").onkeydown = ev => { if (ev.key === "Enter") openLink($("#wsLink").value, $("#wsLinkMsg")); };
    $("#wsLink").onpaste = () => setTimeout(() => openLink($("#wsLink").value, $("#wsLinkMsg")), 0);
    document.addEventListener("click", ev => {
      const ws = ev.target.closest("[data-ws]");
      if (ws) { openWorkspace(ws.dataset.ws); return; }
      if (!ev.target.closest(".ws, .menu, .search")) closePops();
    });
    $("#reload").onclick = () => { if (S.ws) openWorkspace(S.ws.id, "", true); };
    $("#fit").onclick = () => fit();
    $("#focusBtn").onclick = () => { if (S.focusMode) leaveFocus(); else enterFocus(); };
    $("#focusLeave").onclick = () => leaveFocus();
    $("#pinOff").onclick = () => unpinHighlight();
    $("#relayout").onclick = () => { if (S.layout === "struct") { S.offsets.clear(); S.focusIds = null; S.focusSig = ""; layoutStructured(true); return; } for (const n of S.nodes.values()) n.fixed = false; reheat(1); };
    for (const b of document.querySelectorAll("[data-layout]")) b.onclick = () => setLayout(b.dataset.layout);
    markLayout();
    $("#viewBtn").onclick = () => togglePop("#viewBtn", "#viewMenu");
    $("#exportBtn").onclick = () => togglePop("#exportBtn", "#exportMenu");
    $("#exportMenu").onclick = ev => { const b = ev.target.closest("[data-export]"); if (b) { exportMap(b.dataset.export); closePops(); } };
    $("#theme").onclick = () => applyTheme(document.documentElement.dataset.theme === "blueprint" ? "sheet" : "blueprint");
    for (const box of document.querySelectorAll("[data-opt]")) { box.checked = !!S.opts[box.dataset.opt]; box.onchange = () => setOpt(box.dataset.opt, box.checked); }
    for (const b of document.querySelectorAll("[data-hl]")) {
      b.setAttribute("aria-pressed", String(b.dataset.hl === S.hl));
      b.onclick = () => { S.hl = b.dataset.hl; store.set("hl", S.hl); for (const o of document.querySelectorAll("[data-hl]")) o.setAttribute("aria-pressed", String(o === b)); computeActive(); renderDetails(); requestDraw(); refocus(); };
    }
    for (const b of document.querySelectorAll("[data-group]")) {
      b.setAttribute("aria-pressed", String(b.dataset.group === S.groupMode));
      b.onclick = () => { for (const o of document.querySelectorAll("[data-group]")) o.setAttribute("aria-pressed", String(o === b)); setGroupMode(b.dataset.group); };
    }
    $("#treeFilter").oninput = ev => { S.treeFilter = ev.target.value.trim().toLowerCase(); renderTree(); };
    $("#treeBody").addEventListener("change", ev => {
      const v = ev.target.closest("[data-vis]");
      if (v) { setNodeOnMap(v.dataset.vis, v.checked); return; }
      const sv = ev.target.closest("[data-secvis]");
      if (sv) { setDataOnMap(sectionIds(sv.dataset.secvis), sv.checked); return; }
      const fo = ev.target.closest("[data-folders]");
      if (fo) { setFolders(fo.dataset.folders, fo.checked); return; }
      const fv = ev.target.closest("[data-foldvis]");
      if (fv) { S.folderVis.set(fv.dataset.foldvis, fv.checked); afterVisibility(); return; }
      const g = ev.target.closest("[data-gvis]");
      if (g) { setGroupOnMap(g.dataset.gvis, g.checked); return; }
      const c = ev.target.closest("[data-contents]");
      if (c) setContents(c.dataset.contents, c.checked);
    });
    $("#treeBody").onclick = ev => {
      if (ev.target.closest("input, label.tick")) return;            // tickboxes act on change
      const dd = ev.target.closest("[data-tdd]");
      if (dd) {
        const id = dd.dataset.tdd, lh = S.nodes.get(id);
        if (S.treeDD.has(id)) S.treeDD.delete(id);
        else {
          S.treeDD.add(id);
          if (lh && lh.kind === "item" && !lh.external && lh.scannable && !S.scans.has(lh.itemId) && !S.busy.has(lh.itemId)) { S.quietReads.add(lh.itemId); enqueue(lh.itemId); }
        }
        renderTree(); return;
      }
      const t = ev.target.closest("[data-tgroup]");
      if (t) { const id = t.dataset.tgroup; S.treeOpen.has(id) ? S.treeOpen.delete(id) : S.treeOpen.add(id); renderTree(); return; }
      const sh = ev.target.closest("[data-sub]");
      if (sh) { S.subState.set(sh.dataset.sub, sh.dataset.open !== "true"); renderTree(); return; }
      const cb = ev.target.closest("[data-cols]");
      if (cb) { select(cb.dataset.cols, false); showColumns(cb.dataset.cols); return; }
      const r = ev.target.closest("[data-node]");
      if (r) reveal(r.dataset.node);
    };
    for (const b of document.querySelectorAll("[data-tab]")) b.onclick = () => setLeftTab(b.dataset.tab);
    $("#detailsBody").addEventListener("mouseover", ev => {
      const el = ev.target.closest("[data-node]"), id = el && V.vis.has(el.dataset.node) ? el.dataset.node : null;
      if (id !== S.hover) { S.hover = id; markHover(); requestDraw(); }
    });
    $("#detailsBody").addEventListener("mouseleave", () => { if (S.hover) { S.hover = null; markHover(); requestDraw(); } });
    $("#detailsBody").addEventListener("toggle", ev => { if (ev.target.classList && ev.target.classList.contains("focus-list")) S.focusOpen = ev.target.open; }, true);
    $("#detailsBody").addEventListener("change", ev => {
      const v = ev.target.closest("[data-vis]");
      if (v) { setNodeOnMap(v.dataset.vis, v.checked); return; }
      if (ev.target.matches("[data-modelonly]")) { S.modelColsVisibleOnly = ev.target.checked; store.set("modelColsVisibleOnly", ev.target.checked); renderDetails(); return; }
      const sv = ev.target.closest("[data-ddsecvis]");
      if (sv) setDataOnMap(S.ddSecIds.get(sv.dataset.ddsecvis) || [], sv.checked);
    });
    $("#dsBody").addEventListener("change", ev => {
      const v = ev.target.closest("[data-vis]");
      if (v) { setNodeOnMap(v.dataset.vis, v.checked); return; }
      const g = ev.target.closest("[data-dsgroup]");
      if (g) setDataOnMap((datasetView().groups.get(g.dataset.dsgroup) || []).map(n => n.id), g.checked);
    });
    $("#dsBody").onclick = ev => {
      if (ev.target.closest("input")) return;
      const b = ev.target.closest("[data-dsscope], [data-dsfilter], [data-dsall], [data-readall], [data-node]");
      if (!b) return;
      if (b.dataset.dsscope) { S.dsScope = b.dataset.dsscope; store.set("dsScope", S.dsScope); renderDatasets(); }
      else if (b.dataset.dsfilter) { S.dsFilter = b.dataset.dsfilter; renderDatasets(); }
      else if (b.dataset.dsall) setDataOnMap(datasetView().listed.map(n => n.id), b.dataset.dsall === "1");
      else if (b.dataset.readall) readAll([...S.nodes.values()].filter(n => n.kind === "item" && !n.external && n.type === b.dataset.readall && n.scannable).map(n => n.itemId));
      else if (b.dataset.node) reveal(b.dataset.node);
    };
    for (const b of document.querySelectorAll("[data-all]")) b.onclick = () => showAllItems(b.dataset.all === "1");
    $("#colsBody").onclick = ev => {
      const cb = ev.target.closest("[data-cols]");
      if (cb) { S.colTarget = cb.dataset.cols; renderColumns(); return; }
      const a = ev.target.closest("[data-cact]");
      if (!a) return;
      const n = S.colTarget && S.nodes.get(S.colTarget);
      if (a.dataset.cact === "copy") copyColumns();
      else if (a.dataset.cact === "refresh" && n) loadColumns(n, true);
    };
    $("#colsBody").addEventListener("input", ev => {
      if (ev.target.id !== "colFilter") return;
      const q = ev.target.value.trim().toLowerCase();
      for (const li of document.querySelectorAll("#colList li")) li.hidden = !!q && !li.textContent.toLowerCase().includes(q);
    });
    $("#detailsBody").onclick = ev => {
      if (ev.target.closest("input")) return;
      const th = ev.target.closest("[data-thread]");
      if (th) { setThreadDir(th.dataset.thread); return; }
      const cm = ev.target.closest("[data-colsmap]");
      if (cm) { setColsOnMap(cm.dataset.model ? modelSourceTables(cm.dataset.model) : [S.sel], cm.dataset.colsmap); return; }
      const mc = ev.target.closest("[data-mcols]");
      if (mc) { S.modelColsOpen.set(mc.dataset.mcols, mc.getAttribute("aria-expanded") !== "true"); renderDetails(); return; }
      const dd = ev.target.closest("[data-dd]");
      if (dd) {
        const k = dd.dataset.dd, lh = S.nodes.get(dd.dataset.lh);
        if (S.ddOpen.has(k)) S.ddOpen.delete(k);
        else {
          S.ddOpen.add(k);
          if (lh && lh.kind === "item" && !lh.external && lh.scannable && !S.scans.has(lh.itemId) && !S.busy.has(lh.itemId)) { S.quietReads.add(lh.itemId); enqueue(lh.itemId); }
        }
        renderDetails(); return;
      }
      const ds = ev.target.closest("[data-ddsec]");
      if (ds) { S.ddSec.set(ds.dataset.ddsec, ds.dataset.open !== "true"); renderDetails(); return; }
      const evb = ev.target.closest("[data-ev]");
      if (evb) { const box = document.getElementById(evb.dataset.ev); if (box) { box.hidden = !box.hidden; evb.setAttribute("aria-expanded", String(!box.hidden)); } return; }
      const a = ev.target.closest("[data-act]");
      if (a) {
        const act = a.dataset.act;
        if (act === "rescan") enqueue(a.dataset.item, S.scans.has(a.dataset.item));
        else if (act === "readall") readAll([...S.nodes.values()].filter(n => n.kind === "item" && !n.external && n.type === a.dataset.type && n.scannable).map(n => n.itemId));
        else if (act === "readgroup") readAll((S.nodes.get(a.dataset.id) || {members: []}).members.map(id => S.nodes.get(id)).filter(n => n && n.scannable).map(n => n.itemId));
        else if (act === "groupmap") toggleGroupOnMap(a.dataset.id);
        else if (act === "contents") setContents(a.dataset.id, !S.contents.has(a.dataset.id));
        else if (act === "columns") showColumns(a.dataset.id);
        else if (act === "focusnow") { if (S.focusMode) leaveFocus(); else enterFocus(); }
        else if (act === "hide") setNodeOnMap(a.dataset.id, false);
        else if (act === "show") { reveal(a.dataset.id); }
        else if (act === "zoomchain" && S.active) fit(true, S.active);
        else if (act === "zoomsel") fit(true, new Set(S.multi));
        else if (act === "onlysel") onlySelection();
        else if (act === "hidesel") { for (const id of S.multi) onMap(id, false); S.multi = new Set(); afterVisibility(); }
        else if (act === "clearsel") clearSelection();
        else if (act === "unload") unload([a.dataset.item]);
        else if (act === "refresh") refreshItems([a.dataset.item]);
        else if (act === "refreshgroup") refreshItems(readIds(((S.nodes.get(a.dataset.id) || {}).members || []).map(id => S.nodes.get(id))));
        else if (act === "reveal") revealLineage(a.dataset.id);
        else if (act === "pin") pinHighlight(a.dataset.id);
        else if (act === "unpin") unpinHighlight();
        else if (act === "unloadgroup") unload(readIds(((S.nodes.get(a.dataset.id) || {}).members || []).map(id => S.nodes.get(id))));
        else if (act === "unloadall") unload([...S.scans.keys()]);
        else if (act === "unloadsel") unload(readIds([...S.multi].map(id => S.nodes.get(id))));
        else if (act === "readsel") readAll([...S.multi].map(id => S.nodes.get(id)).filter(n => n && n.kind === "item" && !n.external && n.scannable).map(n => n.itemId));
        return;
      }
      const r = ev.target.closest("[data-node]");
      if (r) reveal(r.dataset.node);
    };
    $("#titleblock").onclick = ev => { if (ev.target.closest('[data-act="stop"]')) stopReading(); };
    $("#search").oninput = runSearch;
    $("#search").onkeydown = ev => {
      const list = $("#searchList");
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault(); if (!searchHits.length) return;
        searchOn = (searchOn + (ev.key === "ArrowDown" ? 1 : -1) + searchHits.length) % searchHits.length;
        for (const b of list.querySelectorAll("button")) b.classList.toggle("on", +b.dataset.hit === searchOn);
      } else if (ev.key === "Enter") pickHit(Math.max(searchOn, 0));
      else if (ev.key === "Escape") { list.hidden = true; ev.target.blur(); }
    };
    $("#searchList").onclick = ev => { const b = ev.target.closest("[data-hit]"); if (b) pickHit(+b.dataset.hit); };
    $("#logToggle").onclick = () => {
      const body = $("#logBody"); body.hidden = !body.hidden; $("#logToggle").setAttribute("aria-expanded", String(!body.hidden));
      clearInterval(logTimer);
      if (!body.hidden) { renderLog(); refreshLog(); logTimer = setInterval(refreshLog, 2000); }
      requestAnimationFrame(resize);
    };
    $("#toggleLeft").onclick = () => { const off = document.body.classList.toggle("no-left"); $("#toggleLeft").setAttribute("aria-pressed", String(!off)); store.set("noLeft", off); requestAnimationFrame(resize); };
    $("#toggleRight").onclick = () => { const off = document.body.classList.toggle("no-right"); $("#toggleRight").setAttribute("aria-pressed", String(!off)); store.set("noRight", off); requestAnimationFrame(resize); };
    if (store.get("noLeft", false)) { document.body.classList.add("no-left"); $("#toggleLeft").setAttribute("aria-pressed", "false"); }
    if (store.get("noRight", false)) { document.body.classList.add("no-right"); $("#toggleRight").setAttribute("aria-pressed", "false"); }
    document.addEventListener("keydown", ev => {
      const typing = ev.target.closest("input, textarea, select");
      if (ev.key === "Escape") {
        closePops();
        if (!typing) { if (S.sel || S.multi.size || S.selEdge) clearSelection(); else if (S.focusMode) leaveFocus(); else unpinHighlight(); }
        return;
      }
      if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      if (ev.code === "Space" && (ev.target === canvas || ev.target === document.body)) {
        ev.preventDefault();
        if (!S.space) { S.space = true; canvas.classList.add("spacepan"); }
        return;
      }
      if (ev.key === "/") { ev.preventDefault(); $("#search").focus(); }
      else if (ev.key === "f" || ev.key === "F") fit();
    });
    const spaceUp = () => { S.space = false; canvas.classList.remove("spacepan"); };
    document.addEventListener("keyup", ev => { if (ev.code === "Space") spaceUp(); if (ev.key === "Shift") canvas.classList.remove("boxmode"); });
    document.addEventListener("keydown", ev => { if (ev.key === "Shift" && !ev.target.closest("input, textarea, select")) canvas.classList.add("boxmode"); });
    window.addEventListener("blur", () => { spaceUp(); canvas.classList.remove("boxmode"); });
    document.addEventListener("paste", ev => {
      if (ev.target.closest("input, textarea")) return;
      const text = (ev.clipboardData && ev.clipboardData.getData("text")) || "";
      if (/\/groups\/|[0-9a-f]{8}-[0-9a-f]{4}-/i.test(text)) { ev.preventDefault(); openLink(text, null); }
    });
    new ResizeObserver(resize).observe($("#stage"));
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", e => { if (!store.get("themeChosen", false)) applyTheme(e.matches ? "blueprint" : "sheet"); });
    $("#theme").addEventListener("click", () => store.set("themeChosen", true));
  }

  function initResizer() {
    const r = $("#resizeLeft");
    if (!r) return;
    const set = w => document.documentElement.style.setProperty("--left-w", `${Math.round(w)}px`);
    const saved = store.get("leftW", 0);
    if (saved) set(saved);
    r.addEventListener("pointerdown", ev => {
      ev.preventDefault(); r.setPointerCapture(ev.pointerId); r.classList.add("on");
      const x0 = ev.clientX, w0 = $("#tree").getBoundingClientRect().width;
      const move = e => { const w = clamp(w0 + e.clientX - x0, 220, Math.max(240, Math.min(640, window.innerWidth - 520))); set(w); store.set("leftW", Math.round(w)); resize(); };
      const up = () => { r.classList.remove("on"); r.removeEventListener("pointermove", move); r.removeEventListener("pointerup", up); };
      r.addEventListener("pointermove", move); r.addEventListener("pointerup", up);
    });
    r.addEventListener("dblclick", () => { document.documentElement.style.removeProperty("--left-w"); store.set("leftW", 0); resize(); });
  }
  async function boot() {
    readColors();
    applyTheme(store.get("theme", matchMedia("(prefers-color-scheme: dark)").matches ? "blueprint" : "sheet"));
    bind(); buildLegend(); initResizer(); resize();
    if (!KEY) { showEmpty("nokey"); return; }
    try { S.hello = await api("/api/hello"); }
    catch (err) { showEmpty("error", err); return; }
    S.concurrency = S.hello.concurrency || 4;
    $("#account").textContent = S.hello.account ? (S.hello.demo ? S.hello.account : `Signed in as ${S.hello.account}`) : "";
    if (S.hello.workspaceId) openWorkspace(S.hello.workspaceId, S.hello.startItem || "");
    else showEmpty("start");
    refreshLog();
  }
  window.fabricExplorer = {S, get V() { return V; }, refreshItems, setColsOnMap, modelUse, pinHighlight, unpinHighlight, revealLineage, unload, toggleOf, expandLake, collapseLake, enterFocus, leaveFocus, setThreadDir, select, selectMany, clearSelection, reveal, setFolders, cascadeLake, setNodeOnMap, openLink, fit, setContents, setLayout, setDataOnMap, get LANES() { return LANES; }, setItemOnMap, setGroupOnMap, showAllItems, showColumns, setLeftTab,
    readType(type) { readAll([...S.nodes.values()].filter(n => n.kind === "item" && !n.external && n.type === type && n.scannable).map(n => n.itemId)); }, screenOf(id) { const n = S.nodes.get(id); return n ? {x: n.x * S.view.k + S.view.x, y: n.y * S.view.k + S.view.y} : null; }};
  boot();
})();
