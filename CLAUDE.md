Codex will review your output once you are done.

# Project guide

This repo holds two Python pipelines plus a small FastAPI web UI that consumes both.

## The two pipelines

- `eXercise/` — exercise sheet **generation**. Flat package, ~28 modules. Entry point: `python eXercise.py "<natural-language prompt>"`.
- `xscore/` — exam scan **marking**. Structured package, 8 subpackages (`pipeline/`, `steps/`, `shared/`, `marking/`, `scaffold/`, `preprocessing/`, `extraction/`, `prompts/`). Entry point: `python xScore.py "grade <exam name>"`.

`eXercise/` *also* hosts shared infrastructure that `xscore/` depends on: `eXercise.ai_client`, `eXercise.prompt_logger`, `eXercise.env_load`, `eXercise.config`, `eXercise.fonts`, `eXercise.latex_utils`. Treat `eXercise/` as both a peer pipeline **and** a foundation library — don't move it.

## Web UI

`web/app.py` is a FastAPI app that wraps both pipelines. Run with:

```
uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
```

Then open [http://127.0.0.1:8001](http://127.0.0.1:8001) (port 8000 often clashes with Docker on macOS).

Web grade jobs upload to `output/xscore/grade_uploads/<job_id>/` (segregated from CLI runs, which use `output/xscore/<exam>/<timestamp>/`). See `web/routes/grade_jobs.py` and `web/grade_service.py`.

## xscore pipeline structure

Steps are numbered 1–36. Each step writes its artifacts under `output/xscore/<exam_stem>/<timestamp>/<NN_step_name>/`. Folder-name constants and path builders live in `xscore/shared/exam_paths.py`.

Step registry: `xscore/shared/pipeline_steps.py` holds a `Step` dataclass list — canonical ordering and naming. Step bodies live in one module per phase under `xscore/steps/`: `prelude`, `scan`, `geometry`, `scaffold`, `marking`, `reports`, `summary`. `wire_step_fns()` looks each one up by name at startup. All steps are migrated; there are no `_unmigrated` placeholders.

Resume mid-pipeline: `python xScore.py "grade <exam>" --resume-dir output/xscore/<exam>/<timestamp>` — re-uses already-completed step artifacts.

Stop early / start late: `--stop-after <N>` and `--from-step <N>`.

## Marking & scaffold output formats

AI structured-output steps (scaffold 19/20, scheme parsing 23/24, marking 27/28) emit YAML — block scalars preserve LaTeX without escaping and the diffs are reviewable. Format classes live in `xscore/marking/formats/base.py` (`MarkingFormat`) and `xscore/scaffold/formats/base.py` (`ScaffoldFormat`); call sites get an instance via `get_marking_format()` / `get_scaffold_format()`.

## Scaffold subsystem

`xscore/scaffold/` is one of the largest subsystems (~24 modules) and covers steps 19–25: detecting exam structure, filling it from the empty paper, projecting question boxes onto scanned pages, splitting the mark-scheme PDF per-question, generating LaTeX templates, and caching the whole thing for resume. Resume hook: `scaffold_cache.py` (the cache key includes the empty-exam hash so re-running with the same paper short-circuits expensive AI calls).

## Prompts

Live in `xscore/prompts/<name>.md`, loaded via `xscore/prompts/loader.py`. Files have optional YAML frontmatter (`version`, `model_hint`, `output_format`, `description`). Use `load_prompt(name, /, *, section=None, **substitutions)` from the loader; templates use `$placeholder` substitution (`string.Template.safe_substitute`). Files with multiple roles use `## SECTION_NAME` H2 headers (uppercase) — pass `section=` to extract one. Shared fragments are inlined via `$include_<stem>` (resolved recursively before substitution).

## Model configuration

`default.env` declares model choices in the form `MODEL_NAME=model-id, thinking_tokens, max_tokens`. Both budgets are optional; the legacy `off | low | high` strings are still accepted (parse to `0 | 1024 | 8192` thinking tokens) for back-compat with the old two-position syntax. Example: `MARKING_MODEL=qwen3.6-plus, off`.

**Qwen gotcha**: with no thinking budget specified, Qwen defaults to `enable_thinking=True`, which forces streaming and breaks the non-streaming JSON output path. Always pass an explicit `0` (or `off`) for Qwen unless you need thinking.

Determinism is injected automatically: `ALL_AI_TEMPERATURE` and `ALL_AI_SEED` env vars are applied to every call through `make_ai_client` (default temperature 0). Pass `deterministic=False` only when you actively want sampling.

API keys go in `.env` (gitignored); `default.env` has only model selections and is safe to commit.

## AI client

`make_ai_client(*, model_env, legacy_model_env, default_model, deterministic=True)` (keyword-only) in `eXercise/ai_client.py` returns `(client, model, provider, thinking_tokens, max_tokens) | None`. `None` means the required API key is missing. Both budgets are `None` when the env string didn't specify them — callers should fall back to their own defaults.

`parse_model_spec("qwen3.6-plus, 0, 4096")` → `("qwen3.6-plus", 0, 4096)`. Provider is auto-detected from the model name (see Native PDF section).

## Multimodal pattern (Qwen / OpenAI-compat)

Image content is passed as `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}` inside the message content array. JPEG rendering: open the fitz doc once outside the loop, then `doc[idx].get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72), colorspace=fitz.csRGB)` → PIL → `to_jpeg_bytes()` from `xscore/extraction/images.py`.

## Native PDF input (Gemini / Kimi)

Steps 19 and 20 (scaffold detect / fill) dispatch on the configured model and send the empty exam paper / mark scheme as a native PDF when possible:

- **Gemini** — `gemini_pdf_part(client, path)` in `eXercise/ai_client.py` returns a `Part.from_bytes(application/pdf)` for ≤18 MB or falls back to the Files API. Used via the native `google.genai` client (`make_gemini_native_client()`).
- **Kimi / Moonshot** — `kimi_pdf_text(client, path)` uploads via `client.files.create(purpose="file-extract")`, retrieves the server-extracted text via `client.files.content(id).text`, deletes the upload, and returns the text. Caller injects the result as a system message in addition to the existing system+user prompt. Same OpenAI-compatible client as Qwen — no separate native SDK.
- **Qwen / Grok / others** — fall back to rasterizing pages to PNG and sending base64 `image_url` parts.

Provider is auto-detected from the model name: `gemini*` → Gemini, `kimi*`/`moonshot*` → Kimi, `qwen*` → Qwen, `grok*` → xAI. Set `KIMI_API_KEY` to use Kimi. The default Kimi endpoint is `https://api.moonshot.cn/v1` (China); override via `KIMI_BASE_URL=https://api.moonshot.ai/v1` for the international endpoint — keys are region-specific and not interchangeable. Step 24 marking and step 3 student list intentionally do **not** dispatch to Kimi — they keep their existing provider choices.

## Response cache

`xscore/shared/response_cache.py` — opt-in cache for the AI marking step (28) only. Activated when the user includes "reuse cache" or "use cache" in the natural-language prompt; default is OFF. Cache lives at `~/.cache/xscore/responses/<key[:2]>/<key>.json` (override with `XSCORE_CACHE_DIR`). Scope is deliberately narrow: only the OpenAI-compatible marking call (`xscore.marking.mark_page._mark_page`) is cached. The Gemini-native PDF upload path is intentionally **not** cached yet. Misses, read errors, and write errors are all silent — caching never breaks the pipeline.

## Prompt logging

Every AI call auto-saves its prompt and response to the step's artifact dir as `<task>_prompt.md` (with binary attachments as sidecar files) and `<task>_response.txt`. Two parallel implementations kept in sync: `eXercise/prompt_logger.py` (generation pipeline) and `xscore/shared/prompt_logger.py` (marking pipeline). Logging silently no-ops on I/O error so a logging fault never breaks the pipeline.

Each `<task>_response.txt` is the concatenation of the model's thinking trace (as a leading `[thinking]…[/thinking]` block) and the structured response that follows it. Both are saved together for review convenience; downstream parsers operate on the post-`[/thinking]` body only. Don't conflate verbose thinking-trace prose with content actually emitted into structured fields when auditing output.

## Test exam

`Space Physics Unit Test/` is the canonical regression input for the marking pipeline. After any structural change to `xscore/`, run:

```
python xScore.py "grade Space Physics Unit Test"
```

end-to-end and diff outputs against a baseline run.

## Exam directories

Convention is `exams/<subject_slug>/`. Current subjects: `physics`, `chemistry`, `biology`, `mathematics`, `computer_science`, `a_level_physics`, `a_level_computer_science`. The natural-language exam resolver looks here when the user names an exam in the prompt.

## System dependencies

Beyond pip, the pipelines need:

- `pdflatex` + texlive packages (`texlive-latex-extra`, `texlive-fonts-extra`) — exam paper rendering.
- `pdfjam` (`texlive-extra-utils`) — PDF post-processing.
- `poppler` (`pdftoppm`) — PDF rasterization fallback.
- `tesseract` — OCR fallback when vision-model OCR fails.

`Dockerfile` is the canonical install list; mirror it on a new dev machine.

## Diagnostic scripts

- `scripts/diagnose_qwen_json_schema.py` — probe Qwen's JSON schema acceptance.
- `scripts/diagnose_qwen_pdf_upload.py`, `scripts/diagnose_kimi_pdf_upload.py` — debug provider PDF-upload paths in isolation.
- `scripts/count_bare_keywords.py` — prompt audit helper.

## File size

Keep source files between 150–500 lines. If a file exceeds 500 lines, refactor it into smaller focused modules.

## Don't

- Don't move `eXercise/` into a `legacy/` folder — `xscore/` imports `ai_client`, `prompt_logger`, `env_load`, `config`, `fonts`, and `latex_utils` from it; the package is foundation infrastructure, not deprecated code.
- Don't add `tests/` infrastructure unless asked — the `Space Physics Unit Test/` end-to-end run is the current regression strategy and a parallel test suite would create two sources of truth.
- Don't commit anything in `output/`, `logs/`, or `Space Physics Unit Test/` — all gitignored, all contain student data or large artifacts.
