# eXercise

**Version 0.4**

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

    subgraph geometry ["Exam geometry"]
        direction TB
        s8["Step 8 — Scan geometry\n(scan÷exam pages → num_students · roster cross-check)"]
        s9["Step 9 — Cover page detection\n(empty exam · EMPTY_EXAM_COVER_MODEL)"]
        s10["Step 10 — Cover page detection (scan)\n(COVER_PAGE_DETECTION_MODEL)"]
        s11["Step 11 — Student names\n(NAME_DETECTION_MODEL · parallel)"]
        s12["Step 12 — Page count validation\n(abort if mismatch)"]
        s13["Step 13 — Page order check"]
        s14a["Step 14 — Blank page detection"]
        s8 --> s9 --> s10 --> s11 --> s12 --> s13 --> s14a
    end

    subgraph scaffold ["Exam scaffold"]
        direction TB
        s15["Step 15 — Detect empty exam layout\n(Gemini · DETECT_LAYOUT_MODEL)"]
        s16["Step 16 — Cut empty exam\n(1×1 → skipped · multi-up → split sub-pages)"]
        s17["Step 17 — Parse exam PDF\n(READ_EXAM_PDF_MODEL)"]
        s18["Step 18 — Parse mark scheme\n(graphics: DETECT_SCHEME_GRAPHICS_MODEL\n+ READ_MARK_SCHEME_MODEL)"]
        s19["Step 19 — Merge report"]
        s15 --> s16 --> s17 --> s18 --> s19
    end

    subgraph marking ["AI marking"]
        direction TB
        s20["Step 20 — Marking blueprints\n(per-page XML templates from scaffold)"]
        s21["Step 21 — AI marking\n(MARKING_MODEL · one API call per student page · parallel)"]
        s22["Step 22 — Compile reports\n(per-student PDF + class PDF\n· xelatex · MARKING_WORKERS)"]
        s23["Step 23 — Timing summary"]
        s24["Step 24 — AI Costs"]
        s20 --> s21 --> s22 --> s23 --> s24
    end

    bg["Pre-render scan pages\n(background · parallel · MARKING_WORKERS threads)"]

    uploads --> s1
    s1 --> routeCond
    routeCond -->|terminal| s2 --> s3
    routeCond -->|web| s3
    s3 --> cleaning --> geometry --> scaffold --> marking
    s11 -.->|"starts in background"| bg
    bg -.->|"images ready"| s21
```

### Parallel execution — steps 3–24

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

    subgraph geometry ["Exam geometry (main thread)"]
        direction TB
        s8["Step 8 — Scan geometry\n(scan÷exam pages → num_students · roster cross-check)"]
        s9["Step 9 — Cover page detection\n(empty exam · EMPTY_EXAM_COVER_MODEL)"]
        s10["Step 10 — Cover page detection (scan)\n(COVER_PAGE_DETECTION_MODEL)"]
        s11["Step 11 — Student names\n(NAME_DETECTION_MODEL · parallel)"]
        s12["Step 12 — Page count validation\n(abort if mismatch)"]
        s13["Step 13 — Page order check"]
        s14a["Step 14 — Blank page detection"]
        s8 --> s9 --> s10 --> s11 --> s12 --> s13 --> s14a
    end

    s7 --> s8

    s14a -->|"geometry done → scaffold thread unblocks"| s15
    s11 -.->|"background thread"| bg["Pre-render scan pages\n(parallel · MARKING_WORKERS threads)"]

    subgraph scaffold ["Scaffold thread"]
        direction TB
        s15["Step 15 — Detect empty exam layout\n(Gemini · DETECT_LAYOUT_MODEL)"]
        s16["Step 16 — Cut empty exam\n(1×1 → skipped · multi-up → split sub-pages)"]
        s17["Step 17 — Parse exam PDF\n(READ_EXAM_PDF_MODEL)"]
        s18["Step 18 — Parse mark scheme\n(graphics: DETECT_SCHEME_GRAPHICS_MODEL\n+ READ_MARK_SCHEME_MODEL)"]
        s19["Step 19 — Merge scaffold"]
        s15 --> s16 --> s17 --> s18 --> s19
    end

    s19 --> s20["Step 20 — Marking blueprints"]
    bg -.->|"images ready"| s21
    s20 --> s21["Step 21 — AI marking\n(MARKING_WORKERS threads\n· one per student page)"]
    s21 --> s22["Step 22 — Compile reports\n(MARKING_WORKERS\n· parallel xelatex)"]
    s22 --> s23["Step 23 — Timing summary"]
    s23 --> s24["Step 24 — AI Costs"]
```

| Step | Description |
|------|-------------|
| **1** | • Parses any free-text grading prompt into structured config (DPI, task type, student filter)<br>• Configure with `INTERPRET_PROMPT_MODEL` in `default.env` |
| **2** | • Terminal route only — skipped on the web route<br>• Fuzzy folder search locates the exam folder from the prompt hint or `--folder` flag |
| **3** | • Reads `StudentList.*` from the exam folder (`.xlsx`, `.xls`, `.csv`, `.pdf` via Gemini)<br>• Writes `03_read_student_list/students.json` and `students.md`<br>• Configure with `READ_STUDENT_LIST_MODEL` |
| **4** *(optional)* | • Only when two scan PDFs are found (duplex split into front-pages and back-pages files)<br>• Interleaves the two files into a single combined scan<br>• Skipped when a single scan file is present |
| **5** | • Low-resolution (72 DPI) pass classifies each page as blank or content<br>• Blank pages are dropped<br>• Runs in parallel (up to `min(4, cpu_count)` threads) |
| **6** | • Applies each page's PDF `/Rotate` metadata so encoded rotation becomes portrait<br>• Optional Tesseract OSD pass for extra correction |
| **7** | • Detects IGCSE header anchors on each page (parallel)<br>• Anchor positions drive a fine deskew transform<br>• Corrected pages written to `07_deskew/cleaned_scan.pdf` |
| **8** | • `scan_pages ÷ exam_pages` computes `num_students`<br>• Cross-checked against the roster; mismatch is a warning, not an error<br>• Writes `08_exam_geometry/exam_geometry.json` |
| **9** | • Checks page 1 of the empty exam PDF for a cover page (`EMPTY_EXAM_COVER_MODEL`)<br>• Informational; sets `empty_exam_has_cover` for blank-page detection<br>• Non-fatal: network errors are logged; pipeline continues<br>• Writes prompt artifacts to `09_cover_page/` |
| **10** | • Checks scan page 1 for a cover page (`COVER_PAGE_DETECTION_MODEL`); if detected, verifies each expected cover position in parallel<br>• Result is authoritative and drives cover-page mode in all downstream steps<br>• Non-fatal: if `GEMINI_API_KEY` is not set, detection is skipped (standard mode assumed)<br>• Writes prompt artifacts to `10_cover_page_scan/` |
| **11** | • Renders scan pages at `NAME_RECOGNITION_DPI` (300 DPI)<br>• Detects student names on the first scan page of each student block (`NAME_DETECTION_MODEL`)<br>• Fuzzy-matches names against the roster<br>• Writes `11_student_names/exam_student_list.json` / `.md`<br>• Immediately starts pre-rendering all scan pages to JPEG in a background thread |
| **12** | • Validates each student's page count against the expected `exam_pages (+ 1 cover)`<br>• Aborts with `SystemExit(1)` on mismatch, printing a per-student breakdown |
| **13** | • Verifies printed question text on each scan page is in the correct order (`PAGE_ORDER_CHECK_MODEL`)<br>• Non-fatal: exceptions are caught and logged<br>• Writes text artifacts to `13_page_order/` |
| **14** | • Identifies blank exam pages (no question text)<br>• Checks each corresponding student scan page for handwriting (`BLANK_PAGE_DETECTION_MODEL`)<br>• Pages with no handwriting are flagged as skip pages in the marking blueprint<br>• Non-fatal; writes `14_blank_pages/blank_pages.json` and per-page JPEG images |
| **15** | • AI vision call detects the printing layout of the exam PDF (1×1, 2-up, 4-up) (`DETECT_LAYOUT_MODEL`)<br>• Writes `15_detect_exam_layout/exam_layout.json` + `.md` |
| **16** | • Pure geometry step — no AI call<br>• 1×1 layout: prints "skipped" and continues immediately<br>• Multi-up: crops and reassembles each physical page into one PDF page per sub-page in reading order<br>• Split PDF saved to `15_detect_exam_layout/split_exam.pdf` |
| **17** | • Reads the exam PDF and extracts every question and sub-question<br>• Returns number, type, marks, page, subpage position, and answer options<br>• Writes `17_parse_exam_pdf/exam_questions.json` + `.md`<br>• Configure with `READ_EXAM_PDF_MODEL` (Gemini or Qwen) |
| **18** | • Detects graphics (diagrams, tables) on each mark scheme page; crops bounding boxes to `18_parse_mark_scheme/mark_scheme_graphics/` (`DETECT_SCHEME_GRAPHICS_MODEL`; skipped when not set)<br>• Reads the mark scheme and returns correct answers and marking criteria (`READ_MARK_SCHEME_MODEL`)<br>• Exam question structure from step 17 is embedded in the prompt for answer-to-question matching<br>• Writes `18_parse_mark_scheme/mark_scheme.json` + `.md` |
| **19** | • Merges the exam question tree with mark scheme annotations<br>• Writes `19_create_report/report.json` / `.xml` + `.md` and `short_report.*`<br>• Runs even without a mark scheme (exam-only report)<br>• Drives the marking blueprints and AI marking |
| **20** | • Extracts leaf questions from the scaffold for each exam page<br>• Writes per-page blueprints to `20_ai_marking_blueprints/blueprint_page_N.*`<br>• Includes subpage coordinates and page layout for the vision model |
| **21** | • Sends each student's scan pages to the vision model (one API call per page)<br>• Page images pre-rendered after step 11 — no rendering wait at API call time<br>• Model fills in `student_answer`, `assigned_marks`, and `explanation` for every question<br>• All pages run in parallel (`MARKING_WORKERS` threads); results written to `21_ai_marking/students/`<br>• Requires `DASHSCOPE_API_KEY` (or the provider matching `MARKING_MODEL`) |
| **22** | • Merges per-page results (cross-page questions: takes max marks)<br>• Writes `.json`, `.md`, and compiled `.pdf` per student to `22_compile_reports/students/`<br>• Writes class summary PDF with per-question averages to `22_compile_reports/class_report.pdf`<br>• PDF compilation runs in parallel (`MARKING_WORKERS` xelatex processes); requires `xelatex` |
| **23** | • Prints wall-clock durations per pipeline phase and API call count<br>• Evaluates marking accuracy against ground truth (if available) and writes `23_timing_summary/accuracy.json` |
| **24** | • Computes token counts and RMB cost per model from `AI API costs.xlsx`<br>• Writes `23_timing_summary/timing.json` and `timing.md` with full timing, token usage, and cost breakdown |

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
| `GEMINI_API_KEY` | Steps 1, 3, 9–15, 17–18 — prompt parsing, roster reading, cover detection, name detection, layout detection, exam and mark-scheme parsing (`GOOGLE_API_KEY` accepted as fallback) |
| `DASHSCOPE_API_KEY` | Step 21 — AI marking via Qwen vision model |

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
| `EMPTY_EXAM_COVER_MODEL` | xScore step 9 — checks page 1 of the empty exam PDF for a cover page (informational; sets `empty_exam_has_cover` for blank-page detection) |
| `COVER_PAGE_DETECTION_MODEL` | xScore step 10 — checks page 1 of the scan to determine cover-page mode; result is authoritative and drives all downstream logic |
| `DETECT_LAYOUT_MODEL` | xScore step 15 — detect printing layout (1×1, 2-up, 4-up) and split multi-up exam PDFs |
| `READ_EXAM_PDF_MODEL` | xScore step 16 — extract question hierarchy from the (split) exam PDF |
| `READ_MARK_SCHEME_MODEL` | xScore step 17 — extract answers and criteria from mark scheme |
| `MARKING_MODEL` | xScore step 20 — vision model for AI marking; requires `DASHSCOPE_API_KEY` (Qwen) or `GEMINI_API_KEY`; use `, off` to disable thinking (required for non-streaming JSON output) |
| `MARKING_WORKERS` | Parallel workers for step 20 (AI marking) and step 21 (xelatex compiles); default `4` |

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
| **Grade** | `/grade` | Upload student scan PDF, exam PDF, mark scheme, and optional roster; runs the full 23-step pipeline and returns a cleaned PDF plus per-student and class mark reports. Requires `xscore`, `GEMINI_API_KEY`, and `DASHSCOPE_API_KEY`. |
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
| `xScore.py` | xScore pipeline entry (steps 1–23) |
| `xscore/marking/` | Steps 1, 3, 8–22 — prompt parsing, geometry, name detection, page checks, scaffold, blueprints, AI marking, report compilation |
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
