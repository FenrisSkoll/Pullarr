const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
class Node {
    constructor(tag='div') { this.tag=tag; this.children=[]; this.nodes=new Map(); this.dataset={}; this.value=''; this.style={}; this.classList={contains:()=>true}; }
    appendChild(child) { this.children.push(child); }
    append(child) { this.appendChild(child); }
    querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector,new Node()); return this.nodes.get(selector); }
    querySelectorAll(selector) { return selector.includes('entry-description') ? [this.querySelector('description')] : []; }
    cloneNode() { return new Node('button'); }
    blur() {}
    set innerHTML(value) { assert.ok(value === '' || value === undefined, 'Untrusted HTML interpretation'); }
}
function harness(script) {
    const root=new Node(), requests=[], submissions=[];
    const context=vm.createContext({document:{querySelector:s=>root.querySelector(s), getElementById:s=>root.querySelector('#'+s),
        createElement:tag=>new Node(tag), querySelectorAll:()=>[]}, URL, URLSearchParams, console,
        window:{location:{search:''}}, url_base:'', hide:()=>{}, usingApiKey:()=>Promise.resolve('fixture-key'),
        showLoadWindow:()=>{}, setLocalStorage:()=>{}, closeWindow:()=>{}, showWindow:()=>{},
        fetchAPI:async (path,key,params)=> { requests.push({path,key,params}); return {result:context.data}; },
        sendAPI:async (method,path,key,params,data)=> {
            submissions.push({method,path,params,data});
            return {json:async()=>({result:{id:8}})};
        }
    });
    vm.runInContext(fs.readFileSync('frontend/static/js/metadata_search.js','utf8'),context);
    vm.runInContext(fs.readFileSync('frontend/static/js/'+script,'utf8').split('// code run on load')[0],context);
    return {context,root,requests,submissions};
}
const hostile='<img onerror=alert(1)> "雪"';
const result=provider=>({metadata_source:{provider,id:'7'}, comicvine_id:provider==='comicvine'?7:null,
    title:hostile,year:2016,volume_number:1,cover_link:null,description:hostile,site_url:'javascript:alert(1)',
    aliases:[hostile],publisher:hostile,issue_count:3,already_added:null,translated:false,
    local_identity_annotations:[{kind:'local_persisted_cross_reference',volume_id:9,selected_provider:'metron',provenance:hostile}],identity_conflict:true});
const data={schema:'metadata-search/v2',status:'partial',providers:[
    {provider:'comicvine',label:'ComicVine',status:'complete',result_count:1,results:[result('comicvine')]},
    {provider:'metron',label:'Metron',status:'rate_limited',reason:'rate_limited',result_count:0,results:[]},
    {provider:'gcd',label:'GCD',status:'complete',result_count:1,results:[result('gcd')]}]};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
    const add=harness('add_volume.js'); add.context.data=data;
    vm.runInContext(`processURLFilters=()=>{}; applyFilters=()=>{}; emptyParams=()=>{};
        SearchEls.provider.value='all'; SearchEls.search_bar.input.value='Literal & title'; search();`,add.context);
    await tick();
    assert.equal(add.requests[0].params.provider,'all');
    const entries=add.root.querySelector('#search-results').children;
    assert.equal(entries.length,5);
    assert.match(entries[2].textContent,/Metron.*Rate limited/);
    assert.equal(entries[1].dataset.provider,'comicvine');
    assert.equal(entries[4].dataset.provider,'gcd');
    assert.equal(entries[4].querySelector('description').textContent,hostile);
    assert.equal(entries[4].querySelector('h2').innerText,hostile);
    for (const entry of [entries[1],entries[4]]) {
        add.context.chosen=entry;
        vm.runInContext('SearchEls.window.selectedEntry=chosen; addVolume();',add.context);
        await tick();
        const sent=add.submissions.at(-1).data;
        assert.equal(sent.provider,entry.dataset.provider);
        assert.equal(sent.provider_id,'7');
        assert.ok(!('comicvine_id' in sent));
    }
    const imp=harness('library_import.js'); imp.context.data=data;
    vm.runInContext(`liEls.search.provider.value='all'; liEls.search.input.value='Literal';
        rowidToFilepath[0]={filepath:'/fixture.cbz'}; editMatchId=0; searchMetadata();`,imp.context);
    await tick();
    assert.equal(imp.requests[0].params.provider,'all');
    const rows=imp.root.querySelector('.search-results').children;
    assert.equal(rows.length,5);
    assert.match(rows[2].children[0].textContent,/Rate limited/);
    assert.equal(vm.runInContext('rowidToFilepath[0].identity',imp.context),undefined);
    rows[4].querySelector('td:nth-child(2) button').onclick();
    assert.equal(vm.runInContext('rowidToFilepath[0].identity.provider',imp.context),'gcd');
    assert.equal(vm.runInContext('rowidToFilepath[0].identity.id',imp.context),'7');
    assert.equal(vm.runInContext("MetadataSearchPresentation.safeLink('https://user:secret@example.invalid/')",imp.context),'');
    console.log('Aggregate search: independent groups/status, hostile text, exact Add/Import selection, no auto-match PASS');
})();
