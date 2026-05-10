"""Step 25 — transcribe_scheme_graphics: per-graphic textual descriptions.

For each PNG that step 22 (``detect_mark_scheme_graphics``) extracted from the
mark scheme, run one vision call that — given the question text and parsed
mark-scheme answer text — emits a short, faithful textual description of the
graphic. Step 29 (``ai_marking``) then attaches the description alongside the
image so the marker has both modalities.

Always-on. Skips no work on missing env var; only short-circuits when there are
no graphics to transcribe. Per-graphic call failures are isolated — the entry
gets ``transcription: ""`` and the rest of the run continues.
"""

from __future__ import annotations

import base64
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml as _yaml

from eXercise.ai_client import (
    build_completion_kwargs,
    collect_streamed_response,
    make_ai_client,
)
from eXercise.api_retry import retry_api_call
from xscore.prompts.loader import load_prompt
from xscore.shared.exam_paths import (
    TRANSCRIBE_SCHEME_GRAPHICS_DIR,
    artifact_mark_scheme_graphics_dir,
    artifact_scheme_graphic_transcriptions_path,
)
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import info_line, warn_line


_DEFAULT_MODEL = "qwen3.6-plus, 0, 8192"
_GFX_FILENAME_RE = re.compile(r"^(\d+)_(.+)_(\d+)\.png$")


# ---------------------------------------------------------------------------
# YAML dumper — block-scalar (|) for transcription strings with newlines or
# backslashes (so LaTeX survives), plain scalars otherwise. Mirrors the
# _ExtractAnswersDumper pattern in xscore.marking.extract_answers.
# ---------------------------------------------------------------------------

class _TranscribeDumper(_yaml.SafeDumper):
    pass


def _str_representer(dumper: _yaml.Dumper, data: str) -> _yaml.ScalarNode:
    if "\n" in data or "\\" in data:
        data = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_TranscribeDumper.add_representer(str, _str_representer)


# ---------------------------------------------------------------------------
# Per-graphic AI call
# ---------------------------------------------------------------------------

def _transcribe_one(
    client: Any,
    model_id: str,
    png_path: Path,
    qnum: str,
    question_text: str,
    correct_answer: str,
    mark_scheme_text: str,
    thinking_kw: dict,
    use_stream: bool,
    prompt_save_path: Path | None,
    request_timeout: "Any | None" = None,
) -> tuple[str, str]:
    """Single AI call: describe one mark-scheme graphic.

    Returns ``(transcription, problem)``. ``problem`` is the QA flag from the
    v2 prompt (audit item [72]) — empty string when nothing was flagged or the
    response was the legacy v1 plain-bullet shape.
    """
    _, system_prompt = load_prompt("transcribe_scheme_graphic", section="system")
    _, user_text = load_prompt(
        "transcribe_scheme_graphic", section="user",
        question_number=qnum,
        question_text=question_text,
        correct_answer=correct_answer,
        mark_scheme_text=mark_scheme_text,
    )

    b64 = base64.b64encode(png_path.read_bytes()).decode()
    # Image first, text after — system → image → user-text per audit item [5].
    user_content: list[dict] = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": user_text},
    ]

    kwargs: dict[str, Any] = dict(
        model=model_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    kwargs.update(thinking_kw)
    if request_timeout is not None:
        kwargs["timeout"] = request_timeout

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])

    def _do_call() -> tuple[str, str]:
        if use_stream:
            _th: list[str] = []
            stream = client.chat.completions.create(**kwargs, stream=True)
            return collect_streamed_response(stream, thinking_out=_th), "".join(_th)
        resp = client.chat.completions.create(**kwargs)
        return (
            resp.choices[0].message.content or "",
            getattr(resp.choices[0].message, "reasoning_content", "") or "",
        )

    raw, thinking_text = retry_api_call(_do_call, label=f"Transcribe ({qnum})")
    save_response(prompt_save_path, raw, thinking=thinking_text)
    from xscore.shared.response_parsing import strip_code_fences
    cleaned = strip_code_fences(raw or "").strip()
    if not cleaned:
        return "", ""
    # Prompt v3 emits {bullets: '|' block scalar containing LaTeX itemize,
    # problem: '' or '|' block scalar}. v2 (legacy) emitted bullets as a
    # YAML list of double-quoted strings — that shape is still accepted but
    # joined with newlines for downstream consumption. v1 (legacy) emitted
    # plain bullet lines with no wrapper. On parse failure or unknown shape,
    # fall back to the raw text and warn so the silent-degradation path
    # that hid the run-2026-05-10_20-46-57 transcribe_5b_1 YAML break is
    # visible next time.
    try:
        parsed = _yaml.safe_load(cleaned)
    except _yaml.YAMLError as exc:
        warn_line(
            f"Transcribe {qnum}: YAML parse failed — falling back to raw text "
            f"(degraded transcription). Error: {str(exc).splitlines()[0][:100]}"
        )
        return cleaned, ""
    if isinstance(parsed, dict):
        _b = parsed.get("bullets")
        if isinstance(_b, str):
            bullets_text = _b.strip()
        elif isinstance(_b, list):
            bullets_text = "\n".join(
                f"- {str(b).strip()}" for b in _b if str(b).strip()
            )
        else:
            bullets_text = ""
        problem = str(parsed.get("problem") or "").strip()
        if problem:
            warn_line(f"Transcribe {qnum}: problem flagged — {problem}")
        if bullets_text:
            return bullets_text, problem
    return cleaned, ""


# ---------------------------------------------------------------------------
# Phase orchestration
# ---------------------------------------------------------------------------

def _format_mark_scheme(ms: Any) -> str:
    """Render a list of ``{mark, criterion}`` dicts as readable plain text.

    The mark-scheme parser (step 24) produces structured criteria. Plain
    ``str(list_of_dicts)`` gives Python's ``repr()``: single-quote dict keys,
    doubled backslashes, literal ``\\n`` — the model has to mentally unescape.
    This helper emits the same content as a clean ``[N marks] criterion``
    block per entry, which the transcribe-graphic prompt's ``$mark_scheme_text``
    placeholder consumes verbatim.
    """
    if isinstance(ms, str):
        return ms  # pre-formatted (legacy callers)
    if not ms:
        return ""
    parts: list[str] = []
    for entry in ms:
        if not isinstance(entry, dict):
            continue
        mark = entry.get("mark", "")
        criterion = (entry.get("criterion") or "").rstrip()
        suffix = "marks" if str(mark) != "1" else "mark"
        parts.append(f"[{mark} {suffix}] {criterion}")
    return "\n\n".join(parts)


def _scheme_question_lookup(scheme_data: Any, raw_questions: Any) -> dict[str, dict[str, str]]:
    """``{safe_qnum: {question_text, correct_answer, mark_scheme}}``.

    safe_qnum is the filename-safe form (``re.sub(r"[^\\w]", "_", number)``)
    so a graphic file ``1_2_b__i_1.png`` matches its question record. Mark
    scheme fields fall back to empty strings when the mark scheme didn't
    name the question; question_text falls back to ``raw_questions``.
    """
    def _safe(num: str) -> str:
        return re.sub(r"[^\w]", "_", num)

    out: dict[str, dict[str, str]] = {}
    raw_text_by_safe: dict[str, str] = {}
    if isinstance(raw_questions, list):
        for q in raw_questions:
            if not isinstance(q, dict):
                continue
            num = str(q.get("number", "")).strip()
            if not num:
                continue
            raw_text_by_safe[_safe(num)] = str(q.get("question_text") or q.get("text") or "")

    if isinstance(scheme_data, dict):
        for q in scheme_data.get("questions", []) or []:
            if not isinstance(q, dict):
                continue
            num = str(q.get("number", "")).strip()
            if not num:
                continue
            safe = _safe(num)
            # New (post-refactor) shape: non-MCQ has `mark_scheme_answer` (one
            # block); MCQ has `correct_answer` (letter) + `explanation`.
            # Legacy shape: `correct_answer` + `mark_scheme: [{mark, criterion}]`.
            ms_block = q.get("mark_scheme_answer")
            if ms_block:
                ms_text = str(ms_block)
                ca_text = ""
            else:
                ms_text = _format_mark_scheme(q.get("mark_scheme") or q.get("explanation") or [])
                ca_text = str(q.get("correct_answer") or "")
            out[safe] = {
                "question_text": str(q.get("question_text") or raw_text_by_safe.get(safe) or ""),
                "correct_answer": ca_text,
                "mark_scheme": ms_text,
                "human_qnum": num,
            }

    # Graphics may target a question that isn't in the mark scheme YAML
    # (e.g. parent question for a sub-part). Backfill from raw_questions.
    for safe, qtext in raw_text_by_safe.items():
        if safe not in out:
            out[safe] = {
                "question_text": qtext,
                "correct_answer": "",
                "mark_scheme": "",
                "human_qnum": safe.replace("_", ""),
            }
    return out


def _load_existing(transcriptions_path: Path) -> dict[str, dict]:
    """Read prior transcriptions.yaml entries keyed by filename. Empty on miss."""
    if not transcriptions_path.exists():
        return {}
    try:
        doc = _yaml.safe_load(transcriptions_path.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError:
        return {}
    out: dict[str, dict] = {}
    for entry in doc.get("graphics", []) or []:
        if not isinstance(entry, dict):
            continue
        fname = str(entry.get("file") or "")
        if fname:
            out[fname] = entry
    return out


def transcribe_scheme_graphics_phase(
    raw_questions: Any,
    scheme_data: Any,
    artifact_dir: Path,
    *,
    should_cache: bool = False,
) -> tuple[int, int]:
    """Transcribe every PNG in ``22_detect_mark_scheme_graphics/graphics/``.

    Returns ``(new_count, total_count)``. ``new_count`` excludes entries
    already present from a prior partial run (resume-safe re-use); on a fresh
    run it equals ``total_count``. ``total_count`` is the number of PNGs
    detected, regardless of cache status.
    """
    graphics_dir = artifact_mark_scheme_graphics_dir(artifact_dir)
    out_path = artifact_scheme_graphic_transcriptions_path(artifact_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pngs = sorted(graphics_dir.glob("*.png")) if graphics_dir.is_dir() else []
    if not pngs:
        out_path.write_text(
            _yaml.dump(
                {"graphics": []}, Dumper=_TranscribeDumper,
                allow_unicode=True, default_flow_style=False, sort_keys=False,
            ),
            encoding="utf-8",
        )
        return 0, 0

    qmap = _scheme_question_lookup(scheme_data, raw_questions)
    existing = _load_existing(out_path)

    result = make_ai_client(
        model_env="TRANSCRIBE_SCHEME_GRAPHIC_MODEL",
        default_model=_DEFAULT_MODEL,
        should_cache=should_cache,
    )
    if result is None:
        raise RuntimeError(
            "TRANSCRIBE_SCHEME_GRAPHIC_MODEL client could not be created — "
            "check DASHSCOPE_API_KEY / GEMINI_API_KEY in .env"
        )
    client, model_id, provider, thinking, max_tok = result
    use_stream, thinking_kw = build_completion_kwargs(provider, thinking, max_tok)
    from eXercise.ai_client import make_request_timeout  # noqa: PLC0415
    request_timeout = make_request_timeout("long")

    info_line(f"Transcribing {len(pngs)} graphic(s) ({model_id}) …")

    try:
        workers = int(os.environ.get("TRANSCRIBE_SCHEME_GRAPHIC_WORKERS", "500"))
    except ValueError:
        workers = 500

    # Build per-PNG work items. PNGs whose filename doesn't match the
    # NN_safeqnum_idx.png shape are still transcribed, but with empty
    # question/answer context.
    tasks: list[dict[str, Any]] = []
    for png in pngs:
        m = _GFX_FILENAME_RE.match(png.name)
        if m:
            ms_page = int(m.group(1))
            safe = m.group(2)
            gidx = int(m.group(3))
        else:
            ms_page = 0
            safe = png.stem
            gidx = 1
        ctx = qmap.get(safe, {
            "question_text": "", "correct_answer": "", "mark_scheme": "",
            "human_qnum": safe,
        })
        tasks.append({
            "png": png, "ms_page": ms_page, "safe_qnum": safe,
            "graphic_index": gidx,
            "human_qnum": ctx["human_qnum"],
            "question_text": ctx["question_text"],
            "correct_answer": ctx["correct_answer"],
            "mark_scheme": ctx["mark_scheme"],
        })

    def _run(task: dict[str, Any]) -> tuple[str, str, str]:
        png_path: Path = task["png"]
        prior = existing.get(png_path.name)
        if prior is not None and (prior.get("transcription") or "").strip():
            return (
                png_path.name,
                str(prior.get("transcription") or ""),
                str(prior.get("problem") or ""),
            )
        prompt_save = (
            artifact_dir / TRANSCRIBE_SCHEME_GRAPHICS_DIR
            / f"transcribe_{task['safe_qnum']}_{task['graphic_index']}_prompt.txt"
        )
        try:
            text, problem = _transcribe_one(
                client, model_id, png_path,
                qnum=task["human_qnum"],
                question_text=task["question_text"],
                correct_answer=task["correct_answer"],
                mark_scheme_text=task["mark_scheme"],
                thinking_kw=thinking_kw,
                use_stream=use_stream,
                prompt_save_path=prompt_save,
                request_timeout=request_timeout,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            warn_line(f"Transcribe {png_path.name}: failed ({exc})")
            text = ""
            problem = ""
        return png_path.name, text, problem

    transcribed: dict[str, str] = {}
    problems: dict[str, str] = {}
    new_count = 0
    if workers <= 1 or len(tasks) == 1:
        for task in tasks:
            fname, text, problem = _run(task)
            transcribed[fname] = text
            problems[fname] = problem
            if existing.get(fname, {}).get("transcription", "") != text and text:
                new_count += 1
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            futures = {pool.submit(_run, t): t for t in tasks}
            for fut in as_completed(futures):
                fname, text, problem = fut.result()
                transcribed[fname] = text
                problems[fname] = problem
                if existing.get(fname, {}).get("transcription", "") != text and text:
                    new_count += 1

    # Re-emit in deterministic filename order so diffs are stable across runs.
    # `problem` is persisted for human review only — downstream steps must not
    # consume it (audit item [72]).
    entries: list[dict[str, Any]] = []
    for task in tasks:
        fname = task["png"].name
        entries.append({
            "question_number": task["human_qnum"],
            "safe_qnum": task["safe_qnum"],
            "graphic_index": task["graphic_index"],
            "file": fname,
            "ms_page": task["ms_page"],
            "transcription": transcribed.get(fname, ""),
            "problem": problems.get(fname, ""),
        })

    out_path.write_text(
        _yaml.dump(
            {"graphics": entries}, Dumper=_TranscribeDumper,
            allow_unicode=True, default_flow_style=False, sort_keys=False,
        ),
        encoding="utf-8",
    )
    return new_count, len(pngs)
