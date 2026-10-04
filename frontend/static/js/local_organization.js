/* Local publication authority is server-owned; this module presents receipts. */
const LocalOrganizationUI = (() => {
    const reasons = {
        local_file_unavailable: 'File is unavailable. Refresh Local Scan after checking it.',
        local_file_changed: 'File changed during inspection. Refresh Local Scan.',
        blocking_candidate_diagnostic: 'Archive or local metadata could not be inspected.',
        multiple_issue_matches: 'Issue number is ambiguous.',
        issue_coverage_unresolved: 'Issue could not be determined.',
        unsupported_number_semantics: 'Issue numbering needs review.',
        title_disagreement: 'Publication title differs from this volume.',
        provider_identity_conflict: 'Embedded publication identity conflicts with the selection.',
        candidate_evidence_conflict: 'Local metadata contains conflicting evidence.',
        folder_assignment_required: 'Select one configured library root for this publication.',
        existing_volume_folder_conflict: 'This publication has an unavailable or unestablished managed folder. Repair its folder before importing.',
        filesystem_target_occupied: 'The destination already contains a file. Resolve the collision before importing; the source is preserved.',
        managed_folder_ownership_conflict: 'Another publication owns the destination folder.',
        source_unavailable_or_changed: 'The source file changed or is unavailable. Refresh the import preview.',
        target_occupied: 'The destination became occupied. The source is preserved.',
        source_missing_or_changed: 'The source file changed or is unavailable.',
        permission_denied: 'Filesystem access was denied.',
        disk_full: 'The destination has insufficient free space.',
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
    function rows(parent, plans, review) {
        for (const plan of plans) {
            const row = text(parent, 'div', '');
            const ready = ['ready', 'no_changes', 'associated'].includes(plan.status);
            text(row, 'p', `${name(plan.source)} → ${ready ? (plan.status === 'associated' ? 'Associated: ' : plan.status === 'no_changes' ? 'Already associated: ' : '') + ((plan.issue_labels || []).join(', ') || 'Ready to associate') : 'Needs review'}`);
            if (plan.publication) text(row, 'p', `Selected publication: ${plan.publication}`);
            else if (plan.authority) text(row, 'p', `Selected publication: ${plan.authority.provider}:${plan.authority.id}`);
            if (!ready) {
                const reason = [...(plan.identification_reasons || []), ...(plan.diagnostics || []).map(d => d.code)].map(code => reasons[code]).find(Boolean);
                text(row, 'p', reason || 'Issue matching or file ownership needs review. Files remain unchanged.');
                if (review && plan.review_available) {
                    const button = text(row, 'button', 'Review issue match'); button.type = 'button';
                    button.onclick = event => { event?.preventDefault(); event?.stopPropagation(); return review(plan, button); };
                } else if (Number.isSafeInteger(plan.volume_id)) {
                    const link = text(row, 'a', 'Open publication');
                    link.href = `${typeof url_base === 'string' ? url_base : ''}/volumes/${plan.volume_id}`;
                }
            }
        }
    }
    function result(parent, value) {
        parent.replaceChildren();
        const completed = (value.jobs || []).filter(job => job.state === 'completed');
        const failures = (value.jobs || []).filter(job => job.state !== 'completed');
        const publications = new Set(completed.map(j => j.volume_id).filter(Number.isSafeInteger));
        text(parent, 'h3', publications.size ? `Imported ${completed.length} files into ${publications.size} publications.` : `${completed.length} files imported or associated.`);
        for (const job of completed) if (job.publication) text(parent, 'p', `${name(job.source)} → ${job.publication}`);
        if (value.review?.length) text(parent, 'p', `${value.review.length} files need issue matching or folder review.`);
        rows(parent, value.review || []);
        if (failures.length) text(parent, 'p', `${failures.length} operations need recovery. Check Maintenance history before retrying.`);
        for (const job of failures) {
            text(parent, 'p', `${name(job.source)} → ${job.publication || 'Selected publication'}. ${reasons[job.error] || 'The operation needs recovery review.'}`);
            const link = text(parent, 'a', 'Review in Maintenance');
            link.href = `${typeof url_base === 'string' ? url_base : ''}/maintenance`;
        }
        return completed;
    }
    const reviewErrors = {
        stale_preview: 'This preview is stale. Refresh Local Scan and try again.',
        association_lookup_failed: 'Association lookup failed. Refresh Local Scan and try again.',
        override_required: 'Review the existing or embedded issue association before replacing it.',
        invalid_issue_selection: 'Choose valid issues from this managed volume.',
        publication_or_file_conflict: 'Publication identity or file ownership conflicts require separate review. This issue action cannot override them.',
        file_busy: 'This file or volume has another operation in progress. Try a fresh Local Scan after it finishes.'
    };
    function preview(parent, value, apply, reviewAPI) {
        const token = {}; parent.reviewToken = token;
        let reviewing = false, applying = false;
        const current = () => parent.reviewToken === token;
        text(parent, 'h3', 'Local files preview');
        text(parent, 'p', 'Ready files can be associated. Files are not moved or renamed; provider metadata and missing files are unchanged.');
        if (value.message) text(parent, 'p', value.message);
        if (value.reviewMessage) text(parent, 'p', value.reviewMessage);
        const open = async (plan, trigger) => {
            if (reviewing || applying || !current()) return;
            reviewing = true; trigger.disabled = true; button.disabled = true;
            const panel = text(parent, 'section', ''); panel.className = 'local-issue-review';
            panel.setAttribute('aria-label', 'Review issue match'); panel.tabIndex = -1;
            text(panel, 'h3', 'Review issue match');
            const status = text(panel, 'p', 'Loading issue evidence…');
            const cancel = text(panel, 'button', 'Cancel'); cancel.type = 'button';
            cancel.onclick = () => {
                panel.hidden = true; panel.remove(); reviewing = false; trigger.disabled = false;
                button.disabled = !value.plans.some(p => p.status === 'ready'); trigger.focus();
            };
            panel.focus(); panel.scrollIntoView?.({block:'nearest'});
            try {
                const detail = await reviewAPI.load(plan.row_id);
                if (!current() || panel.hidden) return;
                status.textContent = '';
                text(panel, 'p', `File: ${detail.filename}`);
                text(panel, 'p', `Publication: ${detail.publication} (managed volume ${detail.volume_id})`);
                for (const entry of detail.evidence) text(panel, 'p', `${entry.label}: ${entry.value || 'Unavailable'}`);
                text(panel, 'h4', 'Why review is required');
                for (const reason of detail.reasons) text(panel, 'p', reason);
                text(panel, 'p', 'Existing association: ' + (detail.existing.map(e => `#${e.label}${e.forced ? ' (manual)' : ''}`).join(', ') || 'None'));
                text(panel, 'h4', 'Choose issue');
                const search = text(panel, 'input', ''); search.type = 'search'; search.maxLength = 100;
                search.setAttribute('aria-label', 'Find issue by number or title');
                const find = text(panel, 'button', 'Find issues'); find.type = 'button';
                const choices = text(panel, 'div', ''); choices.className = 'local-issue-choices';
                let inputs = [];
                const show = data => {
                    choices.replaceChildren(); inputs = [];
                    for (const issue of data.issues) {
                        const label = text(choices, 'label', '');
                        const input = text(label, 'input', ''); input.type = 'checkbox'; input.value = String(issue.id);
                        input.checked = data.existing.some(e => e.issue_id === issue.id); inputs.push(input);
                        text(label, 'span', issue.label); text(choices, 'br', '');
                    }
                    if (data.more) text(choices, 'p', 'Showing 200 issues. Search by issue number or title for more.');
                };
                show(detail);
                find.onclick = async () => {
                    if (find.disabled) return; find.disabled = true;
                    try { const data = await reviewAPI.load(plan.row_id, search.value); if (current() && !panel.hidden) show(data); }
                    catch (error) { status.textContent = reviewErrors[error.code] || 'Issue evidence could not be loaded. Try again.'; }
                    finally { find.disabled = false; }
                };
                const save = text(panel, 'button', 'Save association'); save.type = 'button';
                save.disabled = !!detail.blocked;
                if (detail.blocked) status.textContent = reviewErrors[detail.blocked];
                let overrideSelection = '';
                save.onclick = async () => {
                    if (save.disabled || !current()) return;
                    const ids = inputs.filter(i => i.checked).map(i => Number(i.value));
                    if (!ids.length) { status.textContent = reviewErrors.invalid_issue_selection; return; }
                    const previous = detail.existing.map(e => e.issue_id).filter(Number.isSafeInteger).sort((a,b)=>a-b);
                    const embedded = (detail.embedded_issue_ids || []).slice().sort((a,b)=>a-b);
                    const selection = JSON.stringify(ids.slice().sort((a,b)=>a-b));
                    const replacing = previous.length && JSON.stringify(previous) !== selection;
                    const contradicting = embedded.length && JSON.stringify(embedded) !== selection;
                    if ((replacing || contradicting) && overrideSelection !== selection) {
                        overrideSelection = selection;
                        status.textContent = replacing ? 'This replaces the existing issue association shown above. Confirm the selected issues.' : 'This overrides the embedded exact issue identity shown above. Confirm the selected issues.';
                        save.textContent = replacing ? 'Replace association' : 'Override embedded identity';
                        return;
                    }
                    save.disabled = true; cancel.disabled = true; find.disabled = true;
                    status.textContent = 'Saving association…';
                    try {
                        const updated = await reviewAPI.save(plan.row_id, ids, !!(replacing || contradicting));
                        if (!current()) return;
                        parent.replaceChildren(); preview(parent, {...updated,message:'Association saved.'}, apply, reviewAPI);
                    } catch (error) {
                        status.textContent = reviewErrors[error.code] || 'Association could not be confirmed. Retry the same selection or refresh Local Scan.';
                        save.disabled = false; cancel.disabled = false; find.disabled = false;
                    }
                };
            } catch (error) { status.textContent = reviewErrors[error.code] || 'Issue evidence could not be loaded. Cancel and try again.'; }
        };
        rows(parent, value.plans, reviewAPI ? open : null);
        const button = text(parent, 'button', 'Apply ready associations'); button.type = 'button';
        button.disabled = !value.plans.some(p => p.status === 'ready');
        button.onclick = async () => {
            if (button.disabled || reviewing) return;
            applying = true;
            button.disabled = true;
            const progress = text(parent, 'p', 'Associating selected files…');
            try {
                const applied = await apply();
                if (!current()) return;
                const completed = (applied.jobs || []).filter(j => j.state === 'completed');
                const plans = value.plans.map(p => completed.some(j => j.source === p.source) ? {...p,status:'associated',review_available:false} : p);
                parent.replaceChildren();
                preview(parent, {...value,plans,message:`${completed.length} files imported or associated.`,
                    reviewMessage:`${applied.review?.length || 0} files need issue matching or folder review.`}, apply, reviewAPI);
            }
            catch (error) {
                progress.textContent = reviewErrors[error.code] || 'Association could not be confirmed. Refresh Local Scan and try again.';
                applying = false; button.disabled = false;
            }
        };
        parent.tabIndex = -1; parent.focus(); parent.scrollIntoView?.({block: 'nearest'});
    }
    return {panel, preview, result, rows, reasons};
})();
if (typeof module !== 'undefined') module.exports = LocalOrganizationUI;
