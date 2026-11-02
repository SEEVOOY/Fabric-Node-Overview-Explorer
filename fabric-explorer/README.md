# Fabric Explorer

A map of a Microsoft Fabric workspace that you explore as you go. It shows which notebooks read
and write which lakehouse tables, what runs what, and what feeds the semantic models and reports.
Every item on the map has a direct link to open it in Fabric.

It uses the same sign-in and REST method as pims-explorer: one Microsoft sign-in in your browser
with your own work account, then Fabric's `getDefinition` API (the call we used to read the
semantic model) for the content of each item. There are no tokens to paste and no Azure CLI.

```
Open workspace  ->  map of its items (nothing inside them is read yet)
Select an item  ->  only that item's content is read
                ->  the map grows with what it reads, writes and runs
                ->  the result is kept in memory; Refresh reads it again
```

## Setup (once)

```
pip install -r requirements.txt
```

Python 3.10 or newer. Only `requests` and `azure-identity` are needed (pytest only for the tests).

## Start

Give it the workspace in any of these ways. The workspace **address from your browser** works
everywhere; the app takes the ID out of it.

1. **In config.py.** Put the address (or the ID) in `fabric_explorer\config.py`:
   ```python
   REST = dict(
       workspace_id="https://app.powerbi.com/groups/<workspace ID>/list?experience=fabric-developer",
       ...
   )
   ```
   then start with `python run.py` (or double-click `Start Fabric Explorer.bat`).
2. **On the command line:** `python run.py "https://app.fabric.microsoft.com/groups/<workspace ID>/list"`.
   Put the address in quotes; addresses with `&` in them need it.
3. **In the app:** start without a workspace and paste the address into the box. You can also
   paste an address anywhere on the page, or pick from the list of workspaces you can access.

Other things that work as an address: a notebook's or lakehouse's address (the workspace opens
with that item selected), `https://app.powerbi.com/groups/me/list` (My workspace), and the bare
workspace ID.

**Using your pims-explorer settings:** `python run.py --config ..\pims-explorer\pims_explorer\config.py`
reads the `REST` block from that file. Or paste the block into this project's `config.py`; it has
the same shape.

**Sign-in.** The first start opens the Microsoft sign-in page in your browser; sign in with your
work account. The sign-in is remembered (azure-identity's encrypted token cache), so later starts
don't ask again. `python run.py --sign-out` forgets it. Set `LOGIN_HINT = "you@company.com"` in
config.py to pre-fill your email on the sign-in page.

The app then opens its page at `http://127.0.0.1:8765/#k=...`. Keep the console window open
while you explore; Ctrl+C stops it.

Want to look first? `python run.py --demo` opens a built-in demo workspace (bronze, silver and
gold lakehouses, a dozen notebooks, a pipeline, a semantic model) without signing in.

## Exploring

- **Select an item** on the map or in the list on the left. Its content is read once and the
  map draws its links. A dashed outline means *not read yet*, a solid one *read*, and red
  *couldn't be read* (the details panel says why).
- **Open in Fabric.** Ctrl+click an item, click the arrow at its corner, use *Open in Fabric* in
  the details panel, or the arrow next to it in the list.
- **Lineage.** Selecting something highlights everything upstream and downstream of it
  (*Full lineage*), or only what is directly linked (*Direct links*). In the details panel, every
  name that is highlighted on the map is one size larger, bold and italic, and *Highlighted on the
  map* at the bottom lists the whole highlighted chain. Hovering a row in the panel rings that item
  on the map; hovering an item on the map tints its row in the panel.
- **A lakehouse's datasets from the details panel.** Every lakehouse row in the details panel
  (*Default lakehouse*, *Highlighted on the map*, a selection, the Lakehouses group) has a ▾ button
  that opens its schemas (or Tables) and Files right there, with every table and folder. Click one
  to select it (it's put on the map if it wasn't), tick or untick it, or use *Columns*. A
  lakehouse that hasn't been read yet is read when you open it. *Zoom to its lineage* in
  the details panel moves the map to it; *Show only its lineage* takes everything else off the map.
- **Choose what's on the map.** Every item in the left panel has a tickbox: untick it to take it
  off the map, tick it to put it back. A group's tickbox does the whole group. Tables and folders
  have tickboxes too: under a lakehouse's contents (each schema, Tables and Files heading does its
  whole section) and in the lists of the details panel on the right. A lakehouse's own tickbox
  decides for everything under it: ticking it on puts the lakehouse, its folders and all its
  datasets on the map (Folders, Contents and every box in its list get ticked, whatever they were);
  ticking it off takes all of it off. *Folders* and *Contents* work the same way for what they
  cover. A lakehouse with more than 300 tables and folders asks first. *Hide all* followed
  by ticking the notebooks you care about is the quickest way to focus: ticking a notebook brings
  its tables and their lakehouses with it, and unticking one also removes the tables only it uses.
- **Lakehouses** are drawn as vessels with a coloured area around their tables (bronze, silver
  and gold get those colours). Selecting a lakehouse lists its tables with how many notebooks
  write and read each, plus every notebook that writes to it or reads from it. Notebooks only show
  up there once they have been read: *Read the other N* reads the rest.
- **A lakehouse's contents.** The arrow in front of a lakehouse in the left panel opens its
  schemas (or Tables) with every table, shortcuts included, and its Files folders, right there in
  the panel, without changing the map; tick the rows you want on the map. The *Contents* tickbox
  beside it (or *Show its contents* in the details panel) puts all of it on the map and opens the
  list too; untick it to put the map back to only the tables something uses. A lakehouse that
  hasn't been read is read when you open it. Drag the left panel's right edge to make it wider for
  long names (double-click the edge to reset).
- **Open a lakehouse on the map.** Every lakehouse node has a **+** at its right end: click it
  to load its datasets onto the map, shown under their folders (the lakehouse is read first if it
  hasn't been). **−** collapses it again: its folders and datasets leave the map and notebook links
  point at the lakehouse itself. Folder nodes have their own **+ / −** to open or close one folder at
  a time. A lakehouse with more than 300 tables and folders asks first; answering no shows just its
  folders, each with its own **+**. Right-click a lakehouse for *Expand*, *Show its folders only* and
  *Collapse*. The left panel's tickboxes keep working alongside and follow what you do here.
- **A semantic model's columns.** Selecting a semantic model lists its tables in the details panel;
  each opens onto the columns of its source table, marked *in the model*, *hidden in the model*
  (italic), *not in the model* (grey), *in the model as …* for renamed ones, plus the model's
  calculated columns. Tick *Only the model's visible columns* to see just what the model shows.
  The Table content tab marks the columns a model uses.
- **Columns on the map** (off by default). In a table's details, *Columns on the map: Off / All /
  In use* draws its columns inside its node. *All* shows every column: the ones a semantic model
  uses have a green dot and bold text, and the ones it hides are in italics. *In use* shows only the
  columns a semantic model shows, leaving out the ones it hides. A model's details have the same switch
  for all of its tables at once. Very long tables show the first 40 and point to the Table content tab.
- **Folders (schemas) on the map.** The *Folders* tickbox beside a lakehouse (its column is
  labelled in the group heading, next to *Contents*) shows its schemas, or Tables, and Files as
  nodes on the map, with the datasets on the map hanging under their folder: bronze, then d2_temp,
  then lci_documents. Each folder in the lakehouse's list then gets a second tickbox, to the left of
  the one it already has: the first shows the folder's name on the map, the second puts all its
  datasets on the map, and they work independently. A folder shows by default when one of its
  datasets is on the map. In the Structured view the folders become the headings of the
  lakehouse's column.
- **Datasets** (the second tab on the left) lists every table and folder the app knows, by
  lakehouse, with how many notebooks write, read and maintain each one, and marks the ones
  *not used*. Count the notebooks on the map (the ones you have ticked) or all notebooks. Filter on
  *In use* or *Not used*; *Put listed on the map* shows exactly those. A folder counts as in use when
  something inside it is used. Tables used only by a pipeline or the semantic model say so. A
  lakehouse's unused tables only show up once the lakehouse has been read (*Read the other N*).
- **Table content** (the third tab on the left) shows a table's column names, in order. Select a
  table anywhere, or use *Columns* beside a table under a lakehouse's contents. *Copy names*
  copies them one per line, ready for Excel or a SELECT.
- **Tables** show who writes, reads and maintains them, each with the cell and line of code where
  that happens. A dashed table outline means the code names it but the lakehouse doesn't list it.
- **Groups.** Items start grouped by type (or by folder). Groups other than lakehouses and
  notebooks start folded; click a group on the map, or *Show* in the list, to unfold it. Read
  items move out of their group into the data flow, arranged left to right from sources to reports.
- **Pin a highlight.** Right-click an item and choose *Pin its highlight* (or use *Pin its
  highlight* in the details panel): its lineage stays highlighted while you click and select other
  items, which only changes what the details panel shows; the Structured view doesn't rearrange
  either. The pinned item gets a dark ring and a pin on the map and a pin in the left panel, a bar
  above the map says what is pinned, and other items' details say where the highlight is pinned.
  *Unpin* (in the bar, the details panel or the right-click menu, or Esc once nothing is selected)
  lets the highlight follow the selection again. *Show its lineage* on another item moves the pin.
- **Show all relations, even what you took off the map.** Selecting something highlights only
  what is on the map. To see everything related to it, right-click it and choose *Show its
  lineage* (a table or folder: *Show its thread* / *where it comes from* / *where it goes*), use
  *Show all its relations* in the details panel, or change a dataset's *Follow* direction: the
  lineage is worked out from everything the app has read, every item and dataset in it is put back
  on the map (their tickboxes get ticked) and it is highlighted. This works after *Hide all* too.
  *Direct links* brings back only what is directly linked; in focus mode it stays inside the focus.
- **Follow a dataset's thread.** Selecting a table or folder lights up its thread: the notebooks
  (and pipelines, models) tied to it, everything those read and write, and onwards. Choose the
  direction in the details panel (*Follow: Inputs / Both ways / Outputs*) or by right-clicking it:
  *Inputs* follows where it comes from (what writes it, everything those read and write, then on
  upstream), *Outputs* follows where it goes (what reads it, everything those read and write, then
  on downstream), *Both ways* does both. Only the notebooks right next to the dataset get their full
  context; further along only the chosen direction is followed, so the thread stays readable. With
  *Direct links* only the notebooks right next to it are lit.
- **Focus mode** (*Focus* above the map, *Focus* in a table's details, or right-click *Focus on
  what's highlighted*) shows only what is highlighted, without changing any tickbox. Inside it you
  can click and explore freely: the map doesn't grow beyond the focus, and anything you tick is
  added to it. *Leave focus* (or Esc with nothing selected) brings everything back as it was.
- **Right-click an item on the map** for a menu: the thread options for tables and folders,
  *Focus on what's highlighted*, *Remove from view* (with several selected and the item among
  them, it removes them all) and *Open in Fabric*. Right-drag still pans.
- **Free or Structured** (top of the map). *Free* is the map described above: items find their
  place by their links, and you can drag them. *Structured* arranges the same nodes tidily by
  hierarchy: every lakehouse becomes a column with its schemas (or Tables) and Files listed under
  it, the columns follow the data flow from sources to gold, and notebooks sit in the gaps between
  the lakehouses they read and write, ordered so links cross as little as possible. Orchestrating
  notebooks sit just before what they run, reports just after their model. Clicking a notebook (or
  anything else) lines up what it highlights: its sources and branches move to the top of their
  lakehouse columns and the highlighted notebooks sit level with them, while the item you clicked
  stays where it is on screen. *Re-layout* goes back to name order. Items without links
  wait above, grouped as in the left panel. Everything else (selection, lineage, tickboxes) works
  the same in both; switching back to Free puts the items where they were.
- **Moving and selecting several.** In both views, dragging on empty space pans the map and
  dragging an item moves it; in the Structured view, dragging a lakehouse moves its whole column
  with its tables. Hold Shift and drag on empty space to draw a selection box (the cursor turns into
  a crosshair): everything inside is selected, and dragging any selected item moves them all
  together. Ctrl+Shift+drag adds another box to the selection, Shift+click adds or removes a single
  item, Esc or a click on empty space clears it. In the Structured view your moves are kept when the
  map changes (new reads, tickboxes); *Re-layout* puts everything back in the tidy arrangement.
  The right or middle mouse button, or Space+drag, also pans. With several selected, the details panel lists them with *Zoom to them*,
  *Show only these*, *Take off the map* and *Read the ones not read yet*.
- **View** (top of the map) switches tables, lakehouse areas, groups, default-lakehouse lines and
  the left-to-right arrangement on and off. **Export** saves the relationships as a CSV for Excel,
  or the whole map as JSON.
- **Requests to Fabric** (bottom bar) lists every API call the app made, so you can see that
  opening a workspace is cheap and an item is only fetched when you select it.

Keys: `/` search, `F` fit the map, `Esc` clear the selection, `+` / `-` zoom, Space+drag pan.

## What is fetched when

| When | Calls |
|---|---|
| Opening a workspace | `GET /v1/workspaces/{ws}`, `/items` (all pages), `/lakehouses`, `/folders` |
| Selecting a notebook | `POST /v1/workspaces/{ws}/notebooks/{id}/getDefinition?format=ipynb` (then polling the operation) |
| Selecting a lakehouse | `GET /lakehouses/{id}`, `/lakehouses/{id}/tables` (schema-enabled lakehouses: the OneLake `Tables` folder), `/items/{id}/shortcuts`, the top-level folders under OneLake `Files` |
| A table's column names | the OneLake table API (`GET onelake.table.fabric.microsoft.com/delta/{ws}/{lakehouse}/api/2.1/unity-catalog/tables/...`); if it doesn't answer, the newest commit in the table's Delta log that records its schema |
| Selecting a semantic model | `POST /semanticModels/{id}/getDefinition?format=TMSL`, the same as pims-explorer |
| Selecting a report | Power BI `GET /groups/{ws}/reports/{id}` (its semantic model) |
| Selecting a pipeline or other item | `POST /items/{id}/getDefinition` |
| A reference into another workspace | that item's name, once (`GET /workspaces/{ws}/items/{id}`) |

Results are kept in memory while the app runs: selecting an item again, or reloading the page,
costs no calls. *Refresh* (right-click an item, or the button in its details; a group's panel has
*Refresh all*) reads it again from Fabric after dropping what was read before, so a change you just
saved in a notebook shows, including tables and links it no longer has; what you chose on the map
stays as it was. Right-clicking a table offers *Refresh its lakehouse*; *Reload* reads the list of items again and keeps
what was already read. *Unload* forgets what was read for an item (right-click it, or the button
next to *Refresh*; a group's panel has *Unload all*, and with nothing selected the panel has
*Unload everything read*): it goes back to "not read" with a dashed outline, its links leave the
map, anything only that read knew about goes too, and for a lakehouse its Folders and Contents
are reset. It stays unloaded after a page reload; selecting it reads it fresh from Fabric.

## What a notebook read finds

From the notebook's settings: its **default lakehouse**, other attached lakehouses and its
environment.

From its code (Python with the Python parser; SQL cells and `spark.sql(...)` with a SQL reader;
Scala and R with simpler pattern matching):

- reading tables: `spark.read.table`, `spark.table`, `FROM` / `JOIN` / `USING` in SQL,
  `DeltaTable.forName`, `delta_scan(...)` in DuckDB, `pd.read_*` on lakehouse paths
- writing tables: `saveAsTable`, `insertInto`, `writeTo`, `.save("Tables/...")`,
  `INSERT` / `MERGE` / `CREATE TABLE` / `UPDATE` / `DELETE`, `DeltaTable` merges, `write_deltalake`,
  `to_lakehouse_table`
- maintenance: `OPTIMIZE`, `VACUUM` (shown dotted; it doesn't move data)
- files: `Files/...`, `/lakehouse/default/...` and `abfss://...@onelake...` paths, `notebookutils.fs.cp/mv`
- other notebooks: `%run`, `notebookutils.notebook.run` and `runMultiple` (including DAGs)
- semantic models through semantic link: `fabric.read_table`, `evaluate_dax`, `refresh_dataset`

Names are worked out where the code makes it possible: f-strings, constants, loops over lists and
`dict.items()`, helper-function arguments, and constants from a notebook pulled in with `%run`.
Table names a notebook only builds while it runs (from widgets, the runtime context, a config
table) can't be known by reading the code; they are listed under *Names built at run time* with
the code line, not drawn on the map. Table names are matched the way Spark resolves them:
`t` (default lakehouse), `lakehouse.t`, `lakehouse.schema.t` and `workspace.lakehouse.t`.

## Permissions

- **The map and lakehouse tables:** the Viewer role on the workspace is enough.
- **Reading a notebook, semantic model or pipeline** (`getDefinition`): Fabric requires write
  access to the item, which means the Contributor role or higher, or edit permission on the item.
  Without it the item turns red and the details panel says so; everything else keeps working.
- **Schema-enabled lakehouses:** Fabric's tables API doesn't support them, so their tables are
  listed from OneLake, using the same sign-in.
- **Column names and Files folders** are read from OneLake, which needs read access to the
  lakehouse's data (the Contributor role, or the ReadAll permission on the lakehouse). Without it,
  the Table content tab says so and the rest keeps working.

## Settings (fabric_explorer\config.py)

| Setting | Meaning |
|---|---|
| `REST["workspace_id"]` | The workspace to open: its address from the browser, or its ID. Empty = choose in the app |
| `REST["tenant_id"]`, `REST["client_id"]` | Only for guest access to another tenant, or a tenant that requires its own app registration |
| `LOGIN_HINT` | Your email, to pre-fill the sign-in page |
| `PORTAL_URL` | Where *Open in Fabric* links go (`https://app.fabric.microsoft.com` or `https://app.powerbi.com`) |
| `SCAN_CONCURRENCY` | How many items *Read all* reads at the same time (default 4) |
| `OPEN_GROUPS` | Item types shown on the map as soon as a workspace opens |
| `HOST`, `PORT` | The local page's address (if the port is taken, a free one is used) |

## Command line

```
python run.py [address or ID]          open the explorer
python run.py --demo                   the demo workspace, no sign-in
python run.py <address> --scan NAME    print one item's links in the console and exit
python run.py --config FILE            take the REST block from another config.py
python run.py --sign-out               forget the remembered sign-in
python run.py --port 9000 --no-browser
```

## Troubleshooting

- **"The workspace ID in that address isn't complete"**: copy the address again from the browser's
  address bar while the workspace is open; the ID has the form `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.
- **A notebook is red: "needs write access"**: see Permissions. Ask for the Contributor role,
  or explore with someone who has it.
- **"This page belongs to an earlier run"**: the page was left open from a previous start; open
  the address the console printed this time.
- **Sign-in window doesn't appear**: check the console, sign in once in the browser it opened,
  or run `python run.py --sign-out` and start again.
- **A link to a type of item opens the workspace instead of the item**: the portal address for
  that item type isn't known to the app; the list of addresses is `SEGMENTS` in
  `fabric_explorer\links.py`.

## Project layout

```
run.py                     start here
Start Fabric Explorer.bat  the same, by double-click
fabric_explorer/
  config.py                your settings
  auth.py                  the one browser sign-in (same as pims-explorer)
  fabric_api.py            REST calls: paging, getDefinition operations, throttling, request log
  links.py                 pasted addresses -> workspace / item; items -> portal links
  session.py               what is known: workspace map, cached reads, one fetch per item
  scanners.py              what selecting each item type reads, turned into nodes and links
  graph.py                 node ids and how names and paths are placed on lakehouses and tables
  parse/                   notebook, SQL, OneLake path, pipeline and semantic model parsers
  server.py                the local page and its API (127.0.0.1 only, per-run key)
  demo.py                  the demo workspace (also used by the tests)
  web/                     the page: index.html, app.js, app.css
tests/                     python -m pytest
```
