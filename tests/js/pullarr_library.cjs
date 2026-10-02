'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/static/js/volumes.js', 'utf8');
const controls = new Map();
const requests = [], rendered = [];
const field = () => ({value: '', textContent: '', disabled: false});
const context = {
    document: {querySelector(id) { if (!controls.has(id)) controls.set(id, field()); return controls.get(id); }},
    library_els: {mass_edit: {progress: field(), select_all: {checked: true}},
        pages: {loading: 'loading', empty: 'empty', view: 'view'},
        view_options: {sort: {value: 'title'}, filter: {value: ''}}, search: {input: field()}},
    showLibraryPage() {},
    populateLibrary(rows) { rendered.push(rows); },
    fetchAPI(path, key, params) { return new Promise((resolve, reject) => requests.push({path, key, params, resolve, reject})); }
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('let libraryOffset'), source.indexOf('function searchLibrary')), context);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    vm.runInContext("fetchLibrary('fixture')", context);
    assert.equal(requests[0].params.limit, 50);
    assert.equal(requests[0].params.offset, 0);
    vm.runInContext("libraryOffset = 50; fetchLibrary('fixture', true)", context);
    const page = Array.from({length: 50}, (_, id) => ({id: id + 51}));
    requests[1].resolve({result: page}); await flush();
    requests[0].resolve({result: [{id: 1}]}); await flush();
    assert.equal(rendered.length, 1);
    assert.equal(rendered[0][0].id, 51);
    assert.match(controls.get('#library-page').textContent, /^Page 2/);
    assert.equal(controls.get('#library-previous').disabled, false);
    assert.equal(context.library_els.mass_edit.select_all.checked, false);
    vm.runInContext("fetchLibrary('fixture')", context);
    requests[2].reject(new Error('controlled')); await flush();
    assert.match(controls.get('#library-error').textContent, /Unable to load/);
    assert.equal(requests[2].params.offset, 0);
    console.log('Pullarr Library: bounded paging, stale suppression, reset, page-scoped selection and controlled error passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
