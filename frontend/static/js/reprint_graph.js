// Catalog identities are separate from REST observations; text only, no inference.
function renderReprints(container, data, loadPage) {
    container.replaceChildren();
    const line = text => {
        const p = document.createElement('p');
        p.textContent = text;
        container.appendChild(p);
    };
    line('Source: Grand Comics Database catalog. Explicit material-reprint evidence, not complete issue coverage. Separate from REST story observations.');
    if (!data.available) { line('Catalog graph not yet synced for this issue.'); return; }
    line(`Last graph sync: ${new Date(data.snapshot.observed_at * 1000).toISOString()}`);
    line('Last sync does not establish catalog freshness; the operator-supplied dump may be older.');
    line(`Catalog observation: ${data.snapshot.fingerprint}`);
    const issues = new Map(data.issues.map(x => [x.provider_id, x]));
    const stories = new Map(data.stories.map(x => [x.provider_id, x]));
    const endpoint = (iid, sid) => {
        const i = issues.get(iid);
        const publication = `${i.series_name} #${i.number} (GCD issue ${iid})`;
        return sid === null ? `material from ${publication}` :
            `story ${stories.get(sid).title} (GCD story ${sid}) of ${publication}`;
    };
    for (const direction of ['incoming', 'outgoing']) {
        line(`${direction === 'incoming' ? 'Incoming' : 'Outgoing'} reprints`);
        for (const edge of data[direction]) {
            line(`${endpoint(edge.origin_issue, edge.origin_story)} is reprinted in ${endpoint(edge.target_issue, edge.target_story).replace(/^material from /, '')}.`);
            line(`GCD reprint ${edge.provider_id} · ${edge.shape} · ${edge.active ? 'active evidence' : 'inactive/deleted endpoint evidence'}`);
            if (edge.modified) line(`Source edge modified: ${edge.modified}`);
            if (edge.notes) line(`Source note (not a coverage claim): ${edge.notes}`);
        }
    }
    for (const issue of data.issues) {
        if (Number.isSafeInteger(issue.volume_id) && Number.isSafeInteger(issue.local_issue_id)) {
            const a = document.createElement('a');
            a.href = `${url_base}/volumes/${issue.volume_id}#issue-${issue.local_issue_id}`;
            a.textContent = `Local issue ${issue.local_issue_id}: ${issue.series_name} #${issue.number}`;
            container.appendChild(a);
        } else line(`External-only GCD issue ${issue.provider_id}: ${issue.series_name} #${issue.number}`);
    }
    for (const credit of data.credits) {
        line(`GCD story ${credit.story_id} — ${credit.role} (role ${credit.role_id}): ${credit.creator_name} (creator ${credit.creator_id}); name ${credit.name_text} (name ${credit.name_id}); credit ${credit.provider_id}`);
        if (credit.credited_as) line(`Credited as: ${credit.credited_as}`);
        if (credit.signed_as) line(`Signed as: ${credit.signed_as}`);
        if (credit.uncertain) line('Credit is uncertain in the source.');
        if (credit.deleted || credit.name_deleted || credit.creator_deleted) line('Inactive/deleted credit endpoint evidence.');
    }
    if (data.credits_truncated) line('Credit display capped at 1,000 records on this page.');
    if (data.offset > 0 || data.next_offset !== null) {
        for (const [label, offset] of [['First page', 0], ['Next page', data.next_offset]]) {
            if (offset === null) continue;
            const button = document.createElement('button');
            button.type = 'button'; button.textContent = label;
            button.onclick = () => loadPage(offset);
            container.appendChild(button);
        }
    }
}

function setupReprints(issueId, apiKey) {
    const panel = document.querySelector('#issue-reprints');
    const content = document.querySelector('#issue-reprints-content');
    panel.open = false; panel.dataset.issueId = String(issueId);
    content.replaceChildren();
    let loaded = false, generation = 0;
    const loadStories = async (offset = 0) => {
        const current = ++generation;
        try {
            const response = await fetchAPI(`/issues/${issueId}/catalog-stories`, apiKey, {offset});
            if (panel.dataset.issueId !== String(issueId) || generation !== current) return;
            renderCatalogStories(content, response.result, loadStories, () => load());
        } catch (_) {
            if (panel.dataset.issueId === String(issueId) && generation === current)
                content.textContent = 'Catalog stories unavailable. Reopen to retry.';
            loaded = false;
        }
    };
    const load = async (offset = 0) => {
        const current = ++generation;
        try {
            const data = await fetchAPI(`/issues/${issueId}/reprints`, apiKey, {offset});
            if (panel.dataset.issueId === String(issueId) && generation === current) {
                renderReprints(content, data.result, load);
                const button = document.createElement('button');
                button.type = 'button'; button.textContent = 'Catalog story identities / credits';
                button.onclick = () => loadStories(); content.appendChild(button);
            }
        } catch (_) {
            if (panel.dataset.issueId === String(issueId) && generation === current) {
                content.textContent = 'Catalog graph unavailable. Reopen to retry.'; loaded = false;
            }
        }
    };
    panel.ontoggle = () => { if (panel.open && !loaded) { loaded = true; load(); } };
}

function renderCatalogStories(container, data, loadPage, back) {
    container.replaceChildren();
    const line = text => {
        const p = document.createElement('p'); p.textContent = text; container.appendChild(p);
    };
    line('Grand Comics Database catalog identities. Not linked heuristically to REST observations.');
    if (!data.available) line('Catalog story identities not yet synced.');
    for (const story of data.stories) {
        line(`GCD story ${story.provider_id}, parent issue ${story.issue_id}, sequence ${story.sequence}: ${story.title}${story.deleted ? ' (inactive/deleted)' : ''}`);
    }
    for (const c of data.credits) {
        line(`Story ${c.story_id} — ${c.role} [${c.role_id}], credit ${c.provider_id}: ${c.creator_name} [creator ${c.creator_id}], ${c.name_text} [name ${c.name_id}${c.official ? ', official' : ''}]. Credited as: ${c.credited_as}; signed as: ${c.signed_as}${c.uncertain ? '; uncertain' : ''}${c.deleted || c.creator_deleted || c.name_deleted ? '; inactive/deleted' : ''}`);
    }
    if (data.credits_truncated) line('Credit display capped at 1,000 records.');
    for (const [label, action] of [['Reprints', back], ['First story page', () => loadPage(0)],
        ['Next story page', data.next_offset === null ? null : () => loadPage(data.next_offset)]]) {
        if (!action) continue;
        const button = document.createElement('button'); button.type = 'button';
        button.textContent = label; button.onclick = action; container.appendChild(button);
    }
}
