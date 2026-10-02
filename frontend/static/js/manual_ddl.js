/* Explicit selection only. All compatibility, points and prose are backend receipts. */
function renderManualDDL(tbody, batch, act) {
    const doc = tbody.ownerDocument;
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
    for (const result of batch.results) {
        const row = doc.createElement('tr');
        const dto = result.explanation;
        cell(row, dto.state);
        const title = cell(row, dto.candidate.raw_title);
        const details = doc.createElement('details');
        const summary = doc.createElement('summary');
        summary.textContent = 'Why this result?';
        details.appendChild(summary);
        const explanation = doc.createElement('div');
        renderReleaseExplanation(explanation, dto);
        details.appendChild(explanation);
        title.appendChild(details);
        if (result.quality) {
            const quality = doc.createElement('p'), q = result.quality;
            quality.textContent = `${q.claims.quality_class.replaceAll('_', ' ')} · Claimed · Profile group ${q.group === null ? 'unresolved' : q.group + 1} · ${q.result.replaceAll('_', ' ')}${q.reason ? ' · ' + q.reason.replaceAll('_', ' ') : ''}. Verified after download; no same-group or automatic downgrade replacement.`;
            title.appendChild(quality);
        }
        cell(row, `${dto.candidate.source.name}${result.mechanism ? ' / ' + result.mechanism : ''}`);
        cell(row, dto.candidate.size_bytes === null ? 'Unknown' : `${dto.candidate.size_bytes} bytes`);
        const actions = cell(row, result.blocked ? 'Blocklisted. ' : result.operationally_available === false ? 'Transport unavailable. ' : '');
        const feedback = doc.createElement('p');
        actions.appendChild(feedback);
        const execute = async (force, offeringId) => {
            for (const b of actions.querySelectorAll('button')) b.disabled = true;
            feedback.textContent = 'Resolving selected release…';
            try {
                const value = await act(batch.search_id, result.selection_id,
                    {action: 'download', force, ...(offeringId ? {offering_id: offeringId} : {})});
                if (value.state === 'offering_selection_required') {
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
                feedback.textContent = `Download stopped: ${error.message}. Re-search if the selection is stale.`;
            }
        };
        const normal = button(actions, 'Download', () => execute(false));
        normal.disabled = !result.download_eligible;
        const force = button(actions, 'Force download', () => {
            if (window.confirm('Override bibliographic compatibility for this exact release? Security checks still apply.')) execute(true);
        });
        force.disabled = !result.force_eligible;
        if (!result.blocked) button(actions, 'Blocklist', async () => {
            try {
                await act(batch.search_id, result.selection_id, {action: 'block'});
                normal.disabled = true;
                feedback.textContent = 'Blocklisted. Evaluation and score are unchanged.';
            } catch (error) { feedback.textContent = error.message; }
        });
        if (result.blocked) button(actions, 'Unblock', async () => {
            try {
                await act(batch.search_id, result.selection_id, {action: 'unblock'});
                feedback.textContent = 'Unblocked. Re-search for current eligibility.';
            } catch (error) { feedback.textContent = error.message; }
        });
        tbody.appendChild(row);
    }
}
