Codex will review your output once you are done.

# Project guide

This repo holds two Python pipelines plus a small FastAPI web UI that consumes both.

## The two pipelines

- `**eXercise/**` — exercise sheet **generation**. Flat package, ~25 modules. Entry point: `python eXercise.py "<natural-language prompt>"`.
- `**xscore/`** — exam scan **marking**. Structured package, 8 subpackages (pipeline/, steps/, shared/, marking/, scaffold/, preprocessing/, extraction/, prompts/). Entry point: `python xScore.py "grade <exam name>"`.

`eXercise/` *also* hosts shared infrastructure that `xscore/` depends on: `eXercise.ai_client`, `eXercise.prompt_logger`, `eXercise.env_load`, `eXercise.config`, `eXercise.fonts`, `eXercise.latex_utils`. Treat `eXercise/` as both a peer pipeline **and** a foundation library — don't move it.

## Web UI

`web/app.py` is a FastAPI app that wraps both pipelines. Run with:

```
uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
```

Then open [http://127.0.0.1:8001](http://127.0.0.1:8001) (port 8000 often clashes with Docker on macOS).

## xscore pipeline structure

Steps are numbered 1–30. Each step writes its artifacts under `output/xscore/<exam_stem>/<timestamp>/<NN_step_name>/`. Folder-name constants and path builders live in `xscore/shared/exam_paths.py`.

Step registry: `xscore/shared/pipeline_steps.py` holds a `Step` dataclass list. Step bodies are being incrementally migrated out of `xScore.py` into `xscore/steps/<phase>.py` modules; unmigrated entries carry `fn=_unmigrated`.

Resume mid-pipeline: `python xScore.py "grade <exam>" --resume-dir output/xscore/<exam>/<timestamp>` — re-uses already-completed step artifacts.

Stop early / start late: `--stop-after <N>` and `--from-step <N>`.

## Prompts

Live in `xscore/prompts/*.md`, loaded via `xscore/prompts/loader.py`. Files have optional YAML frontmatter (`version`, `model_hint`, `output_format`, `description`). Use `load_prompt("name", **substitutions)` from the loader; templates use `$placeholder` substitution.

## Model configuration

`default.env` declares model choices in the form `MODEL_NAME=model-id, effort` (effort is one of `off | low | high`). Gotcha: for Qwen, **the effort suffix is required** — Qwen with `effort=None` defaults to `enable_thinking=True` which forces streaming and breaks the non-streaming JSON output path. Use `MARKING_MODEL=qwen3.6-plus, off`.

API keys go in `.env` (gitignored); `default.env` has only model selections and is safe to commit.

## Test exam

`Space Physics Unit Test/` is the canonical regression input for the marking pipeline. After any structural change to xscore/, run:

```
python xScore.py "grade Space Physics Unit Test"
```

end-to-end and diff outputs against a baseline run.

## Multimodal pattern (Qwen / OpenAI-compat)

Image content is passed as `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}` inside the message content array. JPEG rendering: open the fitz doc once outside the loop, then `doc[idx].get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72), colorspace=fitz.csRGB)` → PIL → `to_jpeg_bytes()` from `xscore/extraction/images.py`.

## Native PDF input (Gemini / Kimi)

Steps 18, 20, 21 dispatch on the configured model and send the empty exam paper / mark scheme as a native PDF when possible:

- **Gemini** — `gemini_pdf_part(client, path)` in `eXercise/ai_client.py` returns a `Part.from_bytes(application/pdf)` for ≤18 MB or falls back to the Files API. Used via the native `google.genai` client (`make_gemini_native_client()`).
- **Kimi / Moonshot** — `kimi_pdf_text(client, path)` uploads via `client.files.create(purpose="file-extract")`, retrieves the server-extracted text via `client.files.content(id).text`, deletes the upload, and returns the text. Caller injects the result as a system message in addition to the existing system+user prompt. Same OpenAI-compatible client as Qwen — no separate native SDK.
- **Qwen / Grok / others** — fall back to rasterizing pages to PNG and sending base64 `image_url` parts.

Provider is auto-detected from the model name: `gemini*` → Gemini, `kimi*`/`moonshot*` → Kimi, `qwen*` → Qwen, `grok*` → xAI. Set `KIMI_API_KEY` to use Kimi. The default Kimi endpoint is `https://api.moonshot.cn/v1` (China); override via `KIMI_BASE_URL=https://api.moonshot.ai/v1` for the international endpoint — keys are region-specific and not interchangeable. Step 24 marking and step 3 student list intentionally do **not** dispatch to Kimi — they keep their existing provider choices.

## AI client

`make_ai_client(*, model_env, legacy_model_env, default_model)` (keyword-only) in `eXercise/ai_client.py` returns `(client, model, provider, effort) | None`. `parse_model_effort("qwen3.6-plus, off")` → `("qwen3.6-plus", "off")`.

## File size

Keep source files between 150–500 lines. If a file exceeds 500 lines, refactor it into smaller focused modules and update tests.

## Don't

- Don't move `eXercise/` into a `legacy/` folder — it's load-bearing for `xscore/`.
- Don't add `tests/` infrastructure unless asked; the `Space Physics Unit Test/` end-to-end run is the current regression strategy.
- Don't commit anything in `output/`, `logs/`, or `Space Physics Unit Test/` — all gitignored.

