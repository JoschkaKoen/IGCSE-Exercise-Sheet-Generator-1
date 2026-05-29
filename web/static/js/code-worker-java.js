// CheerpJ Java Web Worker for the Code page.
//
// Mirrors code-worker.js's message protocol so code-playground.js can drive it
// unchanged:
//   in:  {init} | {run, code} | {check, code, stdin, files?, check}
//   out: {ready} | {initError, error} | {stdout, text} | {stderr, text}
//        | {runDone, ok} | {checkDone, pass, error, output, expected, kind}
//
// Java runs via CheerpJ — a full OpenJDK JVM compiled to WebAssembly. CheerpJ has
// no headless stdout/stderr API (output normally goes to a #console DOM node, which
// a worker has no access to), so we precompile two tiny helper classes once at init:
//
//   • Compiler — runs javac through com.sun.tools.javac.Main.compile(args, writer),
//     so the compiler's diagnostics (real OpenJDK errors) are captured to a file.
//   • Runner   — redirects System.in/out/err to files, then loads the target class
//     with a FRESH URLClassLoader each run (so a student's edits take effect on
//     re-run despite the long-lived JVM) and invokes its main.
//
// We read the captured files back with cjFileBlob. The javac compiler classes are
// not in CheerpJ's base runtime, so we serve an OpenJDK 8 tools.jar same-origin;
// CheerpJ sees the web root under its /app/ mount.
//
// Loaded as a CLASSIC worker (NOT {type:"module"}) because the CheerpJ loader is a
// classic script pulled in with importScripts().

const CHEERPJ_LOADER = "https://cjrtnc.leaningtech.com/4.3/loader.js";
const TOOLS_JAR = "/app/static/vendor/cheerpj/tools.jar";
const CP = TOOLS_JAR + ":/files/";   // classpath: javac classes + our compiled helpers/students
const RUN_DIR = "/files/";           // .class output (persistent Java-writable mount)

let ready = false;

function post(type, extra) { self.postMessage(Object.assign({ type }, extra || {})); }

// --- helper classes, compiled once at init -------------------------------------

// Runs javac and routes ALL diagnostics to /files/diag.txt (the PrintWriter
// overload), then exits with javac's own return code. Needs tools.jar on the cp.
const COMPILER_SRC = `
import java.io.*;
public class Compiler {
  public static void main(String[] a) throws Exception {
    PrintWriter pw = new PrintWriter(new FileWriter("/files/diag.txt"));
    int rc = com.sun.tools.javac.Main.compile(a, pw);
    pw.flush();
    pw.close();
    System.exit(rc);
  }
}
`;

// args: [mainClass, stdinPath, runDir]. Redirects stdio to files, loads mainClass
// from runDir via a fresh URLClassLoader (parent = platform loader, so student
// classes are reloaded from disk every run), invokes its main(String[]).
const RUNNER_SRC = `
import java.io.*;
import java.lang.reflect.*;
import java.net.*;
public class Runner {
  public static void main(String[] a) throws Exception {
    String mainClass = a[0];
    String stdinPath = a.length > 1 ? a[1] : "";
    String runDir    = a.length > 2 ? a[2] : "/files/";
    if (!runDir.endsWith("/")) runDir = runDir + "/";

    PrintStream out = new PrintStream(new FileOutputStream("/files/out.txt"), true);
    PrintStream err = new PrintStream(new FileOutputStream("/files/err.txt"), true);
    System.setOut(out);
    System.setErr(err);
    if (stdinPath.length() > 0) {
      try { System.setIn(new FileInputStream(stdinPath)); } catch (FileNotFoundException e) {}
    }

    int exit = 0;
    try {
      URLClassLoader cl = new URLClassLoader(
        new URL[]{ new URL("file:" + runDir) },
        ClassLoader.getSystemClassLoader().getParent());
      Class<?> c = Class.forName(mainClass, true, cl);
      Method m = c.getMethod("main", String[].class);
      m.invoke(null, (Object) new String[0]);
    } catch (InvocationTargetException e) {
      Throwable cause = (e.getCause() != null) ? e.getCause() : e;
      cause.printStackTrace();
      exit = 1;
    } catch (Throwable t) {
      t.printStackTrace();
      exit = 1;
    } finally {
      System.out.flush();
      System.err.flush();
      out.close();
      err.close();
    }
    System.exit(exit);
  }
}
`;

importScripts(CHEERPJ_LOADER);

// --- VFS helpers ---------------------------------------------------------------

function writeStr(path, text) {
  // /str/ is the JS→Java transient mount; cheerpOSAddStringFile is the 4.x API.
  cheerpOSAddStringFile(path, text != null ? String(text) : "");
}

async function readFile(path) {
  try { const b = await cjFileBlob(path); return await b.text(); }
  catch (e) { return ""; }
}

// Public/top-level class name → the file it must live in (Java requires the match).
function deriveClassName(src) {
  const pub = /\bpublic\s+(?:final\s+|abstract\s+)?(?:class|interface|enum)\s+([A-Za-z_]\w*)/.exec(src || "");
  if (pub) return pub[1];
  const any = /\b(?:class|interface|enum)\s+([A-Za-z_]\w*)/.exec(src || "");
  return (any && any[1]) || "Main";
}

// Compile {name -> source} into RUN_DIR. Returns {ok, diag}.
async function compile(files) {
  const srcPaths = [];
  for (const name in files) {
    const p = "/str/" + name;
    writeStr(p, files[name]);
    srcPaths.push(p);
  }
  writeStr("/str/_clear.txt", "");          // touch so /str/ exists
  const rc = await cheerpjRunMain("Compiler", CP, ...srcPaths, "-d", RUN_DIR);
  // Strip the /str/ VFS prefix so diagnostics read "Main.java:3:" not "/str/Main.java:3:".
  const diag = (await readFile("/files/diag.txt")).replace(/\/str\//g, "");
  return { ok: rc === 0, diag: diag };
}

// Run mainClass (fresh classloader) with scripted stdin. Returns {exit, out, err}.
async function runProgram(mainClass, stdin) {
  writeStr("/str/stdin.txt", stdin || "");
  const exit = await cheerpjRunMain("Runner", CP, mainClass, "/str/stdin.txt", RUN_DIR);
  const out = await readFile("/files/out.txt");
  const err = await readFile("/files/err.txt");
  return { exit: exit, out: out, err: err };
}

// --- init ----------------------------------------------------------------------

async function init() {
  try {
    await cheerpjInit({ status: "none", version: 8 });
    writeStr("/str/Runner.java", RUNNER_SRC);
    writeStr("/str/Compiler.java", COMPILER_SRC);
    // First compile uses raw javac (Compiler doesn't exist yet); our helpers are
    // trusted and compile cleanly, so we don't need to capture this one's output.
    const rc = await cheerpjRunMain(
      "com.sun.tools.javac.Main", CP,
      "/str/Runner.java", "/str/Compiler.java", "-d", RUN_DIR);
    if (rc !== 0) {
      post("initError", { error: "Java runtime failed to build its helper classes (javac rc=" + rc + ")." });
      return;
    }
    ready = true;
    post("ready");
  } catch (err) {
    post("initError", { error: String((err && err.message) || err) });
  }
}

// --- run / check ---------------------------------------------------------------

function compareStdout(got, check) {
  let a = got;
  let b = String(check.expected != null ? check.expected : "");
  if (check.normalize == null || check.normalize === "strip") { a = a.trim(); b = b.trim(); }
  return a === b;
}

async function runFree(code) {
  if (!ready) { post("stderr", { text: "Java runtime is not ready.\n" }); post("runDone", { ok: false }); return; }
  const cls = deriveClassName(code);
  const files = {}; files[cls + ".java"] = code;
  const c = await compile(files);
  if (!c.ok) { post("stderr", { text: c.diag || "Compilation failed.\n" }); post("runDone", { ok: false }); return; }
  const r = await runProgram(cls, "");
  if (r.out) post("stdout", { text: r.out });
  if (r.err) post("stderr", { text: r.err });
  post("runDone", { ok: r.exit === 0 });
}

async function runCheck(payload) {
  const check = payload.check || {};
  const expected = check.expected != null ? String(check.expected) : null;
  const kind = check.kind || null;
  if (!ready) { post("checkDone", { pass: false, error: "Java runtime is not ready.", output: "", expected: expected, kind: kind }); return; }

  const studentCode = payload.code || "";
  const cls = deriveClassName(studentCode);
  const files = {}; files[cls + ".java"] = studentCode;
  if (payload.files && typeof payload.files === "object") {
    for (const n in payload.files) files[n] = payload.files[n];
  }

  let mainClass = check.main_class || cls;
  if (kind === "harness" && check.code) {
    const hc = check.main_class || "Harness";
    files[hc + ".java"] = check.code;
    mainClass = hc;
  }

  const c = await compile(files);
  if (!c.ok) {
    post("checkDone", { pass: false, error: c.diag || "Compilation failed.", output: "", expected: expected, kind: kind });
    return;
  }

  const r = await runProgram(mainClass, payload.stdin || "");
  let pass = false, error = null;
  if (kind === "harness") {
    pass = (r.exit === 0);
    if (!pass) error = r.err || ("Program exited with code " + r.exit);
  } else { // stdout
    if (r.exit !== 0) error = r.err || ("Program exited with code " + r.exit);
    else pass = compareStdout(r.out, check);
  }
  post("checkDone", { pass: pass, error: error, output: r.out, expected: expected, kind: kind });
}

self.onmessage = (e) => {
  const msg = e.data || {};
  if (msg.type === "init") init();
  else if (msg.type === "run") runFree(msg.code);
  else if (msg.type === "check") runCheck(msg);
};
