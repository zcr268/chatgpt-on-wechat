/* Startup: theme, i18n, auth gate, initial fetches. Loads last.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

fetch('/config').then(r => r.json()).then(data => {
    if (data.status === 'success') {
        appConfig = data;
        const title = data.title || 'CowAgent';
        document.getElementById('welcome-title').textContent = title;
        initConfigView(data);
    }
    loadHistory(1);
}).catch(() => { loadHistory(1); });

// Start polling immediately so scheduler/push messages are received at any time
startPolling();
// =====================================================================
// Initialization
// =====================================================================
applyTheme();
applyI18n();

fetch('/auth/check').then(r => r.json()).then(data => {
    if (data.auth_required && !data.authenticated) {
        showLoginScreen();
    } else {
        if (data.auth_required) {
            const logoutBtn = document.getElementById('logout-btn-header');
            if (logoutBtn) logoutBtn.classList.remove('hidden');
        }
        initApp();
    }
}).catch(() => {
    initApp();
});

requestAnimationFrame(() => {
    document.body.classList.add('transition-colors', 'duration-200');
});

