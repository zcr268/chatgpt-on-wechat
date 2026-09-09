# encoding:utf-8
"""Guard the console's asset wiring.

The frontend is plain classic scripts with no bundler, so nothing fails at
build time: a script that is on disk but missing from the page just silently
stops existing, and one listed in the wrong place breaks at load. These are
the invariants the split relies on.
"""

import os
import re

from channel.web import template

WEB = os.path.join(os.path.dirname(__file__), "..", "channel", "web")
STATIC = os.path.join(WEB, "static")


def _page():
    return template.render("chat.html")


def _scripts(page):
    return re.findall(r'<script defer src="assets/(js/[^"?]+)"', page)


def test_every_console_script_is_listed_exactly_once_and_exists():
    scripts = _scripts(_page())

    duplicated = sorted({s for s in scripts if scripts.count(s) > 1})
    assert not duplicated, duplicated

    absent = [s for s in scripts if not os.path.exists(os.path.join(STATIC, s))]
    assert not absent, absent

    on_disk = set()
    for root, _, files in os.walk(os.path.join(STATIC, "js")):
        for name in files:
            if name.endswith(".js"):
                rel = os.path.relpath(os.path.join(root, name), STATIC)
                on_disk.add(rel.replace(os.sep, "/"))

    # A file nobody loads is dead weight that still looks live in a search.
    assert not sorted(on_disk - set(scripts))


def test_every_stylesheet_is_listed_and_exists():
    sheets = re.findall(r'<link rel="stylesheet" href="assets/(css/[^"?]+)"', _page())
    absent = [s for s in sheets if not os.path.exists(os.path.join(STATIC, s))]
    assert not absent, absent
    assert sheets == sorted(set(sheets), key=sheets.index), "a sheet is linked twice"


def test_boot_runs_last_among_the_console_scripts_but_before_workspace():
    """boot.js is the only script with top-level work that calls into the rest,
    so everything it touches has to be declared by the time it runs.

    It stays ahead of workspace.js, which is where console.js used to sit:
    applyI18n() probes for relocalizeWorkspacePanel behind a typeof guard, and
    has always run before workspace.js defined that function.
    """
    scripts = _scripts(_page())

    boot = scripts.index("js/boot.js")
    split_out = [i for i, s in enumerate(scripts)
                 if s.startswith(("js/core/", "js/chat/", "js/views/"))]
    assert boot > max(split_out)
    assert boot < scripts.index("js/workspace.js")


def test_shared_layers_load_before_the_views_that_call_them():
    scripts = _scripts(_page())
    last_core = max(i for i, s in enumerate(scripts) if s.startswith("js/core/"))
    first_view = min(i for i, s in enumerate(scripts) if s.startswith("js/views/"))
    assert last_core < first_view

    # views/doc-viewers.js calls createDocEditor() at top level.
    assert scripts.index("js/doc-editor.js") < scripts.index("js/views/doc-viewers.js")


def test_the_split_scripts_do_not_declare_the_same_global_twice():
    """Every top-level declaration lands on `window`, which is what the inline
    onclick handlers in generated markup reach. Two scripts declaring the same
    const is a SyntaxError that blanks the page."""
    pattern = re.compile(r"^(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)", re.M)

    owners = {}
    clashes = []
    for script in _scripts(_page()):
        if not script.startswith(("js/core/", "js/chat/", "js/views/", "js/boot")):
            continue
        with open(os.path.join(STATIC, script), encoding="utf-8") as f:
            for name in pattern.findall(f.read()):
                first = owners.setdefault(name, script)
                if first != script:
                    clashes.append(f"{name}: {first} and {script}")

    assert not clashes, clashes
    # Guard the regex itself: if it stopped matching, the test above would pass
    # for the wrong reason.
    assert len(owners) > 600, len(owners)
