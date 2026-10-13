const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let observed;
class Observer {
    constructor(callback) { this.callback=callback; this.targets=[]; observed=this; }
    observe(node) { this.targets.push(node); }
    unobserve() {}
    disconnect() { this.targets=[]; }
    emit() { this.callback(this.targets.map(target=>({target,isIntersecting:true}))); }
}
const requests=[];
const context=vm.createContext({document:{createElement:()=>({})}, URL,
    IntersectionObserver:Observer,
    sendAPI:async (method,path,key,params,body)=> {
        requests.push(body.identities);
        return {json:async()=>({result:body.identities.map(result_key=>({result_key,
            artwork_state:'available',image:'data:image/jpeg;base64,/9j/'}))})};
    }});
vm.runInContext(fs.readFileSync('frontend/static/js/metadata_search.js','utf8'),context);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function row() {
    const image={src:''};
    return {dataset:{},image,querySelector:()=>image};
}

(async()=>{
    const safe=vm.runInContext('MetadataSearchPresentation.safeLink',context);
    assert.equal(safe('javascript:alert(1)'),'');
    assert.equal(safe('data:image/jpeg;base64,/9j/'),'');
    assert.equal(safe('https://example.test/cover.jpg'),'https://example.test/cover.jpg');
    const artwork=vm.runInContext('MetadataSearchPresentation.artwork',context);
    const controller=artwork('ticket','key',()=>true);
    const rows=Array.from({length:15},()=>row());
    rows.forEach((item,index)=>controller.observe(item,{artwork_state:'pending',result_key:`comicvine:${index}`}));
    observed.emit();
    for (let index=0; index<8; index++) await tick();
    assert.deepEqual(requests.map(batch=>batch.length),[4,4,4]);
    assert.equal(rows.filter(item=>item.image.src).length,12);
    controller.stop();

    let release;
    context.sendAPI=()=>new Promise(resolve=>{release=resolve;});
    const late=artwork('other-ticket','key',()=>true), stale=row();
    late.observe(stale,{artwork_state:'pending',result_key:'metron:7'});
    observed.emit();
    late.stop();
    release({json:async()=>({result:[{result_key:'metron:7',artwork_state:'available',
        image:'data:image/jpeg;base64,/9j/'}]})});
    await tick();
    assert.equal(stale.image.src,'');
    console.log('Artwork safe links, visible capped batches and stale-result guard PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
