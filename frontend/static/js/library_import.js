/* Local discovery and explicit one-step import. No draft writes during scans. */
const importRows = new Map(), importCatalogues = new Map(), importProposals = new Map();
const importEvidence = new Map(), importCatalogueQueue = new Map(), importSuggestions = new Map();
const importRowsByPath = new Map();
let importKey, importOffset, importSearchContext, importGeneration = 0, importMatchGeneration = 0;
let importSearchGeneration = 0, importArtwork, importCoverObserver, importScope, importPageLoading = false;
let importDiscovered = 0, importBulkApplying = false;
let importAutoCatalogueQueue = Promise.resolve();
const imp = id => document.getElementById(id);
const importError = message => { imp('import-error').textContent = message; };
const identityKey = row => `${row.identity.provider}:${row.identity.id}`;
function importReason(reason) {
    if (reason?.startsWith('target_exists: ')) return `Target already exists: ${reason.slice(15)}`;
    return ({source_unavailable:'Source file is missing', source_unreadable:'Source file is unreadable',
        source_outside_library_roots:'Source is outside configured library roots',
        issue_not_in_publication:'Selected issue is not in this publication',
        file_owned_by_other_publication:'File belongs to another publication',
        file_busy_or_recovery_ambiguous:'An earlier file operation may still be active; source and target were left untouched',
        seeding_source_use_keep_in_place:'This is a seeding source; choose Keep in place',
        invalid_target_filename:'Choose a filename with the same archive extension',
        copy_verification_failed:'Copy could not be verified; source was kept',
        source_changed_before_import:'Source changed during import; retry after it is stable',
        source_changed_during_copy:'Source changed during copying; source was kept',
        OSError:'Filesystem operation failed; source was kept'}[reason] || reason || 'Import failed');
}

function filterImport() {
    const query = imp('import-filter').value.toLowerCase();
    for (const row of importRows.values())
        row.element.hidden = !!query && !`${row.data.filepath} ${row.element.querySelector('.publication-title').textContent} ${row.status || ''}`.toLowerCase().includes(query);
    updateSelectionSummary();
}
function setImportStatus(row, status) {
    row.status = status;
    row.element.querySelector('.import-status').textContent = status;
    filterImport();
}
function showIssueChoice(row) {
    const cell = row.element.querySelector('.issue-column'); cell.replaceChildren();
    const issues = (row.catalogue?.issues || []).filter(issue => row.issueIds.includes(issue.id));
    const label = document.createElement('span');
    label.textContent = issues.length ? issues.map(i => `${String(i.number).startsWith('#') ? '' : '#'}${i.number}${i.title ? ' — ' + i.title : ''}`).join(', ') : 'Choose issue';
    const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Edit issues';
    button.onclick = () => editIssues(row);
    cell.append(label, button);
}
async function loadCatalogue(row) {
    if (!row.identity) throw new Error('Choose a publication first');
    const key = identityKey(row);
    const identity = {...row.identity};
    const generation = importGeneration, rowVersion = row.choiceVersion;
    const previous = importCatalogueQueue.get(key) || Promise.resolve();
    const task = previous.catch(() => {}).then(async () => {
        const seen = importEvidence.get(key) || new Set();
        const proposals = importProposals.get(key) || new Map();
        importEvidence.set(key, seen); importProposals.set(key, proposals);
        const group = [...importRows.values()].filter(item => item.identity &&
            identityKey(item) === key && !seen.has(item.data.filepath));
        for (let start = 0; start < group.length; start += 100) {
            const batch = group.slice(start, start + 100);
            const versions = new Map(batch.map(item => [item, item.choiceVersion]));
            const response = await sendAPI('POST', '/libraryimport/catalog', importKey, {}, {
                provider:identity.provider, provider_id:identity.id,
                files:batch.map(item => item.data.filepath)
            });
            if (!response.ok) throw new Error('Issue catalogue unavailable');
            const result = (await response.json()).result;
            if (generation !== importGeneration) return;
            importCatalogues.set(key, result);
            for (const item of batch) {
                const path = item.data.filepath;
                seen.add(path); proposals.set(path, result.proposals?.[path]);
                if (item.identity && identityKey(item) === key && item.choiceVersion === versions.get(item)) {
                    item.catalogue = result; item.proposal = proposals.get(path);
                    if (!item.manual && !item.issueIds.length && item.proposal?.issue_ids?.length)
                        item.issueIds = item.proposal.issue_ids;
                    showIssueChoice(item);
                }
            }
        }
        if (row.identity && identityKey(row) === key && row.choiceVersion === rowVersion &&
                importCatalogues.has(key)) {
            row.catalogue = importCatalogues.get(key);
            row.proposal = proposals.get(row.data.filepath);
            if (!row.manual && !row.issueIds.length && row.proposal?.issue_ids?.length)
                row.issueIds = row.proposal.issue_ids;
            showIssueChoice(row);
        }
    });
    importCatalogueQueue.set(key, task);
    try { await task; }
    finally { if (importCatalogueQueue.get(key) === task) importCatalogueQueue.delete(key); }
}
async function editIssues(row) {
    try { await loadCatalogue(row); }
    catch (error) { setImportStatus(row, error.message); return; }
    showIssueChoice(row);
    const panel = document.createElement('section'); panel.className = 'import-issue-editor';
    const heading = document.createElement('p');
    heading.textContent = `${row.catalogue.title} (${row.catalogue.year || 'year unknown'}) · ${identityKey(row)} · ${row.catalogue.loaded_issue_count} loaded / ${row.catalogue.reported_issue_count ?? 'unknown'} reported · ${row.catalogue.completeness}`;
    const search = document.createElement('input'); search.type = 'search';
    search.placeholder = 'Search number or title'; search.setAttribute('aria-label', 'Find issue');
    const list = document.createElement('div'); list.className = 'import-issue-list';
    const chosen = new Set(row.issueIds);
    function render() {
        list.replaceChildren();
        const matches = row.catalogue.issues.filter(i => `${i.number} ${i.title || ''}`.toLowerCase().includes(search.value.toLowerCase()));
        for (const issue of matches.slice(0, 100)) {
            const label = document.createElement('label'), input = document.createElement('input');
            input.type = 'checkbox'; input.checked = chosen.has(issue.id);
            input.onchange = () => input.checked ? chosen.add(issue.id) : chosen.delete(issue.id);
            label.append(input, document.createTextNode(` ${issue.number}${issue.title ? ' · ' + issue.title : ''}${issue.date ? ' · ' + issue.date : ''}`));
            list.appendChild(label);
        }
        if (matches.length > 100) list.append(document.createTextNode('Narrow the search for more issues.'));
    }
    search.oninput = render; render();
    const apply = document.createElement('button'); apply.type = 'button'; apply.textContent = 'Use selected issues';
    apply.onclick = () => { row.issueIds = [...chosen]; row.manual = true; showIssueChoice(row); };
    panel.append(heading, search, list, apply); row.element.querySelector('.issue-column').append(panel); search.focus();
}
function choosePublication(row, identity, title, url, load=true, provenance='manual') {
    const userChosen = provenance === 'manual';
    if (row.identity && identityKey(row) === `${identity.provider}:${identity.id}` && row.userChosen && !userChosen) return;
    const changed = !row.identity || identityKey(row) !== `${identity.provider}:${identity.id}`;
    if (changed) {
        row.choiceVersion = (row.choiceVersion || 0) + 1;
        row.catalogue = null; row.proposal = null; row.issueIds = []; row.manual = false;
    }
    row.identity = identity; row.userChosen = userChosen; row.provenance = provenance;
    const name = row.element.querySelector('.publication-title');
    name.textContent = title; name.classList.remove('hidden');
    const source = {manual:'Selected manually', suggestion:'Suggested by ComicVine', embedded:'From file metadata'}[provenance];
    row.element.querySelector('.publication-meta').textContent = `${source} · ${identity.provider} · ID ${identity.id}`;
    const button = row.element.querySelector('.publication-choice');
    button.textContent = 'Change publication';
    button.setAttribute('aria-label', `Change publication for ${row.data.file_title}`);
    const link = row.element.querySelector('.provider-link');
    const safe = MetadataSearchPresentation.safeLink(url);
    if (safe) link.href = safe;
    else link.removeAttribute('href');
    link.classList.toggle('hidden', !safe);
    showIssueChoice(row);
    filterImport();
    if (load && (changed || !row.catalogue)) loadCatalogue(row).catch(() =>
        setImportStatus(row, 'Catalogue unavailable; retry Edit issues'));
}
function updateSelectionSummary() {
    const rows = [...importRows.values()];
    const importing = rows.filter(row => row.element.querySelector('.import-file-select').checked).length;
    const bulkRows = rows.filter(row => row.element.querySelector('.bulk-match-select').checked);
    const outside = bulkRows.filter(row => row.element.hidden).length;
    imp('import-selection-count').textContent = `${importing} selected for import`;
    imp('bulk-selection-count').textContent = `${bulkRows.length} selected for bulk matching${outside ? ` (${outside} outside current filter)` : ''}`;
    imp('loaded-count').textContent = `${rows.length} loaded of ${importDiscovered} discovered`;
    imp('import-button').textContent = `Import selected (${importing})`;
    imp('open-bulk-match').textContent = `Bulk match (${bulkRows.length})`;
    imp('open-bulk-match').disabled = !bulkRows.length;
    imp('clear-bulk-match').classList.toggle('hidden', !bulkRows.length);
    const all = imp('selectall-input');
    all.checked = !!rows.length && importing === rows.length;
    all.indeterminate = importing > 0 && importing < rows.length;
}
function openPublicationSearch(row=null) {
    const targets = row ? [row] : [...importRows.values()].filter(item =>
        item.element.querySelector('.bulk-match-select').checked);
    if (!targets.length) return;
    importSearchContext = {mode:row ? 'single' : 'bulk', ids:targets.map(item => item.data.row_id),
        generation:importGeneration, trigger:row ? row.element.querySelector('.publication-choice') : imp('open-bulk-match')};
    imp('match-context').textContent = row ? `Selecting a publication for ${row.data.file_title}. Only this file will change.` :
        `Matching ${targets.length} explicitly chosen ${targets.length === 1 ? 'file' : 'files'}${targets.filter(item => item.element.hidden).length ?
            `, including ${targets.filter(item => item.element.hidden).length} outside the current filter` : ''}. Import selection is unchanged.`;
    showWindow('cv-window');
    requestAnimationFrame(() => {
        if (imp('cv-window').hasAttribute('show-window')) imp('search-input').focus();
    });
}
function applyAutoSuggestion(row) {
    const suggestion = importSuggestions.get(row.data.filepath);
    if (!row.identity && suggestion?.id) {
        choosePublication(row, {provider:'comicvine', id:String(suggestion.id)},
            suggestion.title, suggestion.link, false, 'suggestion');
        const generation = importGeneration, matchRun = importMatchGeneration;
        importAutoCatalogueQueue = importAutoCatalogueQueue.catch(() => {}).then(async () => {
            if (generation === importGeneration && matchRun === importMatchGeneration &&
                    row.identity && !row.userChosen)
                await loadCatalogue(row).catch(() => setImportStatus(row, 'Catalogue unavailable; retry Edit issues'));
        });
    }
}
function addImportRow(data) {
    const element = document.querySelector('.pre-build-els .li-result').cloneNode(true);
    const row = {data, element, identity:null, catalogue:null, issueIds:[], manual:false,
        userChosen:false, provenance:null, choiceVersion:0, status:''};
    importRows.set(data.row_id, row);
    importRowsByPath.set(data.filepath, row);
    element.dataset.rowid = data.row_id;
    const file = element.querySelector('.file-column'); file.textContent = data.file_title; file.title = data.filepath;
    element.querySelector('.import-file-select').setAttribute('aria-label', `Import ${data.file_title}`);
    element.querySelector('.bulk-match-select').setAttribute('aria-label', `Match ${data.file_title} in bulk`);
    element.querySelector('.publication-choice').setAttribute('aria-label', `Choose publication for ${data.file_title}`);
    const details = document.createElement('details'), summary = document.createElement('summary');
    summary.textContent = 'Path and evidence';
    const evidence = document.createElement('p');
    evidence.textContent = `${data.filepath} · ${data.size} bytes${data.embedded ? ' · ComicInfo: ' + data.embedded.series + ' #' + data.embedded.number : ''}`;
    details.append(summary, evidence); file.append(details);
    element.querySelector('.publication-choice').onclick = () => openPublicationSearch(row);
    element.querySelector('.import-file-select').onchange = updateSelectionSummary;
    element.querySelector('.bulk-match-select').onchange = updateSelectionSummary;
    if (data.metadata_source) choosePublication(row, data.metadata_source, data.embedded?.series || data.file_title, '', true, 'embedded');
    else showIssueChoice(row);
    applyAutoSuggestion(row);
    return element;
}
async function scanImport(reset=true) {
    if (importPageLoading || (!reset && importOffset === null)) return;
    importPageLoading = true;
    const generation = reset ? ++importGeneration : importGeneration;
    if (reset) {
        ++importMatchGeneration;
        importRows.clear(); importRowsByPath.clear(); importOffset = 0;
        importSuggestions.clear(); importCatalogues.clear(); importProposals.clear();
        importEvidence.clear(); importCatalogueQueue.clear();
        importAutoCatalogueQueue = Promise.resolve();
        importSearchContext = null; importDiscovered = 0;
        const folder = imp('folder-filter-input').value.trim();
        importScope = {folder_limit:Number(imp('scan-folder-limit').value)};
        if (imp('target-input').value === 'false' && folder)
            importScope.folder_filter = encodeURIComponent(folder);
        document.querySelector('.proposal-list').replaceChildren();
        imp('list-window').classList.add('hidden'); imp('loading-window').classList.remove('hidden');
        imp('start-window').classList.add('hidden');
        imp('scan-summary').textContent = '';
        imp('matching-status').textContent = '';
        imp('bulk-result-status').textContent = '';
        imp('import-filter').value = '';
        imp('rate-limit-banner').classList.add('hidden');
        importError('');
        imp('stop-matching').classList.add('hidden');
        imp('selectall-input').checked = true;
    }
    const params = {...importScope, offset:importOffset, page_size:100};
    imp('import-next-page').disabled = true;
    try {
        const page = (await fetchAPI('/libraryimport/files', importKey, params)).result;
        if (generation !== importGeneration) return;
        importOffset = page.next_offset;
        const counts = page.counts;
        importDiscovered = counts.discovered;
        const reason = counts.limited ? 'Traversal safety bound reached' :
            counts.not_yet_scanned_folders ? `${counts.not_yet_scanned_folders} eligible folders outside selected limit` :
            'Folder scan complete';
        imp('scan-summary').textContent = `${counts.scanned_folders} of ${counts.eligible_folders} eligible folders scanned · ` +
            `${counts.discovered} unregistered archives · ${counts.already_managed} already in library · ` +
            `${reason} · ${importOffset === null ? 'All result pages shown' : 'More result pages available'}`;
        const body = document.querySelector('.proposal-list');
        for (const item of page.items)
            if (!importRowsByPath.has(item.filepath))
                body.appendChild(addImportRow(item));
        filterImport();
        imp('import-next-page').classList.toggle('hidden', importOffset === null);
        imp('list-window').classList.remove('hidden'); imp('loading-window').classList.add('hidden');
        imp('no-result-window').classList.toggle('hidden', importRows.size > 0);
        updateSelectionSummary();
        if (reset && imp('auto-match-input').checked) void suggestImportPublications(++importMatchGeneration);
    } catch (_) {
        if (generation !== importGeneration) return;
        imp('loading-window').classList.add('hidden'); imp('start-window').classList.remove('hidden');
        importError('Local scan failed. Check the selected folder and retry.');
    } finally { importPageLoading = false; imp('import-next-page').disabled = false; }
}
async function suggestImportPublications(run) {
    const current = () => run === importMatchGeneration;
    const budget = Math.min(importScope.folder_limit, Number(imp('match-folder-limit').value));
    const scope = {...importScope, folder_limit:budget, page_size:100};
    const all = [];
    let offset = 0;
    imp('stop-matching').classList.remove('hidden');
    try {
        do {
            const page = (await fetchAPI('/libraryimport/files', importKey, {...scope, offset})).result;
            if (!current()) return;
            all.push(...page.items);
            offset = page.next_offset;
            imp('matching-status').textContent = `Preparing suggestions for ${all.length} of ${page.counts.discovered} files in ` +
                `${page.counts.scanned_folders} eligible folders (limit ${budget}).`;
        } while (offset !== null && current());
        const groups = new Map();
        for (const item of all) {
            const title = item.parsed?.title?.trim().toLowerCase();
            if (!title) continue;
            const key = `${title}|${item.parsed?.year || ''}`;
            if (!groups.has(key)) groups.set(key, {representative:item.filepath, paths:[]});
            groups.get(key).paths.push(item.filepath);
        }
        const entries = [...groups.values()];
        for (let start = 0; start < entries.length && current(); start += 10) {
            if (start) {
                await new Promise(resolve => setTimeout(resolve, 10000));
                if (!current()) return;
            }
            const batch = entries.slice(start, start + 10);
            const response = await sendAPI('POST', '/libraryimport/suggestions', importKey, {},
                {files:batch.map(group => group.representative)});
            if (!current()) return;
            if (!response.ok) throw new Error('ComicVine suggestions unavailable');
            const results = (await response.json()).result;
            if (!current()) return;
            const byPath = new Map(results.map(item => [item.filepath, item.cv]));
            for (const group of batch) {
                const cv = byPath.get(group.representative);
                if (!cv?.id) continue;
                for (const path of group.paths) {
                    importSuggestions.set(path, cv);
                    const row = importRowsByPath.get(path);
                    if (row) applyAutoSuggestion(row);
                }
            }
            imp('matching-status').textContent = `Publication suggestions checked for ` +
                `${Math.min(start + 10, entries.length)} of ${entries.length} series groups in up to ${budget} folders.`;
        }
        if (current()) imp('matching-status').textContent += ' Matching complete; unmatched files remain editable.';
    } catch (_) {
        if (current()) {
            imp('rate-limit-banner').classList.remove('hidden');
            imp('matching-status').textContent = 'Automatic matching stopped by provider error or rate limit; local files remain available.';
        }
    } finally { if (current()) imp('stop-matching').classList.add('hidden'); }
}
function closeImportCover() {
    const viewer = imp('import-cover-lightbox');
    if (!viewer?.open) return;
    viewer.close();
    imp('import-cover-image').removeAttribute('src');
    const trigger = viewer._trigger;
    viewer._trigger = null;
    if (trigger?.isConnected) trigger.focus();
}
function resetImportSearch() {
    ++importSearchGeneration;
    importArtwork?.stop(); importArtwork = null;
    importCoverObserver?.disconnect(); importCoverObserver = null;
    closeImportCover();
}
function openImportCover(button, entry, source) {
    const viewer = imp('import-cover-lightbox'), image = imp('import-cover-image');
    if (!source || !viewer?.showModal) return;
    const generation = importSearchGeneration;
    viewer._trigger = button;
    imp('import-cover-caption').textContent = `${entry.title} (${entry.year || 'year unknown'}) · ` +
        `${entry.metadata_source?.provider || 'comicvine'}:${entry.metadata_source?.id || entry.comicvine_id}`;
    imp('import-cover-feedback').textContent = 'Loading cover…';
    imp('import-cover-resolution').classList.add('hidden');
    image.hidden = true;
    image.alt = `Cover of ${entry.title} (${entry.year || 'year unknown'})`;
    image.onload = () => {
        if (generation !== importSearchGeneration || !viewer.open) return;
        image.hidden = false;
        imp('import-cover-feedback').textContent = '';
        imp('import-cover-resolution').classList.toggle('hidden',
            image.naturalWidth >= 500 && image.naturalHeight >= 700);
    };
    image.onerror = () => {
        if (generation !== importSearchGeneration || !viewer.open) return;
        image.hidden = true;
        imp('import-cover-feedback').textContent = 'No cover available';
    };
    viewer.showModal();
    imp('import-cover-close').focus();
    image.src = source;
}
function reviewBulkChange(rows, identity) {
    const replacingManual = rows.filter(row => row.userChosen && row.identity &&
        identityKey(row) !== `${identity.provider}:${identity.id}`).length;
    const mixed = new Set(rows.filter(row => row.identity).map(identityKey)).size > 1;
    // A single review covers manual replacements or a broad/mixed selection.
    if (!replacingManual && rows.length <= 25 && !mixed) return Promise.resolve(true);
    const dialog = imp('bulk-match-review');
    imp('bulk-review-detail').textContent = `${rows.length} files will receive this publication; ` +
        `${replacingManual} existing manual matches will change. Issue choices for changed publications will be recalculated.`;
    return new Promise(resolve => {
        const finish = accepted => { dialog.close(); resolve(accepted); };
        imp('bulk-review-cancel').onclick = () => finish(false);
        imp('bulk-review-apply').onclick = () => finish(true);
        dialog.oncancel = event => { event.preventDefault(); finish(false); };
        dialog.showModal();
        imp('bulk-review-cancel').focus();
    });
}
async function applyBulkPublication(context, identity, entry) {
    if (importBulkApplying) return;
    importBulkApplying = true;
    try { await applyBulkPublicationOnce(context, identity, entry); }
    finally { importBulkApplying = false; }
}
async function applyBulkPublicationOnce(context, identity, entry) {
    if (context !== importSearchContext || context.generation !== importGeneration) return;
    const selected = context.ids.map(id => importRows.get(id)).filter(row =>
        row && row.element.querySelector('.bulk-match-select').checked);
    if (selected.length !== context.ids.length || !selected.length) return;
    if (!await reviewBulkChange(selected, identity)) return;
    if (context !== importSearchContext || context.generation !== importGeneration) return;
    for (const row of selected) choosePublication(row, identity, entry.title, entry.site_url, false);
    let catalogueFailed = false;
    try { await loadCatalogue(selected[0]); }
    catch (_) {
        catalogueFailed = true;
        selected.forEach(row => setImportStatus(row, 'Catalogue unavailable; retry Edit issues'));
    }
    if (context.generation !== importGeneration) return;
    for (const row of selected) row.element.querySelector('.bulk-match-select').checked = false;
    const proposed = selected.filter(row => row.proposal?.issue_ids?.length).length;
    imp('bulk-result-status').textContent = `Publication assigned to ${selected.length} ${selected.length === 1 ? 'file' : 'files'}; ` +
        `${proposed} issue suggestions found; ${selected.length - proposed} need review${catalogueFailed ? ' (catalogue unavailable)' : ''}.`;
    resetImportSearch(); closeWindow();
    imp('bulk-result-status').focus();
    updateSelectionSummary();
}
function importResult(entry, artwork, generation) {
    const identity = entry.metadata_source || {provider:'comicvine', id:String(entry.comicvine_id)};
    const tr = document.createElement('tr'); tr.className = 'search-result';
    const title = document.createElement('td'), action = document.createElement('td');
    const content = document.createElement('div'); content.className = 'import-search-content';
    const thumb = document.createElement('button'); thumb.type = 'button';
    thumb.className = 'import-cover-button';
    thumb.setAttribute('aria-label', `View full cover: ${entry.title} (${entry.year || 'year unknown'})`);
    const image = document.createElement('img');
    image.width = 48; image.height = 72; image.loading = 'lazy'; image.decoding = 'async';
    image.alt = ''; thumb.append(image);
    const placeholder = `${url_base}/static/img/cover-placeholder.svg`;
    const safeCover = MetadataSearchPresentation.safeLink(entry.cover_link);
    tr.dataset.cover = safeCover;
    image.src = safeCover || placeholder;
    thumb.disabled = !safeCover;
    image.onerror = () => {
        if (generation !== importSearchGeneration) return;
        tr.dataset.cover = ''; thumb.disabled = true; image.onerror = null; image.src = placeholder;
    };
    thumb.onclick = () => openImportCover(thumb, entry, tr.dataset.cover);
    const details = document.createElement('div'); details.className = 'import-search-details';
    const name = document.createElement('strong');
    name.textContent = `${entry.title} (${entry.year || 'year unknown'})`;
    const facts = document.createElement('p');
    facts.textContent = `${identity.provider}:${identity.id} · ${entry.issue_count ?? 'unknown'} issues` +
        (entry.publisher ? ` · ${entry.publisher}` : '');
    const safeProvider = MetadataSearchPresentation.safeLink(entry.site_url);
    if (safeProvider) {
        const titleLink = document.createElement('a');
        titleLink.href = safeProvider; titleLink.target = '_blank';
        titleLink.rel = 'noopener noreferrer'; titleLink.append(name);
        details.append(titleLink);
    } else details.append(name);
    details.append(facts);
    if (safeProvider) {
        const link = document.createElement('a');
        link.href = safeProvider; link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.textContent = 'View on provider';
        details.append(link);
    }
    content.append(thumb, details); title.append(content);
    const context = importSearchContext;
    const select = document.createElement('button'); select.type = 'button';
    if (context?.mode === 'bulk') {
        select.className = 'bulk-match';
        select.textContent = `Apply to ${context.ids.length} selected ${context.ids.length === 1 ? 'file' : 'files'}`;
        select.onclick = () => void applyBulkPublication(context, identity, entry);
    } else {
        select.textContent = 'Select for this file';
        select.onclick = () => {
            if (context !== importSearchContext || context?.generation !== importGeneration) return;
            const row = importRows.get(context.ids[0]);
            if (!row) return;
            choosePublication(row, identity, entry.title, entry.site_url);
            resetImportSearch(); closeWindow();
            context.trigger?.focus();
        };
    }
    action.className = 'import-search-actions'; action.append(select);
    tr.append(title, action);
    if (!safeCover) artwork.observe(tr, entry);
    if (importCoverObserver) importCoverObserver.observe(tr, {attributes:true, attributeFilter:['data-cover']});
    return tr;
}
async function searchImport(event) {
    event.preventDefault();
    const query = imp('search-input').value.trim(); if (!query) return;
    resetImportSearch();
    const generation = importSearchGeneration;
    const results = document.querySelector('.search-results'); results.replaceChildren();
    imp('match-error').textContent = '';
    try {
        const response = await fetchAPI('/volumes/search', importKey, {query:encodeURIComponent(query),
            provider:imp('import-metadata-provider').value, presentation:'v2', artwork:'true', expand_relations:'false'});
        if (generation !== importSearchGeneration) return;
        const groups = MetadataSearchPresentation.groups(response.result);
        importArtwork = MetadataSearchPresentation.artwork(response.result.artwork_ticket, importKey,
            () => generation === importSearchGeneration && imp('cv-window').hasAttribute('show-window'));
        if (typeof MutationObserver !== 'undefined') {
            importCoverObserver = new MutationObserver(records => {
                for (const record of records) {
                    if (generation !== importSearchGeneration) return;
                    const tr = record.target, source = tr.dataset.cover;
                    tr.querySelector('.import-cover-button').disabled = !/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(source || '') &&
                        !MetadataSearchPresentation.safeLink(source);
                }
            });
        }
        if (groups) for (const group of groups) {
            results.append(MetadataSearchPresentation.heading(group, true));
            for (const entry of group.results) results.append(importResult(entry, importArtwork, generation));
        } else for (const entry of response.result) results.append(importResult(entry, importArtwork, generation));
        updateSelectionSummary();
        document.querySelector('.search-results-container').classList.remove('hidden');
    } catch (_) {
        if (generation === importSearchGeneration)
            imp('match-error').textContent = 'Publication search unavailable. Retry or choose another provider.';
    }
}
async function submitImport(only=null) {
    const selected = (only || [...importRows.values()]).filter(row =>
        row.element.querySelector('.import-file-select').checked &&
        !row.status.startsWith('Imported') && row.status !== 'Already in library');
    if (!only) for (const row of importRows.values())
        if (!row.element.querySelector('.import-file-select').checked && !row.status)
            setImportStatus(row, 'Skipped');
    if (!selected.length) { importError('Select at least one unimported file.'); return; }
    importError(''); imp('import-button').disabled = true;
    const mode = imp('import-mode').value;
    try {
        for (let start = 0; start < selected.length; start += 100) {
            const batch = selected.slice(start, start + 100);
            const ready = batch.filter(row => row.identity && row.issueIds.length);
            for (const row of batch) if (!ready.includes(row)) setImportStatus(row, 'Failed: choose a publication and issue');
            if (!ready.length) continue;
            const response = await sendAPI('POST', '/libraryimport/submit', importKey, {}, {files:ready.map(row => ({
                source:row.data.filepath, provider:row.identity.provider, provider_id:row.identity.id,
                issue_ids:row.issueIds, mode, filename:row.filename || undefined
            }))});
            if (!response.ok) throw new Error('Import request failed');
            (await response.json()).result.results.forEach((result, index) => {
                const row = ready[index];
                setImportStatus(row, result.status === 'imported' ?
                    `Imported${result.warning ? ' · ' + result.warning : ''}` :
                    result.status === 'already_in_library' ? 'Already in library' : `Failed: ${importReason(result.reason)}`);
                if (result.reason?.startsWith('target_exists: ')) {
                    const name = document.createElement('input'); name.type = 'text';
                    name.placeholder = 'Another filename (same extension)';
                    name.setAttribute('aria-label', 'Alternative filename for ' + row.data.file_title);
                    name.onchange = () => { row.filename = name.value.trim(); };
                    row.element.querySelector('.import-status').append(name, document.createTextNode(' Or choose Keep in place and retry.'));
                }
                if (result.status === 'failed') {
                    const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry';
                    retry.onclick = () => submitImport([row]);
                    row.element.querySelector('.import-status').append(retry);
                }
            });
        }
    } catch (error) { importError(error.message); }
    finally { imp('import-button').disabled = false; }
}
usingApiKey().then(async key => {
    importKey = key;
    const viewer = imp('import-cover-lightbox');
    imp('bulk-match-review').addEventListener('click', event => event.stopPropagation());
    viewer.addEventListener('cancel', event => { event.preventDefault(); closeImportCover(); });
    viewer.addEventListener('click', event => {
        event.stopPropagation();
        if (event.target === viewer) closeImportCover();
    });
    imp('import-cover-close').onclick = closeImportCover;
    document.addEventListener('keydown', event => {
        if (imp('bulk-match-review').open && event.key === 'Escape') {
            event.stopImmediatePropagation();
            return;
        }
        if (viewer.open && event.key === 'Escape') {
            event.preventDefault(); event.stopImmediatePropagation(); closeImportCover();
        }
    }, true);
    if (typeof MutationObserver !== 'undefined') new MutationObserver(() => {
        if (!document.querySelector('.window').hasAttribute('show-window')) {
            const trigger = importSearchContext?.trigger;
            resetImportSearch();
            importSearchContext = null;
            if (trigger?.isConnected && !trigger.disabled) trigger.focus();
        }
    }).observe(document.querySelector('.window'), {attributes:true, attributeFilter:['show-window']});
    try { imp('import-mode').value = localStorage.getItem('pullarr.importMode') || 'keep'; } catch (_) { /* private mode */ }
    imp('import-mode').onchange = () => { try { localStorage.setItem('pullarr.importMode', imp('import-mode').value); } catch (_) { /* private mode */ } };
    imp('run-import-button').onclick = () => scanImport();
    imp('open-bulk-match').onclick = () => openPublicationSearch();
    imp('clear-bulk-match').onclick = () => {
        for (const row of importRows.values()) row.element.querySelector('.bulk-match-select').checked = false;
        updateSelectionSummary();
        imp('open-bulk-match').focus();
    };
    imp('auto-match-input').onchange = () => {
        imp('advanced-matching').hidden = !imp('auto-match-input').checked;
        imp('match-folder-limit').disabled = !imp('auto-match-input').checked;
    };
    imp('stop-matching').onclick = () => {
        ++importMatchGeneration;
        for (const row of importRows.values()) if (row.identity && !row.userChosen)
            ++row.choiceVersion;
        imp('matching-status').textContent = 'Automatic matching stopped; local files remain available.';
        imp('stop-matching').classList.add('hidden');
    };
    imp('import-next-page').onclick = () => scanImport(false);
    imp('import-button').onclick = () => submitImport();
    imp('import-filter').oninput = filterImport;
    imp('selectall-input').onchange = event => {
        for (const row of importRows.values()) row.element.querySelector('.import-file-select').checked = event.target.checked;
        updateSelectionSummary();
    };
    document.querySelector('.search-bar').onsubmit = searchImport;
    imp('import-metadata-provider').onchange = () => {
        resetImportSearch();
        document.querySelector('.search-results').replaceChildren();
    };
    document.querySelectorAll('.cancel-button').forEach(button => button.onclick = () => {
        ++importMatchGeneration;
        imp('stop-matching').classList.add('hidden');
        imp('list-window').classList.add('hidden'); imp('no-result-window').classList.add('hidden');
        imp('start-window').classList.remove('hidden');
    });
    try {
        const settings = (await fetchAPI('/settings', key)).result;
        imp('import-metadata-provider').querySelector('option[value="metron"]').disabled = !settings.metron_api_token;
        imp('import-metadata-provider').querySelector('option[value="gcd"]').disabled = !settings.gcd_enabled;
    } catch (_) { /* ComicVine remains available. */ }
});
