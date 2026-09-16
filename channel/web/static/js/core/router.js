/* Hash routing: mirrors the current view and tab into location.hash, so a
   reload lands where the user left off and Back/Forward move between views.
   These are classic scripts sharing one global scope; see
   channel/web/README.md before changing the load order. */

// =====================================================================
// Routing
// =====================================================================
// A route names a view and, for the views that have tabs, one of its tabs:
// #config, #config/models. Deeper state -- which session is open, which file
// the editor holds -- deliberately stays out. It is already restored from
// localStorage, and putting it in the URL would rewrite the address bar on
// every click in the session list.
//
// The hash carries this rather than the path: every asset in the page is
// referenced relatively (assets/js/...), so a path route like /chat/config
// would resolve them against /chat/ and 404 the whole console.
const ROUTE_TABS = {
    config:    ['basic', 'models'],
    memory:    ['files', 'dreams'],
    tasks:     ['tasks', 'records'],
    knowledge: ['docs', 'graph'],
};

// The tab showing in the current view, '' for a view that has none. Tracked so
// a navigation the unsaved-edit guard refuses can put the address bar back
// exactly where it was.
let routeTab = '';

// Set while a route is being applied to the page. The handlers that normally
// write the address bar then leave it alone: it already says what is being
// applied, and writing would stack a duplicate entry onto the history.
let _routeApplying = false;

function _routeParse(hash) {
    const [view, tab] = String(hash || '').replace(/^#/, '').split('/');
    // An unknown view -- a stale bookmark, a hand-edited URL -- falls back to
    // chat rather than leaving the console on whatever happens to be on screen.
    if (!view || !VIEW_META[view]) return { view: 'chat', tab: '' };
    const allowed = ROUTE_TABS[view] || [];
    // An unknown tab is dropped, not passed on: the tab switchers index into
    // the DOM by name and would throw on one that does not exist.
    return { view: view, tab: allowed.indexOf(tab) === -1 ? '' : tab };
}

function _routeWrite(view, tab, replace) {
    const hash = '#' + view + (tab ? '/' + tab : '');
    if (location.hash === hash) return;
    // pushState/replaceState do not fire hashchange, so writing the address bar
    // cannot loop back in as a navigation.
    history[replace ? 'replaceState' : 'pushState'](null, '', hash);
}

function _routeApplyTab(view, tab) {
    if (view === 'config') switchConfigTab(tab);
    else if (view === 'memory') switchMemoryTab(tab);
    else if (view === 'tasks') switchTasksTab(tab);
    else if (view === 'knowledge') switchKnowledgeTab(tab);
}

// Called by navigateTo() once it has committed to a view: the history gains an
// entry, so Back returns to where the user came from.
function routeEnterView(view) {
    if (!VIEW_META[view]) return;
    routeTab = '';
    if (_routeApplying) return;
    // Re-entering the view already on screen -- clicking its sidebar item
    // again -- is not a new destination. Refine the entry instead of stacking
    // another, or Back would have to undo a run of no-op navigations before it
    // appeared to do anything.
    const reentry = _routeParse(location.hash).view === view;
    _routeWrite(view, '', reentry);
}

// Called by the tab switchers. A tab is a refinement of the view already on
// screen rather than a new destination, so it replaces the entry navigateTo
// just pushed instead of stacking another one -- Back then leaves the view,
// instead of stepping back through every tab visited inside it.
function routeNoteTab(view, tab) {
    if (view !== currentView) return;
    routeTab = tab;
    if (_routeApplying) return;
    _routeWrite(view, tab, true);
}

// Apply what the address bar says: on Back/Forward, and once at startup.
function routeApply() {
    const parsed = _routeParse(location.hash);

    if (parsed.view === currentView) {
        if (parsed.tab && parsed.tab !== routeTab) {
            _routeApplying = true;
            try { _routeApplyTab(parsed.view, parsed.tab); }
            finally { _routeApplying = false; }
        }
        return;
    }

    _routeApplying = true;
    let moved;
    try { moved = navigateTo(parsed.view, parsed.tab); }
    finally { _routeApplying = false; }

    // navigateTo refuses while a document editor holds unsaved changes: it
    // asks first, and calls back only if the user discards them. On Back the
    // address bar has already moved by then, so put it back until it happens.
    if (moved === false) _routeWrite(currentView, routeTab, true);
}

window.addEventListener('hashchange', routeApply);
