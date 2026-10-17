// Execute the actual issue-row renderer with a minimal DOM fixture.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('frontend/static/js/view_volume.js', 'utf8');
const renderer = source.slice(source.indexOf('function meaningfulIssueText('), source.indexOf('function fillPage('));
for (const [fields, expected] of [
    [{title: 'TPB', display_title: 'Cyberpunk 2077: You Have My Word'}, 'Cyberpunk 2077: You Have My Word'],
    [{title: 'HC', display_title: 'Full: Parent'}, 'Full: Parent'],
    [{title: 'Crime'}, 'Crime'],
    [{title: '1', display_title: '1'}, '1'],
    [{title: 'Original', display_title: null}, 'Original'],
    [{title: null, display_title: null}, 'Untitled'],
    [{title: 'Original', display_title: ''}, 'Original'],
    [{title: '  ', display_title: '\t'}, 'Untitled'],
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
    const context = {IssueEntry, document:{querySelector:()=>null}, ViewEls: {
        issues_list: {appendChild() {}},
        pre_build: {issue_entry: {cloneNode() { return {dataset: {}}; }}}
    }};
    vm.createContext(context);
    vm.runInContext(renderer, context);
    const issue = Object.freeze({id: 1, issue_number: '1', files: [], ...fields});
    context.fillTable([issue], 'fake-test-key');
    assert.equal(rendered.title.innerText, expected);
    assert.equal(issue.title, fields.title);
}
const heading = source.slice(source.indexOf('function meaningfulIssueText('),
    source.indexOf('function fillTable(')) + source.slice(source.indexOf('function issueInfoHeading('),
    source.indexOf('function showIssueInfo('));
const titles = vm.createContext({});
vm.runInContext(heading, titles);
for (const [issue, expected] of [
    [{title:null, display_title:null, issue_number:'1', date:null}, 'Issue #1'],
    [{title:'Chapter One', issue_number:'1'}, 'Chapter One · #1'],
    [{title:'Chapter One', issue_number:'1', date_display:'2025-01-01'}, 'Chapter One · #1 · 2025-01-01'],
    [{title:'Raw', display_title:'Display', issue_number:'1A', date_display:'2025-01'}, 'Display · #1A · 2025-01'],
    [{title:'  ', display_title:'', issue_number:'[nn]', date_display:' ', date:'2025'}, 'Issue #[nn] · 2025'],
]) assert.equal(titles.issueInfoHeading(issue), expected);
console.log('Issue presentation renderer and heading: 8 row cases, 5 headings passed');
