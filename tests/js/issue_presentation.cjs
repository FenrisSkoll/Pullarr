// Execute the actual issue-row renderer with a minimal DOM fixture.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('frontend/static/js/view_volume.js', 'utf8');
const renderer = source.slice(source.indexOf('function fillTable('), source.indexOf('function fillPage('));
for (const [fields, expected] of [
    [{title: 'TPB', display_title: 'Cyberpunk 2077: You Have My Word'}, 'Cyberpunk 2077: You Have My Word'],
    [{title: 'HC', display_title: 'Full: Parent'}, 'Full: Parent'],
    [{title: 'Crime'}, 'Crime'],
    [{title: '1', display_title: '1'}, '1'],
    [{title: 'Original', display_title: null}, 'Original'],
    [{title: null, display_title: null}, null],
    [{title: 'Original', display_title: ''}, ''],
]) {
    let rendered;
    class IssueEntry {
        constructor() {
            for (const key of ['entry', 'title', 'issue_number', 'date', 'auto_search', 'manual_search', 'convert']) this[key] = {};
            this.monitored = {dataset: {}};
            rendered = this;
        }
        setMonitorIcon() {}
        setDownloaded() {}
    }
    const context = {IssueEntry, ViewEls: {
        issues_list: {appendChild() {}},
        pre_build: {issue_entry: {cloneNode() { return {dataset: {}}; }}}
    }};
    vm.createContext(context);
    vm.runInContext(renderer, context);
    context.fillTable([{id: 1, issue_number: '1', files: [], ...fields}], 'fake-test-key');
    assert.equal(rendered.title.innerText, expected);
}
console.log('Issue presentation renderer: 7 cases passed');
