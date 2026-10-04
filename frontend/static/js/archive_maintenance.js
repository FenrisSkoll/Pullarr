/* Container maintenance presentation. Server-issued reviews own authority. */
const ArchiveMaintenanceUI = (() => {
    const label = value => String(value ?? '').replaceAll('_', ' ');
    function text(parent, tag, value) {
        const node = document.createElement(tag); node.textContent = String(value ?? ''); parent.appendChild(node); return node;
    }
    class Controller {
        constructor(root, api, sleep = ms => new Promise(resolve => setTimeout(resolve, ms))) {
            this.root = root; this.api = api; this.sleep = sleep; this.generation = 0;
            this.after = 0; this.stack = []; this.busy = false; this.task = null; this.review = null;
            this.resultGeneration = 0; this.volumeGeneration = 0;
        }
        el(name) { return this.root.querySelector(`#archive-${name}`); }
        message(value) { this.el('message').textContent = value; }
        selected(name) { return [...this.el(name).querySelectorAll('input:checked')].map(n => Number(n.value)); }
        lock(value) {
            this.busy = value;
            for (const name of ['scan', 'preview', 'apply', 'select']) this.el(name).disabled = value;
            this.el('cancel').disabled = !value;
        }
        table(parent, headings) {
            parent.replaceChildren(); const wrapper = text(parent, 'div', ''); wrapper.className = 'maintenance-table';
            const table = text(wrapper, 'table', ''); const row = text(text(table, 'thead', ''), 'tr', '');
            headings.forEach(h => { text(row, 'th', h).scope = 'col'; });
            return text(table, 'tbody', '');
        }
        check(cell, id, name, checked = false) {
            const input = text(cell, 'input', ''); input.type = 'checkbox'; input.value = String(id);
            input.checked = checked; input.setAttribute('aria-label', `Select ${name}`);
        }
        async start() {
            document.addEventListener('pullarr-archive-reconciled', () => {
                if (!this.busy) {
                    this.review = null;
                    this.el('results').replaceChildren();
                    this.load();
                }
            });
            this.el('volume-form').onsubmit = async event => {
                event.preventDefault();
                const generation = ++this.volumeGeneration;
                try {
                    const result = await this.api('GET', '/maintenance/volumes', null, {q: this.el('volume-query').value, limit: 100});
                    if (generation !== this.volumeGeneration) return;
                    const select = this.el('volume'); select.replaceChildren(); text(select, 'option', 'Library — paged files').value = '';
                    for (const row of result.items) text(select, 'option', `${row.title} (${row.year ?? 'year unknown'})`).value = row.id;
                } catch (_) { if (generation === this.volumeGeneration) this.message('Volume lookup failed. Retry.'); }
            };
            this.el('volume').onchange = () => { this.after = 0; this.stack = []; this.load(); };
            this.el('select').onclick = () => this.el('files').querySelectorAll('input').forEach(n => { n.checked = true; });
            this.el('scan').onclick = () => this.run('scan');
            this.el('preview').onclick = () => this.run('preview');
            this.el('apply').onclick = () => this.apply();
            this.el('close').onclick = () => this.el('dialog').close();
            this.el('dialog').addEventListener('close', () => this.el('preview').focus());
            this.el('cancel').onclick = async () => {
                try {
                    if (this.task) { await this.api('POST', `/maintenance/archives/tasks/${this.task}/cancel`, {}); this.message('Cancellation requested; journaled effects remain recoverable.'); }
                } catch (_) { this.message('Cancellation could not be confirmed. Inspect task and History before retrying.'); }
            };
            this.el('filter').onchange = () => this.results(0);
            this.el('previous').onclick = () => { this.after = this.stack.pop() ?? 0; this.load(); };
            this.el('next').onclick = () => { this.stack.push(this.after); this.after = this.next; this.load(); };
            this.el('results-next').onclick = () => this.results(this.resultNext);
            await this.load();
        }
        async load() {
            const generation = ++this.generation;
            try {
                const query = {after: this.after, limit: 50};
                if (this.el('volume').value) query.volume_id = this.el('volume').value;
                const result = await this.api('GET', '/maintenance/archives', null, query);
                if (generation !== this.generation) return;
                const body = this.table(this.el('files'), ['Select', 'Comic', 'File', 'Bytes']);
                for (const row of result.items) {
                    const tr = text(body, 'tr', ''); this.check(text(tr, 'td', ''), row.file_id, row.filename);
                    text(tr, 'td', row.title); text(tr, 'td', row.filename); text(tr, 'td', row.size);
                }
                this.next = result.next_after; this.el('next').disabled = this.next === null;
                this.el('previous').disabled = this.stack.length === 0;
                this.message(result.items.length ? 'Select files to inspect or preview. No automatic library scan.' : 'No archive maintenance needed. No files in this scope.');
            } catch (_) { if (generation === this.generation) this.message('File list unavailable. Reload to retry.'); }
        }
        async poll(id) {
            this.task = id;
            for (let n = 0; n < 1800; n++) {
                const result = await this.api('GET', `/maintenance/archives/tasks/${id}`);
                this.message(`${label(result.state)} · ${result.done} files processed`);
                if (!['queued', 'running'].includes(result.state)) return result;
                await this.sleep(1000);
            }
            throw new Error('Task remains active; inspect history');
        }
        async run(operation) {
            if (this.busy) return;
            const selected = this.selected('files');
            if (!selected.length) { this.message('Select at least one file.'); return; }
            this.lock(true);
            try {
                const result = await this.api('POST', `/maintenance/archives/${operation === 'preview' ? 'batch-preview' : 'scan'}`, {selected});
                const done = await this.poll(result.id); await this.results(0);
                if (operation === 'preview' && done.state === 'complete') {
                    this.review = result.id; const body = this.table(this.el('review'), ['Apply', 'Current → Proposed', 'Preservation / source']);
                    for (const row of done.items) {
                        const tr = text(body, 'tr', ''); const cell = text(tr, 'td', '');
                        if (row.apply_available) this.check(cell, row.file_id, row.filename, row.status === 'convertible');
                        text(tr, 'td', `${row.filename ?? 'File'} (${String(row.container || 'unknown').toUpperCase()}, ${row.size ?? '?'} bytes) → ${row.target ?? 'Unavailable'} · ${row.pages ?? '?'} pages`);
                        text(tr, 'td', row.apply_available ? `Page bytes and metadata preserved; original member order. Metadata: ${(row.metadata || []).join(', ') || 'none observed'}. ${row.shared_source ? 'Shared bytes: new independent CBZ; seed source unchanged.' : 'Independent replacement.'} ${row.status === 'healthy' ? 'Healthy CBZ: repack is optional.' : ''}` : label(row.reason));
                    }
                    this.el('dialog').showModal(); this.el('dialog-title').focus();
                }
            } catch (error) { this.message(`Archive request failed: ${label(error.reason || 'unavailable')}. Inspect History if a replacement was requested.`); }
            finally { this.lock(false); }
        }
        async results(offset) {
            if (!this.task) return;
            const generation = ++this.resultGeneration;
            const id = this.task; const status = this.el('filter').value;
            try {
                const result = await this.api('GET', `/maintenance/archives/tasks/${id}`, null, {offset, status});
                if (generation !== this.resultGeneration || id !== this.task || status !== this.el('filter').value) return;
                const body = this.table(this.el('results'), ['File', 'State', 'Pages / metadata', 'Source / history']);
                if (result.items.some(row => row.workspace_findings?.length))
                    text(this.el('results'), 'p', 'Unreferenced archive workspace found. Leave it intact for manual inspection; other files can still be converted.');
                for (const row of result.items) {
                    const tr = text(body, 'tr', ''); text(tr, 'td', row.filename || `Selected file ${row.file_id}`);
                    text(tr, 'td', label(row.reason || row.status)); text(tr, 'td', `${row.pages ?? '—'} · ${(row.metadata || []).join(', ') || 'No metadata observed'}`);
                    const cell = text(tr, 'td', row.shared_source ? 'Shared source retained' : '');
                    if (row.job_id) text(cell, 'a', 'View durable history').href = '#maintenance-history';
                }
                if (!result.items.length) text(this.el('results'), 'p', 'No archive maintenance findings in this filter.');
                this.resultNext = result.next_offset; this.el('results-next').disabled = this.resultNext === null;
            } catch (_) { this.message('Findings unavailable or expired. Run a fresh scan; durable history is retained.'); }
        }
        async apply() {
            if (this.busy || !this.review) return;
            const selected = this.selected('review');
            if (!selected.length) { this.message('Select a proposed replacement.'); return; }
            this.lock(true);
            try {
                const result = await this.api('POST', '/maintenance/archives/batch-apply', {review_id: this.review, selected, confirmed: true});
                this.el('dialog').close(); await this.poll(result.id); await this.results(0); await this.load();
            } catch (error) { this.message(`Replacement blocked or uncertain: ${label(error.reason || 'unavailable')}. Inspect durable History before retrying.`); }
            finally { this.lock(false); if (!this.el('dialog').open) this.el('preview').focus(); }
        }
    }
    return {Controller, label, text};
})();
if (typeof module !== 'undefined') module.exports = ArchiveMaintenanceUI;
if (typeof usingApiKey === 'function') usingApiKey().then(key => {
    const api = async (method, path, body = null, params = {}) => {
        const response = await fetch(`${url_base}/api${path}?${new URLSearchParams({api_key: key, ...params})}`, {
            method, headers: body === null ? {} : {'Content-Type': 'application/json'},
            body: body === null ? undefined : JSON.stringify(body)});
        const result = await response.json(); if (!response.ok) throw result.result; return result.result;
    };
    new ArchiveMaintenanceUI.Controller(document.getElementById('archive-maintenance'), api).start();
});
