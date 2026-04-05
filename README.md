# eXercise

## Screenshots

Local web UI (**Generate** page at `http://127.0.0.1:8001`):

![Generate page — natural language prompt and example buttons](screenshots/web-ui.png)

Extract chosen questions from Cambridge-style IGCSE question papers (PDF) and lay them out into a single printable PDF. Optionally pull matching answers from a mark scheme PDF. Natural-language mode uses an LLM to pick papers and question numbers from your exam folders; legacy mode takes explicit paths.

## Requirements

- **Python 3.10+** (3.12+ recommended)
- **Python packages:** `pip install -r requirements.txt` (see file header for what each line is for)
- **Exam PDFs** for natural-language mode: bundled under `exams/physics/`, `exams/computer_science/`, and `exams/mathematics/` (see `exams/README.md`). Override paths in `eXercise/config.py` if you keep papers elsewhere.
- **LLM API key** for natural-language mode (see [Configuration](#configuration)); the code uses the OpenAI Python client against **xAI’s** OpenAI-compatible endpoint by default.

### System dependencies (optional features)

These are **not** installed via pip. If they are missing, the pipeline still runs but some outputs are skipped or simplified.

| Feature | Needs | Notes |
|--------|--------|--------|
| **MCQ explanations** (LaTeX PDF block) | `pdflatex` + TeX packages used in `eXercise/mcq_explanations.py` (`article`, `geometry`, `enumitem`, `booktabs`, `lmodern`, etc.) | Without TeX: explanations fall back to plain text. |
| **2-up / 4-up exercise PDFs** | `pdfjam` on `PATH` | On **Debian/Ubuntu Docker** this comes from **`texlive-extra-utils`**. On bare Ubuntu, if `pdfjam` is missing from your mirror, add an official `universe` source or install `texlive-extra-utils`. |

**Ubuntu (host, not Docker)** — typical install:

```bash
sudo apt update
sudo apt install -y texlive-latex-extra texlive-fonts-extra texlive-extra-utils
```

The **Dockerfile** installs `texlive-extra-utils`, `texlive-latex-extra`, and `texlive-fonts-extra` so the container has `pdflatex` and `pdfjam` without extra host steps.

## Setup

```bash
cd "/path/to/eXercise"
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Environment variables are loaded in this order (see `eXercise/env_load.py`):

1. **`default.env`** (committed) — safe defaults such as `AI_PROVIDER` and `DISABLE_LOGIN`. Does not override variables already set in the process environment.
2. **`.env`** at the project root (gitignored) — API keys and any overrides. Wins over `default.env` for keys it defines.
3. **`.env`** in the current working directory (if different from the project root).

Copy `.env.example` to `.env` and add **only API keys and other secrets**. Behaviour flags (`AI_PROVIDER`, `DISABLE_LOGIN`, optional model overrides, etc.) belong in **`default.env`** so the whole team shares them; change `default.env` and commit when you want to update those defaults. Use `.env` only for values that must never be committed.

### LLM (natural language + MCQ explanations)

Provider selection is controlled by `AI_PROVIDER`. The code uses the OpenAI Python client against the provider’s endpoint, so any OpenAI-compatible API works.

| Provider | `AI_PROVIDER` value | API key env | Default model |
|----------|---------------------|-------------|---------------|
| **Google Gemini** (default) | `gemini` | `GOOGLE_API_KEY` | `gemini-2.5-flash` |
| **xAI / Grok** | `xai` | `XAI_API_KEY` | `grok-4-1-fast-non-reasoning` |

If `AI_PROVIDER` is unset and `XAI_API_KEY` is present but `GOOGLE_API_KEY` is not, the code automatically uses `xai` for backward compatibility.

| Variable | Required | Description |
|----------|----------|-------------|
| `AI_PROVIDER` | No | `gemini` (default) or `xai`. |
| `GOOGLE_API_KEY` | Yes when `AI_PROVIDER=gemini` | API key for Google Gemini. |
| `XAI_API_KEY` | Yes when `AI_PROVIDER=xai` | API key for xAI / Grok. |
| `AI_MODEL` | No | Override model for all calls (any provider). |
| `AI_PRECHECK_MODEL` | No | Override model for the precheck call only. |
| `AI_MCQ_MODEL` | No | Override model for MCQ explanation calls only. |
| `XAI_MODEL` | No | Legacy alias for `AI_MODEL` (still supported). |
| `XAI_PRECHECK_MODEL` | No | Legacy alias for `AI_PRECHECK_MODEL`. |
| `XAI_MCQ_MODEL` | No | Legacy alias for `AI_MCQ_MODEL`. |
| `NL_SKIP_PRECHECK` | No | Set to `1` / `true` / `yes` to skip the precheck (e.g. tests). |

**Hosting note:** Some cloud providers’ IPs are blocked by xAI/Cloudflare (“abusive traffic”). Switch to `AI_PROVIDER=gemini` — Google’s API rarely blocks datacenter IPs.

### Web app (login gate)

| Variable | Required | Description |
|----------|----------|-------------|
| `DISABLE_LOGIN` | No | In **`default.env`**: `true` = no login modal; `false` = require access code. If unset anywhere, the app falls back to `true` (see `web/auth_gate.py`). |
| `ACCESS_CODE` | No | Access code when login is enabled; default `NBFLS` if unset. |
| `APP_SECRET_KEY` | Recommended when login enabled | Secret used to sign the auth cookie; set a long random string in production. |
| `ASK_LOGIN` | No | If `true`, session-style cookie behaviour for testing (see `web/auth_gate.py`). |

Query overrides (same truthy/falsey strings): `?disable_login=0` forces the gate on for that request; `?ask_login=1` enables ask-login mode.

## Usage

**Natural language** (one quoted sentence):

```bash
python eXercise.py "Winter 2024 Physics paper 21, questions 12–14, include mark scheme"
```

**Legacy** (explicit PDFs and question numbers):

```bash
python eXercise.py /path/to/qp.pdf output.pdf 12 13 14
python eXercise.py /path/to/qp.pdf output.pdf 12-14 --ms /path/to/ms.pdf
```

**Module invocation**:

```bash
python -m eXercise --help
```

## Web UI

The site is **not** started automatically—you must keep a terminal open with Uvicorn running while you use the browser.

Run a local browser UI (same natural-language flow as the one-argument CLI: prompt → generated PDFs, plus an exam library page for bundled PDFs):

```bash
cd "/path/to/eXercise"
source .venv/bin/activate
pip install -r requirements.txt
uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001) (use the same port as in the command). Put your LLM API key in `.env` (see `.env.example`) as for CLI natural-language mode. Jobs run in the background; the page polls until your sheet (and optional `*_answers.pdf`) is ready.

**If the page does not load:** (1) Confirm the terminal shows `Uvicorn running on http://127.0.0.1:…`—if you see `Address already in use`, pick another port, e.g. `--port 8002`. (2) On many Macs, **port 8000 is already taken** (often by Docker), so use `8001` or higher instead of `8000`. (3) Use the exact URL printed by Uvicorn, including the port.

**Programmatic**:

```python
from eXercise import run_extraction_jobs

run_extraction_jobs(
    [{"input_pdf": "...", "questions": [1, 2], "mark_scheme_pdf": "..."}],
    "sheet.pdf",
    exam_key="physics",  # "computer_science", "mathematics", or None for legacy-style labelling
)
```

## Docker deployment

The repo includes a **`Dockerfile`** and **`docker-compose.yml`**.

- **Image:** `python:3.12-slim` plus TeX (`texlive-extra-utils`, `texlive-latex-extra`, `texlive-fonts-extra`) for `pdflatex` and `pdfjam`, then `pip install -r requirements.txt`.
- **Runtime:** `uvicorn` on port **8000** inside the container; compose maps **host `80` → container `8000`**.
- **Env files:** Compose loads **`default.env`** (from the repo) then **`.env`** on the host. Put **only secrets and overrides** in `.env` (`GOOGLE_API_KEY`, `XAI_API_KEY`, `APP_SECRET_KEY`, etc.). Do **not** commit `.env`.

```bash
docker compose up -d --build
```

Docker **caches** layers: the TeX `apt-get` step is **not** re-run on every build unless you change the Dockerfile above that line or use `--no-cache`.

After **code** changes: `git pull` on the server, then `docker compose up -d --build` again.

## Output

- Bare filenames (e.g. `sheet.pdf`) are written under `output/run_YYYYMMDD_HHMMSS/`.
- A mark scheme run also produces `sheet_answers.pdf` beside the main output when applicable.
- When `pdfjam` is available, sibling **`_2up`** and **`_4up`** PDFs may be created next to the main exercise sheet.

## Project layout

| Path | Role |
|------|------|
| `eXercise.py` | Thin CLI entry point |
| `eXercise/` | Package: config, question detection, vector PDF layout, mark schemes, NL resolver, pipeline |
| `web/` | FastAPI app, templates, and static assets for the local web UI |
| `exams/physics/`, `exams/computer_science/`, `exams/mathematics/` | Bundled question paper & mark scheme PDFs for NL mode |
| `fonts/lmroman10-*.otf` | Latin Modern Roman (LaTeX `lmodern` text) for raster labels; see `fonts/README.md` |
| `Dockerfile`, `docker-compose.yml` | Container build and run |
| `default.env` | Committed non-secret defaults; merged before `.env` |
| `.env.example` | Template for a gitignored `.env` (secrets only) |
| `.dockerignore` | Keeps `.git`, `.env`, caches out of the image build context |

## License

Add a `LICENSE` file if you want to specify terms; the repository currently has no default license.
