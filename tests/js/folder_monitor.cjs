// Monitoring UI contract: text-only health, no automatic reconciliation POST.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(key) {
    if (!elements.has(key)) elements.set(key, {value: '', checked: false,
        classList: {remove() {}, add() {}}, querySelectorAll: () => []});
    return elements.get(key);
}
let posted = [];
const status = {enabled: false, backend: 'bounded_polling',
    roots: [{path: '<img src=x onerror=alert(1)>', health: 'unavailable', error: 'permission_denied'}],
    counts: [{status: 'review', count: 2}]};
const context = vm.createContext({document: {querySelector: element}, Date,
    fetchAPI: () => Promise.resolve({result: status}),
    sendAPI: (...args) => { posted.push(args); return Promise.resolve(); }});
const source = fs.readFileSync('frontend/static/js/settings_mediamanagement.js', 'utf8');
vm.runInContext(source.slice(0, source.indexOf('usingApiKey()')), context);
(async () => {
    await vm.runInContext("fillMonitorStatus('fixture')", context);
    assert.match(element('#folder-monitor-status').textContent, /Disabled/);
    assert.match(element('#folder-monitor-status').textContent, /permission_denied/);
    assert.equal(element('#folder-monitor-status').innerHTML, undefined);
    assert.equal(posted.length, 0);
    context.fetchAPI = () => Promise.reject(new Error('offline'));
    await vm.runInContext("fillMonitorStatus('fixture')", context);
    assert.match(element('#folder-monitor-status').textContent, /Manual scans remain available/);
    console.log('Folder monitoring health, text safety and read-only preview contracts passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
