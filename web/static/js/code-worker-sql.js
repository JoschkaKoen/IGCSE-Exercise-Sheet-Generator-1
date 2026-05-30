// sql.js Web Worker for the Code page (SQL / Databases course).
//
// Mirrors code-worker.js / code-worker-java.js's message protocol so
// code-playground.js drives it unchanged:
//   in:  {init} | {run, code, seed} | {check, code, seed, check}
//   out: {ready} | {initError, error} | {stdout, text} | {stderr, text}
//        | {runDone, ok} | {checkDone, pass, error, output, expected, kind}
//
// Student SQL runs HERE via sql.js — SQLite compiled to WebAssembly — entirely in
// the browser; nothing touches the server. Each run/check opens a FRESH in-memory
// database, applies the task's `seed` (CREATE + INSERT), runs the student's SQL,
// then closes the DB. Loaded as a CLASSIC worker (NOT {type:"module"}) because
// sql.js ships an Emscripten UMD loader pulled in with importScripts(); everything
// is same-origin, so it works under the page's COEP: require-corp.
//
// The result→text renderer (renderGrid / fmtCell) is kept BYTE-IDENTICAL to
// web/sql_runner.py's render_grid / _fmt, so the validator (Python sqlite3) and
// this worker (sql.js) — the same SQLite engine — produce the same canonical text
// we string-compare. See that module's docstring for the number-format contract.

importScripts("/static/vendor/sqljs/sql-wasm.js");

// Canonical render constants — MUST match web/sql_runner.py exactly.
const SEP = " | ";
const NULL_TOKEN = "NULL";
const DP = 6;

let SQL = null;
let ready = false;

function post(type, extra) { self.postMessage(Object.assign({ type }, extra || {})); }

// ── result rendering (mirror of web/sql_runner.py) ──────────────────────────────

function fmtCell(v) {
  if (v === null || v === undefined) return NULL_TOKEN;
  if (typeof v === "boolean") return v ? "1" : "0";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return String(v);            // 2 or 2.0 -> "2"
    return v.toFixed(DP).replace(/0+$/, "").replace(/\.$/, "");
  }
  if (v instanceof Uint8Array) return new TextDecoder().decode(v);  // BLOB
  return String(v);
}

// columns: string[]; rows: any[][]. With ordered=false the row lines are sorted
// (set semantics for queries without ORDER BY).
function renderGrid(columns, rows, ordered) {
  const header = columns.join(SEP);
  let body = rows.map((r) => r.map(fmtCell).join(SEP));
  if (!ordered) body = body.slice().sort();
  return [header].concat(body).join("\n");
}

// ── init ────────────────────────────────────────────────────────────────────────

async function init() {
  try {
    SQL = await initSqlJs({ locateFile: (f) => "/static/vendor/sqljs/" + f });
    ready = true;
    post("ready");
  } catch (err) {
    post("initError", { error: String((err && err.message) || err) });
  }
}

// ── run (free) ────────────────────────────────────────────────────────────────

function runFree(payload) {
  if (!ready) { post("stderr", { text: "SQL engine is not ready.\n" }); post("runDone", { ok: false }); return; }
  let db = null;
  try {
    db = new SQL.Database();
    if (payload.seed) db.run(payload.seed);
    const res = db.exec(payload.code || "");
    if (res.length) {
      // Render every result set the student's statements produced.
      const text = res.map((r) => renderGrid(r.columns, r.values, true)).join("\n\n");
      post("stdout", { text: text + "\n" });
    } else {
      const n = db.getRowsModified();
      post("stdout", { text: "→ " + n + " row" + (n === 1 ? "" : "s") + " affected.\n" });
    }
    post("runDone", { ok: true });
  } catch (err) {
    post("stderr", { text: String((err && err.message) || err) + "\n" });
    post("runDone", { ok: false });
  } finally {
    if (db) { try { db.close(); } catch (e) { /* ignore */ } }
  }
}

// ── check (kind: rows) ──────────────────────────────────────────────────────────

function runCheck(payload) {
  const check = payload.check || {};
  const expected = check.expected != null ? String(check.expected) : null;
  if (!ready) { post("checkDone", { pass: false, error: "SQL engine is not ready.", output: "", expected, kind: "rows" }); return; }

  const ordered = check.ordered !== false;   // default true
  let db = null, error = null, output = "";
  try {
    db = new SQL.Database();
    if (payload.seed) db.run(payload.seed);
    let cols = [], rows = [];
    if (check.probe) {
      // Mutations first (INSERT/UPDATE/DELETE/CREATE — may be several statements),
      // then the single probe SELECT reads the resulting state.
      if (payload.code && payload.code.trim()) db.run(payload.code);
      const res = db.exec(check.probe);
      if (res.length) { const last = res[res.length - 1]; cols = last.columns; rows = last.values; }
    } else {
      // The student's query; take the LAST result set (lenient to multi-statement).
      const res = db.exec(payload.code || "");
      if (res.length) { const last = res[res.length - 1]; cols = last.columns; rows = last.values; }
    }
    output = renderGrid(cols, rows, ordered);
  } catch (err) {
    error = String((err && err.message) || err);
  } finally {
    if (db) { try { db.close(); } catch (e) { /* ignore */ } }
  }

  let pass = false;
  if (!error) pass = output.trim() === (expected || "").trim();
  post("checkDone", { pass, error, output, expected, kind: "rows" });
}

self.onmessage = (e) => {
  const msg = e.data || {};
  if (msg.type === "init") init();
  else if (msg.type === "run") runFree(msg);
  else if (msg.type === "check") runCheck(msg);
};
