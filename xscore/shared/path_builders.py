"""Backwards-compat shim for the historical flat ``path_builders`` module.

Path builders now live in :mod:`xscore.shared.paths` (split by pipeline
phase). New code should import from that package directly. This shim keeps
the historic ``from xscore.shared.path_builders import …`` call sites
working.
"""

from xscore.shared.paths import *  # noqa: F401, F403
