# eXercise

![Generate page — natural language prompt and example buttons](screenshots/web-ui.png)

Build printable exercise sheets from Cambridge-style IGCSE question papers (PDF). You describe what you want in plain English or pass explicit file paths. The app extracts questions from bundled exam PDFs, optionally attaches mark-scheme answers, generates short MCQ explanations with an LLM, ranks questions by difficulty, and includes a browser for the bundled paper library. A separate **Grade** page accepts student exam scans and returns a cleaned, deskewed PDF ready for review.

---

## What you get

- **Natural language** — one sentence picks subject, session, paper, and question numbers; an LLM maps it to PDFs in your `exams/` folders.
- **Legacy CLI** — point at any QP PDF, list question numbers, optional mark scheme path.
- **Web UI** — three pages: **Generate** (exercise builder with PDF preview and zoom), **Grade** (scan cleaner), and **Library** (browse/download bundled papers).
- **PDF preview** — continuous-scroll in-browser render with Ctrl-wheel zoom; tabs for exercise, answers, 2-up, 4-up, and ranking variants; jump-to-question overview panel.
- **Outputs** — exercise PDF, optional answers PDF, optional 2-up/4-up print variants (`pdfjam`), and an LLM-generated difficulty ranking PDF.
- **Grading** — upload student scan(s) + optional roster; the pipeline auto-rotates, deskews, and removes blank pages, returning a clean PDF.
- **Library** — browse and download the bundled IGCSE papers by subject, year, and session directly from the web UI.

---

## How it works (step by step)

Overview (rendered on GitHub as a diagram):

```mermaid
flowchart TD
    subgraph nlPath [Natural language mode]
        direction TB
        n1["Describe: subject, paper, questions"]
        n2[Optional precheck LLM call]
        n3[LLM maps text → PDFs and question numbers]
        n1 --> n2 --> n3
    end

    subgraph legPath [Legacy / explicit mode]
        direction TB
        l1[Provide PDF paths and question numbers directly]
    end

    cut[Locate and cut each question from PDF as vector graphics]
    ex[exercise.pdf — one continuous exercise sheet]
    ms{Mark scheme\nprovided?}
    ans["answers.pdf\n(MCQ: optional LLM explanations)"]
    nup["_2up / _4up variants\n(if pdfjam installed)"]
    rank["ranking.pdf — questions ranked by difficulty\n(background LLM job)"]

    n3 --> cut
    l1 --> cut
    cut --> ex --> ms
    ms -->|Yes| ans --> nup
    ms -->|No| nup
    ex -.->|background| rank
```

### Natural language mode (one sentence)

1. **You describe the run** — subject, which paper(s), which question numbers, and whether you want mark-scheme material. This is the same idea in the CLI (one quoted argument) or in the web **Generate** page.

2. **Optional precheck** — a small LLM call checks that your text mentions a supported subject and enough detail to identify a paper (unless you turn precheck off in config).

3. **Main interpretation** — the LLM sees the list of real PDF filenames in your exam folders and returns structured data: which question paper(s) to open, which question numbers, output filename, and matching mark scheme files when they exist.

4. **Cut questions from the PDFs** — for each paper, the program opens the question paper, finds where each question sits on the page, and extracts those regions as vector graphics (not screenshots), preserving crisp text and diagrams.

5. **Build the exercise PDF** — all extracted strips are combined into **one continuous PDF** (your exercise sheet), with layout and headers appropriate to the subject.

6. **Answers PDF (if a mark scheme is available)** — the matching mark scheme is opened. For typical structured MS layouts, answer regions are extracted the same way. For **MCQ** mark schemes, the tool can optionally call the LLM once per batch to add short explanation blocks, then compile them; if TeX is missing, it falls back to simpler answer lines.

7. **Optional n-up copies** — if `pdfjam` is installed, **2-up** and **4-up** versions of the exercise (and answers) may be generated for printing.

8. **Difficulty ranking (background)** — while the exercise is ready, a second LLM job reads the assembled exercise (as images or extracted text) and returns a ranked list of every question part from hardest to easiest. This is compiled into `*_ranking.pdf` and appears as an extra tab in the web UI once ready. Requires `pdflatex`; set `RANKING_SKIP=true` or omit `pdflatex` to skip silently.

### Legacy mode (explicit paths)

1. You pass **question paper path**, **output path**, and **question numbers** (and optionally `--ms` with a mark scheme path).

2. Steps **4–8** above run the same way — there is **no** LLM step; the program goes straight to finding questions and building PDFs.

---

## How grading works

```mermaid
flowchart TD
    subgraph uploads [Uploads — web route]
        direction TB
        u1[exam scan PDF]
        u2[student roster — optional]
        u3[empty exam PDF — optional]
        u4[answer sheet — optional]
    end

    s1["Step 1 — Parse grading instructions\n(LLM extracts DPI and task options)"]
    s2["Step 2 — Locate exam folder\n(terminal route only — fuzzy folder match)"]
    s3["Step 3 — Load student roster\nwrites: 3_students.json + 3_students.md"]

    subgraph s45block ["Steps 4–5 — Build exam scaffold (optional — requires exam PDF)"]
        direction LR
        s4["Step 4 — Gemini call 1\nexam PDF → question hierarchy\nwrites: 4_exam_questions.json + 4_exam_questions.md"]
        s5["Step 5 — Gemini call 2\nanswer sheet → answers + criteria\nwrites: 5_mark_scheme.json + 5_mark_scheme.md"]
        s4 -.->|"if answer sheet present"| s5
    end

    cache["1_scaffold.json + 5_scaffold.md\n(merged scaffold — written after step 5)"]
    s6["Step 6 — Detect and remove blank pages"]
    s7["Step 7 — Auto-rotate pages to correct orientation"]
    s8["Step 8 — Deskew pages"]
    out[3_cleaned_scan.pdf — ready for marking]

    u1 & u2 & u3 & u4 --> s1
    s1 --> s2
    s2 -->|terminal| s3
    s1 -->|web| s3
    s3 --> s45block
    s45block --> cache
    s45block --> s6
    s6 --> s7 --> s8 --> out
```

| Step | Description |
|------|-------------|
| **1** | An LLM (Kimi) parses any free-text grading prompt to extract DPI, task type, and student filter options. |
| **2** | **Terminal route only.** A fuzzy folder search locates the exam folder on disk from the hint in the prompt or `--folder` flag. The web route skips this step because the folder is determined by the upload. |
| **3** | The student roster is read from `StudentList.*` in the exam folder. Supports `.xlsx`, `.xls`, `.csv`, and `.pdf` formats via Gemini. Writes `3_students.json` (plain name array) and `3_students.md` (numbered list). |
| **4** | **Optional — requires exam PDF.** Gemini call 1 reads the exam paper and returns every question and sub-question with its number, type, marks, page, and answer options. Writes `4_exam_questions.json` + `4_exam_questions.md`. Requires `GOOGLE_API_KEY`. Configure with `READ_EXAM_PDF_MODEL` in `default.env`. |
| **5** | **Optional — requires answer sheet.** Gemini call 2 reads the answer sheet and returns correct answers and marking criteria. Writes `5_mark_scheme.json` + `5_mark_scheme.md` (raw scheme, before merge), then merges with step 4 results and writes the final `1_scaffold.json` + `5_scaffold.md`. Configure with `READ_MARK_SCHEME_MODEL` in `default.env`. |
| **6–8** | Blank pages are stripped, all pages are rotated upright, and small-angle skew is corrected. The result is `3_cleaned_scan.pdf` — ready for manual or automated marking. |

The cleaned PDF has blank pages stripped, all pages upright, and skew corrected — ready for manual or automated marking.

---

## Requirements

| | |
|---|---|
| **Python** | 3.10+ (3.12+ recommended) |
| **Python deps** | `pip install -r requirements.txt` |
| **Exam PDFs** | Under `exams/physics/`, `exams/computer_science/`, `exams/mathematics/` (see [`exams/README.md`](exams/README.md)). Paths are configurable in `eXercise/config.py`. |
| **LLM** | For natural-language mode and MCQ explanations: an API key for at least one provider below (see [Configuration](#configuration)). |

### Optional system tools

If missing, the app still runs; some features are skipped or simplified.

| Feature | Needs |
|--------|--------|
| **MCQ explanations** (nice PDF blocks) | `pdflatex` + TeX packages used in `eXercise/mcq_explanations.py` |
| **2-up / 4-up sheets** | `pdfjam` on `PATH` (e.g. Debian/Ubuntu: `texlive-extra-utils`) |
| **Difficulty ranking** | `pdflatex` (same as above); set `RANKING_SKIP=true` to disable |

**Ubuntu example:**

```bash
sudo apt update
sudo apt install -y texlive-latex-extra texlive-fonts-extra texlive-extra-utils
```

The **Dockerfile** installs TeX packages so containers get `pdflatex` and `pdfjam` without extra host setup.

### Grade page (optional)

The **Grade** page depends on the `xscore` package (not in `requirements.txt`) and two API keys:

| | |
|---|---|
| `xscore` | Install separately if you want the scan-cleaning pipeline |
| `KIMI_API_KEY` | Add to `.env`; used for step 1 (prompt parsing) and step 6 (orientation detection) |
| `GOOGLE_API_KEY` | Add to `.env`; used for step 3 (student roster parsing) and steps 4–5 (AI exam scaffold) |

If `xscore` is not installed, the rest of the app runs normally — only `/grade` will return errors.

---

## Quick setup

```bash
cd "/path/to/eXercise"
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your API keys (see below). Non-secret defaults live in **`default.env`** (committed).

---

## Configuration

### Where settings live

1. **`default.env`** — safe defaults (models, login flags). Does not override variables already set in the process environment.
2. **`.env`** at the project root (gitignored) — **secrets** (API keys) and machine-specific overrides. Wins over `default.env` for keys it defines.

**Rule of thumb:** put keys only in `.env`; put shared behaviour defaults in `default.env` and commit them.

### API keys (secrets → `.env`)

The app uses the OpenAI Python client against each vendor's **OpenAI-compatible** endpoint. **You choose models by name**; the **provider is inferred from the model name** (no separate "provider" switch).

| Model name starts with | API key variable | Notes |
|------------------------|------------------|--------|
| `gemini` | `GOOGLE_API_KEY` | Google Gemini |
| `grok` | `XAI_API_KEY` | xAI Grok |
| `qwen` | `DASHSCOPE_API_KEY` | Alibaba Qwen (DashScope) |

Copy [`.env.example`](.env.example) to `.env` and fill in the keys you need.

### Models and "thinking" (non-secrets → `default.env`)

- **`AI_DEFAULT_MODEL`** — fallback model (and optional thinking suffix) when a per-task variable is unset.
- **Per task** — each can override the default:

| Variable | Role |
|----------|------|
| `AI_PRECHECK_MODEL` | Fast validation before the main NL call |
| `NL_MODEL` | Prompt interpretation (subject, papers, questions) |
| `MCQ_MODEL` | MCQ explanation generation |
| `RANKING_MODEL` | Difficulty ranking job (questions ranked hardest to easiest) |
| `STUDENT_LIST_MODEL` | Gemini model used for step 3 — parse student roster files (PDF, Excel, CSV) |
| `READ_EXAM_PDF_MODEL` | Gemini model used for step 4 — extract question hierarchy from exam PDF |
| `READ_MARK_SCHEME_MODEL` | Gemini model used for step 5 — extract answers and criteria from answer sheet |

**Optional thinking suffix:** add `, off`, `, low`, or `, high` after the model name:

```env
NL_MODEL=gemini-2.5-flash, low
AI_PRECHECK_MODEL=gemini-2.5-flash-lite, off
```

Omit the suffix to use the provider's default reasoning behaviour. **Gemini** maps `off` / `low` / `high` to API `reasoning_effort`. **Qwen** uses `off` vs on. **Grok** ignores the suffix.

Full model lists and notes (e.g. which Gemini tiers always use thinking) are in [`default.env`](default.env).

### Other LLM-related flags

| Variable | Meaning |
|----------|---------|
| `NL_SKIP_PRECHECK` | `true` / `1` / `yes` — skip the pre-validation step (e.g. tests). |
| `RANKING_SKIP` | `true` / `1` / `yes` — skip difficulty ranking entirely. |

Legacy fallbacks still supported in code: `AI_MCQ_MODEL` (alias for `MCQ_MODEL` resolution), `XAI_MODEL` (fallback model env), `XAI_PRECHECK_MODEL`.

### Web app (login)

| Variable | Meaning |
|----------|---------|
| `DISABLE_LOGIN` | `true` — open access; `false` — require `ACCESS_CODE`. |
| `ACCESS_CODE` | Used when login is required. |
| `APP_SECRET_KEY` | Optional; fixes session signing across restarts (set a long random value in production). |
| `ASK_LOGIN` | Optional; session-style cookie behaviour for testing (see `web/auth_gate.py`). |

Query hints: `?disable_login=0` forces the gate on for that request; `?ask_login=1` enables ask-login mode.

### Hosting tip

Some cloud IPs are blocked by xAI. **Gemini** often behaves better on shared/datacenter IPs than Grok.

---

## Usage

### Natural language (CLI)

```bash
python eXercise.py "Winter 2024 Physics paper 21, questions 12–14, include mark scheme"
```

### Legacy (explicit paths)

```bash
python eXercise.py /path/to/qp.pdf output.pdf 12 13 14
python eXercise.py /path/to/qp.pdf output.pdf 12-14 --ms /path/to/ms.pdf
```

### Module / help

```bash
python -m eXercise --help
```

### Web UI

Start the server and keep the terminal open:

```bash
source .venv/bin/activate
uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001) (match the port you chose). If the port is busy, try `8002` — on many Macs **8000** is already taken (often by Docker).

Three pages are available:

| Page | Path | Purpose |
|------|------|---------|
| **Generate** | `/` | Build exercise sheets (natural language or legacy); PDF preview with tabs (exercise, answers, 2-up, 4-up, ranking), Ctrl-wheel zoom, and jump-to-question overview. |
| **Grade** | `/grade` | Upload student scan PDF (+ optional roster CSV); returns cleaned, deskewed, blank-page-free PDF. Requires `xscore` + `KIMI_API_KEY`. |
| **Library** | `/library` | Browse and download the bundled Cambridge IGCSE papers by subject, year, and session. |

### Programmatic

```python
from eXercise import run_extraction_jobs

run_extraction_jobs(
    [{"input_pdf": "...", "questions": [1, 2], "mark_scheme_pdf": "..."}],
    "sheet.pdf",
    exam_key="physics",  # or "computer_science", "mathematics", or None
)
```

---

## Docker

See **`Dockerfile`** and **`docker-compose.yml`**.

- Image: Python 3.12 + TeX for `pdflatex` / `pdfjam`, then `pip install -r requirements.txt`.
- Compose maps host **80** → container **8000** by default.
- Load **`default.env`** then **`.env`** on the host; keep secrets only in `.env`.

```bash
docker compose up -d --build
```

After code changes: `git pull`, then `docker compose up -d --build` again.

---

## Output

- Relative output names go under `output/run_YYYYMMDD_HHMMSS/`.
- Mark scheme runs can produce `*_answers.pdf` beside the main sheet.
- With `pdfjam`, **`_2up`** and **`_4up`** variants may appear next to the main PDF.
- If `pdflatex` is installed and `RANKING_SKIP` is not set, a **`*_ranking.pdf`** is generated in the background.

---

## Project layout

| Path | Role |
|------|------|
| `eXercise.py` | CLI entry |
| `eXercise/` | Config, pipeline, NL resolver, MCQ explanations, difficulty ranking, PDF layout |
| `web/app.py` | FastAPI routes and job store |
| `web/grade_service.py` | Scan cleaning pipeline (rotate, deskew, blank detection) |
| `web/templates/` | Jinja2 HTML pages (Generate, Grade, Library) |
| `web/static/` | CSS + JS (PDF preview, zoom, tabs, download-all) |
| `exams/` | Bundled QP/MS PDFs for NL mode |
| `fonts/` | Latin Modern for labels (see `fonts/README.md`) |
| `default.env` | Committed defaults |
| `.env.example` | Template for secrets |

---

## License

No default license is included; add a `LICENSE` file if you want to specify terms.
