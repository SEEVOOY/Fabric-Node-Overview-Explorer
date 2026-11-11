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

Fabric Explorer starts on the workspace map, where the workspace is chosen. The grey dotted lines (default
lakehouse or warehouse, attached lakehouses, environment) are drawn only while they tell something:
not once the map shows the item reading or writing in that lakehouse, nor on an item with lines both
in and out.

- **Select an item** on the map or in the list on the left. Its content is read once and the
  map draws its links. A dashed outline means *not read yet*, a solid one *read*, and red
  *couldn't be read* (the details panel says why).
- **Open in Fabric.** Ctrl+click an item, click the arrow at its corner, use *Open in Fabric* in
  the details panel, or the arrow next to it in the list.
- **Lineage.** Selecting something highlights everything upstream and downstream of it
  (*Full lineage*), or only what is directly linked (*Direct links*). *Selected only* turns this
  off: clicking an item just selects it (its details show on the right) and nothing else is
  highlighted, dimmed or rearranged, which makes moving items around easier. Asking for a lineage
  on purpose (right-click *Show its lineage* or a thread, *Show all its relations*, *Pin its
  highlight*) switches back to *Full lineage* so it can be seen. The choice is remembered. In the details panel, every
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
- **Warehouses work like lakehouses.** Selecting a warehouse reads its tables, by schema, from its
  Tables folder in OneLake (warehouses keep their tables there as Delta tables). It then gets
  everything a lakehouse has: its own column in the Structured view with a folder per schema, the
  **+ / −** on its box, the right-click choices (collapsed, folders only, all datasets, linked
  tables only, show / hide columns), the dropdown and *Folders* / *Contents* tickboxes in the left
  panel, and the same controls in its details. Column names come from the tables' Delta logs.
  Notebooks that read or write warehouse tables with the Spark connector (`synapsesql`), pipelines
  that copy into a warehouse, and three-part names in SQL are linked to its tables. Listing a
  warehouse's tables needs read access to its OneLake data (Contributor, or ReadAll).
- **Open a lakehouse on the map.** Every lakehouse node has a **+** at its right end: click it
  to load its datasets onto the map, shown under their folders (the lakehouse is read first if it
  hasn't been). **−** collapses it again: its folders and datasets leave the map and notebook links
  point at the lakehouse itself. Folder nodes have their own **+ / −** to open or close one folder at
  a time. A lakehouse with more than 300 tables and folders asks first; answering no shows just its
  folders, each with its own **+**. Right-click a lakehouse to choose how much of it is open:
  *Collapsed: just the lakehouse*, *Folders only, each collapsed* (its datasets leave the map and
  links to them point at their folder; **+** on a folder opens it) or *Folders and all datasets*;
  the current choice is ticked, and a line under its name says how many of its datasets are on
  the map. *Linked tables only* (in the same menu, or a tickbox in the lakehouse's details) leaves
  out its datasets that have no line to anything on the map, and folders with nothing linked in
  them; it follows the map as you read or tick items, works in focus mode too, and choosing one of
  the three above switches it off. The left panel's tickboxes keep working alongside and follow
  what you do here.
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
  Right-click a table, a folder, a lakehouse or a group of lakehouses (or a selection of several of
  them) for *Show columns*, *Show columns in use* and *Hide columns*; for a folder, lakehouse or group
  *Show* works on its tables that are on the map and *Hide* on every one of its tables that has
  columns on (also ones collapsed away), and the menu says how many. With *Folders* off, a
  lakehouse's folders are headings in its column (*Tables*, *Files*, or the schema names): those can
  be right-clicked too, for the same column choices and *Expand* / *Collapse* of their datasets.
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
  what's highlighted*) shows only the lineage of the selected (or pinned) item, also the parts you
  had unticked, without changing any tickbox. Switching *Direct links* / *Full lineage*, or a
  dataset's *Follow* direction, works the focus out again; the bar above the map says what it is
  focused on. Inside it you can click and explore freely: the map doesn't grow beyond the focus,
  ticking an item adds it and unticking takes it out. *Leave focus* (or Esc with nothing selected)
  brings everything back as it was.
- **Opening a table in Fabric.** Fabric can open its explorer with one table previewed (*…* next
  to the table, *Copy URL*), but doesn't publish what those links look like, so *Open in Fabric*
  on a table opens its lakehouse until you show the app one: in a table's details, open *Open the
  table itself in Fabric* and paste a link copied that way. The app finds the workspace, the
  lakehouse, the schema and the table in it, and from then on builds the same kind of link for
  every table (warehouses learn their own the same way). It's remembered in this browser;
  *Forget that link* goes back to opening the lakehouse.
- **Right-click an item on the map** for a menu: the thread options for tables and folders,
  *Focus on what's highlighted*, *Remove from view* (with several selected and the item among
  them, it removes them all) and *Open in Fabric*. Right-drag still pans.
- **Structured or Free** (top of the map). The app opens in *Structured*. *Free* is the map
  described above: items find their place by their links, and you can drag them. *Structured* arranges the same nodes tidily by
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
  with its tables, and everything inside a lakehouse stays inside it: dragging a dataset moves it up
  or down within its folder (a blue line shows where it will go, and the others make room when you
  let go), and dragging a folder moves it, with its datasets, among the lakehouse's folders. Your
  order is kept; *Re-layout* goes back to name order. Hold Shift and drag on empty space to draw a selection box (the cursor turns into
  a crosshair): everything inside is selected, and dragging any selected item moves them all
  together. Ctrl+Shift+drag adds another box to the selection, Shift+click adds or removes a single
  item, Esc or a click on empty space clears it. Dragging any selected item moves the whole selection;
  a selected dataset brings its lakehouse along. In the Structured view your moves are kept when the
  map changes (new reads, tickboxes); *Re-layout* puts everything back in the tidy arrangement.
  While you drag in the Structured view, an item snaps into line with a nearby one (centres or
  edges within a few pixels; dashed guide lines show it; hold Alt to place it freely), and nothing
  can end up on top of something else: an item dropped onto another is nudged to the nearest free
  spot, and this is kept after every change to the map. When two would end up in the same place,
  the item you clicked, pinned or just dropped keeps the spot and the other one moves aside (then
  the highlighted chain wins over the rest). *Snap and keep items apart* in *View* turns
  both off. The right or middle mouse button, or Space+drag, also pans. With several selected, the
  details panel lists them with *Zoom to them*, *Focus on the highlighted*, *Take off the map* and
  *Read the ones not read yet*.
- **A lakehouse's details** have *Folders on the map: Show all / Hide all* (all its schemas and
  folders at once) and *Columns of its tables on the map: Off / All / In use* (for its tables that
  are on the map). Clicking a table or folder name in any list takes the map to it: it is centred
  and pulses briefly.
- **View** (top of the map) switches tables, lakehouse areas, groups, default-lakehouse lines,
  the left-to-right arrangement, and snapping / keeping items apart on and off. **Export** saves the relationships as a CSV for Excel,
  or the whole map as JSON.
- **Requests to Fabric** (bottom bar) lists every API call the app made, so you can see that
  opening a workspace is cheap and an item is only fetched when you select it.

Keys: `/` search, `F` fit the map, `Esc` clear the selection, `+` / `-` zoom, Space+drag pan.

## Column-Level Lineage and Notebook-Level Lineage (the lineage tabs)

Two tabs next to *Reload* show what happens to each column. **Column-Level Lineage** is the overview:
the columns that change from dataset to dataset, through the notebooks and pipelines between them,
into Power BI. **Notebook-Level Lineage** is one notebook or pipeline in depth: every column of its
datasets and each piece of its code. The column lineage It is
worked out from the notebook's code when the notebook is read, so it costs no extra calls to Fabric;
only the datasets' column names are read from OneLake (the same reads as the Table content tab).

- **Open them** from the tabs, or by right-clicking on the map: a notebook or pipeline (*Focus on column
  lineage*) opens Notebook-Level Lineage; a table, a semantic model or a report (*Show its column
  changes*) opens Column-Level Lineage on it. Their details panels have the same buttons.
- **Several at once.** Shift+click adds a dataset, a column, a definition or a card to the selection
  (Shift+click it again takes it out), and Shift+drag on the empty sheet draws a box around more. Their
  lines are lit together, dragging one of them moves them all, and in Column-Level Lineage *Focus on
  their threads* keeps just theirs. The details panel lists them (click one to keep only that one);
  Esc or *Clear the selection* lets go.
- **Fold a dataset's columns** with the ⌄ in its heading or by right-clicking it (*Fold its columns*):
  its lines then meet at its heading. Right-clicking the sheet folds or unfolds every dataset, and
  *Unfold all* opens them again. What is folded is kept, per tab.
- **Right-click in the lineage tabs** for what you can do with what is under the mouse: highlight its
  lines (or a column's path), focus on its thread, open a notebook in Notebook-Level Lineage or a
  dataset, model or report in Column-Level Lineage, show it on the map, open it in Fabric, or copy a
  column's name. On the empty sheet: fit, re-layout, stop highlighting.
- **Notebook-Level Lineage.** Pick a notebook or pipeline on the left. Both its sides are always there:
  a dataset the map links to it that its code couldn't be followed into (a class, a function never
  called, a library) is shown on its side, joined through a card for the notebook, and *Changed only*
  never hides a side. A streaming `foreachBatch` function is read through. You see the datasets it reads (left) and writes (right),
  with all their columns, and between them the notebook's definitions, in a frame with the notebook's
  name. The name opens the notebook in Fabric; with *Full lineage*, *Focus on it* next to another
  notebook's name switches to that notebook. Every column has its own line. It shows that notebook only: its own datasets and its own code. To
  follow its columns on through other notebooks and into Power BI, use Column-Level Lineage
  (right-click a dataset: *Show its column changes*).
- **Definitions** are drawn like instrument tags on a process diagram: a balloon with letters above the
  line and the cell and line of the code below it, next to a box with the code (on up to two lines,
  with a stripe in the category's colour; hover or click it for all of it). The letters say what the code does:

  | Letter | Column definitions | Letter | Row definitions (every column passing goes through) |
  |---|---|---|---|
  | S | Text: upper, trim, concat, substring, regexp_replace | F | Filter rows: filter, where, dropDuplicates, distinct, limit |
  | N | Number: + - * /, round, abs, greatest | J | Join and union: join, union, unionByName |
  | D | Date and time: to_date, date_format, current_timestamp | G | Group rows: groupBy, GROUP BY, pivot |
  | C | Condition and nulls: when / otherwise, CASE, coalesce, isNull | M | Merge (upsert): MERGE INTO, DeltaTable.merge, UPDATE |
  | T | Type change: cast, astype, to_number | P | Copy (pipeline): a Copy activity and its column mapping |
  | A | Aggregate: sum, count, avg, max | | |
  | W | Window: row_number, rank, lag, over | | |
  | X | Arrays, maps and JSON: explode, struct, from_json | | |
  | H | Hash and IDs: md5, sha2, xxhash64, uuid | | |
  | L | Constant: lit, a fixed value | | |
  | R | Rename: alias, withColumnRenamed, AS | | |
  | U | Own code: a UDF or a helper the app can't see into | | |

  A row definition is drawn as a bar with one lane per column passing through it, so the columns go
  straight through and you can see which ones share it. The same code applied to several columns (a
  loop such as `for c in cols: df = df.withColumn(c, F.trim(F.col(c)))`, or a `transform(...)` of every
  column) is one node they all go through: by default a block with a lane for each column, so every
  line goes straight through and can be followed (*Lanes*); *Bundled* gathers them into one point. A dashed line is a column a definition looks at without passing it on (a join key that
  isn't kept, a filter on a dropped column). In a dataset, green marks a column the notebook changes
  (*new*, *altered* or *renamed*); grey ones aren't used, or aren't written by this notebook.
- **Pipelines.** A pipeline's Copy activities take part like a notebook: every column goes through the
  copy (P), and the column mapping (the activity's translator) gives renames (R) and type changes (T).
  Without a mapping the columns are copied by name. Pipelines are listed under the notebooks; under
  the line in their balloons is the activity (A1 is the first Copy activity).
- **Into Power BI.** In Column-Level Lineage, the semantic models that read the
  datasets are drawn too: the model's columns that read the dataset columns, the measures and
  calculated columns built on them (DAX balloons: their letters come from the DAX functions, so SUM is
  A and IF is C), and the report visuals that show any of them, page by page. Yellow lines go into
  Power BI. Follow a column and you see which reports a change to it reaches, before you make it.
  Semantic models and reports are read like other items (*Read the other N* reads them too).
- **Column-Level Lineage** shows the datasets and the notebooks between them, with only the columns that
  change: **black lines** for the dataset relations (a notebook reads one dataset and writes
  another), **green lines** for the changed columns, from the columns they come from, through the
  notebook's definition, to where they are written. Each notebook lists its definitions with their
  letters, and what it does to rows in its heading. Pick a dataset, a semantic model or a report on
  the left to follow its thread (a model or report: everything upstream of what it reads, and all its
  columns)
  (*Inputs*, *Both ways*, *Outputs*; *Direct links* or *Full lineage*). *All columns* draws every
  column's whole path, the ones copied unchanged too, so any column can be followed all the way;
  *Changed only* (the default) keeps to the columns that change. Thin grey lines carry a changed column on through copies (silver to gold, say),
  so its thread reaches Power BI.
- **Focus** (in Column-Level Lineage). Select a column, a dataset or a notebook and press *Focus*: only
  its thread is drawn, laid out on its own, with the relations along it. *Leave focus* shows everything.
- **Wide tables.** *Changed only* (in Notebook-Level Lineage) keeps the columns the notebook changes and the
  paths into them, and counts the rest. *Find a column* (or /) finds a column, field, visual or
  definition in the drawing; pick it to select it and move to its row.
- **Links and export.** The address bar follows the view, and *Copy link* copies a link that opens
  the same view (the notebook or dataset, the scope, the selected column, focus). It opens in Fabric
  Explorer while it runs in this browser; it can also be pasted on the page (Ctrl+V). *Export* saves
  *this view* or *everything read* as CSV (one row per column path: target, source, the definitions
  and where they are) or JSON; the JSON of everything read adds OpenLineage column lineage facets
  (one job per notebook or pipeline), for a data catalog.
- **Following a dataset, notebook or card.** Select one (click, or right-click: *Highlight its lines*)
  and its whole lineage lights up both ways: where it goes, all the way to the last node, and where it
  comes from, along the lines between datasets and notebooks too.
- **Following a column.** Hover a column to light up its path through the notebooks (only its own
  path, also through a definition it shares with other columns); click to keep it lit while you move
  around (Esc, or a click on the empty sheet, lets go). Drag the sheet to move around and scroll to
  zoom; drag a node (a dataset, a definition, a card) to move it, and its lines go straight to it
  (*Re-layout* puts everything back); lines that share both ends
  keep their spread, so a bundle stays a ribbon. Lines are drawn as directly as they can: the lanes of a shared
  definition and the rows of a card take the order their lines come in, and a long line goes straight
  when nothing stands in its way. *Fit* or F shows everything, and the drawing stays where you put it when you
  click or come back to the tab. Dragging never selects the drawing's text, so the browser doesn't
  offer to copy it. The details
  panel shows how it is made, step by step with the code, and where it goes. *Show on the map* takes
  you back to the map with the table or notebook selected.
- **Functions.** The notebook's own functions, and those of the notebooks it pulls in with `%run`, are
  read through, `transform(...)` included; a definition made inside one says so, with the first line of
  the function's docstring and the notebook it comes from. A function that can't be read (from a
  library) is read by the columns its call names (`domain_col="Domain"`, `columns=[...]`,
  `out_col="SiteCode"`): those go through it, and the other columns pass it by; when it names none,
  every column goes through it.
- **What is followed:** pyspark (`spark.read`/`spark.table`, select, selectExpr, withColumn(s),
  withColumnRenamed, drop, toDF, filter/where, distinct/dropDuplicates, limit, fillna/na, replace,
  join, union(ByName), groupBy/agg/count/sum/…, transform, writes with saveAsTable, insertInto,
  save, writeTo, synapsesql), `spark.sql(...)` and `%%sql` cells (CTEs, joins, GROUP BY, UNION,
  INSERT, CREATE … AS, MERGE INTO, UPDATE, temporary views), Delta merges and updates, DuckDB with
  `delta_scan` and `write_deltalake`, and pandas basics. Loops over known lists run once per item;
  helper functions in the notebook are read through, and constants from `%run` notebooks are used.
  Scala and R cells, names built at run time, the inside of UDFs and the columns a pivot makes can't
  be followed; the details panel says so where it happens. When a table's columns can't be read, the
  columns the code names are shown (in italics) and *the other columns* stands for the rest.

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
| Column-Level Lineage of a notebook | nothing new for the code (it was read with the notebook); the column names of its datasets, as for Table content |
| Column-Level Lineage across datasets | the column names of the datasets read by notebooks and pipelines, once, as for Table content |
| Reading a report | also its definition, for the fields its visuals show (`POST /workspaces/{ws}/items/{id}/getDefinition`) |
| Export of everything read | nothing new beyond the column names (as above) |

With nothing selected, the details panel lists every type of item in the workspace (notebooks,
lakehouses, warehouses, pipelines, semantic models, reports, SQL analytics endpoints, …) with how
many of each have been read and a *Read* button per type, plus *Read all* for everything not read
yet; items that couldn't be read are counted as failed, and clicking a type's name lists them.

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
  column_lineage/          column-level lineage, from the notebooks' code:
    catalog.py               what Spark functions do: 16 categories, a letter each
    model.py                 the program a notebook is read into (frames, definitions, writes)
    expressions.py           one pyspark or SQL expression -> the columns it reads, what it does
    sql.py, dataframes.py    SQL statements and Python cells -> the program
    resolve.py               the program run against the datasets' real columns -> each column's path
    pipelines.py             a pipeline's Copy activities and their column mappings -> the program
    powerbi.py               semantic model columns and DAX, report visuals and the fields they show
    views.py, service.py     the two views as graphs, export (rows, OpenLineage), the page's requests
  server.py                the local page and its API (127.0.0.1 only, per-run key)
  demo.py                  the demo workspace (also used by the tests)
  web/                     the page: index.html, app.js, app.css
    cll/                     the Column-Level Lineage tab (ES modules): view.mjs, layout.mjs, graph.mjs,
                             render.mjs, cll.css
tests/                     python -m pytest (also runs tests/js with Node.js, and test_browser.py in Chromium
                             with Playwright, when they are installed; otherwise those are skipped)
```
