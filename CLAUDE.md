Codex will review your output once you are done.

# Project guide

This repo holds three Python pipelines plus a small FastAPI web UI that consumes all of them.

## The three pipelines

- `eXercise/` — exercise sheet **generation**. Flat package, ~28 modules. Entry point: `python eXercise.py "<natural-language prompt>"`.
- `xscore/` — exam scan **marking**. Structured package, 8 subpackages (`pipeline/`, `steps/`, `shared/`, `marking/`, `scaffold/`, `preprocessing/`, `extraction/`, `prompts/`). Entry point: `python XScore.py "grade <exam name>"`.
- `eXam/` — on-screen exam **practice with AI marking**. Student/teacher runtime backed by SQLite, served via the web UI's `eXam_*` routes. Pre-indexes papers via `eXam.bank` (CLI: `python -m eXam.bank --paper … --ms … --subject …`).

`eXercise/` *also* hosts shared infrastructure that `xscore/` and `eXam/` depend on: `eXercise.ai_client`, `eXercise.prompt_logger`, `eXercise.env_load`, `eXercise.config`, `eXercise.fonts`, `eXercise.latex_utils`. Treat `eXercise/` as both a peer pipeline **and** a foundation library — don't move it.

## Web UI

`web/app.py` is a FastAPI app that wraps all three pipelines. Run with:

```
uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
```

Then open [http://127.0.0.1:8001](http://127.0.0.1:8001) (port 8000 often clashes with Docker on macOS).

### Tailwind CSS is pre-compiled — recompile after changing classes

The UI's Tailwind utilities are **pre-built into `web/static/css/00-tailwind.css`** (committed). The Tailwind Play CDN (which compiled in-browser on every page load — slow on large DOMs like the practice landing, ~400 KB render-blocking) has been **removed**. Theme (Sora/Outfit fonts + custom `space`/`commerce` colors) plus a safelist for interpolated subject-card colors live in `web/tailwind.config.js`.

**If you add or remove a Tailwind utility class in any template or JS file, you MUST rebuild and commit the CSS** — unlike the old browser JIT, a class absent at build time simply has no styles:

```
python scripts/build_tailwind.py         # downloads the pinned standalone CLI into a gitignored .cache/, rebuilds 00-tailwind.css
git add web/static/css/00-tailwind.css   # commit it — the committed file is what ships (the Docker image does NOT rebuild it)
```

Dynamically-interpolated classes (e.g. `text-{{ theme }}-400` built in Jinja) can't be seen by the scanner — add their shape to the `safelist` in `web/tailwind.config.js`. The Tailwind version is pinned in `scripts/build_tailwind.py`; bump it deliberately and re-verify rendering.

Web grade jobs upload to `output/xscore/grade_uploads/<job_id>/` (segregated from CLI runs, which use `output/xscore/<exam>/<timestamp>/`). See `web/routes/grade_jobs.py` and `web/grade_service.py`.

The **Learn page** (`web/routes/learn.py`) surfaces syllabus-content lookups powered by `web/syllabus_content.py`, `web/syllabus_topics.py`, and `web/exam_questions.py`. The last reuses the scaffold chain through `eXam.xscore_adapter.load_scaffold_api()` to extract structured question YAML from empty exam papers (same flow as `eXam/bank.py`, minus the mark-scheme branch and the per-question snippet renderer).

### Handouts

Per-syllabus-topic markdown handouts live under `output/eXam/handouts/<subject>/<NN>.md`, with a sidecar `<NN>.meta.yaml` (covered question IDs, `language`, `simplified_at`/`glossed_at` stamps), a per-handout `<NN>.glossary.tsv` (terms glossed in that file, first-appearance order, `english / 简体中文 / pinyin`), and a per-subject master `_glossary.tsv` (consistency backbone). **Handouts are human/Claude-authored, not API-generated** — the cost and drift of running 100+ Qwen calls per topic outweighed the benefit when one careful writing pass produces a better handout. Currently complete: **A-Level Physics (25 topics) and A-Level CS (20 topics)**.

**Audience + convention.** Written for **Grade 11 Chinese ESL students**: strong English simplification (short sentences, plain words, ≈ CEFR B1) plus **inline Simplified-Chinese glosses** after difficult words — `term 中文` (one space, no brackets; bold terms → `**term** 中文`), first occurrence per file only, never inside `$…$` math or code, multi-word technical terms glossed as a unit. `eXam/prompts/handout_topic.md` is the authoritative style guide.

**Tooling.** `web/handouts_collect.py` gathers context: `collect_questions_for_topic(subject, topic_num)` walks the bank's `subtopic_matches.yaml` + `exam_questions.yaml`; `load_syllabus_content_for_topic(subject, topic)` concatenates the `syllabi/content/<subject>/<N.M>.md` files. `scripts/dump_handout_context.py` prints both. After authoring `<NN>.md`, run `python -m scripts.build_handout_glossary <subject> <topic> <pairs.tsv>` (auto-pinyin via pypinyin, writes the glossaries, stamps meta, warns on inconsistent Chinese) and `python -m scripts.check_handout_glosses <subject> [topic]` (no CJK in math/code; each gloss once per file; glossary↔inline agreement). For incremental updates use the Edit tool for line-level changes.

**Backup + review.** Frozen English originals are at `output/eXam/handouts_en_backup/<subject>/<NN>.md` (gitignored). The Learn page renders the live `<NN>.md` directly (`web/routes/learn.py` → `load_handout_md`), so editing a file swaps it in place. A dev/review route `GET /learn/handout-review[/<subject>/<topic>]` shows original vs current side-by-side (`?left=`/`?right=` pick versions). Markdown → LaTeX/PDF rendering arrives in a follow-up PR (will need a CJK font: xeCJK/ctex + Noto Sans CJK SC).

### Code page

`web/routes/code.py` serves an in-browser coding playground at `/code` — a course/lesson index plus per-lesson pages (prose + editor + console + auto-checked tasks). Student **Python runs entirely client-side** via Pyodide in a Web Worker (nothing executes on the server); the `/code` HTML responses carry cross-origin-isolation headers (COOP + COEP) so the worker can use `SharedArrayBuffer` for the Stop-button interrupt and blocking `input()`. **These headers are scoped to `/code` only** — the rest of the site is unaffected.

**Lesson content** is committed authored source under `content/code/<course>/` (NOT `output/`): a `course.yaml` manifest plus per-lesson `NN.en.md` / `NN.zh.md` (bilingual prose) and `NN.meta.yaml` (title + task list — each task has `starter` code, an optional scripted `stdin`, and a client-side `check` spec — `{kind: stdout, …}`, `{kind: asserts, …}` (Python), or `{kind: harness, …}` (Java/C)). Loaded by `web/code_content.py` at request time (no in-process cache, like the Learn page); this mirrors the handouts authored-markdown-plus-YAML-sidecar pattern. Five courses today: `python-basics` (15 lessons), `a-level-cs` (15, Python), `ap-csp` (15, Python), `ap-csa` (19, Java), and `c-basics` (23, C, server-side). Validate authored lessons with `python -m scripts.check_code_lessons`.

**Progress** is server-backed in the `code_progress` table (`eXam/db.py` migration 2→3, keyed by the anonymous open-mode `session_id` with a nullable `user_id` that `web.routes.account.link_open_session` fills on login, so progress follows a learner across devices). `web/code_progress.py` merges **monotonically** — `revealed` only rises, a task `done` flag only sets — so a stale client can never lower what the server knows.

**Java + C runners (server-side, the default for `language: java` and `language: c`).** They execute on the server, not in the browser — for Java, benchmarked ~7× faster than CheerpJ warm and far better cold (CheerpJ's runtime CDN is ~90 s from China); C has no in-browser runtime at all. The client-side CheerpJ path (`code-worker-java.js` + the 18 MB `tools.jar`) is **kept as dormant legacy**, reachable only via `?runtime=cheerpj`; `code-playground.js` defaults Java to `server` and C is always `server`.

`web/sandbox_exec.py` is the **language-agnostic core** (process spawn, rlimits, scrubbed env, output caps, wall-clock timeout, `compare_stdout`, `_finalize`) — deliberately stdlib-only and FastAPI-free. `web/java_runner.py` (`javac`/`java`, `--release 8`) and `web/c_runner.py` (`gcc`, `-std=c11`, `-lm` last) build on it; `scripts/check_code_lessons.py` imports their pure helpers so **validator-pass ⇒ server-pass**. **C harness pattern:** for `kind: harness` the student writes functions only (saved as `student.c`), the harness `#include`s it and provides `main`, compiled as one translation unit (`run_c` syntax-checks `student.c` alone first for clean diagnostics and scrubs the hidden `harness.c` path from errors).

Execution is delegated to the **isolated `java-sandbox` sidecar** (`web/java_sandbox_server.py`, `docker-compose.yml`; serves both `/run` and `/run-c`): a container with **no internet** (an `internal` network), **no secrets** (no `env_file`), **no bind-mounts**, a **read-only FS** + tmpfs workdir, all caps dropped, `no-new-privileges`, and pid/mem limits. The public endpoints `web/routes/code_run.py` (`POST /api/code/run-java` and `/api/code/run-c`, under `/api/` so `site_access_gate` requires the cookie) forward via `JAVA_SANDBOX_URL` / `C_SANDBOX_URL` (same container, paths `/run` and `/run-c`); with no sandbox URL (local dev) they run in-process (NOT sandboxed). Per-language access on top: `JAVA_RUNNER_OPEN=1` / `C_RUNNER_OPEN=1` opens to any logged-in user (safe once sandboxed); otherwise a secret `X-Java-Runner-Token` / `X-C-Runner-Token` is required (staging), **fail-closed 404** if neither is set. Concurrency/timeout are env-tunable (`CODE_SANDBOX_CONCURRENCY`, `CODE_SANDBOX_TIMEOUT`; defaults 2 / 10 s).

## xscore pipeline structure

Steps are numbered 1–34 (contiguous). Each step writes its artifacts under `output/xscore/<exam_stem>/<timestamp>/<NN_step_name>/`. Folder names are mechanically `<NN>_<step.name>` — auto-derived by the `Step.writes` property from `step.number` + `step.name`. Set `_explicit_writes=()` on a `Step` for the rare case of a step that writes nothing (today only `locate_exam_folder`). The named constants in `xscore/shared/step_folders.py` mirror the same pattern and are imported by path-builder helpers.

Step registry: `xscore/shared/pipeline_steps.py` holds a `Step` dataclass list — canonical ordering and naming. Step bodies live in one module per phase under `xscore/steps/`: `prelude`, `scan`, `geometry`, `scaffold`, `marking`, `reports`, `summary`. `wire_step_fns()` looks each one up by name at startup; a missing function fails loud (no silent-skip).

Resume mid-pipeline: `python XScore.py "grade <exam>" --resume-dir output/xscore/<exam>/<timestamp>` — re-uses already-completed step artifacts.

Stop early / start late: `--stop-after <N>` and `--from-step <N>`.

## Shared utilities (xscore.shared)

Cross-subsystem helpers live in `xscore/shared/` so the marking pipeline doesn't reach into scaffold internals:

- `xscore.shared.qnum_utils.norm_qnum` — strips `()` from question numbers (`"7(a)"` → `"7a"`); the canonical hashable key for question lookups, used by both scaffold and marking.
- `xscore.shared.exam_questions_io.load_exam_questions_artifact` — loads `exam_questions.yaml` written by step 18; consumed by `xscore.steps.scaffold.detect_cross_page_context` (step 19) and `xscore.marking.merge_reports`.

Each subsystem package (`xscore/scaffold/`, `xscore/marking/`, `xscore/preprocessing/`, `xscore/shared/`) declares its public API via `__all__` in `__init__.py` — leaks become reviewable.

## Marking & scaffold output formats

AI structured-output steps (scaffold 17/18, scheme parsing 21/22, marking 25/26/27) emit YAML — block scalars preserve LaTeX without escaping and the diffs are reviewable. Format classes live in parallel-structured subpackages: `xscore/marking/formats/` (`MarkingFormat`) and `xscore/scaffold/formats/` (`ScaffoldFormat`). Each is split into `_yaml_io.py` (custom YAML dumper), `_parsers.py` (per-field parsers + error classes), `_prompt_builders.py` (blueprint builders), and `<name>_format.py` (the class itself). `base.py` is a re-export shim for historic call sites. Get an instance via `get_marking_format()` / `get_scaffold_format()`.

## Scaffold subsystem

`xscore/scaffold/` is one of the largest subsystems (~24 modules) and covers steps 17–23: detecting exam structure, filling it from the empty paper, splitting the mark-scheme PDF per-question, transcribing scheme graphics, generating LaTeX templates, and caching the whole thing for resume. Resume hook: `scaffold_cache.py` (the cache key includes the empty-exam hash so re-running with the same paper short-circuits expensive AI calls).

Transient state shared across the scaffold-building steps lives on `ctx.scaffold_state: ScaffoldPhaseState | None` (typed dataclass in `xscore/scaffold/scaffold_phase_state.py`). Set by `scaffold_setup`, cleared (set to `None`) by `scaffold_cleanup`. Attribute typos fail loud (`AttributeError`) instead of silently passing init and dying inside a step body.

## eXam subsystem

`eXam/` is the on-screen practice/marking runtime. All `xscore.*` imports are colocated in `eXam/xscore_adapter.py` (lazy: defers xscore's heavy deps until the pre-indexer runs). Consumers in `eXam/bank.py` call `load_scaffold_api()` once to get a `SimpleNamespace` of ten scaffold/format symbols: `detect_layout_phase`, `cut_exam_pdf_phase`, `assign_scheme_questions_phase`, `detect_scheme_graphics_phase`, `parse_mark_scheme_phase`, `get_scaffold_format`, `extract_exam_question_numbers`, `extract_exam_questions`, `extract_question_numbers_model_config`, `extract_questions_model_config`. Other eXam modules (`db.py`, `marker.py`, `runtime.py`, `auth.py`, `users.py`, `roster.py`, `test_builder.py`, `cost_tracker.py`, `pregenerate.py`, `render_helper.py`, `results_export.py`, `open_mode.py`, `flush_cache.py`, `warm_bank.py`) have no xscore dependency.

**Open-mode + warmed bank.** `eXam/open_mode.py` serves anonymous practice (random question per subject, no login) via the FastAPI `eXam_open` routes. The landing page also offers a per-syllabus-topic accordion: `?topic=<N>` restricts the random pick to that topic's question IDs (`subtopic_matches.yaml` → `topic_qids`), while no topic and `?topic=all` both draw from every topic. It depends on a pre-indexed bank: `python -m eXam.warm_bank --year <YYYY> --subject <slug>` indexes every QP up front — each paper costs several xscore-scaffold AI calls but is then cached forever under `output/eXam/bank/` (tracked in git as the deploy-time bank). Helper drawers (hint / solution / example / kb) are generated lazily by `eXam/pregenerate.py` and cached per-question; `eXam/flush_cache.py` clears them when the snippet format changes. `eXam/render_helper.py` is the markdown→HTML pipeline that preserves `$…$` so in-browser KaTeX still renders.

## Prompts

Live in `xscore/prompts/<name>.md`, loaded via `xscore/prompts/loader.py`. Files have optional YAML frontmatter (`version`, `model_hint`, `output_format`, `description`). Use `load_prompt(name, /, *, section=None, **substitutions)` from the loader; templates use `$placeholder` substitution (`string.Template.safe_substitute`). Files with multiple roles use `## SECTION_NAME` H2 headers (uppercase) — pass `section=` to extract one. Shared fragments are inlined via `$include_<stem>` (resolved recursively before substitution).

## Model configuration

`default.env` declares model choices in the form `MODEL_NAME=model-id, thinking_tokens, max_tokens`. Both budgets are optional; the legacy `off | low | high` strings are still accepted (parse to `0 | 1024 | 8192` thinking tokens) for back-compat with the old two-position syntax. Example: `MARKING_MODEL=qwen3.6-plus, off`.

**Qwen gotcha**: with no thinking budget specified, Qwen defaults to `enable_thinking=True`, which forces streaming and breaks the non-streaming JSON output path. Always pass an explicit `0` (or `off`) for Qwen unless you need thinking.

Determinism is injected automatically: `ALL_AI_TEMPERATURE` and `ALL_AI_SEED` env vars are applied to every call through `make_ai_client` (default temperature 0). Pass `deterministic=False` only when you actively want sampling.

API keys go in `.env` (gitignored); `default.env` has only model selections and is safe to commit.

## AI client

`make_ai_client(*, model_env, legacy_model_env, default_model, deterministic=True, should_cache=False)` (keyword-only) in `eXercise/ai_client.py` returns `(client, model, provider, thinking_tokens, max_tokens) | None`. `None` means the required API key is missing. Both budgets are `None` when the env string didn't specify them — callers should fall back to their own defaults. `should_cache=True` routes every `chat.completions.create` through the response cache (and also gates `kimi_pdf_text`'s file-extract caching); resolve via `reuse_cache_enabled(ctx)` at the call site rather than hard-coding.

`parse_model_spec("qwen3.6-plus, 0, 4096")` → `("qwen3.6-plus", 0, 4096)`. Provider is auto-detected from the model name (see Native PDF section).

## Multimodal pattern (Qwen / OpenAI-compat)

Image content is passed as `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}` inside the message content array. JPEG rendering: open the fitz doc once outside the loop, then `doc[idx].get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72), colorspace=fitz.csRGB)` → PIL → `to_jpeg_bytes()` from `xscore/extraction/images.py`.

## Native PDF input (Gemini / Kimi)

Steps 17 and 18 (scaffold detect / fill) dispatch on the configured model and send the empty exam paper / mark scheme as a native PDF when possible:

- **Gemini** — `gemini_pdf_part(client, path)` in `eXercise/ai_client.py` returns a `Part.from_bytes(application/pdf)` for ≤18 MB or falls back to the Files API. Used via the native `google.genai` client (`make_gemini_native_client()`).
- **Kimi / Moonshot** — `kimi_pdf_text(client, path)` uploads via `client.files.create(purpose="file-extract")`, retrieves the server-extracted text via `client.files.content(id).text`, deletes the upload, and returns the text. Caller injects the result as a system message in addition to the existing system+user prompt. Same OpenAI-compatible client as Qwen — no separate native SDK.
- **Qwen / Grok / others** — fall back to rasterizing pages to PNG and sending base64 `image_url` parts.

Provider is auto-detected from the model name: `gemini*` → Gemini, `kimi*`/`moonshot*` → Kimi, `qwen*` → Qwen, `grok*` → xAI. Set `KIMI_API_KEY` to use Kimi. The default Kimi endpoint is `https://api.moonshot.cn/v1` (China); override via `KIMI_BASE_URL=https://api.moonshot.ai/v1` for the international endpoint — keys are region-specific and not interchangeable. Step 22 (parse_mark_scheme) and step 3 (read_student_list) intentionally do **not** dispatch to Kimi — they keep their existing provider choices.

## Response cache

`xscore/shared/response_cache.py` — opt-in cache for the AI marking step (`ai_marking`) only. Activated when the user includes "reuse cache" or "use cache" in the natural-language prompt; default is OFF. Cache lives at `~/.cache/xscore/responses/<key[:2]>/<key>.json` (override with `XSCORE_CACHE_DIR`). Scope is deliberately narrow: only the OpenAI-compatible marking call (`xscore.marking.mark_page._mark_page`) is cached. The Gemini-native PDF upload path is intentionally **not** cached yet. Misses, read errors, and write errors are all silent — caching never breaks the pipeline.

## Prompt logging

Every AI call auto-saves its prompt and response to the step's artifact dir as `<task>_prompt.md` (with binary attachments as sidecar files) and `<task>_response.txt`. Two parallel implementations kept in sync: `eXercise/prompt_logger.py` (generation pipeline) and `xscore/shared/prompt_logger.py` (marking pipeline). Logging silently no-ops on I/O error so a logging fault never breaks the pipeline.

Image sidecars are gated by the `SAVE_AI_IMAGES` env var (default off; set `SAVE_AI_IMAGES=true` to keep them). When off, `<task>_prompt.md` still records mime, byte size, and sha256 of each image part, but no `<task>_attachment_*.{jpg,png,pdf,…}` file is written. `detect_subject`'s `preview_first_pages.pdf` is also routed through a tempfile when the flag is off (it's the only AI-input image written outside `save_prompt`). `detect_mark_scheme_graphics`' cropped mark-scheme graphic PNGs in `mark_scheme_graphics/` are NOT controlled by this flag — `transcribe_scheme_graphics` reads them, so they always go to disk.

Each `<task>_response.txt` is the concatenation of the model's thinking trace (as a leading `[thinking]…[/thinking]` block) and the structured response that follows it. Both are saved together for review convenience; downstream parsers operate on the post-`[/thinking]` body only. Don't conflate verbose thinking-trace prose with content actually emitted into structured fields when auditing output.

## Regression strategy

After any structural change to `xscore/`, run the marking pipeline end-to-end on a current exam (pick one from `exams/<level>/<subject>_<syllabus_code>/`) and diff outputs against a baseline run:

```
python XScore.py "grade <current exam name>"
```

For low-risk changes that can't shift output schema (file splits, import rewrites, internal reorganisation), static checks + a web smoke test are sufficient.

## Exam directories

Convention is `exams/<level>/<subject>_<syllabus_code>/`, with `<level>` ∈ {`igcse`, `a_level`}. Code slugs (in `EXAM_ROOT_BY_KEY`) are `<level>_<subject>` — fifteen today: `igcse_physics`, `igcse_chemistry`, `igcse_biology`, `igcse_mathematics`, `igcse_computer_science`, `igcse_business_studies`, `igcse_economics`, `a_level_physics`, `a_level_biology`, `a_level_chemistry`, `a_level_computer_science`, `a_level_business`, `a_level_economics`, `a_level_mathematics` (9709), `a_level_further_mathematics` (9231). The natural-language exam resolver looks here when the user names an exam in the prompt. Adding a subject means touching the same set in several places: `eXercise/config.py` (three dicts — `EXAM_ROOT_BY_KEY`, `SYLLABUS_CODE_BY_KEY`, `PAGE_HEADER_BY_EXAM`), `eXercise/labels.py` (`_SUBJECT_PREFIXES`), `eXercise/natural_language.py` (the resolver prompt's subject list), `xscore/shared/subjects.py` (`KNOWN_SUBJECTS`, with `filename_patterns` for detection), and `scripts/subtopic_match_tool.py` (`ALL_SUBJECTS`).

## Python environment

The project uses a venv at `.venv/` in the repo root, which is a symlink to a shared venv at `/Users/joschka/Desktop/Programming/Exercise Sheet Generator/.venv` (an adjacent project). Always invoke Python via `.venv/bin/python` — `python3` from the system path will not have the project's dependencies (`fitz` / `pymupdf`, `yaml`, `openai`, `google-genai`, etc.).

Examples:

```
.venv/bin/python XScore.py "grade <current exam name>"
.venv/bin/python -c "from xscore.prompts.loader import load_prompt; ..."
```

Pip-installable dependencies are pinned in `requirements.txt`; the canonical install list also lives in `Dockerfile`.

## System dependencies

Beyond pip, the pipelines need:

- `pdflatex` + texlive packages (`texlive-latex-extra`, `texlive-fonts-extra`) — exam paper rendering.
- `pdfjam` (`texlive-extra-utils`) — PDF post-processing.
- `poppler` (`pdftoppm`) — PDF rasterization fallback.
- `tesseract` — OCR fallback when vision-model OCR fails.
- `openjdk-21-jdk-headless` (`javac`/`java`) — server-side Java runner for the AP CS code page (`web/java_runner.py`); compiles at `--release 8` (Debian trixie has no openjdk-17).

`Dockerfile` is the canonical install list; mirror it on a new dev machine.

## Diagnostic scripts

Provider debugging:
- `scripts/diagnose_qwen_json_schema.py` — probe Qwen's JSON schema acceptance.
- `scripts/diagnose_qwen_pdf_upload.py`, `scripts/diagnose_kimi_pdf_upload.py` — debug provider PDF-upload paths in isolation.
- `scripts/diagnose_gemini_handwriting.py`, `scripts/diagnose_handwriting_models.py` — compare vision models on handwritten student answers.

Writing-area detector:
- `scripts/calibrate_writing_areas.py`, `scripts/diag_writing_areas.py` — calibrate and inspect the WA detector against reference papers.
- `scripts/verify_writing_areas_snapshot.py` — regression net for the WA detector (see commits `8e72f5b`, `6bf77b1`).

Other:
- `scripts/count_bare_keywords.py` — prompt audit helper.
- `scripts/vendor_fetch.py` — (re)download Tailwind, Google Fonts, and Twemoji CSS+fonts into `web/static/vendor/` so the web app loads them locally rather than via CDN. Run once, commit the result.

## File size

Keep source files between 150–500 lines. If a file exceeds 500 lines, refactor it into smaller focused modules.

## Don't

- Don't move `eXercise/` into a `legacy/` folder — `xscore/` and `eXam/` import `ai_client`, `prompt_logger`, `env_load`, `config`, `fonts`, and `latex_utils` from it; the package is foundation infrastructure, not deprecated code.
- Don't add `tests/` infrastructure unless asked — the end-to-end run on a chosen exam is the current regression strategy and a parallel test suite would create two sources of truth.
- Don't commit anything in `output/` or `logs/` — both gitignored, both contain student data or large artifacts.
- Don't reach into `xscore.scaffold.<submodule>` from `xscore/marking/`, `xscore/preprocessing/`, the non-scaffold step modules, or `eXam/`. Use the public surface (or `eXam/xscore_adapter.py` for eXam). Cross-subsystem helpers live in `xscore.shared.*`.
