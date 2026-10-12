const liEls = {
	preBuild: {
		liResult: document.querySelector('.pre-build-els .li-result'),
		searchResult: document.querySelector('.pre-build-els .search-result')
	},
	views: {
		start: document.getElementById('start-window'),
		noResult: document.getElementById('no-result-window'),
		list: document.getElementById('list-window'),
		loading: document.getElementById('loading-window'),
		noCv: document.getElementById('no-cv-window')
	},
	rateLimitBanner: document.getElementById('rate-limit-banner'),
	proposalList: document.querySelector('.proposal-list'),
	selectAll: document.getElementById('selectall-input'),
	search: {
		provider: document.getElementById('import-metadata-provider'),
		window: document.getElementById('cv-window'),
		input: document.getElementById('search-input'),
		results: document.querySelector('.search-results'),
		container: document.querySelector('.search-results-container'),
		bar: document.querySelector('.search-bar')
	},
	buttons: {
		cancel: document.querySelectorAll('.cancel-button'),
		run: document.getElementById('run-import-button'),
		preview: document.getElementById('preview-import-button'),
		previewRename: document.getElementById('preview-import-rename-button'),
		import: document.getElementById('import-button'),
		importRename: document.getElementById('import-rename-button')
	}
}

const rowidToFilepath = {}
let shiftSelectStart = null
const selectedRows = new Set()
const issueRequests = new Map();
let scanGeneration = 0;
let reviewDraft = null;
let reviewScanScope = null;
let reviewSaveQueue = new Set();
let reviewSaveTimer = null;
let reviewSavePromise = Promise.resolve();
let reviewSaveBusy = false;
const reviewStorageKey = 'pullarr.libraryImportDraft.v3';

function reviewRow(rowid) {
	const entry = rowidToFilepath[rowid];
	if (!entry) return null;
	const row = liEls.proposalList.querySelector(`tr[data-rowid="${rowid}"]`);
	const choice = {
		filepath: entry.filepath, stamp: entry.stamp, selected: !!row?.querySelector('.import-file-select')?.checked,
		issue_ids: entry.issueIds || [], issue_override: !!entry.manualIssues,
		manual_issues: !!entry.manualIssues, overlap_override: !!entry.allowOverlap,
		catalog_revision: entry.catalog?.revision || entry.catalogRevision || null,
		catalog_completeness: entry.catalog?.completeness || entry.catalogSummary?.completeness || null,
		reported_issue_count: entry.catalog?.reported_issue_count ?? entry.catalogSummary?.reported ?? null,
		loaded_issue_count: entry.catalog?.loaded_issue_count ?? entry.catalogSummary?.loaded ?? null,
		selected_issues: (entry.catalog?.issues || entry.selectedIssues || [])
			.filter(issue => entry.issueIds?.includes(issue.id))
			.map(issue => ({id:issue.id,number:issue.number,title:issue.title,date:issue.date})),
		issue_labels: entry.issueLabels || [], issue_reason: entry.issueReason || ''
	};
	if (entry.identity) { choice.provider = entry.identity.provider; choice.provider_id = entry.identity.id; }
	if (entry.display) {
		choice.display = entry.display;
		if (JSON.stringify(choice.display).length > 7000)
			choice.display = {file_title:entry.display.file_title, folder:entry.display.folder,
				size:entry.display.size, extension:entry.display.extension};
	}
	return choice;
}

function markReviewDirty(rowid) {
	if (!rowidToFilepath[rowid]) return;
	reviewSaveQueue.add(String(rowid));
	document.getElementById('import-save-status').textContent = 'Unsaved review changes…';
	if (reviewSaveTimer) clearTimeout(reviewSaveTimer);
	reviewSaveTimer = setTimeout(() => usingApiKey().then(flushReviewSave), 500);
}

async function flushReviewSave(apiKey) {
	if (reviewSaveTimer) { clearTimeout(reviewSaveTimer); reviewSaveTimer = null; }
	const ids = [...reviewSaveQueue].slice(0, 100);
	if (!ids.length) { await reviewSavePromise; return; }
	ids.forEach(id => reviewSaveQueue.delete(id));
	const rows = ids.map(reviewRow).filter(Boolean);
	reviewSavePromise = reviewSavePromise.catch(() => {}).then(async () => {
		if (!rows.length) return;
		reviewSaveBusy = true;
		const path = reviewDraft ? `/libraryimport/drafts/${reviewDraft.id}` : '/libraryimport/drafts';
		const body = reviewDraft ? {rows, revision:reviewDraft.revision} :
			{rows, scope:reviewScanScope || {}};
		const response = await sendAPI(reviewDraft ? 'PATCH' : 'POST', path, apiKey, {}, body);
		if (!response.ok) throw response;
		reviewDraft = (await response.json()).result;
		try { localStorage.setItem(reviewStorageKey, reviewDraft.id); } catch (_) { /* private mode */ }
		document.getElementById('import-save-status').textContent = 'Saved import review';
	}).catch(async error => {
		ids.forEach(id => reviewSaveQueue.add(id));
		const body = await error?.json?.().catch(() => null);
		document.getElementById('import-save-status').textContent =
			`Unsaved: ${body?.result?.code || 'save unavailable'}. Retry before leaving this page.`;
		throw error;
	}).finally(() => { reviewSaveBusy = false; });
	await reviewSavePromise;
	if (reviewSaveQueue.size) await flushReviewSave(apiKey);
}
window.addEventListener('beforeunload', event => {
	if (reviewSaveQueue.size || reviewSaveBusy) {
		event.preventDefault();
		event.returnValue = '';
	}
});

function issueCell(rowid) {
	return liEls.proposalList.querySelector(`tr[data-rowid="${rowid}"] .issue-column`);
}

function issueSummary(rowid) {
	const entry = rowidToFilepath[rowid], cell = issueCell(rowid);
	if (!cell) return;
	cell.replaceChildren();
	const status = document.createElement('span');
	if (!entry.identity) {
		status.textContent = 'Choose publication first'; cell.appendChild(status); return;
	}
	const labels = (entry.catalog?.issues || []).filter(issue => entry.issueIds?.includes(issue.id))
		.map(issue => `${issue.number}${issue.title ? ' · ' + issue.title : ''}`);
	if (!labels.length && entry.issueLabels?.length) labels.push(...entry.issueLabels);
	status.textContent = labels.length ? labels.join(', ') :
		(entry.catalog ? 'Choose issue · needs review' : 'Issue catalogue not loaded');
	if (labels.length && entry.issueConfidence)
		status.textContent += ` · ${entry.issueConfidence}`;
	cell.appendChild(status);
	const button = document.createElement('button');
	button.type = 'button'; button.textContent = entry.catalog ? 'Edit issues' : 'Load issues';
	button.onclick = () => openIssueEditor(rowid);
	cell.appendChild(button);
}

async function loadIssueCatalogue(rowid, apiKey, showEditor=false) {
	const entry = rowidToFilepath[rowid];
	if (!entry?.identity) return;
	const generation = (issueRequests.get(rowid) || 0) + 1;
	issueRequests.set(rowid, generation);
	const identity = {...entry.identity};
	const cell = issueCell(rowid);
	if (cell) cell.textContent = 'Loading provider issue catalogue…';
	try {
		const response = await sendAPI('POST', '/libraryimport/catalog', apiKey, {}, {
			provider: identity.provider, provider_id: identity.id, files: [entry.filepath]
		});
		if (!response.ok) throw response;
		const catalog = (await response.json()).result;
		if (generation !== issueRequests.get(rowid) || rowidToFilepath[rowid] !== entry
			|| entry.identity.provider !== identity.provider || entry.identity.id !== identity.id) return;
		entry.catalog = catalog;
		if (!entry.manualIssues && !entry.issueIds?.length) {
			const proposal = catalog.proposals[entry.filepath];
			entry.issueIds = ['exact', 'strong'].includes(proposal?.confidence) ? proposal.issue_ids : [];
			entry.issueReason = proposal?.reason || '';
			entry.issueConfidence = proposal?.confidence || 'none';
		}
		entry.catalogRevision = catalog.revision;
		entry.issueLabels = catalog.issues.filter(issue => entry.issueIds?.includes(issue.id))
			.map(issue => `${issue.number}${issue.title ? ' · ' + issue.title : ''}`);
		issueSummary(rowid);
		markReviewDirty(rowid);
		if (showEditor) openIssueEditor(rowid);
	} catch (_) {
		if (generation === issueRequests.get(rowid) && rowidToFilepath[rowid] === entry && cell) {
			issueSummary(rowid);
			const notice = document.createElement('p');
			notice.textContent = 'Catalogue unavailable. Retry Load issues; local file remains listed.';
			cell.appendChild(notice);
		}
	}
}

function openIssueEditor(rowid) {
	const entry = rowidToFilepath[rowid];
	if (!entry?.identity) return;
	if (!entry.catalog) {
		usingApiKey().then(key => loadIssueCatalogue(rowid, key, true));
		return;
	}
	issueSummary(rowid);
	const cell = issueCell(rowid), panel = document.createElement('section');
	panel.className = 'import-issue-editor';
	panel.setAttribute('aria-label', 'Choose issues for ' + entry.filepath.split(/[\\/]/).pop());
	const heading = document.createElement('p');
	heading.textContent = `${entry.catalog.provider}:${entry.catalog.provider_id} · ${entry.catalog.reported_issue_count ?? 'unknown'} reported · ${entry.catalog.loaded_issue_count} loaded (${entry.catalog.completeness})`;
	panel.appendChild(heading);
	const reason = document.createElement('p');
	reason.textContent = entry.issueReason || 'Choose exact issues from this publication.';
	panel.appendChild(reason);
	const search = document.createElement('input');
	search.type = 'search'; search.placeholder = 'Search number or title';
	search.setAttribute('aria-label', 'Search provider issues'); panel.appendChild(search);
	const list = document.createElement('div'); list.className = 'import-issue-list'; panel.appendChild(list);
	const chosen = new Set(entry.issueIds || []);
	const render = () => {
		list.replaceChildren();
		const query = search.value.trim().toLowerCase();
		const matches = entry.catalog.issues.filter(issue => !query ||
			`${issue.number} ${issue.title || ''} ${issue.date || ''}`.toLowerCase().includes(query));
		for (const issue of matches.slice(0, 100)) {
			const label = document.createElement('label');
			const input = document.createElement('input'); input.type = 'checkbox';
			input.checked = chosen.has(issue.id);
			input.onchange = () => input.checked ? chosen.add(issue.id) : chosen.delete(issue.id);
			label.appendChild(input);
			const span = document.createElement('span');
			span.textContent = `${issue.number}${issue.title ? ' · ' + issue.title : ''}${issue.date ? ' · ' + issue.date : ''}${issue.owned ? ' · already owned' : ''}`;
			label.appendChild(span); list.appendChild(label);
		}
		if (matches.length > 100) {
			const more = document.createElement('p'); more.textContent = `${matches.length} results; narrow the search.`; list.appendChild(more);
		}
	};
	search.oninput = render; render();
	const overlapLabel = document.createElement('label');
	const overlap = document.createElement('input'); overlap.type = 'checkbox';
	overlap.checked = !!entry.allowOverlap; overlapLabel.appendChild(overlap);
	const overlapText = document.createElement('span');
	overlapText.textContent = 'I reviewed overlap with another file or an already owned issue';
	overlapLabel.appendChild(overlapText); panel.appendChild(overlapLabel);
	const save = document.createElement('button'); save.type='button'; save.textContent='Use selected issues';
	save.onclick = () => {
		entry.issueIds = [...chosen]; entry.manualIssues = true;
		entry.allowOverlap = overlap.checked;
		entry.issueReason = 'Reviewed issue selection; validated against publication identity during preview.';
		entry.issueConfidence = 'reviewed';
		entry.issueLabels = entry.catalog.issues.filter(issue => entry.issueIds.includes(issue.id))
			.map(issue => `${issue.number}${issue.title ? ' · ' + issue.title : ''}`);
		issueSummary(rowid);
		markReviewDirty(rowid);
	};
	panel.appendChild(save); cell.appendChild(panel); search.focus();
}

function buildMatchTitle(title, year, issueCount) {
	let result = ''
	if (title)
		result += title

	if (year !== null)
		result += ` (${year})`

	if (issueCount !== null) {
		const plural = issueCount !== 1 ? 's' : ''
		result += ` [${issueCount} issue${plural}]`
	}

	return result
}

function updateSelection() {
	liEls.proposalList.querySelectorAll(".li-result").forEach((entry, rowid) => {
		if (selectedRows.has(rowid))
			entry.classList.add("selected")
		else
			entry.classList.remove("selected")
	})
}

function loadPublicationProposals(apiKey, scanParams, generation) {
	const params = {auto_match: true, limit: Math.min(scanParams.folder_limit, 20),
		limit_parent_folder: document.querySelector('#folder-input').value,
		only_english: document.querySelector('#lang-input').value};
	if (scanParams.folder_filter) params.folder_filter = scanParams.folder_filter;
	fetchAPI('/libraryimport', apiKey, params).then(json => {
		if (generation !== scanGeneration) return;
		const byPath = new Map(Object.entries(rowidToFilepath).map(([id, value]) => [value.filepath, id]));
		let matched = 0;
		for (const result of json.result) {
			const rowid = byPath.get(result.filepath);
			if (rowid === undefined || result.cv?.id == null || rowidToFilepath[rowid].identity) continue;
			rowidToFilepath[rowid].identity = {provider:'comicvine', id:String(result.cv.id)};
			matched++;
			const link = liEls.proposalList.querySelector(`tr[data-rowid="${rowid}"] a`);
			link.href = result.cv.link || '';
			link.textContent = buildMatchTitle(result.cv.title, null, result.cv.issue_count) + ' · ComicVine proposal';
			issueSummary(rowid);
			markReviewDirty(rowid);
		}
		if (matched) document.getElementById('scan-summary').textContent += ` · ${matched} ComicVine publication suggestions loaded`;
	}).catch(() => {
		liEls.rateLimitBanner.textContent = 'Automatic publication suggestions are unavailable. All discovered files remain listed; choose a publication manually.';
		hide([], [liEls.rateLimitBanner]);
	});
}

function loadProposal(apiKey, offset=0) {
	const params = {
		folder_limit: parseInt(document.querySelector('#limit-input').value),
		page_size: 100, offset
	};
	const ffi = document.querySelector('#folder-filter-input');
	if (ffi.offsetParent !== null && (ffi.value || null) !== null)
		params.folder_filter = encodeURIComponent(ffi.value);

	if (!offset) hide(
		[
			liEls.views.start,
			document.querySelector('#folder-filter-error'),
			liEls.rateLimitBanner
		],
		[liEls.views.loading]
	);

	if (!offset) {
		scanGeneration++;
		reviewDraft = null;
		reviewScanScope = {folder_limit:params.folder_limit,
			page_size:params.page_size, folder_filter:params.folder_filter || ''};
		liEls.proposalList.innerHTML = '';
		lastRenderedFolder = null; stagedImport = null;
		selectedRows.clear(); issueRequests.clear();
		Object.keys(rowidToFilepath).forEach(key => delete rowidToFilepath[key]);
		liEls.selectAll.checked = true;
	}
	const generation = scanGeneration;

	fetchAPI('/libraryimport/files', apiKey, params)
	.then(json => {
		if (generation !== scanGeneration) return;
		const page = json.result;
		const summary = page.counts;
		document.getElementById('scan-summary').textContent =
			`${summary.scanned_folders}/${summary.eligible_folders} folders scanned · ${summary.discovered} unmanaged archives · ${summary.already_managed} already managed · ${summary.invalid} invalid · ${summary.skipped} skipped${page.enumeration_complete ? '' : ' · partial scan; increase folder limit or narrow the filter'}`;
		const next = document.getElementById('import-next-page');
		next.classList.toggle('hidden', page.next_offset === null);
		next.onclick = () => loadProposal(apiKey, page.next_offset);
		page.items.forEach((result, index) => {
			const rowid = offset + index;
			if (result.folder !== lastRenderedFolder) {
				const group = document.createElement('tr');
				group.className = 'import-source-group';
				const label = document.createElement('th');
				label.colSpan = 5; label.scope = 'rowgroup';
				label.textContent = result.folder;
				group.appendChild(label); liEls.proposalList.appendChild(group);
				lastRenderedFolder = result.folder;
			}
			const entry = liEls.preBuild.liResult.cloneNode(true);
			entry.dataset.rowid = rowid;
			rowidToFilepath[rowid] = {
				identity: result.metadata_source || (result.cv.id === null ? null : {
					provider: 'comicvine', id: String(result.cv.id)
				}),
				filepath: result.filepath, stamp: result.stamp,
				issueIds: [], manualIssues: false, allowOverlap: false,
				display: {file_title:result.file_title, size:result.size,
					extension:result.extension, folder:result.folder,
					cv:result.cv, metadata_source:result.metadata_source,
					parsed:result.parsed, embedded:result.embedded,
					warnings:result.warnings}
			};
			entry.addEventListener("click", e => e.stopPropagation())

			const toggle = entry.querySelector('.import-file-select')
			toggle.onchange = () => toggleSelected(rowid)

			const title = entry.querySelector('.file-column');
			title.innerText = result.file_title;
			title.title = result.filepath;
			const evidence = document.createElement('details');
			const summary = document.createElement('summary'); summary.textContent = 'File details';
			evidence.appendChild(summary);
			const facts = document.createElement('p');
			facts.textContent = `${result.filepath} · ${result.size} bytes · ${result.extension}` +
				(result.parsed ? ` · parsed year ${result.parsed.year ?? 'unknown'}, number ${result.parsed.issue_number ?? 'unknown'}, volume ${result.parsed.volume_number ?? 'unknown'}` : '');
			evidence.appendChild(facts); title.appendChild(evidence);
			if (result.embedded) {
				const embedded = document.createElement('p');
				embedded.textContent = `ComicInfo: ${result.embedded.series || 'unknown series'} · Number ${result.embedded.number || 'unknown'} · ${result.embedded.title || 'untitled'}` +
					(result.embedded.references?.length ? ` · exact IDs ${result.embedded.references.map(ref => `${ref.provider}:${ref.kind}:${ref.id}`).join(', ')}` : '');
				evidence.appendChild(embedded);
			}
			if (result.warnings?.length) {
				const warning = document.createElement('p');
				warning.textContent = `Archive evidence: ${result.warnings.join(', ')}`;
				evidence.appendChild(warning);
			}
			evidence.onclick = event => event.stopPropagation();
			title.onclick = (e) => {
				e.stopPropagation()

				if (
					e.ctrlKey
					|| e.shiftKey && shiftSelectStart === null
				) {
					if (selectedRows.has(rowid))
						selectedRows.delete(rowid)
					else
						selectedRows.add(rowid)

					shiftSelectStart = rowid
				}

				else if (e.shiftKey) {
					let start = shiftSelectStart,
						end = rowid
					if (start > end) {
						start = rowid
						end = shiftSelectStart
					}

					const addSelection = selectedRows.has(shiftSelectStart)
					for (let i = start; i <= end; i++) {
						if (addSelection)
							selectedRows.add(i)
						else
							selectedRows.delete(i)
					}
				}

				else {
					selectedRows.clear()
					selectedRows.add(rowid)
					shiftSelectStart = rowid
				}

				updateSelection()
			}

			const CV_link = entry.querySelector('a');
			CV_link.href = result.cv.link || '';
			CV_link.innerText = result.metadata_source ?
				`Embedded ${result.metadata_source.provider}:${result.metadata_source.id} · verify publication` :
				buildMatchTitle(result.cv.title, null, result.cv.issue_count) +
				(result.cv.id === null ? '' : ' · ComicVine')

			entry.querySelector('button').onclick = e => openEditCVMatch(rowid);

			liEls.proposalList.appendChild(entry);
			issueSummary(rowid);
			markReviewDirty(rowid);
		});

		if (page.items.length > 0 || offset > 0) {
			hide([liEls.views.loading], [liEls.views.list]);
			if (!offset && document.getElementById('auto-match-input').checked)
				loadPublicationProposals(apiKey, params, generation);
		} else
			hide([liEls.views.loading], [liEls.views.noResult]);
	})
	.catch(async error => {
		if (generation !== scanGeneration) return;
		const body = await error?.json?.().catch(() => null);
		const code = body?.result?.code;
		if (code === 'unsafe_folder_filter' || code === 'invalid_scan_parameters')
			hide([liEls.views.loading], [liEls.views.start, document.querySelector('#folder-filter-error')]);
		else {
			hide([liEls.views.loading], [liEls.views.start]);
			document.getElementById('scan-summary').textContent =
				`Local scan failed: ${code || 'filesystem unavailable'}. Retry after checking the selected root.`;
		}
	});
};

function toggleSelectAll() {
	const checked = liEls.selectAll.checked;
	liEls.proposalList.querySelectorAll('.import-file-select').forEach(
		e => { e.checked = checked; markReviewDirty(e.closest('tr').dataset.rowid); }
	);
};

function toggleSelected(rowid) {
	if (!selectedRows.has(rowid))
		return

	const checked = liEls.proposalList.querySelector(
		`tr[data-rowid="${rowid}"] .import-file-select`
	).checked

	selectedRows.forEach(selected => {
		liEls.proposalList.querySelector(
			`tr[data-rowid="${selected}"] .import-file-select`
		).checked = checked;
		markReviewDirty(selected);
	});
}

let editMatchId = null

function openEditCVMatch(rowid) {
	editMatchId = rowid
	liEls.search.results.innerHTML = '';
	hide([liEls.search.container]);
	liEls.search.input.value = '';
	showWindow('cv-window');
	liEls.search.input.focus();
};

function editCVMatch(
	comicvine_id,
	site_url,
	title,
	year,
	issue_count
) {
	// Compatibility entry point: a bare ID here always means ComicVine.
	editMetadataMatch({provider: 'comicvine', id: String(parseInt(comicvine_id))},
		site_url, title, year, issue_count);
};

function editMetadataMatch(identity, site_url, title, year, issue_count, bulk=false) {
	// Editing a row never silently overwrites other selected rows' authority.
	const target_td = bulk ? [...liEls.proposalList.querySelectorAll('tr[data-rowid]')]
		.filter(row => row.querySelector('.import-file-select')?.checked)
		.map(row => row.dataset.rowid) : [editMatchId];
	if (!target_td.length) {
		document.getElementById('match-error').textContent = 'Check at least one file before bulk assignment.';
		return false;
	}

	target_td.forEach(rowid => {
		const tr = liEls.proposalList.querySelector(`tr[data-rowid="${rowid}"]`)
		rowidToFilepath[rowid].identity = {provider: identity.provider, id: identity.id};
		rowidToFilepath[rowid].catalog = null;
		rowidToFilepath[rowid].catalogRevision = null;
		rowidToFilepath[rowid].selectedIssues = [];
		rowidToFilepath[rowid].issueIds = [];
		rowidToFilepath[rowid].manualIssues = false;
		rowidToFilepath[rowid].issueReason = '';
		rowidToFilepath[rowid].issueConfidence = 'none';
		rowidToFilepath[rowid].allowOverlap = false;
		const link = tr.querySelector('a');
		link.href = site_url;
		link.innerText = buildMatchTitle(title, year, issue_count) + ` · ${identity.provider}`
		issueSummary(rowid);
		markReviewDirty(rowid);
		usingApiKey().then(key => loadIssueCatalogue(rowid, key, true));
	});
	return true;
};

let metadataSearchGeneration = 0;
let importSearchArtwork = null;
function searchMetadata() {
	const generation = ++metadataSearchGeneration;
	if (importSearchArtwork) importSearchArtwork.stop();
	const input = liEls.search.input;
	const provider = liEls.search.provider.value;
	const query = input.value;
	document.getElementById('match-error').innerText = '';
	input.blur();
	usingApiKey()
	.then(api_key => {
		liEls.search.results.innerHTML = '';
		const params = {query: encodeURIComponent(query), provider,
			presentation: 'v2', artwork: 'true', expand_relations: 'false'};
		fetchAPI('/volumes/search', api_key, params)
		.then(json => {
			if (generation !== metadataSearchGeneration || provider !== liEls.search.provider.value || query !== input.value) return;
			const groups = MetadataSearchPresentation.groups(json.result);
			importSearchArtwork = MetadataSearchPresentation.artwork(
				json.result.artwork_ticket, api_key,
				() => generation === metadataSearchGeneration && provider === liEls.search.provider.value);
			const entries = groups ? groups.flatMap(group => [{search_group: group}, ...group.results]) : json.result;
			entries.forEach(result => {
				if (result.search_group) {
					liEls.search.results.appendChild(MetadataSearchPresentation.heading(result.search_group, true));
					return;
				}
				const entry = liEls.preBuild.searchResult.cloneNode(true);

				const title = entry.querySelector('td:nth-child(1) a');
				title.href = groups ? MetadataSearchPresentation.safeLink(result.site_url) : result.site_url;
				const cover = document.createElement('img');
				cover.className = 'import-publication-cover'; cover.alt = '';
				cover.src = MetadataSearchPresentation.safeLink(result.cover_link) ||
					`${typeof url_base === 'string' ? url_base : ''}/static/img/favicon.svg`;
				title.innerText = buildMatchTitle(
					result.title, result.year, result.issue_count
				) + ` · ${result.metadata_source ? result.metadata_source.provider + ':' + result.metadata_source.id : provider}`;
				title.prepend(cover);
				if (groups) {
					const note = document.createElement('p');
					note.textContent = (result.already_added == null ? '' : 'Already in Library · ') +
						MetadataSearchPresentation.annotations(result);
					entry.querySelector('td:nth-child(1)').appendChild(note);
				}

				const select_button = entry.querySelector('td:nth-child(2) button');
				select_button.onclick = e => {
					editMetadataMatch(
						result.metadata_source || {provider: 'comicvine', id: String(result.comicvine_id)},
						groups ? MetadataSearchPresentation.safeLink(result.site_url) : result.site_url,
						result.title,
						result.year,
						result.issue_count
					);
					closeWindow();
				};
				const bulkButton = entry.querySelector('.bulk-match');
				bulkButton.onclick = e => {
					const assigned = editMetadataMatch(result.metadata_source ||
						{provider: 'comicvine', id: String(result.comicvine_id)},
						groups ? MetadataSearchPresentation.safeLink(result.site_url) : result.site_url,
						result.title, result.year, result.issue_count, true);
					if (assigned) closeWindow();
				};

				liEls.search.results.appendChild(entry);
				importSearchArtwork.observe(entry, result);
			});
			hide([], [liEls.search.container]);
		}).catch(() => {
			if (generation === metadataSearchGeneration && provider === liEls.search.provider.value)
				document.getElementById('match-error').innerText = provider === 'all'
					? 'Combined search could not complete. Check the query and server diagnostics.'
					: `${provider} search failed. Check credentials or rate limits; no other provider was searched.`;
		});
	});
};

let importPending = false;
let stagedImport = null;
let lastRenderedFolder = null;
function selectedImportData() {
	return [...liEls.proposalList.querySelectorAll('tr[data-rowid]')]
		.filter(row => row.querySelector('.import-file-select')?.checked)
		.filter(i => rowidToFilepath[i.dataset.rowid].identity !== null)
		.map(e => {
			const rowid = e.dataset.rowid;
			return {
				'filepath': rowidToFilepath[rowid].filepath,
				'stamp': rowidToFilepath[rowid].stamp || null,
				'provider': rowidToFilepath[rowid].identity.provider,
				'provider_id': rowidToFilepath[rowid].identity.id,
				'issue_ids': rowidToFilepath[rowid].issueIds || [],
				'issue_override': !!rowidToFilepath[rowid].manualIssues,
				'catalog_revision': rowidToFilepath[rowid].catalog?.revision ||
					rowidToFilepath[rowid].catalogRevision || null,
				'overlap_override': !!rowidToFilepath[rowid].allowOverlap
			};
		});
}

async function restoreReviewDraft(apiKey, identifier) {
	if (!/^[0-9a-f]{32}$/.test(identifier || '')) return;
	const pages = [];
	let offset = 0;
	do {
		const response = await fetchAPI(`/libraryimport/drafts/${identifier}`, apiKey,
			{offset, limit:100});
		const page = response.result;
		pages.push(page);
		offset = page.next_offset;
	} while (offset !== null && pages.length < 100);
	if (!pages.length) return;
	scanGeneration++;
	liEls.proposalList.replaceChildren();
	Object.keys(rowidToFilepath).forEach(key => delete rowidToFilepath[key]);
	const first = pages[0];
	reviewDraft = {id:identifier, revision:first.revision, state:first.state};
	reviewScanScope = first.scope;
	stagedImport = null;
	let count = 0, folder = null;
	for (const page of pages) for (const saved of page.rows) {
		const choice = saved.selection, display = choice.display || {};
		const sourceFolder = display.folder || choice.filepath.replace(/[\\/][^\\/]+$/, '');
		if (sourceFolder !== folder) {
			const group = document.createElement('tr'); group.className = 'import-source-group';
			const title = document.createElement('th'); title.colSpan = 5; title.scope = 'rowgroup';
			title.textContent = sourceFolder; group.appendChild(title); liEls.proposalList.appendChild(group);
			folder = sourceFolder;
		}
		const rowid = String(count++), row = liEls.preBuild.liResult.cloneNode(true);
		row.dataset.rowid = rowid;
		rowidToFilepath[rowid] = {
			filepath:choice.filepath, stamp:choice.stamp, display,
			identity:choice.provider ? {provider:choice.provider,id:choice.provider_id} : null,
			issueIds:choice.issue_ids || [], issueLabels:choice.issue_labels || [],
			issueReason:choice.issue_reason || '', manualIssues:!!choice.manual_issues,
			allowOverlap:!!choice.overlap_override, catalogRevision:choice.catalog_revision,
			catalogSummary:{completeness:choice.catalog_completeness,
				reported:choice.reported_issue_count,loaded:choice.loaded_issue_count},
			selectedIssues:choice.selected_issues || []
		};
		row.querySelector('.file-column').textContent = display.file_title || choice.filepath.split(/[\\/]/).pop();
		row.querySelector('.file-column').title = choice.filepath;
		const link = row.querySelector('a'); link.textContent = choice.provider ?
			`${choice.provider}:${choice.provider_id} · saved publication` : 'Choose publication';
		link.removeAttribute('href');
		row.querySelector('.import-file-select').checked = choice.selected !== false;
		row.querySelector('.import-file-select').onchange = () => toggleSelected(rowid);
		row.querySelector('button').onclick = () => openEditCVMatch(rowid);
		row.addEventListener('click', event => event.stopPropagation());
		liEls.proposalList.appendChild(row);
		issueSummary(rowid);
		if (saved.stale_reason) {
			const stale = document.createElement('p');
			stale.textContent = `Revalidation needed: ${saved.stale_reason.replaceAll('_', ' ')}.`;
			row.querySelector('.issue-column').appendChild(stale);
		}
		if (saved.receipt) {
			const receipt = document.createElement('p');
			receipt.textContent = `${saved.receipt.state}: ${saved.receipt.target || choice.filepath}` +
				(saved.receipt.error ? ` · ${saved.receipt.error}` : '');
			row.querySelector('.issue-column').appendChild(receipt);
			if (saved.receipt.state === 'completed' && /\.cbr$/i.test(saved.receipt.target || '')) {
				const maintenance = document.createElement('button');
				maintenance.type = 'button';
				maintenance.textContent = 'Review archive maintenance';
				maintenance.onclick = () => queueImportMaintenance(apiKey, identifier,
					saved.row_id, maintenance);
				row.querySelector('.issue-column').appendChild(maintenance);
			}
		}
	}
	lastRenderedFolder = folder;
	document.getElementById('scan-summary').textContent =
		`Recovered ${count} saved local rows · ${first.state} · revision ${first.revision}`;
	document.getElementById('import-save-status').textContent =
		pages.some(page => page.rows.some(row => row.stale_reason)) ?
		'Recovered review · revalidation needed for affected rows' :
		'Recovered saved import review';
	if (['applying','recovery_required','partially_completed'].includes(first.state)) {
		const resume = document.createElement('button'); resume.type = 'button';
		resume.textContent = 'Inspect or resume journalled import';
		resume.onclick = async () => {
			resume.disabled = true;
			try {
				const response = await sendAPI('POST',
					`/libraryimport/drafts/${encodeURIComponent(identifier)}/resume`, apiKey,
					{}, {revision:reviewDraft.revision});
				if (!response.ok) throw response;
				const result = (await response.json()).result;
				LocalOrganizationUI.result(LocalOrganizationUI.panel(
					document.getElementById('import-error'), 'library-import-results'), result);
				await restoreReviewDraft(apiKey, identifier);
			} catch (_) {
				document.getElementById('import-save-status').textContent =
					'Journal recovery needs review in Maintenance history.';
				resume.disabled = false;
			}
		};
		document.getElementById('import-save-status').appendChild(resume);
	}
	hide([liEls.views.start, liEls.views.loading], [liEls.views.list]);
}

async function queueImportMaintenance(apiKey, draftId, rowId, button) {
	button.disabled = true;
	try {
		const response = await sendAPI('POST',
			`/libraryimport/drafts/${encodeURIComponent(draftId)}/maintenance-review`,
			apiKey, {}, {row_id:rowId});
		if (!response.ok) throw response;
		const task = (await response.json()).result;
		button.textContent = `Maintenance review ${task.id} queued; open Maintenance to inspect and confirm.`;
	} catch (_) {
		button.textContent = 'Maintenance review unavailable; retry in Maintenance.';
		button.disabled = false;
	}
}

function showImportPreflight(panel, stage, apiKey, rename, unmatched=0) {
	panel.replaceChildren();
	const heading = document.createElement('h3'); heading.textContent = 'Import preflight'; panel.appendChild(heading);
	const counts = document.createElement('p');
	counts.textContent = `${stage.counts.ready} ready · ${stage.counts.review_required} need review · ${stage.counts.blocked} blocked · ${stage.counts.already_imported || 0} already imported`;
	panel.appendChild(counts);
	if (unmatched) {
		const skipped = document.createElement('p');
		skipped.textContent = `${unmatched} checked local files have no selected publication and are excluded from this import.`;
		panel.appendChild(skipped);
	}
	if (stage.plans.some(plan => (plan.identification_reasons || []).some(reason => reason.startsWith('destination_')))
		&& stage.worker?.platform !== 'windows') {
		const worker = document.createElement('p');
		worker.textContent = `Pullarr worker UID ${stage.worker.uid}, GID ${stage.worker.gid}, groups ${(stage.worker.groups || []).join(', ')}, umask ${stage.worker.umask || 'unknown'}. SMB access still requires a share-client test.`;
		panel.appendChild(worker);
	}
	for (const plan of stage.plans) {
		const row = document.createElement('div'); row.className = 'import-plan-row';
		const label = document.createElement('p');
		label.textContent = `${plan.source} → ${plan.target || 'No safe destination'} · ${plan.status}`;
		row.appendChild(label);
		const detail = document.createElement('p');
		detail.textContent = plan.status === 'ready' ?
			`${plan.authority.provider}:${plan.authority.id} · issues ${(plan.issue_labels || []).join(', ')}${plan.issue_owned?.some(Boolean) ? ' · already owned' : ''}${plan.warnings?.length ? ' · overlap reviewed' : ''} · ${plan.operation} · ${plan.registration} publication` :
			(plan.identification_reasons || []).join(', ');
		row.appendChild(detail); panel.appendChild(row);
		if (plan.repair_available) {
			const repair = document.createElement('button'); repair.type = 'button';
			repair.textContent = 'Repair this empty canonical folder';
			repair.onclick = async () => {
				repair.disabled = true;
				try {
					const response = await sendAPI('POST', '/libraryimport/repair-folder', apiKey, {},
						{volume_id:plan.volume_id, folder:plan.target, source:plan.source});
					if (!response.ok) throw response;
					await prepareImport(apiKey, rename);
				} catch (error) {
					const body = await error?.json?.().catch(() => null);
					detail.textContent = `Repair failed: ${body?.result?.code || 'check worker identity and ACL permissions'}.`;
					repair.disabled = false;
				}
			}; row.appendChild(repair);
		}
	}
}

async function prepareImport(api_key, rename=false) {
	if (importPending) return;
	try { await flushReviewSave(api_key); }
	catch (_) { document.getElementById('import-error').textContent = 'Save review changes before previewing.'; return; }
	const data = selectedImportData();
    const unmatched = [...liEls.proposalList.querySelectorAll('tr[data-rowid]')]
        .filter(row => row.querySelector('.import-file-select')?.checked &&
            !rowidToFilepath[row.dataset.rowid]?.identity).length;
    const status = document.getElementById('import-error');
    if (!data.length) { status.textContent = 'Choose a publication for at least one checked file.'; return; }
    importPending = true;
    status.textContent = 'Checking files, issue identities and destinations…';
    const panel = LocalOrganizationUI.panel(status, 'library-import-results');
    try {
        const params = {rename_files: rename};
		if (reviewDraft) { params.draft_id = reviewDraft.id; params.revision = reviewDraft.revision; }
		const response = await sendAPI('POST', '/libraryimport/stage', api_key, params, data);
        if (!response.ok) throw response;
        const stage = (await response.json()).result;
        reviewDraft = {id:stage.id, revision:stage.revision, state:stage.state};
		try { localStorage.setItem(reviewStorageKey, stage.id); } catch (_) { /* private mode */ }
		stagedImport = {id:stage.id, revision:stage.revision, rename,
			selection:JSON.stringify(data)};
        showImportPreflight(panel, stage, api_key, rename, unmatched);
        status.textContent = stage.counts.ready ?
            `Review the paths and issues below, then press ${rename ? 'Import and Rename' : 'Import'} to confirm ready rows.` :
            'No ready file. Correct issue selections or destination conflicts and preview again.';
    } catch (error) {
        stagedImport = null;
        const detail = await error?.json?.().catch(() => null);
        status.textContent = `Preflight failed: ${detail?.result?.code || 'check provider, file and folder access, then retry'}.`;
    } finally { importPending = false; }
}

async function importLibrary(api_key, rename=false) {
	if (importPending) return;
	const data = selectedImportData();
    if (!stagedImport || stagedImport.rename !== rename || stagedImport.selection !== JSON.stringify(data)) {
        await prepareImport(api_key, rename);
        return;
    }

    importPending = true;
    liEls.buttons.import.disabled = liEls.buttons.importRename.disabled = true;
    const status = document.getElementById('import-error');
    status.innerText = 'Importing selected files…';
    const panel = LocalOrganizationUI.panel(status, 'library-import-results');
    try {
        const applied = await sendAPI('POST', `/local-organization/${encodeURIComponent(stagedImport.id)}/apply`,
			api_key, {}, {revision:stagedImport.revision});
        if (!applied.ok) throw applied;
        const result = (await applied.json()).result;
		const appliedDraftId = stagedImport.id;
        LocalOrganizationUI.result(panel, result);
		for (const job of result.jobs || []) if (job.state === 'completed' &&
				/\.cbr$/i.test(job.target || '') && job.row_id) {
			const review = document.createElement('button'); review.type = 'button';
			review.textContent = `Review archive maintenance: ${job.target.split(/[\\/]/).pop()}`;
			review.onclick = () => queueImportMaintenance(api_key, appliedDraftId,
				job.row_id, review);
			panel.appendChild(review);
		}
        status.innerText = result.jobs?.length ? 'Import attempt finished. Check each file result below.' :
            'No file imported. Review the row reasons below.';
        stagedImport = null;
    } catch (error) {
        const detail = await error?.json?.().catch(() => null);
        status.innerText = `Import could not be confirmed: ${detail?.result?.code || 'check the provider and Maintenance history'}.`;
    } finally {
        importPending = false;
        liEls.buttons.import.disabled = liEls.buttons.importRename.disabled = false;
    }
};

// code run on load

usingApiKey()
.then(api_key => {
	const draftList = document.getElementById('saved-import-drafts');
	const openDraft = document.getElementById('open-import-draft');
	openDraft.onclick = () => {
		if (draftList.value) restoreReviewDraft(api_key, draftList.value).catch(() => {
			document.getElementById('import-save-status').textContent =
				'Saved review could not be opened; check the selected draft and database.';
		});
	};
	fetchAPI('/libraryimport/drafts', api_key, {limit:50}).then(response => {
		draftList.replaceChildren();
		for (const item of response.result.items || []) {
			const option = document.createElement('option'); option.value = item.id;
			option.textContent = `${item.updated_at} · ${item.rows} files · ${item.state} · ${item.id.slice(0, 8)}`;
			draftList.appendChild(option);
		}
		openDraft.disabled = !draftList.options.length;
	}).catch(() => {
		openDraft.disabled = true;
		document.getElementById('import-save-status').textContent =
			'Saved review list unavailable; retry after checking the database.';
	});
	liEls.buttons.run.onclick = e => loadProposal(api_key);
	liEls.buttons.preview.onclick = e => prepareImport(api_key, false);
	liEls.buttons.previewRename.onclick = e => prepareImport(api_key, true);
	liEls.buttons.import.onclick = e => importLibrary(api_key, false);
	liEls.buttons.importRename.onclick = e => importLibrary(api_key, true);
	const resume = document.getElementById('resume-import-draft');
	let savedId = null;
	try { savedId = localStorage.getItem(reviewStorageKey); } catch (_) { /* private mode */ }
	if (savedId) {
		resume.classList.remove('hidden');
		resume.onclick = () => restoreReviewDraft(api_key, savedId).catch(() => {
			document.getElementById('import-save-status').textContent =
				'Saved review could not be loaded. Scan again or check the database.';
		});
		resume.click();
	}
	fetchAPI('/settings', api_key).then(json => {
		const option = liEls.search.provider.querySelector('option[value="metron"]');
		option.disabled = !json.result.metron_api_token;
		if (!option.disabled) option.innerText = 'Metron';
		liEls.search.provider.querySelectorAll('[data-enabled-setting]').forEach(item => {
			item.disabled = !json.result[item.dataset.enabledSetting];
		});
	});
});

liEls.search.provider.onchange = () => {
	metadataSearchGeneration++;
	if (importSearchArtwork) importSearchArtwork.stop();
	liEls.search.results.innerHTML = '';
	document.getElementById('match-error').innerText = '';
	hide([liEls.search.container]);
};

liEls.search.bar.action = 'javascript:searchMetadata();';
liEls.selectAll.onchange = e => toggleSelectAll();
liEls.buttons.cancel.forEach(b =>
	b.onclick = e => hide(
		[liEls.views.list, liEls.views.noResult, liEls.views.noCv],
		[liEls.views.start]
	)
);
document.addEventListener("click", () => {
	if (selectedRows.size) {
		selectedRows.clear()
		updateSelection()
		shiftSelectStart = null
	}
})
document.addEventListener("keydown", (e) => {
	if (e.key === "Escape") {
		selectedRows.clear()
		updateSelection()
		shiftSelectStart = null
	}
})
