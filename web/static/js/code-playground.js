// Code page playground — wires CodeMirror editors to a Pyodide Web Worker.
//
// One worker per page, lazily booted on the first Run/Check. Python runs in the
// worker so the main thread can (a) interrupt infinite loops via a shared
// interrupt buffer (Stop) and (b) feed live input() through a SharedArrayBuffer
// the worker blocks on with Atomics.wait. Checks also run in the worker, but
// with scripted stdin and captured output — deterministic, never blocking.

const PROGRESS_KEY = "code.progress.v1";

function t(key) {
  return (window.i18n && window.i18n[key]) || key;
}

const ROOT = document.querySelector(".code-lesson");
if (ROOT) init(ROOT);

function init(root) {
  // Cross-origin isolation + SharedArrayBuffer are required (Stop + live input).
  // Target browsers are modern, so this guard should essentially never fire.
  if (!self.crossOriginIsolated || typeof SharedArrayBuffer === "undefined") {
    const warn = document.getElementById("code-runtime-warning");
    if (warn) warn.hidden = false;
    root.querySelectorAll(".code-run, .code-check, .code-stop").forEach((b) => { b.disabled = true; });
    return;
  }

  const slug = root.dataset.slug;
  const nn = root.dataset.nn;
  const tasks = readTasks();
  const editors = {};
  const encoder = new TextEncoder();

  // Shared memory for the worker.
  const interruptSab = new SharedArrayBuffer(4);
  const stdinMetaSab = new SharedArrayBuffer(8);
  const stdinDataSab = new SharedArrayBuffer(8192);
  const interruptView = new Int32Array(interruptSab);
  const stdinMeta = new Int32Array(stdinMetaSab);
  const stdinData = new Uint8Array(stdinDataSab);

  let worker = null;
  let workerReady = false;
  let booting = null;
  let active = null;   // { taskId, mode: "run" | "check" }

  initEditors();

  function initEditors() {
    // CodeMirror 5 loads as a classic deferred script; wait until the global exists.
    if (!window.CodeMirror) { setTimeout(initEditors, 30); return; }
    root.querySelectorAll("textarea.code-editor").forEach((ta) => {
      editors[ta.dataset.taskId] = window.CodeMirror.fromTextArea(ta, {
        mode: "python",
        theme: "dracula",
        lineNumbers: true,
        indentUnit: 4,
        matchBrackets: true,
        autoCloseBrackets: true,
        styleActiveLine: true,
        viewportMargin: Infinity,
      });
    });
    wireButtons();
    markCompletedFromStorage();
  }

  function wireButtons() {
    root.querySelectorAll(".code-run").forEach((b) =>
      b.addEventListener("click", () => runTask(b.dataset.taskId)));
    root.querySelectorAll(".code-check").forEach((b) =>
      b.addEventListener("click", () => checkTask(b.dataset.taskId)));
    root.querySelectorAll(".code-reset").forEach((b) =>
      b.addEventListener("click", () => resetTask(b.dataset.taskId)));
    root.querySelectorAll(".code-stop").forEach((b) =>
      b.addEventListener("click", stopActive));
    root.querySelectorAll(".code-input").forEach((inp) =>
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); submitInput(inp.dataset.taskId); }
      }));
  }

  // ---- Worker lifecycle --------------------------------------------------
  function ensureWorker() {
    if (workerReady) return Promise.resolve();
    if (booting) return booting;
    booting = new Promise((resolve, reject) => {
      worker = new Worker("/static/js/code-worker.js", { type: "module" });
      worker.onmessage = (e) => handleWorker(e.data || {}, resolve, reject);
      worker.onerror = (e) => { booting = null; reject(new Error(e.message || "worker failed to load")); };
      worker.postMessage({ type: "init", interruptSab, stdinMetaSab, stdinDataSab });
    });
    return booting;
  }

  function handleWorker(msg, bootResolve, bootReject) {
    switch (msg.type) {
      case "ready": workerReady = true; bootResolve(); break;
      case "initError": booting = null; bootReject(new Error(msg.error)); break;
      case "stdout": if (active) appendConsole(active.taskId, msg.text, "out"); break;
      case "stderr": if (active) appendConsole(active.taskId, msg.text, "err"); break;
      case "inputRequest": if (active && active.mode === "run") showInput(active.taskId); break;
      case "runDone": finishRun(); break;
      case "checkDone": finishCheck(msg); break;
    }
  }

  // ---- Run (free) --------------------------------------------------------
  async function runTask(id) {
    if (active) return;
    active = { taskId: id, mode: "run" };
    setBusy(id, true);
    clearConsole(id);
    hideResult(id);
    Atomics.store(interruptView, 0, 0);
    if (!workerReady) {
      appendConsole(id, t("code.console.loading"), "sys");
      try { await ensureWorker(); }
      catch (err) { appendConsole(id, String(err.message || err), "err"); finishRun(); return; }
      clearConsole(id);
    }
    worker.postMessage({ type: "run", code: editors[id].getValue() });
  }

  function finishRun() {
    const id = active ? active.taskId : null;
    active = null;
    if (id != null) { setBusy(id, false); hideInput(id); }
  }

  // ---- Check -------------------------------------------------------------
  async function checkTask(id) {
    if (active) return;
    active = { taskId: id, mode: "check" };
    setBusy(id, true);
    const box = resultBox(id);
    box.hidden = false;
    box.className = "code-result mt-2 code-result-pending";
    box.textContent = t("code.task.checking");
    Atomics.store(interruptView, 0, 0);
    if (!workerReady) {
      try { await ensureWorker(); }
      catch (err) {
        box.className = "code-result mt-2 code-result-fail";
        box.textContent = String(err.message || err);
        active = null; setBusy(id, false); return;
      }
    }
    const task = tasks[id] || {};
    worker.postMessage({ type: "check", code: editors[id].getValue(), stdin: task.stdin || "", check: task.check || {} });
  }

  function finishCheck(msg) {
    const id = active ? active.taskId : null;
    active = null;
    if (id == null) return;
    setBusy(id, false);
    const box = resultBox(id);
    box.hidden = false;
    if (msg.pass) {
      box.className = "code-result mt-2 code-result-pass";
      box.textContent = t("code.task.pass");
      markDone(id);
    } else if (msg.error) {
      box.className = "code-result mt-2 code-result-fail";
      box.textContent = t("code.task.error");
      clearConsole(id);
      appendConsole(id, msg.error, "err");
    } else {
      box.className = "code-result mt-2 code-result-fail";
      box.innerHTML = "";
      box.appendChild(textDiv(t("code.task.fail")));
      if (msg.kind === "stdout") {
        box.appendChild(diffLine(t("code.task.expected"), msg.expected));
        box.appendChild(diffLine(t("code.task.got"), msg.output));
      }
    }
  }

  // ---- Stop / live input -------------------------------------------------
  function stopActive() {
    Atomics.store(interruptView, 0, 2);   // SIGINT for CPU-bound loops
    Atomics.store(stdinMeta, 0, 2);        // EOF to unblock a pending input()
    Atomics.notify(stdinMeta, 0);
    if (active) appendConsole(active.taskId, t("code.console.stopped"), "sys");
  }

  function submitInput(id) {
    const inp = root.querySelector(`.code-input[data-task-id="${id}"]`);
    if (!inp) return;
    appendConsole(id, inp.value, "in");
    const bytes = encoder.encode(inp.value + "\n");
    const n = Math.min(bytes.length, stdinData.length);
    stdinData.set(bytes.subarray(0, n));
    Atomics.store(stdinMeta, 1, n);
    Atomics.store(stdinMeta, 0, 1);
    Atomics.notify(stdinMeta, 0);
    inp.value = "";
  }

  function showInput(id) {
    const row = root.querySelector(`.code-input-row[data-task-id="${id}"]`);
    if (!row) return;
    row.hidden = false;
    const inp = row.querySelector(".code-input");
    if (inp) inp.focus();
  }
  function hideInput(id) {
    const row = root.querySelector(`.code-input-row[data-task-id="${id}"]`);
    if (row) row.hidden = true;
  }

  // ---- Reset -------------------------------------------------------------
  function resetTask(id) {
    if (active) return;
    const task = tasks[id] || {};
    if (editors[id]) editors[id].setValue(task.starter || "");
    clearConsole(id);
    hideResult(id);
  }

  // ---- Console / result helpers -----------------------------------------
  function consoleEl(id) { return root.querySelector(`.code-console[data-task-id="${id}"]`); }
  function resultBox(id) { return root.querySelector(`.code-result[data-task-id="${id}"]`); }
  function clearConsole(id) { const p = consoleEl(id); if (p) p.innerHTML = ""; }
  function hideResult(id) { const b = resultBox(id); if (b) { b.hidden = true; b.textContent = ""; } }

  function appendConsole(id, text, kind) {
    const pre = consoleEl(id);
    if (!pre) return;
    const empty = pre.querySelector(".code-console-empty");
    if (empty) empty.remove();
    const span = document.createElement("span");
    span.className = "code-line code-line-" + (kind || "out");
    span.textContent = text;
    pre.appendChild(span);
    pre.scrollTop = pre.scrollHeight;
  }

  function textDiv(text) { const d = document.createElement("div"); d.textContent = text; return d; }
  function diffLine(label, value) {
    const d = document.createElement("div");
    d.className = "code-result-line";
    const l = document.createElement("span");
    l.className = "code-result-label";
    l.textContent = label + " ";
    d.appendChild(l);
    d.appendChild(document.createTextNode(value == null ? "" : value));
    return d;
  }

  // ---- Busy state --------------------------------------------------------
  function setBusy(activeId, busy) {
    root.querySelectorAll(".code-run, .code-check, .code-reset").forEach((b) => { b.disabled = busy; });
    root.querySelectorAll(".code-stop").forEach((b) => {
      b.disabled = !(busy && b.dataset.taskId === activeId);
    });
  }

  // ---- Progress (localStorage) ------------------------------------------
  function readProgress() {
    try { return JSON.parse(localStorage.getItem(PROGRESS_KEY)) || {}; }
    catch (e) { return {}; }
  }
  function markDone(id) {
    const sec = root.querySelector(`.code-task[data-task-id="${id}"]`);
    if (sec) sec.classList.add("code-done");
    const prog = readProgress();
    const key = `${slug}/${nn}/${id}`;
    const prev = prog[key] || {};
    prog[key] = { done: true, ts: Date.now(), attempts: (prev.attempts || 0) + 1 };
    try { localStorage.setItem(PROGRESS_KEY, JSON.stringify(prog)); } catch (e) { /* quota */ }
  }
  function markCompletedFromStorage() {
    const prog = readProgress();
    Object.keys(tasks).forEach((id) => {
      const entry = prog[`${slug}/${nn}/${id}`];
      if (entry && entry.done) {
        const sec = root.querySelector(`.code-task[data-task-id="${id}"]`);
        if (sec) sec.classList.add("code-done");
      }
    });
  }

  function readTasks() {
    const el = document.getElementById("code-tasks");
    const map = {};
    if (!el) return map;
    try { (JSON.parse(el.textContent) || []).forEach((task) => { map[task.id] = task; }); }
    catch (e) { /* malformed island */ }
    return map;
  }
}
