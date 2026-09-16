# encoding:utf-8
"""Guard the console's hash routing.

The address bar is now part of the console's contract: a reload or a shared
link has to land on the view and tab it names. Nothing here fails at build
time, and the failure mode is quiet -- a renamed tab turns an old bookmark
into a dead route, and a tab switcher that stops reporting leaves the URL
pointing somewhere the user is not.
"""

import os
import re

from channel.web import template

WEB = os.path.join(os.path.dirname(__file__), "..", "channel", "web")
STATIC = os.path.join(WEB, "static")


def _js(rel_path):
    with open(os.path.join(STATIC, "js", rel_path), encoding="utf-8") as f:
        return f.read()


def _route_tabs():
    """The tab vocabulary the router accepts, read from its own source."""
    block = re.search(r"const ROUTE_TABS = \{(.*?)\n\};", _js("core/router.js"), re.S)
    assert block, "ROUTE_TABS is no longer where the tests can read it"
    return {view: re.findall(r"'([^']+)'", tabs)
            for view, tabs in re.findall(r"(\w+):\s*\[([^\]]+)\]", block.group(1))}


def test_the_routable_tabs_are_the_tabs_the_page_actually_has():
    """A route names a tab by the id its element carries. Rename the element
    and every link to that tab dies silently -- the router drops the unknown
    name and lands the user on the view's default tab instead."""
    routable = _route_tabs()
    assert routable, "no routable tabs parsed"

    page = template.render("chat.html")
    for view, tabs in routable.items():
        present = set(re.findall(r'id="%s-tab-([a-z]+)"' % view, page))
        assert present == set(tabs), (view, sorted(present), sorted(tabs))


def test_every_routable_tab_switch_reports_itself_to_the_router():
    """The switchers are reached from onclick handlers in the markup, not
    through the router, so each has to say where it went. One that stops
    reporting leaves the address bar naming the tab the user just left."""
    sources = {
        "config": "views/config.js",
        "memory": "views/memory.js",
        "tasks": "views/tasks.js",
        "knowledge": "views/knowledge.js",
    }
    for view, rel_path in sources.items():
        assert view in _route_tabs(), view
        assert "routeNoteTab('%s'" % view in _js(rel_path), rel_path


def test_a_guarded_navigation_keeps_the_tab_it_was_asked_for():
    """navigateTo refuses while a document editor holds unsaved changes and
    retries through a callback once the user discards them. The retry has to
    carry the tab, or a routed navigation that hits the guard quietly lands on
    the view's default tab instead of the one the URL named."""
    nav = _js("core/nav.js")
    assert "docGuardUnsaved(() => navigateTo(viewId, tab))" in nav


def test_the_router_loads_after_the_navigation_it_drives():
    """router.js calls navigateTo and validates against VIEW_META, both of
    which live in nav.js."""
    page = template.render("chat.html")
    scripts = re.findall(r'<script defer src="assets/(js/[^"?]+)(?:\?[^"]*)?"', page)

    assert scripts.count("js/core/router.js") == 1, scripts
    assert scripts.index("js/core/nav.js") < scripts.index("js/core/router.js")


def test_the_first_route_is_applied_only_once_auth_has_settled():
    """Routing at load time would switch views behind the login overlay, so
    the first apply hangs off initApp() -- the one function all three paths
    into the app go through. The router itself must only listen at load time."""
    auth = _js("core/auth.js")
    init = auth[auth.index("function initApp()"):]
    init = init[:init.index("\n}")]
    assert "routeApply()" in init

    router = _js("core/router.js")
    assert "addEventListener('hashchange', routeApply)" in router
    # A bare call at the top level would run before auth.
    assert not re.search(r"^routeApply\(\)", router, re.M)
