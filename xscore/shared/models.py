"""Data structures for the generic grading pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field

# Result / extraction sentinels (pipeline output and tooling):
#   "?" — unreadable student answer or unknown mark in printed/AI results.
#   "EXTRACTION_ERROR" — extraction path failed (see per-field error details).
#   None — absent optional field (e.g. ground truth not provided).
# Use "" for intentional empty strings where the schema allows; do not overload "?".


@dataclass
class StudentFilter:
    mode: str = "all"           # "all" | "specific" | "first_n"
    names: list[str] = field(default_factory=list)
    n: int = 0

    def __post_init__(self) -> None:
        if self.mode not in ("all", "specific", "first_n"):
            raise ValueError(
                f"StudentFilter.mode must be 'all', 'specific', or 'first_n', got {self.mode!r}"
            )
        if self.mode == "specific" and not self.names:
            raise ValueError("StudentFilter with mode 'specific' requires non-empty names")
        if self.mode == "first_n" and self.n <= 0:
            raise ValueError("StudentFilter with mode 'first_n' requires n > 0")


@dataclass
class TaskInstruction:
    task_type: str              # "count_marks" | "check_mc" | "check_answers"
    student_filter: StudentFilter = field(default_factory=StudentFilter)
    dpi: int = 400
    folder_hint: str | None = None
    # Optional explicit exam folder path (from prompt); lower priority than CLI --folder
    folder_path: str | None = None
    force_clean_scan: bool = False
    no_report: bool = False
    from_step: int | None = None
    # Set to True when the user includes a cache opt-in phrase in the NL prompt
    # (e.g. "grade the exam, reuse cache"). Currently honoured only by step 22
    # (AI marking) — see :mod:`xscore.shared.response_cache`.
    reuse_cache: bool = False


@dataclass
class BBox:
    """Bounding box in PDF points; *page* is 1-based (first page = 1)."""

    x0: float
    y0: float
    x1: float
    y1: float
    page: int


@dataclass
class ExamImage:
    bbox: BBox
    path: str


@dataclass
class WritingArea:
    bbox: BBox
    kind: str  # "box" | "lines"


@dataclass
class McAnswerOption:
    """One row in a multiple-choice stem (Cambridge-style letter on its own line)."""

    letter: str  # "A" … "D"
    text: str


@dataclass
class ExamLayout:
    rows: int = 1  # number of subpage rows per physical scan page
    cols: int = 1  # number of subpage cols per physical scan page


@dataclass
class Question:
    number: str                 # hierarchical label: "9", "9a", "9ai", "9aii"; duplicate mains "38_2"
    question_type: str          # "multiple_choice" | "short_answer" | "calculation" | "long_answer"
    text: str                   # stem only for MC (options in answer_options); full text otherwise
    marks: int
    bbox: BBox                  # primary region (first segment of multi-page questions)
    page: int = 0               # 1-based page in exam PDF; auto-set from bbox.page if not provided
    subpage_row: int = 1        # 1-based row in the layout grid (always 1 for 1×1 exams)
    subpage_col: int = 1        # 1-based col in the layout grid (always 1 for 1×1 exams)
    images: list[ExamImage] = field(default_factory=list)
    equation_blank_bboxes: list[BBox] = field(default_factory=list)  # one per "label = …… [n]" line
    writing_areas: list[WritingArea] = field(default_factory=list)
    subquestions: list[Question] = field(default_factory=list)
    correct_answer: str | None = None
    marking_criteria: str | None = None
    answer_images: list[ExamImage] = field(default_factory=list)
    answer_options: list[McAnswerOption] = field(default_factory=list)  # MC only

    def __post_init__(self) -> None:
        if self.page == 0 and self.bbox.page:
            self.page = self.bbox.page

def flatten_questions(questions: list[Question]) -> list[Question]:
    """Depth-first list of this node and all nested subquestions."""
    out: list[Question] = []
    for q in questions:
        out.append(q)
        out.extend(flatten_questions(q.subquestions))
    return out


def gradable_questions(questions: list[Question]) -> list[Question]:
    """Leaf questions only (parts that carry marks); skips parent nodes that have subquestions."""
    out: list[Question] = []
    for q in questions:
        if q.subquestions:
            out.extend(gradable_questions(q.subquestions))
        else:
            out.append(q)
    return out


@dataclass
class ExamScaffold:
    questions: list[Question]
    total_marks: int
    page_count: int = 0
    raw_description: str = ""
    layout: ExamLayout = field(default_factory=ExamLayout)

    @property
    def gradable_questions(self) -> list[Question]:
        """Leaf parts only — use for summing exam marks and per-part grading."""
        return gradable_questions(self.questions)


@dataclass
class PageAssignment:
    student_name: str
    page_numbers: list[int]
    confidence: str                         # "high" | "medium" | "low"
    cover_page_number: int | None = None    # 1-based scan page of the cover page, or None


@dataclass
class StudentResult:
    student_name: str
    page_numbers: list[int]
    answers: dict[str, str]                 # question_number → student's answer
    marks_per_question: dict[str, float]
    total_marks: float
    max_marks: float
