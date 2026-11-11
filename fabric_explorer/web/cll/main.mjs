// Entry point of the two lineage tabs: builds their views and registers them with the app (app.js).
//   columns    Column-Level Lineage: the columns that change from dataset to dataset, into Power BI
//   notebooks  Notebook-Level Lineage: what one notebook or pipeline does to every column
import {createColumnLineageView} from "./view.mjs";

const fx = window.fabricExplorer;
for (const [id, mode, view] of [["cllLayout", "datasets", "columns"], ["nllLayout", "notebook", "notebooks"]]) {
  const root = document.getElementById(id);
  if (fx && fx.host && root) fx.registerView(view, createColumnLineageView(root, fx.host, {mode, view}));
}
