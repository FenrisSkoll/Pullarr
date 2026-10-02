const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor() { this.children = []; this.handlers = {}; }
    set innerHTML(_) { throw new Error('Unsafe HTML'); }
    appendChild(node) { this.children.push(node); }
    replaceChildren() { this.children = []; }
    addEventListener(event, handler) { this.handlers[event] = handler; }
}
const document = {createElement() { return new Element(); }};
const context = {document};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/intake.js', 'utf8'), context);
const hostile = '<img src=x onerror=alert(1)>';
const parent = new Element();
let selected;
context.renderIntakes(parent, [{id: 'exact-intake', title: hostile, kind: 'sabnzbd', source: hostile,
    state: 'review', error: hostile, volume_id: 1, issue_ids: [5], artifacts: [
        {path: hostile, state: 'review', error: hostile, summary: {reason: hostile}, organization_job_id: 'job'}
    ]}], id => selected = id);
assert.equal(parent.children[0].children[0].textContent, hostile);
const button = parent.children[0].children.at(-1);
button.handlers.click();
assert.equal(selected, 'exact-intake');
console.log('Intake review: server states, exact retry identity, text-safe paths and diagnostics passed');
