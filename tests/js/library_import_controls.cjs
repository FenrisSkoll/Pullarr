const assert = require('node:assert/strict');
const vm = require('node:vm');
const harness = require('./library_import_contract.cjs');
const {context} = harness;

function row(number) {
    const cell = {children:[], replaceChildren(){this.children=[];}, append(...items){this.children.push(...items);}};
    const path = `/synthetic/Book ${number}.cbz`;
    return {data:{filepath:path}, identity:{provider:'comicvine',id:'2127'},
        element:{querySelector:selector => selector === '.issue-column' ? cell : {checked:false}},
        catalogue:null, issueIds:[], manual:false, choiceVersion:0};
}

harness.finished.then(async () => {
    const records = [];
    context.sendAPI = async (method, path, key, params, body) => {
        records.push({path, body});
        if (path !== '/libraryimport/catalog') throw new Error(path);
        return {ok:true, json:async()=>({result:{title:'Books',issues:body.files.map((file,index)=>({id:file.split(' ')[1].split('.')[0],number:String(index+1)})),
            proposals:Object.fromEntries(body.files.map(file=>[file,{issue_ids:[file.split(' ')[1].split('.')[0]]}]))}})};
    };
    const rows = Array.from({length:205},(_,index)=>row(index+1));
    rows[40].manual = true; rows[40].issueIds = ['chosen'];
    context.rows = rows;
    vm.runInContext("importRows.clear(); rows.forEach((row,index)=>importRows.set(String(index),row));",context);
    await vm.runInContext('loadCatalogue(rows[0])',context);
    assert.deepEqual(records.map(record=>record.body.files.length),[100,100,5]);
    assert.deepEqual(rows.map(item=>item.issueIds[0]).filter(Boolean).length,205);
    assert.equal(rows[204].issueIds[0],'205');
    assert.equal(rows[40].issueIds[0],'chosen');
    assert.equal(vm.runInContext('importCatalogues.size',context),1);

    vm.runInContext('importRows.clear(); importOffset=0;',context);
    const scans=[];
    context.fetchAPI = async (path,key,params) => {
        scans.push({path,params});
        return {result:{items:[],next_offset:null,counts:{scanned_folders:params.folder_limit,
            eligible_folders:905,discovered:0,already_managed:0,not_yet_scanned_folders:0,limited:false}}};
    };
    context.document.getElementById('auto-match-input').checked=false;
    context.document.getElementById('target-input').value='true';
    for (const limit of [20,50,100,200,500,1000,50000]) {
        context.document.getElementById('scan-folder-limit').value=String(limit);
        await vm.runInContext('scanImport()',context);
        assert.equal(scans.at(-1).params.folder_limit,limit);
        assert.equal(scans.at(-1).params.page_size,100);
    }
    assert.equal(vm.runInContext('importScope.folder_limit',context),50000);
    context.document.getElementById('match-folder-limit').value='20';
    vm.runInContext("importScope.folder_filter='30%20Days';",context);
    await vm.runInContext('suggestImportPublications(++importMatchGeneration)',context);
    assert.equal(scans.at(-1).params.folder_limit,20);
    assert.equal(scans.at(-1).params.folder_filter,'30%20Days');
    let release;
    context.fetchAPI = () => new Promise(resolve => { release=resolve; });
    const pending = vm.runInContext('suggestImportPublications(++importMatchGeneration)',context);
    context.document.getElementById('stop-matching').onclick();
    release({result:{items:[{filepath:'/synthetic/Book 1.cbz',parsed:{title:'Books',year:2002}}],
        next_offset:null,counts:{discovered:1,scanned_folders:20}}});
    await pending;
    assert.equal(vm.runInContext('importSuggestions.size',context),0);
    const changed = row(999);
    context.changed = changed;
    vm.runInContext("importRows.set('changed',changed);",context);
    let finish;
    context.sendAPI = () => new Promise(resolve => { finish=resolve; });
    const staleCatalogue = vm.runInContext('loadCatalogue(changed)',context);
    await new Promise(resolve=>setImmediate(resolve));
    changed.identity = {provider:'metron',id:'22'};
    ++changed.choiceVersion;
    finish({ok:true,json:async()=>({result:{title:'Old ComicVine result',issues:[],
        proposals:{'/synthetic/Book 999.cbz':{issue_ids:['999']}}}})});
    await staleCatalogue;
    assert.equal(changed.catalogue,null);
    assert.deepEqual(changed.issueIds,[]);
    console.log('Library Import bounded scan, matching stop, stale choice guard and 205 file proposals PASS');
}).catch(error=>{console.error(error);process.exitCode=1;});
