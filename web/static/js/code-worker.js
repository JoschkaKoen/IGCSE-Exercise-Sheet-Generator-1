// Pyodide Web Worker for the Code page.
//
// Student Python runs HERE, off the main thread, so two things work:
//   • Stop — setInterruptBuffer() lets the main thread raise KeyboardInterrupt
//     (killing infinite loops) by writing 2 into a shared interrupt buffer.
//   • live input() — Python's input() is synchronous; the stdin callback blocks
//     this worker with Atomics.wait() until the main thread posts the typed line.
//
// Loaded as a module worker ({type:"module"}) so it can import pyodide.mjs.
// Everything is same-origin, so it works under the page's COEP: require-corp.

import { loadPyodide } from "/static/vendor/pyodide/pyodide.mjs";

const PYODIDE_INDEX_URL = "/static/vendor/pyodide/";
const decoder = new TextDecoder();

let pyodide = null;
let interruptBuf = null;   // Int32Array(1): main writes 2 to interrupt
let stdinMeta = null;      // Int32Array(2): [0]=state (0 wait,1 data,2 EOF), [1]=byte length
let stdinData = null;      // Uint8Array: one typed input line

// When non-null, input() is fed from this scripted queue (deterministic checks)
// instead of blocking for live input.
let scriptedStdin = null;
// When true, stdout/stderr are captured (checks) instead of streamed (free run).
let capturing = false;
let captured = "";

function post(type, extra) {
  self.postMessage(Object.assign({ type }, extra || {}));
}

// Free-run stdin: ask the main thread for a line, block until it answers.
function readLiveLine() {
  post("inputRequest");
  Atomics.store(stdinMeta, 0, 0);
  Atomics.wait(stdinMeta, 0, 0);
  if (Atomics.load(stdinMeta, 0) === 2) return undefined; // EOF (e.g. Stop)
  const len = Atomics.load(stdinMeta, 1);
  return decoder.decode(stdinData.slice(0, len));
}

function stdin() {
  if (scriptedStdin !== null) {
    return scriptedStdin.length ? scriptedStdin.shift() : undefined; // EOF when drained
  }
  return readLiveLine();
}

function onText(stream, text) {
  if (capturing) captured += text + "\n";
  else post(stream, { text });
}

function errText(err) {
  // Pyodide raises PythonError; .message carries the traceback.
  return String((err && err.message) || err);
}

function freshNamespace() {
  return pyodide.toPy({});
}

function destroy(ns) {
  try { ns.destroy(); } catch (e) { /* ignore */ }
}

async function init(msg) {
  interruptBuf = new Int32Array(msg.interruptSab);
  stdinMeta = new Int32Array(msg.stdinMetaSab);
  stdinData = new Uint8Array(msg.stdinDataSab);
  try {
    pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX_URL });
    pyodide.setInterruptBuffer(interruptBuf);
    pyodide.setStdout({ batched: (s) => onText("stdout", s) });
    pyodide.setStderr({ batched: (s) => onText("stderr", s) });
    pyodide.setStdin({ stdin, isatty: false, autoEOF: false });
    post("ready");
  } catch (err) {
    post("initError", { error: errText(err) });
  }
}

async function runFree(code) {
  Atomics.store(interruptBuf, 0, 0);
  scriptedStdin = null;   // live input
  capturing = false;
  const ns = freshNamespace();
  try {
    await pyodide.runPythonAsync(code, { globals: ns });
    post("runDone", { ok: true });
  } catch (err) {
    post("stderr", { text: errText(err) });
    post("runDone", { ok: false });
  } finally {
    destroy(ns);
  }
}

async function runCheck(payload) {
  Atomics.store(interruptBuf, 0, 0);
  // Deterministic: scripted stdin then EOF, captured output, never live input.
  scriptedStdin = payload.stdin ? [payload.stdin] : [];
  capturing = true;
  captured = "";
  const check = payload.check || {};
  const ns = freshNamespace();
  let error = null;
  try {
    await pyodide.runPythonAsync(payload.code, { globals: ns });
    if (check.kind === "asserts" && check.code) {
      await pyodide.runPythonAsync(check.code, { globals: ns });
    }
  } catch (err) {
    error = errText(err);
  } finally {
    capturing = false;
    scriptedStdin = null;
    destroy(ns);
  }

  let pass = false;
  if (!error) {
    if (check.kind === "asserts") pass = true;
    else if (check.kind === "stdout") pass = compareStdout(captured, check);
  }
  post("checkDone", {
    pass,
    error,
    output: captured,
    expected: check.expected != null ? String(check.expected) : null,
    kind: check.kind || null,
  });
}

function compareStdout(got, check) {
  let a = got;
  let b = String(check.expected != null ? check.expected : "");
  if (check.normalize == null || check.normalize === "strip") {
    a = a.trim();
    b = b.trim();
  }
  return a === b;
}

self.onmessage = (e) => {
  const msg = e.data || {};
  if (msg.type === "init") init(msg);
  else if (msg.type === "run") runFree(msg.code);
  else if (msg.type === "check") runCheck(msg);
};
