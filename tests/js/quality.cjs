const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
 constructor(tag='div'){this.tag=tag;this.children=[];this.nodes=new Map();this.value='';}
 appendChild(e){this.children.push(e);return e;} replaceChildren(){this.children=[];}
 querySelector(k){if(!this.nodes.has(k))this.nodes.set(k,new Element());return this.nodes.get(k);}
 setAttribute(k,v){this[k]=v;} showModal(){this.open=true;} close(){this.open=false;} focus(){this.focused=true;}
 set innerHTML(_){throw Error('Unsafe HTML');}
}
const context=vm.createContext({module:{exports:{}},document:{createElement:t=>new Element(t)},console,setTimeout,structuredClone});
vm.runInContext(fs.readFileSync('frontend/static/js/quality.js','utf8'),context);
const {Controller,text}=context.module.exports,all=e=>[e,...e.children.flatMap(all)],find=(e,t)=>all(e).find(n=>n.tag==='button'&&n.textContent===t);
const profile={id:1,name:'<script>hostile</script>',revision:3,is_default:true,groups:[{name:'Low',classes:['unknown'],allowed:true},{name:'High',classes:['digital'],allowed:true}],cutoff:1,upgrades:false,minimum_p10:0};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
(async()=>{
 const calls=[],ui=new Controller(new Element(),async(...args)=>{calls.push(args);return {items:[profile],default:{profile_id:1,revision:0}};});
 await ui.list();assert.ok(all(ui.el('content')).some(e=>String(e.textContent).includes(profile.name)));
 ui.edit(profile);assert.ok(find(ui.el('dialog-body'),'Higher Low'));await find(ui.el('dialog-body'),'Higher Low').onclick();
 await find(ui.el('dialog-body'),'Save Quality Profile').onclick();const saved=calls.find(c=>c[0]==='POST');assert.equal(saved[2].revision,3);assert.equal(saved[2].policy.groups[1].name,'Low');assert.equal(saved[2].policy.cutoff,0);
 const pending=deferred();let count=0;const work=()=>{count++;return pending.promise;};const a=ui.perform(work,true),b=ui.perform(work,true);assert.equal(count,1);pending.resolve();await a;await b;
 const old=deferred(),fresh=deferred();let n=0;ui.api=()=>++n===1?old.promise:fresh.promise;const p1=ui.list(),p2=ui.list();fresh.resolve({items:[{...profile,name:'fresh'}]});await p2;old.resolve({items:[{...profile,name:'obsolete'}]});await p1;assert.ok(!all(ui.el('content')).some(e=>String(e.textContent).includes('obsolete')));
 const node=new Element();text(node,'p','<img onerror=alert(1)>');assert.equal(node.children[0].textContent,'<img onerror=alert(1)>');
 const source=fs.readFileSync('frontend/static/js/quality.js','utf8');assert.ok(!source.includes('innerHTML'));for(const term of ['Profile conflict','Analyze Quality','expected_profile_id','Previous File Acquisition','dialogGeneration','cutoff','minimum_p10'])assert.ok(source.includes(term));
 console.log('Quality JS: profile revision/group order/cutoff, pending lock, stale suppression, hostile text, inheritance and provenance controls passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
