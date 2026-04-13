# -*- coding: utf-8 -*-
"""Smoke tests for pipeline helper functions — pure functions, no I/O or LLM."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import only the pure helper functions — not the full pipeline module
# (which imports fitz and other heavy dependencies at module level).
import importlib.util, types

# Stub the eXercise sub-modules that pipeline.py imports at module level.
# conftest.py has already stubbed fitz and pre-registered the eXercise package.

def _make_stub(name: str, **attrs: object) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]

_make_stub("eXercise.config",
    PAGE_HEADER_BY_EXAM={},
    get_subject_config=lambda *a, **kw: None,
)
_make_stub("eXercise.exceptions",
    ExtractionError=type("ExtractionError", (Exception,), {}),
)
_make_stub("eXercise.labels",
    page_header_label=lambda *a, **kw: "",
    paper_label_from_qp_path=lambda path, **kw: str(path).rsplit("/", 1)[-1].rsplit(".", 1)[0],
    build_exam_header_label=lambda *a, **kw: "",
    build_exam_header_label_from_paths=lambda *a, **kw: "",
    exam_label_from_filename=lambda *a, **kw: "",
)
_make_stub("eXercise.mark_scheme",
    detect_ms_type=lambda *a, **kw: None,
    find_ms_answer_regions=lambda *a, **kw: [],
    parse_mcq_answers=lambda *a, **kw: {},
)
_make_stub("eXercise.mcq_explanations",
    McqPaperData=object,
    batch_generate_mcq_explanations=lambda *a, **kw: [],
    finalize_mcq_explanation_strips=lambda *a, **kw: [],
    generate_mcq_explanation_strips=lambda *a, **kw: [],
    prepare_mcq_job_data=lambda *a, **kw: None,
)
_make_stub("eXercise.difficulty_ranking",
    generate_difficulty_ranking=lambda *a, **kw: None,
)
_make_stub("eXercise.pdfjam_post",
    run_exercise_sheet_pdfjam_variants=lambda *a, **kw: (None, None),
)
_make_stub("eXercise.questions",
    find_question_positions=lambda *a, **kw: [],
    get_question_regions=lambda *a, **kw: [],
)

class _GapStrip:
    def __init__(self, height_pt: float = 0.0) -> None:
        self.height_pt = height_pt

_make_stub("eXercise.rendering",
    GapStrip=_GapStrip,
    Strip=object,
    VectorStrip=object,
    collect_vector_strips=lambda *a, **kw: [],
    create_mcq_answer_strips=lambda *a, **kw: [],
    layout_vector_strips_to_pdf=lambda *a, **kw: ([], []),
)

from eXercise.pipeline import (
    build_exercise_overview,
    merge_answer_anchors_into_overview,
    merge_mcq_flags_into_overview,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _anchor(q: int, page: int, y_pt: float, paper: str | None = None) -> dict:
    a: dict = {"q": q, "page": page, "y_pt": y_pt}
    if paper is not None:
        a["paper"] = paper
    return a


# ---------------------------------------------------------------------------
# build_exercise_overview
# ---------------------------------------------------------------------------

class TestBuildExerciseOverview:
    def test_empty_anchors(self):
        result = build_exercise_overview([])
        assert result["papers"] == []
        assert result["anchors"] == []

    def test_single_anchor_no_paper(self):
        anchors = [_anchor(1, 0, 10.0)]
        result = build_exercise_overview(anchors)
        assert len(result["papers"]) == 1
        assert result["papers"][0]["label"] == ""
        assert result["papers"][0]["exercises"][0]["q"] == 1

    def test_single_paper_multiple_questions(self):
        anchors = [
            _anchor(1, 0, 10.0, "P1"),
            _anchor(2, 0, 50.0, "P1"),
            _anchor(3, 1, 20.0, "P1"),
        ]
        result = build_exercise_overview(anchors)
        assert len(result["papers"]) == 1
        assert result["papers"][0]["label"] == "P1"
        assert len(result["papers"][0]["exercises"]) == 3

    def test_two_papers(self):
        anchors = [
            _anchor(1, 0, 10.0, "P1"),
            _anchor(2, 0, 50.0, "P1"),
            _anchor(1, 0, 10.0, "P2"),
        ]
        result = build_exercise_overview(anchors)
        assert len(result["papers"]) == 2
        assert result["papers"][0]["label"] == "P1"
        assert result["papers"][1]["label"] == "P2"

    def test_exercise_fields(self):
        anchors = [_anchor(5, 2, 77.5, "P1")]
        ex = build_exercise_overview(anchors)["papers"][0]["exercises"][0]
        assert ex["q"] == 5
        assert ex["page"] == 2
        assert ex["y_pt"] == 77.5

    def test_y_view_pt_included_when_present(self):
        a = _anchor(1, 0, 10.0)
        a["y_view_pt"] = 15.0
        result = build_exercise_overview([a])
        ex = result["papers"][0]["exercises"][0]
        assert ex["y_view_pt"] == 15.0

    def test_y_view_pt_omitted_when_absent(self):
        result = build_exercise_overview([_anchor(1, 0, 10.0)])
        ex = result["papers"][0]["exercises"][0]
        assert "y_view_pt" not in ex

    def test_anchors_preserved_in_result(self):
        anchors = [_anchor(1, 0, 10.0, "P1"), _anchor(2, 0, 20.0, "P1")]
        result = build_exercise_overview(anchors)
        assert result["anchors"] is anchors

    def test_paper_boundary_detected_by_label_change(self):
        anchors = [
            _anchor(1, 0, 10.0, "P1"),
            _anchor(2, 0, 20.0, "P2"),
            _anchor(3, 0, 30.0, "P1"),  # P1 again → new paper group
        ]
        result = build_exercise_overview(anchors)
        assert len(result["papers"]) == 3


# ---------------------------------------------------------------------------
# merge_mcq_flags_into_overview
# ---------------------------------------------------------------------------

class TestMergeMcqFlagsIntoOverview:
    def _make_overview(self, *qs_by_paper: tuple[str, list[int]]) -> dict:
        papers = []
        all_anchors = []
        for label, qs in qs_by_paper:
            exercises = [{"q": q, "page": 0, "y_pt": float(q * 10)} for q in qs]
            papers.append({"label": label, "exercises": exercises})
            for ex in exercises:
                a = dict(ex)
                a["paper"] = label if label else None
                all_anchors.append(a)
        return {"papers": papers, "anchors": all_anchors}

    def _fake_jobs(self, *qp_names: str) -> list[dict]:
        """Build minimal job dicts with 'input_pdf' and 'questions' keys."""
        jobs = []
        for name in qp_names:
            # paper_label_from_qp_path strips path and extension, so use a simple name
            jobs.append({"input_pdf": f"/exams/{name}.pdf", "questions": [1, 2, 3]})
        return jobs

    def test_no_mcq_jobs_all_false(self):
        ov = self._make_overview(("", [1, 2, 3]))
        jobs = [{"input_pdf": "/exams/p1.pdf", "questions": [1, 2, 3]}]
        job_mcq_ms = [False]
        merge_mcq_flags_into_overview(ov, jobs, job_mcq_ms, use_paper_sublabels=False)
        for ex in ov["papers"][0]["exercises"]:
            assert ex["mcq"] is False

    def test_mcq_job_marks_exercises(self):
        ov = self._make_overview(("", [1, 2]))
        jobs = [{"input_pdf": "/exams/p1.pdf", "questions": [1, 2]}]
        job_mcq_ms = [True]
        merge_mcq_flags_into_overview(ov, jobs, job_mcq_ms, use_paper_sublabels=False)
        for ex in ov["papers"][0]["exercises"]:
            assert ex["mcq"] is True

    def test_mcq_flag_set_on_exercises_key(self):
        ov = self._make_overview(("", [5]))
        jobs = [{"input_pdf": "/exams/p1.pdf", "questions": [5]}]
        merge_mcq_flags_into_overview(ov, jobs, [True], use_paper_sublabels=False)
        assert "mcq" in ov["papers"][0]["exercises"][0]

    def test_empty_jobs_no_error(self):
        ov = self._make_overview(("", [1]))
        merge_mcq_flags_into_overview(ov, [], [], use_paper_sublabels=False)

    def test_empty_overview_no_error(self):
        ov = {"papers": [], "anchors": []}
        jobs = [{"input_pdf": "/exams/p1.pdf", "questions": [1]}]
        merge_mcq_flags_into_overview(ov, jobs, [True], use_paper_sublabels=False)


# ---------------------------------------------------------------------------
# merge_answer_anchors_into_overview
# ---------------------------------------------------------------------------

class TestMergeAnswerAnchorsIntoOverview:
    def _make_overview(self, label: str, qs: list[int]) -> dict:
        exercises = [{"q": q, "page": 0, "y_pt": float(q * 10)} for q in qs]
        return {"papers": [{"label": label, "exercises": exercises}], "anchors": []}

    def _answer_anchor(
        self, q: int, page: int, y_pt: float, paper: str | None = None, y_view_pt: float | None = None
    ) -> dict:
        a: dict = {"q": q, "page": page, "y_pt": y_pt}
        if paper is not None:
            a["paper"] = paper
        if y_view_pt is not None:
            a["y_view_pt"] = y_view_pt
        return a

    def test_answer_anchor_merged(self):
        ov = self._make_overview("", [1, 2])
        aa = [self._answer_anchor(1, 3, 100.0), self._answer_anchor(2, 3, 200.0)]
        merge_answer_anchors_into_overview(ov, aa)
        ex1 = ov["papers"][0]["exercises"][0]
        assert ex1["answers_page"] == 3
        assert ex1["answers_y_pt"] == 100.0

    def test_y_view_pt_falls_back_to_y_pt(self):
        ov = self._make_overview("", [1])
        aa = [self._answer_anchor(1, 0, 50.0)]  # no y_view_pt
        merge_answer_anchors_into_overview(ov, aa)
        ex = ov["papers"][0]["exercises"][0]
        assert ex["answers_y_view_pt"] == 50.0

    def test_y_view_pt_used_when_present(self):
        ov = self._make_overview("", [1])
        aa = [self._answer_anchor(1, 0, 50.0, y_view_pt=60.0)]
        merge_answer_anchors_into_overview(ov, aa)
        ex = ov["papers"][0]["exercises"][0]
        assert ex["answers_y_view_pt"] == 60.0

    def test_no_anchor_for_question_leaves_no_key(self):
        ov = self._make_overview("", [1, 2])
        aa = [self._answer_anchor(1, 0, 10.0)]
        merge_answer_anchors_into_overview(ov, aa)
        ex2 = ov["papers"][0]["exercises"][1]
        assert "answers_page" not in ex2

    def test_empty_answer_anchors_no_change(self):
        ov = self._make_overview("", [1])
        merge_answer_anchors_into_overview(ov, [])
        ex = ov["papers"][0]["exercises"][0]
        assert "answers_page" not in ex

    def test_empty_overview_no_error(self):
        ov = {"papers": [], "anchors": []}
        merge_answer_anchors_into_overview(ov, [{"q": 1, "page": 0, "y_pt": 0.0}])

    def test_paper_label_none_treated_as_empty_string(self):
        # An anchor with paper=None should match an exercise with label=""
        ov = self._make_overview("", [1])
        aa = [{"q": 1, "page": 2, "y_pt": 30.0, "paper": None}]
        merge_answer_anchors_into_overview(ov, aa)
        ex = ov["papers"][0]["exercises"][0]
        assert ex["answers_page"] == 2

    def test_multiple_papers_matched_independently(self):
        ov = {
            "papers": [
                {"label": "P1", "exercises": [{"q": 1, "page": 0, "y_pt": 10.0}]},
                {"label": "P2", "exercises": [{"q": 1, "page": 0, "y_pt": 10.0}]},
            ],
            "anchors": [],
        }
        aa = [
            self._answer_anchor(1, 5, 50.0, paper="P1"),
            self._answer_anchor(1, 9, 90.0, paper="P2"),
        ]
        merge_answer_anchors_into_overview(ov, aa)
        assert ov["papers"][0]["exercises"][0]["answers_page"] == 5
        assert ov["papers"][1]["exercises"][0]["answers_page"] == 9
