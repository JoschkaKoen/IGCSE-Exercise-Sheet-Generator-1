# -*- coding: utf-8 -*-
"""Data layer for ``web.handouts``: question grouping, syllabus loading, meta I/O.

Pulled out of ``web/handouts.py`` to keep individual files under the
500-line project guideline. The orchestration / CLI half lives there.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from xscore.shared.qnum_utils import norm_qnum

from . import extracted_questions
from .content_cache import mtime_cached
from .subtopic_matcher import iter_leaves
from .syllabus_content import load_content
from .syllabus_topics import load_topics

# ── Constants ────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDOUTS_ROOT = REPO_ROOT / "output" / "eXam" / "handouts"
CONTENT_ROOT = REPO_ROOT / "syllabi" / "content"

# Subjects in scope for this PR (per the plan).
TARGET_SUBJECTS = ("a_level_physics", "a_level_computer_science")

_NUM_CHUNK_RE = re.compile(r"(\d+)")
_CE_PREFIX_RE = re.compile(r"^[CE](?=\d)")


def _topic_code(code: str) -> str:
    """Normalise a subtopic code for topic-membership tests.

    IGCSE Mathematics prefixes its subtopic codes with ``C``/``E`` (Core /
    Extended) — e.g. ``C1.5``, ``E1.5`` — but both belong to top-level topic
    ``1``. Every other subject uses bare numeric codes (``1.5``, ``1.5.1``,
    or a bare ``7``), which pass through unchanged.
    """
    return _CE_PREFIX_RE.sub("", str(code).strip())


# ── QuestionEntry ────────────────────────────────────────────────────────


@dataclass
class QuestionEntry:
    paper_stem: str
    qnum_leaf: str
    parent_text: str
    text: str
    marks: int | None
    question_type: str
    answer_options: list[dict[str, str]] = field(default_factory=list)

    def format_for_prompt(self) -> str:
        marks_str = f"[{self.marks} marks]" if isinstance(self.marks, int) else "[? marks]"
        qtype = self.question_type or "?"
        header = f'Q{self.qnum_leaf} (from "{self.paper_stem}"), {marks_str}, {qtype}'
        lines = [header, ""]
        if self.parent_text.strip():
            lines.append("Parent context:")
            lines.append(self.parent_text.strip())
            lines.append("")
        lines.append("Question:")
        lines.append(self.text.strip() or "(no text extracted)")
        if self.answer_options:
            lines.append("")
            for opt in self.answer_options:
                letter = str(opt.get("letter") or "").strip()
                opt_text = str(opt.get("text") or "").strip()
                lines.append(f"{letter}. {opt_text}")
        return "\n".join(lines)

    @property
    def key(self) -> tuple[str, str]:
        return (self.paper_stem, self.qnum_leaf)


def format_question_chunk(entries: list[QuestionEntry]) -> str:
    """Render one or more QuestionEntry into the prompt's ``$question_block`` / ``$all_questions_block`` slot."""
    return "\n\n---\n\n".join(e.format_for_prompt() for e in entries)


# ── Sorting / topic lookup ───────────────────────────────────────────────


def _natural_qnum_key(qnum: str) -> tuple:
    """Sort key for question numbers like ``"1"``, ``"2a"``, ``"2bi"``, ``"10"``.

    Splits into alternating numeric/non-numeric segments so ``"2a"`` < ``"10"``
    and ``"2a"`` < ``"2b"``.
    """
    parts = []
    for chunk in _NUM_CHUNK_RE.split(qnum):
        if not chunk:
            continue
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk))
    return tuple(parts)


def topic_for_number(subject_key: str, topic_number: str) -> dict[str, Any] | None:
    """Look up the topic dict (``{number, title, subtopics}``) for *topic_number*."""
    data = load_topics(subject_key)
    if data is None:
        return None
    target = str(topic_number)
    for t in data.get("topics") or []:
        if str(t.get("number")) == target:
            return t
    return None


def enumerate_topics(subject_key: str) -> list[str]:
    """Return every top-level topic number for *subject_key* (or [] when topics.yaml absent)."""
    data = load_topics(subject_key)
    if data is None:
        return []
    return [
        str(t.get("number"))
        for t in (data.get("topics") or [])
        if t.get("number") is not None
    ]


# ── Syllabus content loader ──────────────────────────────────────────────


def load_syllabus_content_for_topic(subject_key: str, topic: dict[str, Any]) -> str:
    """Concatenate every subtopic's markdown content for the given topic.

    Falls back to glob over ``<subject>/<N>*.md`` when ``subtopics: []``
    (IGCSE-CS-style topic-only entries — kept defensive even though the two
    target subjects of this PR don't trigger it).
    """
    subs = topic.get("subtopics") or []
    parts: list[str] = []
    if subs:
        for s in subs:
            num = str(s.get("number") or "").strip()
            if not num:
                continue
            md = load_content(subject_key, num)
            if md:
                parts.append(md.strip())
    else:
        topic_num = str(topic.get("number") or "").strip()
        if not topic_num:
            raise FileNotFoundError(
                f"topic {topic.get('title')} for {subject_key} has no number — cannot glob content"
            )
        subj_dir = CONTENT_ROOT / subject_key
        if subj_dir.is_dir():
            candidates = sorted(subj_dir.glob(f"{topic_num}.md")) + sorted(
                subj_dir.glob(f"{topic_num}.*.md")
            )
            for p in candidates:
                try:
                    parts.append(p.read_text(encoding="utf-8").strip())
                except OSError:
                    continue
    if not parts:
        raise FileNotFoundError(
            f"no syllabus content found for {subject_key} topic {topic.get('number')}"
        )
    return "\n\n---\n\n".join(parts)


# ── Question collection ──────────────────────────────────────────────────


def collect_questions_for_topic(
    subject_key: str, topic_number: int | str
) -> list[QuestionEntry]:
    """Walk the bank, gather every leaf question whose matched codes fall under *topic_number*."""
    target = str(topic_number)
    prefix = f"{target}."

    out: list[QuestionEntry] = []
    papers = extracted_questions.list_papers(subject_key)
    for paper_stem in papers:
        matches = extracted_questions.load_subtopic_matches(subject_key, paper_stem)
        if not matches:
            continue
        # Keys: norm_qnum'd; values: list of codes.
        keys_in_topic = {
            k for k, codes in matches.items()
            if any(
                (tc := _topic_code(c)) == target or tc.startswith(prefix)
                for c in codes
            )
        }
        if not keys_in_topic:
            continue
        paper = extracted_questions.load_paper(subject_key, paper_stem)
        if paper is None:
            continue
        questions = paper.get("questions") or []
        for leaf_q, parent_text in iter_leaves(questions):
            qnum_raw = str(leaf_q.get("number") or "")
            key = norm_qnum(qnum_raw)
            if key not in keys_in_topic:
                continue
            text = (leaf_q.get("text") or "").strip()
            if text == "STUB ERROR":
                continue
            marks_v = leaf_q.get("marks")
            marks = int(marks_v) if isinstance(marks_v, int) else None
            out.append(
                QuestionEntry(
                    paper_stem=paper_stem,
                    qnum_leaf=qnum_raw or key,
                    parent_text=parent_text or "",
                    text=text,
                    marks=marks,
                    question_type=str(leaf_q.get("question_type") or "").strip(),
                    answer_options=list(leaf_q.get("answer_options") or []),
                )
            )

    out.sort(key=lambda q: (q.paper_stem, _natural_qnum_key(q.qnum_leaf)))
    return out


@lru_cache(maxsize=32)
def topic_qids(subject_key: str) -> dict[str, frozenset[str]]:
    """Map each syllabus topic number → the set of **top-level** practice question
    ids (``subject::paper_stem::N``) that touch it.

    Built from the per-paper ``subtopic_matches.yaml`` sidecars alone — no full
    paper parse (lighter than :func:`collect_questions_for_topic`, which returns
    leaf text). Those sidecar keys are leaf-level for structured papers
    (``1a``, ``2ci`` …), so each leaf is rolled up to its top-level integer and a
    top-level question belongs to every topic that is the leading integer of any
    of its leaves' codes — e.g. a question whose parts map to ``4.1`` and ``6.2``
    appears under topics ``4`` and ``6``. MCQ papers are already top-level.

    The picker serves whole top-level questions, so this returns the exact id
    form ``open_mode.pick_random_question`` uses (``subject::stem::qnum``). Some
    ids may name a question with no rendered snippet (matched but unservable);
    the caller intersects with the servable set, so phantoms never get served.
    ``frozenset`` values keep the lru_cached dict safe to share.
    """
    by_topic: dict[str, set[str]] = {}
    for paper_stem in extracted_questions.list_papers(subject_key):
        matches = extracted_questions.load_subtopic_matches(subject_key, paper_stem)
        if not matches:
            continue
        # Roll leaf keys up to their top-level integer, unioning the codes.
        toplevel_codes: dict[str, set[str]] = {}
        for leaf_key, codes in matches.items():
            m = re.match(r"\d+", str(leaf_key))
            if m:
                toplevel_codes.setdefault(m.group(), set()).update(codes)
        for n, codes in toplevel_codes.items():
            qid = f"{subject_key}::{paper_stem}::{n}"
            for code in codes:
                topic_num = _topic_code(code).split(".")[0].strip()
                if topic_num.isdigit():
                    by_topic.setdefault(topic_num, set()).add(qid)
    return {k: frozenset(v) for k, v in by_topic.items()}


# ── Output paths ─────────────────────────────────────────────────────────


def padded_topic(topic_number: str) -> str:
    """``"1"`` → ``"01"``; ``"15"`` → ``"15"``; non-numeric values pass through."""
    try:
        return f"{int(topic_number):02d}"
    except ValueError:
        return str(topic_number).strip()


def handout_dir(subject_key: str) -> Path:
    return HANDOUTS_ROOT / subject_key


def md_path(subject_key: str, topic_number: str) -> Path:
    return handout_dir(subject_key) / f"{padded_topic(topic_number)}.md"


def meta_path(subject_key: str, topic_number: str) -> Path:
    return handout_dir(subject_key) / f"{padded_topic(topic_number)}.meta.yaml"


def logs_dir(subject_key: str, topic_number: str) -> Path:
    return handout_dir(subject_key) / f"{padded_topic(topic_number)}_logs"


def pdf_dir(subject_key: str) -> Path:
    """Per-subject dir holding generated print artifacts (``<NN>.tex`` + ``<NN>.pdf``)."""
    return handout_dir(subject_key) / "pdf"


def tex_path(subject_key: str, topic_number: str) -> Path:
    return pdf_dir(subject_key) / f"{padded_topic(topic_number)}.tex"


def pdf_path(subject_key: str, topic_number: str) -> Path:
    return pdf_dir(subject_key) / f"{padded_topic(topic_number)}.pdf"


def vocab_tex_path(subject_key: str, topic_number: str) -> Path:
    return pdf_dir(subject_key) / f"{padded_topic(topic_number)}.vocab.tex"


def vocab_pdf_path(subject_key: str, topic_number: str) -> Path:
    return pdf_dir(subject_key) / f"{padded_topic(topic_number)}.vocab.pdf"


def glossary_path(subject_key: str, topic_number: str) -> Path:
    """The per-topic vocab TSV lives beside the ``.md`` (subject root), not in ``pdf/``."""
    return handout_dir(subject_key) / f"{padded_topic(topic_number)}.glossary.tsv"


# ── Subject discovery / display names ────────────────────────────────────


def handout_subjects() -> list[str]:
    """Subject keys with at least one authored handout markdown (``<NN>.md``)."""
    if not HANDOUTS_ROOT.is_dir():
        return []
    return sorted(
        d.name
        for d in HANDOUTS_ROOT.iterdir()
        if d.is_dir() and any(d.glob("[0-9][0-9].md"))
    )


def vocab_subjects() -> list[str]:
    """Subject keys with at least one vocab glossary (``<NN>.glossary.tsv``)."""
    if not HANDOUTS_ROOT.is_dir():
        return []
    return sorted(
        d.name
        for d in HANDOUTS_ROOT.iterdir()
        if d.is_dir() and any(d.glob("[0-9][0-9].glossary.tsv"))
    )


def subject_display_name(subject_key: str) -> str:
    """Human label for a subject key, e.g. ``a_level_physics`` → ``A-Level Physics``."""
    from eXercise.config import PAGE_HEADER_BY_EXAM

    return PAGE_HEADER_BY_EXAM.get(subject_key) or subject_key.replace("_", " ").title()


_FILENAME_BAD_RE = re.compile(r"[\\/]+")


def descriptive_pdf_name(
    subject_key: str,
    topic_number: str,
    *,
    kind: str = "handout",
    title: str | None = None,
) -> str:
    """User-facing download filename ``<Subject> <NN> <Topic Title>[ Vocabulary].pdf``.

    On-disk artifacts stay terse (``<NN>.pdf`` / ``<NN>.vocab.pdf``); this is only the
    ``Content-Disposition`` / ``download=`` name. *title* is looked up when not supplied.
    """
    # Normalise a padded "01" (as it arrives from a filename stem) back to the
    # unpadded "1" that topics.yaml keys on, so the title lookup hits.
    n = str(int(topic_number)) if str(topic_number).strip().isdigit() else str(topic_number).strip()
    if title is None:
        topic = topic_for_number(subject_key, n)
        title = (topic or {}).get("title") or f"Topic {n}"
    clean = " ".join(_FILENAME_BAD_RE.sub(" ", str(title)).split())
    suffix = " Vocabulary" if kind == "vocab" else ""
    return f"{subject_display_name(subject_key)} {padded_topic(n)} {clean}{suffix}.pdf"


# ── Meta sidecar I/O ─────────────────────────────────────────────────────


def load_meta(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data or {}


def save_meta(path: Path, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(meta, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    os.replace(tmp, path)


def save_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


@mtime_cached(lambda subject_key, topic_number: [md_path(subject_key, topic_number)])
def load_handout_md(subject_key: str, topic_number: str) -> str | None:
    """Read the topic handout markdown from disk, or None when the file is absent / unreadable."""
    path = md_path(subject_key, topic_number)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def load_glossary(subject_key: str, topic_number: str) -> list[tuple[str, str, str]] | None:
    """Read the per-topic vocab TSV → ``[(english, 中文, pinyin), …]`` (None when absent).

    Skips the ``english⇥简体中文⇥pinyin`` header and any blank / short rows, mirroring
    ``scripts/check_handout_glosses.py:_load_glossary`` (extended to keep the pinyin column).
    """
    path = glossary_path(subject_key, topic_number)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        eng, zh = parts[0].strip(), parts[1].strip()
        if eng.lower() == "english":  # header
            continue
        pinyin = parts[2].strip() if len(parts) > 2 else ""
        rows.append((eng, zh, pinyin))
    return rows


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
