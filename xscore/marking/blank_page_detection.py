"""Backwards-compat shim for the historical monolithic ``blank_page_detection`` module.

The real implementations now live in dedicated step modules:

- :mod:`xscore.marking._blank_page_vision_client` — shared client, image
  extraction, ``BlankCheckStatus``, JPEG-DPI constants.
- :mod:`xscore.marking.empty_exam_page_classifier` — step 14 entry point
  (``classify_empty_exam_pages``) + ``PAGE_TYPE_VOCABULARY``.
- :mod:`xscore.marking.student_handwriting_check` — step 15 entry point
  (``check_student_handwriting``) + the closed-vocab matcher and the
  out-of-order recheck logic.

New code should import directly from those modules. This shim keeps the
historic ``from xscore.marking.blank_page_detection import …`` call sites
working — at the time of the split, only ``xscore/steps/geometry.py`` imports
from here.
"""

from __future__ import annotations

from xscore.marking._blank_page_vision_client import (  # noqa: F401
    HANDWRITING_JPEG_DPI,
    HANDWRITING_JPEG_QUALITY,
    BlankCheckStatus,
)
from xscore.marking.empty_exam_page_classifier import (  # noqa: F401
    PAGE_TYPE_VOCABULARY,
    classify_empty_exam_pages,
)
from xscore.marking.student_handwriting_check import (  # noqa: F401
    check_student_handwriting,
)
