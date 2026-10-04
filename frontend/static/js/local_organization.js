/* Local publication authority is server-owned; this module presents receipts. */
const LocalOrganizationUI = (() => {
    const reasons = {
        multiple_issue_matches: 'Issue number is ambiguous.',
        issue_coverage_unresolved: 'Issue could not be determined.',
        unsupported_number_semantics: 'Issue numbering needs review.',
        title_disagreement: 'Publication title differs from this volume.',
        provider_identity_conflict: 'Embedded publication identity conflicts with the selection.',
        candidate_evidence_conflict: 'Local metadata contains conflicting evidence.',
        folder_assignment_required: 'Selected files must share one immediate parent folder.',
        existing_volume_folder_conflict: 'This publication already uses a different or unestablished folder.',
        publication_registration_conflict: 'Publication identity or folder ownership conflicts with the selection.',
        publication_registration_unavailable: 'Publication registration failed. Check the selected metadata provider.'
    };
    const name = path => String(path || '').split(/[\\/]/).pop();
    function text(parent, tag, value) {
        const node = parent.ownerDocument.createElement(tag);
        node.textContent = value; parent.appendChild(node); return node;
    }
    function panel(anchor, id) {
        let node = anchor.ownerDocument.getElementById(id);
        if (!node) {
            node = anchor.ownerDocument.createElement('section'); node.id = id;
            node.className = 'local-organization-results'; node.setAttribute('aria-live', 'polite');
            anchor.parentNode.appendChild(node);
        }
        node.replaceChildren(); return node;
    }
    function rows(parent, plans) {
        for (const plan of plans) {
            const row = text(parent, 'div', '');
            const ready = ['ready', 'no_changes'].includes(plan.status);
            text(row, 'p', `${name(plan.source)} → ${ready ? (plan.issue_labels || []).join(', ') || 'Ready to associate' : 'Needs review'}`);
            if (plan.publication) text(row, 'p', `Selected publication: ${plan.publication}`);
            if (!ready) {
                const reason = (plan.identification_reasons || []).map(code => reasons[code]).find(Boolean);
                text(row, 'p', reason || 'Issue matching or file ownership needs review. Files remain unchanged.');
                const link = text(row, 'a', 'Review issue match');
                if (Number.isSafeInteger(plan.volume_id)) link.href = `${typeof url_base === 'string' ? url_base : ''}/volumes/${plan.volume_id}`;
            }
        }
    }
    function result(parent, value) {
        parent.replaceChildren();
        const completed = (value.jobs || []).filter(job => job.state === 'completed');
        const failures = (value.jobs || []).filter(job => job.state !== 'completed');
        text(parent, 'h3', `${completed.length} files imported or associated.`);
        if (value.review?.length) text(parent, 'p', `${value.review.length} files need issue matching or folder review.`);
        rows(parent, value.review || []);
        if (failures.length) text(parent, 'p', `${failures.length} operations need recovery. Check Maintenance history before retrying.`);
        return completed;
    }
    function preview(parent, value, apply) {
        text(parent, 'h3', 'Local files preview');
        text(parent, 'p', 'Ready files can be associated. Files are not moved or renamed; provider metadata and missing files are unchanged.');
        rows(parent, value.plans);
        const button = text(parent, 'button', 'Apply ready associations'); button.type = 'button';
        button.disabled = !value.plans.some(p => ['ready', 'no_changes'].includes(p.status));
        button.onclick = async () => {
            if (button.disabled) return;
            button.disabled = true;
            const progress = text(parent, 'p', 'Associating selected files…');
            try { result(parent, await apply()); }
            catch (_) { progress.textContent = 'Association could not be confirmed. Inspect Maintenance history before retrying.'; }
        };
        parent.tabIndex = -1; parent.focus(); parent.scrollIntoView?.({block: 'nearest'});
    }
    return {panel, preview, result, rows, reasons};
})();
if (typeof module !== 'undefined') module.exports = LocalOrganizationUI;
