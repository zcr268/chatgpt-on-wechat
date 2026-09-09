/* Model configuration tab: vendors, capabilities, fallbacks.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

// =====================================================================
// Models View
// =====================================================================
// Capability cards rendered on the Models page. Order matters — main model
// comes first because it transitively decides defaults for vision and image.
// Icon palette is grouped by capability family:
//   - chat                       → primary (brand green; the "main" capability)
//   - vision + image             → blue    (everything visual)
//   - asr + tts                  → amber   (everything audio)
//   - embedding                  → purple  (vectors)
//   - search                     → orange  (retrieval)
// Each card uses an explicit `iconClass` string so Tailwind's CDN JIT can
// see the literal class names — dynamic `bg-${color}-50` strings would not
// be picked up reliably.
const MODELS_CAPABILITY_DEFS = [
    { id: 'chat',      icon: 'fa-microchip',        editable: true,  needsModel: true,  toggleable: false, titleKey: 'models_capability_chat',      descKey: 'models_capability_chat_desc',
      iconChip: 'bg-primary-50 dark:bg-primary-900/30',  iconGlyph: 'text-primary-500' },
    // NOTE: the chat fallback is deliberately NOT a top-level card. It is a
    // rarely-touched safety net, so it lives behind a small gear on the main
    // model card (see renderCapabilityHeaderTag / openChatFallbackModal) and
    // is edited in a modal that reuses the same picker machinery.
    { id: 'vision',    icon: 'fa-eye',              editable: true,  needsModel: true,  titleKey: 'models_capability_vision',    descKey: 'models_capability_vision_desc',
      iconChip: 'bg-blue-50 dark:bg-blue-900/30',        iconGlyph: 'text-blue-500' },
    { id: 'image',     icon: 'fa-image',            editable: true,  needsModel: true,  titleKey: 'models_capability_image',     descKey: 'models_capability_image_desc',
      iconChip: 'bg-blue-50 dark:bg-blue-900/30',        iconGlyph: 'text-blue-500' },
    { id: 'asr',       icon: 'fa-microphone',       editable: true,  needsModel: true,  titleKey: 'models_capability_asr',       descKey: 'models_capability_asr_desc',
      iconChip: 'bg-amber-50 dark:bg-amber-900/30',      iconGlyph: 'text-amber-500' },
    { id: 'tts',       icon: 'fa-volume-high',      editable: true,  needsModel: true,  titleKey: 'models_capability_tts',       descKey: 'models_capability_tts_desc',
      iconChip: 'bg-amber-50 dark:bg-amber-900/30',      iconGlyph: 'text-amber-500' },
    { id: 'embedding', icon: 'fa-vector-square',    editable: true,  needsModel: true,  titleKey: 'models_capability_embedding', descKey: 'models_capability_embedding_desc',
      iconChip: 'bg-purple-50 dark:bg-purple-900/30',    iconGlyph: 'text-purple-500' },
    { id: 'search',    icon: 'fa-magnifying-glass', editable: true,  needsModel: false, titleKey: 'models_capability_search',    descKey: 'models_capability_search_desc',
      iconChip: 'bg-orange-50 dark:bg-orange-900/30',    iconGlyph: 'text-orange-500' },
];

// Provider logos: when a real SVG exists under static/logos/<id>.svg we use
// it; otherwise we fall back to a neutral monogram chip. SVGs are fetched
// via <img> with a hidden onerror so layout stays stable when files are
// absent. Vendors whose mark is rendered in pure (or near-pure) black are
// listed in MODELS_PROVIDER_LOGO_DARK_INVERT — for those, we apply a CSS
// invert filter in dark mode so the glyph stays visible against #1A1A1A.
const MODELS_PROVIDER_LOGO_PATH = 'assets/logos';
const MODELS_PROVIDER_LOGO_DARK_INVERT = new Set([
    'openai',     // black wordmark
    'moonshot',   // dark monogram
    'zhipu',      // dark monogram
    'custom',     // single-color slider glyph
]);

let modelsState = { providers: [], capabilities: {} };

// One-shot: { capabilityId, providerId } stashed before a Models reload,
// consumed by renderCapabilityBody to preselect a just-configured vendor.
let pendingCapabilitySelection = null;

// `opts.preserveScroll` keeps the page's vertical scroll position across the
// refresh. We capture it before unhiding the loading skeleton (which collapses
// content height to zero) and restore it after the new content is mounted.
// This matters when the user configures a vendor from inside a capability
// card's dropdown — without preservation, the post-save reload bounces them
// back to the top of the page, away from the card they were configuring.
function loadModelsView(opts) {
    const loading = document.getElementById('models-loading');
    const content = document.getElementById('models-content');
    if (!loading || !content) return;
    const preserveScroll = !!(opts && opts.preserveScroll);
    // The Models pane has its own scrollable container; capture its position
    // (not window.scrollY) so we can put the user back exactly where they were.
    const scroller = document.querySelector('#view-config .overflow-y-auto');
    const savedTop = preserveScroll && scroller ? scroller.scrollTop : null;

    loading.classList.remove('hidden');
    content.classList.add('hidden');

    fetch('/api/models').then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            loading.innerHTML = `<span class="text-sm text-red-400">${escapeHtml(data.message || 'Failed to load')}</span>`;
            return;
        }
        modelsState.providers = data.providers || [];
        modelsState.capabilities = data.capabilities || {};
        renderModelsView();
        loading.classList.add('hidden');
        content.classList.remove('hidden');
        if (savedTop !== null && scroller) {
            // Wait one frame for the new layout to settle, otherwise the
            // restored scrollTop snaps to the previous (smaller) max.
            requestAnimationFrame(() => { scroller.scrollTop = savedTop; });
        }
    }).catch(err => {
        loading.innerHTML = `<span class="text-sm text-red-400">${escapeHtml(String(err))}</span>`;
    });
}

function renderModelsView() {
    const container = document.getElementById('models-content');
    container.innerHTML = '';
    container.appendChild(renderVendorsSection());
    MODELS_CAPABILITY_DEFS.forEach(def => container.appendChild(renderCapabilityCard(def)));
}

// True when a provider card is one of the expanded custom (OpenAI-compatible)
// providers (id "custom:<id>") — shown in the vendor grid alongside built-in
// vendors, but edited via the dedicated custom-provider modal.
function isCustomProviderCard(p) {
    return !!(p && p.is_custom && p.custom_name);
}

// ---------- Vendor section (Layer 1) -----------------------------------

function renderVendorsSection() {
    const wrap = document.createElement('div');
    wrap.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-6';

    // Custom providers always show once created (even without an api key,
    // e.g. a local vLLM/Ollama endpoint); built-in vendors show when configured.
    const configured = modelsState.providers.filter(p => p.configured || isCustomProviderCard(p));

    const header = `
        <div class="flex items-start gap-3 mb-5">
            <div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center flex-shrink-0">
                <i class="fas fa-key text-primary-500 text-sm"></i>
            </div>
            <div class="flex-1 min-w-0">
                <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t('models_section_vendors')}</h3>
                <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${t('models_section_vendors_desc')}</p>
            </div>
        </div>`;

    let body;
    if (configured.length === 0) {
        body = `
            <div class="flex flex-col items-center justify-center py-8 px-4 rounded-lg border border-dashed border-slate-200 dark:border-white/10">
                <p class="text-sm text-slate-500 dark:text-slate-400 text-center">${t('models_not_configured')}</p>
                <button onclick="openVendorModal('')"
                        class="mt-3 px-3 py-1.5 rounded-lg text-xs font-medium bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400 hover:bg-primary-100 dark:hover:bg-primary-900/50 cursor-pointer transition-colors">
                    <i class="fas fa-plus text-[10px] mr-1"></i>${t('models_add_vendor')}
                </button>
            </div>`;
    } else {
        // Existing vendors as chips, plus a trailing "add" tile so a new
        // built-in or custom provider can still be added once at least one is
        // already configured (otherwise the add entry only showed on the empty
        // state). openVendorModal('') opens the picker → built-in or custom.
        const addTile = `
            <button onclick="openVendorModal('')"
                    class="flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg border border-dashed
                           border-slate-300 dark:border-white/15 text-slate-500 dark:text-slate-400
                           hover:border-primary-400 hover:text-primary-500 cursor-pointer transition-colors text-sm">
                <i class="fas fa-plus text-[11px]"></i>${t('models_add_vendor')}
            </button>`;
        body = `<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            ${configured.map(renderVendorChip).join('')}
            ${addTile}
        </div>`;
    }

    wrap.innerHTML = header + body;
    return wrap;
}

function renderVendorChip(p) {
    // The masked API key is intentionally not surfaced here; it is shown
    // inside the edit modal so the chip stays uncluttered and scannable.
    // Custom providers open their dedicated modal (name + base + key);
    // their ids are server-generated hex, safe to inline.
    const onclick = isCustomProviderCard(p)
        ? `openCustomProviderModal('${escapeHtml(p.custom_id)}')`
        : `openVendorModal('${escapeHtml(p.id)}')`;
    return `
        <button onclick="${onclick}"
                class="group flex items-center gap-3 px-3 py-2.5 rounded-lg border border-slate-200 dark:border-white/10
                       bg-slate-50 dark:bg-white/5 hover:border-primary-300 dark:hover:border-primary-500/50
                       cursor-pointer transition-colors duration-150 text-left">
            ${renderProviderLogo(p, 28)}
            <span class="flex-1 min-w-0 text-sm font-medium text-slate-800 dark:text-slate-100 truncate">${escapeHtml(localizedLabel(p.label))}</span>
            <i class="fas fa-pen-to-square text-[11px] text-slate-400 dark:text-slate-500 group-hover:text-primary-500 transition-colors"></i>
        </button>`;
}

// Render a uniformly-styled logo for a provider. Tries an SVG asset first; if
// it 404s the <img> swaps itself for a monogram fallback via onerror.
function renderProviderLogo(p, sizePx) {
    const initial = (localizedLabel(p.label) || p.id || '?').slice(0, 1).toUpperCase();
    const sz = sizePx || 32;
    const url = `${MODELS_PROVIDER_LOGO_PATH}/${encodeURIComponent(p.id)}.svg`;
    const fallbackId = `pl-${p.id}-${Math.random().toString(36).slice(2, 8)}`;
    const imgClass = MODELS_PROVIDER_LOGO_DARK_INVERT.has(p.id)
        ? 'absolute inset-0 m-auto provider-logo-img provider-logo-invert-dark'
        : 'absolute inset-0 m-auto provider-logo-img';
    return `
        <span class="relative flex items-center justify-center rounded-lg bg-slate-100 dark:bg-white/10
                     text-slate-600 dark:text-slate-300 flex-shrink-0 overflow-hidden"
              style="width:${sz}px;height:${sz}px;">
            <span id="${fallbackId}" class="text-xs font-bold">${escapeHtml(initial)}</span>
            <img src="${url}" alt="" aria-hidden="true"
                 class="${imgClass}"
                 style="width:${Math.round(sz * 0.65)}px;height:${Math.round(sz * 0.65)}px;"
                 onload="(function(el){var f=document.getElementById('${fallbackId}');if(f)f.style.display='none';})(this)"
                 onerror="this.remove();">
        </span>`;
}

function getCustomProviderCards() {
    return modelsState.providers.filter(isCustomProviderCard);
}

// ---------- Capability cards (Layer 2) ---------------------------------

function renderCapabilityCard(def) {
    const cap = modelsState.capabilities[def.id] || {};
    const wrap = document.createElement('div');
    wrap.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-6';
    wrap.id = `models-card-${def.id}`;

    const headerRight = renderCapabilityHeaderTag(def, cap);

    wrap.innerHTML = `
        <div class="flex items-start gap-3 mb-5">
            <div class="w-9 h-9 rounded-lg ${def.iconChip} flex items-center justify-center flex-shrink-0">
                <i class="fas ${def.icon} ${def.iconGlyph} text-sm"></i>
            </div>
            <div class="flex-1 min-w-0">
                <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t(def.titleKey)}</h3>
                <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${t(def.descKey)}</p>
            </div>
            ${headerRight}
        </div>
        <div class="space-y-4" data-cap-body="${def.id}"></div>`;

    const body = wrap.querySelector(`[data-cap-body="${def.id}"]`);
    renderCapabilityBody(def, cap, body);
    return wrap;
}

function renderCapabilityHeaderTag(def, cap) {
    // The main model card carries a small gear that opens the chat-fallback
    // modal. The fallback is a rarely-touched safety net, so it stays out of
    // the card body; a badge appears next to the gear only while it is on, so
    // an active fallback is still discoverable at a glance.
    if (def.id === 'chat') {
        const fb = modelsState.capabilities.chat_fallback || {};
        // A single entry point that also reflects state: green + "on" label
        // when the fallback is enabled, muted + "configure" label when off.
        const on = !!fb.enabled;
        const cls = on
            ? 'text-primary-600 dark:text-primary-300 bg-primary-50 dark:bg-primary-900/30 hover:bg-primary-100 dark:hover:bg-primary-900/50'
            : 'text-slate-500 dark:text-slate-400 hover:text-primary-600 dark:hover:text-primary-300 hover:bg-slate-100 dark:hover:bg-white/5';
        const label = on ? t('models_fallback_badge_on') : t('models_fallback_config');
        return `
            <button type="button" onclick="openChatFallbackModal()"
                    title="${escapeHtml(t('models_fallback_config_tip'))}"
                    class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs flex-shrink-0
                           cursor-pointer transition-colors ${cls}">
                <i class="fas fa-shield-halved text-[11px]"></i>${label}
            </button>`;
    }
    return '';
}

// The chat fallback is configured in a modal rather than as a top-level card
// (it is a rarely-touched safety net). The modal body reuses the exact same
// picker machinery as a capability card — `renderCapabilityBody` keys every
// element off `cap-chat_fallback-*`, so we hand it a def with that id and let
// the existing provider/model/toggle/save code run unchanged. No such card is
// registered in MODELS_CAPABILITY_DEFS, so the ids never collide.
const CHAT_FALLBACK_DEF = {
    id: 'chat_fallback', editable: true, needsModel: true, toggleable: true,
    titleKey: 'models_fallback_modal_title', descKey: 'models_capability_chat_fallback_desc',
};

// Resolve a capability def by id. The chat fallback is intentionally absent
// from MODELS_CAPABILITY_DEFS (it renders in a modal, not as a card), so the
// shared save/toggle handlers look it up here too.
function capabilityDefById(capId) {
    if (capId === 'chat_fallback') return CHAT_FALLBACK_DEF;
    return MODELS_CAPABILITY_DEFS.find(d => d.id === capId);
}

function openChatFallbackModal() {
    closeChatFallbackModal(); // never stack two

    const overlay = document.createElement('div');
    overlay.id = 'chat-fallback-modal-overlay';
    overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4';
    overlay.innerHTML = `
        <div class="w-full max-w-md rounded-2xl bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 shadow-xl">
            <div class="flex items-start gap-3 px-6 pt-6 pb-4 border-b border-slate-100 dark:border-white/5">
                <div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center flex-shrink-0">
                    <i class="fas fa-shield-halved text-primary-500 text-sm"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t('models_fallback_modal_title')}</h3>
                    <p class="text-xs text-slate-500 dark:text-slate-400 mt-1 leading-relaxed">${t('models_fallback_modal_desc')}</p>
                </div>
                <button type="button" onclick="closeChatFallbackModal()"
                        class="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 cursor-pointer transition-colors flex-shrink-0">
                    <i class="fas fa-xmark"></i>
                </button>
            </div>
            <div class="px-6 py-5 space-y-4" data-cap-body="chat_fallback"></div>
        </div>`;

    // Close on backdrop click (but not when clicking inside the dialog).
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeChatFallbackModal(); });

    document.body.appendChild(overlay);

    const cap = modelsState.capabilities.chat_fallback || {};
    const body = overlay.querySelector('[data-cap-body="chat_fallback"]');
    renderCapabilityBody(CHAT_FALLBACK_DEF, cap, body);
}

function closeChatFallbackModal() {
    const overlay = document.getElementById('chat-fallback-modal-overlay');
    if (overlay) overlay.remove();
}

function _searchProviderLabel(cap, providerId) {
    const list = (cap && cap.providers) || [];
    const hit = list.find(p => p.id === providerId);
    return hit ? localizedLabel(hit.label) : providerId;
}

// Search card body: strategy picker + (when fixed) provider picker + a
// status row that surfaces which providers are ready and how to add the
// missing ones. Three of the four backends piggy-back on model-vendor
// credentials (zhipu / qianfan / linkai); bocha owns its own key under
// tools.web_search and gets its own minimal credential modal.
function renderSearchCapability(def, cap, body) {
    const providers = cap.providers || [];
    const configuredIds = cap.configured_providers || [];
    const hasAny = configuredIds.length > 0;
    const strategy = cap.strategy || 'auto';

    body.innerHTML = `
        <div>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_search_strategy_label')}</label>
            <div id="cap-search-strategy" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>
        <div id="cap-search-provider-wrap" class="hidden">
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_provider')}</label>
            <div id="cap-search-provider" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>
        <div id="cap-search-summary"></div>
        <div class="flex items-center justify-end gap-3 pt-1">
            <span id="cap-search-status" class="text-xs text-primary-500 opacity-0 transition-opacity duration-300"></span>
            <button onclick="saveSearchCapability()"
                    class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed">
                ${t('save')}
            </button>
        </div>
    `;

    // Strategy dropdown — when no provider is configured the strategy
    // value is meaningless, so we show a "待配置" placeholder instead of
    // a default selection. Once any provider gets configured the saved
    // strategy (or "auto") becomes the active value.
    initDropdown(
        body.querySelector('#cap-search-strategy'),
        [
            { value: 'auto',  label: t('models_strategy_auto'),         hint: t('models_search_strategy_auto_hint') },
            { value: 'fixed', label: t('models_search_strategy_fixed'), hint: t('models_search_strategy_fixed_hint') },
        ],
        hasAny ? strategy : '',
        (value) => _onSearchStrategyChange(cap, value, body),
        hasAny ? null : { placeholder: t('models_pending_config') },
    );

    // Provider dropdown — populated with configured providers only;
    // unconfigured ones cannot be pinned (they'd silently fall back).
    const provOpts = configuredIds.map(id => ({
        value: id,
        label: _searchProviderLabel(cap, id),
    }));
    if (provOpts.length === 0) provOpts.push({ value: '', label: '--' });
    initDropdown(
        body.querySelector('#cap-search-provider'),
        provOpts,
        cap.fixed_provider || configuredIds[0] || '',
        () => {},
    );

    _renderSearchSummary(body, cap);
    _setSearchProviderPickerVisible(body, strategy === 'fixed' && hasAny);
}

function _onSearchStrategyChange(cap, value, body) {
    const configuredIds = cap.configured_providers || [];
    _setSearchProviderPickerVisible(body, value === 'fixed' && configuredIds.length > 0);
}

function _setSearchProviderPickerVisible(body, visible) {
    const wrap = body.querySelector('#cap-search-provider-wrap');
    if (!wrap) return;
    if (visible) wrap.classList.remove('hidden');
    else wrap.classList.add('hidden');
}

// Search summary line: just lists configured providers + a trailing "+
// add" button. Unconfigured backends are hidden — the user picks one from
// a small chooser when they click add. Empty state surfaces the same add
// button as a primary CTA.
function _renderSearchSummary(body, cap) {
    const host = body.querySelector('#cap-search-summary');
    if (!host) return;
    const providers = cap.providers || [];
    const configured = providers.filter(p => p.configured);

    const missing = providers.filter(p => !p.configured);

    const addBtn = missing.length
        ? `<button type="button" id="cap-search-add-btn"
                  class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] rounded-md cursor-pointer
                         bg-slate-100 dark:bg-white/5 text-slate-500 dark:text-slate-400
                         hover:bg-slate-200 dark:hover:bg-white/10 transition-colors">
              <i class="fas fa-plus text-[10px]"></i>${t('models_search_add_provider')}
           </button>`
        : '';

    if (configured.length === 0) {
        host.innerHTML = `
            <div class="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                <i class="fas fa-circle-info text-[10px] text-amber-500"></i>
                <span>${t('models_search_none_configured')}</span>
                ${addBtn}
            </div>
        `;
    } else {
        const chips = configured.map(p => `
            <button type="button" data-search-edit-provider="${p.id}"
                    title="${t('models_search_edit_hint')}"
                    class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] rounded-md cursor-pointer
                           bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400
                           hover:bg-emerald-100 dark:hover:bg-emerald-900/50 transition-colors">
                <i class="fas fa-check text-[10px]"></i>${escapeHtml(localizedLabel(p.label))}${p.anonymous ? ` · ${t('models_search_anonymous_badge')}` : ''}
            </button>
        `).join('');
        host.innerHTML = `
            <div class="flex items-center flex-wrap gap-2 text-xs text-slate-500 dark:text-slate-400">
                <span>${t('models_search_available_label')}</span>
                ${chips}
                ${addBtn}
            </div>
        `;
    }

    const addBtnEl = host.querySelector('#cap-search-add-btn');
    if (addBtnEl) {
        addBtnEl.addEventListener('click', (ev) => {
            ev.preventDefault();
            openSearchAddProviderPicker(missing);
        });
    }
    host.querySelectorAll('[data-search-edit-provider]').forEach(el => {
        el.addEventListener('click', (ev) => {
            ev.preventDefault();
            const pid = el.getAttribute('data-search-edit-provider');
            const meta = (cap.providers || []).find(p => p.id === pid);
            _launchSearchProviderConfig(pid, meta);
        });
    });
}

// Two-step add flow: click "+ 添加厂商" -> chooser dialog -> per-provider
// credential editor. Bocha lands on the dedicated key modal; the others
// piggy-back on the existing vendor credential modal.
function openSearchAddProviderPicker(missingProviders) {
    if (!missingProviders || missingProviders.length === 0) return;
    if (missingProviders.length === 1) {
        _launchSearchProviderConfig(missingProviders[0].id);
        return;
    }

    const existing = document.getElementById('search-add-modal');
    if (existing) existing.remove();

    const rows = missingProviders.map(p => `
        <button type="button" data-pid="${p.id}"
                class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg cursor-pointer
                       bg-slate-50 dark:bg-white/5 hover:bg-slate-100 dark:hover:bg-white/10
                       text-sm text-slate-700 dark:text-slate-200 transition-colors">
            <span>${escapeHtml(localizedLabel(p.label))}</span>
            <i class="fas fa-chevron-right text-[10px] text-slate-400"></i>
        </button>
    `).join('');

    const modal = document.createElement('div');
    modal.id = 'search-add-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_add_provider')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${t('models_search_add_desc')}</p>
            <div class="space-y-2">${rows}</div>
            <div class="flex items-center justify-end mt-5">
                <button type="button" onclick="document.getElementById('search-add-modal').remove()"
                        class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
                               hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                    ${t('cancel')}
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.querySelectorAll('[data-pid]').forEach(el => {
        el.addEventListener('click', () => {
            const pid = el.getAttribute('data-pid');
            modal.remove();
            _launchSearchProviderConfig(pid);
        });
    });
}

function _launchSearchProviderConfig(providerId, providerMeta) {
    // Providers that hold their own credential (dedicated key or, for SearXNG,
    // an instance URL) use the bespoke search-key modal. zhipu/qianfan/linkai
    // reuse a model-vendor key and go through the vendor modal instead.
    if (['bocha', 'anysearch', 'serply', 'tavily', 'searxng', 'keenable'].includes(providerId)) {
        openSearchKeyModal(providerId, providerMeta);
    } else {
        openVendorModal(providerId, () => loadModelsView({ preserveScroll: true }));
    }
}


function saveSearchCapability() {
    const strategyDd = document.getElementById('cap-search-strategy');
    const providerDd = document.getElementById('cap-search-provider');
    // 如果策略下拉框的值是空（待配置），默认使用 'auto'
    const strategy = strategyDd ? (getDropdownValue(strategyDd) || 'auto') : 'auto';
    const provider = (strategy === 'fixed' && providerDd) ? getDropdownValue(providerDd) : '';

    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'set_capability',
            capability: 'search',
            strategy,
            provider,
        }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            showStatus('cap-search-status', 'models_save_success', false);
            setTimeout(() => loadModelsView({ preserveScroll: true }), 400);
        } else {
            console.log('[saveSearchCapability] Error:', data.message);
            showStatus('cap-search-status', 'models_save_failed', true);
        }
    }).catch(() => showStatus('cap-search-status', 'models_save_failed', true));
}


// Minimal bocha API-key modal. Reuses the existing vendor-modal markup
// helpers would be nice, but bocha isn't in PROVIDER_MODELS (it's not a
// model vendor), so we render a tiny dedicated dialog.
// For search vendors that hold their own keys.


function openSearchKeyModal(providerId, providerMeta) {
    const existing = document.getElementById('search-key-modal');
    if (existing) existing.remove();

    const searchCap = (modelsState && modelsState.capabilities && modelsState.capabilities.search) || {};
    const prov = (searchCap.providers || []).find(p => p.id === providerId);
    const isSearxng = providerId === 'searxng';
    // SearXNG holds an instance URL (echoed back verbatim in url_masked); the
    // rest hold a masked API key. Resolve whichever applies as the field value.
    let masked;
    if (isSearxng) {
        masked = (providerMeta && providerMeta.url_masked) || (prov && prov.url_masked) || '';
    } else {
        masked = (providerMeta && providerMeta.api_key_masked) || '';
        if (!masked && prov && prov.api_key_masked) masked = prov.api_key_masked;
    }
    // SearXNG URL is not masked, so it's safe to keep editable (not a sentinel).
    const hasKey = !!masked;
    const isAnonymous = (providerId === 'anysearch' || providerId === 'keenable')
        && !!((providerMeta && providerMeta.anonymous) || (prov && prov.anonymous));
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    let descText = t('models_search_' + providerId + '_desc');
    if (providerId === 'anysearch') {
        const hint = currentLang === 'zh'
            ? '（留空可启用匿名模式，每日有免费额度）'
            : '(Leave blank to enable anonymous mode with daily free quota)';
        descText = descText + ' ' + hint;
    }
    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${providerId === 'searxng' ? 'Instance URL' : 'API Key'}</label>
            <input id="search-key-input" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                          bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
                   value="${escapeHtml(masked)}"
                   data-masked="${(hasKey && !isSearxng) ? '1' : ''}"
                   placeholder="${isSearxng ? 'https://searxng.example.com' : 'sk-...'}" />
            <div class="flex items-center justify-between gap-3 mt-5">
                <div>${clearBtnHtml}</div>
                <div class="flex items-center gap-3">
                    <button type="button" onclick="document.getElementById('search-key-modal').remove()"
                            class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
                                   hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                        ${t('cancel')}
                    </button>
                    <button type="button" onclick="_saveSearchKey('${providerId}')"
                            class="px-4 py-1.5 rounded-md bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                                   cursor-pointer transition-colors">
                        ${t('save')}
                    </button>
                </div>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // Reset masked sentinel as soon as the user starts editing so the save
    // handler can tell apart "kept the existing key" vs "typed a new one".
    const input = document.getElementById('search-key-input');
    if (input) {
        const unmask = () => {
            if (input.dataset.masked === '1') {
                input.value = '';
                input.dataset.masked = '';
                input.classList.remove('cfg-key-masked');
            }
        };
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Tab' || e.key === 'Escape') return;
            unmask();
        });
        input.addEventListener('paste', unmask);
        if (!hasKey) setTimeout(() => input.focus(), 50);
    }
    const clearBtn = document.getElementById('search-key-clear');
    if (clearBtn) clearBtn.addEventListener('click', () => _clearSearchKey(providerId));

    modal.addEventListener('mousedown', (e) => {
        if (e.target === modal) modal.remove();
    });
    const onKey = (e) => {
        if (e.key === 'Escape') {
            modal.remove();
            document.removeEventListener('keydown', onKey);
        }
    };
    document.addEventListener('keydown', onKey);
}


function _saveSearchKey(providerId) {
    const input = document.getElementById('search-key-input');
    if (!input) return;
    if (input.dataset.masked === '1') {
        const modal = document.getElementById('search-key-modal');
        if (modal) modal.remove();
        return;
    }
    const apiKey = input.value.trim();

    // anysearch and keenable: saving with an empty key enables the anonymous tier.
    if (providerId === 'anysearch' || providerId === 'keenable') {
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'set_search_credential',
            provider: providerId,
            api_key: apiKey,
            anonymous: !apiKey, // ← 字段名必须是 anonymous；留空保存 = 启用匿名（表第 2 行）
        }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            const modal = document.getElementById('search-key-modal');
            if (modal) modal.remove();
            loadModelsView({ preserveScroll: true });
        }
    });
    return;
}

    if (providerId === 'searxng') {
        // SearXNG uses an instance URL, not an API key. Empty input is a no-op
        // here (use the clear button to remove it).
        if (!apiKey) {
            input.focus();
            return;
        }
        fetch('/api/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'set_search_credential',
                provider: providerId,
                url: apiKey, // reuse the input value as the URL
            }),
        }).then(r => r.json()).then(data => {
            if (data.status === 'success') {
                const modal = document.getElementById('search-key-modal');
                if (modal) modal.remove();
                loadModelsView({ preserveScroll: true });
            }
        });
        return;
    }

    if (!apiKey) {
        input.focus();
        return;
    }
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_search_credential', provider: providerId, api_key: apiKey }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            const modal = document.getElementById('search-key-modal');
            if (modal) modal.remove();
            loadModelsView({ preserveScroll: true });
        }
    });
}

function _clearSearchKey(providerId) {
    // SearXNG is cleared by emptying its instance URL, not an API key.
    const payload = (providerId === 'searxng')
        ? { action: 'set_search_credential', provider: providerId, url: '' }
        : { action: 'set_search_credential', provider: providerId, api_key: '' };
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            const modal = document.getElementById('search-key-modal');
            if (modal) modal.remove();
            loadModelsView({ preserveScroll: true });
        }
    });
}

function renderCapabilityBody(def, cap, body) {
    if (def.id === 'search') {
        renderSearchCapability(def, cap, body);
        return;
    }

    // Editable cards: provider dropdown + (optional) model dropdown + save row
    const providerOpts = buildCapabilityProviderOptions(def, cap);
    const providerHtml = `
        <div>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_provider')}</label>
            <div id="cap-${def.id}-provider" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>`;

    // The model-picker container is always emitted so the provider-change
    // handler can show/hide it; for `auto` capabilities it starts hidden and
    // gets toggled by setCapabilityModelPickerVisible.
    const modelHtml = def.needsModel ? `
        <div id="cap-${def.id}-model-wrap">
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_model')}</label>
            <div id="cap-${def.id}-model" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
            <div id="cap-${def.id}-model-custom-wrap" class="mt-2 hidden">
                <input id="cap-${def.id}-model-custom" type="text"
                       class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                              bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                              focus:outline-none focus:border-primary-500 font-mono transition-colors"
                       placeholder="custom model name">
            </div>
        </div>` : '';

    const dimHtml = (def.id === 'embedding' && cap.current_dim) ? `
        <p class="text-xs text-slate-400 dark:text-slate-500">
            <i class="fas fa-cube text-[10px] mr-1"></i>${t('models_dim_label')}: <span class="font-mono">${cap.current_dim}</span>
        </p>` : '';

    // Opt-in capabilities get an on/off switch above the pickers. Everything
    // below it is hidden while off, so a disabled fallback never looks like an
    // unconfigured one — it is simply not part of the setup.
    const toggleHtml = def.toggleable ? `
        <div id="cap-${def.id}-toggle-wrap" class="flex items-center justify-between gap-3">
            <label class="text-sm font-medium text-slate-600 dark:text-slate-400">${t('models_fallback_enable')}</label>
            <button type="button" id="cap-${def.id}-toggle" role="switch"
                    aria-checked="${cap.enabled ? 'true' : 'false'}"
                    onclick="toggleCapabilityEnabled('${def.id}')"
                    class="relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition-colors cursor-pointer ${cap.enabled ? 'bg-primary-500' : 'bg-slate-200 dark:bg-slate-700'}">
                <span class="inline-block h-3.5 w-3.5 rounded-full bg-white shadow transition-transform ${cap.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}"></span>
            </button>
        </div>` : '';

    // Footer layout: a "hint slot" (filled later by renderCapabilityHints for
    // auto-mode cards) sits on the left while status + save stay anchored on
    // the right. Keeping them on the same row means the save button hugs the
    // inputs above instead of being pushed down by a separate hint line.
    const footer = `
        <div class="flex items-center justify-between gap-3 pt-1">
            <div data-cap-hint="${def.id}" class="flex-1 min-w-0"></div>
            <div class="flex items-center gap-3 flex-shrink-0">
                <span id="cap-${def.id}-status" class="text-xs text-primary-500 opacity-0 transition-opacity duration-300"></span>
                <button onclick="saveCapability('${def.id}')"
                        class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                               cursor-pointer transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed">
                    ${t('save')}
                </button>
            </div>
        </div>`;

    // Pickers live in their own wrapper so a disabled opt-in capability can
    // hide them as a group (the toggle itself stays visible above). The
    // wrapper carries its own `space-y-4` because the body's `space-y-4` only
    // applies to *direct* children: without it the provider/model rows would
    // collapse against each other (and against the label above them).
    const pickersHtml = `<div id="cap-${def.id}-pickers" class="space-y-4">${providerHtml + modelHtml + dimHtml}</div>`;
    body.innerHTML = toggleHtml + pickersHtml + footer;

    // TTS: mount reply-mode above provider; defer off-mode toggle to the end.
    if (def.id === 'tts') {
        renderVoiceReplyMode(body, cap.reply_mode || 'off', { skipVisibilityToggle: true });
        // Voice-timbre picker depends on provider+model; rebuilt by callbacks.
        const modelWrap = body.querySelector(`#cap-${def.id}-model-wrap`);
        if (modelWrap) {
            const voiceWrap = document.createElement('div');
            voiceWrap.id = `cap-${def.id}-voice-wrap`;
            voiceWrap.innerHTML = `
                <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_voice')}</label>
                <div id="cap-${def.id}-voice" class="cfg-dropdown" tabindex="0">
                    <div class="cfg-dropdown-selected">
                        <span class="cfg-dropdown-text">--</span>
                        <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                    </div>
                    <div class="cfg-dropdown-menu"></div>
                </div>
                <div id="cap-${def.id}-voice-custom-wrap" class="hidden mt-2">
                    <input id="cap-${def.id}-voice-custom" type="text"
                           class="w-full px-3 py-2 text-sm rounded-md border border-slate-200 dark:border-slate-700
                                  bg-white dark:bg-slate-800 text-slate-700 dark:text-slate-200
                                  placeholder:text-slate-400 dark:placeholder:text-slate-500
                                  focus:outline-none focus:ring-2 focus:ring-primary-500"
                           placeholder="voice id" />
                </div>
            `;
            modelWrap.parentNode.insertBefore(voiceWrap, modelWrap.nextSibling);
        }
    }

    // `body` is still detached from `document`; scope lookups locally.
    const provDd = body.querySelector(`#cap-${def.id}-provider`);
    // Strip private fields before handing to the generic initDropdown helper.
    const ddOpts = providerOpts.map(o => ({ value: o.value, label: o.label }));

    let pendingProvider = null;
    if (pendingCapabilitySelection
            && pendingCapabilitySelection.capabilityId === def.id
            && providerOpts.some(o => o.value === pendingCapabilitySelection.providerId)) {
        pendingProvider = pendingCapabilitySelection.providerId;
        pendingCapabilitySelection = null;
    }

    // Auto strategy => leave empty sentinel selected. `suggested_provider`
    // is a UI-only preselect (not persisted until the user clicks Save).
    // No current + no suggestion => leave unselected with a placeholder.
    //
    // Pending-config takes priority over both "auto" and "pick provider":
    // when no real (non-sentinel) configured option exists, surfacing
    // "auto" or "pick" misleads the user — there's nothing to auto-route
    // to or pick from. Force a "待配置" placeholder instead so all
    // capabilities behave consistently on a fresh environment.
    const hasConfiguredOpt = providerOpts.some(o => !o._isAuto && o._configured);
    const noSelectionAndNoHint = !cap.current_provider && !cap.suggested_provider;
    let initialProviderValue;
    let dropdownPlaceholder = null;
    if (!hasConfiguredOpt) {
        initialProviderValue = '';
        dropdownPlaceholder = { placeholder: t('models_pending_config') };
    } else {
        initialProviderValue = pendingProvider
            ? pendingProvider
            : ((cap.strategy === 'auto' && capabilitySupportsAuto(def.id))
                ? ''
                : (cap.current_provider
                    || cap.suggested_provider
                    || (noSelectionAndNoHint ? '' : (ddOpts[0] && ddOpts[0].value))
                    || ''));
        if (noSelectionAndNoHint) {
            dropdownPlaceholder = { placeholder: t('models_pick_provider') };
        }
    }
    // Seed the "provider active before the last switch" tracker so the very
    // first vendor switch can still stash the initial provider's custom model.
    capabilityLastProviderId[def.id] = initialProviderValue;
    // If the initially selected model is a custom one, remember it against the
    // initial provider so a switch-away-and-back keeps it too.
    if (initialProviderValue && cap.current_model) {
        const provList = (cap.provider_models && cap.provider_models[initialProviderValue])
            || (initialProviderValue.startsWith('custom:') && cap.provider_models && cap.provider_models['custom'])
            || [];
        const presetValues = provList.map(e => (typeof e === 'string' ? e : e.value));
        if (!presetValues.includes(cap.current_model)) {
            capabilityCustomModelMemory[`${def.id}:${initialProviderValue}`] = cap.current_model;
        }
    }
    initDropdown(
        provDd,
        ddOpts,
        initialProviderValue,
        (value) => onCapabilityProviderChange(def, value, body),
        dropdownPlaceholder,
    );
    decorateCapabilityProviderDropdown(def, provDd, providerOpts);

    if (def.needsModel) {
        rebuildCapabilityModelDropdown(def, initialProviderValue, cap.current_model || '', body);
        // Embedding: hide model picker when no provider is selected.
        const showModel = def.id === 'embedding' ? initialProviderValue !== '' :
            (initialProviderValue !== '' || !capabilitySupportsAuto(def.id));
        setCapabilityModelPickerVisible(def, showModel, body);
    }

    if (def.id === 'tts') {
        rebuildCapabilityVoiceDropdown(
            initialProviderValue,
            cap.current_voice || '',
            body,
            cap.current_model || ''
        );
    }

    // Inject auto/router-pending hint banners before the action footer.
    renderCapabilityHints(def, cap, body, initialProviderValue);

    // Opt-in capabilities start collapsed when disabled, so an inactive
    // fallback reads as "off" rather than as a half-configured capability.
    if (def.toggleable) {
        _setCapabilityPickersVisible(def, body, !!cap.enabled);
    }

    if (def.id === 'tts') {
        _setTtsConfigVisible(body, (cap.reply_mode || 'off') !== 'off');
    }
}

// TTS reply-policy dropdown (off / voice_if_voice / always). Persists on
// change. When off, hides the rest of the TTS card.
function renderVoiceReplyMode(host, currentMode, options) {
    options = options || {};
    const opts = [
        { value: 'off',            label: t('voice_reply_off') },
        { value: 'voice_if_voice', label: t('voice_reply_if_voice') },
        { value: 'always',         label: t('voice_reply_always') },
    ];
    const wrap = document.createElement('div');
    wrap.id = 'voice-reply-mode-wrap';
    wrap.innerHTML = `
        <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('voice_reply_mode_label')}</label>
        <div id="voice-reply-mode-dd" class="cfg-dropdown" tabindex="0">
            <div class="cfg-dropdown-selected">
                <span class="cfg-dropdown-text">--</span>
                <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
            </div>
            <div class="cfg-dropdown-menu"></div>
        </div>
    `;
    host.prepend(wrap);

    const dd = wrap.querySelector('#voice-reply-mode-dd');
    const valid = ['off', 'voice_if_voice', 'always'];
    const initial = valid.includes(currentMode) ? currentMode : 'off';
    if (!options.skipVisibilityToggle) _setTtsConfigVisible(host, initial !== 'off');
    initDropdown(dd, opts, initial, (mode) => {
        if (!valid.includes(mode)) return;
        _setTtsConfigVisible(host, mode !== 'off');
        fetch('/api/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'set_voice_reply_mode', mode }),
        })
            .then(r => r.json())
            .then(data => {
                if (data && data.status === 'success') {
                    _ttsReadyPromise = null;  // force re-probe on next bubble
                }
            })
            .catch(() => {});
    });
}

// Show/hide everything in the TTS card below the reply-mode dropdown.
function _setTtsConfigVisible(host, visible) {
    if (!host) return;
    Array.from(host.children).forEach((child) => {
        if (child.id === 'voice-reply-mode-wrap') return;
        child.classList.toggle('hidden', !visible);
    });
}

// Toggle wrapper visibility instead of re-rendering so dropdown state survives.
function setCapabilityModelPickerVisible(def, visible, scope) {
    const root = scope || document;
    const wrap = root.querySelector(`#cap-${def.id}-model-wrap`);
    if (!wrap) return;
    wrap.classList.toggle('hidden', !visible);
}

function renderCapabilityHints(def, cap, body, currentProvider) {
    // Capabilities that can be in "auto" mode show a fallback hint right
    // under the inputs so users always know what'd actually be hit. The
    // image card additionally surfaces a "router pending" warning until the
    // standalone dispatcher lands.
    // The hint slot is co-located with the save button in the footer row
    // (see renderCapabilityBody) so the save button stays close to the
    // inputs above. We just rewrite the slot's innerHTML — emptying it
    // when the card leaves auto mode, or rendering a one-line hint when
    // it's in auto mode.
    const slot = body.querySelector(`[data-cap-hint="${def.id}"]`);
    if (!slot) return;
    slot.innerHTML = '';

    if (currentProvider !== '' || !capabilitySupportsAuto(def.id)) return;

    // The hint mirrors what the runtime would actually pick when in auto
    // mode. fallback_provider/model are pre-computed on the backend (see
    // _predict_vision_auto, _predict_image_auto) so we can trust them
    // here without re-implementing the provider chain.
    const fbProv = cap.fallback_provider || '';
    const fbModel = cap.fallback_model || '';
    if (!fbProv && !fbModel) return;
    // Show the vendor's display label (e.g. "LinkAI") instead of the raw
    // id ("linkai") when we know it. Falls back to the id when the
    // provider isn't in our vendor table (rare).
    const provMeta = modelsState.providers.find(p => p.id === fbProv);
    const fbProvLabel = (provMeta && localizedLabel(provMeta.label)) || fbProv;
    const fbText = fbModel ? `${fbProvLabel} / ${fbModel}` : fbProvLabel;
    slot.innerHTML = `
        <p class="flex items-center gap-1.5 text-xs text-slate-400 dark:text-slate-500 min-w-0">
            <i class="fas fa-circle-info text-[10px] flex-shrink-0"></i>
            <span class="flex-shrink-0">${t('models_auto_using')}</span>
            <span class="font-mono text-slate-500 dark:text-slate-400 truncate">${escapeHtml(fbText)}</span>
        </p>`;
}

function buildCapabilityProviderOptions(def, cap) {
    // Show ALL vendors in capability dropdowns so users can see at a glance
    // who's configured (green check) and who isn't (gray dot, click to set
    // up). The list order puts configured vendors first; clicking an
    // unconfigured row opens the vendor modal in-place. ASR/TTS engines that
    // aren't tracked by PROVIDER_MODELS (azure/baidu/google etc.) are treated
    // as "always available" — no credential gate.
    const knownProviderMap = {};
    modelsState.providers.forEach(p => { knownProviderMap[p.id] = p; });

    const explicitList = cap.providers && cap.providers.length ? cap.providers : null;
    let providerIds = explicitList ? explicitList.slice() : modelsState.providers.map(p => p.id);
    if (cap.current_provider && !providerIds.includes(cap.current_provider)) {
        providerIds = [cap.current_provider, ...providerIds];
    }

    const opts = providerIds.map(pid => {
        const meta = knownProviderMap[pid];
        const tracked = !!meta;
        const configured = !tracked || !!meta.configured;
        return {
            value: pid,
            label: (meta && localizedLabel(meta.label)) || pid,
            _tracked: tracked,
            _configured: configured,
        };
    });

    opts.sort((a, b) => {
        if (a._configured === b._configured) return 0;
        return a._configured ? -1 : 1;
    });

    // Capabilities with a fallback ("auto") strategy expose it as a sentinel
    // option pinned to the top of the list. We use empty-string as the auto
    // value so the existing save handler propagates it untouched to the
    // backend, which interprets "" as "fall back to the main model".
    // Skip the sentinel when no real vendor is configured — "auto" would
    // route to nothing useful and the renderer will show "待配置" instead.
    const hasAnyConfigured = opts.some(o => o._configured);
    if ((cap.strategy === 'auto' || cap.strategy === 'specified') && hasAnyConfigured) {
        if (capabilitySupportsAuto(def.id)) {
            opts.unshift({
                value: '',
                label: t('models_strategy_auto'),
                _tracked: false,
                _configured: true,
                _isAuto: true,
            });
        }
    }
    return opts;
}

function capabilitySupportsAuto(capId) {
    // Embedding is intentionally NOT here: runtime only auto-falls back to
    // OpenAI/LinkAI, so dressing it up as "auto" hides reality from users.
    return capId === 'image' || capId === 'vision';
}

// After initDropdown renders the capability provider menu, decorate each
// row with the right-aligned configuration cue:
//   - configured rows: nothing extra — the .active marker (a brand-green ✓)
//     already comes from initDropdown's selected-state CSS for the row the
//     user currently picked. Other configured rows show no chrome, mirroring
//     a plain "switch to this" selector.
//   - unconfigured rows: a subdued gear icon hints at "click to configure".
//     The row's whole click handler is swapped to launch the vendor modal
//     in place rather than selecting an unusable value.
function decorateCapabilityProviderDropdown(def, ddEl, opts) {
    if (!ddEl) return;
    const menu = ddEl.querySelector('.cfg-dropdown-menu');
    if (!menu) return;

    const optByValue = {};
    opts.forEach(o => { optByValue[o.value] = o; });

    menu.querySelectorAll('.cfg-dropdown-item').forEach(item => {
        const value = item.dataset.value;
        const opt = optByValue[value];
        if (!opt) return;
        item.classList.add('cap-provider-item');
        if (!opt._configured) item.classList.add('cap-provider-unconfigured');

        // Wrap the label so the trailing affordance lines up via flex:auto.
        const labelText = item.textContent;
        item.textContent = '';
        const labelEl = document.createElement('span');
        labelEl.className = 'cap-provider-label';
        labelEl.textContent = labelText;
        item.appendChild(labelEl);

        if (!opt._configured) {
            // Trailing gear icon as the "configure this vendor" affordance.
            const gear = document.createElement('i');
            gear.className = 'fas fa-gear cap-provider-gear';
            item.appendChild(gear);
        }

        if (!opt._configured && opt._tracked) {
            // Hijack the click: open the vendor modal instead of selecting
            // an unusable value, and remember which capability the user was
            // configuring so the post-save reload can preselect the vendor.
            const newItem = item.cloneNode(true);
            item.replaceWith(newItem);
            newItem.addEventListener('click', (e) => {
                e.stopPropagation();
                ddEl.classList.remove('open');
                openVendorModal(value, (savedProviderId) => {
                    pendingCapabilitySelection = {
                        capabilityId: def.id,
                        providerId: savedProviderId || value,
                    };
                    loadModelsView({ preserveScroll: true });
                });
            });
        }
    });
}

// Lightweight decorator for the "add vendor" modal's provider picker:
// every configured vendor row gets a trailing brand-green ✓ so the user can
// see at a glance who's already set up, without having to read each row.
// Unlike decorateCapabilityProviderDropdown we don't hijack clicks here —
// picking an unconfigured vendor in this modal *is* the intended action.
function decorateVendorModalPicker(ddEl, opts) {
    if (!ddEl) return;
    const menu = ddEl.querySelector('.cfg-dropdown-menu');
    if (!menu) return;

    const optByValue = {};
    opts.forEach(o => { optByValue[o.value] = o; });

    menu.querySelectorAll('.cfg-dropdown-item').forEach(item => {
        const opt = optByValue[item.dataset.value];
        if (!opt) return;
        // Tag the row so the global active-row ✓ rule is suppressed in CSS
        // (otherwise configured AND selected rows would render two checks).
        item.classList.add('vendor-picker-item');
        if (opt._isAddNew) {
            // "Custom" is an add-new action (multiple entries allowed),
            // so show a trailing + instead of the configured ✓.
            const plus = document.createElement('i');
            plus.className = 'fas fa-plus vendor-picker-add-mark';
            item.appendChild(plus);
            return;
        }
        if (!opt._configured) return;
        const check = document.createElement('i');
        check.className = 'fas fa-check vendor-picker-configured-mark';
        item.appendChild(check);
    });
}

function rebuildCapabilityModelDropdown(def, providerId, selectedModel, scope) {
    // `scope` lets the caller (renderCapabilityBody) target a still-detached
    // subtree. After the card is mounted, callers may pass `document` instead.
    const root = scope || document;
    const el = root.querySelector(`#cap-${def.id}-model`);
    if (!el) return;

    // Prefer the capability-scoped model list when the backend provides one
    // (vision / image). It reflects the models the runtime can actually
    // dispatch to for this capability, instead of the vendor's full chat-
    // model catalog. Fall back to the generic provider.models for chat /
    // embedding / tts where any vendor model is fair game.
    //
    // Entries may be plain strings or {value, hint} objects (image catalog
    // uses the latter to surface brand aliases like "Nano Banana 2" next to
    // the technical Gemini model id). We normalize to {value, label, hint}
    // before handing off to initDropdown.
    const cap = modelsState.capabilities[def.id] || {};
    const capModelMap = cap.provider_models || {};
    let rawList;
    if (capModelMap[providerId]) {
        rawList = capModelMap[providerId].slice();
    } else if (providerId.startsWith('custom:') && capModelMap['custom']) {
        // Expanded custom:<id> entries share the same preset model list
        rawList = capModelMap['custom'].slice();
    } else {
        const provider = modelsState.providers.find(p => p.id === providerId);
        rawList = (provider && provider.models) ? provider.models.slice() : [];
    }
    const modelValues = [];
    const opts = rawList.map(entry => {
        if (typeof entry === 'string') {
            modelValues.push(entry);
            return { value: entry, label: entry };
        }
        modelValues.push(entry.value);
        return { value: entry.value, label: entry.label || entry.value, hint: entry.hint || '' };
    });
    opts.push({ value: '__custom__', label: currentLang === 'zh' ? '自定义' : 'Custom' });

    let initialValue = selectedModel || '';
    if (initialValue && !modelValues.includes(initialValue)) {
        initialValue = '__custom__';
    }
    if (!initialValue && opts.length) initialValue = opts[0].value;

    initDropdown(el, opts, initialValue, (value) => {
        const customWrap = document.getElementById(`cap-${def.id}-model-custom-wrap`);
        if (customWrap) {
            if (value === '__custom__') {
                customWrap.classList.remove('hidden');
                const input = document.getElementById(`cap-${def.id}-model-custom`);
                if (input && !input.value) input.value = selectedModel || '';
            } else {
                customWrap.classList.add('hidden');
            }
        }
        // TTS voice catalog may be scoped per engine model (aggregating
        // gateways). Rebuild the voice picker whenever the model changes.
        if (def.id === 'tts') {
            const provDd = document.getElementById('cap-tts-provider');
            const provId = provDd ? getDropdownValue(provDd) : '';
            rebuildCapabilityVoiceDropdown(provId, '', null, value);
        }
    });

    const customWrap = root.querySelector(`#cap-${def.id}-model-custom-wrap`);
    if (customWrap) {
        if (initialValue === '__custom__') {
            customWrap.classList.remove('hidden');
            const input = root.querySelector(`#cap-${def.id}-model-custom`);
            if (input) input.value = selectedModel || '';
        } else {
            customWrap.classList.add('hidden');
        }
    }
}

// TTS-only: rebuild the voice timbre picker against the provider's
// curated voice list. Hidden when no provider is picked.
//
// Each voice entry may be:
//   - a bare string  (code = label)
//   - {value, label, hint?}   so we can show a friendly Chinese name
//     while persisting the raw API code that the runtime sends.
function rebuildCapabilityVoiceDropdown(providerId, selectedVoice, scope, modelId) {
    const root = scope || document;
    const wrap = root.querySelector(`#cap-tts-voice-wrap`);
    const el = root.querySelector(`#cap-tts-voice`);
    if (!wrap || !el) return;
    const cap = modelsState.capabilities.tts || {};
    const voicesByProvider = cap.provider_voices || {};
    let raw = (providerId && voicesByProvider[providerId]) || [];
    // Some providers (gateways) scope voices by engine model id.
    if (raw && !Array.isArray(raw) && typeof raw === 'object') {
        const activeModel = modelId
            || (root.querySelector(`#cap-tts-model`) ? getDropdownValue(root.querySelector(`#cap-tts-model`)) : '');
        raw = (activeModel && raw[activeModel]) || [];
    }
    if (!raw || raw.length === 0) {
        wrap.classList.add('hidden');
        return;
    }
    wrap.classList.remove('hidden');
    // Voice picker: friendly name on the left, raw API code as right-hand
    // hint. Persisted/sent value is always the raw code.
    const codes = [];
    const opts = raw.map(entry => {
        if (typeof entry === 'string') {
            codes.push(entry);
            return { value: entry, label: entry };
        }
        codes.push(entry.value);
        const code = entry.value;
        const desc = entry.hint || entry.label || code;
        return {
            value: code,
            label: desc,
            hint: desc === code ? '' : code,
        };
    });
    opts.push({ value: '__custom__', label: currentLang === 'zh' ? '自定义' : 'Custom' });

    // Off-catalog values route through the custom branch.
    let initial = selectedVoice || '';
    const isCustom = initial && !codes.includes(initial);
    if (isCustom) initial = '__custom__';
    if (!initial) initial = codes[0];

    initDropdown(el, opts, initial, (value) => {
        const customWrap = root.querySelector(`#cap-tts-voice-custom-wrap`);
        if (!customWrap) return;
        if (value === '__custom__') {
            customWrap.classList.remove('hidden');
            const input = root.querySelector(`#cap-tts-voice-custom`);
            if (input && !input.value) input.value = isCustom ? selectedVoice : '';
        } else {
            customWrap.classList.add('hidden');
        }
    });

    const customWrap = root.querySelector(`#cap-tts-voice-custom-wrap`);
    if (customWrap) {
        if (initial === '__custom__') {
            customWrap.classList.remove('hidden');
            const input = root.querySelector(`#cap-tts-voice-custom`);
            if (input) input.value = isCustom ? selectedVoice : '';
        } else {
            customWrap.classList.add('hidden');
        }
    }
}

function onCapabilityProviderChange(def, providerId, scope) {
    if (def.needsModel) {
        // Before rebuilding the model picker for the newly picked provider,
        // stash the custom model the user had typed under the *previous*
        // provider, so switching back to it later restores that value.
        const prevProvider = capabilityLastProviderId[def.id];
        if (prevProvider && prevProvider !== providerId) {
            const prevDd = document.getElementById(`cap-${def.id}-model`);
            const prevInput = document.getElementById(`cap-${def.id}-model-custom`);
            if (prevDd && prevInput && getDropdownValue(prevDd) === '__custom__') {
                const typed = prevInput.value.trim();
                if (typed) capabilityCustomModelMemory[`${def.id}:${prevProvider}`] = typed;
            }
        }
        capabilityLastProviderId[def.id] = providerId;

        // Embedding: hide model picker when no provider is selected.
        const showModel = def.id === 'embedding' ? providerId !== '' :
            !(providerId === '' && capabilitySupportsAuto(def.id));
        if (showModel) {
            // Restore a remembered custom model for this provider (if any) so
            // switching vendors and back does not drop it.
            const remembered = capabilityCustomModelMemory[`${def.id}:${providerId}`] || '';
            rebuildCapabilityModelDropdown(def, providerId, remembered, scope);
        }
        setCapabilityModelPickerVisible(def, showModel, scope);
    }
    if (def.id === 'tts') {
        rebuildCapabilityVoiceDropdown(providerId, '', scope);
    }
    const body = scope || document.querySelector(`[data-cap-body="${def.id}"]`);
    if (body) {
        const cap = modelsState.capabilities[def.id] || {};
        renderCapabilityHints(def, cap, body, providerId);
    }
}

function getCapabilityModelValue(def) {
    if (!def.needsModel) return '';
    const dd = document.getElementById(`cap-${def.id}-model`);
    if (!dd) return '';
    const v = getDropdownValue(dd);
    if (v === '__custom__') {
        const input = document.getElementById(`cap-${def.id}-model-custom`);
        return input ? input.value.trim() : '';
    }
    return v || '';
}

// Opt-in capabilities: show/hide the pickers under the toggle without
// touching config. Mirrors the TTS reply-mode pattern — the toggle itself is
// pure UI state until the user presses Save.
function _setCapabilityPickersVisible(def, body, visible) {
    const wrap = body.querySelector(`#cap-${def.id}-pickers`);
    if (wrap) wrap.classList.toggle('hidden', !visible);
}

// Clicking the toggle flips the local switch. Persisting is a separate act
// (Save), so a user can flip back without ever writing to config.
function toggleCapabilityEnabled(capId) {
    const def = capabilityDefById(capId);
    if (!def || !def.toggleable) return;
    const cap = modelsState.capabilities[capId] || {};
    cap.enabled = !cap.enabled;
    modelsState.capabilities[capId] = cap;
    const btn = document.getElementById(`cap-${capId}-toggle`);
    if (btn) {
        btn.setAttribute('aria-checked', cap.enabled ? 'true' : 'false');
        btn.classList.toggle('bg-primary-500', cap.enabled);
        btn.classList.toggle('bg-slate-200', !cap.enabled);
        btn.classList.toggle('dark:bg-slate-700', !cap.enabled);
        const knob = btn.querySelector('span');
        if (knob) {
            knob.classList.toggle('translate-x-[18px]', cap.enabled);
            knob.classList.toggle('translate-x-[3px]', !cap.enabled);
        }
    }
    // Same lookup the rest of the file uses for a capability body.
    const body = document.querySelector(`[data-cap-body="${capId}"]`);
    if (body) _setCapabilityPickersVisible(def, body, cap.enabled);
}

function saveCapability(capId) {
    const def = capabilityDefById(capId);
    if (!def || !def.editable) return;
    // Search has its own form (strategy + provider, no model picker).
    if (capId === 'search') { saveSearchCapability(); return; }
    const provDd = document.getElementById(`cap-${capId}-provider`);
    const provider = provDd ? getDropdownValue(provDd) : '';
    // When the user is in auto mode (provider == ""), the model picker is
    // hidden and any value left in it is stale; persist an empty model so
    // the backend treats this as "fall back to the runtime chain".
    const isAuto = provider === '' && capabilitySupportsAuto(capId);
    // Embedding without a provider similarly means "cleared" — don't leak
    // a stale model value into config.
    const model = (isAuto || (capId === 'embedding' && !provider)) ? '' : getCapabilityModelValue(def);
    // TTS carries an extra voice timbre (supports free-text custom ids).
    let voice = '';
    if (capId === 'tts' && !isAuto) {
        const voiceDd = document.getElementById(`cap-${capId}-voice`);
        voice = voiceDd ? getDropdownValue(voiceDd) : '';
        if (voice === '__custom__') {
            const input = document.getElementById(`cap-${capId}-voice-custom`);
            voice = input ? input.value.trim() : '';
        }
    }

    // Embedding changes invalidate any pre-existing vector index because
    // dimensions / vendor differ. Gate the save behind a confirm, and on
    // success surface a dedicated info dialog telling the user how to
    // rebuild — both via the in-app custom dialog, not the native alert.
    if (capId === 'embedding') {
        const cap = modelsState.capabilities[capId] || {};
        const before = (cap.current_provider || '').trim();
        const after = (provider || '').trim();
        if (before !== after) {
            showConfirmDialog({
                title: t('models_embedding_change_title'),
                message: t('models_embedding_change_msg'),
                okText: t('save'),
                cancelText: t('cancel'),
                onConfirm: () => _persistCapability(capId, provider, model, () => {
                    showConfirmDialog({
                        title: t('models_embedding_saved_title'),
                        message: t('models_embedding_saved_msg'),
                        okText: t('models_embedding_saved_ok'),
                        hideCancel: true,
                        onConfirm: () => {
                            navigateTo('chat');
                            // Defer focus + value set: navigateTo may
                            // re-render the chat panel; setting value before
                            // the input is mounted would be lost.
                            setTimeout(() => {
                                const input = document.getElementById('chat-input');
                                if (!input) return;
                                input.value = '/memory rebuild-index';
                                input.focus();
                                // Trigger any input listeners (autosize, send-button enable, etc.)
                                input.dispatchEvent(new Event('input', { bubbles: true }));
                            }, 60);
                        },
                    });
                }),
            });
            return;
        }
    }
    // Opt-in capabilities persist their on/off switch alongside the pickers.
    // It is sent even when turning off, so a broken entry can always be
    // cleared — and the backend refuses to enable a half-filled one.
    let enabled = undefined;
    if (def.toggleable) {
        const cap = modelsState.capabilities[capId] || {};
        enabled = !!cap.enabled;
    }
    // The chat fallback is edited inside a modal; close it once the save
    // lands so the user drops straight back to the models page (already
    // reloaded by _persistCapability, which refreshes the main-card badge).
    const onAfterSuccess = capId === 'chat_fallback' ? closeChatFallbackModal : undefined;
    _persistCapability(capId, provider, model, onAfterSuccess, { voice, enabled });
}

function _persistCapability(capId, provider, model, onAfterSuccess, extras) {
    const payload = { action: 'set_capability', capability: capId, provider_id: provider, model: model };
    if (extras && extras.voice !== undefined) payload.voice = extras.voice;
    // Opt-in capabilities (the chat fallback) carry their on/off switch.
    if (extras && extras.enabled !== undefined) payload.enabled = extras.enabled;
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            // Flash "Saved" before reload so the status survives the rebuild.
            showStatus(`cap-${capId}-status`, 'models_save_success', false);
            setTimeout(() => {
                loadModelsView({ preserveScroll: true });
                if (onAfterSuccess) onAfterSuccess();
            }, 400);
        } else {
            showStatus(`cap-${capId}-status`, 'models_save_failed', true);
        }
    }).catch(() => showStatus(`cap-${capId}-status`, 'models_save_failed', true));
}

// ---------- Vendor credential modal ------------------------------------

let vendorModalState = { providerId: '', onSaved: null };

function openVendorModal(providerId, onSaved) {
    vendorModalState = { providerId: providerId || '', onSaved: onSaved || null };

    const overlay = document.getElementById('vendor-modal-overlay');
    const titleEl = document.getElementById('vendor-modal-title');
    const subEl = document.getElementById('vendor-modal-subtitle');
    const pickerWrap = document.getElementById('vendor-modal-picker-wrap');
    const baseWrap = document.getElementById('vendor-modal-base-wrap');
    const baseInput = document.getElementById('vendor-modal-base');
    const baseHint = document.getElementById('vendor-modal-base-hint');
    const keyInput = document.getElementById('vendor-modal-key');
    const clearBtn = document.getElementById('vendor-modal-clear');

    // Reset any leftover status (e.g. previous "Saved" message)
    const statusEl = document.getElementById('vendor-modal-status');
    if (statusEl) {
        statusEl.textContent = '';
        statusEl.classList.add('opacity-0');
    }

    if (!providerId) {
        // Add flow — show provider picker, default to the first unconfigured one.
        // We render every configured vendor with a trailing green ✓ via the
        // dropdown decorator, mirroring the visual language used by the
        // capability provider dropdowns. The .active row already shows the
        // currently selected vendor via its own background highlight, so we
        // intentionally suppress the global active-row ✓ for this picker
        // (see CSS) — otherwise configured + selected rows would show two.
        // Expanded custom provider cards ("custom:<id>") are edited via their
        // dedicated modal, so they are excluded from this picker. Picking the
        // "custom" entry creates a *new* custom provider via that modal —
        // this is how multiple OpenAI-compatible endpoints are added.
        const builtinProviders = modelsState.providers.filter(p => !isCustomProviderCard(p));
        const pickerOpts = builtinProviders.map(p => ({
            value: p.id,
            label: localizedLabel(p.label),
            _configured: !!p.configured,
        }));
        // In multi-provider mode the backend replaces the bare "custom" card
        // with the expanded ones; re-add it here so the entry stays available.
        if (!pickerOpts.some(o => o.value === 'custom')) {
            pickerOpts.push({ value: 'custom', label: t('models_custom_vendor_label'), _configured: false });
        }
        // "Custom" always behaves as an add-new action (multiple entries
        // allowed), so it shows a + mark instead of the configured ✓.
        pickerOpts.forEach(o => { if (o.value === 'custom') { o._isAddNew = true; o._configured = false; } });
        const unconfigured = builtinProviders.filter(p => !p.configured);
        const defaultId = (unconfigured[0] && unconfigured[0].id) || (builtinProviders[0] && builtinProviders[0].id) || 'custom';
        pickerWrap.classList.remove('hidden');
        const pickerEl = document.getElementById('vendor-modal-picker');
        const onPick = (val) => {
            if (val === 'custom') {
                // "Custom" in the add flow always creates a new
                // OpenAI-compatible provider entry via the dedicated modal
                // (name + base + key), supporting multiple custom endpoints.
                closeVendorModal();
                openCustomProviderModal('');
                return;
            }
            fillVendorModalForProvider(val);
        };
        initDropdown(pickerEl, pickerOpts, defaultId, onPick);
        decorateVendorModalPicker(pickerEl, pickerOpts);
        onPick(defaultId);
    } else {
        pickerWrap.classList.add('hidden');
        fillVendorModalForProvider(providerId);
    }

    overlay.classList.remove('hidden');

    document.getElementById('vendor-modal-cancel').onclick = closeVendorModal;
    document.getElementById('vendor-modal-save').onclick = saveVendorModal;
    clearBtn.onclick = clearVendorModal;

    // Once the user edits the masked value, drop the "masked sentinel" dataset
    // so the save handler treats their input as a real new key. We compare on
    // the next tick because keydown fires before the new char lands in .value.
    keyInput.oninput = function () {
        if (keyInput.dataset.masked === '1' && keyInput.value !== keyInput.dataset.maskedVal) {
            keyInput.dataset.masked = '';
        }
    };

    function onOverlayClick(e) {
        if (e.target === overlay) {
            closeVendorModal();
            overlay.removeEventListener('click', onOverlayClick);
        }
    }
    overlay.addEventListener('click', onOverlayClick);
    keyInput.focus();
}

function fillVendorModalForProvider(providerId) {
    const meta = modelsState.providers.find(p => p.id === providerId);
    if (!meta) return;
    document.getElementById('vendor-modal-title').textContent = localizedLabel(meta.label);
    document.getElementById('vendor-modal-subtitle').textContent = meta.id;

    // LinkAI aggregates many vendors, so only for it do we surface a link to its
    // console for creating/managing the aggregated key. Other providers manage
    // their keys on their own sites.
    const manageKey = document.getElementById('vendor-modal-manage-key');
    if (manageKey) manageKey.classList.toggle('hidden', meta.id !== 'linkai');

    // ----- API Base -----
    // Always reflect the *current effective* base as the input value so the
    // user can see (and edit) what's in use today. Placeholder is reserved
    // strictly for the "not yet typed anything" state and shows the official
    // default — never mixed with the actual value.
    const baseWrap = document.getElementById('vendor-modal-base-wrap');
    const baseInput = document.getElementById('vendor-modal-base');
    const baseHint = document.getElementById('vendor-modal-base-hint');
    if (meta.api_base_field) {
        baseWrap.classList.remove('hidden');
        baseInput.placeholder = meta.api_base_default || meta.api_base_placeholder || '';
        baseInput.value = meta.api_base || '';
        baseHint.classList.add('hidden');
    } else {
        baseWrap.classList.add('hidden');
        baseInput.value = '';
    }

    // ----- API Key -----
    // For configured vendors, surface the masked key as the input *value* so
    // it shows up in the same dark text as a real entry — making "configured"
    // visually unambiguous. The masked form (e.g. "sk-r***zRU") is also a
    // sentinel: the save handler treats untouched masked input as "no change".
    const keyInput = document.getElementById('vendor-modal-key');
    if (meta.configured && meta.api_key_masked) {
        keyInput.value = meta.api_key_masked;
        keyInput.dataset.masked = '1';
        keyInput.dataset.maskedVal = meta.api_key_masked;
        keyInput.placeholder = '';
    } else {
        keyInput.value = '';
        keyInput.dataset.masked = '';
        keyInput.dataset.maskedVal = '';
        keyInput.placeholder = 'sk-...';
    }

    const clearBtn = document.getElementById('vendor-modal-clear');
    clearBtn.classList.toggle('hidden', !meta.configured);

    vendorModalState.providerId = providerId;
}

function closeVendorModal() {
    document.getElementById('vendor-modal-overlay').classList.add('hidden');
}

function saveVendorModal() {
    const providerId = vendorModalState.providerId;
    if (!providerId) return;
    const keyInput = document.getElementById('vendor-modal-key');
    const apiBase = document.getElementById('vendor-modal-base').value.trim();

    // Treat "input still equals the masked value we surfaced on open" as "no
    // change" — the backend uses missing/empty api_key to skip the field.
    let apiKey = keyInput.value.trim();
    const masked = keyInput.dataset.masked === '1';
    const maskedVal = keyInput.dataset.maskedVal || '';
    if (masked && apiKey === maskedVal) {
        apiKey = '';
    }

    if (!apiKey && !masked) {
        // First-time setup with no key entered → nudge the user.
        keyInput.focus();
        return;
    }

    const btn = document.getElementById('vendor-modal-save');
    btn.disabled = true;
    const payload = { action: 'set_provider', provider_id: providerId, api_base: apiBase };
    if (apiKey) payload.api_key = apiKey;
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        btn.disabled = false;
        if (data.status !== 'success') {
            showStatus('vendor-modal-status', 'models_save_failed', true);
            return;
        }
        const finish = () => {
            closeVendorModal();
            const onSaved = vendorModalState.onSaved;
            if (onSaved) {
                try { onSaved(providerId); } catch (e) { /* noop */ }
            } else {
                loadModelsView();
            }
        };
        finish();
    }).catch(() => {
        btn.disabled = false;
        showStatus('vendor-modal-status', 'models_save_failed', true);
    });
}

function clearVendorModal() {
    const providerId = vendorModalState.providerId;
    if (!providerId) return;
    showConfirmDialog({
        title: t('models_clear_confirm_title'),
        message: t('models_clear_confirm_msg'),
        okText: t('models_clear_credential'),
        cancelText: t('cancel'),
        onConfirm: () => {
            fetch('/api/models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'delete_provider', provider_id: providerId }),
            }).then(r => r.json()).then(data => {
                if (data.status === 'success') {
                    closeVendorModal();
                    loadModelsView();
                } else {
                    showStatus('vendor-modal-status', 'models_clear_failed', true);
                }
            }).catch(() => showStatus('vendor-modal-status', 'models_clear_failed', true));
        }
    });
}

