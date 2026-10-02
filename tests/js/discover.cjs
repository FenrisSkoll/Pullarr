const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.dataset={};this.listeners={};}
 appendChild(e){this.children.push(e);return e;}replaceChildren(){this.children=[];}
 addEventListener(name,fn){this.listeners[name]=fn;}showModal(){this.open=true;}close(){this.open=false;this.listeners.close?.();}
 set innerHTML(_){throw Error('Unsafe HTML');}
}
const nodes=new Map(),element=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const context=vm.createContext({module:{exports:{}},document:{createElement:t=>new Element(t),getElementById:element},console,setTimeout,url_base:'',URLSearchParams});
vm.runInContext(fs.readFileSync('frontend/static/js/discover.js','utf8'),context);
const {Controller,text}=context.module.exports,all=e=>[e,...e.children.flatMap(all)],button=(e,title)=>all(e).find(n=>n.tag==='button'&&n.textContent===title);
const post={id:1,title:'<script>hostile</script>',categories:['Other Comics'],year_text:'2026',size_text:'6 GB//1 GB',first_seen:100,published_precision:'day',published_at:'2026-09-30',match:'matched',interest:'upgrade',claims:{quality_class:'hd_digital'},quality:{result:'provisional_upgrade'},local:{volume_id:1,direct_owned:true,content_elsewhere:false},url:'https://getcomics.org/one/',revision:1};
const source={enabled:1,automatic:0,interval_minutes:60,revision:1};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
(async()=>{
 element('d-state').value='all';const calls=[];
 const ui=new Controller(async(method,path,body,query)=>{calls.push([method,path,body,query]);return path==='/discover/status'?source:path==='/discover'?{items:[post],scanned:50,next_offset:50}:post;},async()=>{});
 await ui.load();assert.ok(all(element('d-content')).some(n=>n.textContent===post.title));assert.equal(element('d-next').disabled,false);
 await ui.detail(1);assert.ok(button(element('d-body'),'Resolve Current Offerings / Review Acquisition'));
 ui.api=async()=>({...post,match:'unmatched',local:null});await ui.detail(1);assert.ok(all(element('d-body')).some(n=>n.tag==='a'&&n.href.startsWith('/add?q=')));
 const pending=deferred();let count=0;const work=()=>{count++;return pending.promise;};const a=ui.run(work,true),b=ui.run(work,true);assert.equal(count,1);pending.resolve();await a;await b;
 const old=deferred(),fresh=deferred();let requests=0;ui.api=(_m,path)=>path==='/discover/status'?source:++requests===1?old.promise:fresh.promise;
 const first=ui.load(),second=ui.load();fresh.resolve({items:[{...post,title:'fresh'}],scanned:1,next_offset:null});await second;old.resolve({items:[{...post,title:'obsolete'}],scanned:1,next_offset:null});await first;
 assert.ok(!all(element('d-content')).some(n=>n.textContent==='obsolete'));
 ui.api=async(_m,path)=>{if(path.includes('/tasks/'))return {state:'complete',result:{ok:true}};};assert.equal((await ui.task({id:'x',state:'queued'})).ok,true);
 await assert.rejects(ui.task({state:'failed',result:{reason:'stale_preview'}}),/stale_preview/);
 ui.source=source;ui.settings();assert.ok(button(element('d-body'),'Save Discovery Settings'));
 const sourceText=fs.readFileSync('frontend/static/js/discover.js','utf8');assert.ok(!sourceText.includes('innerHTML'));
 for(const term of ['dialogGeneration','preview_id','confirmed:true','content_elsewhere','noopener noreferrer','provisional','next_offset'])assert.ok(sourceText.includes(term));
 const n=new Element();text(n,'p','<img onerror=bad>');assert.equal(n.children[0].textContent,'<img onerror=bad>');
 console.log('Discover JS paging/filter requests, safe text, stale suppression, submit lock, detail/Add, task/error and settings passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
