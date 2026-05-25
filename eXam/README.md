# eXam — on-screen exam practice with AI marking

`eXam/` is one of three top-level pipelines in this repo:

- **`eXercise/`** — generates exercise sheets from a natural-language prompt.
- **`xscore/`** — marks scanned student exam papers against an AI-extracted scheme.
- **`eXam/`** *(this package)* — serves Cambridge past papers on screen, indexes them on demand, marks each submitted answer with AI, and lazily generates per-question hints / solutions / examples / KB topics.

eXam is a **consumer** of the other two. It reuses xscore's scaffold extraction (`ai_scaffold_exam`, `ai_scaffold_scheme`) to index a paper, and eXercise's vector-rendering primitives (`collect_vector_strips`, `layout_vector_strips_to_pdf`) to crop a single question out of the source PDF as its own snippet for the browser.

## Two modes

| Mode | Audience | Auth | Entry route |
|---|---|---|---|
| **Student-mode** | Enrolled students taking teacher-built tests | HMAC PIN cookie (`auth.py`) | `/eXam/login` → `/eXam/` |
| **Open-mode** | Anyone (public practice) | Anonymous cookie session | `/eXam/practice/<subject>` |

Both modes share the same on-disk **bank** of indexed papers and snippet PDFs — the difference is gating, persistence (DB vs. cookie), and which papers are exposed (open-mode is locked to the most recent year so the latest material is what visitors see).

## How it fits together

```mermaid
flowchart TB
    subgraph User["Browser"]
        S[Student / Visitor]
    end

    subgraph Web["FastAPI (web/app.py)"]
        RS[/eXam_student.py/]
        RT[/eXam_teacher.py/]
        RO[/eXam_open.py/]
    end

    subgraph eXam["eXam/ (this package)"]
        RT2[test_builder.py<br/>natural-lang → papers]
        BANK[bank.py<br/>ensure_paper_indexed<br/>ensure_question_pdf]
        RT3[runtime.py<br/>load YAML, order qs]
        MK[marker.py<br/>MCQ / numeric / free]
        PG[pregenerate.py<br/>hint / solution / example / kb]
        OM[open_mode.py<br/>public sessions]
        DB[(eXam.db<br/>SQLite)]
    end

    subgraph Deps["Reused pipelines"]
        XS[xscore.scaffold<br/>ai_scaffold_exam<br/>ai_scaffold_scheme]
        EX[eXercise.rendering<br/>collect_vector_strips<br/>layout_vector_strips_to_pdf]
        AI[eXercise.ai_client<br/>Gemini / Qwen]
    end

    subgraph Disk["output/eXam/"]
        F1[bank/&lt;subject&gt;/&lt;paper_stem&gt;/<br/>exam_questions.yaml<br/>mark_scheme.yaml<br/>&lt;qnum&gt;/question.pdf<br/>&lt;qnum&gt;/helpers/*.md]
    end

    subgraph Source["exams/"]
        SRC[Cambridge PDFs<br/>per subject_slug/]
    end

    S --> RS
    S --> RO
    RS -->|/api/submit| MK
    RS -->|/pdf/{qid}| BANK
    RT --> RT2
    RT2 --> BANK
    RT2 --> PG
    BANK --> XS
    BANK --> EX
    XS -.reads.-> SRC
    EX -.reads.-> SRC
    BANK -->|writes| F1
    PG --> AI
    PG -->|writes| F1
    MK --> AI
    RT3 -->|reads| F1
    RS --> RT3
    RO --> OM
    OM --> RT3
    MK -->|attempts| DB
    OM -->|sessions| DB
    RS -->|test+attempts| DB
    RT -->|tests| DB
```

## Modules

- **`bank.py`** — the bank index. `ensure_paper_indexed(paper_path)` runs the xscore scaffold (segmentation + scheme parsing) and writes the per-paper YAMLs once. `ensure_question_pdf(question_id)` renders a single-question snippet PDF using eXercise's vector-strip layout and caches it next to the YAMLs.
- **`runtime.py`** — loads cached YAMLs at request time, parses question IDs into a canonical form, computes per-student question order (with optional shuffle), and exposes `_collect_leaves(...)` used by the marker.
- **`marker.py`** — three marking paths chosen by question type: **MCQ** (deterministic letter match), **numeric final-answer** (unit-aware via `_parse_value_unit` + `_units_match`), **free-response** (Qwen text-only, or Gemini with the question's snippet PDF attached when the question contains images). All paths return `{assigned_marks, reasoning}` and write to `attempts`.
- **`pregenerate.py`** — generates four kinds of helpers per question on demand: `hint`, `solution`, `example` (analogous worked example), `kb` (knowledge-base topic). Multimodal via Gemini when the question has images, text-only via Qwen otherwise. Cached as markdown under `helpers/`.
- **`warm_bank.py`** — CLI to bulk-index every paper for a subject in a given year. Equivalent to calling `ensure_paper_indexed` per paper, used to prime the cache before a session.
- **`test_builder.py`** — resolves a teacher's natural-language prompt to a paper + question selection (via eXercise's resolver), then runs `ensure_paper_indexed` + helper pre-generation. Submitted to a module-level `ThreadPoolExecutor` (max 2 workers) so the POST returns immediately and the build proceeds in the background.
- **`open_mode.py`** — public-practice plumbing: lists most-recent-year papers per subject, picks a random question for a visitor, manages anonymous sessions and per-question view/attempt rows.
- **`auth.py`** — HMAC-SHA256 PIN cookie (12-hour TTL) keyed to `student_id`.
- **`users.py`** — pbkdf2_sha256 password hashing + username normalization (teacher accounts).
- **`roster.py`** — XLSX → `students` table importer; emits a PIN-card PDF for the teacher to print.
- **`cost_tracker.py`** — per-AI-call rows in `ai_calls`; aggregation queries for test/student/question cost breakdown.
- **`flush_cache.py`** — CLI to purge helpers (`--helpers`) or helpers + snippet PDFs (`--snippets`) from the bank.
- **`render_helper.py`** — markdown → HTML with KaTeX delimiter passthrough (browser renders math).
- **`results_export.py`** — SQLite → XLSX for a test (per-student rows × per-question columns + topic rollup).
- **`db.py`** — SQLite schema (`students`, `tests`, `attempts`, `question_helpers`, `open_sessions`, `open_views`, `open_attempts`, `ai_calls`, `users`) in WAL mode, with a simple migration framework.

## Data flow

**Student opens a test:**

1. `POST /eXam/login` → PIN verified against `students` table → HMAC cookie set.
2. `GET /eXam/test/<test_id>` loads the test row, calls `runtime.load_paper_questions()` to read `exam_questions.yaml`, computes per-student question order.
3. For each question shown, the page requests `GET /eXam/pdf/<question_id>` → `bank.ensure_question_pdf()` (cache hit returns immediately; cache miss runs the eXercise vector-strip pipeline once).

**Student submits an answer:**

1. `POST /eXam/api/submit` with `{question_id, answer}`.
2. `marker._mark_leaf_dispatch()` picks MCQ / numeric / free-response based on the leaf's metadata.
3. For free-response with images, the snippet PDF is attached to the Gemini call.
4. Result + AI-call cost are written to `attempts` and `ai_calls`. The response includes `{assigned_marks, max_marks, reasoning}`.

**Teacher creates a new test from a prompt:**

1. `POST /eXam/api/teacher/create-test` with `{prompt}` (e.g. *"physics paper 4 from 2024, omit MCQ"*).
2. A row in `tests` is created with `status='building'`.
3. `test_builder.run_build()` is submitted to the background executor:
   - eXercise's natural-language resolver picks the papers,
   - `bank.ensure_paper_indexed()` runs xscore scaffold per paper (the slow step — minutes per paper on cold AI cache),
   - `pregenerate.generate_all_helpers()` warms hint/solution/example/kb per question.
4. `status` flips to `ready`; the teacher polls `/api/teacher/build-status/<test_id>`.

## On-disk layout

```
output/eXam/
├── eXam.db                          # SQLite (WAL mode)
└── bank/
    └── <subject_slug>/
        └── <paper_stem>/            # e.g. "0625_w23_qp_42"
            ├── exam_questions.yaml  # xscore segmentation output
            ├── mark_scheme.yaml     # xscore scheme parser output
            ├── paper_sha.txt        # sha256 of source PDF (used for source resolution)
            └── <qnum>/              # e.g. "3a" or "5bii"
                ├── question.pdf     # cropped snippet (eXercise output)
                └── helpers/
                    ├── hint.md
                    ├── solution.md
                    ├── example.md
                    └── kb.md
```

Source PDFs live in `exams/<subject_slug>/` and are addressed by sha256 (so renaming a file doesn't invalidate the bank cache).

## Entry points

**Web (FastAPI):**

| Route | Module |
|---|---|
| `/eXam/login`, `/eXam/`, `/eXam/test/{id}`, `/eXam/pdf/{qid}`, `/eXam/api/submit`, `/eXam/api/helper` | `web/routes/eXam_student.py` |
| `/eXam/teacher`, `/eXam/api/teacher/create-test`, `/eXam/api/teacher/build-status/{id}`, `/eXam/api/teacher/export`, `/eXam/api/teacher/cost` | `web/routes/eXam_teacher.py` |
| `/eXam/practice/`, `/eXam/practice/{subject}`, `/eXam/practice/pdf/{qid}`, `/eXam/practice/submit`, `/eXam/practice/helper` | `web/routes/eXam_open.py` |

Run the web app:

```
uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
```

**CLI:**

```
# Index every 2025 paper for one subject (slow, runs xscore scaffold per paper)
.venv/bin/python -m eXam.warm_bank --subject physics --year 2025

# Drop cached helpers (keep snippet PDFs and YAMLs)
.venv/bin/python -m eXam.flush_cache --helpers

# Drop helpers + snippet PDFs (force re-render on next view)
.venv/bin/python -m eXam.flush_cache --snippets

# Import a student roster + print PIN cards
.venv/bin/python -m eXam.roster --xlsx path/to/roster.xlsx --class "9B"
```

## Dependencies on the other pipelines

**From `xscore/`** (paper indexing):

- `xscore.scaffold.ai_scaffold_exam` — AI-driven question segmentation.
- `xscore.scaffold.ai_scaffold_scheme` — AI-driven mark-scheme extraction.
- Various `xscore.scaffold.formats`, `scaffold_detect`, `scaffold_fill`, `scaffold_prompts` helpers.

**From `eXercise/`** (rendering + AI primitives):

- `eXercise.config` — `PROJECT_ROOT`, `get_subject_config()`, `EXAM_ROOT_BY_KEY`.
- `eXercise.questions` — `find_question_positions()`, `get_question_regions()`.
- `eXercise.rendering` — `collect_vector_strips()`, `layout_vector_strips_to_pdf()`.
- `eXercise.ai_client` — Gemini + Qwen client factories (multimodal helpers).

## Cost & caching notes

- **Paper indexing** is the expensive step — both YAML files are AI-generated and run through xscore's scaffold cache (keyed on the source paper sha). The first index of a paper costs a few cents and a few minutes; subsequent loads are sidecar reads.
- **Snippet PDFs** are deterministic given the source PDF + the paper's `exam_questions.yaml`; no AI, ~100–500 ms per question.
- **Helpers** are AI-generated on first request and cached as markdown. `flush_cache --helpers` is the way to regenerate them after a prompt-template change.
- **AI marking** runs on every submission; cost rows land in `ai_calls`. Use the teacher cost endpoint or `cost_tracker.aggregate_*` helpers to see per-test / per-student / per-question spend.
