"""The page in a real browser (Chromium through Playwright), on the demo workspace.

Skipped unless Playwright and its Chromium are installed:
    pip install playwright && python -m playwright install chromium
These catch what the other tests can't: a click handled by the wrong element, something covering a
button, the drawing jumping while you work in it.
"""
import pytest

pytest.importorskip("playwright.sync_api")

from fabric_explorer.demo import ID, WS, demo_client  # noqa: E402
from fabric_explorer.server import make_server, serve_in_thread  # noqa: E402
from fabric_explorer.session import Session  # noqa: E402

# every visible button, link, tab, field and summary that something else sits on top of
COVERED = """(() => {
  const out = [];
  const clipped = (el, x, y) => {
    for (let a = el.parentElement; a; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (/(auto|scroll|hidden)/.test(cs.overflow + cs.overflowX + cs.overflowY)) {
        const r = a.getBoundingClientRect();
        if (x < r.left || x > r.right || y < r.top || y > r.bottom) return true;
      }
    }
    return false;
  };
  for (const el of document.querySelectorAll('button, a[href], [role=tab], input, summary, select')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || el.closest('[hidden]') || getComputedStyle(el).visibility === 'hidden') continue;
    if (el.closest('svg.cll-svg')) continue;          // the drawing pans under the overlays: not a control
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight || clipped(el, x, y)) continue;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !(el === hit || el.contains(hit))) out.push((el.id || el.textContent.trim().slice(0, 30)) + ' under ' + (hit ? hit.id || hit.className : '-'));
  }
  return out;
})()"""
def until(pg, js, timeout=15.0):
    """Wait until a JavaScript expression is true (the page's security policy doesn't allow eval)."""
    for _ in range(int(timeout * 10)):
        if pg.evaluate(js):
            return
        pg.wait_for_timeout(100)
    raise AssertionError(f"never true: {js}")


TRANSFORM = "document.querySelector('#nllLayout [data-c=world]').getAttribute('transform')"
LIT = "document.querySelectorAll('#nllLayout .e.hl').length"


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright
    srv = make_server(Session(demo_client(latency=None, sleep=lambda s: None), default_workspace=WS), "127.0.0.1", 0)
    serve_in_thread(srv)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Playwright's Chromium isn't installed ({type(e).__name__})")
        pg = browser.new_context(viewport={"width": 1600, "height": 950}).new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(srv.url)
        until(pg, "window.fabricExplorer && window.fabricExplorer.S.ws")
        pg.errors = errors
        yield pg
        browser.close()
    srv.shutdown()
    srv.server_close()


def _open_notebook(pg, key, scope=""):
    """Open a notebook's column lineage and wait until that drawing (not the one before it) is shown."""
    drawn = "(s => s && !s.loading && s.full && s.full.focus && s.full.focus.itemId === '%s' && s.full.scope === '%s')" \
            "(window.fabricExplorer.views.notebooks.state)"
    pg.evaluate(f"window.fabricExplorer.showView('notebooks', {{mode: 'notebook', notebookId: '{ID[key]}'}})")
    until(pg, f"(s => s && !s.loading && s.full && s.full.focus && s.full.focus.itemId === '{ID[key]}')"
              "(window.fabricExplorer.views.notebooks.state)")
    current = pg.evaluate("window.fabricExplorer.views.notebooks.state.full.scope")
    if scope and scope != current:
        pg.click(f"#nllLayout [data-scope={scope}]")
    until(pg, drawn % (ID[key], scope or current))


def test_workspace_picker_opens_in_both_views(page):
    for view in ("map", "columns", "notebooks"):
        page.evaluate(f"window.fabricExplorer.showView('{view}')")
        page.click("#wsButton")
        assert page.is_visible("#wsPop"), view
        page.keyboard.press("Escape")
        assert not page.is_visible("#wsPop")
    assert page.evaluate(COVERED) == []


def test_clicking_and_dragging_keep_the_drawing_where_it_is(page):
    page.evaluate("window.fabricExplorer.readType('Notebook')")
    until(page, "window.fabricExplorer.S.busy.size === 0 && window.fabricExplorer.S.scans.size > 8")
    _open_notebook(page, "nb20")
    box = page.locator("#nllLayout [data-c=svg]").bounding_box()
    x, y = box["x"] + 420, box["y"] + box["height"] - 140
    scale = page.evaluate(TRANSFORM).split("scale")[1]
    for _ in range(2):                                   # two quick drags (the second one is a "double click")
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 60, y - 30)
        page.mouse.move(x + 120, y - 60)
        page.mouse.up()
    page.mouse.click(x, y)
    page.wait_for_timeout(350)
    assert page.evaluate(TRANSFORM).split("scale")[1] == scale     # moved, not fitted back to everything
    page.evaluate("window.fabricExplorer.showView('map')")
    before = page.evaluate(TRANSFORM)
    page.evaluate("window.fabricExplorer.showView('notebooks')")
    page.wait_for_timeout(250)
    assert page.evaluate(TRANSFORM) == before                      # back to the tab: where you left it
    assert page.evaluate(COVERED) == []


def test_a_clicked_column_stays_lit_and_the_frame_names_its_notebook(page):
    _open_notebook(page, "nb20")
    page.click("#nllLayout [data-act=fit]")                       # everything in view, as a user would
    page.wait_for_timeout(400)
    page.locator('#nllLayout g.row[data-port$="|tagno"]').last.click()
    lit = page.evaluate(LIT)
    assert lit > 0
    frame = page.locator("#nllLayout .nb-frame rect").bounding_box()
    page.mouse.move(frame["x"] + frame["width"] / 2, frame["y"] + frame["height"] - 10)
    page.mouse.move(frame["x"] + 30, frame["y"] + 60)
    assert page.evaluate(LIT) == lit                               # the frame is background
    link = page.locator("#nllLayout .nb-frame a.nb-link")
    assert "/synapsenotebooks/" in link.get_attribute("href") and link.get_attribute("target") == "_blank"
    link.hover()
    assert "in Fabric" in page.inner_text("#nllLayout [data-c=tip]")
    assert page.evaluate(LIT) == lit
    page.keyboard.press("Escape")
    assert page.evaluate(LIT) == 0


def test_the_notebook_tab_shows_only_the_chosen_notebook(page):
    link = page.evaluate("window.fabricExplorer.host.link({v: 'notebooks', nb: '%s', s: 'full'})" % ID["nb20"])
    page.goto(link)                                       # even a link asking for the full lineage
    until(page, f"window.fabricExplorer.views.notebooks && (s => s.full && !s.loading && s.full.focus.itemId === '{ID['nb20']}')(window.fabricExplorer.views.notebooks.state)")
    assert page.evaluate("window.fabricExplorer.views.notebooks.state.full.scope") == "direct"
    assert page.evaluate("document.querySelectorAll('#nllLayout .nb-frame').length") == 1
    assert page.evaluate("document.querySelectorAll('#nllLayout g.nb').length") == 0           # no other notebook, no Power BI
    assert page.locator("#nllLayout [data-scope]").count() == 0
    # every definition's text sits inside its box
    overflow = page.evaluate("""[...document.querySelectorAll('#nllLayout .pill')].filter(p => {
        const box = p.getBoundingClientRect();
        return [...p.parentNode.querySelectorAll('text.code')].some(t => {
          const r = t.getBoundingClientRect(); return r.right > box.right + 0.5 || r.left < box.left; }); }).length""")
    assert overflow == 0
    assert page.evaluate(COVERED) == []

def test_changed_only_and_finding_a_column(page):
    _open_notebook(page, "nb20")
    all_rows = page.evaluate("document.querySelectorAll('#nllLayout g.ds g.row').length")
    page.click("#nllLayout [data-only=changed]")
    until(page, f"document.querySelectorAll('#nllLayout g.ds g.row').length < {all_rows}")
    assert "unchanged" in page.evaluate("document.querySelector('#nllLayout [data-c=world]').textContent") or \
        page.evaluate("document.querySelectorAll('#nllLayout .ds-more').length") > 0
    page.keyboard.press("/")
    page.keyboard.type("tagno")
    until(page, "!document.querySelector('#nllLayout [data-c=hits]').hidden")
    page.keyboard.press("Enter")
    until(page, "document.querySelector('#nllLayout g.row.sel') !== null")
    assert page.evaluate("document.querySelector('#nllLayout g.row.sel').dataset.port").endswith("|tagno")
    page.click("#nllLayout [data-only=all]")
    until(page, f"document.querySelectorAll('#nllLayout g.ds g.row').length == {all_rows}")
    assert page.evaluate(COVERED) == []


def test_focus_mode_in_the_datasets_view_reaches_power_bi(page):
    page.evaluate("window.fabricExplorer.host.readAll(['DataPipeline', 'SemanticModel', 'Report'])")
    until(page, "window.fabricExplorer.S.busy.size === 0 && window.fabricExplorer.S.scans.size > 11")
    page.evaluate("window.fabricExplorer.showView('columns', {mode: 'datasets'})")
    until(page, "document.querySelectorAll('#cllLayout g.card-model').length === 1")
    everything = page.evaluate("document.querySelectorAll('#cllLayout .n').length")
    assert page.evaluate("document.querySelectorAll('#cllLayout .e-bi').length") > 0
    port = page.evaluate("[...document.querySelectorAll('#cllLayout g.ds g.row')].find(r => r.dataset.port.endsWith('|tagno') && r.classList.contains('st-altered')).dataset.port")
    page.evaluate(f"window.fabricExplorer.views.columns.select({{port: '{port}', node: '{port.split('|')[0]}'}})")
    page.click("#cllLayout [data-act=focus]")
    until(page, f"document.querySelectorAll('#cllLayout .n').length < {everything}")
    assert "Focus on the thread of" in page.inner_text("#cllLayout [data-c=bar]")
    assert page.evaluate("document.querySelectorAll('#cllLayout g.card-report').length") >= 1   # the thread reaches a report
    assert page.evaluate(COVERED) == []
    page.click("#cllLayout [data-c=bar] [data-act=focus]")
    until(page, f"document.querySelectorAll('#cllLayout .n').length == {everything}")


def test_export_and_a_link_that_opens_the_view(page):
    _open_notebook(page, "nb21")
    page.click("#nllLayout .cll-menu > summary")
    with page.expect_download() as dl:
        page.click("#nllLayout [data-export=view-csv]")
    text = open(dl.value.path(), encoding="utf-8-sig").read()
    assert text.startswith("target,target_column,status,source") and "DisciplineName" in text
    page.click("#nllLayout .cll-menu > summary")
    with page.expect_download() as dl:
        page.click("#nllLayout [data-export=ws-json]")
    import json
    data = json.load(open(dl.value.path(), encoding="utf-8"))
    assert data["openlineage"] and data["rows"]
    port = page.evaluate("[...document.querySelectorAll('#nllLayout g.ds g.row')].find(r => r.dataset.port.endsWith('|disciplinename')).dataset.port")
    page.evaluate(f"window.fabricExplorer.views.notebooks.select({{port: '{port}', node: '{port.split('|')[0]}'}})")
    link = page.evaluate("window.fabricExplorer.host.link(window.fabricExplorer.views.notebooks.linkParams())")
    assert "k=" not in link and "v=notebooks" in link and f"nb={ID['nb21']}" in link
    other = page.context.new_page()                    # the same browser, the key it keeps: the link opens the view
    other.goto(link)
    until(other, "document.querySelector('#nllLayout g.row.sel') !== null", timeout=20)
    assert other.evaluate("document.querySelector('#nllLayout g.row.sel').dataset.port") == port
    other.close()


def test_dragging_never_selects_the_drawings_text(page):
    _open_notebook(page, "nb20")
    page.click("#nllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    box = page.locator('#nllLayout g.ds').first.bounding_box()
    sheet = page.locator('#nllLayout [data-c=svg]').bounding_box()
    page.mouse.move(max(sheet["x"] + 6, box["x"] - 40), box["y"] + 30)   # from the sheet, across a table's names
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    page.mouse.up()
    page.mouse.dblclick(box["x"] + 30, box["y"] + 60)      # a double click on a column name
    assert page.evaluate("String(window.getSelection())") == ""
    assert page.evaluate("getComputedStyle(document.querySelector('#nllLayout .col-name')).userSelect") == "none"


def test_shared_definitions_as_lanes_or_one_point(page):
    _open_notebook(page, "nb21")
    block = "document.querySelectorAll('#nllLayout g.blk').length"
    assert page.evaluate(block) >= 1                         # transform(...) of every column: one lane each
    lanes = page.evaluate("document.querySelector('#nllLayout g.blk').querySelectorAll('.lane').length")
    assert lanes >= 4
    page.click("#nllLayout [data-lanes=bundled]")
    until(page, f"{block} === 0")
    assert "b=1" in page.evaluate("window.fabricExplorer.host.link(window.fabricExplorer.views.notebooks.linkParams())")
    page.click("#nllLayout [data-lanes=lanes]")
    until(page, f"{block} >= 1")
    assert page.evaluate(COVERED) == []


def test_moving_a_node_and_putting_it_back(page):
    _open_notebook(page, "nb21", scope="direct")
    page.click("#nllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    index = page.evaluate("""[...document.querySelectorAll('#nllLayout .nodes > g.ds')].findIndex(g => {
        const r = g.querySelector('.ds-top').getBoundingClientRect();
        return g.contains(document.elementFromPoint(r.left + 40, r.top + 12)); })""")
    assert index >= 0                                        # after Fit, the cards' headings are clear of the bars
    node = page.locator("#nllLayout .nodes > g.ds").nth(index)
    before = node.get_attribute("transform")
    edge = page.evaluate(f"""(() => {{ const id = document.querySelectorAll('#nllLayout .nodes > g.ds')[{index}].dataset.node;
        const e = [...window.fabricExplorer.views.notebooks.state.graph.edges].find(x => x.fromNode === id || x.toNode === id);
        return e ? e.i : -1; }})()""")
    d_before = page.get_attribute(f"#nllLayout [data-e='{edge}']", "d")
    head = node.locator(".ds-top").bounding_box()
    page.mouse.move(head["x"] + 40, head["y"] + 12)
    page.mouse.down()
    page.mouse.move(head["x"] + 40, head["y"] + 130, steps=6)
    page.mouse.up()
    assert node.get_attribute("transform") != before
    assert page.get_attribute(f"#nllLayout [data-e='{edge}']", "d") != d_before        # its lines followed it
    assert page.evaluate("window.fabricExplorer.views.notebooks.state.sel") is None       # a move isn't a click
    page.click("#nllLayout [data-act=relayout]")
    until(page, f"document.querySelectorAll('#nllLayout .nodes > g.ds')[{index}].getAttribute('transform') === '{before}'")
    assert page.locator("#nllLayout [data-act=relayout]").count() == 0



def _menu_click(pg, label):
    """Click an entry of the right-click menu the lineage tabs open."""
    until(pg, "document.querySelector('#ctxFloat') && !document.querySelector('#ctxFloat').hidden")
    pg.locator("#ctxFloat button", has_text=label).first.click()


def test_right_click_moves_between_the_tabs(page):
    page.evaluate("window.fabricExplorer.showView('columns', {mode: 'datasets', focus: ''})")
    until(page, "(s => s.full && !s.loading && !s.focus)(window.fabricExplorer.views.columns.state)")
    page.click("#cllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    card = page.locator("#cllLayout .nodes > g.card-notebook", has_text="nb_20_bronze_to").first
    card.locator("[data-port$='|'] .nb-name").dispatch_event("contextmenu", {"bubbles": True, "cancelable": True, "button": 2, "clientX": 700, "clientY": 300})
    _menu_click(page, "Focus on column lineage")
    until(page, f"window.fabricExplorer.view === 'notebooks' && (s => s.full && !s.loading && s.full.focus.itemId === '{ID['nb20']}')(window.fabricExplorer.views.notebooks.state)")
    page.click("#nllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    page.locator("#nllLayout .nodes > g.ds .ds-name", has_text="cms_tags_raw").first.dispatch_event("contextmenu", {"bubbles": True, "cancelable": True, "button": 2, "clientX": 700, "clientY": 300})
    _menu_click(page, "Show its column changes")
    until(page, "window.fabricExplorer.view === 'columns' && window.fabricExplorer.views.columns.state.focus.endsWith(':cms_tags_raw')")
    assert page.errors == []


def test_map_right_click_on_a_semantic_model_shows_its_column_changes(page):
    page.evaluate("window.fabricExplorer.showView('map')")
    page.evaluate(f"window.fabricExplorer.reveal('item:{ID['model']}')")
    page.wait_for_timeout(900)
    pos = page.evaluate(f"window.fabricExplorer.screenOf('item:{ID['model']}')")
    box = page.locator("#sheet").bounding_box()
    page.mouse.click(box["x"] + pos["x"], box["y"] + pos["y"], button="right")
    page.locator("#ctxMenu button", has_text="Show its column changes").click()
    until(page, f"window.fabricExplorer.view === 'columns' && (s => s.full && !s.loading && s.focus === 'item:{ID['model']}')(window.fabricExplorer.views.columns.state)")
    assert page.evaluate("document.querySelectorAll('#cllLayout g.card-model').length") == 1
    assert "reaching PIMS Model" in page.inner_text("#cllLayout [data-c=bar]")


def test_a_moved_nodes_lines_go_straight_to_it(page):
    _open_notebook(page, "nb20", scope="direct")
    page.click("#nllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    head = page.locator("#nllLayout .nodes > g.ds .ds-top").first
    box = head.bounding_box()
    page.mouse.move(box["x"] + 40, box["y"] + 12)
    page.mouse.down()
    page.mouse.move(box["x"] + 40, box["y"] + 200, steps=6)
    page.mouse.up()
    lengths = page.evaluate("""(() => { const st = window.fabricExplorer.views.notebooks.state;
        const id = document.querySelector('#nllLayout .nodes > g.ds').dataset.node;
        return (st.edgesOf.get(id) || []).map(e => st.lay.routes.get(e).length); })()""")
    assert lengths and set(lengths) == {2}


def test_the_map_is_the_start_page(page):
    page.evaluate("window.fabricExplorer.showView('columns')")      # the last tab used: not where it starts
    start = page.evaluate("location.origin + location.pathname + '#k=' + new URLSearchParams(location.hash.slice(1)).get('k')")
    page.goto(start)                                         # the link Fabric Explorer opens: just the session key
    page.reload()
    until(page, "window.fabricExplorer && window.fabricExplorer.S.ws")
    page.wait_for_timeout(300)
    assert page.evaluate("window.fabricExplorer.view") == "map"


def test_folding_a_datasets_columns(page):
    page.evaluate("window.fabricExplorer.host.readAll(['Notebook'])")
    until(page, "window.fabricExplorer.S.busy.size === 0 && window.fabricExplorer.S.scans.size > 8")
    page.evaluate("window.fabricExplorer.showView('columns', {mode: 'datasets', focus: ''})")
    until(page, "(s => s.full && !s.loading && !s.focus)(window.fabricExplorer.views.columns.state)")
    card = "[...document.querySelectorAll('#cllLayout .nodes > g.ds')].find(g => g.querySelectorAll('g.row').length > 2)"
    target = page.evaluate(f"{card}.dataset.node")
    rows = f"document.querySelector('#cllLayout .nodes > g.ds[data-node=\"{target}\"]').querySelectorAll('g.row').length"
    assert page.evaluate(rows) > 2
    page.locator(f"#cllLayout .nodes > g.ds[data-node='{target}'] .ds-name").dispatch_event(
        "contextmenu", {"bubbles": True, "cancelable": True, "button": 2, "clientX": 700, "clientY": 300})
    _menu_click(page, "Fold its columns")
    until(page, f"{rows} === 0")
    assert "folded away" in page.evaluate(f"document.querySelector('#cllLayout .nodes > g.ds[data-node=\"{target}\"]').textContent")
    ends = page.evaluate(f"""window.fabricExplorer.views.columns.state.graph.edges
        .filter(e => e.fromNode === '{target}' || e.toNode === '{target}').map(e => e.fromNode === '{target}' ? e.from : e.to)""")
    assert ends and set(ends) == {f"{target}|"}                          # its lines meet at the heading
    page.locator(f"#cllLayout .nodes > g.ds[data-node='{target}'] [data-fold]").dispatch_event("click", {"bubbles": True})
    until(page, f"{rows} > 2")


def test_a_bundle_stays_a_ribbon_when_its_node_is_moved(page):
    _open_notebook(page, "nb21")
    page.click("#nllLayout [data-lanes=bundled]")
    until(page, "document.querySelectorAll('#nllLayout g.blk').length === 0")
    page.click("#nllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    op = page.locator("#nllLayout .nodes > g.op", has_text="standardise_column_names").first
    box = op.bounding_box()
    page.mouse.move(box["x"] + 12, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 12, box["y"] + box["height"] / 2 + 90, steps=6)
    page.mouse.up()
    spread = page.evaluate("""(() => { const st = window.fabricExplorer.views.notebooks.state;
        const id = [...st.graph.nodes].find(n => n.kind === 'op' && n.data.label.includes('standardise_column_names')).id;
        const groups = {};
        for (const e of st.edgesOf.get(id)) { const r = st.lay.routes.get(e); const k = JSON.stringify([r[0], r[r.length - 1]]);
          (groups[k] = groups[k] || []).push(r); }
        return Object.values(groups).filter(g => g.length > 1).map(g => new Set(g.map(r => r.length === 3 ? Math.round(r[1].y) : 'flat')).size === g.length); })()""")
    assert spread and all(spread)                                        # each line of a bundle has its own way
    page.click("#nllLayout [data-lanes=lanes]")


def test_a_mixed_selection_on_the_map_moves_together(page):
    page.evaluate("window.fabricExplorer.showView('map')")
    page.wait_for_timeout(300)
    nb = f"item:{ID['nb20']}"
    table = page.evaluate(f"""[...window.fabricExplorer.S.nodes.values()].find(n => n.kind === 'table' && n.label === 'cms_tags_raw' &&
        window.fabricExplorer.V.vis.has(n.id)).id""")
    lane = page.evaluate(f"window.fabricExplorer.S.nodes.get('{table}').parent")
    assert page.evaluate(f"!window.fabricExplorer.V.edges.some(e => e.kind === 'default_lakehouse' && e.source === '{nb}')")
    page.evaluate(f"window.fabricExplorer.reveal('{nb}')")
    page.wait_for_timeout(900)
    sheet = page.locator("#sheet").bounding_box()
    def at(id):
        p = page.evaluate(f"window.fabricExplorer.screenOf('{id}')")
        return sheet["x"] + p["x"], sheet["y"] + p["y"]
    pos = lambda id: page.evaluate(f"(n => [Math.round(n.x), Math.round(n.y)])(window.fabricExplorer.S.nodes.get('{id}'))")
    page.keyboard.press("Escape")
    page.keyboard.down("Shift")
    page.mouse.click(*at(nb))
    page.mouse.click(*at(table))
    page.keyboard.up("Shift")
    assert page.evaluate("window.fabricExplorer.S.multi.size") == 2
    before = pos(lane), pos(nb)
    x, y = at(nb)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 90, y + 40, steps=8)
    page.mouse.up()
    page.wait_for_timeout(700)
    after = pos(lane), pos(nb)
    assert after[1] != before[1] and after[0] != before[0]              # the dataset's lakehouse came along


def test_column_tab_follows_a_whole_column_and_a_table_all_the_way(page):
    page.evaluate("window.fabricExplorer.host.readAll(['Notebook', 'DataPipeline', 'SemanticModel', 'Report'])")
    until(page, "window.fabricExplorer.S.busy.size === 0 && window.fabricExplorer.S.scans.size > 14")
    page.evaluate("window.fabricExplorer.showView('columns', {mode: 'datasets', focus: ''})")
    until(page, "(s => s.full && !s.loading && !s.focus)(window.fabricExplorer.views.columns.state)")
    assert [b.inner_text() for b in page.locator("#cllLayout [data-cols]").all()] == ["All columns", "Changed only"]
    page.click("#cllLayout [data-cols=all]")
    until(page, "(s => s.full && !s.loading && s.full.allColumns)(window.fabricExplorer.views.columns.state)")
    port = page.evaluate("""[...document.querySelectorAll('#cllLayout g.ds')].find(g => g.textContent.includes('fact_pims_tags'))
        .querySelector('g.row[data-port$="|domain"]').dataset.port""")
    page.evaluate(f"window.fabricExplorer.views.columns.select({{port: '{port}', node: '{port.split('|')[0]}'}})")
    lit = page.evaluate("[...document.querySelectorAll('#cllLayout .e.hl')].map(e => e.getAttribute('class'))")
    assert any("e-copy" in c for c in lit)                                   # an unchanged column, followed too
    assert page.evaluate("[...document.querySelectorAll('#cllLayout g.ds.hl')].some(g => g.textContent.includes('cms_tags_raw'))")
    raw = page.evaluate("[...document.querySelectorAll('#cllLayout .nodes > g.ds')].find(g => g.textContent.includes('cms_tags_raw')).dataset.node")
    page.evaluate(f"window.fabricExplorer.views.columns.select({{node: '{raw}'}})")
    for name in ("nb_20_bronze_to_silver_tags", "nb_30_silver_to_gold", "PIMS Model", "PIMS Tags"):
        assert page.evaluate(f"[...document.querySelectorAll('#cllLayout .nodes > g.nb.hl')].some(g => g.textContent.includes('{name}'))"), name
    page.click("#cllLayout [data-cols=changed]")
    until(page, "(s => s.full && !s.loading && !s.full.allColumns)(window.fabricExplorer.views.columns.state)")


def test_no_grey_dotted_line_on_a_node_with_lines_in_and_out(page):
    page.evaluate("window.fabricExplorer.showView('map')")
    page.wait_for_timeout(300)
    left = page.evaluate("""(() => { const V = window.fabricExplorer.V, ins = new Set(), outs = new Set();
        const dotted = new Set(['default_lakehouse', 'attached', 'environment']);
        for (const e of V.edges) if (e.kind !== 'structure' && !dotted.has(e.kind)) { outs.add(e.source); ins.add(e.target); }
        return V.edges.filter(e => dotted.has(e.kind) && [e.source, e.target].some(id => ins.has(id) && outs.has(id))).length; })()""")
    assert left == 0


def test_changed_only_keeps_both_sides_of_the_notebook(page):
    _open_notebook(page, "nb30")
    page.click("#nllLayout [data-only=changed]")
    page.wait_for_timeout(400)
    roles = page.evaluate("[...new Set(window.fabricExplorer.views.notebooks.state.data.datasets.map(d => d.role))]")
    assert "source" in roles and "target" in roles
    page.click("#nllLayout [data-only=all]")


def test_shift_click_selects_several_and_they_move_together(page):
    _open_notebook(page, "nb20")
    page.click("#nllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    ids = page.evaluate("[...document.querySelectorAll('#nllLayout .nodes > g.ds')].slice(0, 2).map(g => g.dataset.node)")
    for i, id in enumerate(ids):
        head = page.locator(f"#nllLayout .nodes > g.ds[data-node='{id}'] .ds-top").bounding_box()
        if i:
            page.keyboard.down("Shift")
        page.mouse.click(head["x"] + 30, head["y"] + 12)
    page.keyboard.up("Shift")
    assert page.evaluate("document.querySelectorAll('#nllLayout .nodes > g.ds.sel').length") == 2
    assert "2 selected" in page.inner_text("#nllLayout [data-c=info]")
    before = page.evaluate(f"{ids}.map(id => document.querySelector(`#nllLayout .nodes > g.ds[data-node='${{id}}']`).getAttribute('transform'))")
    head = page.locator(f"#nllLayout .nodes > g.ds[data-node='{ids[0]}'] .ds-top").bounding_box()
    page.mouse.move(head["x"] + 30, head["y"] + 12)
    page.mouse.down()
    page.mouse.move(head["x"] + 30, head["y"] + 112, steps=6)
    page.mouse.up()
    after = page.evaluate(f"{ids}.map(id => document.querySelector(`#nllLayout .nodes > g.ds[data-node='${{id}}']`).getAttribute('transform'))")
    assert after[0] != before[0] and after[1] != before[1]                 # both moved
    page.keyboard.down("Shift")                                            # Shift+click one again: it leaves the selection
    head = page.locator(f"#nllLayout .nodes > g.ds[data-node='{ids[1]}'] .ds-top").bounding_box()
    page.mouse.click(head["x"] + 30, head["y"] + 12)
    page.keyboard.up("Shift")
    assert page.evaluate("document.querySelectorAll('#nllLayout .nodes > g.ds.sel').length") == 1
    page.click("#nllLayout [data-act=relayout]")
    page.keyboard.press("Escape")


def test_shift_drag_draws_a_box_and_focus_takes_them_all(page):
    page.evaluate("window.fabricExplorer.host.readAll(['Notebook'])")
    until(page, "window.fabricExplorer.S.busy.size === 0 && window.fabricExplorer.S.scans.size > 8")
    page.evaluate("window.fabricExplorer.showView('columns', {mode: 'datasets', focus: ''})")
    until(page, "(s => s.full && !s.loading && !s.focus)(window.fabricExplorer.views.columns.state)")
    page.click("#cllLayout [data-act=fit]")
    page.wait_for_timeout(400)
    sheet = page.locator("#cllLayout [data-c=svg]").bounding_box()
    page.keyboard.down("Shift")
    page.mouse.move(sheet["x"] + 20, sheet["y"] + 140)
    page.mouse.down()
    page.mouse.move(sheet["x"] + sheet["width"] * 0.45, sheet["y"] + sheet["height"] - 20, steps=8)
    assert page.locator("#cllLayout [data-c=box]").is_visible()
    page.mouse.up()
    page.keyboard.up("Shift")
    n = page.evaluate("window.fabricExplorer.views.columns.state.sels.length")
    assert n > 1 and f"{n} selected" in page.inner_text("#cllLayout [data-c=info]")
    page.click("#cllLayout [data-c=info] [data-act=focus]")
    until(page, "Array.isArray(window.fabricExplorer.views.columns.state.focusOn)")
    assert f"{n} selected" in page.inner_text("#cllLayout [data-c=bar]")
    page.click("#cllLayout [data-c=info] [data-act=focus]")                  # leave focus
    page.click("#cllLayout [data-c=info] [data-act=clearsel]")
    assert page.evaluate("window.fabricExplorer.views.columns.state.sel") is None
    assert page.evaluate(COVERED) == []
