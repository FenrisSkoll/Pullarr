/* Local organization only; exact provider evidence and ownership stay server-owned. */
const CollectionsUI = (() => {
    const kinds = ['unknown', 'series', 'limited_series', 'trade_paperback', 'hardcover', 'deluxe', 'omnibus', 'absolute', 'one_shot', 'anthology', 'compendium', 'other'];
    const label = value => String(value ?? '').replaceAll('_', ' ');
    const messages = {revision_conflict: 'This Collection changed. Reload and review your edit; stale edits are not merged.',
        search_expired: 'This provider search expired or the application restarted. Search again. Accepted publications and decisions are still saved.',
        bounded: 'A safe Collection limit was reached. Narrow the request.', publication_identity_conflict: 'Exact identities conflict. No publications were merged.',
        invalid_tree: 'That move would create a cycle or exceed eight hierarchy levels.', invalid_parent: 'That parent is not permitted.',
        duplicate_sibling_title: 'A subgroup with that title already exists here.', task_unavailable: 'The provider task could not be queued.'};
    function text(parent, tag, value = '') { const e = document.createElement(tag); e.textContent = String(value ?? ''); parent.appendChild(e); return e; }
    function button(parent, title, action, disabled = false) { const b = text(parent, 'button', title); b.type = 'button'; b.disabled = disabled; b.onclick = action; return b; }
    function table(parent, headings) { const wrap = text(parent, 'div'); wrap.className = 'collection-table'; const t = text(wrap, 'table');
        const row = text(text(t, 'thead'), 'tr'); for (const h of headings) text(row, 'th', h).scope = 'col'; return text(t, 'tbody'); }
    function field(parent, title, value = '', options = null) { const l = text(parent, 'label', title); const e = text(l, options ? 'select' : 'input');
        e.setAttribute('aria-label', title);
        if (options) for (const [v, name] of options) { const o = text(e, 'option', name); o.value = v; }
        else e.maxLength = title === 'Description' ? 4000 : 200;
        e.value = value; return e; }
    class Controller {
        constructor(root, api, sleep = ms => new Promise(r => setTimeout(r, ms))) { this.root = root; this.api = api; this.sleep = sleep; this.versions = {}; this.tree = null; this.node = null; this.pending = false; }
        el(id) { return this.root.querySelector('#collections-' + id); }
        version(key) { return this.versions[key] = (this.versions[key] || 0) + 1; }
        current(key, version) { return this.versions[key] === version; }
        message(value) { this.el('message').textContent = value; }
        error(e) { this.message(messages[e?.reason] || 'Request could not complete. Reload saved state before retrying. No provider internals are displayed.'); }
        async start() {
            this.el('create').onclick = () => this.editNode(true);
            this.el('subgroup').onclick = () => this.editNode(false, true);
            this.el('edit').onclick = () => this.editNode();
            this.el('delete').onclick = () => this.deleteNode();
            this.el('local-form').onsubmit = e => { e.preventDefault(); this.local(); };
            this.el('search-form').onsubmit = e => { e.preventDefault(); this.search(); };
            this.el('decision').onchange = () => this.suggestions();
            this.el('refresh-suggestions').onclick = () => this.suggestions();
            this.el('cancel').onclick = () => { if (!this.pending) this.el('dialog').close(); };
            this.el('dialog').addEventListener('cancel', e => { if (this.pending) e.preventDefault(); });
            this.el('dialog').addEventListener('close', () => this.invoker?.focus());
            await this.list();
            const id = Number(new URLSearchParams(location.search).get('collection'));
            if (Number.isSafeInteger(id) && id > 0) await this.open(id);
        }
        modal(title, build, confirm, submit) {
            const d = this.el('dialog'); if (d.open || this.pending) return;
            this.invoker = document.activeElement; this.el('dialog-title').textContent = title;
            this.el('dialog-body').replaceChildren(); const values = build(this.el('dialog-body'));
            this.el('confirm').textContent = confirm;
            this.el('dialog-form').onsubmit = async e => { e.preventDefault(); if (this.pending) return;
                this.pending = true; this.el('confirm').disabled = true; this.el('cancel').disabled = true;
                try { await submit(values); d.close(); this.message('Saved. Library files and monitoring were not changed by Collection edits.'); }
                catch (error) { this.error(error); }
                finally { this.pending = false; this.el('confirm').disabled = false; this.el('cancel').disabled = false; }
            };
            d.showModal(); (d.querySelector('input,select,button') || d).focus();
        }
        async list(after = 0) {
            const v = this.version('list');
            try { const p = await this.api('GET', '/collections', null, {after, limit: 50}); if (!this.current('list', v)) return;
                const c = this.el('list'); c.replaceChildren(); if (!p.items.length) text(c, 'p', 'No Collections yet. Create your own local hierarchy.');
                for (const item of p.items) button(c, item.title, () => this.open(item.id));
                button(c, 'First Collections page', () => this.list(), after === 0);
                button(c, 'Next Collections page', () => this.list(p.next_after), p.next_after === null);
            } catch (e) { this.error(e); }
        }
        async open(id, node = null) {
            const v = this.version('tree'); for (const key of ['publications', 'suggestions', 'local', 'search']) this.version(key);
            try { const tree = await this.api('GET', `/collections/${id}`); if (!this.current('tree', v)) return;
                this.tree = tree; this.node = tree.nodes.find(n => n.id === node) || tree.nodes.find(n => n.parent_id === null);
                history.replaceState(null, '', `${location.pathname}?collection=${id}`);
                this.el('workspace').hidden = false; this.el('title').textContent = tree.nodes.find(n => n.parent_id === null).title;
                const c = tree.completeness; this.el('completeness').textContent = `${c.in_library} / ${c.total} accepted publications In Library · ${c.external} Not in Library · ${c.unresolved} ambiguous. Pending suggestions do not count.`;
                this.el('node-title').textContent = `${this.node.title} · ${this.node.effective_monitored ? 'Monitored for discovery' : 'Not monitored for discovery'}`;
                const container = this.el('tree'); container.replaceChildren();
                const render = (parent, id) => { const ul = text(parent, 'ul'); for (const n of tree.nodes.filter(x => x.parent_id === id)) {
                    const li = text(ul, 'li'); const b = button(li, `${n.title} (${label(n.monitoring)})`, () => this.open(tree.id, n.id));
                    if (n.id === this.node.id) b.setAttribute('aria-current', 'true');
                    if (tree.nodes.some(x => x.parent_id === n.id)) { const d = text(li, 'details'); d.open = true; text(d, 'summary', 'Subgroups'); render(d, n.id); }
                }}; render(container, null);
                const quality = text(container, 'a', 'Quality Profile Inheritance');
                quality.href = `${typeof url_base === 'string' ? url_base : ''}/settings/quality?node=${this.node.id}`;
                this.el('search-results').replaceChildren(); this.el('local-results').replaceChildren();
                await Promise.all([this.publications(), this.suggestions()]);
            } catch (e) { if (this.current('tree', v)) this.error(e); }
        }
        editNode(create = false, child = false) {
            const old = create || child ? null : this.node, tree = this.tree, node = this.node;
            this.modal(create ? 'Create Collection' : child ? 'Add subgroup' : 'Edit / move subgroup', c => {
                const title = field(c, 'Title', old?.title || ''), description = field(c, 'Description', old?.description || ''); title.required = true;
                const monitoring = field(c, 'Collection monitoring', old?.monitoring || (create ? 'unmonitored' : 'inherit'), ['inherit', 'monitored', 'unmonitored'].map(x => [x, label(x)]));
                const kind = create ? null : field(c, 'Kind hint', old?.kind || 'unknown', kinds.map(x => [x, label(x)]));
                const parent = !create && (child || old.parent_id !== null) ? field(c, 'Parent subgroup', child ? node.id : old.parent_id, tree.nodes.map(n => [n.id, n.title])) : null;
                const position = create ? null : field(c, 'Sibling position', old?.position || 0);
                if (position) { position.type = 'number'; position.min = '0'; position.max = '256'; }
                text(c, 'p', 'This edits local organization only. Deleting or moving subgroups never changes library paths.'); return {title, description, monitoring, kind, parent, position};
            }, 'Save Collection structure', async f => {
                const data = {title: f.title.value, description: f.description.value, monitoring: f.monitoring.value};
                const result = await this.api('POST', create ? '/collections' : `/collections/${tree.id}/nodes`, create ? data : {...data,
                    revision: tree.revision, node_id: old?.id ?? null, kind: f.kind.value, parent_id: f.parent ? Number(f.parent.value) : null, position: Number(f.position.value)});
                await this.list(); await this.open(result.id, old?.id);
            });
        }
        deleteNode() {
            const tree = this.tree, node = this.node;
            this.modal('Delete Collection subtree', c => text(c, 'p', `Delete ${node.title}, all child subgroups, memberships and their suggestions. Local volumes, files, identities and C2 are preserved.`), 'Delete subtree', async () => {
                await this.api('POST', `/collections/${tree.id}/nodes/${node.id}/delete`, {revision: tree.revision, confirmed: true});
                if (node.parent_id === null) { this.el('workspace').hidden = true; this.tree = null; history.replaceState(null, '', location.pathname); }
                else await this.open(tree.id); await this.list();
            });
        }
        async publications(offset = 0) {
            const v = this.version('publications'), tree = this.tree, node = this.node;
            try { const p = await this.api('GET', `/collections/${tree.id}/publications`, null, {node_id: node.id, offset, limit: 50}); if (!this.current('publications', v)) return;
                const c = this.el('publications'); c.replaceChildren(); if (!p.items.length) text(c, 'p', 'No accepted publications in this subtree.');
                const t = table(c, ['Publication', 'Local status / content', 'Exact sources', 'Membership / provenance', 'Actions']);
                for (const item of p.items) { const row = text(t, 'tr'); text(row, 'td', `${item.title} (${item.year ?? 'Unknown year'}) · ${label(item.kind)}`);
                    const state = text(row, 'td'); text(state, 'span', item.status === 'in_library' ? 'In Library' : item.status === 'ambiguous' ? 'Link Ambiguous' : 'Not in Library');
                    text(state, 'p', `Content: ${label(item.content_context.state)} (${item.content_context.owned_constituents}/${item.content_context.claims} known claims). This is not publication ownership or a complete publication inventory.`);
                    text(row, 'td', item.refs.map(r => `${r.provider}:${r.provider_id}`).join(' · ')); const memberships = text(row, 'td');
                    for (const m of item.memberships) { text(memberships, 'p', `${m.node_title}: ${label(m.source)}${m.note ? ' · ' + m.note : ''}`);
                        const d = text(memberships, 'details'); text(d, 'summary', 'Accepted evidence'); text(d, 'p', m.evidence.explanation || label(m.evidence.kind)); }
                    text(memberships, 'p', item.effective_monitored ? 'Monitored for discovery' : 'Not monitored for discovery');
                    const actions = text(row, 'td'); if (item.local_volume_id) { const a = text(actions, 'a', 'Open local volume'); a.href = `${url_base}/volumes/${item.local_volume_id}`; }
                    else if (item.status === 'external') button(actions, 'Add to Library', () => this.addLibrary(item));
                    button(actions, 'Edit membership / kind', () => this.editMembership(item));
                }
                button(c, 'Previous publications', () => this.publications(Math.max(0, offset - 50)), offset === 0);
                button(c, 'Next publications', () => this.publications(offset + 50), !p.has_next);
            } catch (e) { if (this.current('publications', v)) this.error(e); }
        }
        editMembership(item) {
            const tree = this.tree;
            this.modal('Publication membership', c => { text(c, 'p', `Existing memberships: ${item.memberships.map(m => m.node_title).join(', ')}. Additional membership does not silently move the publication.`);
                const localMemberships = item.memberships.filter(m => m.collection_id === tree.id);
                const source = field(c, 'Current membership', localMemberships[0].node_id, localMemberships.map(m => [m.node_id, m.node_title]));
                const action = field(c, 'Action', 'edit', [['edit', 'Edit note/order'], ['add', 'Add another membership'], ['move', 'Move membership'], ['remove', 'Remove membership'], ['kind', 'Set publication kind (shared across Collections)']]);
                const target = field(c, 'Target subgroup', this.node.id, tree.nodes.map(n => [n.id, n.title]));
                const note = field(c, 'Membership note', item.memberships[0].note); note.maxLength = 1000;
                const position = field(c, 'Membership position', item.memberships[0].position); position.type = 'number'; position.min = '0';
                const kind = field(c, 'Publication kind', item.kind, kinds.map(k => [k, label(k)])); return {source, action, target, note, position, kind};
            }, 'Save membership', async f => {
                const n = f.action.value === 'add' ? Number(f.target.value) : Number(f.source.value);
                await this.api('POST', `/collections/nodes/${n}/${f.action.value === 'kind' ? 'kind' : 'membership'}`, f.action.value === 'kind' ?
                    {revision: tree.revision, publication: item.id, kind: f.kind.value} : {revision: tree.revision, publication: item.id, action: f.action.value,
                        target: Number(f.target.value), note: f.note.value, position: Number(f.position.value)});
                await this.open(tree.id, this.node.id);
            });
        }
        async local(after = 0) {
            const v = this.version('local');
            try { const p = await this.api('GET', '/maintenance/volumes', null, {q: this.el('local-query').value, after, limit: 50}); if (!this.current('local', v)) return;
                const c = this.el('local-results'); c.replaceChildren(); for (const vol of p.items) button(c, `${vol.title} (${vol.year ?? '?'}) — add membership`, () => {
                    const tree = this.tree, node = this.node;
                    this.modal('Add local publication', d => text(d, 'p', `Add ${vol.title} to ${node.title}. Other memberships, files, monitoring and Wanted remain unchanged.`), 'Add membership', async () => {
                        await this.api('POST', `/collections/nodes/${node.id}/local`, {revision: tree.revision, volume_id: vol.id}); await this.open(tree.id, node.id);
                    });
                }); button(c, 'Next local volumes', () => this.local(p.next_after), p.next_after === null);
            } catch (e) { if (this.current('local', v)) this.error(e); }
        }
        async poll(handle, container, generation, key) {
            while (['queued', 'running'].includes(handle.state)) { text(container, 'p', `Provider task: ${handle.state}`); await this.sleep(2000);
                if (!this.current(key, generation)) return null;
                handle = await this.api('GET', `/collections/tasks/${handle.id}`);
            } return handle;
        }
        async search() {
            const v = this.version('search'), c = this.el('search-results'); c.replaceChildren();
            const submit = this.el('search-form').querySelector('button'); submit.disabled = true;
            try { let handle = await this.api('POST', `/collections/nodes/${this.node.id}/search`, {query: this.el('query').value, provider: this.el('provider').value, suggestions: this.el('proposals').checked});
                handle = await this.poll(handle, c, v, 'search'); if (!handle || !this.current('search', v)) return;
                await this.searchPage(handle.id); await this.suggestions();
            } catch (e) { if (this.current('search', v)) this.error(e); } finally { submit.disabled = false; }
        }
        async searchPage(id, offset = 0) {
            const v = this.version('search');
            try { const p = await this.api('GET', `/collections/tasks/${id}`, null, {offset, limit: 50}); if (!this.current('search', v)) return;
                const c = this.el('search-results'); c.replaceChildren(); text(c, 'p', `Task ${p.state}${p.reason ? ': ' + label(p.reason) : ''}`);
                for (const provider of p.providers) text(c, 'p', `${provider.provider}: ${label(provider.state)}${provider.reason ? ' — ' + label(provider.reason) : ''}`);
                const t = table(c, ['Exact provider result', 'Publisher / kind', 'Action']);
                for (const item of p.items) { const r = text(t, 'tr'); text(r, 'td', `${item.title} (${item.year ?? '?'}) · ${item.key}`); text(r, 'td', `${item.publisher} · ${label(item.kind)}`);
                    button(text(r, 'td'), 'Propose membership', async () => { try { await this.api('POST', `/collections/tasks/${id}/propose`, {result_key: item.key}); this.el('decision').value = 'pending'; await this.suggestions(); } catch (e) { this.error(e); } }); }
                button(c, 'Previous search results', () => this.searchPage(id, Math.max(0, offset - 50)), offset === 0);
                button(c, 'Next search results', () => this.searchPage(id, offset + 50), !p.has_next);
            } catch (e) { if (this.current('search', v)) this.error(e); }
        }
        async suggestions(offset = 0) {
            const v = this.version('suggestions'), tree = this.tree, node = this.node;
            try { const p = await this.api('GET', `/collections/nodes/${node.id}/suggestions`, null, {decision: this.el('decision').value, offset, limit: 50}); if (!this.current('suggestions', v)) return;
                const c = this.el('suggestions'); c.replaceChildren(); if (!p.items.length) text(c, 'p', 'No suggestions with this decision.');
                const t = table(c, ['Proposed publication', 'Evidence', 'Decision']);
                for (const item of p.items) { const r = text(t, 'tr'); text(r, 'td', `${item.title} · ${item.provider}:${item.provider_id}`); text(r, 'td', item.evidence.explanation);
                    const a = text(r, 'td'); text(a, 'p', label(item.decision));
                    for (const [decision, title] of item.decision === 'accepted' ? [] : item.decision === 'rejected' ? [['pending', 'Reconsider']] : [['accepted', 'Review / Accept'], ['rejected', 'Reject'], ['review_later', 'Review Later']]) {
                        button(a, title, () => this.modal(title, d => { text(d, 'p', `Publication: ${item.title}. Exact identity: ${item.provider}:${item.provider_id}.`);
                            text(d, 'p', `To: ${node.path.map(id => tree.nodes.find(n => n.id === id).title).join(' → ')}.`); text(d, 'p', item.evidence.explanation);
                            text(d, 'p', `Exact local matches: ${item.local_match_ids.length ? item.local_match_ids.join(', ') : 'none'}. Existing memberships: ${item.existing_memberships.map(m => m.title).join(', ') || 'none'}.`);
                            text(d, 'p', 'Acceptance adds only this membership. It does not add a library volume or change monitoring. Rejection is scoped to this exact node/evidence, not a global blacklist.');
                        }, decision === 'accepted' ? 'Accept membership' : title, async () => {
                            await this.api('POST', `/collections/suggestions/${item.id}/decision`, {revision: item.revision, collection_revision: tree.revision, decision}); await this.open(tree.id, node.id);
                        }));
                    }
                } button(c, 'Previous suggestions', () => this.suggestions(Math.max(0, offset - 50)), offset === 0); button(c, 'Next suggestions', () => this.suggestions(offset + 50), !p.has_next);
            } catch (e) { if (this.current('suggestions', v)) this.error(e); }
        }
        async addLibrary(item) {
            try { const roots = await this.api('GET', '/rootfolder');
                this.modal('Add publication to Library', c => { text(c, 'p', `Add ${item.title} using the normal Add Volume pipeline. Collection membership remains if acquisition fails. Automatic search and local monitoring are off.`);
                    const root = field(c, 'Configured root', roots[0]?.id || '', roots.map(r => [r.id, r.folder]));
                    const provider = field(c, 'Exact provider reference', item.refs[0].provider, item.refs.map(r => [r.provider, `${r.provider}:${r.provider_id}`])); return {root, provider};
                }, 'Add to Library', async f => {
                    const c = this.el('dialog-body'), v = this.version('add');
                    let result = await this.api('POST', `/collections/publications/${item.id}/add`, {root_id: Number(f.root.value), provider: f.provider.value, confirmed: true});
                    result = await this.poll(result, c, v, 'add'); if (result?.state !== 'complete') throw {reason: result?.reason};
                    await this.open(this.tree.id, this.node.id);
                });
            } catch (e) { this.error(e); }
        }
    }
    return {Controller, text, label, messages};
})();
if (typeof module !== 'undefined') module.exports = CollectionsUI;
if (typeof usingApiKey === 'function') usingApiKey().then(apiKey => {
    const api = async (method, path, body = null, params = {}) => {
        const response = await fetch(`${url_base}/api${path}?${new URLSearchParams({api_key: apiKey, ...params})}`, {
            method, headers: body === null ? {} : {'Content-Type': 'application/json'}, body: body === null ? undefined : JSON.stringify(body)});
        if (response.status === 401) { window.location.href = `${url_base}/login`; throw {reason: 'authentication_required'}; }
        const envelope = await response.json(); if (!response.ok) throw envelope.result; return envelope.result;
    };
    new CollectionsUI.Controller(document.getElementById('collections'), api).start();
});
