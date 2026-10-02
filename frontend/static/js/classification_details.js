/* Presentation only. The server supplies policy results and application facts. */
const ClassificationDetails = (() => {
    const text = (parent, label, value) => {
        const row = document.createElement('p');
        row.textContent = label + ': ' + (value === null || value === undefined ? 'Not recorded' : String(value));
        parent.append(row);
    };
    const mode = value => value === null ? 'Normal' : value;
    // Recorded reason -> wording only; no policy inputs or precedence here.
    const reasonText = reason => ({
        current_lock_preserved: 'The supplied lock preserved the stored value',
        all_issue_titles_volume_numbered: 'All canonical issue titles used Volume N numbering',
        sole_issue_physical_evidence: 'Admitted physical-format evidence for the sole issue',
        sole_issue_publication_evidence: 'Admitted publication-kind evidence for the sole issue',
        volume_title_omnibus_marker: 'Omnibus marker in the volume title',
        volume_title_one_shot_marker: 'One-shot marker in the volume title',
        volume_title_hardcover_marker: 'Hardcover marker in the volume title',
        issue_title_omnibus_label: 'Omnibus issue-title label',
        issue_title_hardcover_label: 'Hardcover issue-title label',
        issue_title_one_shot_label: 'One-shot issue-title label',
        volume_title_annual_exclusion: 'Annual exclusion in the volume title',
        description_annual_exclusion: 'Annual exclusion in the evaluated description segment',
        description_omnibus_marker: 'Omnibus marker in the evaluated description segment',
        description_one_shot_marker: 'One-shot marker in the evaluated description segment',
        description_hardcover_marker: 'Hardcover marker in the evaluated description segment',
        aged_single_issue_tpb: 'The sole issue exceeded the legacy 30-day age threshold at evaluation time',
        no_rule_matched: 'No classification rule matched these inputs'
    })[reason] || reason;
    function render(parent, data) {
        parent.replaceChildren();
        text(parent, 'Stored classification', mode(data.stored.value));
        text(parent, 'Currently locked', data.stored.locked);
        const p = data.provenance;
        if (p.status !== 'recorded') {
            text(parent, 'Why this value is stored', p.status === 'invalidated'
                ? 'Reason no longer trustworthy: value was assigned outside a provenance-aware application.'
                : 'Reason not recorded');
        } else {
            const labels = {automatic_decision: 'Automatic classification', explicit_selection: 'Explicitly selected',
                legacy_default_application: 'Legacy add default: no explicit mode; applied locked Normal'};
            text(parent, 'Why this value is stored', labels[p.application_kind] || p.application_kind);
            text(parent, 'Applied (UTC)', p.recorded_at);
            if (p.source !== null) {
                text(parent, 'Recorded source / winning reason', p.source + ' / ' + reasonText(p.reason) + ' [' + p.reason + ']');
                text(parent, 'Recorded policy', p.policy_id);
                text(parent, 'Evaluated (naive local clock)', p.evaluated_at);
                text(parent, 'Input scope', p.input_scope);
                text(parent, 'Matching Volume N titles / issue count', p.volume_numbered_count + ' / ' + p.issue_count);
                if (p.issue_date) text(parent, 'Sole issue date / age seconds', p.issue_date + ' / ' + p.age_seconds);
                for (const e of p.evidence) {
                    text(parent, e.axis + ' evidence', e.availability + ' — ' + e.disposition);
                    if (e.provider) text(parent, 'Provider / owner / field', e.provider + ' / ' + e.provider_id + ' / ' + e.source_field);
                    if (e.raw_value !== null) text(parent, 'Raw label', e.raw_value + (e.raw_truncated ? ' [bounded excerpt]' : ''));
                    if (e.normalized_value !== null) text(parent, 'Normalized observation', e.normalized_value);
                }
            }
            text(parent, 'Replay status', p.replay_status);
        }
        const control = data.last_control_action;
        text(parent, 'Last known control action', control ? control.action + ' — ' + control.occurred_at + ' (' + control.context + ')' : null);
        const current = data.current_evaluation;
        if (current) {
            text(parent, 'Would classify now (not applied)', current.status === 'evaluated' ? mode(current.value) : current.status);
            text(parent, 'Current evaluation scope', current.input_scope + ' — provider evidence unavailable in this scope');
            if (current.status === 'evaluated') {
                text(parent, 'Current reason', reasonText(current.reason));
                text(parent, 'Current evaluation clock (naive local)', current.evaluated_at);
            }
        }
    }
    function setup(volumeId, apiKey) {
        const panel = document.querySelector('#classification-details');
        const body = document.querySelector('#classification-details-body');
        const evaluate = document.querySelector('#classification-evaluate');
        let generation = 0;
        async function load(current) {
            const token = ++generation;
            body.textContent = 'Loading classification details…';
            try {
                const data = await fetchAPI('/volumes/' + volumeId + '/classification', apiKey,
                    current ? {evaluate: 'true'} : {});
                if (token === generation) render(body, data.result);
            } catch (_) {
                if (token === generation) body.textContent = 'Classification details unavailable.';
            }
        }
        panel.addEventListener('toggle', () => { if (panel.open) load(false); });
        evaluate.addEventListener('click', () => load(true));
    }
    return {render, setup};
})();
