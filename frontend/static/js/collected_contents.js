// Explicit operator assertions, then separate exact-file coverage. All text is data.
function renderCollectedContents(container, data, actions) {
    container.replaceChildren();
    const line = text => {
        const p = document.createElement('p'); p.textContent = text; container.appendChild(p); return p;
    };
    const button = (label, callback, parent = container) => {
        const b = document.createElement('button'); b.type = 'button'; b.textContent = label;
        b.onclick = async () => {
            b.disabled = true;
            try { await callback(); } catch (_) { line('Action unavailable or preview changed. Reload and review again.'); }
            finally { b.disabled = false; }
        };
        parent.appendChild(b); return b;
    };
    const publication = x => `${x.series_name ?? x.source_series ?? ''} #${x.number ?? x.source_number ?? ''} (${x.provider ?? x.source_provider}:${x.provider_id ?? x.source_provider_id})`;
    line(`Collected publication: ${publication(data.target)}`);
    line('Reprint evidence does not prove completeness. A complete claim is your explicit confirmation for Pullarr ownership purposes. It does not affect ownership until applied to a selected local file.');
    const previewArea = document.createElement('section'); container.appendChild(previewArea);
    const preview = async (operation, body) => {
        const value = await actions.preview(operation, body);
        previewArea.replaceChildren();
        const p = document.createElement('p');
        p.textContent = operation === 'claim' ?
            `${publication(value.target)} → ${publication(value.source)}: ${value.kind}. ${value.warning} Evidence: ${value.evidence_outcome}. No new file coverage is applied. This retires ${value.prior_coverage_retired} previous coverage links; Wanted may reopen until explicitly reapplied.` :
            `${value.file.filepath} directly represents ${publication(value.target)}. Add coverage for: ${value.sources.map(publication).join('; ')}. These sources will count as owned; monitored missing issues will no longer be Wanted. No move, rename, direct-association change or ComicInfo write.`;
        previewArea.appendChild(p);
        if (value.evidence) {
            for (const edge of value.evidence) {
                const row = document.createElement('p');
                row.textContent = `${edge.provider} edge ${edge.edge_id}: ${edge.shape}, source story ${edge.origin_story ?? 'none'}, target story ${edge.target_story ?? 'none'}, ${edge.active ? 'active' : 'inactive'}; snapshot ${edge.snapshot_id}`;
                previewArea.appendChild(row);
            }
        }
        button(operation === 'claim' ? 'Confirm this exact claim' : 'Apply coverage to this exact file',
            () => actions.confirm(operation, {...body, preview_token: value.preview_token}), previewArea);
        button('Cancel preview', () => previewArea.replaceChildren(), previewArea);
    };
    const review = source => {
        line(`Material from ${publication(source)} is reprinted here. ${source.edge_count} explicit edges. ${source.local_issue_id == null ? 'External-only: cannot apply file coverage until added locally.' : `Local issue ${source.local_issue_id}.`}`);
        if (source.deleted) line('Source is marked deleted in the catalog. Review this exact identity carefully.');
        const body = {source_provider: source.provider, source_provider_id: source.provider_id};
        button('Review complete claim', () => preview('claim', {...body, kind: 'complete_issue_containment'}));
        button('Review partial claim', () => preview('claim', {...body, kind: 'partial_issue_content'}));
    };
    data.candidates.forEach(review);
    const manual = document.createElement('input'); manual.type = 'number'; manual.min = '1';
    manual.placeholder = 'Exact local source issue ID'; manual.setAttribute('aria-label', 'Exact local source issue ID');
    container.appendChild(manual);
    for (const [label, kind] of [['complete', 'complete_issue_containment'], ['partial', 'partial_issue_content']]) {
        button(`Review manual ${label} claim (no graph receipt)`, () => {
            const id = Number(manual.value);
            if (!Number.isSafeInteger(id) || id <= 0) throw new Error('Exact local ID required');
            return preview('claim', {source_local_id: id, kind, manual: true});
        });
    }
    line('Claim history and file application are separate. Select exact active complete claims to apply; none are preselected.');
    const selected = new Set();
    for (const claim of data.claims) {
        line(`${publication(claim)} — ${claim.kind}; ${claim.retired_at == null ? 'active' : 'retired'} operator claim ${claim.id}; applied files: ${claim.applied_files}. ${claim.evidence_count ? `${claim.evidence_count} receipt edges; ${claim.evidence_no_longer_current} no longer current.` : 'Operator-confirmed; no graph evidence receipt.'}`);
        if (claim.graph_deleted) line('Source is marked deleted in the catalog; the operator claim has not been revoked automatically.');
        button('View original evidence receipt', () => actions.history(claim.id, previewArea));
        if (claim.retired_at == null) button('Review claim revocation', () => actions.retire('content-claims', claim.id, previewArea));
        if (claim.retired_at == null && claim.kind === 'complete_issue_containment' && claim.source_local_id != null) {
            const label = document.createElement('label');
            const check = document.createElement('input'); check.type = 'checkbox'; check.checked = false;
            check.onchange = () => check.checked ? selected.add(claim.id) : selected.delete(claim.id);
            label.appendChild(check); label.appendChild(document.createTextNode(` Apply ${publication(claim)}`));
            container.appendChild(label);
        } else if (claim.source_local_id == null) line('Not in local library metadata — cannot apply file coverage.');
    }
    const files = document.createElement('select'); files.setAttribute('aria-label', 'Exact collected file');
    const blank = document.createElement('option'); blank.value = ''; blank.textContent = 'Select one direct file explicitly'; files.appendChild(blank);
    for (const file of data.files) {
        const option = document.createElement('option'); option.value = String(file.id); option.textContent = file.filepath; files.appendChild(option);
    }
    container.appendChild(files);
    if (data.files_truncated) line('File list capped at 100. Use exact-ID API review for another eligible file.');
    button('Preview selected file coverage', () => preview('coverage', {file_id: Number(files.value), claim_ids: [...selected]}));
    for (const coverage of data.coverage) {
        line(`Coverage ${coverage.id}: file ${coverage.file_id ?? coverage.original_file_id}, source local issue ${coverage.source_issue_id ?? coverage.original_source_id}; ${coverage.valid ? 'valid ownership evidence' : 'retired or no longer applicable'}.`);
        if (coverage.retired_at == null) button('Review coverage revocation', () => actions.retire('content-coverage', coverage.id, previewArea));
    }
    if (data.offset > 0) button('First page', () => actions.load(0));
    if (data.next_offset !== null) button('Next page', () => actions.load(data.next_offset));
}

function setupCollectedContents(issueId, apiKey) {
    const panel = document.querySelector('#issue-contents');
    const container = document.querySelector('#issue-contents-content');
    panel.open = false; panel.dataset.issueId = String(issueId); container.replaceChildren();
    const setupGeneration = String(Number(panel.dataset.generation || 0) + 1);
    panel.dataset.generation = setupGeneration;
    let loaded = false, generation = 0;
    const current = () => panel.dataset.issueId === String(issueId) && panel.dataset.generation === setupGeneration;
    const post = async (path, body) => (await (await sendAPI('POST', path, apiKey, {}, body)).json()).result;
    const actions = {
        load: async (offset = 0) => {
            const request = ++generation;
            const response = await fetchAPI(`/issues/${issueId}/contents`, apiKey, {offset});
            if (current() && generation === request) renderCollectedContents(container, response.result, actions);
        },
        preview: (kind, body) => post(`/issues/${issueId}/contents/${kind}-preview`, body),
        confirm: async (kind, body) => {
            if (!current()) return;
            await post(`/issues/${issueId}/contents/${kind === 'claim' ? 'claim-confirm' : 'coverage-apply'}`, body);
            if (current()) await actions.load();
            await refreshIssueOwnership(apiKey);
        },
        history: async (id, destination) => {
            const result = await fetchAPI(`/content-claims/${encodeURIComponent(id)}`, apiKey);
            if (current()) destination.textContent = JSON.stringify(result.result, null, 2);
        },
        retire: async (kind, id, destination) => {
            const route = `/${kind}/${encodeURIComponent(id)}`;
            const result = await post(`${route}/retire-preview`, {});
            if (!current()) return;
            destination.replaceChildren();
            const warning = document.createElement('p');
            warning.textContent = `${result.warning} Affected source local IDs: ${result.affected.map(x => x.source_issue_id).join(', ') || 'none'}.`;
            destination.appendChild(warning);
            const confirm = document.createElement('button'); confirm.type = 'button'; confirm.textContent = 'Confirm revocation';
            confirm.onclick = async () => {
                confirm.disabled = true;
                try {
                    if (!current()) return;
                    await post(`${route}/retire`, {preview_token: result.preview_token});
                    if (current()) await actions.load();
                    await refreshIssueOwnership(apiKey);
                } catch (_) { if (current()) destination.textContent = 'Revocation failed or changed. Review again.'; }
            };
            destination.appendChild(confirm);
        }
    };
    panel.ontoggle = async () => {
        if (!panel.open || loaded) return;
        loaded = true;
        try { await actions.load(); } catch (_) { if (current()) container.textContent = 'Contents unavailable. Reopen to retry.'; loaded = false; }
    };
    const ownership = document.querySelector('#issue-ownership');
    ownership.textContent = 'Loading ownership evidence…';
    fetchAPI(`/issues/${issueId}/ownership`, apiKey).then(response => {
        if (!current()) return;
        const state = response.result.issues[0];
        ownership.textContent = `Ownership: ${state.state.replaceAll('_', ' ')}. ` + state.collected_coverage.map(
            x => `Covered by ${x.target_title} #${x.target_number}, file ${x.filepath}, operator claim ${x.claim_id}.`).join(' ');
    }).catch(() => { if (current()) ownership.textContent = 'Ownership evidence unavailable.'; });
}

let ownershipGeneration = 0;
async function refreshIssueOwnership(apiKey) {
    const generation = ++ownershipGeneration;
    try {
        const response = await fetchAPI(`/volumes/${volume_id}/ownership`, apiKey);
        if (generation !== ownershipGeneration) return;
        for (const state of response.result.issues) {
            const row = ViewEls.issues_list.querySelector(`tr[data-id="${state.issue_id}"]`);
            if (!row) continue;
            const entry = new IssueEntry(state.issue_id, apiKey, row);
            entry.setDownloaded(state.owned);
            const labels = {none: 'Missing', direct: 'Owned directly', collected: 'Owned via collected edition', direct_and_collected: 'Owned directly + collected'};
            entry.status.title = labels[state.state];
            let badge = entry.status.querySelector('.ownership-label');
            if (!badge) {
                badge = document.createElement('span'); badge.className = 'ownership-label'; entry.status.appendChild(badge);
            }
            badge.textContent = state.collected_coverage.length ? labels[state.state] : '';
        }
    } catch (_) {
        if (generation !== ownershipGeneration) return;
        for (const status of ViewEls.issues_list.querySelectorAll('.issue-status'))
            status.title = 'Canonical ownership unavailable; only direct-file status is shown. Reload to retry.';
    }
}
