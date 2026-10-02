function loadFields() {
	fetch(`${url_base}/api/public`)
	.then(response => response.json())
	.then(json => {
		const am = json.result.authentication_method;
		if (am == 1) {
			document.querySelector("#username-input").parentElement.remove();
			document.querySelector("#login-form").classList.remove('hidden');
			document.querySelector("#password-input").focus();
		} else {
			document.querySelector("#login-form").classList.remove('hidden');
		};
	});
};

function login() {
	const submit = document.querySelector('#login-form button');
	if (submit.disabled) return;
	submit.disabled = true;
	const error = document.querySelector('#error-message');
	error.classList.add('hidden');

	const password_input = document.querySelector('#password-input');
	const data = {
		'password': password_input.value
	};

	const username_input = document.querySelector('#username-input');
	if (username_input !== null)
		data.username = username_input.value

	fetch(`${url_base}/api/auth`, {
		'method': 'POST',
		'headers': {'Content-Type': 'application/json'},
		'body': JSON.stringify(data)
	})
	.then(response => {
		if (!response.ok) return Promise.reject(response.status);
		return response.json();
	})
	.then(json => registerLogin(json.result.api_key))
	.catch(e => {
		// Login failed
		if (e === 401) {
			error.classList.remove('hidden');
		} else {
			error.textContent = 'Unable to sign in. Check the connection and try again.';
			error.classList.remove('hidden');
		};
	}).finally(() => { submit.disabled = false; });
};

function registerLogin(api_key) {
	let data;
	try { data = JSON.parse(localStorage.getItem('kapowarr') || '{}') || {}; }
	catch { data = {}; }
	if (typeof data !== 'object') data = {};
	data.api_key = api_key;
	data.last_login = Date.now() / 1000;
	localStorage.setItem('kapowarr', JSON.stringify(data));
	redirect();
};

function redirect() {
	const parameters = new URLSearchParams(window.location.search);
	try {
		const target = new URL(parameters.get('redirect') || `${url_base}/`, location.origin);
		window.location.href = target.origin === location.origin ? target.href : `${url_base}/`;
	} catch { window.location.href = `${url_base}/`; }
};

// code run on load

const url_base = document.querySelector('#url_base').dataset.value;

usingApiKey(false)
.then(api_key => {
	if (api_key)
		redirect();
	else
		loadFields();
})

try {
	if (JSON.parse(localStorage.getItem('kapowarr') || '{}')?.theme === 'dark')
		document.querySelector(':root').classList.add('dark-mode');
} catch { /* Invalid legacy browser preferences use the default theme. */ }

document.querySelector('#login-form').addEventListener('submit', event => {
	event.preventDefault();
	login();
});
