// Exercise the production row renderer: rich labels/dates are text, never HTML.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('frontend/static/js/view_volume.js', 'utf8');
const renderer = source.slice(source.indexOf('function fillTable('), source.indexOf('function fillPage('));
for (const [label, date] of [['[nn]', '2021-12'], ['1A', '2021'], ['01', '2021-12-31'],
                           ['<img src=x onerror=alert(1)>', '<script>not markup</script>']]) {
    let row;
    class IssueEntry {
        constructor() {
            for (const key of ['entry', 'title', 'issue_number', 'date', 'auto_search', 'manual_search', 'convert']) this[key] = {};
            this.monitored = {dataset: {}};
            row = this;
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
    context.fillTable([{id: 1, issue_number: label, date: null, date_display: date,
                       files: [], title: '<b>literal</b>'}], 'fixture');
    assert.equal(row.issue_number.innerText, label);
    assert.equal(row.date.innerText, date);
    assert.equal(row.title.innerText, '<b>literal</b>');
    for (const node of [row.issue_number, row.date, row.title]) assert.equal(node.innerHTML, undefined);
}
const general = fs.readFileSync('frontend/static/js/general.js', 'utf8');
assert.equal((general.match(/issue_facts: '1'/g) || []).length, 2);
console.log('Rich issue transport/display: raw labels, partial dates and hostile text passed');
