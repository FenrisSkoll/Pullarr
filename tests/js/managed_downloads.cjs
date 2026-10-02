'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.textContent = ''; }
    appendChild(child) { this.children.push(child); return child; }
    replaceChildren() { this.children = []; }
}
const fields = new Map();
for (const name of ['rows', 'status', 'refresh', 'previous', 'next']) fields.set('managed-download-' + name, new Element('div'));
let requests = [], posts = [], pending, confirmed = 0;
const context = {
    document: {createElement: tag => new Element(tag), getElementById: id => fields.get(id)},
    usingApiKey: () => new Promise(() => {}),
    fetchAPI: () => new Promise(resolve => requests.push(resolve)),
    sendAPI(method, path, key, query, body) {
        posts.push({method, path, body});
        return new Promise(resolve => { pending = resolve; });
    },
    confirm: () => { confirmed++; return false; }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/managed_downloads.js', 'utf8'), context);
const value = {id: 'owned-job', title: '<img src=x onerror=alert(1)>', source: 'fixture', client: 'qBittorrent',
    protocol: 'torrent', acquisition: 'completed', torrent_state: 'seeding', torrent: {ratio: .5, seeding_time: 60},
    retention: {mode: 'client_managed', ratio_target: '1', seed_seconds: 86400}, requirements: {minimumratio: '1'}};
const flush = () => new Promise(resolve => setImmediate(resolve));
const field = name => fields.get('managed-download-' + name);
const all = node => [node, ...node.children.flatMap(all)];
(async () => {
    context.setupManagedDownloads('synthetic');
    field('refresh').onclick();
    requests[1]({result: [value]}); await flush();
    requests[0]({result: []}); await flush();
    const nodes = all(field('rows'));
    assert.ok(nodes.some(n => n.textContent === 'Imported'));
    assert.ok(nodes.some(n => n.textContent.includes(value.title)));
    assert.ok(nodes.some(n => n.textContent === 'Policy: Client managed'));
    assert.ok(!nodes.some(n => n.textContent.includes('Time target')));
    const button = nodes.find(n => n.textContent === 'Remove torrent + data');
    button.onclick(); button.onclick(); assert.equal(posts.length, 1);
    pending({ok: true, json: async () => ({result: {eligible: false, reasons: ['tracker_requirements']}})});
    await flush();
    assert.match(field('status').textContent, /minimum seed requirements/);
    assert.equal(confirmed, 0);
    button.onclick();
    pending({ok: true, json: async () => ({result: {eligible: true, confirmation: 'review'}})});
    await flush();
    assert.equal(confirmed, 1); assert.equal(posts.length, 2); // Cancel never mutates.
    button.onclick();
    pending({ok: false, json: async () => ({error: 'stale'})}); await flush();
    assert.match(field('status').textContent, /Refresh and review/);
    assert.ok(nodes.filter(n => n.tagName === 'th').every(n => n.scope === 'col'));
    console.log('Managed downloads: safe text, lifecycle labels, stale responses, mutation lock, review/cancel and controlled failure PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
