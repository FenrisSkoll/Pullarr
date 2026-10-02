/* Observational Calendar: backend identity/precision, explicit existing Add Volume only. */
const CalendarUI = (() => {
    const label = value => String(value ?? '').replaceAll('_', ' ');
    function text(parent, tag, value = '') { const e = document.createElement(tag); e.textContent = String(value ?? ''); parent.appendChild(e); return e; }
    function button(parent, title, action, disabled = false) { const e = text(parent, 'button', title); e.type = 'button'; e.disabled = disabled; e.onclick = action; return e; }
    function dateLabel(value) { return value.date ? `${value.date} (${label(value.precision)} precision · ${label(value.kind)})` : 'TBA / Date Unknown — no usable release date'; }
    function iso(value) { return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`; }
    class Controller {
        constructor(root, api, sleep = ms => new Promise(r => setTimeout(r, ms))) { this.root = root; this.api = api; this.sleep = sleep; this.generations = {}; this.pending = false; this.offset = 0; }
        el(id) { return this.root.querySelector('#calendar-' + id); }
        version(key) { return this.generations[key] = (this.generations[key] || 0) + 1; }
        current(key, version) { return this.generations[key] === version; }
        message(value) { this.el('message').textContent = value; }
        error(error) { const messages = {bounded: 'Calendar reached a safe bound. Select one provider or a narrower scope; completeness is not claimed.',
            range_bounded: 'Choose a date range of at most 366 days.', sync_active: 'A Calendar refresh is already active.',
            not_found: 'This subject is no longer in the accepted monitored scope.', invalid_date: 'Enter valid calendar dates.',
            invalid_request: 'Check the Calendar filters.', task_unavailable: 'Calendar refresh could not be queued.'};
            this.message(messages[error?.reason] || 'Calendar could not complete this request. Existing evidence and library state are preserved.'); }
        start() {
            this.el('filters').onsubmit = e => { e.preventDefault(); this.load(0); };
            this.el('refresh').onclick = () => this.refresh();
            this.el('close').onclick = () => this.el('dialog').close();
            this.el('dialog').onclose = () => { this.version('detail'); this.invoker?.focus(); };
            this.load(0);
        }
        filters() {
            const horizon = this.el('horizon').value, today = new Date(), from = new Date(today), to = new Date(today);
            if (horizon === 'recent') from.setDate(from.getDate() - 30); else to.setDate(to.getDate() + (Number(horizon) || 90));
            const q = {limit: 50, unknown: horizon === 'unknown' ? 'true' : 'false'};
            if (horizon === 'custom') { if (this.el('from').value) q.from = this.el('from').value; if (this.el('to').value) q.to = this.el('to').value; }
            else { q.from = iso(from); q.to = iso(to); }
            for (const key of ['scope', 'provider', 'precision']) if (this.el(key).value) q[key] = this.el(key).value;
            return q;
        }
        async load(offset = 0) {
            const generation = this.version('page'); this.offset = offset;
            try { const page = await this.api('GET', '/calendar', null, {...this.filters(), offset}); if (!this.current('page', generation)) return;
                const c = this.el('results'); c.replaceChildren();
                text(c, 'h2', `Release agenda · ${page.total} observations`);
                if (!page.items.length) text(c, 'p', 'No release observations for these filters. Try TBA / Date Unknown or refresh known monitored publications. This is not proof that no releases exist.');
                const seen = new Set();
                for (const item of page.items) { if (seen.has(item.id)) continue; seen.add(item.id);
                    const card = text(c, 'article'); card.dataset.eventId = item.id;
                    text(card, 'h3', `${item.publication_title}${item.issue_number ? ' #' + item.issue_number : ''}`);
                    if (item.title !== item.publication_title) text(card, 'p', item.title);
                    text(card, 'p', dateLabel(item.effective));
                    text(card, 'p', `${item.status === 'in_library' ? 'In Library' : item.status === 'ambiguous' ? 'Link Ambiguous' : 'Not in Library'} · ${label(item.kind)} · ${label(item.effective.provider || 'No date source')}`);
                    if (item.file_owned !== null) text(card, 'p', `Direct file: ${item.file_owned ? 'owned' : 'not owned'} · Canonical content: ${item.content_represented ? 'represented' : 'not represented'} · Existing Wanted: ${item.wanted ? 'yes' : 'no'}`);
                    text(card, 'p', `Interest: ${item.monitoring_sources.map(label).join(', ')}${item.memberships.length ? ' · Collections: ' + item.memberships.map(m => m.node.title).join(', ') : ''}`);
                    if (item.multiple_source_dates) text(card, 'p', 'Multiple source dates — inspect evidence.');
                    if (item.stale) text(card, 'p', 'Stale evidence — not a cancellation.');
                    const actions = text(card, 'div'); actions.className = 'calendar-actions';
                    button(actions, 'Release details', () => this.detail(item.id));
                    if (item.volume_id) { const a = text(actions, 'a', 'Open local volume'); a.href = `${typeof url_base === 'undefined' ? '' : url_base}/volumes/${item.volume_id}`; }
                }
                button(c, 'Previous releases', () => this.load(Math.max(0, offset - 50)), offset === 0);
                button(c, 'Next releases', () => this.load(offset + 50), !page.has_next);
                const sync = page.latest_sync;
                this.el('freshness').textContent = sync ? `Last observation task: ${label(sync.state)} · ${sync.processed}/${sync.total} known subjects. Provider success is separate from task completion.` : 'No Calendar sync yet. Canonical library dates are shown locally.';
            } catch (error) { if (this.current('page', generation)) this.error(error); }
        }
        async detail(id) {
            const v = this.version('detail'); this.invoker = document.activeElement;
            try { const item = await this.api('GET', '/calendar/events/' + encodeURIComponent(id)); if (!this.current('detail', v)) return;
                const c = this.el('dialog-body'); c.replaceChildren();
                this.el('dialog-title').textContent = item.publication_title;
                text(c, 'p', dateLabel(item.effective)); text(c, 'p', item.date_policy);
                text(c, 'p', 'Date observations do not establish file ownership. Cover/publication dates are not necessarily on-sale dates.');
                const wrap = text(c, 'div'); wrap.className = 'calendar-table'; const table = text(wrap, 'table'), head = text(text(table, 'thead'), 'tr');
                for (const h of ['Source', 'Date', 'Kind', 'Current', 'Previously', 'Observed']) text(head, 'th', h).scope = 'col';
                const body = text(table, 'tbody');
                for (const e of item.evidence) { const row = text(body, 'tr'); for (const value of [e.provider, dateLabel(e), label(e.kind), e.current ? 'Observed' : 'Stale / not observed', e.previous_date || '—', e.fetched_at ? new Date(e.fetched_at * 1000).toISOString() : 'Current canonical facts']) text(row, 'td', value); }
                if (!item.evidence.length) text(c, 'p', 'No usable provider date yet. Multi-issue families are not assigned an inferred publication date.');
                if (item.content_context) text(c, 'p', `Separate C2 context: ${label(item.content_context.state)} (known confirmed claims only). This does not establish publication ownership.`);
                for (const m of item.memberships) text(c, 'p', `Collection: ${m.node.title} · ${label(m.source)} · ${m.evidence.explanation || 'Accepted local membership'}`);
                if (item.publication_id && item.status === 'external' && item.refs.length) button(c, 'Add to Library', () => this.add(item));
                this.el('dialog').showModal(); this.el('close').focus();
            } catch (error) { if (this.current('detail', v)) this.error(error); }
        }
        async refresh() {
            if (this.pending) return; this.pending = true; this.el('refresh').disabled = true;
            try { let task = await this.api('POST', '/calendar/refresh', {provider: this.el('refresh-provider').value});
                for (let tries = 0; tries < 450 && ['queued', 'running'].includes(task.state); tries++) {
                    this.message(`Calendar ${label(task.state)} · ${task.processed || 0}/${task.total || '?'} observed. No acquisition actions.`);
                    await this.sleep(2000); task = await this.api('GET', '/calendar/tasks/' + task.id);
                }
                this.message(`Calendar refresh: ${label(task.state)}. ${task.state === 'partial' ? 'Some sources failed; prior evidence is retained.' : ''}`);
                await this.load(this.offset);
            } catch (error) { this.error(error); }
            finally { this.pending = false; this.el('refresh').disabled = false; }
        }
        async add(item) {
            if (this.pending) return;
            const v = this.version('add');
            try { const roots = await this.api('GET', '/rootfolder'); if (!this.current('add', v) || !this.el('dialog').open) return;
                const c = this.el('dialog-body'); c.replaceChildren();
                text(c, 'p', `Add ${item.publication_title} through the normal Add Volume pipeline. No automatic search; local monitoring remains off.`);
                const select = (title, values) => { const l = text(c, 'label', title), s = text(l, 'select'); s.setAttribute('aria-label', title); for (const [value, name] of values) { const o = text(s, 'option', name); o.value = value; } return s; };
                const root = select('Configured root', roots.map(r => [r.id, r.folder])), provider = select('Exact provider', item.refs.map(r => [r.provider, r.provider]));
                const confirm = button(c, 'Confirm Add to Library', async () => {
                    if (this.pending) return; this.pending = true; confirm.disabled = true;
                    try { let result = await this.api('POST', `/collections/publications/${item.publication_id}/add`, {root_id: Number(root.value), provider: provider.value, confirmed: true});
                        for (let tries = 0; tries < 450 && ['queued', 'running'].includes(result.state); tries++) { await this.sleep(2000); result = await this.api('GET', '/collections/tasks/' + result.id); }
                        if (result.state !== 'complete') throw {reason: result.reason};
                        this.el('dialog').close(); this.message('Publication added. The Calendar identity and Collection membership are preserved.'); await this.load(this.offset);
                    } catch (error) { this.error(error); text(c, 'p', 'If the response was lost, reload release details to check the exact library link before retrying.'); }
                    finally { this.pending = false; confirm.disabled = false; }
                }, !roots.length); confirm.focus();
            } catch (error) { this.error(error); }
        }
    }
    return {Controller, dateLabel, text, iso};
})();
if (typeof module !== 'undefined') module.exports = CalendarUI;
if (typeof usingApiKey === 'function') usingApiKey().then(key => {
    const api = async (method, path, body = null, params = {}) => {
        const response = await fetch(`${url_base}/api${path}?${new URLSearchParams({api_key: key, ...params})}`, {method,
            headers: body === null ? {} : {'Content-Type': 'application/json'}, body: body === null ? undefined : JSON.stringify(body)});
        if (response.status === 401) { location.href = `${url_base}/login`; throw {reason: 'authentication_required'}; }
        const data = await response.json(); if (!response.ok) throw data.result; return data.result;
    };
    new CalendarUI.Controller(document.getElementById('calendar'), api).start();
});
