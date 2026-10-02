function fillSettings(api_key) {
	fetchAPI('/settings', api_key)
	.then(json => {
		document.querySelector('#bind-address-input').value = json.result.host;
		document.querySelector('#port-input').value = json.result.port;
		document.querySelector('#url-base-input').value = json.result.url_base;
		document.querySelector('#username-input').value = json.result.auth_username;
		document.querySelector('#password-input').value = json.result.auth_password;
		document.querySelector('#api-input').value = api_key;
		document.querySelector('#proxy-type-input').value = json.result.proxy_type || '';
		document.querySelector('#proxy-host-input').value = json.result.proxy_host;
		document.querySelector('#proxy-port-input').value = json.result.proxy_port;
		document.querySelector('#proxy-username-input').value = json.result.proxy_username;
		document.querySelector('#proxy-password-input').value = json.result.proxy_password;
		document.querySelector('#proxy-ignored-addresses-input').value = json.result.proxy_ignored_addresses.join(',');
		document.querySelector('#cv-input').value = json.result.comicvine_api_key;
		document.querySelector('#metron-token-input').value = json.result.metron_api_token;
		document.querySelector('#metron-budget-input').value = json.result.metron_refresh_requests_per_day;
		document.querySelector('#gcd-enabled-input').checked = json.result.gcd_enabled;
		document.querySelector('#catalog-enabled-input').checked = json.result.gcd_catalog_enabled;
		document.querySelector('#catalog-path-input').value = json.result.gcd_catalog_path;
		document.querySelector('#gcd-username-input').value = json.result.gcd_username;
		document.querySelector('#gcd-password-input').value = json.result.gcd_password;
		document.querySelector('#flaresolverr-input').value = json.result.flaresolverr_base_url;
		document.querySelector('#log-level-input').value = json.result.log_level;
		document.querySelector('#db-backup-folder-input').value = json.result.db_backup_folder;
		document.querySelector('#db-backup-count-input').value = json.result.db_backup_amount;

		if (json.result.auth_username && json.result.auth_password) {
			document.querySelector('#auth-toggle').value = 'username-password';
		} else if (json.result.auth_password) {
			document.querySelector('#auth-toggle').value = 'password';
		};
	});
	document.querySelector('#theme-input').value = getLocalStorage('theme')['theme'];
};

function saveSettings(api_key) {
	document.querySelector("#save-button p").innerText = 'Saving';
	document.querySelector('#proxy-host-input').classList.remove('error-input');
	document.querySelector('#proxy-username-input').classList.remove('error-input');
	document.querySelector('#proxy-password-input').classList.remove('error-input');
	document.querySelector('#cv-input').classList.remove('error-input');
	document.querySelector("#flaresolverr-input").classList.remove('error-input');
	document.querySelector("#db-backup-folder-input").classList.remove("error-input");

	let proxyIgnoredAddresses = document.querySelector('#proxy-ignored-addresses-input').value.split(',');
	if (proxyIgnoredAddresses[0] === '') {
		proxyIgnoredAddresses = []
	}
	const data = {
		'host': document.querySelector('#bind-address-input').value,
		'port': parseInt(document.querySelector('#port-input').value),
		'url_base': document.querySelector('#url-base-input').value,
		'auth_username': '',
		'auth_password': '',
		'proxy_type': document.querySelector('#proxy-type-input').value || null,
		'proxy_host': document.querySelector('#proxy-host-input').value,
		'proxy_port': parseInt(document.querySelector('#proxy-port-input').value),
		'proxy_username': document.querySelector('#proxy-username-input').value,
		'proxy_password': document.querySelector('#proxy-password-input').value,
		'proxy_ignored_addresses': proxyIgnoredAddresses,
		'comicvine_api_key': document.querySelector('#cv-input').value,
		'metron_api_token': document.querySelector('#metron-token-input').value,
		'metron_refresh_requests_per_day': parseInt(document.querySelector('#metron-budget-input').value),
		'gcd_enabled': document.querySelector('#gcd-enabled-input').checked,
		'gcd_catalog_enabled': document.querySelector('#catalog-enabled-input').checked,
		'gcd_catalog_path': document.querySelector('#catalog-path-input').value,
		'gcd_username': document.querySelector('#gcd-username-input').value,
		'gcd_password': document.querySelector('#gcd-password-input').value,
		'flaresolverr_base_url': document.querySelector('#flaresolverr-input').value,
		'log_level': parseInt(document.querySelector('#log-level-input').value),
		'db_backup_folder': document.querySelector("#db-backup-folder-input").value,
		'db_backup_amount': parseInt(document.querySelector("#db-backup-count-input").value)
	};

	const auth_toggle = document.querySelector('#auth-toggle');
	if (auth_toggle.value === 'username-password')
		data.auth_username = document.querySelector('#username-input').value;

	if (auth_toggle.value === 'username-password' || auth_toggle.value === 'password')
		data.auth_password = document.querySelector('#password-input').value;

	sendAPI('PUT', '/settings', api_key, {}, data)
	.then(response => response.json())
	.then(json => {
		document.querySelector("#save-button p").innerText = 'Saved';
		fillSettings(api_key);
	})
	.catch(async e => {
		document.querySelector("#save-button p").innerText = 'Failed';
		const json = await e.json();
		if (json.error === 'MetadataProviderError' && json.result.provider === 'metron') {
			document.querySelector('#metron-test-result').innerText = `Metron: ${json.result.reason}`;
			return;
		};
		if (
			json.error === "InvalidKeyValue"
			&& json.result.key === "comicvine_api_key"
		)
			document.querySelector('#cv-input').classList.add('error-input');

		else if (
			json.error === "InvalidKeyValue"
			&& json.result.key === "proxy_host"
		)
			document.querySelector('#proxy-host-input').classList.add('error-input');

		else if (
			json.error === "InvalidKeyValue"
			&& json.result.key === "proxy_username"
		) {
			document.querySelector('#proxy-username-input').classList.add('error-input');
			document.querySelector('#proxy-password-input').classList.add('error-input');
		}

		else if (
			json.error === "InvalidKeyValue"
			&& json.result.key === "flaresolverr_base_url"
		)
			document.querySelector("#flaresolverr-input").classList.add('error-input');

		else if (
			json.error === "FolderNotFound"
		)
			document.querySelector("#db-backup-folder-input").classList.add("error-input");

		else
			console.log(json.error);
	});
};

function maskApiKey() {
	document.querySelector('#api-input').type = 'password';
	const button = document.querySelector('#reveal-api');
	button.textContent = 'Show API Key';
	button.setAttribute('aria-pressed', 'false');
}

function setupApiKeyControls(getKey, changed) {
	const input = document.querySelector('#api-input');
	const copy = document.querySelector('#copy-api');
	const reveal = document.querySelector('#reveal-api');
	const regenerate = document.querySelector('#generate-api');
	const status = document.querySelector('#api-key-status');
	maskApiKey();
	copy.onclick = async () => {
		if (copy.disabled || regenerate.disabled) return;
		copy.disabled = true;
		try {
			await navigator.clipboard.writeText(getKey());
			status.textContent = 'API key copied.';
		} catch (_) {
			status.textContent = 'Clipboard unavailable. Use Show and copy the key manually.';
		} finally { copy.disabled = false; }
	};
	reveal.onclick = () => {
		const show = input.type === 'password';
		input.type = show ? 'text' : 'password';
		reveal.textContent = show ? 'Hide API Key' : 'Show API Key';
		reveal.setAttribute('aria-pressed', String(show));
	};
	regenerate.onclick = async () => {
		if (regenerate.disabled || copy.disabled) return;
		if (!confirm('Regenerate the application API key? Applications using the previous key will need the new key.')) return;
		regenerate.disabled = true;
		maskApiKey();
		try {
			const response = await sendAPI('POST', '/settings/api_key', getKey());
			const json = await response.json();
			if (json.error || typeof json.result?.api_key !== 'string' || !json.result.api_key) throw new Error('key unavailable');
			setLocalStorage({api_key: json.result.api_key});
			input.value = json.result.api_key;
			changed(json.result.api_key);
			status.textContent = 'API key regenerated. Update applications using the previous key.';
		} catch (_) {
			status.textContent = 'Could not confirm regeneration. Reload to check the current key before retrying.';
		} finally { regenerate.disabled = false; }
	};
	window.addEventListener('pagehide', maskApiKey);
	window.addEventListener('pageshow', maskApiKey);
}

// code run on load

usingApiKey()
.then(api_key => {
	fillSettings(api_key);
	setupApiKeyControls(() => api_key, key => { api_key = key; });
	for (const action of ['test', 'sync', 'status']) {
		document.querySelector(`#catalog-${action}`).onclick = async () => {
			const output = document.querySelector('#catalog-result');
			output.textContent = 'Reading saved catalog configuration…';
			try {
				const response = action === 'status'
					? await fetchAPI('/settings/gcd/catalog', api_key)
					: await (await sendAPI('POST', `/settings/gcd/catalog/${action}`, api_key)).json();
				output.textContent = JSON.stringify(response.result);
			} catch (error) {
				const response = await error.json().catch(() => null);
				output.textContent = response?.result?.reason || 'Catalog operation failed; previous graph retained.';
			}
		};
	}
	document.querySelector('#gcd-test').onclick = async () => {
		const status = document.querySelector('#gcd-test-result');
		status.textContent = 'Testing…';
		try {
			await sendAPI('POST', '/settings/gcd/test', api_key, {}, {
				username: document.querySelector('#gcd-username-input').value,
				password: document.querySelector('#gcd-password-input').value
			});
			status.textContent = 'Read-only connection successful';
		} catch (error) {
			const response = await error.json().catch(() => null);
			status.textContent = `GCD: ${response?.result?.reason || 'connection unavailable'}`;
		}
	};
	document.querySelector('#gcd-clear').onclick = async () => {
		if (!confirm('Remove saved GCD credentials and use anonymous access?')) return;
		await sendAPI('DELETE', '/settings/gcd/credentials', api_key);
		fillSettings(api_key);
	};
	document.querySelector('#metron-test').onclick = async () => {
		const status = document.querySelector('#metron-test-result');
		status.innerText = 'Testing…';
		try {
			await sendAPI('POST', '/settings/metron/test', api_key, {}, {
				metron_api_token: document.querySelector('#metron-token-input').value
			});
			status.innerText = 'Token valid';
		} catch (error) {
			status.innerText = 'Metron token test failed; check token or retry after the rate-limit window.';
		};
	};
	document.querySelector('#save-button').onclick = e => saveSettings(api_key);
	document.querySelector('#download-logs-button').href =
		`${url_base}/api/system/logs?api_key=${api_key}`;
});

document.querySelector('#theme-input').onchange = e => {
	const value = document.querySelector('#theme-input').value;
	setLocalStorage({'theme': value});
	setupTheme();
};
