/* Explicit selection only. All compatibility, points and prose are backend receipts. */
function renderManualDDL(tbody, batch, act, view = {source: '', sort: 'match'}) {
    const doc = tbody.ownerDocument;
    tbody.pendingSelections ??= new Set();
    tbody.replaceChildren();
    const cell = (row, text) => {
        const node = doc.createElement('td');
        node.textContent = text;
        row.appendChild(node);
        return node;
    };
    const button = (parent, label, handler) => {
        const node = doc.createElement('button');
        node.type = 'button';
        node.textContent = label;
        node.onclick = handler;
        parent.appendChild(node);
        return node;
    };
    const notice = doc.createElement('tr');
    const status = cell(notice, `Search: ${batch.state}. ${batch.results.length} results. Results expire after ${batch.expires_in} seconds.`);
    status.colSpan = 5;
    tbody.appendChild(notice);
    if (batch.automatic_policy) {
        const row = doc.createElement('tr');
        cell(row, `Automatic policy preview: ${batch.automatic_policy.outcome}. This manual search does not grab automatically.`).colSpan = 5;
        tbody.appendChild(row);
    }
    for (const error of batch.errors) {
        const row = doc.createElement('tr');
        cell(row, `Source ${error.source ?? ''}: ${error.code}`).colSpan = 5;
        tbody.appendChild(row);
    }
    const controls = doc.createElement('tr');
    const controlCell = cell(controls, ''); controlCell.colSpan = 5;
    function select(label, entries, value, changed) {
        const wrap = doc.createElement('label'); wrap.textContent = label + ' ';
        const node = doc.createElement('select'); node.setAttribute('aria-label', label);
        for (const [key, title] of entries) {
            const option = doc.createElement('option'); option.value = key; option.textContent = title; node.appendChild(option);
        }
        node.value = value; node.onchange = () => changed(node.value);
        wrap.appendChild(node); controlCell.appendChild(wrap);
    }
    const sources = [...new Set(batch.results.map(r => r.explanation.candidate.source.name))].sort();
    select('Source filter', [['', 'All sources'], ...sources.map(s => [s, s])], view.source,
        source => renderManualDDL(tbody, batch, act, {...view, source}));
    select('Sort results', [['match','Match'],['source_asc','Source ascending'],['source_desc','Source descending'],['title','Title'],['size','Size']], view.sort,
        sort => renderManualDDL(tbody, batch, act, {...view, sort}));
    tbody.appendChild(controls);
    const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
    const results = batch.results.map((result, index) => ({result, index})).filter(v => !view.source || v.result.explanation.candidate.source.name === view.source);
    results.sort((a, b) => {
        const x = a.result.explanation.candidate, y = b.result.explanation.candidate;
        const primary = view.sort.startsWith('source') ? compare(x.source.name.toLowerCase(), y.source.name.toLowerCase()) * (view.sort === 'source_desc' ? -1 : 1) :
            view.sort === 'title' ? compare(x.raw_title.toLowerCase(), y.raw_title.toLowerCase()) : view.sort === 'size' ? (x.size_bytes || 0) - (y.size_bytes || 0) : 0;
        return primary || a.index - b.index;
    });
    for (const {result} of results) {
        const row = doc.createElement('tr');
        const dto = result.explanation;
        const reason = (dto.rejections || []).concat(dto.review || [], dto.undetermined || []).map(i => dto.entries[i]?.message).find(Boolean);
        cell(row, `${dto.headline || dto.state}${reason ? ' — ' + reason : ''}`);
        const title = cell(row, dto.candidate.raw_title);
        const details = doc.createElement('details');
        const summary = doc.createElement('summary');
        summary.textContent = 'Why this result?';
        details.appendChild(summary);
        const explanation = doc.createElement('div');
        renderReleaseExplanation(explanation, dto);
        details.appendChild(explanation);
        for (const discovery of result.discovery || []) {
            const provenance = doc.createElement('p');
            provenance.textContent = `Query: ${discovery.query}. Source categories: ${discovery.categories.join(', ')}.`;
            details.appendChild(provenance);
        }
        title.appendChild(details);
        if (result.quality) {
            const quality = doc.createElement('p'), q = result.quality;
            quality.textContent = `${q.claims.quality_class.replaceAll('_', ' ')} · Claimed · Profile group ${q.group === null ? 'unresolved' : q.group + 1} · ${q.result.replaceAll('_', ' ')}${q.reason ? ' · ' + q.reason.replaceAll('_', ' ') : ''}. Verified after download; no same-group or automatic downgrade replacement.`;
            title.appendChild(quality);
        }
        const protocol = {direct_download: 'Direct download', nzb: 'NZB', torrent: 'Torrent'}[result.mechanism];
        cell(row, `${dto.candidate.source.name}${protocol ? ' / ' + protocol : ''}`);
        const bytes = dto.candidate.size_bytes;
        const size = cell(row, bytes == null ? 'Unknown' : bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(2)} MB`);
        if (bytes != null) size.title = `${bytes} bytes`;
        const unavailable = {
            no_enabled_nzb_client: 'No enabled NZB download client is configured.',
            no_enabled_torrent_client: 'No enabled torrent download client is configured.',
            manual_nzb_client_selection_required: 'Choose a default manual NZB client in Wanted / Upgrades.',
            manual_torrent_client_selection_required: 'Choose an enabled torrent client in Download Clients.',
            client_configuration_invalid: 'Download client configuration needs attention.',
            acquisition_policy_blocked: 'Acquisition is blocked by the current client or quality policy.',
            blocklisted: 'This release is blocklisted.'
        };
        const actions = cell(row, result.blocked ? 'Blocklisted. ' : result.operationally_available === false ?
            unavailable[result.unavailable_reason] || 'Acquisition unavailable. Check the source, client and quality policy.' : '');
        if (result.operationally_available === false) {
            const link = doc.createElement('a'); link.textContent = 'Download Client settings';
            link.href = `${typeof url_base === 'string' ? url_base : ''}/settings/downloadclients`; actions.appendChild(link);
        }
        const feedback = doc.createElement('p');
        actions.appendChild(feedback);
        let selectedClient = result.selected_client_id;
        let clientSelect;
        if ((result.clients || []).length > 1) {
            clientSelect = doc.createElement('select'); clientSelect.setAttribute('aria-label', 'Download client');
            const placeholder = doc.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Choose download client';
            clientSelect.appendChild(placeholder);
            for (const client of result.clients) {
                const option = doc.createElement('option'); option.value = client.id; option.textContent = client.name;
                clientSelect.appendChild(option);
            }
            clientSelect.value = selectedClient || '';
            actions.appendChild(clientSelect);
        }
        const pendingKey = `${batch.search_id}:${result.selection_id}`;
        let submitting = tbody.pendingSelections.has(pendingKey);
        const execute = async (force, offeringId) => {
            if (submitting || tbody.pendingSelections.has(pendingKey)) return;
            if (clientSelect && !selectedClient) { feedback.textContent = 'Choose a download client first.'; return; }
            submitting = true;
            tbody.pendingSelections.add(pendingKey);
            if (clientSelect) clientSelect.disabled = true;
            for (const b of actions.querySelectorAll('button')) b.disabled = true;
            feedback.textContent = 'Resolving selected release…';
            try {
                const value = await act(batch.search_id, result.selection_id,
                    {action: 'download', force, ...(selectedClient ? {client_id: selectedClient} : {}), ...(offeringId ? {offering_id: offeringId} : {})});
                if (value.state === 'offering_selection_required') {
                    submitting = false;
                    tbody.pendingSelections.delete(pendingKey);
                    feedback.textContent = 'Select the actual offering. No download has started.';
                    for (const offering of value.offerings) {
                        const panel = doc.createElement('div');
                        renderReleaseExplanation(panel, offering.explanation);
                        actions.appendChild(panel);
                        button(panel, force ? 'Force this offering' : 'Download this offering',
                            () => execute(force, offering.offering_id));
                    }
                } else {
                    feedback.textContent = 'Dispatched to the download queue.';
                }
            } catch (error) {
                feedback.textContent = 'Download could not be confirmed. Check acquisition history before making another selection.';
            }
        };
        const normal = button(actions, 'Download', () => execute(false));
        normal.disabled = submitting || !result.download_eligible || !!clientSelect && !selectedClient;
        const force = button(actions, 'Download anyway', () => {
            force.disabled = true;
            feedback.textContent = 'Download this exact release despite unresolved bibliographic matching? Automatic acquisition will never make this override. Security, client and quality checks still apply.';
            const confirm = button(actions, 'Confirm download anyway', () => execute(true));
            const cancel = button(actions, 'Cancel override', () => {
                confirm.disabled = cancel.disabled = true;
                force.disabled = !result.force_eligible; feedback.textContent = 'Override cancelled.';
            });
        });
        force.disabled = submitting || !result.force_eligible || !!clientSelect && !selectedClient;
        if (clientSelect) clientSelect.onchange = () => {
            selectedClient = clientSelect.value;
            normal.disabled = submitting || !result.download_eligible || !selectedClient;
            force.disabled = submitting || !result.force_eligible || !selectedClient;
        };
        if (!result.blocked) button(actions, 'Blocklist', async () => {
            try {
                await act(batch.search_id, result.selection_id, {action: 'block'});
                normal.disabled = true;
                feedback.textContent = 'Blocklisted. Evaluation and score are unchanged.';
            } catch (_) { feedback.textContent = 'Blocklist update could not be confirmed.'; }
        });
        if (result.blocked) button(actions, 'Unblock', async () => {
            try {
                await act(batch.search_id, result.selection_id, {action: 'unblock'});
                feedback.textContent = 'Unblocked. Re-search for current eligibility.';
            } catch (_) { feedback.textContent = 'Blocklist update could not be confirmed.'; }
        });
        tbody.appendChild(row);
    }
}
