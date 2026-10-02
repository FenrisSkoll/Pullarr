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
		import: document.getElementById('import-button'),
		importRename: document.getElementById('import-rename-button')
	}
}

const rowidToFilepath = {}
let shiftSelectStart = null
const selectedRows = new Set()

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

function loadProposal(apiKey) {
	const params = {
		auto_match: document.getElementById('auto-match-input').checked,
		limit: parseInt(document.querySelector('#limit-input').value),
		limit_parent_folder: document.querySelector('#folder-input').value,
		only_english: document.querySelector('#lang-input').value
	};
	const ffi = document.querySelector('#folder-filter-input');
	if (ffi.offsetParent !== null && (ffi.value || null) !== null)
		params.folder_filter = encodeURIComponent(ffi.value);

	hide(
		[
			liEls.views.start,
			document.querySelector('#folder-filter-error'),
			liEls.rateLimitBanner
		],
		[liEls.views.loading]
	);

	liEls.proposalList.innerHTML = '';
	selectedRows.clear();
	Object.keys(rowidToFilepath).forEach(key => delete rowidToFilepath[key]);
	liEls.selectAll.checked = true;

	fetchAPI('/libraryimport', apiKey, params)
	.then(json => {
		json.result.forEach((result, rowid) => {
			const entry = liEls.preBuild.liResult.cloneNode(true);
			entry.dataset.rowid = rowid;
			rowidToFilepath[rowid] = {
				identity: result.metadata_source || (result.cv.id === null ? null : {
					provider: 'comicvine', id: String(result.cv.id)
				}),
				filepath: result.filepath
			};
			entry.addEventListener("click", e => e.stopPropagation())

			const toggle = entry.querySelector("input[type='checkbox']")
			toggle.onchange = () => toggleSelected(rowid)

			const title = entry.querySelector('.file-column');
			title.innerText = result.file_title;
			title.title = result.filepath;
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
			CV_link.innerText = buildMatchTitle(
				result.cv.title, null, result.cv.issue_count
			) + (result.cv.id === null ? '' : ' · ComicVine')

			entry.querySelector('button').onclick = e => openEditCVMatch(rowid);

			liEls.proposalList.appendChild(entry);
		});

		if (json.result.length > 0) {
			hide([liEls.views.loading], [liEls.views.list]);

			const has_empty_matches = json.result.some(
				r => r.cv.id === null
			);
			if (has_empty_matches) {
				fetchAPI('/system/status', apiKey)
				.then(checks => {
					const search_limited = checks.result.some(
						st => st.type === 'cv_rate_limit'
							&& st.display_subtypes.includes('search_volumes')
					);
					if (search_limited)
						hide([], [liEls.rateLimitBanner]);
				});
			};
		} else
			hide([liEls.views.loading], [liEls.views.noResult]);
	})
	.catch(e => {
		e.json().then(j => {
			if (
				j.error === "InvalidKeyValue"
				&& j.result.key === "comicvine_api_key"
			)
				hide([liEls.views.loading], [liEls.views.noCv]);

			else if (
				j.error === "InvalidKeyValue"
				&& j.result.key === "folder_filter"
			)
				hide(
					[liEls.views.loading],
					[liEls.views.start, document.querySelector('#folder-filter-error')]
				);

			else
				console.log(j);
		});
	});
};

function toggleSelectAll() {
	const checked = liEls.selectAll.checked;
	liEls.proposalList.querySelectorAll('input[type="checkbox"]').forEach(
		e => e.checked = checked
	);
};

function toggleSelected(rowid) {
	if (!selectedRows.has(rowid))
		return

	const checked = liEls.proposalList.querySelector(
		`tr[data-rowid="${rowid}"] input[type="checkbox"]`
	).checked

	selectedRows.forEach(rowid =>
		liEls.proposalList.querySelector(
			`tr[data-rowid="${rowid}"] input[type="checkbox"]`
		).checked = checked
	)
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

function editMetadataMatch(identity, site_url, title, year, issue_count) {
	let target_td;
	if (selectedRows.has(editMatchId))
		target_td = selectedRows
	else
		target_td = [editMatchId]

	target_td.forEach(rowid => {
		const tr = liEls.proposalList.querySelector(`tr[data-rowid="${rowid}"]`)
		rowidToFilepath[rowid].identity = {provider: identity.provider, id: identity.id};
		const link = tr.querySelector('a');
		link.href = site_url;
		link.innerText = buildMatchTitle(title, year, issue_count) + ` · ${identity.provider}`
	});
};

let metadataSearchGeneration = 0;
function searchMetadata() {
	const generation = ++metadataSearchGeneration;
	const input = liEls.search.input;
	const provider = liEls.search.provider.value;
	const query = input.value;
	document.getElementById('match-error').innerText = '';
	input.blur();
	usingApiKey()
	.then(api_key => {
		liEls.search.results.innerHTML = '';
		const params = {query: encodeURIComponent(query)};
		if (provider !== 'comicvine') params.provider = provider;
		fetchAPI('/volumes/search', api_key, params)
		.then(json => {
			if (generation !== metadataSearchGeneration || provider !== liEls.search.provider.value || query !== input.value) return;
			const groups = MetadataSearchPresentation.groups(json.result);
			const entries = groups ? groups.flatMap(group => [{search_group: group}, ...group.results]) : json.result;
			entries.forEach(result => {
				if (result.search_group) {
					liEls.search.results.appendChild(MetadataSearchPresentation.heading(result.search_group, true));
					return;
				}
				const entry = liEls.preBuild.searchResult.cloneNode(true);

				const title = entry.querySelector('td:nth-child(1) a');
				title.href = groups ? MetadataSearchPresentation.safeLink(result.site_url) : result.site_url;
				title.innerText = buildMatchTitle(
					result.title, result.year, result.issue_count
				) + ` · ${result.metadata_source ? result.metadata_source.provider + ':' + result.metadata_source.id : provider}`;
				if (groups) {
					const note = document.createElement('p');
					note.textContent = MetadataSearchPresentation.annotations(result);
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

				liEls.search.results.appendChild(entry);
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

function importLibrary(api_key, rename=false) {
	const data = [...liEls.proposalList.querySelectorAll(
		'tr:has(input[type="checkbox"]:checked)'
	)]
		.filter(i => rowidToFilepath[i.dataset.rowid].identity !== null)
		.map(e => {
			const rowid = e.dataset.rowid;
			return {
				'filepath': rowidToFilepath[rowid].filepath,
				'provider': rowidToFilepath[rowid].identity.provider,
				'provider_id': rowidToFilepath[rowid].identity.id
			};
		});

	hide([liEls.views.list], [liEls.views.loading]);
	document.getElementById('import-error').innerText = '';
	sendAPI('POST', '/libraryimport/preview', api_key, {rename_files: rename}, data)
	.then(async response => {
		if (!response.ok) throw response;
		const preview = (await response.json()).result;
		const message = preview.plans.map(plan =>
			`${plan.status}: ${plan.source}\n→ ${plan.target || 'Review required'}\nVolume ${plan.volume_id}; issues ${JSON.stringify(plan.issue_ids)}\nEffects: ${JSON.stringify(plan.effects)}\n${JSON.stringify(plan.diagnostics)}`
		).join('\n\n');
		if (!window.confirm(`Organization preview (no files moved yet):\n\n${message}\n\nApply only ready plans? Review files will remain unchanged.`)) return;
		const applied = await sendAPI('POST', `/local-organization/${encodeURIComponent(preview.id)}/apply`, api_key);
		if (!applied.ok) throw applied;
		const result = (await applied.json()).result;
		if (result.review?.length || result.jobs.some(job => job.state !== 'completed')) {
			window.alert(`Organization needs review. Artifacts and existing journals are retained.\n${JSON.stringify(result, null, 2)}`);
		}
	})
	.then(() => hide([liEls.views.loading], [liEls.views.start]))
	.catch(e => {
		hide([liEls.views.loading], [liEls.views.list]);
		document.getElementById('import-error').innerText = e.status === 409
			? 'Identity conflict: no automatic merge or provider switch. Earlier groups may already be imported.'
			: 'Import stopped. Check the selected provider credentials/rate limits. Earlier groups may already be imported; review before retrying.';
	});
};

// code run on load

usingApiKey()
.then(api_key => {
	liEls.buttons.run.onclick = e => loadProposal(api_key);
	liEls.buttons.import.onclick = e => importLibrary(api_key, false);
	liEls.buttons.importRename.onclick = e => importLibrary(api_key, true);
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
