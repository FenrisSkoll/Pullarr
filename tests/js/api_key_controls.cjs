'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const events = new Map();
const node = () => ({type: 'password', value: '', textContent: '', disabled: false,
    attributes: {}, setAttribute(name, value) { this.attributes[name] = value; }});
let key = 'synthetic-current-key', clipboard, stored, calls = 0, pending;
const context = {
    document: {querySelector(id) { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); }},
    window: {addEventListener(name, handler) { events.set(name, handler); }},
    navigator: {clipboard: {async writeText(value) { clipboard = value; }}},
    confirm: () => true,
    sendAPI(method, path, supplied) {
        assert.equal(method, 'POST'); assert.equal(path, '/settings/api_key');
        assert.equal(supplied, key); calls++;
        return new Promise(resolve => { pending = resolve; });
    },
    setLocalStorage(value) { stored = value; },
    getKey: () => key, changed(value) { key = value; }
};
vm.createContext(context);
const source = fs.readFileSync('frontend/static/js/settings_general.js', 'utf8');
vm.runInContext(source.slice(source.indexOf('function maskApiKey'), source.indexOf('// code run on load')), context);
vm.runInContext('setupApiKeyControls(getKey, changed)', context);
const field = name => elements.get('#' + name);
(async () => {
    assert.equal(field('api-input').type, 'password');
    await field('copy-api').onclick();
    assert.equal(clipboard, key); assert.equal(calls, 0);
    assert.equal(field('api-input').type, 'password');
    field('reveal-api').onclick(); assert.equal(field('api-input').type, 'text');
    assert.equal(field('reveal-api').attributes['aria-pressed'], 'true');
    field('reveal-api').onclick(); assert.equal(field('api-input').type, 'password');
    field('reveal-api').onclick(); events.get('pagehide')();
    assert.equal(field('api-input').type, 'password');
    const operation = field('generate-api').onclick();
    await field('generate-api').onclick(); assert.equal(calls, 1);
    pending({json: async () => ({result: {api_key: 'synthetic-replacement-key'}})});
    await operation;
    assert.equal(field('api-input').value, key); assert.equal(stored.api_key, key);
    assert.equal(field('api-input').type, 'password');
    await field('copy-api').onclick(); assert.equal(clipboard, key);
    context.navigator.clipboard.writeText = async () => { throw new Error('controlled'); };
    await field('copy-api').onclick();
    assert.match(field('api-key-status').textContent, /Clipboard unavailable/);
    assert.equal(field('copy-api').disabled, false);
    for (const element of elements.values()) assert.ok(!element.textContent.includes(key));
    console.log('API key controls: copy, reveal, mask, regeneration separation, current key, lock and safe failure PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
