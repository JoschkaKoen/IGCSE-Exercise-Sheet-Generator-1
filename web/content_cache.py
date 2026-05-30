# -*- coding: utf-8 -*-
"""mtime-keyed memoization for authored-content loaders.

The Learn / Code pages read authored markdown on every request (by design, so
edits show up live). Where a loader returns an **immutable** value (a markdown
string), we memoize it keyed on its arguments PLUS the mtimes of the files it
reads. Editing a file changes an mtime → cache miss → fresh read, preserving the
live-authoring workflow while removing the repeated IO on hot pages.

Deliberately NOT used on loaders that return dict/list structures the routes
mutate in place (e.g. ``load_topics`` — ``topics_page`` injects ``content_html``;
``load_paper`` — ``extracted_paper`` calls ``attach_matches``): caching a shared
mutable object would leak state across requests. Those parse one file per page
(cheap); the expensive work is the markdown render, which IS cached (immutable
string out).
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Callable, Iterable


def _stamp(paths: tuple[str, ...]) -> tuple:
    out = []
    for p in paths:
        try:
            out.append(os.stat(p).st_mtime_ns)
        except OSError:
            out.append(None)  # missing now; a later create/delete flips this → miss
    return tuple(out)


def mtime_cached(paths_for: Callable[..., Iterable]):
    """Memoize ``fn`` keyed on its args + the mtimes of the files it reads.

    ``paths_for(*args, **kwargs)`` returns the file paths the call depends on; a
    change to any of them (including create/delete) invalidates the entry. Only
    apply to functions returning immutable values. Caching never changes
    behaviour: if ``paths_for`` raises, the call runs uncached.
    """

    def decorate(fn):
        cache: dict = {}

        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                paths = tuple(str(p) for p in paths_for(*args, **kwargs))
            except Exception:
                return fn(*args, **kwargs)
            key = (args, tuple(sorted(kwargs.items())))
            stamp = _stamp(paths)
            hit = cache.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1]
            value = fn(*args, **kwargs)
            cache[key] = (stamp, value)
            return value

        wrapper.cache_clear = cache.clear  # for flush_cache / tests
        return wrapper

    return decorate
