const assert = require('node:assert/strict');
const vm = require('node:vm');
const harness = require('./library_import_contract.cjs');
const {context} = harness;

harness.finished.then(async () => {
    const ignored = [];
    context.sendAPI = async (method, path, key, params, body) => {
        assert.equal(path, '/libraryimport/ignore');
        ignored.push(...body.files);
        return {json: async () => ({result: body.files.map(file => ({path:file,status:'ignored'}))})};
    };
    const makeRow = (id, importing, action) => {
        const choices = {'.import-file-select':{checked:importing},
            '.bulk-match-select':{checked:action}};
        return {data:{row_id:id,filepath:`/fixture/${id}.cbz`,file_title:id},
            element:{hidden:false,remove(){this.removed=true;},querySelector:key => choices[key]},
            status:''};
    };
    const rows = [makeRow('one',true,false),makeRow('two',false,true),makeRow('three',true,false)];
    context.rows = rows;
    vm.runInContext(`importRows.clear(); importRowsByPath.clear(); rows.forEach(row => {
        importRows.set(row.data.row_id,row); importRowsByPath.set(row.data.filepath,row);
    }); importDiscovered=3; updateSelectionSummary();`, context);
    assert.equal(context.document.getElementById('import-selection-count').textContent,'2 selected for import');
    assert.equal(context.document.getElementById('bulk-selection-count').textContent,'1 selected for actions');
    assert.equal(context.document.getElementById('ignore-selected').disabled,false);
    assert.equal(context.document.getElementById('create-custom-selected').disabled,false);
    await vm.runInContext('ignoreImportRows(actionRows())',context);
    assert.deepEqual(ignored,['/fixture/two.cbz']);
    assert.equal(rows[0].element.removed,undefined);
    assert.equal(rows[1].element.removed,true);
    assert.equal(rows[2].element.removed,undefined);
    assert.equal(rows[0].element.querySelector('.import-file-select').checked,true);
    assert.equal(rows[2].element.querySelector('.import-file-select').checked,true);
    assert.equal(context.document.getElementById('bulk-selection-count').textContent,'0 selected for actions');

    let endpoint;
    context.fetchAPI = async path => {
        endpoint=path;
        return {result:[{title:'Civil War Ultimate Edition',provider_id:'f'.repeat(32),
            year:2024,issue_count:13}]};
    };
    vm.runInContext(`importSearchContext={mode:'single',ids:['one'],generation:importGeneration};`,context);
    context.document.getElementById('search-input').value='Civil War';
    await vm.runInContext('searchMyPublications()',context);
    assert.equal(endpoint,'/local-publications');
    const found=context.document.getElementById('my-publications-results').children;
    assert.equal(found.length,1);
    assert.match(found[0].textContent,/Civil War Ultimate Edition.*13 issues/);
    console.log('Custom actions: import/action separation, exact ignore targets, local search PASS');
}).catch(error => { console.error(error); process.exitCode=1; });
