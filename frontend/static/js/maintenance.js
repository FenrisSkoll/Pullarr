/* Transport/presentation only. Backend reviews own all maintenance policy. */
const MaintenanceUI = (() => {
    const messages = {
        review_expired: 'This review has expired or the application restarted. Create a fresh review before making changes.',
        revision_conflict: 'This worklist changed. Reload it and review the current selection; your stale edit was not merged.',
        capacity: 'The bounded review capacity is full. Finish or cancel an existing review, or wait for expiry.',
        bounded: 'This request exceeds a safe backend bound. Narrow the selection.',
        scan_not_available: 'The scan has not produced a report yet.',
        task_unavailable: 'The task could not be queued. No library mutation was requested.',
        history_unavailable: 'This historical entry is unavailable.',
        invalid_request: 'The request was rejected. Check the selection and reload current state.',
        internal_error: 'Maintenance could not complete this request. No internal error details are exposed.',
        authentication_required: 'Sign in to view Maintenance.',
        stale: 'Reviewed state changed. Create a fresh review; the recorded target will not be replaced silently.',
        blocked: 'The backend blocked this operation. Inspect current history and create a fresh review where appropriate.'
    };
    Object.assign(messages, {provider_rate_limited: 'Provider budget or rate limit reached. Wait before creating a fresh review.',
        provider_configuration: 'Provider authentication or configuration requires attention. No credentials are displayed.',
        provider_unavailable: 'Required provider or review input is unavailable.', incomplete_snapshot: 'The provider snapshot is incomplete. Repair is blocked.'});
    const label = value => String(value ?? '').replaceAll('_', ' ');
    function text(parent, tag, value) {
        const node = document.createElement(tag); node.textContent = String(value ?? ''); parent.appendChild(node); return node;
    }
    function button(parent, title, action, disabled = false) {
        const node = text(parent, 'button', title); node.type = 'button'; node.disabled = disabled; node.onclick = action; return node;
    }
    function table(parent, headings) {
        const wrapper = text(parent, 'div', ''); wrapper.className = 'maintenance-table';
        const result = text(wrapper, 'table', ''); const row = text(text(result, 'thead', ''), 'tr', '');
        for (const heading of headings) { const th = text(row, 'th', heading); th.scope = 'col'; }
        return text(result, 'tbody', '');
    }
    function fieldValue(parent, value) {
        if (value === null || typeof value !== 'object') { text(parent, 'span', value ?? 'Not set'); return; }
        const detail = text(parent, 'details', ''); text(detail, 'summary', 'Structured field value');
        const list = text(detail, 'dl', '');
        for (const [key, item] of Object.entries(value)) { text(list, 'dt', label(key)); fieldValue(text(list, 'dd', ''), item); }
    }
    const duplicateLabels = {exact_bytes: 'Exact byte duplicate', same_direct_publication: 'Same publication / different file',
        overlapping_content_coverage: 'Collected-content overlap', path_collision: 'Path collision', probable_duplicate: 'Review-only similarity'};
    const actions = ['no_action', 'acknowledge', 'review_later', 'metadata_repair', 'comicinfo_repair', 'rename', 'folder_organization', 'duplicate_review', 'keep_both'];
    function validConfirmation(value) {
        if (!value || !['rename', 'folder', 'metadata', 'comicinfo', 'duplicate', 'inverse', 'recovery'].includes(value.kind) ||
            typeof value.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value.id)) return false;
        const body = value.body;
        if (!body || body.confirmed !== true || typeof body.digest !== 'string' || !/^[0-9a-f]{64}$/.test(body.digest)) return false;
        if (['metadata', 'comicinfo'].includes(value.kind)) return Object.keys(body).sort().join(',') === 'confirmed,digest,revision' &&
            Number.isInteger(body.revision) && body.revision >= 0 && body.revision <= 1000;
        if (!['rename', 'folder', 'duplicate'].includes(value.kind)) return Object.keys(body).sort().join(',') === 'confirmed,digest';
        return Object.keys(body).sort().join(',') === 'confirmed,digest,origin,revision,selected' &&
            Number.isInteger(body.revision) && body.revision >= 0 && body.revision <= 1000 &&
            Array.isArray(body.origin) && body.origin.length === 3 && /^[0-9a-f]{32}$/.test(body.origin[0]) &&
            Number.isInteger(body.origin[1]) && body.origin[1] >= 0 && body.origin[1] <= 1000 && /^[0-9a-f]{64}$/.test(body.origin[2]) &&
            Array.isArray(body.selected) && body.selected.length > 0 && body.selected.length <= (value.kind === 'folder' ? 50 : 250) &&
            body.selected.every(id => typeof id === 'string' && /^[0-9a-f]{64}$/.test(id)) &&
            (!value.batch_id || (typeof value.batch_id === 'string' && /^[A-Za-z0-9_:.-]{1,512}$/.test(value.batch_id)));
    }
    class Controller {
        constructor(root, api, storage, sleep = ms => new Promise(resolve => setTimeout(resolve, ms))) {
            this.root = root; this.api = api; this.storage = storage; this.sleep = sleep;
            this.versions = {}; this.pending = false; this.saved = {}; this.worklist = null;
            this.storageKey = 'kapowarr-maintenance-v1'; // gitleaks:allow -- fixed browser storage namespace, not a credential.
            this.selectedVolumes = new Map();
        }
        el(id) { return this.root.querySelector('#maintenance-' + id); }
        version(key) { return this.versions[key] = (this.versions[key] || 0) + 1; }
        current(key, value) { return this.versions[key] === value; }
        message(value) { this.el('message').textContent = value; }
        error(error) { this.message(messages[error?.reason] || 'Request failed. Reload current state. For a possible lost mutation response, inspect durable history before retrying.'); }
        save(key, value) {
            this.saved[key] = value;
            try { this.storage.setItem(this.storageKey, JSON.stringify(this.saved)); } catch (_) { /* Tab persistence is optional. */ }
        }
        async start() {
            try {
                const value = JSON.parse(this.storage.getItem(this.storageKey) || '{}');
                for (const key of ['scan', 'worklist', 'delivery']) if (/^[0-9a-f]{32}$/.test(value[key] || '')) this.saved[key] = value[key];
                if (value.child && ['rename','folder','metadata','comicinfo','duplicate'].includes(value.child.kind) && /^[A-Za-z0-9_-]{32}$/.test(value.child.id)) this.saved.child = value.child;
            } catch (_) { /* Corrupt or disabled tab storage is not authority. */ }
            this.el('scan-form').onsubmit = event => { event.preventDefault(); this.startScan(); };
            this.el('filter-form').onsubmit = event => { event.preventDefault(); this.findings(); if (this.worklist) this.loadWorklist(); };
            this.el('history-form').onsubmit = event => { event.preventDefault(); this.history(); };
            this.el('scope').onchange = () => {
                this.el('root-label').hidden = this.el('scope').value !== 'root';
                this.el('volumes-label').hidden = this.el('scope').value !== 'volumes';
                if (this.el('scope').value === 'volumes') this.volumePicker();
            };
            this.el('volume-search-form').onsubmit = event => { event.preventDefault(); this.volumePicker(); };
            this.el('cancel').onclick = () => this.cancelScan();
            this.el('create-review').onclick = () => this.createWorklist();
            this.el('reload-review').onclick = () => this.loadWorklist();
            this.el('confirm-cancel').onclick = () => this.el('confirm').close();
            this.el('confirm').onclose = () => this.confirmInvoker?.focus();
            this.el('confirm-submit').onclick = () => this.submitConfirmation();
            this.el('retry-action').onclick = () => this.submitConfirmation(this.retryConfirmation);
            try {
                const retry = JSON.parse(this.storage.getItem(this.storageKey + '-confirmation') || 'null');
                if (validConfirmation(retry)) {
                    this.retryConfirmation = retry; this.el('retry-action').hidden = false;
                    this.message('A confirmed request may already have run. Check its durable result before creating another review.');
                }
            } catch (_) { /* Invalid saved identity is never execution authority. */ }
            try {
                const roots = await this.api('GET', '/rootfolder');
                for (const root of roots) { const option = text(this.el('root'), 'option', root.folder); option.value = String(root.id); }
            } catch (error) { this.error(error); }
            this.history();
            if (this.saved.scan) this.pollScan(this.saved.scan);
            if (this.saved.worklist) this.loadWorklist();
            if (this.saved.delivery) this.waitDelivery(this.saved.delivery).catch(error => this.error(error));
            if (this.retryConfirmation) await this.discoverConfirmation(this.retryConfirmation);
            else if (this.saved.child) await this.openChild(this.saved.child.kind, this.saved.child.id);
        }
        filters() {
            const result = {};
            for (const key of ['category', 'severity', 'inspection']) if (this.el(key).value) result[key] = this.el(key).value;
            return result;
        }
        async startScan() {
            if (this.pending) return;
            this.pending = true; this.el('start').disabled = true;
            try {
                const kind = this.el('scope').value;
                const raw = kind === 'volumes' ? [...this.selectedVolumes.keys()].map(String) : kind === 'root' ? [this.el('root').value] : [];
                if (kind === 'volumes' && !raw.length) throw {reason: 'invalid_request'};
                if (raw.length > 1000 || raw.some(v => !/^[1-9][0-9]*$/.test(v) || !Number.isSafeInteger(Number(v)))) throw {reason: 'invalid_request'};
                const scan = await this.api('POST', '/maintenance/scans', {scope: {kind, ids: raw.map(Number)}, level: this.el('level').value});
                this.save('scan', scan.id); this.el('create-review').disabled = true;
                this.version('findings'); this.el('findings').replaceChildren();
                this.pollScan(scan.id); this.message('Read-only scan queued. No repairs will run automatically.');
            } catch (error) { this.error(error); }
            finally { this.pending = false; this.el('start').disabled = false; }
        }
        async pollScan(identifier) {
            const generation = this.version('scan');
            try {
                while (this.current('scan', generation)) {
                    const scan = await this.api('GET', `/maintenance/scans/${identifier}`);
                    if (!this.current('scan', generation)) return;
                    const container = this.el('scan-status'); container.replaceChildren();
                    text(container, 'h3', `Scan: ${label(scan.state)}`);
                    const counts = scan.summary?.counts || scan.progress;
                    for (const [key, value] of Object.entries(counts || {})) if (['number', 'string'].includes(typeof value)) text(container, 'p', `${label(key)}: ${value}`);
                    if (scan.summary) {
                        text(container, 'p', `${label(scan.summary.level)} inspection · ${label(scan.summary.state)} · ${scan.summary.reasons.map(label).join(', ')}`);
                        text(container, 'p', 'Observations only; unrequested or bounded probes do not establish complete health.');
                    }
                    const active = ['queued', 'running'].includes(scan.state);
                    this.el('cancel').disabled = !active; this.el('create-review').disabled = !scan.summary;
                    if (!active) { if (scan.summary) await this.findings(); return; }
                    await this.sleep(2000);
                }
            } catch (error) { if (this.current('scan', generation)) this.error(error); }
        }
        async cancelScan() {
            if (!this.saved.scan) return;
            this.el('cancel').disabled = true;
            try { await this.api('POST', `/maintenance/scans/${this.saved.scan}/cancel`, {}); this.message('Cancellation requested. An OS-blocked read may need to finish first.'); }
            catch (error) { this.error(error); }
        }
        renderSelectedVolumes(offset = 0) {
            const container = this.el('volume-selected'); container.replaceChildren();
            text(container, 'p', `${this.selectedVolumes.size} selected volumes (maximum 1000). Selection is retained across search pages until this tab is reloaded.`);
            for (const [id, title] of [...this.selectedVolumes].slice(offset, offset + 50)) button(container, `Remove ${title}`, () => {
                this.selectedVolumes.delete(id); this.renderSelectedVolumes(); this.volumePicker();
            });
            button(container, 'Previous selected volumes', () => this.renderSelectedVolumes(Math.max(0, offset - 50)), offset === 0);
            button(container, 'Next selected volumes', () => this.renderSelectedVolumes(offset + 50), offset + 50 >= this.selectedVolumes.size);
        }
        async volumePicker(after = 0) {
            const generation = this.version('volume-picker');
            const query = this.el('volume-query').value;
            try {
                const page = await this.api('GET', '/maintenance/volumes', null, {q: query, after, limit: 50});
                if (!this.current('volume-picker', generation)) return;
                this.renderSelectedVolumes();
                const container = this.el('volume-results'); container.replaceChildren();
                if (!page.items.length) text(container, 'p', 'No volumes match this title search.');
                for (const item of page.items) button(container,
                    `${this.selectedVolumes.has(item.id) ? 'Selected' : 'Select'}: ${item.title} (${item.year ?? 'year unknown'}) · ${label(item.metadata_provider)}`, () => {
                        if (this.selectedVolumes.size >= 1000 || this.selectedVolumes.has(item.id)) return;
                        this.selectedVolumes.set(item.id, item.title); this.volumePicker(after);
                    }, this.selectedVolumes.has(item.id) || this.selectedVolumes.size >= 1000);
                button(container, 'First volume page', () => this.volumePicker(), after === 0);
                button(container, 'Next volume page', () => this.volumePicker(page.next_after), page.next_after === null);
            } catch (error) { if (this.current('volume-picker', generation)) this.error(error); }
        }
        pager(parent, page, load) {
            text(parent, 'p', `${page.total} matching items · showing ${page.total ? page.offset + 1 : 0}–${Math.min(page.offset + page.limit, page.total)}`);
            button(parent, 'Previous page', () => load(Math.max(0, page.offset - page.limit)), page.offset === 0);
            button(parent, 'Next page', () => load(page.offset + page.limit), page.offset + page.limit >= page.total);
        }
        async findings(offset = 0) {
            if (!this.saved.scan) return;
            const generation = this.version('findings');
            try {
                const page = await this.api('GET', `/maintenance/scans/${this.saved.scan}/findings`, null, {...this.filters(), offset, limit: 50});
                if (!this.current('findings', generation)) return;
                const container = this.el('findings'); container.replaceChildren();
                if (!page.findings.length) text(container, 'p', 'No findings for this filter. This does not establish health beyond the requested inspection.');
                const body = table(container, ['Severity', 'Finding', 'Volume / file', 'Inspection']);
                for (const finding of page.findings) {
                    const row = text(body, 'tr', '');
                    text(row, 'td', label(finding.severity)); text(row, 'td', finding.explanation);
                    text(row, 'td', `Volume ${finding.volume_id ?? '—'} · ${finding.path ?? 'No file path'}`);
                    text(row, 'td', label(finding.inspection));
                }
                this.pager(container, page, next => this.findings(next));
            } catch (error) { if (this.current('findings', generation)) this.error(error); }
        }
        async createWorklist() {
            if (this.pending || !this.saved.scan) return;
            this.pending = true; this.el('create-review').disabled = true;
            try {
                const worklist = await this.api('POST', '/maintenance/worklists', {scan_id: this.saved.scan});
                this.save('worklist', worklist.id); this.pending = false; await this.loadWorklist();
            } catch (error) { this.error(error); }
            finally { this.pending = false; this.el('create-review').disabled = false; }
        }
        async loadWorklist(offset = 0) {
            if (!this.saved.worklist) return;
            const generation = this.version('worklist');
            try {
                const base = `/maintenance/worklists/${this.saved.worklist}`;
                const summary = await this.api('GET', base);
                const filters = this.filters();
                const page = await this.api('GET', base + '/items', null, {...filters, offset, limit: 50});
                if (!this.current('worklist', generation)) return;
                if (summary.revision !== page.revision) throw {reason: 'revision_conflict'};
                this.worklist = summary;
                const container = this.el('worklist'); container.replaceChildren();
                text(container, 'h3', `Revision ${summary.revision} · ${summary.selected} selected · ${label(summary.status)}`);
                text(container, 'p', `Source inspection: ${label(summary.source_completeness)}. ${summary.source_reasons.map(label).join(', ')}`);
                button(container, `Select all ${page.total} findings matching these filters`, () => this.edit('select-filtered', {
                    revision: summary.revision, report_id: summary.scan_id, snapshot_digest: summary.snapshot_digest, filters, selected: true
                }), this.pending);
                const body = table(container, ['Finding', 'Intent', 'Selection', 'Capability']);
                for (const item of page.items) {
                    const row = text(body, 'tr', ''); text(row, 'td', item.finding.explanation);
                    const actionCell = text(row, 'td', ''); const select = text(actionCell, 'select', '');
                    select.setAttribute('aria-label', `Intent for ${item.finding.explanation}`);
                    for (const action of actions) { const option = text(select, 'option', label(action)); option.value = action; }
                    select.value = item.action;
                    const selection = text(row, 'td', '');
                    text(selection, 'p', item.excluded ? 'Excluded' : item.selected ? 'Selected' : 'Not selected');
                    const edit = (selected, excluded) => this.edit('revise', {revision: summary.revision,
                        edits: [{finding_id: item.finding.id, selected, excluded, action: select.value}]});
                    button(selection, 'Select intent', () => edit(true, false), this.pending);
                    button(selection, 'Exclude', () => edit(false, true), this.pending);
                    text(row, 'td', `${label(item.capability)} · ${item.blockers.map(label).join(', ')}`);
                }
                this.pager(container, page, next => this.loadWorklist(next));
                if (summary.rename_selection?.length) button(container, 'Review selected renames', () => this.createRename(summary), this.pending);
                if (summary.folder_selection?.length) button(container, 'Review selected folders', () => this.createFolder(summary), this.pending);
                for (const group of summary.specialized?.metadata || []) button(container, `Review metadata for volume ${group.volume_id}`, () => this.createSpecialized('metadata', summary, {selected: group.selected}), this.pending);
                for (const group of summary.specialized?.comicinfo || []) button(container, `Review ComicInfo for file ${group.file_id}`, () => this.createSpecialized('comicinfo', summary, {finding_id: group.finding_id}), this.pending);
                if (summary.specialized?.duplicate.length) button(container, 'Review selected duplicates', () => this.createSpecialized('duplicate', summary, {selected: summary.specialized.duplicate}), this.pending);
                text(container, 'p', 'Selections persist across pages on the backend. Each action requires a separate exact-effect review; there is no mixed Apply.');
            } catch (error) { if (this.current('worklist', generation)) this.error(error); }
        }
        async edit(operation, body) {
            if (this.pending || !this.saved.worklist) return;
            this.pending = true;
            try {
                const result = await this.api('POST', `/maintenance/worklists/${this.saved.worklist}/${operation}`, body);
                this.save('delivery', result.id); await this.waitDelivery(result.id);
            } catch (error) { this.error(error); }
            finally { this.pending = false; }
        }
        async waitDelivery(identifier) {
            const generation = this.version('delivery');
            while (this.current('delivery', generation)) {
                const result = await this.api('GET', `/maintenance/review-tasks/${identifier}`);
                if (!this.current('delivery', generation)) return;
                if (['queued', 'running'].includes(result.state)) { this.message('Review task is pending. No library mutation is authorized.'); await this.sleep(2000); continue; }
                this.save('delivery', null);
                if (result.state !== 'complete') throw {reason: result.reason};
                this.save('worklist', result.worklist_id); this.pending = false;
                await this.loadWorklist(); this.message('Reviewed intent updated. No library files or metadata changed.'); return;
            }
        }
        async history(before = null) {
            const generation = this.version('history'); this.version('detail'); this.el('detail').replaceChildren();
            const params = {limit: 50};
            for (const key of ['operation', 'state']) if (this.el(key).value) params[key] = this.el(key).value;
            if (before) params.before = JSON.stringify(before);
            try {
                const page = await this.api('GET', '/maintenance/history', null, params);
                if (!this.current('history', generation)) return;
                const container = this.el('history-rows'); container.replaceChildren();
                if (!page.items.length) text(container, 'p', 'No history for this filter.');
                const body = table(container, ['Operation', 'Time', 'Domain state', 'Affected objects', 'Inverse hint']);
                for (const entry of page.items) {
                    const row = text(body, 'tr', '');
                    button(text(row, 'td', ''), label(entry.operation), () => this.detail(entry));
                    text(row, 'td', entry.time); text(row, 'td', label(entry.state));
                    text(row, 'td', `Volume ${entry.volume_id ?? '—'} · File ${entry.file_id ?? '—'}`);
                    text(row, 'td', label(entry.inverse_capability));
                }
                button(container, 'Newest history', () => this.history(), !before);
                button(container, 'Older history', () => this.history(page.next_cursor), !page.next_cursor);
            } catch (error) { if (this.current('history', generation)) this.error(error); }
        }
        async detail(entry, offset = 0) {
            const generation = this.version('detail');
            try {
                const result = await this.api('GET', `/maintenance/history/${encodeURIComponent(entry.domain)}/${encodeURIComponent(entry.id)}`, null, {offset, limit: 50});
                if (!this.current('detail', generation)) return;
                const container = this.el('detail'); container.replaceChildren();
                const current = result.entry;
                text(container, 'h3', label(current.operation));
                text(container, 'p', `${label(current.state)} · domain state: ${label(current.domain_state)}`);
                if (current.source) text(container, 'p', `Original/source: ${current.source}`);
                if (current.target && !current.internal_storage_hidden) text(container, 'p', `Target: ${current.target}`);
                if (current.internal_storage_hidden) text(container, 'p', 'Quarantine storage is internal. Files are retained, not permanently deleted; restore is conditional.');
                text(container, 'p', current.operation === 'provider_switch' ? 'Reverse switching requires a fresh Provider Switching review.' :
                    current.operation === 'metadata_repair' ? 'Audit history only. No generic metadata rollback.' :
                    current.operation === 'comicinfo_repair' ? 'Forward recovery may be supported; lossless archive undo is unavailable.' :
                    'Current recovery/revert eligibility is not checked by listing history. No universal Undo is available.');
                if (current.state === 'recovery_required') text(container, 'p', 'Recovery required. Review the current recorded state before continuing; the domain job remains authoritative.');
                if (current.domain === 'organization' && current.state === 'recovery_required') button(container, 'Review Recovery', () => this.historyPreview(current, 'recovery'));
                if (current.domain === 'organization' && current.state === 'complete' && current.inverse_capability === 'unchecked') button(container, 'Check Revert', () => this.historyPreview(current, 'inverse'));
                if (current.batch_id) button(container, 'Open batch', () => this.batch(current.batch_id));
                if (current.domain === 'organization') {
                    const intent = result.detail.recorded_intent || {};
                    text(container, 'p', `Volume ${intent.volume_id ?? current.volume_id ?? 'historical'} · File ${intent.file_id ?? current.file_id ?? 'multiple or historical'}`);
                    if (intent.file_count !== undefined) text(container, 'p', `${intent.file_count} registered file paths; ${intent.issue_count} issues preserved.`);
                    if (intent.custom_before !== undefined) text(container, 'p', `Folder ownership: ${intent.custom_before ? 'custom' : 'managed'} → ${intent.custom_after ? 'custom' : 'managed'}`);
                    if (intent.retained_ids) text(container, 'p', `Retained active copy IDs: ${intent.retained_ids.join(', ')}. Restore remains conditional.`);
                    if (intent.comicinfo_original_bytes_retained === false) text(container, 'p', 'Lossless revert is not available. Original archive bytes were not retained.');
                    if (current.operation === 'archive_normalization' && intent.receipt) {
                        const receipt = intent.receipt;
                        text(container, 'h4', 'Archive maintenance receipt');
                        text(container, 'p', `${String(receipt.source_container).toUpperCase()} → ${String(receipt.target_container).toUpperCase()} · ${receipt.pages} pages · ${receipt.members} members.`);
                        text(container, 'p', `Archive size: ${intent.old_size} → ${intent.new_size} bytes. Size changes are not visual-quality changes.`);
                        text(container, 'p', `Page payloads: ${receipt.pages_preserved ? 'byte-identical, SHA-256 verified' : 'not verified'}. Metadata: ${label(receipt.metadata_action)}. Order: ${label(receipt.member_order)}.`);
                        text(container, 'p', intent.shared_source ? 'Independent replacement. Shared source bytes were retained; torrent retention is unchanged.' : 'Independent library archive replacement.');
                        text(container, 'p', 'Container normalization is not a quality upgrade or a new acquisition. Recovery protects interruptions; permanent lossless undo is not promised.');
                    }
                    text(container, 'h4', 'Journal steps');
                    const steps = table(container, ['Step', 'Effect', 'State']);
                    for (const step of result.detail.steps || []) {
                        const row = text(steps, 'tr', ''); text(row, 'td', step.ordinal); text(row, 'td', label(step.kind)); text(row, 'td', label(step.state));
                    }
                    text(container, 'h4', 'Journal events');
                    const events = table(container, ['Time', 'Step', 'Event']);
                    for (const event of result.detail.events || []) {
                        const row = text(events, 'tr', ''); text(row, 'td', event.created_at); text(row, 'td', event.ordinal ?? '—'); text(row, 'td', label(event.event));
                    }
                } else if (current.domain === 'metadata_repair') {
                    const receipt = result.detail.receipt;
                    text(container, 'h4', 'Audit history');
                    text(container, 'p', `${label(receipt.provider)} · generation ${receipt.authority_generation} · ${receipt.field_count} fields · ${receipt.applied_at}`);
                    text(container, 'p', `Classification: ${label(receipt.classification_action)} · Bibliography: ${label(receipt.bibliography_action)}`);
                    const fields = table(container, ['Field / owner', 'Before', 'After']);
                    for (const field of result.detail.fields) {
                        const row = text(fields, 'tr', ''); text(row, 'td', `${label(field.field)} · ${field.scope} ${field.local_id}`);
                        fieldValue(text(row, 'td', ''), field.before_value); fieldValue(text(row, 'td', ''), field.after_value);
                    }
                } else if (current.domain === 'provider_switch') {
                    const receipt = result.detail;
                    text(container, 'p', `${label(receipt.source_provider)} generation ${receipt.source_generation} → ${label(receipt.target_provider)} generation ${receipt.target_generation}`);
                    text(container, 'p', `${receipt.mapped_count} mapped issues · ${receipt.added_count} added · ${receipt.claim_count} claims · ${receipt.coverage_count} coverage records.`);
                    text(container, 'p', 'Start New Provider Switch from the volume page. This history does not pre-authorize a reverse switch.');
                    const issues = table(container, ['Local issue', 'Source identity', 'Target identity', 'Correspondence']);
                    for (const issue of receipt.issues || []) {
                        const row = text(issues, 'tr', ''); text(row, 'td', issue.local_issue_id);
                        text(row, 'td', `${issue.source_provider ?? 'New'} ${issue.source_provider_id ?? ''}`);
                        text(row, 'td', `${issue.target_provider} ${issue.target_provider_id}`); text(row, 'td', label(issue.correspondence));
                    }
                } else if (current.domain === 'content_claim' || current.domain === 'content_coverage') {
                    const claim = result.detail.claim || result.detail.coverage;
                    text(container, 'h4', current.domain === 'content_claim' ? 'Collected-content claim history' : 'File coverage history');
                    for (const key of ['id','kind','authority','target_provider','target_provider_id','source_provider','source_provider_id','file_id','target_issue_id','source_issue_id','claim_id','created_at','retired_at','supersedes','currently_valid']) {
                        if (key in claim) text(container, 'p', `${label(key)}: ${claim[key] ?? 'None'}`);
                    }
                    text(container, 'p', 'Content coverage is not duplicate publication ownership. Confirm/revoke/supersession belongs to Collected Contents, not generic revert.');
                    const evidence = table(container, ['Provider', 'Recorded edge', 'Origin issue', 'Target issue', 'Recorded active']);
                    for (const item of result.detail.evidence || []) {
                        const row = text(evidence, 'tr', '');
                        for (const key of ['provider','edge_id','origin_issue','target_issue','active']) text(row, 'td', item[key]);
                    }
                } else if (current.domain === 'intake') {
                    text(container, 'p', 'Operational intake history; OrganizationJob state is authoritative for library mutation.');
                    const rows = table(container, ['Artifact', 'Intake state', 'Organization job', 'Job state']);
                    for (const item of result.detail.artifacts || []) { const row = text(rows, 'tr', ''); for (const key of ['id','state','organization_job_id','job_state']) text(row, 'td', item[key]); }
                }
                button(container, 'Previous details', () => this.detail(entry, offset - 50), offset === 0);
                button(container, 'Next details', () => this.detail(entry, offset + 50), !result.page?.has_next);
            } catch (error) { if (this.current('detail', generation)) this.error(error); }
        }
        async action(path, body) {
            const delivery = await this.api('POST', path, body);
            // This poll is operational delivery only. Result carries a domain
            // reference; completed task is not assumed to mean completed job.
            for (;;) {
                const status = await this.api('GET', `/maintenance/action-tasks/${delivery.id}`);
                if (['queued', 'running'].includes(status.state)) {
                    this.message(`${label(status.state)} — checking the recorded operation. Please wait.`);
                    await this.sleep(2000); continue;
                }
                if (status.state !== 'complete') throw {reason: status.reason};
                return status.result;
            }
        }
        async createRename(worklist) {
            if (this.pending) return;
            this.pending = true;
            const generation = this.version('specialized');
            try {
                const result = await this.action('/maintenance/rename/reviews', {worklist_id: worklist.id,
                    revision: worklist.revision, digest: worklist.manifest_digest, selected: worklist.rename_selection});
                if (this.current('specialized', generation)) await this.renameReview(result.id);
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
            finally { this.pending = false; }
        }
        async renameReview(id, offset = 0) {
            const generation = this.version('specialized');
            try {
                const review = await this.api('GET', `/maintenance/rename/reviews/${id}`, null, {offset, limit: 50});
                if (!this.current('specialized', generation)) return;
                this.save('child', {kind: 'rename', id});
                const container = this.el('specialized'); container.replaceChildren();
                text(container, 'h3', 'Reviewed filename-only rename');
                text(container, 'p', 'Parent folders, associations, metadata and archive contents will not change. Eligibility is rechecked before registration.');
                const rows = table(container, ['Current path', 'Proposed path', 'Status', 'Selection']);
                for (const item of review.items) {
                    const row = text(rows, 'tr', ''); text(row, 'td', item.source); text(row, 'td', item.target);
                    text(row, 'td', `${label(item.state)} ${item.blockers.map(label).join(', ')}`);
                    button(text(row, 'td', ''), item.selected ? 'Exclude from rename' : 'Include in rename', async () => {
                        if (this.pending) return;
                        this.pending = true;
                        try {
                            const selected = item.selected ? review.selected.filter(i => i !== item.finding_id) : [...review.selected, item.finding_id];
                            await this.action(`/maintenance/rename/reviews/${id}/selection`, {revision: review.revision, selected});
                            await this.renameReview(id, offset);
                        } catch (error) { this.error(error); }
                        finally { this.pending = false; }
                    }, item.selected && review.selected.length === 1);
                }
                for (const collision of review.collisions) text(container, 'p', `Blocked: ${label(collision.code)}`);
                this.pager(container, review, next => this.renameReview(id, next));
                button(container, 'Review rename confirmation', () => this.confirm({kind: 'rename', id, batch_id: review.batch_id,
                    body: {revision: review.revision, digest: review.digest, origin: review.origin,
                        selected: review.selected, confirmed: true}},
                    `Rename ${review.mutation_count} files. Parent folders, associations, metadata and archive contents will not change.`, 'Rename reviewed files'), !review.apply_available);
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
        }
        async createFolder(worklist, canonical = []) {
            if (this.pending) return;
            this.pending = true;
            const generation = this.version('specialized');
            try {
                const result = await this.action('/maintenance/folder/reviews', {worklist_id: worklist.id,
                    revision: worklist.revision, digest: worklist.manifest_digest,
                    selected: worklist.folder_selection, canonical_custom: canonical});
                if (this.current('specialized', generation)) await this.folderReview(result.id);
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
            finally { this.pending = false; }
        }
        async folderReview(id, offset = 0) {
            const generation = this.version('specialized');
            try {
                const review = await this.api('GET', `/maintenance/folder/reviews/${id}`, null, {offset, limit: 50});
                if (!this.current('specialized', generation)) return;
                this.save('child', {kind: 'folder', id});
                const container = this.el('specialized'); container.replaceChildren();
                text(container, 'h3', 'Reviewed whole-folder organization');
                text(container, 'p', 'Same root and filesystem only. Filenames, comic bytes, metadata and associations are preserved, including safe ancillary files and nested/empty directories. No merge, root migration, case staging or dependency-chain execution.');
                const rows = table(container, ['Current folder', 'Reviewed target', 'Ownership / inventory', 'Status / selection']);
                for (const item of review.items) {
                    const row = text(rows, 'tr', ''); text(row, 'td', item.source); text(row, 'td', item.target);
                    const facts = text(row, 'td', '');
                    text(facts, 'p', item.custom_before ? (item.custom_after ? 'Preserve custom folder' : 'Explicit custom → managed transition') : 'Managed folder');
                    text(facts, 'p', `${item.inventory_complete ? 'Complete' : 'Incomplete'} inventory: ${item.registered_count} registered, ${item.direct_count} direct, ${item.general_count} general, ${item.ancillary_count} ancillary files, ${item.directory_count} directories.`);
                    if (item.custom_before) button(facts, item.custom_after ? 'Review use of canonical folder' : 'Review preservation of custom folder', () => {
                        const canonical = item.custom_after ? [...review.canonical_custom, item.finding_id] : review.canonical_custom.filter(i => i !== item.finding_id);
                        this.createFolder({id: review.origin[0], revision: review.origin[1], manifest_digest: review.origin[2], folder_selection: review.selected}, canonical.filter(i => review.selected.includes(i)));
                    }, !item.selected);
                    const state = text(row, 'td', ''); text(state, 'p', `${label(item.state)} ${item.blockers.map(label).join(', ')}`);
                    button(state, item.selected ? 'Exclude folder' : 'Include folder', async () => {
                        if (this.pending) return;
                        this.pending = true;
                        try {
                            const selected = item.selected ? review.selected.filter(i => i !== item.finding_id) : [...review.selected, item.finding_id];
                            await this.action(`/maintenance/folder/reviews/${id}/selection`, {revision: review.revision, selected});
                            await this.folderReview(id, offset);
                        } catch (error) { this.error(error); }
                        finally { this.pending = false; }
                    }, item.selected && review.selected.length === 1);
                }
                for (const collision of review.collisions) text(container, 'p', `Blocked: ${label(collision.code)}`);
                this.pager(container, review, next => this.folderReview(id, next));
                button(container, 'Review folder confirmation', () => this.confirm({kind: 'folder', id, batch_id: review.batch_id,
                    body: {revision: review.revision, digest: review.digest, origin: review.origin, selected: review.selected, confirmed: true}},
                    `Move ${review.mutation_count} complete volume folders within their current roots. Filenames, metadata, associations and comic bytes remain unchanged.`,
                    'Move reviewed folders'), !review.apply_available);
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
        }
        async discoverConfirmation(identity) {
            // Lookup only: a reload must never enqueue another mutation.
            if (!validConfirmation(identity)) return false;
            if (!['rename', 'folder', 'duplicate', 'metadata', 'comicinfo'].includes(identity.kind)) return false;
            if (['rename','folder','duplicate'].includes(identity.kind) && !identity.batch_id) return false;
            const generation = this.version('discovery');
            this.message('Checking durable result…');
            try {
                if (['metadata', 'comicinfo'].includes(identity.kind)) {
                    const result = await this.api('GET', `/maintenance/repair/${identity.kind}/reviews/${identity.id}/result`, null,
                        {revision: identity.body.revision, digest: identity.body.digest});
                    if (!this.current('discovery', generation)) return false;
                    if (!result.found) throw {reason: 'history_unavailable'};
                    await this.history(); await this.showDomainResult(result);
                } else {
                if (!identity.batch_id) return false;
                await this.api('GET', `/maintenance/batches/${encodeURIComponent(identity.batch_id)}`, null, {limit: 1});
                if (!this.current('discovery', generation)) return false;
                await this.history();
                await this.batch(identity.batch_id);
                }
                if (!this.current('discovery', generation)) return false;
                this.el('retry-action').hidden = true;
                try { this.storage.removeItem(this.storageKey + '-confirmation'); } catch (_) { /* Optional. */ }
                this.retryConfirmation = null;
                this.message('Existing durable batch or receipt found. No mutation was resubmitted. Review its current domain state.');
                return true;
            } catch (error) {
                if (!this.current('discovery', generation)) return false;
                this.el('retry-action').hidden = false;
                if (error?.reason === 'history_unavailable') this.message('No durable batch was found yet. A submitted task may still be queued or running. Only the exact confirmation may be retried; do not create another review.');
                else this.error(error);
                return false;
            }
        }
        confirm(identity, description, title) {
            this.confirmInvoker = document.activeElement;
            this.confirmation = identity;
            this.el('confirm-description').textContent = description;
            this.el('confirm-submit').textContent = title;
            this.el('confirm-submit').disabled = false;
            this.el('confirm').showModal(); this.el('confirm-cancel').focus();
        }
        async submitConfirmation(identity = this.confirmation) {
            if (this.pending || !identity) return;
            if (!validConfirmation(identity)) { this.error({reason: 'invalid_request'}); return; }
            const retrying = identity === this.retryConfirmation;
            this.pending = true; this.el('confirm-submit').disabled = true; this.el('retry-action').disabled = true;
            this.message(identity.kind === 'recovery' ? 'Continuing the recorded recovery…' : 'Applying the confirmed operation…');
            this.retryConfirmation = identity;
            try {
                this.storage.setItem(this.storageKey + '-confirmation', JSON.stringify(identity));
            } catch (_) { /* Exact in-memory retry remains available. */ }
            try {
                if (retrying && ['rename', 'folder'].includes(identity.kind) && identity.batch_id) {
                    try {
                        await this.api('GET', `/maintenance/batches/${encodeURIComponent(identity.batch_id)}`, null, {limit: 1});
                        await this.batch(identity.batch_id);
                        this.el('retry-action').hidden = true;
                        try { this.storage.removeItem(this.storageKey + '-confirmation'); } catch (_) { /* Optional. */ }
                        this.message('Existing durable batch found. No mutation was resubmitted. Review its current domain state.');
                        return;
                    } catch (error) { if (error?.reason !== 'history_unavailable') throw error; }
                }
                if (retrying && ['metadata','comicinfo','duplicate'].includes(identity.kind) && await this.discoverConfirmation(identity)) return;
                const path = ['rename', 'folder', 'duplicate'].includes(identity.kind) ? `/maintenance/${identity.kind}/reviews/${identity.id}/apply` :
                    ['metadata','comicinfo'].includes(identity.kind) ? `/maintenance/repair/${identity.kind}/reviews/${identity.id}/apply` :
                    `/maintenance/history/organization/${identity.id}/${identity.kind === 'inverse' ? 'inverse' : 'recover'}`;
                const result = await this.action(path, identity.body);
                this.el('confirm').close(); this.el('retry-action').hidden = true;
                try { this.storage.removeItem(this.storageKey + '-confirmation'); } catch (_) { /* Optional. */ }
                this.retryConfirmation = null;
                await this.history();
                await this.showDomainResult(result);
                this.message(['complete','completed'].includes(result.state) || ['complete','completed'].includes(result.entry?.state) ? 'Operation completed.' :
                    result.state === 'no_changes' ? 'No changes; no jobs were created.' : 'Operation returned. The current job state is shown below.');
                if (identity.kind === 'recovery' && typeof document.dispatchEvent === 'function')
                    document.dispatchEvent(new Event('pullarr-archive-reconciled'));
            } catch (error) {
                this.el('confirm').close(); this.el('retry-action').hidden = false;
                this.error(error);
                if (!error?.reason) this.message('Response lost. The operation may have committed. Check durable history or use this exact retry; do not create another confirmation.');
                if (!error?.reason) await this.discoverConfirmation(identity);
            } finally { this.pending = false; this.el('retry-action').disabled = false; }
        }
        async showDomainResult(result) {
            if (['rename_batch','folder_batch','duplicate_batch'].includes(result.kind) && result.state !== 'no_changes') await this.batch(result.id);
            else if (result.kind === 'organization') await this.detail(result.entry);
            else if (result.kind === 'metadata_receipt') await this.detail({domain: 'metadata_repair', id: result.id});
        }
        async openChild(kind, id, offset = 0) {
            if (kind === 'rename') return this.renameReview(id, offset);
            if (kind === 'folder') return this.folderReview(id, offset);
            if (kind === 'duplicate') return this.duplicateReview(id, offset);
            return this.repairReview(kind, id, offset);
        }
        async createSpecialized(kind, worklist, selection) {
            if (this.pending) return;
            this.pending = true;
            const generation = this.version('specialized');
            this.message(kind === 'metadata' ? 'Acquiring a bounded selected-provider snapshot…' : 'Building fresh read-only evidence…');
            try {
                const base = kind === 'duplicate' ? '/maintenance/duplicate' : `/maintenance/repair/${kind}`;
                const result = await this.action(base + '/reviews', {worklist_id: worklist.id, revision: worklist.revision,
                    digest: worklist.manifest_digest, ...selection});
                if (this.current('specialized', generation)) await this.openChild(kind, result.id);
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
            finally { this.pending = false; }
        }
        async reviseRepair(kind, review, changes, offset) {
            if (this.pending) return;
            this.pending = true;
            try {
                await this.action(`/maintenance/repair/${kind}/reviews/${review.id}/selection`, {revision: review.revision, ...changes});
                await this.repairReview(kind, review.id, offset);
            } catch (error) { this.error(error); }
            finally { this.pending = false; }
        }
        async repairReview(kind, id, offset = 0) {
            const generation = this.version('specialized');
            try {
                const review = await this.api('GET', `/maintenance/repair/${kind}/reviews/${id}`, null, {offset, limit: 50});
                if (!this.current('specialized', generation)) return;
                this.save('child', {kind, id});
                const container = this.el('specialized'); container.replaceChildren();
                text(container, 'h3', kind === 'metadata' ? 'Reviewed provider metadata repair' : 'Reviewed ComicInfo repair');
                text(container, 'p', `${label(review.provider)} · authority generation ${review.generation} · revision ${review.revision}`);
                text(container, 'p', kind === 'metadata' ? 'Files and folders will not change. Excluded fields remain unchanged; exclusion is not a permanent override.' :
                    'The archive will be rewritten. Unknown metadata is preserved. Archive bytes may change. Lossless revert is not available.');
                const rows = table(container, ['Field / scope', 'Current', 'Proposed', 'State / selection']);
                for (const field of review.items) {
                    const row = text(rows, 'tr', ''); text(row, 'td', kind === 'metadata' ? `${label(field.field)} · ${field.scope} ${field.local_id}` : field.key);
                    const before = text(row, 'td', ''), after = text(row, 'td', ''), choice = text(row, 'td', '');
                    if (kind === 'metadata') {
                        fieldValue(before, field.before); fieldValue(after, field.after);
                        text(choice, 'p', `${label(field.change)} · ${label(field.support)} · ${label(field.reason)}`);
                        button(choice, field.selected ? 'Exclude field' : 'Select field', () => this.reviseRepair(kind, review,
                            {edits: [{key: field.key, selected: !field.selected}]}, offset), field.support !== 'supported');
                    } else {
                        for (const change of field.changes) { fieldValue(before, change.before); fieldValue(after, change.after); text(choice, 'p', label(change.action)); }
                        if (field.key === 'Provider identities') text(after, 'p', 'Selected provider-qualified identities; mandatory with any selected changes.');
                        text(choice, 'p', field.supported ? 'Supported semantic field' : 'Unavailable or incomplete value; preserve');
                        button(choice, field.selected ? 'Preserve field' : 'Use reviewed field', () => {
                            const selected = new Set(review.selected);
                            if (field.selected) selected.delete(field.key); else selected.add(field.key);
                            if (selected.size) selected.add('Provider identities');
                            this.reviseRepair(kind, review, {selected: [...selected]}, offset);
                        }, !field.supported || (field.key === 'Provider identities' && field.selected));
                    }
                }
                if (kind === 'metadata') {
                    const edits = review.items.filter(f => f.support === 'supported' && f.change !== 'unchanged').map(f => ({key: f.key, selected: true}));
                    button(container, 'Select supported changed fields on this page', () => this.reviseRepair(kind, review, {edits}, offset), !edits.length);
                    text(container, 'p', `Classification: ${label(review.classification_action)} · Bibliography: ${label(review.bibliography_action)}`);
                    for (const reason of review.blockers) text(container, 'p', label(reason));
                }
                this.pager(container, review, next => this.repairReview(kind, id, next));
                button(container, 'Review repair confirmation', () => this.confirm({kind, id,
                    body: {revision: review.revision, digest: review.digest, confirmed: true}}, kind === 'metadata' ?
                    `Apply ${review.mutation_count} reviewed provider-owned metadata changes. Files and folders will not change.` :
                    'Rewrite ComicInfo metadata in this archive using the reviewed fields. Unknown metadata will be preserved. Lossless revert is not available.',
                    kind === 'metadata' ? 'Apply reviewed metadata' : 'Write reviewed ComicInfo'), !review.apply_available);
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
        }
        async duplicateReview(id, offset = 0) {
            const generation = this.version('specialized');
            try {
                const review = await this.api('GET', `/maintenance/duplicate/reviews/${id}`, null, {offset, limit: 50});
                if (!this.current('specialized', generation)) return;
                this.save('child', {kind: 'duplicate', id});
                const container = this.el('specialized'); container.replaceChildren();
                text(container, 'h3', 'Reviewed duplicate evidence');
                text(container, 'p', 'No automatic best copy. Publication ownership and collected-content coverage are distinct. Only exact-byte groups can be prepared for quarantine.');
                for (const group of review.items) {
                    const section = text(container, 'section', ''); text(section, 'h4', duplicateLabels[group.kind]);
                    text(section, 'p', `${group.files} files · ${label(group.action)} · ${group.blockers.filter(b => b !== 'quarantine_recovery_not_implemented').map(label).join(', ')}`);
                    if (group.requires_prepare) text(section, 'p', 'Fresh executable preparation required before confirmation.');
                    button(section, 'Inspect group and choices', () => this.duplicateGroup(review, group.id));
                }
                this.pager(container, review, next => this.duplicateReview(id, next));
                button(container, 'Prepare reviewed quarantine', async () => {
                    if (this.pending) return; this.pending = true;
                    try { await this.action(`/maintenance/duplicate/reviews/${id}/prepare`, {revision: review.revision}); await this.duplicateReview(id, offset); }
                    catch (error) { this.error(error); } finally { this.pending = false; }
                }, !review.prepare_available);
                if (review.apply_available) text(container, 'p', `Prepared ${review.mutation_count} quarantine effects. Fresh hashes, same-device storage, ownership and bounded journal admission passed; registration rechecks them.`);
                button(container, 'Review quarantine confirmation', () => this.confirm({kind: 'duplicate', id, batch_id: review.batch_id,
                    body: {revision: review.revision, digest: review.digest, origin: review.origin, selected: review.selected, confirmed: true}},
                    `Quarantine ${review.mutation_count} exact duplicate files. They will not be permanently deleted. At least one active copy remains and no issue loses ownership. Quarantined files continue to use disk space. Restore is conditional on the future file/path/domain state. Permanent purge is not available.`,
                    'Quarantine Files'), !review.apply_available);
                const members = text(container, 'div', ''); members.id = 'maintenance-duplicate-members';
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
        }
        async duplicateGroup(review, groupId, offset = 0) {
            const generation = this.version('duplicate-group'), parent = this.versions.specialized;
            try {
                const detail = await this.api('GET', `/maintenance/duplicate/reviews/${review.id}`, null, {group_id: groupId, offset, limit: 50});
                if (!this.current('duplicate-group', generation) || parent !== this.versions.specialized) return;
                const container = this.el('duplicate-members'); container.replaceChildren();
                text(container, 'h4', duplicateLabels[detail.group.kind]);
                const choice = async (action, quarantine = []) => {
                    if (this.pending) return; this.pending = true;
                    try { await this.action(`/maintenance/duplicate/reviews/${review.id}/selection`, {revision: review.revision, choices: [{group_id: groupId, action, quarantine}]}); await this.duplicateReview(review.id); }
                    catch (error) { this.error(error); } finally { this.pending = false; }
                };
                for (const [title, action] of [['Keep All','keep_all'],['Acknowledge','acknowledge'],['Review Later','review_later']]) button(container, title, () => choice(action));
                const rows = table(container, ['Copy', 'Evidence', 'Ownership / choice']);
                for (const member of detail.members) {
                    const row = text(rows, 'tr', ''); text(row, 'td', `${member.path} · File ${member.id}`);
                    text(row, 'td', `${member.size} bytes · ${member.hash_verified ? 'Fresh SHA-256 verified' : 'Not strong byte equivalence'}`);
                    const cell = text(row, 'td', ''); text(cell, 'p', `Direct issues: ${member.direct_issue_ids.join(', ')} · General volumes: ${member.general_volume_ids.join(', ')} · Historical coverage rows: ${member.coverage_count}`);
                    const selected = detail.group.quarantine.includes(member.id);
                    text(cell, 'p', selected ? 'Selected for quarantine' : 'Retained active copy');
                    if (detail.group.kind === 'exact_bytes') button(cell, selected ? 'Retain this copy' : 'Select this copy for quarantine', () => {
                        const ids = selected ? detail.group.quarantine.filter(i => i !== member.id) : [...detail.group.quarantine, member.id];
                        choice(ids.length ? 'quarantine_selected' : 'keep_all', ids);
                    });
                }
                for (const effect of detail.impact) text(container, 'p', `Issue ${effect.issue_id}: owned before by ${effect.before_files.join(', ')}; after by ${effect.after_files.join(', ') || 'none'}. ${effect.loses_ownership ? 'Ownership loss: quarantine blocked.' : 'Ownership preserved.'} ${effect.becomes_wanted ? 'Would become Wanted.' : 'No new Wanted state.'}`);
                this.pager(container, detail, next => this.duplicateGroup(review, groupId, next));
            } catch (error) { if (this.current('duplicate-group', generation)) this.error(error); }
        }
        async historyPreview(entry, operation) {
            const generation = this.version('specialized');
            try {
                const result = await this.action(`/maintenance/history/${entry.domain}/${entry.id}/${operation}-preview`, {});
                if (!this.current('specialized', generation)) return;
                const preview = result.preview, container = this.el('specialized'); container.replaceChildren();
                const title = operation === 'recovery' ? 'Continue Recovery' : entry.operation === 'duplicate_quarantine' ? 'Restore File' :
                    entry.operation === 'folder_organization' ? 'Restore Previous Folder' : 'Revert Rename';
                text(container, 'h3', preview.manual_inspection_required ? 'Manual inspection required' : title);
                text(container, 'p', operation === 'recovery' ? 'Pullarr will continue the original recorded operation. It will not calculate a new target.' : 'A separate conditional inverse will be registered. Current artifact, path and domain checks must still pass.');
                if (preview.current_path && !preview.internal_storage_hidden) text(container, 'p', `Current: ${preview.current_path}`);
                if (preview.restore_path) text(container, 'p', `Restore to: ${preview.restore_path}`);
                if (operation === 'recovery') {
                    text(container, 'p', `Filesystem continuation: ${preview.filesystem_mutation_required ? 'required' : 'not required'}. Database reconciliation: ${preview.database_reconciliation_required ? 'required' : 'not required'}.`);
                    for (const [key, value] of Object.entries(preview.observations || {})) {
                        if (typeof value === 'boolean') text(container, 'p', `${label(key)}: ${value ? 'yes' : 'no'}`);
                    }
                    const steps = table(container, ['Recorded step', 'Continuation']);
                    for (const step of preview.steps || []) { const row = text(steps, 'tr', ''); text(row, 'td', step.ordinal); text(row, 'td', label(step.action)); }
                }
                for (const reason of preview.reasons || []) text(container, 'p', label(reason));
                if (!preview.eligible) text(container, 'p', 'No automatic recovery or deletion will be performed. There is no force action.');
                if (preview.eligible && !preview.manual_inspection_required) button(container, title, () => this.confirm({kind: operation, id: entry.id,
                    body: {digest: preview.digest, confirmed: true}}, operation === 'recovery' ?
                    'Continue the original recorded operation. No new target is calculated.' : `${title}. This is a new journaled operation, not universal undo.`, title));
                container.tabIndex = -1;
                container.focus();
                container.scrollIntoView?.({behavior: 'smooth', block: 'start'});
            } catch (error) { if (this.current('specialized', generation)) this.error(error); }
        }
        async batch(id, offset = 0) {
            const generation = this.version('batch');
            try {
                const result = await this.api('GET', `/maintenance/batches/${encodeURIComponent(id)}`, null, {offset, limit: 50});
                if (!this.current('batch', generation)) return;
                const container = this.el('batch'); container.replaceChildren();
                text(container, 'h3', `Batch: ${label(result.state)}`);
                if (result.selected_count !== undefined) text(container, 'p', `${result.selected_count} selected ${label(result.selected_unit)} items · ${result.volume_count} volumes · ${result.file_count} registered files.`);
                text(container, 'p', `${result.mutation_job_count} mutation jobs; ${result.nonmutating_count} nonmutating outcomes.`);
                for (const [state, count] of Object.entries(result.counts)) text(container, 'p', `${label(state)}: ${count}`);
                for (const item of result.items) {
                    if (item.domain) button(container, `${label(item.operation)} · ${label(item.state)}`, () => this.detail(item));
                    else text(container, 'p', `Nonmutating: ${label(item.state)}`);
                }
                button(container, 'Previous batch page', () => this.batch(id, Math.max(0, offset - 50)), offset === 0);
                button(container, 'Next batch page', () => this.batch(id, result.next_offset), result.next_offset === null);
                if ((result.counts.pending || 0) + (result.counts.active || 0) > 0) {
                    await this.sleep(2000);
                    if (this.current('batch', generation)) this.batch(id, offset);
                }
            } catch (error) { if (this.current('batch', generation)) this.error(error); }
        }
    }
    return {Controller, messages, text, label, validConfirmation};
})();

if (typeof module !== 'undefined') module.exports = MaintenanceUI;
if (typeof usingApiKey === 'function') usingApiKey().then(apiKey => {
    const api = async (method, path, body = null, params = {}) => {
        const query = new URLSearchParams({api_key: apiKey, ...params});
        const response = await fetch(`${url_base}/api${path}?${query}`, {method,
            headers: body === null ? {} : {'Content-Type': 'application/json'},
            body: body === null ? undefined : JSON.stringify(body)});
        if (response.status === 401) { window.location.href = `${url_base}/login`; throw {reason: 'authentication_required'}; }
        const envelope = await response.json();
        if (!response.ok) throw envelope.result;
        return envelope.result;
    };
    new MaintenanceUI.Controller(document.getElementById('maintenance'), api, sessionStorage).start();
});
