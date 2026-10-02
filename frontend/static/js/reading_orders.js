/* Exact server-owned review identities. Text-only rendering; no remote HTML. */
const ReadingOrdersUI = (() => {
    const label = value => String(value ?? '').replaceAll('_', ' ');
    function text(parent, tag, value = '') { const el = document.createElement(tag); el.textContent = String(value ?? ''); parent.appendChild(el); return el; }
    function input(parent, title, value = '', type = 'text') { const l = text(parent, 'label', title), e = text(l, 'input'); e.type = type; e.value = value; e.setAttribute('aria-label', title); return e; }
    function button(parent, title, fn, disabled = false) { const b = text(parent, 'button', title); b.type = 'button'; b.disabled = disabled; b.onclick = fn; return b; }
    function table(parent, headers) { const wrap = text(parent, 'div'); wrap.className = 'ro-table'; const t = text(wrap, 'table'), h = text(text(t, 'thead'), 'tr'); headers.forEach(v => { text(h, 'th', v).scope = 'col'; }); return text(t, 'tbody'); }
    class Controller {
        constructor(root, api, upload, download, sleep = ms => new Promise(r => setTimeout(r, ms))) {
            Object.assign(this, {root, api, upload, download, sleep, generations: {}, busy: false, selected: new Set()});
        }
        el(id) { return this.root.querySelector('#ro-' + id); }
        version(key) { return this.generations[key] = (this.generations[key] || 0) + 1; }
        current(key, v) { return this.generations[key] === v; }
        message(value) { this.el('message').textContent = value; }
        error(e) { const messages = {review_expired: 'This review expired or the application restarted. Create a fresh review. Accepted orders and pending source revisions remain available.',
            revision_conflict: 'The order or source changed. Reload and review again; no stale edit was merged.', stale: 'Library identity or ownership changed. Review again.',
            detach_required: 'Detach this source-managed sequence before making local structural changes.', blocked_destination: 'Only public HTTPS CBL destinations without credentials or query strings are supported.',
            bounded: 'This request exceeds a safe limit. Orders support at most 2,000 entries and reviews/actions are bounded.',
            unsupported_source_order: 'This provider does not expose an admitted ordered-list contract. Import an explicitly ordered CBL instead.',
            unsafe_xml: 'DTD/entity XML is not supported.', invalid_xml: 'This file is not valid supported XML.', unsupported_cbl: 'This source is not a supported CBL ReadingList.'};
            this.message(messages[e?.reason] || 'The request could not complete. Reload durable order/source state before retrying an uncertain change.'); }
        async perform(fn, mutation = false, control = null) {
            if (mutation && this.busy) { this.message('An operation is still pending. Wait for its result before confirming another change.'); return; }
            if (mutation) { this.busy = true; this.message('Working…'); if (control) control.disabled = true; }
            try { return await fn(); } catch (e) { this.error(e); }
            finally { if (mutation) { this.busy = false; if (control) control.disabled = false; } }
        }
        action(parent, title, fn, mutation = false, disabled = false) { let b; b = button(parent, title, () => this.perform(fn, mutation, b), disabled); return b; }
        remember(value) { try { sessionStorage.setItem('reading-order-handle', JSON.stringify(value)); } catch (_) { /* Storage is optional. */ } }
        start() {
            this.el('list').onclick = () => this.perform(() => this.list());
            this.el('create').onclick = () => this.create();
            this.el('import').onclick = () => this.importDialog();
            this.el('provider').onclick = () => this.providerDialog();
            this.el('close').onclick = () => this.el('dialog').close();
            this.el('dialog').onclose = () => { this.version('dialog'); this.invoker?.focus(); };
            let saved; try { saved = JSON.parse(sessionStorage.getItem('reading-order-handle')); } catch (_) { /* Empty storage. */ }
            this.perform(() => saved?.order ? this.order(saved.order) : saved?.review ? this.review(saved.review) : this.list());
        }
        dialog(title) { this.version('dialog'); this.invoker = document.activeElement; this.el('dialog-title').textContent = title; const c = this.el('dialog-body'); c.replaceChildren(); if (!this.el('dialog').open) this.el('dialog').showModal(); this.el('close').focus(); return c; }
        confirm(title, description, action) { const c = this.dialog(title), generation = this.generations.dialog; text(c, 'p', description); this.action(c, title, async () => { await action(); if (this.current('dialog', generation)) this.el('dialog').close(); }, true).focus(); }
        pager(c, page, load) { this.action(c, 'Previous page', () => load(Math.max(0, page.offset-50)), false, !page.offset); this.action(c, 'Next page', () => load(page.offset+50), false, !page.has_next); }
        async list(offset = 0) {
            const v = this.version('page'), page = await this.api('GET', '/reading-orders', null, {offset, limit: 50}); if (!this.current('page', v)) return;
            this.remember({}); const c = this.el('content'); c.replaceChildren(); text(c, 'h2', 'Your Reading Orders');
            if (!page.items.length) text(c, 'p', 'No Reading Orders yet. Create a manual order or review a CBL import.');
            const t = table(c, ['Title', 'Source', 'Entries / exact ownership', 'Action']);
            for (const row of page.items) { const tr = text(t, 'tr'); text(tr, 'td', row.title); text(tr, 'td', label(row.source_kind || 'manual / imported'));
                text(tr, 'td', `${row.entry_count} entries · ${row.counts.owned || 0} owned · ${row.counts.missing || 0} missing · ${row.counts.external || 0} external · ${(row.counts.unresolved || 0)+(row.counts.ambiguous || 0)} unresolved/ambiguous`);
                this.action(text(tr, 'td'), 'Open ' + row.title, () => { this.selected.clear(); return this.order(row.id); }); }
            this.pager(c, page, n => this.list(n));
        }
        create() {
            const c = this.dialog('Create Reading Order'), title = input(c, 'Title'), description = input(c, 'Description');
            this.action(c, 'Create', async () => { const value = await this.api('POST', '/reading-orders', {title: title.value, description: description.value}); this.el('dialog').close(); await this.order(value.id); }, true); title.focus();
        }
        async order(id, offset = 0, status = 'all', expected = null) {
            if (expected !== null && !this.current('page', expected)) return;
            const v = this.version('page'), page = await this.api('GET', `/reading-orders/${id}/entries`, null, {offset, limit: 50, status}); if (!this.current('page', v)) return;
            this.remember({order: id}); this.active = page.order; this.offset = offset; this.status = status;
            const o = page.order, locked = Boolean(o.source?.enabled), c = this.el('content'); c.replaceChildren(); text(c, 'h2', o.title); text(c, 'p', o.description);
            text(c, 'p', `Revision ${o.revision}. ${locked ? 'Source-managed — detach before local structural edits.' : 'Local sequence — dates and provider refresh never reorder entries.'}`);
            const actions = text(c, 'div'); actions.className = 'ro-actions';
            this.action(actions, 'Add Local Issue', () => this.picker(issue => this.api('POST', `/reading-orders/${id}/entries`, {revision: o.revision, issue_id: issue.id}).then(() => this.order(id, 0, 'all', v))), false, locked);
            this.action(actions, 'Edit Title / Description', () => { const d = this.dialog('Edit Reading Order'), title = input(d, 'Title', o.title), description = input(d, 'Description', o.description); this.action(d, 'Save', async () => { await this.api('POST', `/reading-orders/${id}`, {revision: o.revision, title: title.value, description: description.value}); this.el('dialog').close(); await this.order(id); }, true); }, false, locked);
            this.action(actions, 'Export CBL', () => this.download(id));
            if (!o.source) this.action(actions, 'Subscribe to CBL URL', () => { const d = this.dialog('Subscribe to CBL URL'), url = input(d, 'Public HTTPS CBL URL', '', 'url'); text(d, 'p', 'Existing entries remain unchanged until an update is reviewed and accepted. Local structural edits require Detach. Private, signed and query-string feeds are not supported.'); this.action(d, 'Attach Subscription', async () => { await this.api('POST', `/reading-orders/${id}/subscriptions`, {revision: o.revision, url: url.value}); this.el('dialog').close(); await this.order(id); }, true); });
            if (o.source) {
                text(c, 'p', `Source: ${label(o.source.kind)} · ${o.source.locator} · ${o.source.enabled ? 'Enabled' : 'Detached'} · Last successful refresh: ${o.source.success_at ? new Date(o.source.success_at*1000).toISOString() : 'Never'} · ${label(o.source.error || 'No source error')}`);
                this.action(actions, 'Refresh Source', async () => { const generation = this.generations.page; const task = await this.api('POST', `/reading-orders/sources/${o.source.id}/refresh`, {}); await this.poll(task); if (this.current('page', generation)) await this.order(id); }, true, !locked);
                this.action(actions, 'Review Source Changes', () => this.source(o.source.id));
                this.action(actions, 'Detach from Source', () => this.confirm('Detach from Source', 'Preserve the accepted sequence and provenance. Disable future source updates and permit local editing.', async () => { await this.api('POST', `/reading-orders/${id}/detach`, {revision: o.revision, confirmed: true}); await this.order(id); }), false, !locked);
            }
            this.action(actions, 'Send Missing to Wanted', async () => { if (!this.selected.size) { this.message('Select up to 50 entries first.'); return; } const review = await this.api('POST', `/reading-orders/${id}/wanted-preview`, {revision: o.revision, entries: [...this.selected]}); this.wanted(review); });
            this.action(actions, 'Delete Reading Order', () => this.confirm('Delete Reading Order', 'Delete only this sequence and its subscriptions. Library issues, files, Collections, Calendar and acquisition history remain unchanged.', async () => { await this.api('POST', `/reading-orders/${id}/delete`, {revision: o.revision, confirmed: true}); await this.list(); }));
            const filterLabel = text(c, 'label', 'Entry status'), filter = text(filterLabel, 'select'); filter.setAttribute('aria-label', 'Entry status');
            for (const value of ['all', 'owned', 'missing', 'external', 'ambiguous', 'unresolved', 'content']) { const op = text(filter, 'option', value === 'content' ? 'Content represented elsewhere (not exact ownership)' : label(value)); op.value = value; } filter.value = status; filter.onchange = () => this.perform(() => this.order(id, 0, filter.value));
            const t = table(c, ['Select', 'Position', 'Issue', 'Exact status / C2', 'Source', 'Actions']);
            for (const item of page.items) {
                const tr = text(t, 'tr'); tr.dataset.entryId = item.id; const check = input(text(tr, 'td'), `Select entry ${item.position+1}`, '', 'checkbox'); check.checked = this.selected.has(item.id);
                check.onchange = () => { if (check.checked && this.selected.size >= 50) { check.checked = false; this.message('Select at most 50 entries.'); } else if (check.checked) this.selected.add(item.id); else this.selected.delete(item.id); };
                text(tr, 'td', item.position+1); text(tr, 'td', `${item.series} #${item.number} (${item.year || 'year unknown'})`);
                text(tr, 'td', `${label(item.status)}${item.content_elsewhere ? ' · Content represented elsewhere — not exact issue ownership' : ''}${item.wanted ? ' · Already Wanted' : ''}`);
                text(tr, 'td', `${label(item.provenance.kind)} · ${item.refs.map(r => r.provider).join(', ') || 'Textual source'}`);
                const a = text(tr, 'td');
                this.action(a, 'Entry Evidence', () => { const d = this.dialog('Entry Evidence');
                    text(d, 'p', `Occurrence ${item.position+1} · ${label(item.provenance.kind)} · Match: ${label(item.match_kind)}`);
                    text(d, 'p', `Source: ${item.source_fields.series} #${item.source_fields.number} · Year ${item.source_fields.year || 'unknown'} · Volume ${item.source_fields.volume || 'unknown'}`);
                    for (const ref of item.refs) text(d, 'p', `${ref.provider}: issue ${ref.issue_id}${ref.volume_id ? ' · publication '+ref.volume_id : ''}`);
                    if (item.provenance.accepted_at) text(d, 'p', 'Accepted: '+new Date(item.provenance.accepted_at*1000).toISOString());
                    if (item.provenance.source_id) text(d, 'p', 'Source record '+item.provenance.source_id);
                    if (item.provenance.digest) { const details=text(d,'details'); text(details,'summary','Accepted source/review identity'); text(details,'p',item.provenance.digest); }
                    text(d, 'p', 'Exact issue ownership, C2 content availability and Wanted eligibility are separate.'); });
                this.action(a, 'Move Up', async () => { await this.api('POST', `/reading-orders/${id}/reorder`, {revision: o.revision, entry_id: item.id, position: item.position-1}); await this.order(id, offset, status, v); }, true, locked || item.position === 0);
                this.action(a, 'Move Down', async () => { await this.api('POST', `/reading-orders/${id}/reorder`, {revision: o.revision, entry_id: item.id, position: item.position+1}); await this.order(id, offset, status, v); }, true, locked || item.position >= page.total-1 && status === 'all');
                this.action(a, 'Remove Entry', () => this.confirm('Remove Entry', 'Remove this occurrence only. The issue and its files remain unchanged.', async () => { await this.api('POST', `/reading-orders/${id}/remove`, {revision: o.revision, entry_id: item.id}); this.selected.delete(item.id); await this.order(id, offset, status); }), false, locked);
                if (!item.canonical_id) this.action(a, 'Resolve to Local Issue', () => this.picker(issue => this.api('POST', `/reading-orders/${id}/resolve`, {revision: o.revision, entry_id: item.id, issue_id: issue.id}).then(() => this.order(id, offset, status, v))), false, locked);
                if (item.status === 'external' && item.refs.some(r => r.volume_id)) this.action(a, 'Add Publication to Library', () => this.addPublication(o, item));
                if (item.volume_id) { const link = text(a, 'a', 'Open Local Volume'); link.href = `${typeof url_base === 'undefined' ? '' : url_base}/volumes/${item.volume_id}`; }
            }
            if (!page.items.length) text(c, 'p', 'No entries for this filter.');
            this.pager(c, page, n => this.order(id, n, status));
        }
        picker(select) {
            const c = this.dialog('Choose Exact Local Issue'), q = input(c, 'Search local series / issue'), results = text(c, 'div');
            const load = async (offset = 0) => { const v = this.version('picker'), page = await this.api('GET', '/reading-orders/local-issues', null, {query: q.value, offset, limit: 50}); if (!this.current('picker', v) || !this.el('dialog').open) return; results.replaceChildren();
                for (const row of page.items) this.action(results, `${row.title} #${row.issue_number} (${row.year || 'unknown'})`, async () => { await select(row); this.el('dialog').close(); }, true);
                this.pager(results, page, load); };
            this.action(c, 'Search Local Issues', () => load()); q.focus();
        }
        importDialog() {
            const c = this.dialog('Import CBL'), file = input(c, 'CBL file', '', 'file'); file.accept = '.cbl,.xml';
            text(c, 'p', 'Maximum 2 MiB / 2,000 entries. Import creates a new order only after review. Unsupported extensions are reported, not executed.');
            this.action(c, 'Parse and Review', async () => { if (!file.files[0]) return; const review = await this.upload(file.files[0]); this.el('dialog').close(); await this.review(review.id); }, true); file.focus();
        }
        async review(id, offset = 0) {
            const v = this.version('page'), review = await this.api('GET', `/reading-orders/reviews/${id}`, null, {offset, limit: 50}); if (!this.current('page', v)) return;
            this.remember({review: id}); const c = this.el('content'); c.replaceChildren(); text(c, 'h2', 'Import Review: ' + review.title); text(c, 'p', review.description); text(c, 'p', `${review.total} entries. ${review.warnings.map(label).join('; ')}`);
            const t = table(c, ['Position', 'Source issue', 'Match', 'Action']);
            for (const item of review.items) { const tr = text(t, 'tr'); text(tr, 'td', item.position+1); text(tr, 'td', `${item.source.series} #${item.source.number} · ${item.source.refs.map(r=>r.provider+':'+r.issue_id).join(', ') || 'Textual reference only'}`); text(tr, 'td', `${label(item.match)}${item.repeated ? ' · Intentional repeat preserved' : ''}${item.candidates.length ? ' · '+item.candidates.length+' local candidate(s) — choose explicitly' : ''}`);
                this.action(text(tr, 'td'), 'Resolve Entry ' + (item.position+1), () => this.picker(async issue => { await this.api('POST', `/reading-orders/reviews/${id}/resolve`, {revision: review.revision, position: item.position, issue_id: issue.id}); await this.review(id, offset); })); }
            this.pager(c, review, n => this.review(id, n));
            this.action(c, 'Accept Reading Order', () => this.confirm('Accept Reading Order', `Create one new order with ${review.total} entries in the reviewed sequence, including repeats and unresolved source evidence. No monitoring, Wanted or downloads will change.`, async () => { const o = await this.api('POST', `/reading-orders/reviews/${id}/accept`, {revision: review.revision, expected_digest: review.digest, confirmed: true}); await this.order(o.id); }));
        }
        async source(id, offset = 0) {
            const v = this.version('page'), page = await this.api('GET', `/reading-orders/sources/${id}/pending`, null, {offset, limit: 50}); if (!this.current('page', v)) return;
            const c = this.el('content'); c.replaceChildren(); text(c, 'h2', 'Source Change Review'); if (!page.pending) { text(c, 'p', 'No pending source update.'); return; }
            text(c, 'p', `${page.title}. Active sequence remains unchanged until acceptance. ${page.metadata_changed ? 'Source title/description changed.' : ''}`);
            const t = table(c, ['Change', 'New position', 'Previous position', 'Source issue', 'Match']);
            for (const item of page.items) { const tr = text(t, 'tr'); [label(item.change), item.position === undefined ? '—' : item.position+1, item.previous_position === null ? '—' : item.previous_position+1, `${item.source.series} #${item.source.number}`, `${label(item.match)}${item.match_changed ? ' · Match changed' : ''}${item.source_fields_changed ? ' · Source fields changed' : ''}`].forEach(x => text(tr, 'td', x)); }
            this.pager(c, page, n => this.source(id, n));
            for (const [decision, title] of [['accept', 'Accept Update'], ['reject', 'Reject This Revision']]) this.action(c, title, () => this.confirm(title, decision === 'accept' ? 'Apply the reviewed source sequence, including removals and reordering. No library or acquisition state changes.' : 'Keep the accepted sequence and ignore this exact source revision.', async () => { const o = await this.api('POST', `/reading-orders/sources/${id}/decision`, {revision: page.revision, order_revision: page.order_revision, expected_digest: page.digest, decision, confirmed: true}); await this.order(o.id); }));
        }
        async poll(task) {
            for (let i = 0; i < 180 && ['queued', 'running'].includes(task.state); i++) { this.message(`Reading Order task: ${label(task.state)}`); await this.sleep(2000); task = await this.api('GET', `/reading-orders/tasks/${task.id}`); }
            if (task.state !== 'complete') throw {reason: task.reason || 'task_unavailable'};
            this.message('Operation complete. Inspect the durable order/source result.'); return task;
        }
        providerDialog() {
            const c = this.dialog('Find Provider Reading List'); text(c, 'p', 'Metron exposes explicitly ordered lists. ComicVine arc membership is not treated as a proven reading sequence. CBL can carry exact references from either provider.');
            const query = input(c, 'Metron list name'), results = text(c, 'div');
            const generation = this.generations.dialog;
            this.action(c, 'Search Metron Lists', async () => { const task = await this.poll(await this.api('POST', '/reading-orders/providers/search', {provider: 'metron', query: query.value})); if (!this.current('dialog', generation)) return; results.replaceChildren();
                for (const row of task.items || []) this.action(results, 'Review ' + row.title, async () => { const fetched = await this.poll(await this.api('POST', `/reading-orders/providers/${task.id}/fetch`, {result_id: row.id})); if (!this.current('dialog', generation)) return; this.el('dialog').close(); await this.review(fetched.result.id); }, true); }, true); query.focus();
        }
        wanted(review) {
            const c = this.dialog('Send Missing to Wanted'), selected = new Set();
            text(c, 'p', 'Ready entries will have issue monitoring enabled in an already-monitored local volume. Existing Wanted then derives eligibility; its ordinary automation may search. No immediate search is submitted. Volume monitoring never changes. C2-covered and external issues cannot be forced into Wanted.');
            for (const row of review.items) { const p = text(c, 'div'); text(p, 'p', `${row.title} #${row.number || '?'} · ${label(row.bucket)}`); if (row.bucket === 'ready') { const check = input(p, `Include entry ${row.entry_id}`, '', 'checkbox'); check.onchange = () => check.checked ? selected.add(row.entry_id) : selected.delete(row.entry_id); } }
            this.action(c, 'Enable Selected Issues for Wanted', async () => { if (!selected.size) { this.message('Select Ready issues explicitly.'); return; } await this.api('POST', `/reading-orders/wanted/${review.id}/apply`, {expected_digest: review.digest, selected: [...selected], confirmed: true}); this.el('dialog').close(); this.selected.clear(); await this.order(this.active.id); }, true);
        }
        async addPublication(order, entry) {
            const c = this.dialog('Add Publication to Library'), v = this.version('dialog'), roots = await this.api('GET', '/rootfolder'); if (!this.current('dialog', v) || !this.el('dialog').open) return;
            text(c, 'p', 'Use the normal Add Volume pipeline. New volumes and issues are initially unmonitored; no automatic search or download. Exact issue references reconcile after admission.');
            const root = text(text(c, 'label', 'Root folder'), 'select'); root.setAttribute('aria-label', 'Root folder'); for (const r of roots) { const op = text(root, 'option', r.folder); op.value = r.id; }
            const provider = text(text(c, 'label', 'Exact publication provider'), 'select'); provider.setAttribute('aria-label', 'Exact publication provider'); for (const r of entry.refs.filter(r => r.volume_id)) { const op = text(provider, 'option', r.provider); op.value = r.provider; }
            const pageGeneration = this.generations.page;
            this.action(c, 'Add Publication', async () => { await this.poll(await this.api('POST', `/reading-orders/${order.id}/add-publication`, {entry_id: entry.id, provider: provider.value, root_id: Number(root.value), confirmed: true})); if (!this.current('dialog', v)) return; this.el('dialog').close(); await this.order(order.id, 0, 'all', pageGeneration); }, true, !roots.length);
        }
    }
    return {Controller, text, label};
})();
if (typeof module !== 'undefined') module.exports = ReadingOrdersUI;
if (typeof usingApiKey === 'function') usingApiKey().then(key => {
    const url = (path, q = {}) => `${url_base}/api${path}?${new URLSearchParams({api_key: key, ...q})}`;
    const result = async response => { if (response.status === 401) { location.href = `${url_base}/login`; throw {reason: 'authentication_required'}; } const data = await response.json(); if (!response.ok) throw data.result; return data.result; };
    const api = (method, path, body = null, params = {}) => fetch(url(path, params), {method, headers: body === null ? {} : {'Content-Type': 'application/json'}, body: body === null ? undefined : JSON.stringify(body)}).then(result);
    const upload = file => fetch(url('/reading-orders/import'), {method: 'POST', headers: {'Content-Type': 'application/xml'}, body: file}).then(result);
    const download = async id => { const response = await fetch(url(`/reading-orders/${id}/export`)); if (!response.ok) return result(response); const blob = await response.blob(), object = URL.createObjectURL(blob), a = document.createElement('a'); a.href = object; a.download = `reading-order-${id}.cbl`; a.click(); setTimeout(() => URL.revokeObjectURL(object), 1000); };
    new ReadingOrdersUI.Controller(document.getElementById('reading-orders'), api, upload, download).start();
});
