// Drive core/router.js against a stubbed page and check what it does to the
// history stack -- which view opens, and how many entries Back has to walk.
// The Python tests can assert that the routing code is wired up, but not what
// it does once it runs; this is that half. Run it by hand after touching the
// router:
//
//     node channel/web/tools/check-router.mjs
//
// No dependencies, no browser: the router only touches location, history and
// one event listener, all of which are stubbed below.
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const HERE = dirname(fileURLToPath(import.meta.url));
const routerSrc = readFileSync(
    join(HERE, '..', 'static', 'js', 'core', 'router.js'), 'utf8');

// Stands in for nav.js + the view scripts: records what it was asked to do and
// calls back into the router exactly where the real code does.
const fakeNav = `
function switchConfigTab(tab)    { log.tabs.push('config/' + tab);    routeNoteTab('config', tab); }
function switchMemoryTab(tab)    { log.tabs.push('memory/' + tab);    routeNoteTab('memory', tab); }
function switchTasksTab(tab)     { log.tabs.push('tasks/' + tab);     routeNoteTab('tasks', tab); }
function switchKnowledgeTab(tab) { log.tabs.push('knowledge/' + tab); routeNoteTab('knowledge', tab); }

function navigateTo(viewId, tab) {
    log.nav.push(viewId + (tab ? '/' + tab : ''));
    if (guardRefuses) return false;
    currentView = viewId;
    routeEnterView(viewId);
    if (viewId === 'config') switchConfigTab(tab || 'basic');
    else if (viewId === 'memory') switchMemoryTab(tab || 'files');
    else if (viewId === 'tasks') switchTasksTab(tab || 'tasks');
    else if (viewId === 'knowledge') { if (tab) switchKnowledgeTab(tab); }
    return true;
}
`;

function makeApp(startHash) {
    const log = { nav: [], tabs: [], writes: [] };
    const state = { hash: startHash || '' };

    const VIEW_META = {};
    for (const v of ['chat', 'agents', 'config', 'skills', 'memory',
                     'knowledge', 'channels', 'tasks', 'logs']) VIEW_META[v] = {};

    const location = { get hash() { return state.hash; } };
    const history = {
        pushState(_s, _t, h) { log.writes.push('push ' + h); state.hash = h; },
        replaceState(_s, _t, h) { log.writes.push('replace ' + h); state.hash = h; },
    };
    let fire = null;
    const window = { addEventListener(ev, fn) { if (ev === 'hashchange') fire = fn; } };

    const factory = new Function('env', `
        const { VIEW_META, location, history, window, log } = env;
        let currentView = 'chat';
        let guardRefuses = false;
        ${fakeNav}
        ${routerSrc}
        return {
            routeApply, navigateTo,
            get currentView() { return currentView; },
            get routeTab() { return routeTab; },
            set guardRefuses(v) { guardRefuses = v; },
        };
    `);

    const app = factory({ VIEW_META, location, history, window, log });
    // Back/Forward: the browser moves the hash, then notifies.
    app.back = (hash) => { state.hash = hash; fire(); };
    app.log = log;
    app.hash = () => state.hash;
    return app;
}

let failures = 0;
function check(label, actual, expected) {
    const a = JSON.stringify(actual), e = JSON.stringify(expected);
    const ok = a === e;
    if (!ok) failures++;
    console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}`);
    if (!ok) console.log(`        got      ${a}\n        expected ${e}`);
}

// --- a fresh visit with no hash stays on chat and writes nothing ----------
let app = makeApp('');
app.routeApply();
check('empty hash: no navigation', app.log.nav, []);
check('empty hash: address bar untouched', app.log.writes, []);

// --- a shared link opens the view and tab it names, without churn ---------
app = makeApp('#config/models');
app.routeApply();
check('deep link: navigates once', app.log.nav, ['config/models']);
check('deep link: opens the named tab', app.log.tabs, ['config/models']);
check('deep link: no history entries added', app.log.writes, []);
check('deep link: lands on the view', app.currentView, 'config');

// --- a hand-edited or stale route degrades instead of throwing ------------
app = makeApp('#knowledge/bogus');
app.routeApply();
check('unknown tab: dropped, view still opens', app.log.nav, ['knowledge']);
check('unknown tab: no tab switch attempted', app.log.tabs, []);

app = makeApp('#nope');
app.routeApply();
check('unknown view: falls back to chat, no navigation', app.log.nav, []);

// --- clicking through the sidebar leaves one entry per view ---------------
app = makeApp('');
app.navigateTo('config');
check('sidebar: one entry for the view, refined to its tab',
      app.log.writes, ['push #config', 'replace #config/basic']);
app.log.writes.length = 0;
app.navigateTo('skills');
check('sidebar: a tabless view is one plain entry',
      app.log.writes, ['push #skills']);

// --- re-entering the current view refines rather than stacking ------------
app = makeApp('');
app.navigateTo('config');
app.log.writes.length = 0;
app.navigateTo('config', 'models');
check('re-entry: refines the entry, never pushes a second one',
      app.log.writes, ['replace #config', 'replace #config/models']);

// clicking the sidebar item you are already on, repeatedly
app = makeApp('');
app.navigateTo('skills');
app.log.writes.length = 0;
app.navigateTo('skills');
app.navigateTo('skills');
check('re-entry: a tabless view stays at one entry', app.log.writes, []);

// --- Back returns to the previous view -----------------------------------
app = makeApp('');
app.navigateTo('config');
app.log.nav.length = 0;
app.back('#chat');
check('back: navigates to the previous view', app.log.nav, ['chat']);
check('back: lands there', app.currentView, 'chat');

// --- Back with unsaved edits puts the address bar back --------------------
app = makeApp('');
app.navigateTo('config');
app.guardRefuses = true;
app.log.writes.length = 0;
app.back('#chat');
check('guarded back: stays on the view', app.currentView, 'config');
check('guarded back: address bar restored, without a new entry',
      app.log.writes, ['replace #config/basic']);

// --- applying a route must not re-enter through the hash ------------------
app = makeApp('#tasks/records');
app.routeApply();
check('apply: does not loop back in', app.log.nav, ['tasks/records']);
check('apply: tracks the tab for a later restore', app.routeTab, 'records');

console.log(failures ? `\n${failures} FAILED` : '\nall scenarios pass');
process.exit(failures ? 1 : 0);
