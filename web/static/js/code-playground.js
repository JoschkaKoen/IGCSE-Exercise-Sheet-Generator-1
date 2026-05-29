// Code page playground — CodeMirror editors + a Pyodide worker, runnable ```python
// examples, and a step-through reveal engine (one step at a time, Continue ▶).
//
// Output routing is element-based: `active.el` is the target <pre> (a task console
// OR an example output box), so tasks and examples share one code path. Python runs
// in a worker so the main thread can interrupt loops (Stop) and feed live input()
// through a SharedArrayBuffer the worker blocks on with Atomics.wait.

const PROGRESS_KEY = "code.progress.v1";
const STEPS_KEY = "code.steps.v1";
const PLAY_SVG = '<svg class="icon-play" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const STOP_SVG = '<svg class="icon-stop" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>';

function t(key) {
  return (window.i18n && window.i18n[key]) || key;
}

const ROOT = document.querySelector(".code-lesson");
if (ROOT) init(ROOT);

function init(root) {
  const slug = root.dataset.slug;
  const nn = root.dataset.nn;
  const tasks = readTasks();
  const editors = {};
  const encoder = new TextEncoder();

  let worker = null;
  let workerReady = false;
  let booting = null;
  let active = null;   // { el, mode: "run"|"check", kind: "task"|"example", taskId?, runBtn? }

  // Shared memory for the worker — only when cross-origin isolated (else
  // SharedArrayBuffer is undefined). Reading + stepping work without it.
  let interruptView = null, stdinMeta = null, stdinData = null;
  const isolated = self.crossOriginIsolated && typeof SharedArrayBuffer !== "undefined";
  if (isolated) {
    interruptView = new Int32Array(new SharedArrayBuffer(4));
    stdinMeta = new Int32Array(new SharedArrayBuffer(8));   // [0]=state, [1]=byte length
    stdinData = new Uint8Array(new SharedArrayBuffer(8192));
  } else {
    const warn = document.getElementById("code-runtime-warning");
    if (warn) warn.hidden = false;
  }

  initEditors();    // CodeMirror — regardless of isolation (typing needs no worker)
  wireExamples();   // ▶ on prose code blocks
  setupSteps();     // progressive-disclosure reveal engine

  if (!isolated) {  // execution disabled, but stepping/reading still works
    root.querySelectorAll(".code-run, .code-check, .code-stop, .code-example-run")
      .forEach((b) => { b.disabled = true; });
  }

  // ---- Step-through (progressive disclosure) ----------------------------
  function setupSteps() {
    const steps = Array.from(root.querySelectorAll(".code-step"));
    const wrap = root.querySelector(".code-continue-wrap");
    const btn = root.querySelector(".code-continue");
    if (!steps.length) { if (wrap) wrap.hidden = true; return; }

    const key = `${slug}/${nn}`;
    let revealed = 1;
    const saved = readSteps()[key];
    if (typeof saved === "number" && saved > revealed) revealed = Math.min(saved, steps.length);

    steps.forEach((step, i) => {
      step.hidden = i >= revealed;
      if (i < revealed) refreshTaskEditor(step);
    });
    updateContinue();

    if (btn) btn.addEventListener("click", () => {
      if (revealed >= steps.length) return;
      const step = steps[revealed];
      step.hidden = false;
      refreshTaskEditor(step);
      revealed++;
      saveSteps(key, revealed);
      updateContinue();
      step.scrollIntoView({ behavior: "smooth", block: "start" });
      step.focus({ preventScroll: true });
    });

    function updateContinue() {
      if (wrap) wrap.hidden = revealed >= steps.length;
    }
  }

  function refreshTaskEditor(step) {
    const tid = step.dataset.taskId;
    if (tid && editors[tid]) editors[tid].refresh();   // CM mis-measures while hidden
  }

  function readSteps() {
    try { return JSON.parse(localStorage.getItem(STEPS_KEY)) || {}; }
    catch (e) { return {}; }
  }
  function saveSteps(key, n) {
    const s = readSteps();
    s[key] = n;
    try { localStorage.setItem(STEPS_KEY, JSON.stringify(s)); } catch (e) { /* quota */ }
  }

  // ---- Editors -----------------------------------------------------------
  function initEditors() {
    // CodeMirror 5 loads as a classic deferred script; wait until the global exists.
    if (!window.CodeMirror) { setTimeout(initEditors, 30); return; }
    root.querySelectorAll("textarea.code-editor").forEach((ta) => {
      const cm = window.CodeMirror.fromTextArea(ta, {
        mode: "python",
        theme: "exercise",
        lineNumbers: false,
        indentUnit: 4,
        matchBrackets: true,
        autoCloseBrackets: true,
        styleActiveLine: true,
        viewportMargin: Infinity,
      });
      editors[ta.dataset.taskId] = cm;
      const step = ta.closest(".code-step");
      if (step && !step.hidden) cm.refresh();   // a task step restored visible before CM loaded
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
  }

  // Make each ```python block runnable: wrap it, add a ▶ pinned bottom-right,
  // and an output box as a sibling AFTER the wrapper.
  function wireExamples() {
    root.querySelectorAll(".code-prose pre > code.language-python").forEach((codeEl) => {
      const pre = codeEl.parentElement;
      if (!pre || pre.closest(".code-example")) return;
      const wrapEl = document.createElement("div");
      wrapEl.className = "code-example";
      pre.parentNode.insertBefore(wrapEl, pre);
      wrapEl.appendChild(pre);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "code-example-run";
      btn.setAttribute("aria-label", t("code.example.run"));
      btn.innerHTML = PLAY_SVG + STOP_SVG;
      wrapEl.appendChild(btn);

      const out = document.createElement("pre");
      out.className = "code-example-output";
      out.setAttribute("data-label", t("code.example.output"));
      out.hidden = true;
      wrapEl.parentNode.insertBefore(out, wrapEl.nextSibling);

      btn.addEventListener("click", () => {
        if (active && active.runBtn === btn) { stopActive(); return; }  // ■ pressed
        runExample(codeEl.textContent, out, btn);
      });
    });
  }

  // ---- Worker lifecycle --------------------------------------------------
  function ensureWorker() {
    if (workerReady) return Promise.resolve();
    if (booting) return booting;
    booting = new Promise((resolve, reject) => {
      worker = new Worker("/static/js/code-worker.js", { type: "module" });
      worker.onmessage = (e) => handleWorker(e.data || {}, resolve, reject);
      worker.onerror = (e) => { booting = null; reject(new Error(e.message || "worker failed to load")); };
      worker.postMessage({
        type: "init",
        interruptSab: interruptView.buffer,
        stdinMetaSab: stdinMeta.buffer,
        stdinDataSab: stdinData.buffer,
      });
    });
    return booting;
  }

  function handleWorker(msg, bootResolve, bootReject) {
    switch (msg.type) {
      case "ready": workerReady = true; bootResolve(); break;
      case "initError": booting = null; bootReject(new Error(msg.error)); break;
      case "stdout": if (active) appendSpan(active.el, msg.text, "code-out"); break;
      case "stderr": if (active) appendSpan(active.el, msg.text, "code-err"); break;
      case "inputRequest": if (active && active.mode === "run") showInput(active.el); break;
      case "runDone": finishRun(); break;
      case "checkDone": finishCheck(msg); break;
    }
  }

  // ---- Run (free) — tasks and examples share this path -------------------
  async function runTask(id) {
    if (active) return;
    active = { el: consoleEl(id), mode: "run", kind: "task", taskId: id };
    setBusy(active);
    clearPre(active.el);
    hideResult(id);
    Atomics.store(interruptView, 0, 0);
    if (!(await bootInto(active.el))) return;
    worker.postMessage({ type: "run", code: editors[id].getValue() });
  }

  async function runExample(codeText, outputEl, runBtn) {
    if (active) return;
    active = { el: outputEl, mode: "run", kind: "example", runBtn };
    setBusy(active);            // disables other run triggers BEFORE the async boot
    clearPre(outputEl);         // box stays hidden until the first output (appendSpan reveals it)
    Atomics.store(interruptView, 0, 0);
    if (!(await bootInto(outputEl))) return;
    worker.postMessage({ type: "run", code: codeText });
  }

  // Lazily boot the worker. Returns false (and finishes) if it fails to load.
  async function bootInto(el) {
    if (workerReady) return true;
    // No "loading" text — the run button's running state is the feedback, and
    // Pyodide is fetched only once (cached after). On failure, surface the error.
    try { await ensureWorker(); }
    catch (err) { appendSpan(el, String(err.message || err) + "\n", "code-err"); finishRun(); return false; }
    return true;
  }

  function finishRun() {
    const a = active;
    active = null;
    setBusy(null);
    if (!a) return;
    removeInlineInput(a.el);
    if (a.kind === "example" && a.el && !a.el.querySelector(".code-out, .code-err, .code-echo")) {
      appendSpan(a.el, t("code.example.no_output"), "code-sys");
    }
  }

  // ---- Check (tasks only) ------------------------------------------------
  async function checkTask(id) {
    if (active) return;
    active = { el: consoleEl(id), mode: "check", kind: "task", taskId: id };
    setBusy(active);
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
        active = null; setBusy(null); return;
      }
    }
    const task = tasks[id] || {};
    worker.postMessage({ type: "check", code: editors[id].getValue(), stdin: task.stdin || "", check: task.check || {} });
  }

  function finishCheck(msg) {
    const a = active;
    active = null;
    setBusy(null);
    if (!a) return;
    const id = a.taskId;
    const box = resultBox(id);
    box.hidden = false;
    if (msg.pass) {
      box.className = "code-result mt-2 code-result-pass";
      box.textContent = t("code.task.pass");
      markDone(id);
    } else if (msg.error) {
      box.className = "code-result mt-2 code-result-fail";
      box.textContent = t("code.task.error");
      clearPre(a.el);
      appendSpan(a.el, msg.error + "\n", "code-err");
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
    if (active) { removeInlineInput(active.el); appendSpan(active.el, t("code.console.stopped") + "\n", "code-sys"); }
  }

  function showInput(el) {
    if (!el) return;
    const empty = el.querySelector(".code-console-empty");
    if (empty) empty.remove();
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "code-console-input";
    inp.setAttribute("aria-label", t("code.input.label"));
    inp.autocomplete = "off";
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submitInput(el, inp); }
    });
    el.appendChild(inp);  // flows right after the last output → same line as a prompt
    inp.focus();
    el.scrollTop = el.scrollHeight;
  }

  function submitInput(el, inp) {
    const value = inp.value;
    const echo = document.createElement("span");
    echo.className = "code-echo";
    echo.textContent = value + "\n";
    inp.replaceWith(echo);
    const bytes = encoder.encode(value + "\n");
    const n = Math.min(bytes.length, stdinData.length);
    stdinData.set(bytes.subarray(0, n));
    Atomics.store(stdinMeta, 1, n);
    Atomics.store(stdinMeta, 0, 1);
    Atomics.notify(stdinMeta, 0);
    el.scrollTop = el.scrollHeight;
  }

  function removeInlineInput(el) {
    if (el) el.querySelectorAll(".code-console-input").forEach((x) => x.remove());
  }

  // ---- Reset (tasks) -----------------------------------------------------
  function resetTask(id) {
    if (active) return;
    const task = tasks[id] || {};
    if (editors[id]) editors[id].setValue(task.starter || "");
    clearPre(consoleEl(id));
    hideResult(id);
  }

  // ---- Output helpers (element-based) -----------------------------------
  function consoleEl(id) { return root.querySelector(`.code-console[data-task-id="${id}"]`); }
  function resultBox(id) { return root.querySelector(`.code-result[data-task-id="${id}"]`); }
  function clearPre(el) { if (el) el.innerHTML = ""; }
  function hideResult(id) { const b = resultBox(id); if (b) { b.hidden = true; b.textContent = ""; } }

  // Append an output chunk (exact text, may contain newlines) as an inline,
  // stream-colored span; the <pre> renders the line breaks. New output goes
  // before any pending input field so order stays correct.
  function appendSpan(el, text, cls) {
    if (!el || !text) return;
    if (el.hidden) el.hidden = false;   // reveal an example output box on first write
    const empty = el.querySelector(".code-console-empty");
    if (empty) empty.remove();
    const span = document.createElement("span");
    span.className = cls;
    span.textContent = text;
    const input = el.querySelector(".code-console-input");
    if (input) el.insertBefore(span, input);
    else el.appendChild(span);
    el.scrollTop = el.scrollHeight;
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
  // `act` is the current active descriptor, or null when idle.
  function setBusy(act) {
    const busy = act != null;
    root.querySelectorAll(".code-run, .code-check, .code-reset").forEach((b) => { b.disabled = busy; });
    root.querySelectorAll(".code-stop").forEach((b) => {
      b.disabled = !(busy && act.kind === "task" && b.dataset.taskId === act.taskId);
    });
    root.querySelectorAll(".code-example-run").forEach((b) => {
      const isActive = busy && act.kind === "example" && b === act.runBtn;
      b.disabled = busy && !isActive;       // the running example's ▶ stays clickable as ■
      b.classList.toggle("is-running", !!isActive);
    });
  }

  // ---- Task completion (localStorage) -----------------------------------
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
