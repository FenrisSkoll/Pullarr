// One submit sends exact provider-qualified choices without draft persistence.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
    return {value:'', textContent:'', checked:false, disabled:false, children:[],
        append(child) { this.children.push(child); },
        appendChild(child) { this.children.push(child); return child; },
        replaceChildren() { this.children = []; },
        querySelector() { return element(); },
        querySelectorAll() { return []; },
        classList:{add(){}, remove(){}, toggle(){}},
        focus(){}, setAttribute(){}, addEventListener(){}, hasAttribute(){return false;}};
}
const elements = new Map();
function byId(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }
byId('import-mode').value = 'keep';
let submitted;
const context = vm.createContext({
    document:{getElementById:byId, querySelector:()=>element(), querySelectorAll:()=>[], addEventListener(){},
        createElement:element, createTextNode:text => ({textContent:text})},
    localStorage:{getItem:()=>null, setItem(){}},
    usingApiKey:()=>Promise.resolve('test-key'),
    fetchAPI:async()=>({result:{metron_api_token:true,gcd_enabled:true}}),
    sendAPI:async (method, path, key, params, body) => {
        submitted = {method,path,key,body};
        return {ok:true,json:async()=>({result:{results:[
            {status:'imported',source:'/library/Book 1.cbz'},
            {status:'failed',source:'/library/Book 2.cbz',reason:'target_exists: /library/Owned.cbz'}
        ]}})};
    },
    MetadataSearchPresentation:{groups:()=>null, safeLink:()=>''},
    showWindow(){}, closeWindow(){}, console
});
vm.runInContext(fs.readFileSync('frontend/static/js/library_import.js','utf8'), context);

function row(path, provider, id, issue) {
    const select = {checked:true}, status = element(), link = element(), issueCell = element();
    const entry = {hidden:false, querySelector(selector) {
        if (selector === '.import-file-select') return select;
        if (selector === '.import-status') return status;
        if (selector === '.issue-column') return issueCell;
        if (selector === 'a') return link;
        return element();
    }};
    return {data:{filepath:path,file_title:path}, element:entry,
        identity:{provider,id}, issueIds:[issue], manual:true, status:'', select, result:status};
}
const first = row('/library/Book 1.cbz','comicvine','2127','301');
const second = row('/library/Book 2.cbz','metron','2127','401');
const finished = (async()=>{
    await new Promise(resolve=>setImmediate(resolve));
    context.first = first; context.second = second;
    vm.runInContext("importRows.set('first',first); importRows.set('second',second);",context);
    await vm.runInContext('submitImport()',context);
    assert.equal(submitted.path,'/libraryimport/submit');
    assert.equal(submitted.method,'POST');
    assert.equal(submitted.body.files[0].provider,'comicvine');
    assert.equal(submitted.body.files[1].provider,'metron');
    assert.deepEqual(JSON.parse(JSON.stringify(submitted.body.files.map(file=>file.issue_ids[0]))),['301','401']);
    assert.equal(submitted.body.files[0].mode,'keep');
    assert.equal(first.result.textContent,'Imported');
    assert.match(second.result.textContent,/Target already exists: \/library\/Owned\.cbz/);
    assert.ok(!fs.readFileSync('frontend/static/js/library_import.js','utf8').includes('/libraryimport/drafts'));
    const css=fs.readFileSync('frontend/static/css/library_import.css','utf8');
    assert.match(css,/overflow-x:\s*auto/);
    assert.match(css,/min-width:\s*62rem/);
    console.log('Direct Library Import choice, per-file result and narrow-table contract passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
module.exports={context,finished,first,second,submitted:()=>submitted};
