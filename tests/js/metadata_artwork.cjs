/* Visible-only bounded batches and stale-result suppression without a browser. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let notify, disconnected = false, current = true;
const requests = [], replies = [];
const context = vm.createContext({URL,
    IntersectionObserver: class {
        constructor(callback) { notify = callback; }
        observe() {} unobserve() {} disconnect() { disconnected = true; }
    },
    sendAPI: (method, path, key, params, body) => {
        requests.push(body);
        return new Promise(resolve => replies.push(() => resolve({json:async()=>({result:body.identities.map(id=>
            ({result_key:id,artwork_state:'available',image:'data:image/jpeg;base64,/9j/'}))})})));
    },
    current: () => current
});
vm.runInContext(fs.readFileSync('frontend/static/js/metadata_search.js','utf8'), context);
const service = vm.runInContext("MetadataSearchPresentation.artwork('fixture','key',current)", context);
const entries = Array.from({length:250},(_,i)=>({dataset:{},image:{src:'placeholder'},querySelector(){return this.image;}}));
entries.forEach((entry,i)=>service.observe(entry,{artwork_state:'pending',result_key:'gcd:'+(i+1)}));
const tick = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
    assert.equal(requests.length,0);
    notify(entries.map(target=>({target,isIntersecting:false})));
    assert.equal(requests.length,0);
    notify(entries.map(target=>({target,isIntersecting:true})));
    assert.equal(requests.length,1);
    assert.equal(requests[0].identities.length,4);
    replies.shift()(); await tick();
    assert.equal(entries[0].image.src,'data:image/jpeg;base64,/9j/');
    replies.shift()(); await tick();
    replies.shift()(); await tick();
    assert.equal(requests.length,3);
    assert.equal(requests.reduce((n,r)=>n+r.identities.length,0),12);
    assert.equal(entries[12].image.src,'placeholder');
    service.stop(); assert.ok(disconnected);
    const stale = vm.runInContext("MetadataSearchPresentation.artwork('fixture','key',current)", context);
    const entry={dataset:{},image:{src:'placeholder'},querySelector(){return this.image;}};
    stale.observe(entry,{artwork_state:'pending',result_key:'metron:7'});
    notify([{target:entry,isIntersecting:true}]); current=false;
    replies.shift()(); await tick();
    assert.equal(entry.image.src,'placeholder');
    context.result={search_origin:'relation', relations:[{relation_type:'continues_as',target_title:'<script>hostile</script>'}]};
    const label=vm.runInContext('MetadataSearchPresentation.relationships(result)',context);
    assert.match(label,/Related continuation · explicit ComicVine relationship/);
    assert.match(label,/Continues as → <script>hostile<\/script>/); // Caller uses textContent.
    context.result={relations:[{relation_type:'related_series',target_title:'Metron association'}]};
    assert.equal(vm.runInContext('MetadataSearchPresentation.relationships(result)',context),'Related series → Metron association');
    console.log('Metadata artwork: 250 cards, visible-only 4x3 cap, placeholders, stale suppression, relationship labels PASS');
})().catch(error=>{console.error(error); process.exitCode=1;});
