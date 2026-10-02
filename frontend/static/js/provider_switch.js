/* Presentation only. The server owns correspondence, applicability and apply. */
const ProviderSwitchUI = (() => {
    const messages = {
        review_unavailable: 'This review expired, was evicted, or was lost on restart. If Apply was submitted, inspect history or retry that exact request first. Otherwise create a fresh review. Successful switches remain durable.',
        stale_review: 'Library state changed. Start a fresh review; nothing was reapplied.',
        authority_changed: 'Metadata authority changed. Start a fresh review.',
        invalid_revision: 'The reviewed mapping revision changed. Reload the review before confirming.',
        mapping_digest_mismatch: 'The mapping no longer matches the reviewed version. Reload the review.',
        review_blocked: 'This review is blocked. Inspect its correspondence and dependencies.',
        provider_disabled: 'The target provider is disabled. Configure it before reviewing.',
        provider_auth_required: 'Provider credentials are missing or rejected.',
        provider_rate_limited: 'Provider rate limit reached. Wait before requesting a new review.',
        provider_unavailable: 'The provider is unavailable. No fallback provider was selected.',
        target_not_found: 'The exact target record was not found.',
        target_incomplete: 'The provider did not supply a safely complete target snapshot.',
        review_limit: 'Review capacity or size limit reached. Try again later or choose a smaller series.',
        same_provider_or_missing_volume: 'Use Refresh for the current provider. Same-provider identity repair is not switching.',
        target_or_mapping_invalid: 'The exact target or mapping was rejected. No fuzzy replacement was made.',
        review_conflict: 'The target snapshot or correspondence conflicts with established state.',
        invalid_request: 'The request was invalid. Reload before retrying.',
        internal_error: 'The operation failed unexpectedly. Check switch history before retrying the exact request.'
    };
    const text = (parent, tag, value) => {
        const element = document.createElement(tag);
        element.textContent = String(value ?? 'Not supplied'); parent.appendChild(element); return element;
    };
    const button = (parent, label, action, disabled = false) => {
        const element = text(parent, 'button', label); element.type = 'button';
        element.disabled = disabled; element.onclick = action; return element;
    };
    const ref = value => value ? `${value.provider}:${value.provider_id ?? value.id}` : 'Unresolved';
    const label = value => value ? `${value.issue_number ?? value.number?.raw ?? ''} — ${value.title ?? 'Untitled'} (${value.date ?? 'date not supplied'})` : 'Unresolved';
    const technical = (parent, title, value) => {
        const section = document.createElement('details'); parent.appendChild(section);
        text(section, 'summary', title); text(section, 'pre', JSON.stringify(value, null, 2));
    };
    class Controller {
        constructor(root, api, storage) {
            this.root = root; this.api = api; this.storage = storage;
            this.volume = Number(root.dataset.volumeId); this.version = 0; this.historyVersion = 0;
            this.review = null; this.pending = false; this.page = 0; this.newPage = 0; this.current = null;
            this.retryKey = `kapowarr-switch-retry-${this.volume}`;
        }
        el(id) { return this.root.querySelector(`#switch-${id}`); }
        message(value) { this.el('message').textContent = value; }
        error(error) { this.message(messages[error?.reason] || 'Request interrupted. If Apply was submitted, inspect history or retry the exact saved request; do not assume it failed.'); }
        async start() {
            this.el('search').onsubmit = event => { event.preventDefault(); this.search(new FormData(event.target)); };
            this.el('confirm-cancel').onclick = () => this.el('confirm').close();
            this.el('confirm-apply').onclick = () => this.apply();
            this.el('mapping-close').onclick = () => this.el('mapping').close();
            this.el('history-refresh').onclick = () => this.history();
            this.el('retry').onclick = () => this.retry();
            try { this.el('retry').hidden = !this.storage.getItem(this.retryKey); } catch (_) { /* Storage may be disabled. */ }
            await this.loadCurrent(); await this.history();
        }
        async loadCurrent() {
            try {
                this.current = await this.api('GET', `/volumes/${this.volume}`, null, {metadata: 'true', issue_facts: '1'});
                this.el('current').textContent = `Current authority: ${ref(this.current.metadata_source)} — ${this.current.title}`;
            } catch (error) { this.error(error); }
        }
        async search(data) {
            if (this.pending) return;
            const version = ++this.version;
            this.message('Searching metadata providers…');
            try {
                const result = await this.api('GET', '/volumes/search', null, {provider: data.get('provider'), query: data.get('query')});
                if (version !== this.version) return;
                const container = this.el('results'); container.replaceChildren();
                const groups = MetadataSearchPresentation.groups(result) || [{label: data.get('provider'), status: 'complete', result_count: result.length, results: result}];
                for (const group of groups) {
                    container.appendChild(MetadataSearchPresentation.heading(group));
                    for (const row of group.results) {
                        const identity = row.metadata_source || {provider: 'comicvine', id: String(row.comicvine_id)};
                        const card = document.createElement('article'); card.className = 'switch-card'; container.appendChild(card);
                        const image = document.createElement('img'); image.alt = '';
                        image.src = MetadataSearchPresentation.safeLink(row.cover_link) || `${url_base}/static/img/favicon.svg`; card.appendChild(image);
                        const body = document.createElement('div'); card.appendChild(body);
                        text(body, 'h3', `${row.title} (${row.year ?? 'unknown year'}) — ${ref(identity)}`);
                        text(body, 'p', `${row.publisher ?? 'Publisher not supplied'} · ${row.issue_count ?? '?'} issues`);
                        text(body, 'p', MetadataSearchPresentation.annotations(row));
                        if (row.already_added != null) text(body, 'p', `Selected identity already local: volume ${row.already_added}`);
                        const same = identity.provider === this.current?.metadata_source?.provider;
                        button(body, same ? 'Current provider — use Refresh' : `Review ${ref(identity)}`,
                            () => this.create(identity), same);
                    }
                }
                this.message('Select an exact target to acquire a complete review. No result is automatically chosen.');
            } catch (error) { if (version === this.version) this.error(error); }
        }
        async create(identity) {
            if (this.pending) return;
            const version = ++this.version; this.review = null; this.el('review').replaceChildren();
            this.el('confirm').close(); this.el('mapping').close(); this.message('Acquiring the complete target snapshot…');
            try {
                const result = await this.api('POST', '/provider-switch/reviews', {volume_id: this.volume, provider: identity.provider, provider_id: identity.id});
                if (version !== this.version) {
                    this.api('DELETE', `/provider-switch/reviews/${result.session_id}`).catch(() => {}); return;
                }
                this.review = result; this.page = 0; this.newPage = 0; this.render(); this.message('Review all changes before confirming.');
            } catch (error) { if (version === this.version) this.error(error); }
        }
        async reload() {
            if (!this.review || this.pending) return;
            const version = ++this.version, id = this.review.session_id;
            try {
                const result = await this.api('GET', `/provider-switch/reviews/${id}`);
                if (version === this.version) { this.review = result; this.render(); }
            } catch (error) { if (version === this.version) { this.review = null; this.el('review').replaceChildren(); this.error(error); } }
        }
        render() {
            const container = this.el('review'); container.replaceChildren();
            const review = this.review, p = review.preview;
            text(container, 'h2', `${ref(p.source)} → ${ref(p.target)}`);
            text(container, 'p', `Review revision ${review.revision}. Approximately ${Math.ceil(review.expires_in / 60)} minutes remaining at last retrieval.`);
            button(container, 'Revalidate / reload review', () => this.reload());
            button(container, 'Cancel review', async () => {
                if (this.pending) return;
                ++this.version; this.review = null; container.replaceChildren();
                try { await this.api('DELETE', `/provider-switch/reviews/${review.session_id}`); this.message('Review cancelled. This does not undo a committed switch; check history if Apply was submitted.'); } catch (error) { this.error(error); }
            });
            const blockers = document.createElement('section'); blockers.className = 'switch-blockers'; container.appendChild(blockers);
            text(blockers, 'h3', p.apply_available ? 'Backend review is applicable' : 'Switch blocked');
            for (const reason of p.blockers) text(blockers, 'p', reason.replaceAll('_', ' '));
            technical(blockers, 'Dependency details', {dependencies: p.dependencies, observed_tasks: p.observed_tasks});
            text(container, 'h3', 'Volume metadata changes');
            for (const [key, delta] of Object.entries(p.volume_deltas)) text(container, 'p', `${key}: ${delta.before ?? 'Not supplied'} → ${delta.after ?? 'Not supplied'}`);
            if (!Object.keys(p.volume_deltas).length) text(container, 'p', 'No owned volume field changes.');
            const unchanged = document.createElement('details'); container.appendChild(unchanged);
            text(unchanged, 'summary', 'Unchanged / non-owned volume fields');
            for (const field of p.volume_fields || []) {
                if (!field.owned) text(unchanged, 'p', `${field.field}: retained ${field.current ?? 'Not supplied'}; target observation ${field.target ?? 'Not supplied'} is not owned by this provider.`);
                else if (!field.changed) text(unchanged, 'p', `${field.field}: unchanged (${field.current ?? 'Not supplied'}).`);
            }
            technical(container, 'Target observations and fields this provider owns', {target: p.target_volume, owned: p.application_fields});
            text(container, 'h3', `Existing issue correspondence (${p.issues.length})`);
            text(container, 'p', 'Every existing local issue must have one exact target identity. No existing issue is deleted.');
            const table = document.createElement('table'); container.appendChild(table);
            const head = document.createElement('tr'); table.appendChild(head);
            for (const heading of ['Current local issue', 'Target issue', 'Evidence / action']) text(head, 'th', heading);
            for (const row of p.issues.slice(this.page * 25, (this.page + 1) * 25)) {
                const tr = document.createElement('tr'); table.appendChild(tr);
                const local = text(tr, 'td', `Local #${row.local.id}: ${label(row.local)} · ${ref(row.correspondence.source)} · ${row.direct_files.length} direct file(s)`);
                technical(local, 'Current facts / external references', {facts: row.canonical, references: row.external_ids});
                const target = text(tr, 'td', `${ref(row.correspondence.target)} · ${label(row.target)}`);
                if (row.target) technical(target, 'Target number / date / variant facts', row.target);
                const action = text(tr, 'td', row.correspondence.kind.replaceAll('_', ' '));
                for (const reason of row.correspondence.blockers) text(action, 'p', reason.replaceAll('_', ' '));
                technical(action, 'Exact correspondence evidence', row.correspondence.evidence);
                button(action, 'Choose exact target', () => this.pick(row.local.id), this.pending);
            }
            button(container, 'Previous issues', () => { this.page--; this.render(); }, this.page === 0);
            text(container, 'span', `Page ${this.page + 1} / ${Math.max(1, Math.ceil(p.issues.length / 25))}`);
            button(container, 'Next issues', () => { this.page++; this.render(); }, (this.page + 1) * 25 >= p.issues.length);
            text(container, 'h3', `Target-only additions (${p.target_only.length})`);
            this.newPage = Math.min(this.newPage, Math.max(0, Math.ceil(p.target_only.length / 25) - 1));
            const additions = p.target_only.slice(this.newPage * 25, (this.newPage + 1) * 25);
            for (const row of additions) text(container, 'p', `${row.provider}:${row.provider_id} · ${label(row)} · ${row.monitored ? 'Monitored: may become Wanted if missing' : 'Unmonitored'}`);
            button(container, 'Previous additions', () => { this.newPage--; this.render(); }, this.newPage === 0);
            button(container, 'Next additions', () => { this.newPage++; this.render(); }, (this.newPage + 1) * 25 >= p.target_only.length);
            technical(container, 'Addition facts on this page', additions);
            text(container, 'h3', 'Classification');
            const c = p.classification;
            text(container, 'p', `Stored: ${c.current.stored.value || 'Normal'} (${c.current.stored.locked ? 'locked' : 'unlocked'}). Target candidate: ${c.target_unlocked_evaluation.value || 'Normal'} — ${c.target_unlocked_evaluation.reason}.`);
            text(container, 'p', c.current.stored.locked ? 'Stored classification and its historical receipt will remain because it is locked.' : 'A successful apply will record the target candidate through the existing classification provenance system.');
            technical(container, 'Historical provenance (not today’s evaluation) and target evidence', c);
            text(container, 'h3', 'Bibliography and graph');
            text(container, 'p', `${p.bibliography.old_evidence_becomes_historical ? 'Old selected-provider bibliography becomes historical evidence. ' : ''}Retained bibliography stays provider-attributed. Graph rows are unchanged; selected GCD local mappings may appear or disappear. No catalog sync.`);
            technical(container, 'C1 / C2A evidence details', {bibliography: p.bibliography, graph: p.graph});
            text(container, 'h3', 'Collected contents, ownership and Wanted');
            text(container, 'p', `${p.content.claims.length} affected claims; ${p.content.claims.reduce((n, row) => n + row.valid_coverage.length, 0)} currently valid coverage links reviewed for exact rebinding. Stale coverage is not reactivated.`);
            for (const row of p.content.claims) text(container, 'p', `Claim ${row.claim_id}: ${row.kind} / ${row.authority} — ${row.future_action.replaceAll('_', ' ')} ${row.blockers.join(', ')}`);
            for (const row of p.content.ownership) text(container, 'p', `Local issue ${row.issue_id}: owned after exact rebind = ${row.owned_after_exact_rebinding}; would become Wanted without rebind = ${row.wanted_without_rebinding}.`);
            technical(container, 'Exact claim and coverage impact', p.content);
            text(container, 'h3', 'Preserved state');
            text(container, 'p', 'Stable mapped local IDs, existing monitoring, direct files, folder/root, local artwork, old provider references and history are retained. Files moved: 0. Files renamed: 0. ComicInfo writes: 0. Target-only monitoring follows the reviewed policy.');
            button(container, 'Review final confirmation', () => {
                if (!this.review?.preview.apply_available || this.pending) return;
                this.el('confirm-identity').textContent = `${ref(p.source)} → ${ref(p.target)}; ${p.issues.length} mapped issues, ${p.target_only.length} additions.`;
                this.el('confirm').showModal();
            }, !p.apply_available || this.pending);
        }
        pick(localId) {
            if (this.pending) return;
            const review = this.review, input = this.el('mapping-filter'); input.value = '';
            let offset = 0;
            const draw = () => {
                const container = this.el('mapping-choices'); container.replaceChildren();
                const query = input.value.toLocaleLowerCase();
                const choices = review.target_issues.filter(row => `${row.provider_id} ${label(row)}`.toLocaleLowerCase().includes(query));
                for (const row of choices.slice(offset, offset + 25)) button(container, `${row.provider}:${row.provider_id} · ${label(row)}`, () => this.revise(localId, row.provider_id, review));
                button(container, 'Previous targets', () => { offset -= 25; draw(); }, offset === 0);
                button(container, 'Next targets', () => { offset += 25; draw(); }, offset + 25 >= choices.length);
                button(container, 'Remove manual override', () => this.revise(localId, null, review));
            };
            input.oninput = () => { offset = 0; draw(); }; draw(); this.el('mapping').showModal();
        }
        async revise(localId, targetId, review) {
            if (this.pending || this.review !== review) return;
            this.pending = true; const version = ++this.version; this.el('mapping').close(); this.el('confirm').close(); this.render();
            const mappings = review.overrides.filter(row => row.local_issue_id !== localId);
            if (targetId !== null) mappings.push({local_issue_id: localId, target_provider_id: targetId});
            try {
                const result = await this.api('PUT', `/provider-switch/reviews/${review.session_id}`, {revision: review.revision, mappings});
                if (version === this.version) { this.review = result; this.message('Exact mapping revision saved. Review the updated impact.'); }
            } catch (error) { if (version === this.version) this.error(error); }
            finally { this.pending = false; if (version === this.version) this.render(); }
        }
        async apply(retry = null) {
            if (this.pending || (!retry && !this.review?.preview.apply_available)) return;
            const request = retry || {session_id: this.review.session_id, revision: this.review.revision,
                mapping_digest: this.review.preview.mapping_digest, confirmed: true, source_authority: this.review.source_authority};
            this.pending = true; ++this.version; this.el('confirm-apply').disabled = true;
            this.message('Applying the exact reviewed switch…');
            try {
                try { this.storage.setItem(this.retryKey, JSON.stringify(request)); } catch (_) { /* Exact in-page retry remains available. */ }
                this.lastRequest = request;
                const {session_id, ...body} = request;
                const result = await this.api('POST', `/provider-switch/reviews/${session_id}/apply`, body);
                this.review = null; this.el('review').replaceChildren(); this.el('confirm').close();
                try { this.storage.removeItem(this.retryKey); } catch (_) { /* Optional browser storage. */ }
                this.el('retry').hidden = true;
                this.success(result); await this.loadCurrent(); await this.history();
            } catch (error) {
                this.el('confirm').close(); this.el('retry').hidden = false;
                if (['stale_review', 'authority_changed', 'review_unavailable', 'invalid_revision', 'mapping_digest_mismatch'].includes(error?.reason)) {
                    this.review = null; this.el('review').replaceChildren();
                }
                this.error(error);
            }
            finally { this.pending = false; this.el('confirm-apply').disabled = false; }
        }
        retry() {
            try { const request = this.lastRequest || JSON.parse(this.storage.getItem(this.retryKey)); if (request) this.apply(request); }
            catch (_) { this.message('Saved retry unavailable. Inspect successful history before creating another review.'); }
        }
        success(receipt) {
            const container = this.el('success'); container.replaceChildren();
            text(container, 'h2', receipt.already_applied ? 'Switch already committed — durable receipt recovered' : 'Metadata provider switched');
            text(container, 'p', `${receipt.source_provider}:${receipt.source_provider_id} → ${receipt.target_provider}:${receipt.target_provider_id} at ${receipt.applied_at}`);
            text(container, 'p', `${receipt.mapped_count} existing issues mapped; ${receipt.added_count} added. Classification: ${receipt.classification_action} (${receipt.classification_value || 'Normal'}). Claims: ${receipt.claim_count}; coverage: ${receipt.coverage_count}.`);
            text(container, 'p', 'Files were not moved or renamed; ComicInfo was not rewritten. Old external provider IDs were retained. Switching back requires a fresh review.');
            button(container, 'Open successful receipt', () => this.receipt(receipt.id)); this.message('Switch committed successfully.');
        }
        async history(before = null) {
            const version = ++this.historyVersion;
            try {
                const rows = await this.api('GET', `/volumes/${this.volume}/provider-switch/history`, null, before ? {before_generation: before} : {});
                if (version !== this.historyVersion) return;
                const container = this.el('history'); if (!before) container.replaceChildren();
                if (!rows.length && !before) text(container, 'p', 'No successful provider switches recorded.');
                for (const row of rows) button(container, `${row.applied_at}: ${row.source_provider}:${row.source_provider_id} → ${row.target_provider}:${row.target_provider_id}; ${row.mapped_count} mapped / ${row.added_count} new; ${row.classification_action}; ${row.claim_count} claims / ${row.coverage_count} coverage`, () => this.receipt(row.id));
                this.el('history-more').hidden = rows.length < 25;
                this.el('history-more').onclick = () => this.history(rows[rows.length - 1].target_generation);
            } catch (error) { if (version === this.historyVersion) this.error(error); }
        }
        async receipt(id, offset = 0) {
            const version = this.receiptVersion = (this.receiptVersion || 0) + 1;
            try {
                const row = await this.api('GET', `/provider-switch/receipts/${id}`, null, {detail: '1', offset, limit: 100});
                if (version !== this.receiptVersion) return;
                const container = this.el('receipt'); container.replaceChildren();
                text(container, 'h2', 'Successful switch receipt');
                text(container, 'p', `${row.source_provider}:${row.source_provider_id} → ${row.target_provider}:${row.target_provider_id} · ${row.applied_at}`);
                for (const item of row.issues) text(container, 'p', `Local issue ${item.local_issue_id}: ${item.source_provider ?? 'new'}:${item.source_provider_id ?? ''} → ${item.target_provider}:${item.target_provider_id} (${item.correspondence})`);
                for (const item of row.claims) text(container, 'p', `Claim ${item.old_claim_id} → ${item.new_claim_id}`);
                for (const item of row.coverage) text(container, 'p', `Coverage ${item.old_coverage_id} → ${item.new_coverage_id}`);
                technical(container, 'Receipt technical details (not an undo snapshot)', row);
                button(container, 'Previous receipt details', () => this.receipt(id, offset - 100), offset === 0);
                button(container, 'Next receipt details', () => this.receipt(id, offset + 100), Math.max(row.issues.length, row.claims.length, row.coverage.length) < 100);
            } catch (error) { if (version === this.receiptVersion) this.error(error); }
        }
    }
    return {Controller, messages, text, ref};
})();

if (typeof module !== 'undefined') module.exports = ProviderSwitchUI;
if (typeof usingApiKey === 'function') usingApiKey().then(apiKey => {
    const api = async (method, path, body = null, params = {}) => {
        const query = new URLSearchParams({api_key: apiKey, ...params});
        const response = await fetch(`${url_base}/api${path}?${query}`, {method,
            headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
        if (response.status === 401) { window.location.href = `${url_base}/login`; throw {reason: 'authentication_required'}; }
        const envelope = await response.json();
        if (!response.ok) throw envelope.result;
        return envelope.result;
    };
    new ProviderSwitchUI.Controller(document.getElementById('provider-switch'), api, sessionStorage).start();
});
