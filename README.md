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
    subgraph nlPath ["Natural language mode"]
        direction TB
        n1["Step 1 — Describe your exercise\n(subject · paper · questions)"]
        n2["Step 2 — Precheck\n(LLM sanity check · skippable)"]
        n3["Step 3 — Interpret\n(LLM maps request → PDF paths, questions, ranking flag)"]
        n1 --> n2 --> n3
    end

    subgraph legPath ["Legacy / explicit mode"]
        l1["Provide PDF paths and question numbers directly"]
    end

    cut["Step 4 — Extract questions\n(vector graphics · papers in parallel)"]

    subgraph outputs ["Outputs"]
        direction TB
        ex["exercise.pdf"]
        ms{"Mark scheme\nprovided?"}
        ans["answers.pdf — structured MS\n(regions extracted as vectors)"]
        mcqans["answers.pdf — MCQ\n(Gemini PDF upload → LaTeX explanations)"]
        nup["_2up / _4up print variants\n(requires pdfjam)"]
        ex --> ms
        ms -->|"Yes — structured"| ans --> nup
        ms -->|"Yes — MCQ"| mcqans --> nup
        ms -->|No| nup
    end

    rankCond{"Skip ranking?"}
    rank["ranking.pdf\n(hardest → easiest · background · optional)"]

    n3 --> cut
    l1 --> cut
    cut --> ex
    ex -.-> rankCond
    rankCond -->|No| rank
```

### Natural language mode (one sentence)

1. **You describe the run** — subject, which paper(s), which question numbers, and whether you want mark-scheme material. This is the same idea in the CLI (one quoted argument) or in the web **Generate** page.

2. **Optional precheck** — a small LLM call checks that your text mentions a supported subject and enough detail to identify a paper (unless you turn precheck off in config).

3. **Main interpretation** — the LLM sees the list of real PDF filenames in your exam folders and returns structured data: which question paper(s) to open, which question numbers, output filename, matching mark scheme files when they exist, and a `ranking` flag (defaults to `true`; set to `false` by saying "no ranking" in your request).

4. **Cut questions from the PDFs** — all question papers are opened in parallel; for each, the program finds where each question sits on the page and extracts those regions as vector graphics (not screenshots), preserving crisp text and diagrams.

5. **Build the exercise PDF** — all extracted strips are combined into **one continuous PDF** (your exercise sheet), with layout and headers appropriate to the subject.

6. **Answers PDF (if a mark scheme is available)** — the matching mark scheme is opened. For typical structured MS layouts, answer regions are extracted the same way as questions. For **MCQ** mark schemes, the tool uploads the question-paper PDF directly to the **Gemini Files API** (one call per batch of papers) and receives short 3-bullet explanations for each question, which are compiled into LaTeX; if `pdflatex` or the Gemini key is missing, it falls back to plain answer lines.

7. **Optional n-up copies** — if `pdfjam` is installed, **2-up** and **4-up** versions of the exercise (and answers) may be generated for printing.

8. **Difficulty ranking (background, optional)** — a second LLM job reads the assembled exercise as images and returns a ranked list of every question part from hardest to easiest. The result is compiled into `*_ranking.pdf` and appears as an extra tab in the web UI once ready. Requires `pdflatex`. Skipped if: the NL request contains "no ranking" / "skip ranking" (sets `ranking: false`), `RANKING_SKIP=true` is set in the environment, or `pdflatex` is not installed.

### Legacy mode (explicit paths)

1. You pass **question paper path**, **output path**, and **question numbers** (and optionally `--ms` with a mark scheme path).

2. Steps **4–8** above run the same way — there is **no** LLM step; the program goes straight to finding questions and building PDFs.

---

## How grading works

All four input files are required:

- **scan PDF** — the class exam scan (e.g. `scan.pdf`)
- **student roster** — `StudentList.xlsx` / `.csv` / `.pdf`
- **empty exam PDF** — blank exam template (`empty_exam.pdf`)
- **mark scheme PDF** — answer key (`answer_sheet.pdf`)

```mermaid
flowchart TD
    subgraph uploads ["Inputs (all required)"]
        direction LR
        u1[exam scan PDF]
        u2[student roster]
        u3[exam PDF]
        u4[mark scheme]
    end

    s1["Step 1 — Parse grading prompt\n(Gemini · INTERPRET_PROMPT_MODEL)"]
    s2["Step 2 — Locate exam folder\n(terminal only · fuzzy search)"]
    s3["Step 3 — Read student roster\n(Gemini · READ_STUDENT_LIST_MODEL)"]

    routeCond{"Terminal or\nweb route?"}

    subgraph cleaning ["Scan cleaning"]
        direction TB
        s4["Step 4 — Merge duplex scans\n(optional · only when two scan files exist)"]
        s5["Step 5 — Blank page detection\n(parallel · ≤ 4 CPU workers)"]
        s6["Step 6 — Auto-rotate pages"]
        s7["Step 7 — Deskew\n(IGCSE anchor detection · parallel)"]
        s4 -.->|if two scans| s5
        s5 --> s6 --> s7
    end

    s8["Step 8 — Exam geometry & cover detection\n(page count ÷ exam pages = students\n· name detection · cover-page mode)"]

    subgraph scaffold ["Exam scaffold"]
        direction TB
        s9["Step 9 — Detect layout + cut\n(Gemini · DETECT_LAYOUT_MODEL\n· splits multi-up PDFs)"]
        s10["Step 10 — Parse exam PDF\n(Gemini · READ_EXAM_PDF_MODEL)"]
        s11["Step 11 — Parse mark scheme\n(Gemini · READ_MARK_SCHEME_MODEL)"]
        s12["Step 12 — Merge report"]
        s9 --> s10 --> s11 --> s12
    end

    subgraph marking ["AI marking"]
        direction TB
        s13["Step 13 — Marking blueprints\n(per-page XML templates from scaffold)"]
        s14["Step 14 — AI marking\n(MARKING_MODEL · one API call per student page · parallel)"]
        s15["Step 15 — Compile reports\n(per-student PDF + class PDF\n· xelatex · MARKING_WORKERS)"]
        s16["Step 16 — Timing summary"]
        s13 --> s14 --> s15 --> s16
    end

    bg["Pre-render scan pages\n(background · parallel · MARKING_WORKERS threads)"]

    uploads --> s1
    s1 --> routeCond
    routeCond -->|terminal| s2 --> s3
    routeCond -->|web| s3
    s3 --> cleaning --> s8 --> scaffold --> marking
    s8 -.->|"starts in background"| bg
    bg -.->|"images ready"| s14
```

### Parallel execution — steps 3–16

```mermaid
flowchart TD
    s1["Step 1 — Parse grading prompt"]
    s2["Step 2 — Locate exam folder\n(terminal route only)"]
    s3["Step 3 — Read student roster\n(Gemini · READ_STUDENT_LIST_MODEL)"]
    s1 --> s2 --> s3

    s3 -->|"header prints → scan thread starts"| s4

    subgraph scan ["Scan thread"]
        direction TB
        s4["Step 4 — Merge duplex scans\n(optional · only when two scan files exist)"]
        s5["Step 5 — Blank page detection\n(≤ 4 CPU workers)"]
        s6["Step 6 — Auto-rotate pages"]
        s7["Step 7 — Deskew\n(parallel · N CPU workers)"]
        s4 -.->|if two scans| s5
        s5 --> s6 --> s7
    end

    subgraph step8 ["Step 8 — Exam geometry (main thread)"]
        direction TB
        s8a["8a — Compute geometry\n(scan÷exam pages → num_students\n· roster cross-check)"]
        s8b["8b — Empty-exam cover check\n(informational · EMPTY_EXAM_COVER_MODEL)"]
        s8c["8c — Assign pages + name detection\n(COVER_PAGE_DETECTION_MODEL)"]
        s8d["8d — Page-count validation\n(abort if mismatch)"]
        s8e["8e — Page order check"]
        s8f["8f — Blank page detection"]
        s8g["8g — Write final artifacts\n(8_exam_geometry.json\n8_exam_student_list.json / .md)"]
        s8a --> s8b --> s8c --> s8d --> s8e --> s8f --> s8g
    end

    s7 --> s8a

    s8g -->|"step 8 done → scaffold thread unblocks"| s9
    s8g -.->|"background thread"| bg["Pre-render scan pages\n(parallel · MARKING_WORKERS threads)"]

    subgraph scaffold ["Scaffold thread"]
        direction TB
        s9["Step 9 — Detect layout + cut\n(Gemini · DETECT_LAYOUT_MODEL)"]
        s10["Step 10 — Parse exam PDF\n(Gemini · READ_EXAM_PDF_MODEL)"]
        s11["Step 11 — Parse mark scheme\n(Gemini · READ_MARK_SCHEME_MODEL\n· uses step 10 output)"]
        s12["Step 12 — Merge scaffold"]
        s9 --> s10 --> s11 --> s12
    end

    s12 --> s13["Step 13 — Marking blueprints"]
    bg -.->|"images ready"| s14
    s13 --> s14["Step 14 — AI marking\n(MARKING_WORKERS threads\n· one per student page)"]
    s14 --> s15["Step 15 — Compile reports\n(MARKING_WORKERS\n· parallel xelatex)"]
    s15 --> s16["Step 16 — Timing summary"]
```

| Step | Description |
|------|-------------|
| **1** | Gemini parses any free-text grading prompt and returns structured config (DPI, task type, student filter). Configure with `INTERPRET_PROMPT_MODEL` in `default.env`. |
| **2** | **Terminal route only.** A fuzzy folder search locates the exam folder on disk from the hint in the prompt or `--folder` flag. Skipped on the web route because the folder is determined by the upload. |
| **3** | The student roster is read from `StudentList.*` in the exam folder. Supports `.xlsx`, `.xls`, `.csv`, and `.pdf` formats via Gemini. Writes `3_students.json` (name array) and `3_students.md` (numbered list). Configure with `READ_STUDENT_LIST_MODEL`. |
| **4** *(optional)* | Only when two scan PDFs are found in the exam folder (duplex scan split into front-pages and back-pages files). The two files are interleaved into a single combined scan. Skipped when a single scan file is present. |
| **5** | Low-resolution raster pass (72 DPI) to classify each page as blank or content. Blank pages are dropped. Runs in parallel using up to `min(4, cpu_count)` threads. |
| **6** | Each content page's PDF `/Rotate` metadata is applied so scanners that encode rotation in metadata come out portrait. Optional Tesseract OSD pass for extra correction. |
| **7** | IGCSE header anchors are detected on each page (parallel). Anchor positions drive the fine deskew transform; corrected pages are written to `7_cleaned_scan.pdf`. |
| **8** | `scan_pages ÷ exam_pages` gives `num_students`. Cross-checked against the roster; a count mismatch is a warning, not an error. Student names are detected from the first scan page of each student block and fuzzy-matched against the roster. Two independent AI checks determine **cover-page mode**: (1) an informational check on page 1 of the empty exam PDF (`EMPTY_EXAM_COVER_MODEL`) — logged to the console only, no downstream effect; (2) an authoritative check on page 1 of the scan (`COVER_PAGE_DETECTION_MODEL`) — determines whether the pipeline runs in cover-page mode. In cover-page mode, every expected cover page position is verified in parallel (a warning is printed if any block looks misaligned). Each `PageAssignment` gains a `cover_page_number` field, and step 14 skips that page during AI marking. If the empty-exam check and the scan check disagree, a warning is printed and the scan result wins. Writes `8_exam_geometry.json` (includes `cover_page_mode` flag) and `8_exam_student_list.json` / `.md`. Immediately after completion, all scan pages are pre-rendered to JPEG in a background thread (parallel, `MARKING_WORKERS` threads) so they are ready when step 14 starts. |
| **9** | Gemini renders the first page of the exam PDF as an image and detects the printing layout (1×1, 2-up, or 4-up). In multi-up mode the PDF is split into one PDF page per sub-page in reading order — the split PDF is what step 10 uploads. Writes `9_exam_layout.json` + `.md` and `9_exam_input.pdf`. Configure with `DETECT_LAYOUT_MODEL`. In legacy mode (`READ_EXAM_PDF_SPLIT=0`) layout detection and cut are skipped. |
| **10** | Gemini reads the (split) exam PDF and returns every question and sub-question with its number, type, marks, page, subpage position, and answer options. Writes `10_exam_questions.json` + `.md` (and related XML). Configure with `READ_EXAM_PDF_MODEL`. |
| **11** | Gemini reads the mark scheme and returns correct answers and marking criteria. The exam question structure from step 10 is embedded in the prompt so the model can match answers to questions. Writes `11_mark_scheme.json` + `.md` (and related XML). Configure with `READ_MARK_SCHEME_MODEL`. |
| **12** | Merges the exam question tree with mark scheme annotations into a single `12_report.json` / `12_report.xml` + `12_report.md` (and `12_short_report.*`). Runs even without a mark scheme (exam-only report). This report drives the marking blueprints and AI marking. |
| **13** | For each exam page, leaf questions from the scaffold are extracted into a per-page blueprint (`13_ai_marking_blueprint_N.xml`), including `subpage_row`/`subpage_col` coordinates and the page layout. These become the fill-in templates for step 14. |
| **14** | Each student's scan pages are sent to the vision model (one API call per page). Page images were pre-rendered in background after step 8, so all API calls fire immediately with no rendering wait. The model fills in `student_answer`, `assigned_marks`, and `explanation` for every question. All pages run in parallel (`MARKING_WORKERS` threads); results written as `14_marked_name_N.json`. Requires `DASHSCOPE_API_KEY` (or the provider matching `MARKING_MODEL`). |
| **15** | Per-student results are merged (cross-page questions: take max marks). Each student gets `15_student_report_name.json`, `.md`, and a compiled `.pdf`. A class summary PDF with per-question averages is written as `15_class_report.pdf`. PDF compilation runs in parallel (`MARKING_WORKERS` xelatex processes). Requires `xelatex`. |
| **16** | Writes `16_timing.json` and `16_timing.md` with wall-clock durations for each pipeline phase, API call counts, and per-student mark summaries. |

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

The **Grade** page depends on the `xscore` package (not in `requirements.txt`) and API keys for the models it uses:

| | |
|---|---|
| `xscore` | Install separately if you want the scan-cleaning and AI-marking pipeline |
| `GEMINI_API_KEY` | Steps 1, 3, 8–10 — prompt parsing, roster reading, layout detection, exam and mark-scheme parsing (`GOOGLE_API_KEY` accepted as fallback) |
| `DASHSCOPE_API_KEY` | Step 15 — AI marking via Qwen vision model |

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
| `gemini` | `GEMINI_API_KEY` | Google Gemini (`GOOGLE_API_KEY` accepted as fallback) |
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
| `INTERPRET_PROMPT_MODEL` | xScore step 1 — parse grading prompt |
| `READ_STUDENT_LIST_MODEL` | xScore step 3 — parse student roster files (PDF, Excel, CSV) |
| `READ_EXAM_PDF_MODEL` | xScore step 9 — extract question hierarchy from the (split) exam PDF |
| `READ_MARK_SCHEME_MODEL` | xScore step 10 — extract answers and criteria from mark scheme |
| `EMPTY_EXAM_COVER_MODEL` | xScore step 8 — checks page 1 of the empty exam PDF for a cover page (informational only; result is logged but never drives behaviour) |
| `COVER_PAGE_DETECTION_MODEL` | xScore step 8 — checks page 1 of the scan to determine cover-page mode; result is authoritative and drives all downstream logic |
| `MARKING_MODEL` | xScore step 15 — Qwen vision model for AI marking; requires `DASHSCOPE_API_KEY`; use `, off` to disable thinking (required for non-streaming JSON output) |
| `MARKING_WORKERS` | Parallel workers for step 13 (AI marking) and step 14 (xelatex compiles); default `4` |

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
| **Grade** | `/grade` | Upload student scan PDF, exam PDF, mark scheme, and optional roster; runs the full 14-step pipeline and returns a cleaned PDF plus per-student and class mark reports. Requires `xscore`, `GEMINI_API_KEY`, and `DASHSCOPE_API_KEY`. |
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

The two pipelines write to separate sub-folders under `output/`:

| Pipeline | Location |
|----------|----------|
| **eXercise** (exercise sheets) | `output/exercise/<stem>/` |
| **xScore** (exam scans, terminal) | `output/xscore/<exam_stem>/<timestamp>/` |
| **xScore** (web grade uploads) | `output/xscore/grade_uploads/<id>/` |

- `<stem>` is derived from the output PDF filename (e.g. `physics_exercise.pdf` → `output/exercise/physics_exercise/`).
- Mark scheme runs can produce `*_answers.pdf` beside the main sheet.
- With `pdfjam`, **`_2up`** and **`_4up`** variants may appear next to the main PDF.
- If `pdflatex` is installed and `RANKING_SKIP` is not set, a **`*_ranking.pdf`** is generated in the background.

---

## Project layout

| Path | Role |
|------|------|
| `eXercise.py` | CLI entry |
| `eXercise/` | Config, pipeline, NL resolver, MCQ explanations, difficulty ranking, PDF layout |
| `xScore.py` | xScore pipeline entry (steps 1–14) |
| `xscore/marking/` | Steps 1, 3, 7–14 — prompt parsing, geometry, scaffold, blueprints, AI marking, report compilation |
| `xscore/preprocessing/` | Steps 4–6 — blank detection, rotation, deskew |
| `xscore/shared/` | Terminal UI, exam path helpers, config |
| `web/app.py` | FastAPI routes and job store |
| `web/grade_service.py` | Web-facing wrapper for the xScore pipeline |
| `web/templates/` | Jinja2 HTML pages (Generate, Grade, Library) |
| `web/static/` | CSS + JS (PDF preview, zoom, tabs, download-all) |
| `exams/` | Bundled QP/MS PDFs for NL mode |
| `fonts/` | Latin Modern for labels (see `fonts/README.md`) |
| `default.env` | Committed defaults |
| `.env.example` | Template for secrets |

---

## License

No default license is included; add a `LICENSE` file if you want to specify terms.
