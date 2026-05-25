"""Scan preprocessing: raw class scan PDF → ``scanned_exam_merged_and_angles_adjusted.pdf``.

Public submodules (consumed by ``xscore.steps.scan``, ``xscore.steps.geometry``):

- :mod:`xscore.preprocessing.coordinator` — top-level scan-preparation orchestrator.
- :mod:`xscore.preprocessing.assign_pages_to_students` — page→student assignment.
- :mod:`xscore.preprocessing.cover_detection` — first-page cover detection.

Internal submodules (``scan_orientation``, ``scan_orientation_detectors``,
``deskew``, ``merge``, ``trim``, etc.) are implementation details of the
``coordinator``.
"""

__all__ = (
    "coordinator",
    "assign_pages_to_students",
    "cover_detection",
)
