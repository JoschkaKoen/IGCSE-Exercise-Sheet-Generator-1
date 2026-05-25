"""eXam — on-screen exam practice with AI marking and pregenerated helpers.

Peer of ``eXercise/`` (sheet generation) and ``xscore/`` (paper-scan grading).
Reuses primitives from both as a library.

Public submodules (consumed by ``web/routes/eXam_*``):

- :mod:`eXam.bank` — pre-index a paper + mark scheme (caches structured YAML).
- :mod:`eXam.db` — SQLite schema and CRUD for students, tests, attempts, sessions.
- :mod:`eXam.auth` — cookie-based teacher/student session management.
- :mod:`eXam.marker` — three marking paths (MCQ / numeric / free-response AI).
- :mod:`eXam.runtime` — per-student question randomisation and metadata.
- :mod:`eXam.open_mode` — anonymous public practice mode.
- :mod:`eXam.test_builder` — teacher-facing test assembly UI helpers.
- :mod:`eXam.cost_tracker` — AI cost recording per test.
- :mod:`eXam.users` — student/teacher account management.
- :mod:`eXam.roster` — roster import, PIN PDFs, canonical name handling.
- :mod:`eXam.render_helper` — question PDF rendering helpers.
- :mod:`eXam.xscore_adapter` — single entry point for all xscore.* imports
  (lazy: defer xscore's heavy deps until the pre-indexer runs).

Internal submodules (``flush_cache``, ``pregenerate``, ``results_export``,
``warm_bank``) are admin utilities and warm-up scripts.
"""

__all__ = (
    "bank",
    "db",
    "auth",
    "marker",
    "runtime",
    "open_mode",
    "test_builder",
    "cost_tracker",
    "users",
    "roster",
    "render_helper",
    "xscore_adapter",
)
