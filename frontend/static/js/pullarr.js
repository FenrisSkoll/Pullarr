/* Shared presentation only: domain requests and revision checks stay with pages. */
(() => {
    'use strict';
    const main = document.querySelector('main');
    const nav = document.querySelector('#nav-bar');
    const toggle = document.querySelector('#toggle-nav');
    const backdrop = document.querySelector('#nav-backdrop');
    const narrow = window.matchMedia('(max-width: 800px)');
    const base = document.querySelector('#url_base')?.dataset.value || '';
    document.addEventListener('error', event => {
        const image = event.target;
        if (image instanceof HTMLImageElement && image.matches('.list-img, .cover-info-container img, #volume-cover, .volume-info > img') && !image.dataset.fallback) {
            image.dataset.fallback = 'true';
            image.src = base + '/static/img/cover-placeholder.svg';
        }
    }, true);
    const focusable = root => [...root.querySelectorAll(
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex="0"]'
    )].filter(el => el.getClientRects().length && !el.closest('[inert]'));
    let navOpen = false;
    function setNavigation(open, restore = true) {
        navOpen = open && narrow.matches;
        nav.classList.toggle('show-nav', navOpen);
        toggle.setAttribute('aria-expanded', String(navOpen));
        toggle.setAttribute('aria-label', navOpen ? 'Close navigation' : 'Open navigation');
        backdrop.hidden = !navOpen;
        nav.inert = narrow.matches && !navOpen;
        if (main) main.inert = navOpen;
        if (navOpen) (nav.querySelector('[aria-current="page"]') || focusable(nav)[0])?.focus();
        else if (restore) toggle.focus();
    }
    if (nav && toggle && backdrop) {
        toggle.onclick = () => setNavigation(!navOpen);
        backdrop.onclick = () => setNavigation(false);
        narrow.addEventListener('change', () => setNavigation(false, false));
        setNavigation(false, false);
        document.addEventListener('keydown', event => {
            if (!navOpen) return;
            if (event.key === 'Escape') { event.preventDefault(); setNavigation(false); }
            if (event.key === 'Tab') {
                const items = [toggle, ...focusable(nav)];
                const first = items[0], last = items.at(-1);
                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
            }
        });
    }
    if (main) {
        main.tabIndex = -1;
        const labelTables = () => main.querySelectorAll('.q-table,.maintenance-table,.issues-table-container,.remote-mapping-container,.table-container,.container:has(table),#d-content,#table-container').forEach(region => {
            region.tabIndex = 0;
            region.setAttribute('role', 'region');
            region.setAttribute('aria-label', 'Scrollable results table');
        });
        labelTables();
        new MutationObserver(labelTables).observe(main, {childList: true, subtree: true});
        document.querySelector('.skip-link')?.addEventListener('click', event => {
            event.preventDefault();
            main.focus();
            main.scrollIntoView({block: 'start'});
        });
    }
    // Legacy dialogs share focus/inert behavior without changing their controller API.
    const overlay = document.querySelector('.window');
    let invoker = null, activeDialog = null;
    function syncDialog() {
        const current = overlay?.hasAttribute('show-window')
            ? overlay.querySelector('section[show-window]') : null;
        if (current === activeDialog) return;
        if (current) {
            if (!activeDialog) invoker = document.activeElement;
            current.setAttribute('role', 'dialog');
            current.setAttribute('aria-modal', 'true');
            current.tabIndex = -1;
            if (!current.hasAttribute('aria-label')) current.setAttribute('aria-label', current.querySelector('h2,p')?.textContent || 'Review');
            document.querySelector('header').inert = true;
            document.querySelector('.nav-main').inert = true;
            (focusable(current)[0] || current).focus();
        } else if (activeDialog) {
            document.querySelector('header').inert = false;
            document.querySelector('.nav-main').inert = false;
            if (invoker?.isConnected) invoker.focus();
            invoker = null;
        }
        activeDialog = current;
    }
    if (overlay) new MutationObserver(syncDialog).observe(overlay, {attributes: true, subtree: true, attributeFilter: ['show-window']});
    document.addEventListener('keydown', event => {
        if (!activeDialog || event.key !== 'Tab') return;
        const items = focusable(activeDialog);
        if (!items.length) { event.preventDefault(); activeDialog.focus(); return; }
        if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1).focus(); }
        else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0].focus(); }
    });
})();
