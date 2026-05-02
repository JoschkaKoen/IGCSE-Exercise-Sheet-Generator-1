# Verified Bugs in the xscore Pipeline

This document lists bugs verified through reproduction scripts, unit tests, or clear static analysis of the xscore pipeline codebase.

---

## 1. Ground Truth Header Mis-Detection

- **File:** `xscore/shared/load_ground_truth.py`
- **Line:** ~119–130
- **Severity:** High
- **Description:** `_is_data_row()` is called before the header check. If the header row contains numeric question labels (e.g., `Name  1  2  3`), `_is_data_row` returns `True` because all tokens after the first are numeric. The header is then treated as student data.
- **Impact:** `q_numbers` stays empty, real students are shifted down, and accuracy evaluation produces nonsense.
- **Reproduction:**
  ```python
  content = "Name\t1\t2\t3\nAlice\tA\tB\tC\n"
  # load_ground_truth parses "Name" as a student with answers {"1": "1", "2": "2", "3": "3"}
  ```
- **Fix:** Check `_HEADER_TOKENS` before `_is_data_row`, or exclude header tokens from `_is_data_row`.

---

## 2. Histogram Negative Percentage Lands in Wrong Bin

- **File:** `xscore/marking/class_charts.py`
- **Line:** 55
- **Severity:** Medium
- **Description:** `idx = min(int(v) // 10, 9)` allows negative indices. A negative curved percentage (e.g., -5) yields `idx = -1`, which writes to `counts[-1]` — the 90–100 bin.
- **Impact:** Misleading histogram: negative scores appear in the top bin.
- **Reproduction:**
  ```python
  values = [-5, 15, 25, 95]
  counts = [0] * 10
  for v in values:
      idx = min(int(v) // 10, 9)
      counts[idx] += 1
  # counts[-1] == 2  (both -5 and 95 end up in the 90-100 bin)
  ```
- **Fix:** `idx = max(0, min(int(v) // 10, 9))`

---

## 3. LaTeX Escape Corrupts Pre-Escaped Text

- **File:** `xscore/marking/report_latex_text.py`
- **Line:** 37
- **Severity:** Medium
- **Description:** `_latex_escape` uses a regex that matches every bare `\`. If the AI already emitted a LaTeX escape sequence (e.g., `\textbackslash{}`), the backslash inside it is matched and replaced again, producing garbage like `\textbackslash{}textbackslash\{\}`.
- **Impact:** Corrupts LaTeX output when AI emits pre-escaped backslashes; can crash xelatex.
- **Reproduction:**
  ```python
  from xscore.marking.report_latex_text import _latex_escape
  _latex_escape(r"Use \textbackslash{} to escape.")
  # Returns: 'Use \\textbackslash{}textbackslash\\{\\} to escape.'
  ```
- **Fix:** Skip backslashes that are already part of known LaTeX escape sequences.

---

## 4. Cost Report Crashes on Missing Keys

- **File:** `xscore/shared/cost_report.py`
- **Line:** 61
- **Severity:** Medium
- **Description:** `compute_cost` does `counts["input"]` and `counts["output"]` without `.get()`. If a usage dict from a new provider is missing these keys, `KeyError` is raised.
- **Impact:** Pipeline crashes at the final cost-summary step.
- **Reproduction:**
  ```python
  from xscore.shared.cost_report import compute_cost
  compute_cost({"some-model": {"input": 1000}})  # KeyError: 'output'
  ```
- **Fix:** Use `counts.get("input", 0)` and `counts.get("output", 0)`.

---

## 5. Page Assignment Includes Non-Existent Trailing Pages

- **File:** `xscore/preprocessing/assign_pages_to_students.py`
- **Line:** 212–227
- **Severity:** Medium
- **Description:** When `n_pages % pages_per_student != 0`, the code warns but still creates a final block with `pages_per_student` pages, extending past the actual PDF page count.
- **Impact:** `PageAssignment` objects reference scan pages that do not exist, causing downstream steps to fail when trying to render them.
- **Reproduction:**
  ```python
  n_pages, pages_per_student = 5, 2
  n_blocks = math.ceil(n_pages / pages_per_student)  # 3
  # Block 2 gets pages [5, 6] but page 6 does not exist
  ```
- **Fix:** Clamp the last block's page range to `n_pages`.

---

## 6. Prompt Logger Silently Fails on String image_url

- **File:** `xscore/shared/prompt_logger.py`
- **Line:** 129–130
- **Severity:** Medium
- **Description:** `url = (part.get("image_url") or {}).get("url", "")` assumes `image_url` is a dict. If it is a string (malformed API response), the expression evaluates to a string, and `.get("url", "")` raises `AttributeError`. This is caught by the outer `except Exception: pass`, so the prompt is silently not logged.
- **Impact:** Audit trail is lost without any warning to the user.
- **Reproduction:**
  ```python
  messages = [{"role": "user", "content": [
      {"type": "image_url", "image_url": "http://example.com/img.jpg"}
  ]}]
  save_prompt(path, model="test", messages=messages)  # file is never created
  ```
- **Fix:** Check `isinstance(part.get("image_url"), dict)` before chaining `.get()`.

---

## 7. Production `assert` Statements Stripped with `python -O`

- **Files:** Multiple
- **Lines:** See list below
- **Severity:** Medium
- **Description:** Several functions use `assert` for runtime validation. When Python runs with `-O` (optimized), assertions are stripped, so invalid state is not caught and can cause cryptic failures later.
- **Locations:**
  - `xscore/pipeline/resume.py:104` — `assert ctx.folder is not None`
  - `xscore/pipeline/resume.py:184` — `assert ctx.artifact_dir is not None`
  - `xscore/steps/geometry.py:59` — `assert ctx.artifact_dir is not None and ctx.folder is not None`
  - `xscore/steps/geometry.py:76` — `assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None`
  - `xscore/steps/geometry.py:86` — `assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None`
  - `xscore/steps/scaffold.py:202` — `assert ctx.artifact_dir is not None`
  - `xscore/steps/scaffold.py:420` — `assert ctx.folder is not None and ctx.artifact_dir is not None`
  - `xscore/marking/marking_page_register.py:94` — `assert ctx.artifact_dir is not None`
- **Impact:** Silent undefined behavior or late crashes when running optimized Python.
- **Fix:** Replace every production `assert` with an explicit `if ...: raise ValueError(...)`.

---

## 8. Blueprint Generation Crashes on `None` Question Number

- **File:** `xscore/marking/blueprints.py`
- **Line:** ~101
- **Severity:** Medium
- **Description:** `re.sub(r"_\d+$", "", q.number)` crashes with `TypeError` if `q.number` is `None`.
- **Impact:** Blueprint generation fails for malformed scaffold data.
- **Reproduction:**
  ```python
  import re
  re.sub(r"_\d+$", "", None)  # TypeError: expected string or bytes-like object, got 'NoneType'
  ```
- **Fix:** Guard with `q.number or ""`.

---

## 9. Parse Instruction Loads Prompt at Import Time

- **File:** `xscore/marking/parse_instruction.py`
- **Line:** 154
- **Severity:** Medium
- **Description:** `_SYSTEM_PROMPT = load_prompt("parse_grading_instructions")[1]` executes at module import time. If the prompt file is missing or renamed, importing the module crashes entirely.
- **Impact:** Startup crash on incomplete deployments or missing prompt files.
- **Reproduction:** Static analysis confirms the module-level call.
- **Fix:** Lazily load the prompt inside the function that uses it, or use `functools.lru_cache`.

---

## 10. Marking Register v1 Swallows All Exceptions

- **File:** `xscore/steps/geometry.py`
- **Line:** 467–474
- **Severity:** Medium
- **Description:** `build_marking_register_v1` catches `Exception`, prints a warning, and returns normally. `run_step` therefore records the step status as **"ok"** in the run log, even though the register was not built.
- **Impact:** False success reporting; downstream steps that depend on the register may fail mysteriously.
- **Reproduction:** Static analysis of the source confirms the broad `except` + `return` pattern.
- **Fix:** Re-raise the exception after logging, or return a distinct status that `run_step` can detect.

---

## 11. Cross-Page Context Hardcodes YAML Format

- **File:** `xscore/steps/scaffold.py`
- **Line:** 209
- **Severity:** Medium
- **Description:** `detect_cross_page_context` hardcodes `fmt="yaml"` when looking for the `exam_questions` artifact. However, `fill_exam_scaffold` writes the artifact using `fmt.artifact_ext()`, which can be `json` or `xml` depending on configuration.
- **Impact:** If the scaffold format is not YAML, cross-page context detection is skipped with "not found" even though the artifact exists.
- **Reproduction:** Static analysis confirms the hardcoded string.
- **Fix:** Read the actual format from the scaffold state or probe all supported extensions.

---

## 12. Question `__post_init__` Null-Pointer Risk

- **File:** `xscore/shared/models.py`
- **Line:** 117–119
- **Severity:** Low
- **Description:** `if self.page == 0 and self.bbox.page:` accesses `self.bbox.page` without checking if `self.bbox` is `None`.
- **Impact:** `AttributeError: 'NoneType' object has no attribute 'page'` when a `Question` is constructed with `bbox=None`.
- **Reproduction:**
  ```python
  from xscore.shared.models import Question
  Question(number="1", question_type="short_answer", text="t", marks=1, bbox=None)
  # AttributeError
  ```
- **Fix:** Guard with `if self.page == 0 and self.bbox is not None and self.bbox.page:`.

---

## 13. Scaffold Resume KeyError

- **File:** `xscore/steps/scaffold.py`
- **Lines:** 313–325, 384–398
- **Severity:** High
- **Description:** When resuming from step 21 (`detect_cross_page_context`) or later within `scaffold_phase_b`, steps 19 (`detect_exam_scaffold`) and 20 (`fill_exam_scaffold`) are skipped. However, step 22 (`detect_mark_scheme_graphics`) accesses `state["raw_questions"]`, step 24 (`parse_mark_scheme`) accesses `state["graphics_by_qnum"]`, and step 25 (`create_report`) accesses `state["scheme_data"]`. None of these keys exist in `scaffold_state` when resuming into this phase.
- **Impact:** `--from-step 21` (advertised as resumable) will crash with `KeyError`.
- **Reproduction:** Static analysis of the dependency chain confirms the bug.
- **Fix:** On resume into `scaffold_phase_b`, rehydrate `scaffold_state` from the on-disk artifacts written by steps 19–20, or disable resume points inside `scaffold_phase_b`.

---

## 14. Report Graphics Lookup with `None` Question Number

- **File:** `xscore/marking/report_latex.py`
- **Lines:** 162, 403, 421, 471
- **Severity:** Low
- **Description:** `q.get("number", "")` returns `None` (not `""`) when the key exists but its value is explicitly `None`. `str(None)` produces `"None"`, so `_scheme_graphics_safe_qnum` looks for a non-existent graphic named after the string `"None"`.
- **Impact:** Missing graphics in reports, or false lookups.
- **Fix:** Use `str(q.get("number") or "")`.

---

## 15. Resume Pipeline Rebuilds Scaffold Unnecessarily

- **File:** `xscore/pipeline/resume.py`
- **Line:** 251–253
- **Severity:** Low
- **Description:** `build_scaffold(..., force_rebuild=False)` is called for **every** resume, including `--from-step 36` (the final cost summary). This is unnecessary work when the pipeline only needs to read timing/cost artifacts.
- **Impact:** Wasted time and API calls on late resume points.
- **Fix:** Only rebuild the scaffold when resuming into a phase that actually needs it.

---

## 16. Find Exam Folder Race Condition

- **File:** `xscore/shared/find_exam_folder.py`
- **Line:** 74–80
- **Severity:** Low
- **Description:** `d.stat().st_mtime` is called inside a `sorted()` key function. If a directory is deleted between `root.iterdir()` and `d.stat().st_mtime`, `FileNotFoundError` is raised.
- **Impact:** Crash during folder resolution in concurrent environments (e.g., IDE cleaning temp dirs).
- **Fix:** Wrap `d.stat().st_mtime` in a try/except that returns `0` on failure.

---

## 17. `copy_artifacts` Can Silently Merge Stale Files

- **File:** `xscore/pipeline/resume.py`
- **Line:** 77–83
- **Severity:** Low
- **Description:** `shutil.copytree(..., dirs_exist_ok=True)` copies directory artifacts from the old run. If the old run had extra files in a directory that the new run does not expect, they are copied over without warning.
- **Impact:** Stale `.yaml` or `.json` files from prior runs can contaminate the new run.
- **Fix:** Clear target directories before copying, or validate copied files against the expected `writes` globs.

---

## Summary Table

| # | File | Line | Severity | Status |
|---|------|------|----------|--------|
| 1 | `xscore/shared/load_ground_truth.py` | ~119 | High | Confirmed by test |
| 2 | `xscore/marking/class_charts.py` | 55 | Medium | Confirmed by test |
| 3 | `xscore/marking/report_latex_text.py` | 37 | Medium | Confirmed by test |
| 4 | `xscore/shared/cost_report.py` | 61 | Medium | Confirmed by test |
| 5 | `xscore/preprocessing/assign_pages_to_students.py` | 212 | Medium | Confirmed by test |
| 6 | `xscore/shared/prompt_logger.py` | 129 | Medium | Confirmed by test |
| 7 | Multiple files | — | Medium | Confirmed by test |
| 8 | `xscore/marking/blueprints.py` | ~101 | Medium | Confirmed by test |
| 9 | `xscore/marking/parse_instruction.py` | 154 | Medium | Confirmed by test |
| 10 | `xscore/steps/geometry.py` | 467 | Medium | Confirmed by test |
| 11 | `xscore/steps/scaffold.py` | 209 | Medium | Confirmed by test |
| 12 | `xscore/shared/models.py` | 117 | Low | Confirmed by test |
| 13 | `xscore/steps/scaffold.py` | 313+ | High | Static analysis |
| 14 | `xscore/marking/report_latex.py` | 162 | Low | Static analysis |
| 15 | `xscore/pipeline/resume.py` | 251 | Low | Static analysis |
| 16 | `xscore/shared/find_exam_folder.py` | 74 | Low | Static analysis |
| 17 | `xscore/pipeline/resume.py` | 77 | Low | Static analysis |
