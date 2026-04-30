# eXercise

**Version 0.5**

![Generate page — natural language prompt and example buttons](screenshots/web-ui.png)

Two pipelines for Cambridge-style IGCSE exam workflows. **eXercise** assembles printable practice sheets from bundled question papers — you describe the run in plain English, an LLM resolves it to PDF paths and question numbers, and the app extracts question regions as vector graphics, optionally attaches mark-scheme answers, generates MCQ explanations, and ranks by difficulty. **xScore** marks scanned student exams: it cleans the scan, identifies students, parses the mark scheme, runs an AI vision model over each page, and emits per-student PDF reports plus a class summary. Both pipelines share a FastAPI web UI (Generate / Grade / Library) and the same multi-provider AI client (Gemini, Qwen, Grok).

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

## Contents

- [How exercise generation works](#how-exercise-generation-works)
- [How grading works](#how-grading-works)
- [Requirements](#requirements)
- [Quick setup](#quick-setup)
- [Usage](#usage)
- [Configuration](#configuration)
- [Docker](#docker)
- [Output](#output)
- [Project layout](#project-layout)

---

## How exercise generation works

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
        u4[mark scheme PDF]
    end

    s1["Step 1 —\nInterpret prompt\n(Gemini ·\nINTERPRET_PROMPT_MODEL)"]
    s2["Step 2 —\nSelect exam folder\n(terminal only ·\nfuzzy search)"]
    s3["Step 3 —\nRead student list\n(Gemini ·\nREAD_STUDENT_LIST_MODEL)"]
    routeCond{"Terminal or\nweb route?"}

    subgraph cleaning ["Scan cleaning (steps 4–7)"]
        direction TB
        s4["Step 4 —\nMerge duplex scans\n(optional · only when\ntwo scan files exist)"]
        s5["Step 5 —\nDetect white pages\nin scanned exam\n(parallel ·\n≤ 4 CPU workers)"]
        s6["Step 6 —\nAutorotate scanned\nexam pages"]
        s7["Step 7 —\nDeskew scanned pages\n(IGCSE anchor\ndetection · parallel)"]
        s4 -.->|if two scans| s5
        s5 --> s6 --> s7
    end

    subgraph emptyexam ["Empty-exam analysis (steps 8–9)"]
        direction TB
        s8["Step 8 —\nDetect empty\nexam layout\n(DETECT_LAYOUT_MODEL)"]
        s9["Step 9 —\nCut empty exam\n(1×1 → copied ·\nmulti-up →\nsplit sub-pages)"]
        s8 --> s9
    end

    subgraph geometry ["Cover & geometry + subject (steps 10–13)"]
        direction TB
        s10["Step 10 —\nDetect cover page\nin empty exam\n(EMPTY_EXAM_COVER_MODEL)"]
        s11["Step 11 —\nDetect cover page\nin scanned exam\n(page 1 only ·\nCOVER_PAGE_DETECTION_MODEL)"]
        s12["Step 12 —\nCalculate pages\nper student\n(scan÷exam pages →\nnum_students ·\nabort on mismatch)"]
        s13["Step 13 —\nDetect exam subject\n(filename heuristic →\nGemini AI fallback ·\nSUBJECT_DETECTION_MODEL)"]
        s10 --> s11 --> s12 --> s13
    end

    subgraph identity ["Per-page vision + identity + ordering (steps 14–18)"]
        direction TB
        s14["Step 14 —\nVision classify each\nscan page\n(handwriting + page# +\nis_cover_page · parallel ·\nHANDWRITING_CHECK_MODEL)"]
        s15["Step 15 —\nDetect student names\n(cover-anchored from step 14 ·\nNAME_DETECTION_MODEL · parallel)"]
        s16["Step 16 —\nCheck page order\n(heuristic over step 14's\npage numbers — no LLM)"]
        s17["Step 17 —\nDetect blank pages\nin empty exam\n(text only ·\nEXAM_BLANK_DETECTION_MODEL)"]
        s18["Step 18 —\nBuild marking page\nregister v1\n(pure data transform ·\nno LLM)"]
        s14 --> s15 --> s16 --> s17 --> s18
    end

    subgraph scaffold ["Exam parse + cross-page + mark scheme (steps 19–25)"]
        direction TB
        s19["Step 19 —\nDetect exam scaffold\n(Phase A — structure only ·\nDETECT_EXAM_SCAFFOLD_MODEL)"]
        s20["Step 20 —\nFill exam scaffold\n(Phase B — text + options ·\nper-page parallel ·\nFILL_EXAM_SCAFFOLD_MODEL)"]
        s21["Step 21 —\nDetect cross-page\ncontext\n(figures + parent stems ·\nno LLM)"]
        s22["Step 22 —\nDetect mark scheme\ngraphics\n(DETECT_SCHEME_GRAPHICS_MODEL\n· PNG only)"]
        s23["Step 23 —\nAssign questions\nto mark scheme pages\n(ASSIGN_SCHEME_QUESTIONS_MODEL\n· Gemini: native PDF\n· Qwen: per-page PNG)"]
        s24["Step 24 —\nParse mark scheme\n(READ_MARK_SCHEME_MODEL)\n(Gemini: native PDF\n· Qwen: per-page PNG)"]
        s25["Step 25 —\nBuild grading\nscaffold\n(merges question tree\n+ mark scheme)"]
        s19 --> s20 --> s21 --> s22 --> s23 --> s24 --> s25
    end

    subgraph marking ["AI marking (steps 26–28)"]
        direction TB
        s26["Step 26 —\nBuild AI marking\nblueprints\n(per-page templates\nfrom scaffold)"]
        s27["Step 27 —\nExtract student answers\n(transcribe-only pass ·\nEXTRACT_ANSWERS_MODEL ·\nMARKING_WORKERS parallel)"]
        s28["Step 28 —\nRun AI marking\n(MARKING_MODEL ·\nMARKING_WORKERS\nparallel)\n(Gemini: native PDF\n· Qwen: per-page JPEG)"]
        s26 --> s27 --> s28
    end

    subgraph reports ["Reports (steps 29–33)"]
        direction TB
        s29["Step 29 —\nFuse AI marking\noutput to\nstudent reports\n(merge per-page marks\n· cross-page max)"]
        s30["Step 30 —\nCompute class\nstatistics + curve\n(per-question averages\n· grade distribution)"]
        s31["Step 31 —\nGenerate per-student\nreports\n(landscape + portrait\n+ 2UP · xelatex ·\nMARKING_WORKERS\nparallel)"]
        s32["Step 32 —\nGenerate class report"]
        s33["Step 33 —\nBuild review queue\n(low-confidence marks\nfor manual review)"]
        s29 --> s30 --> s31 --> s32 --> s33
    end

    subgraph summary ["Summary (steps 34–36)"]
        direction TB
        s34["Step 34 —\nSummarise step\ntimings\n(wall-clock per phase\n· API call counts)"]
        s35["Step 35 —\nEvaluate marking\naccuracy\n(vs ground truth\nif available)"]
        s36["Step 36 —\nSummarise AI costs\n(token counts ·\nRMB cost per model)"]
        s34 --> s35 --> s36
    end

    bg["Pre-render scan pages\n(background thread ·\nMARKING_WORKERS)"]

    uploads --> s1
    s1 --> routeCond
    routeCond -->|terminal| s2 --> s3
    routeCond -->|web| s3
    s3 --> cleaning --> emptyexam --> geometry --> identity --> scaffold --> marking --> reports --> summary
    s15 -.->|kicks off| bg
    bg -.->|images ready| s28
```

Same pipeline as a flat sequence — step-by-step from 1 to 36:

```mermaid
flowchart TD
    subgraph setup["Setup (1–3)"]
        s1["1 —\nInterpret\nprompt"] --> s2["2 —\nSelect exam\nfolder"] --> s3["3 —\nRead student\nlist"]
    end
    subgraph scan["Scan cleaning (4–7)"]
        s4["4 —\nMerge duplex\nscans"] --> s5["5 —\nDetect blank\npages"] --> s6["6 —\nAutorotate"] --> s7["7 —\nDeskew"]
    end
    subgraph emptyexam["Empty-exam analysis (8–9)"]
        s8["8 —\nDetect exam\nlayout"] --> s9["9 —\nCut empty\nexam"]
    end
    subgraph geometry["Cover & geometry + subject (10–13)"]
        s10["10 —\nCover page\n(empty exam)"] --> s11["11 —\nCover page\n(scan)"] --> s12["12 —\nPages per\nstudent"] --> s13["13 —\nDetect exam\nsubject"]
    end
    subgraph identity["Per-page vision + identity (14–18)"]
        s14["14 —\nVision classify\neach page"] --> s15["15 —\nDetect student\nnames"] --> s16["16 —\nCheck page\norder"] --> s17["17 —\nBlank pages\n(empty exam)"] --> s18["18 —\nBuild marking\nregister"]
    end
    subgraph scaffold["Exam parse + cross-page + mark scheme (19–25)"]
        s19["19 —\nDetect exam\nscaffold"] --> s20["20 —\nFill exam\nscaffold"] --> s21["21 —\nCross-page\ncontext"] --> s22["22 —\nMark scheme\ngraphics"] --> s23["23 —\nAssign questions\nto scheme pages"] --> s24["24 —\nParse mark\nscheme"] --> s25["25 —\nBuild grading\nscaffold"]
    end
    subgraph marking["AI marking (26–28)"]
        s26["26 —\nBuild marking\nblueprints"] --> s27["27 —\nExtract student\nanswers"] --> s28["28 —\nRun AI\nmarking"]
    end
    subgraph reports["Reports (29–33)"]
        s29["29 —\nFuse marks\nto reports"] --> s30["30 —\nClass stats\n& curve"] --> s31["31 —\nPer-student\nPDFs"] --> s32["32 —\nClass\nreport"] --> s33["33 —\nReview\nqueue"]
    end
    subgraph summary["Summary (34–36)"]
        s34["34 —\nTiming\nsummary"] --> s35["35 —\nAccuracy\nevaluation"] --> s36["36 —\nAI cost\nsummary"]
    end

    s3 --> s4
    s7 --> s8
    s9 --> s10
    s13 --> s14
    s18 --> s19
    s25 --> s26
    s28 --> s29
    s33 --> s34
```

**Steps (1–36):**

**Prompt, folder & roster (1–3)**
- 1 — Interpret prompt
- 2 — Select exam folder
- 3 — Read student list

**Scan cleaning (4–7)**
- 4 — Merge duplex scans
- 5 — Detect white pages in scanned exam
- 6 — Autorotate scanned exam pages
- 7 — Deskew scanned pages

**Empty-exam analysis (8–9)**
- 8 — Detect empty exam layout
- 9 — Cut empty exam

**Cover & geometry + subject (10–13)**
- 10 — Detect cover page in empty exam
- 11 — Detect cover page in scanned exam
- 12 — Calculate number of scanned exam pages per student
- 13 — Detect exam subject (filename heuristic → Gemini AI fallback)

**Per-page vision + identity + ordering (14–18)**
- 14 — Vision classify each scan page (handwriting + page# + is_cover_page)
- 15 — Detect student names (cover-anchored from step 14)
- 16 — Check page order (heuristic over step 14)
- 17 — Detect blank pages in empty exam (text only)
- 18 — Build marking page register v1 (data transform)

**Exam parse + cross-page + mark scheme (19–25)**
- 19 — Detect exam scaffold (Phase A — structure only)
- 20 — Fill exam scaffold (Phase B — text + options)
- 21 — Detect cross-page context (figures + parent stems)
- 22 — Detect mark scheme graphics
- 23 — Assign questions to mark scheme pages
- 24 — Parse mark scheme
- 25 — Build grading scaffold

**AI marking (26–28)**
- 26 — Build AI marking blueprints
- 27 — Extract student answers (transcribe-only pass)
- 28 — Run AI marking

**Reports (29–33)**
- 29 — Fuse AI marking output to student reports
- 30 — Compute class statistics + curve
- 31 — Generate per-student reports (landscape + portrait + 2UP)
- 32 — Generate class report
- 33 — Build review queue

**Summary (34–36)**
- 34 — Summarise step timings
- 35 — Evaluate marking accuracy
- 36 — Summarise AI costs

The pipeline is **sequential at the orchestration level**. The only true concurrency is (a) a background thread that pre-renders all scan pages to JPEG starting just after step 15 (`student_names`) — so steps 27 and 28 don't block on rasterisation — (b) `MARKING_WORKERS` parallelism *inside* steps 27 (per-page transcription), 28 (per-page marking), and 31 (one xelatex process per student PDF), and (c) per-step `*_WORKERS` env vars for the parallel sites in steps 14, 15, 20, 22, 23, and 24 (each fans out one task per LLM call on a `ThreadPoolExecutor`; defaults are uncapped via `default.env`).

**Per-page data flow (steps 14 → 15 → 16 → 18 → 27 → 28).** Step 14 vision-classifies every scan page once and writes `14_student_handwriting/handwriting.json` with `has_handwriting`, `detected_page_number`, and `is_cover_page` per page. Steps 15, 16, 17, and 18 all read that artifact:
- Step 15 uses the AI-detected covers as anchors for student-name OCR.
- Step 16 verifies each student's detected page-number sequence (no LLM, no OCR).
- Step 18 joins step 14's per-page handwriting flags with step 15's `page_assignments` and step 17's `blank_exam_pages.json` to write the v1 marking page register.

Steps 27 (transcribe-only) and 28 (AI marking) read the v1 register (refined by step 21's cross-page passes into v2): scan pages flagged no-handwriting are dropped from the marking work (no API call), and scan pages flagged with handwriting + an `attach_to_scan_page` link are appended as continuation images on the parent page's API call.

**Subject-specific prompt formatting (step 13 → steps 19, 20, 24, 27, 28).** Step 13 detects the exam subject and writes `13_detect_subject/subject.json`. Detection runs in two tiers: filename heuristic first (matches against `Subject.filename_patterns` in `xscore/shared/subjects.py`, e.g. `"0478"` → Computer Science) and Gemini AI fallback on the first 2 pages of the empty exam when no filename matched. Subjects flagged `needs_code_formatting=True` (Computer Science today) inject the `## CODE_FORMATTING` section into the scaffold + marking prompts so code/pseudocode renders monospace via `\texttt{…}` / `\begin{alltt}…\end{alltt}`. Other subjects (Physics today) skip that section.

Each run writes one folder per step under `output/xscore/<exam>/<timestamp>/`, named `NN_step_name/` (e.g. `07_deskew/`, `28_ai_marking/`). This layout is what `--resume-dir` reads from — see [Usage](#usage) for partial-run flags.

### Per-step details (1–36)

#### Prompt, folder & roster (1–3)

| Step | Description |
|------|-------------|
| **1 — Interpret prompt** | • Parses any free-text grading prompt into structured config (DPI, task type, student filter)<br>• Configure with `INTERPRET_PROMPT_MODEL` in `default.env` |
| **2 — Select exam folder** | • Terminal route only — skipped on the web route<br>• Fuzzy folder search locates the exam folder from the prompt hint or `--folder` flag |
| **3 — Read student list** | • Reads `StudentList.*` from the exam folder (`.xlsx`, `.xls`, `.csv`, `.pdf` via Gemini)<br>• Writes `03_read_student_list/students.json` and `students.md`<br>• Configure with `READ_STUDENT_LIST_MODEL` |

#### Scan cleaning (4–7)

| Step | Description |
|------|-------------|
| **4 — Merge duplex scans** *(optional)* | • Only when two scan PDFs are found (duplex split into front-pages and back-pages files)<br>• Interleaves the two files into a single combined scan<br>• Skipped when a single scan file is present |
| **5 — Detect white pages in scanned exam** | • Low-resolution (72 DPI) pass classifies each page as blank or content<br>• Blank pages are dropped<br>• Runs in parallel (up to `min(4, cpu_count)` threads) |
| **6 — Autorotate scanned exam pages** | • Applies each page's PDF `/Rotate` metadata so encoded rotation becomes portrait<br>• Optional Tesseract OSD pass for extra correction |
| **7 — Deskew scanned pages** | • Detects IGCSE header anchors on each page (parallel)<br>• Anchor positions drive a fine deskew transform<br>• Corrected pages written to `07_deskew/cleaned_scan.pdf` |

#### Empty-exam analysis (8–9)

These two steps only need the empty exam PDF (no scan dependency); they're pulled forward so problems with the empty exam surface early. They produce the cut/split version that several later steps consume.

| Step | Description |
|------|-------------|
| **8 — Detect empty exam layout** | • AI vision call detects the printing layout of the exam PDF (1×1, 2-up, 4-up) (`DETECT_LAYOUT_MODEL`)<br>• Writes `08_detect_exam_layout/exam_layout.json` + `.md` |
| **9 — Cut empty exam** | • Pure geometry step — no AI call<br>• 1×1 layout: copies the PDF to `09_cut_exam/exam_input.pdf`<br>• Multi-up: crops and reassembles each physical page into one PDF page per sub-page in reading order; writes `09_cut_exam/split_exam.pdf`<br>• Step 16 (blank detection) reads this output, so multi-up exams are blank-detected on the logical page count |

#### Cover & geometry + subject (10–13)

| Step | Description |
|------|-------------|
| **10 — Detect cover page in empty exam** | • Checks page 1 of the empty exam PDF for a cover page (`EMPTY_EXAM_COVER_MODEL`)<br>• Informational; sets `empty_exam_has_cover` (consumed by step 18's register builder)<br>• Non-fatal: network errors are logged; pipeline continues<br>• Writes prompt artifacts to `10_cover_page_empty/` |
| **11 — Detect cover page in scanned exam** | • Checks scan page 1 only for a cover page (`COVER_PAGE_DETECTION_MODEL`)<br>• Sets `cover_page_mode` — final after this step; drives `pages_per_student` in step 12<br>• Non-fatal: if `GEMINI_API_KEY` is not set, detection is skipped (standard mode assumed)<br>• Writes prompt artifacts to `11_cover_page_scan/` |
| **12 — Calculate number of scanned exam pages per student** | • Deterministic arithmetic: `pages_per_student = exam_pages + (1 if cover_page_mode else 0)`<br>• Aborts with `SystemExit(1)` if `scan_pages` is not an exact multiple of `pages_per_student` — re-scan the missing/extra page(s) and re-run<br>• Cross-checks against the roster; mismatch is a warning, not an error<br>• Writes `12_exam_geometry/exam_geometry.json` |
| **13 — Detect exam subject** | • Two-tier: filename heuristic first (matches each input PDF name against `Subject.filename_patterns` in `xscore/shared/subjects.py`, e.g. `"0478"` → Computer Science), Gemini AI fallback on the first 2 pages of the empty exam if nothing matched (`SUBJECT_DETECTION_MODEL`, default `gemini-3.1-flash-lite-preview`)<br>• Available subjects come from `AVAILABLE_SUBJECTS` env var; structured-output schema enforces the choice<br>• Sets `ctx.subject`; gates the `## CODE_FORMATTING` prompt section in steps 19, 20, 24, 27, 28<br>• Writes `13_detect_subject/subject.json` (with `detection_method: filename` or `ai`) + `subject.md` |

#### Per-page vision + identity + ordering (14–18)

Step 14 is the single vision call that classifies every scan page; downstream steps 15, 16, and 18 consume its output instead of running their own per-page calls.

| Step | Description |
|------|-------------|
| **14 — Vision classify each scan page** | • Per-scan-page vision call: returns `has_handwriting`, `detected_page_number`, and `is_cover_page` for every page<br>• Iterates the entire scan PDF (no `page_assignments` dependency yet)<br>• Configure with `HANDWRITING_CHECK_MODEL`<br>• Parallel (one task per scan page; `HANDWRITING_WORKERS`)<br>• Writes `14_student_handwriting/handwriting.json` (flat `scan_pages` list + `metadata` block) and per-page JPEGs |
| **15 — Detect student names** | • Reads step 14's `is_cover_page` flags to anchor name OCR to AI-confirmed cover positions<br>• Disagreement with positional covers (computed from `pages_per_student`) is logged as a warning — likely misorder<br>• Renders the cover pages at `NAME_RECOGNITION_DPI` (300 DPI)<br>• Per-cover-page name OCR call (`NAME_DETECTION_MODEL`); fuzzy-matched against the roster<br>• Writes `15_student_names/exam_student_list.json` / `.md`<br>• Immediately after this step, the runner kicks off background pre-rendering of every scan page to JPEG so steps 27 and 28 don't block on rasterisation |
| **16 — Check page order** | • Pure heuristic — no LLM, no OCR<br>• For each student, looks up the `detected_page_number` for every page they own and verifies the sequence matches the empty-exam layout (with `cover_offset` adjustment)<br>• Non-fatal by default; set `PAGE_ORDER_CHECK_STRICT=1` to fail-fast on detected mismatch<br>• Mismatches are summarised in the terminal as `<student> scan N: detected M (expected K)` |
| **17 — Detect blank pages in empty exam** | • Text-only LLM call: identifies blank pages in the (cut) empty exam PDF — no question text, only writing lines or "BLANK PAGE" heading<br>• Reads step 9's cut output, so multi-up exams are blank-detected on the logical page count<br>• Configure with `EXAM_BLANK_DETECTION_MODEL`<br>• Non-fatal; writes `17_exam_blank_detection/blank_exam_pages.json` |
| **18 — Build marking page register v1** | • Pure data transform — no LLM call<br>• Joins step 14's per-page handwriting flags, step 15's `page_assignments`, step 17's `blank_exam_pages.json`, and `empty_exam_has_cover` from step 10 into the v1 marking page register<br>• Drops scan pages where the AI saw no handwriting (no marking call) and attaches blank-but-handwritten pages as continuation extras<br>• Writes `18_build_marking_register/marking_page_register.json`<br>• Step 21 refines this into v2 by adding cross-page figure + parent stems |

#### Exam parse + cross-page + mark scheme (19–25)

| Step | Description |
|------|-------------|
| **19 — Detect exam scaffold** | • Phase A of the (formerly monolithic) parse_exam step<br>• One cheap call against the cut PDF returns `number/type/page/subpage/marks` (no text)<br>• Configure with `DETECT_EXAM_SCAFFOLD_MODEL`<br>• Writes `19_detect_exam_scaffold/exam_scaffold.{yaml,json,xml}` |
| **20 — Fill exam scaffold** | • Phase B: per-page parallel calls populate `text` and `options` for each question<br>• Reads step 19's scaffold from `ctx.scaffold_state` (in-memory, same run) or disk (resume)<br>• Configure with `FILL_EXAM_SCAFFOLD_MODEL` (or legacy `READ_EXAM_PDF_MODEL`)<br>• Parallel (`FILL_EXAM_SCAFFOLD_WORKERS`)<br>• Writes `20_fill_exam_scaffold/exam_questions.{yaml,json,xml}` + `pages/*.pdf` |
| **21 — Detect cross-page context** | • Pure data transform — no LLM call<br>• Augments the v1 register from step 18 with figure references ("Fig. N.N" mentioned on a different page from where it's drawn) and parent-question stems (so child sub-questions get their parent's flowchart attached)<br>• Writes `21_detect_cross_page_context/marking_page_register.json` (v2) plus diagnostics<br>• Toggle parent pass via `CROSS_PAGE_PARENT_DETECTION` |
| **22 — Detect mark scheme graphics** | • Detects graphics (diagrams, tables) on each mark scheme page; crops bounding boxes to `22_detect_mark_scheme_graphics/` (`DETECT_SCHEME_GRAPHICS_MODEL`; skipped when not set) |
| **23 — Assign questions to mark scheme pages** | • Cheap per-page vision call asks which question numbers' criteria appear on each mark scheme page (`ASSIGN_SCHEME_QUESTIONS_MODEL`; Gemini → PDF upload, Qwen → PNG)<br>• Step 24 then sends only the relevant questions per page instead of the full scaffold — fewer hallucinations on pages with 1–3 of N questions<br>• Writes `23_assign_scheme_questions/questions_per_page.json`<br>• Skipped when env var is unset → step 24 falls back to full-scaffold behaviour |
| **24 — Parse mark scheme** | • Reads the mark scheme and returns correct answers and marking criteria (`READ_MARK_SCHEME_MODEL`)<br>• Per-page scaffold is filtered by step 23's mapping (or full scaffold when step 23 was skipped)<br>• Writes `24_parse_mark_scheme/mark_scheme.json` + `.md` |
| **25 — Build grading scaffold** | • Merges the exam question tree with mark scheme annotations<br>• Writes `25_create_report/report.json` / `.xml` + `.md` and `short_report.*`<br>• Runs even without a mark scheme (exam-only report)<br>• Drives the marking blueprints and AI marking |

#### AI marking (26–28)

| Step | Description |
|------|-------------|
| **26 — Build AI marking blueprints** | • Extracts leaf questions from the scaffold for each exam page<br>• Writes per-page blueprints to `26_ai_marking_blueprints/blueprint_page_N.*`<br>• Includes subpage coordinates and page layout for the vision model |
| **27 — Extract student answers** | • Transcribe-only pre-pass: vision model reads each (student, page) scan and fills `student_answer` per question, leaving marks/explanation for step 28<br>• Same model class as `MARKING_MODEL` by default — the win is shorter outputs, not a cheaper model. Falls back to `MARKING_MODEL` when `EXTRACT_ANSWERS_MODEL` is unset<br>• Page images pre-rendered after step 15 — no rendering wait at API call time<br>• All pages run in parallel (`MARKING_WORKERS` threads); results written to `27_extract_student_answers/students/` |
| **28 — Run AI marking** | • Sends each student's scan pages to the vision model (one API call per page)<br>• Page images pre-rendered after step 15 — no rendering wait at API call time<br>• Reads the v2 marking register from step 21 (or v1 from step 18 as a fallback): no-handwriting pages are dropped, blank-with-handwriting pages are appended as continuation images<br>• Model fills in `student_answer`, `assigned_marks`, and `explanation` for every question<br>• All pages run in parallel (`MARKING_WORKERS` threads); results written to `28_ai_marking/students/`<br>• Requires `DASHSCOPE_API_KEY` (or the provider matching `MARKING_MODEL`) |

#### Reports (29–33)

| Step | Description |
|------|-------------|
| **29 — Fuse AI marking output to student reports** | • Merges per-page results into one record per student (cross-page questions: takes max marks)<br>• Writes `.xml` and `.md` per student to `29_student_report_preparation/<student>/`<br>• No PDF compile yet — that's step 31 |
| **30 — Compute class statistics + curve** | • Aggregates per-question averages across the class and produces a grade-distribution curve<br>• Writes `30_class_stats/class_stats.json` and `.md` |
| **31 — Generate per-student reports (landscape + portrait + 2UP)** | • Compiles each per-student report to PDF via `xelatex`<br>• Runs in parallel (`MARKING_WORKERS` processes); requires `xelatex`<br>• Outputs to `31_student_pdfs/` |
| **32 — Generate class report** | • Compiles the class-wide PDF (per-question averages, grade curve, combined student marks)<br>• Writes `32_class_report/class_report.pdf` |
| **33 — Build review queue** | • Extracts low-confidence or flagged marks into a manual-review queue<br>• Writes `33_review_queue/review.json` and `.md` |

#### Summary (34–36)

| Step | Description |
|------|-------------|
| **34 — Summarise step timings** | • Wall-clock durations per pipeline phase + API call counts<br>• Writes `34_timing_summary/timing.json` and `timing.md` |
| **35 — Evaluate marking accuracy** | • Evaluates marking accuracy against ground truth when present<br>• Writes `35_accuracy/accuracy.json` |
| **36 — Summarise AI costs** | • Computes token counts and RMB cost per model from `AI API costs.xlsx`<br>• Writes `36_ai_costs/` with the per-model cost breakdown |

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
| `GEMINI_API_KEY` | Required for any step whose model is a Gemini model (`GOOGLE_API_KEY` accepted as fallback). With the shipped defaults that's step 13 (detect subject — AI fallback only), steps 19 + 20 (detect + fill exam scaffold), and 24 (parse mark scheme). Other steps fall back to Gemini if their `*_MODEL` env var is set to a Gemini model. |
| `DASHSCOPE_API_KEY` | Required for any step whose model is a Qwen model (DashScope). With the shipped defaults that's steps 1, 3, 8, 10, 11, 14, 15, 17, 22, 23, 27, and 28. Switch any of these to Gemini in `default.env` and the key becomes optional. |

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

### Grading (CLI)

```bash
python xScore.py "grade Space Physics Unit Test"
```

Useful flags: `--resume-dir output/xscore/<exam>/<timestamp>` re-uses already-completed step artifacts; `--from-step N` starts at step N (assumes earlier artifacts exist on disk); `--stop-after N` halts after step N. Together they make iterating on the late marking/report stages cheap — the scan-cleaning steps don't have to re-run.

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
| **Grade** | `/grade` | Upload student scan PDF, exam PDF, mark scheme, and optional roster. Runs the **web subset** of the xScore pipeline — a condensed sequence of the 36 terminal steps (skips terminal-only stages like fuzzy folder lookup and accuracy evaluation). Returns a cleaned PDF plus per-student and class mark reports. Requires `xscore` plus the API keys for whichever providers your `*_MODEL` env vars resolve to (typically `GEMINI_API_KEY` and `DASHSCOPE_API_KEY`). |
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

### Models, thinking, and token budgets (non-secrets → `default.env`)

Every model env var follows the same one-line format:

```
<model>[, <thinking_tokens>][, <max_output_tokens>]
```

Both budgets are integers; omit either to use the code fallback. The **provider is inferred from the model-name prefix** (`gemini-*`, `qwen*`, `grok-*`) — no separate provider switch.

```env
MARKING_MODEL=qwen3.6-plus, 0, 64000          # Qwen, thinking off, 64k output
RANKING_MODEL=gemini-2.5-pro, 8192, 32768     # Gemini, deep thinking, 32k output
NAME_DETECTION_MODEL=qwen3.6-plus, 0, 64      # tight 64-token cap for name OCR
NL_MODEL=gemini-3.1-flash-lite-preview, 1024  # max_tokens omitted → fallback
```

Legacy `, off` / `, low` / `, high` strings still parse for back-compat (mapped to `0` / `1024` / `8192`).

**`thinking_tokens` semantics**

| Provider | Behaviour |
|---|---|
| Gemini (native PDF / generate_content_stream) | Any non-negative integer is passed through as `thinking_budget`. Recommended: `0` off, `1024` light, `4096` moderate, `8192` deep, `16384+` very deep (Gemini 3/3.1 only). |
| Gemini (OpenAI-compat / chat.completions) | Bucketed to `none/low/high`: `0` → `none`, `1-1024` → `low`, `1025+` → `high`. The OpenAI-compat reasoning_effort enum doesn't accept arbitrary integers. |
| Qwen | Binary — any positive value enables thinking (forces streaming). `0` disables it (non-streaming, JSON-friendly). |
| Grok | Ignored. |

**`max_output_tokens` rules of thumb**

| Range | Use case |
|---|---|
| 16–256 | single-field classification (yes/no, name extraction) |
| 1024–4096 | small JSON config / decision output |
| 8192–16384 | medium generation (MCQ explanations, page-order check) |
| 32768–64000 | long-form (mark scheme parsing, scaffold, marking) |

**Per-task model variables**

| Variable | Role |
|----------|------|
| `AI_DEFAULT_MODEL` | Fallback for any task whose own var is unset |
| `AI_PRECHECK_MODEL` | Fast validation before the main NL call |
| `NL_MODEL` | Prompt interpretation (subject, papers, questions) |
| `MCQ_MODEL` | MCQ explanation generation (Gemini gets native PDF upload) |
| `RANKING_MODEL` | Difficulty ranking (Gemini gets native PDF upload) |
| `INTERPRET_PROMPT_MODEL` | xScore step 1 — parse grading prompt |
| `READ_STUDENT_LIST_MODEL` | xScore step 3 — parse student roster files (PDF, Excel, CSV) |
| `DETECT_LAYOUT_MODEL` | xScore step 8 — detect printing layout (1×1, 2-up, 4-up) of the empty exam |
| `EMPTY_EXAM_COVER_MODEL` | xScore step 10 — informational text-based cover-page check on the empty exam |
| `COVER_PAGE_DETECTION_MODEL` | xScore step 11 — cover-page check on scan page 1 (drives `cover_page_mode`) |
| `AVAILABLE_SUBJECTS` | xScore step 13 — comma-separated list of subjects the detector may choose from (e.g. `Computer Science,Physics`). Names must match `KNOWN_SUBJECTS` in `xscore/shared/subjects.py`. |
| `SUBJECT_DETECTION_MODEL` | xScore step 13 — Gemini model used when the filename heuristic doesn't match. Native PDF input on first 2 pages; structured-output enum constrained to `AVAILABLE_SUBJECTS`. |
| `HANDWRITING_CHECK_MODEL` | xScore step 14 — per-scan-page vision LLM. Returns handwriting + printed page number + cover-page flag for every scan page. Drives steps 15, 16, and 18. |
| `NAME_DETECTION_MODEL` | xScore step 15 — student-name OCR on AI-detected cover pages. **Must use `thinking_tokens=0`** — runs through a non-streaming helper that raises if thinking is on. |
| `EXAM_BLANK_DETECTION_MODEL` | xScore step 17 — text-only LLM that identifies blank pages in the empty exam PDF |
| `DETECT_EXAM_SCAFFOLD_MODEL` | xScore step 19 — Phase A of exam parse: returns scaffold structure (number/type/page/marks, no text) |
| `FILL_EXAM_SCAFFOLD_MODEL` | xScore step 20 — Phase B: per-page parallel calls that populate question text + options. Falls back to `READ_EXAM_PDF_MODEL` when unset. |
| `READ_EXAM_PDF_MODEL` | Legacy fallback for `FILL_EXAM_SCAFFOLD_MODEL`. Gemini → native PDF upload; Qwen → per-page PNG. |
| `DETECT_SCHEME_GRAPHICS_MODEL` | xScore step 22 — graphics detection. **PNG-only for all providers** (the bbox frame requires a known raster). |
| `ASSIGN_SCHEME_QUESTIONS_MODEL` | xScore step 23 — cheap per-page vision call that lists which question numbers' criteria appear on each mark scheme page. Gemini → native PDF; Qwen → per-page PNG. Skipped when unset → step 24 sends the full scaffold per page (legacy behaviour). |
| `READ_MARK_SCHEME_MODEL` | xScore step 24 — parse mark scheme. Gemini → native PDF; Qwen → per-page PNG. |
| `EXTRACT_ANSWERS_MODEL` | xScore step 27 — transcribe-only pre-pass that fills `student_answer` per question (no marking). Falls back to `MARKING_MODEL` when unset. Gemini → native PDF; Qwen → per-page JPEG. |
| `MARKING_MODEL` | xScore step 28 — vision model for AI marking. Gemini → native PDF; Qwen → per-page JPEG. Any thinking budget works (the call streams when thinking is on). |
| `HANDWRITING_WORKERS` | xScore step 14 — parallel per-scan-page vision calls (one task per scan page). Shipped `default.env` value: `500`. |
| `NAME_WORKERS` | xScore step 15 — parallel workers for student-name OCR (one per cover page). Shipped `default.env` value: `500`. |
| `FILL_EXAM_SCAFFOLD_WORKERS` | xScore step 20 — parallel per-page fill calls. Shipped `default.env` value: `500`. |
| `SCHEME_GRAPHICS_WORKERS` | xScore step 22 — parallel mark-scheme graphics-detection vision calls (one per scheme page). Shipped `default.env` value: `500`. |
| `ASSIGN_SCHEME_QUESTIONS_WORKERS` | xScore step 23 — parallel question-assignment vision calls (one per scheme page). Shipped `default.env` value: `500`. |
| `PARSE_SCHEME_WORKERS` | xScore step 24 — parallel mark-scheme parsing calls (one per scheme page; covers both Gemini and OpenAI-compat paths). Shipped `default.env` value: `500`. |
| `MARKING_WORKERS` | Parallel workers for steps 27 (extract student answers) and 28 (AI marking). Shipped `default.env` value: `500`. Also serves as the fallback for `REPORT_COMPILE_WORKERS`. |
| `REPORT_COMPILE_WORKERS` | xScore steps 29 + 31 — parallel xelatex per-student PDF compilation. Falls back to `MARKING_WORKERS` then to `4`. Shipped `default.env` value: `500`. |

Full model lists and recommended preset values are in [`default.env`](default.env).

### Other LLM-related flags

| Variable | Meaning |
|----------|---------|
| `NL_SKIP_PRECHECK` | `true` / `1` / `yes` — skip the pre-validation step (e.g. tests). |
| `RANKING_SKIP` | `true` / `1` / `yes` — skip difficulty ranking entirely. |

Legacy fallbacks still supported in code: `AI_MCQ_MODEL` (alias for `MCQ_MODEL` resolution), `XAI_MODEL` (fallback model env), `XAI_PRECHECK_MODEL`.

### Web app (login)

| Variable | Meaning |
|----------|---------|
| `DISABLE_LOGIN` | `false` — require `ACCESS_CODE`; `true` (or unset) — open access. |
| `ACCESS_CODE` | Used when login is required. |
| `APP_SECRET_KEY` | Optional; fixes session signing across restarts (set a long random value in production). |
| `ASK_LOGIN` | Optional; session-style cookie behaviour for testing (see `web/auth_gate.py`). |

Query hints: `?disable_login=0` forces the gate on for that request; `?ask_login=1` enables ask-login mode.

### Hosting tip

Some cloud IPs are blocked by xAI. **Gemini** often behaves better on shared/datacenter IPs than Grok.

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
| `eXercise.py` | eXercise CLI entry |
| `eXercise/` | Config, NL resolver, MCQ explanations, difficulty ranking, PDF layout. Also hosts shared infra (`ai_client`, `prompt_logger`, `env_load`, `config`, `fonts`) used by both pipelines. |
| `xScore.py` | xScore pipeline entry (steps 1–36) |
| `xscore/pipeline/` | Orchestration (`runner.py`) — walks the `STEPS` registry, dispatches each step on its `phase` field, and owns the page-render background thread. |
| `xscore/steps/` | Phase modules: `prelude.py` (1–2), `scan.py` (3–7), `geometry.py` (10–18), `scaffold.py` (8–9 + 19–25), `marking.py` (26–28), `reports.py` (29–33), `summary.py` (34–36). Function names match `step.name` exactly — renumbering a step only edits the `STEPS` registry. |
| `xscore/shared/` | `pipeline_steps.py` (the canonical 36-step registry), exam path helpers (`step_folders.py`, `path_builders.py`), terminal UI, run log. |
| `xscore/marking/` | Marking-side library code: blueprint generation, AI mark calls, answer extraction, report merging. |
| `xscore/scaffold/` | Scaffold-side library code: layout detection, exam-PDF parsing, mark-scheme parsing (split across `ai_scaffold_exam.py` / `ai_scaffold_scheme.py` / `ai_scaffold.py`). |
| `xscore/preprocessing/` | Scan-cleaning library code: orientation, blank detection, rotation, deskew, cover detection. |
| `xscore/extraction/` | Provider adapters and image helpers (Gemini, Kimi, JPEG/PNG renderers). |
| `xscore/prompts/` | `.md` prompt templates loaded by `prompts/loader.py`. |
| `web/app.py` | FastAPI routes and job store |
| `web/grade_service.py` | Web-facing wrapper for the xScore pipeline (subset of the 36-step terminal pipeline) |
| `web/templates/` | Jinja2 HTML pages (Generate, Grade, Library) |
| `web/static/` | CSS + JS (PDF preview, zoom, tabs, download-all) |
| `exams/` | Bundled QP/MS PDFs for NL mode |
| `fonts/` | Latin Modern for labels (see `fonts/README.md`) |
| `default.env` | Committed defaults |
| `.env.example` | Template for secrets |

---

## License

No default license is included; add a `LICENSE` file if you want to specify terms.
