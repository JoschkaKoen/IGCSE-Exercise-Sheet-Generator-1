"""Backwards-compat shim re-exporting step folder names and path builders.

Folder-name constants now live in :mod:`xscore.shared.step_folders`; path
builders in :mod:`xscore.shared.path_builders`. New code should import from
those modules directly. This shim keeps the historic
``from xscore.shared.exam_paths import …`` call sites working.
"""

from xscore.shared.path_builders import *  # noqa: F401, F403
from xscore.shared.step_folders import *  # noqa: F401, F403
