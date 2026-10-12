// Exercise staged issue selection and confirmation without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node() {
    return {value:'', textContent:'', innerText:'', children:[],
        appendChild(child) { this.children.push(child); return child; },
        replaceChildren() { this.children = []; },
        querySelectorAll() { return []; },
        setAttribute() {}, focus() {}, classList:{toggle(){},add(){},remove(){}}};
}
const elements = new Map();
function element(key) { if (!elements.has(key)) elements.set(key, node()); return elements.get(key); }
const link = node(), cell = node();
const selectedBox = {checked:true}, unselectedBox = {checked:false};
const row = {dataset:{rowid:'0'}, removed:false,
    querySelector(selector) { return selector === 'a' ? link : selector === '.import-file-select' ? selectedBox : cell; },
    remove() { this.removed = true; }};
const other = {dataset:{rowid:'1'}, querySelector(selector) { return selector === '.import-file-select' ? unselectedBox : node(); }};
const list = element('.proposal-list');
list.querySelectorAll = selector => selector === 'tr[data-rowid]' ? [row, other] : [row];
list.querySelector = selector => selector.includes('.issue-column') ? cell : row;
let staged, applied;
let saved;
const context = vm.createContext({
    document:{querySelector:element, getElementById:element, createElement:node, querySelectorAll:()=>[]},
    window:{addEventListener(){}}, setTimeout, clearTimeout,
    localStorage:{setItem(){},getItem(){return null;}},
    usingApiKey:()=>Promise.resolve('fake-app-key'), hide:()=>{},
    LocalOrganizationUI:{panel:()=>node(), result:(_, value)=>value.jobs || []},
    sendAPI:async (method, url, key, params, data) => {
        if (url === '/libraryimport/drafts' || url === '/libraryimport/drafts/draft-1') {
            saved = {method,url,data};
            return {ok:true,json:async()=>({result:{id:'draft-1',revision:
                url === '/libraryimport/drafts' ? 1 : data.revision + 1,state:'draft'}})};
        }
        if (url === '/libraryimport/catalog') return {ok:true, json:async()=>({result:{
            provider:'comicvine', provider_id:'2127', reported_issue_count:1,
            loaded_issue_count:1, completeness:'complete', revision:'catalog-revision',
            issues:[{id:'301',number:'1',title:'Book 1'}],
            proposals:{'/disposable/Example.cbz':{confidence:'strong',issue_ids:['301'],reason:'Unique Book 1'}}}})};
        if (url === '/libraryimport/stage') {
            staged = {method,url,params,data};
            return {ok:true,json:async()=>({result:{id:'draft-1',revision:2,state:'ready',plans:[{source:data[0].filepath,
                target:'/managed/Example.cbz',status:'ready',authority:{provider:'comicvine',id:'2127'},
                issue_labels:['1'],operation:'move',registration:'new'}],
                counts:{ready:1,review_required:0,blocked:0}}})};
        }
        if (url === '/local-organization/draft-1/apply') {
            applied = data;
            return {ok:true,json:async()=>({result:{jobs:[{source:'/disposable/Example.cbz',state:'completed'}]}})};
        }
        throw Error('Unexpected API call: ' + url);
    }, console
});
const source = fs.readFileSync('frontend/static/js/library_import.js', 'utf8');
vm.runInContext(source.split('// code run on load')[0], context);

const finished = (async () => {
    vm.runInContext(`editMatchId=0; rowidToFilepath[0]={filepath:'/disposable/Example.cbz',issueIds:[],manualIssues:false};
        editCVMatch('2127','https://example.invalid/cv','Example',2021,1);`, context);
    await new Promise(resolve=>setTimeout(resolve,0));
    assert.equal(link.href, 'https://example.invalid/cv');
    vm.runInContext(`rowidToFilepath[1]={filepath:'/disposable/Other.cbz',identity:null,issueIds:[]};
        editMetadataMatch({provider:'comicvine',id:'2127'},'https://example.invalid/cv','Example',2021,1,true);`, context);
    assert.equal(vm.runInContext('rowidToFilepath[1].identity', context), null);
    await new Promise(resolve=>setTimeout(resolve,0));
    await vm.runInContext("prepareImport('fake-app-key', false)", context);
    assert.equal(saved.data.rows[0].provider, 'comicvine');
    assert.equal(staged.url, '/libraryimport/stage');
    assert.equal(staged.params.draft_id, 'draft-1');
    assert.equal(staged.data[0].provider, 'comicvine');
    assert.equal(staged.data[0].provider_id, '2127');
    assert.deepEqual(Array.from(staged.data[0].issue_ids), ['301']);
    assert.equal(staged.data[0].catalog_revision, 'catalog-revision');
    await vm.runInContext("importLibrary('fake-app-key', false)", context);
    assert.equal(applied.revision, 2);
    assert.equal(row.removed, false);
    console.log('Library Import catalogue → staged issues → apply contract passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
module.exports = {context, finished, selectedBox, unselectedBox, submitted:() => staged};
