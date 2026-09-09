"""Server-side assembly for the console's HTML shell.

The console page used to be one 2k-line ``chat.html``. It is now a thin shell
that pulls in fragments from ``templates/`` via ``<!--#include path-->``
markers, assembled here before the response is written. Assembly happens on
the server on purpose: the page relies on the Tailwind CDN's JIT compiler,
which is far more predictable when the whole DOM is present at parse time than
when views are injected later by script.

The bytes the browser receives are identical to the pre-split page, so the
split is invisible to the frontend.
"""

import os
import re
from typing import Dict, Tuple

# ``<!--#include templates/views/chat.html-->``. Whitespace around the path is
# tolerated so the markers can be indented to match surrounding markup.
_INCLUDE_RE = re.compile(r'<!--#include\s+([^\s>]+?)\s*-->')

# First-party scripts and stylesheets, which live under assets/js and
# assets/css. Vendored copies sit in assets/vendor and are deliberately left
# alone: they are pinned, so a version query would only waste cache entries.
_ASSET_RE = re.compile(r'assets/((?:js|css)/[A-Za-z0-9_\-./]+\.(?:js|css))')

# An include that resolves back to an ancestor would loop forever. Fragments
# nest at most two deep today (shell -> view -> shared row), so this is a
# generous ceiling that still fails fast on a cycle.
_MAX_INCLUDE_DEPTH = 8

_WEB_DIR = os.path.dirname(os.path.abspath(__file__))

# path -> (mtime, text). Keyed on mtime so an edit is picked up on the next
# request without a restart, while a steady-state page load stays at one stat()
# per fragment instead of a full read.
_cache: Dict[str, Tuple[float, str]] = {}


def _read(rel_path: str) -> str:
    full_path = os.path.normpath(os.path.join(_WEB_DIR, rel_path))
    if not full_path.startswith(_WEB_DIR + os.sep):
        raise ValueError(f"include escapes the web directory: {rel_path}")

    mtime = os.path.getmtime(full_path)
    cached = _cache.get(full_path)
    if cached and cached[0] == mtime:
        return cached[1]

    with open(full_path, 'r', encoding='utf-8') as f:
        text = f.read()
    _cache[full_path] = (mtime, text)
    return text


def _expand(text: str, depth: int) -> str:
    if depth > _MAX_INCLUDE_DEPTH:
        raise ValueError("include nesting too deep; check for a cycle")

    def substitute(match):
        return _expand(_read(match.group(1)), depth + 1)

    return _INCLUDE_RE.sub(substitute, text)


def render(rel_path: str, cache_bust: str = '') -> str:
    """Assemble ``rel_path`` and stamp a version onto its first-party assets.

    ``cache_bust`` guards against a browser running an upgraded console's
    markup against cached copies of the old scripts. Every first-party asset
    is stamped, including ones referenced from included fragments, so adding a
    script no longer means remembering to extend a hardcoded list.
    """
    html = _expand(_read(rel_path), 0)
    if cache_bust:
        html = _ASSET_RE.sub(rf'assets/\1?v={cache_bust}', html)
    return html
