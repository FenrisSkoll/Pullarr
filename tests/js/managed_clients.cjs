'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fields = new Map();
const element = () => ({value: '', textContent: '', checked: false, hidden: false, children: [], disabled: false,
    replaceChildren() {this.children = [];}, appendChild(child) {this.children.push(child);},
    querySelectorAll() {return this.children;}});
for (const name of ['name','url','category','kind','priority','enabled','username','password','mode','ratio','seconds',
    'respect','torrent','list','form','status','test','delete','new']) fields.set('#managed-'+name,element());
let submit, reject, calls = [], allow = true;
const value = {id:'fixture',name:'<script>inert</script>',kind:'qbittorrent',url:'http://fixture',enabled:true,
    revision:'r1',category:'pullarr',priority:0,retention:{mode:'both',ratio_target:'1',seed_seconds:60,respect_minimums:true}};
const context = {
    document: {querySelector: id => fields.get(id),createElement: element},
    usingApiKey: () => new Promise(() => {}),
    fetchAPI: async () => ({result:[value]}),
    sendAPI(method,path,key,query,body) {calls.push({method,path,body}); return new Promise((resolve, fail) => {submit=resolve; reject=fail;});},
    confirm: () => allow
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/managed_clients.js','utf8'),context);
const field = name => fields.get('#managed-'+name);
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    context.setupManagedClients('synthetic'); await flush();
    assert.equal(field('torrent').hidden,true);
    assert.equal(field('enabled').checked,false);
    field('list').children[0].onclick();
    assert.equal(field('name').value,value.name);
    assert.equal(field('torrent').hidden,false);
    assert.equal(field('password').value,'');
    assert.equal(field('mode').value,'both');
    field('test').onclick(); field('test').onclick(); assert.equal(calls.length,1);
    assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),{revision:'r1'});
    submit({ok:true,json:async()=>({result:{product:'qBittorrent',version:'5',protocol:'torrent'}})}); await flush();
    assert.match(field('status').textContent,/Connected/);
    field('password').value='synthetic-private';
    field('form').onsubmit({preventDefault(){}});
    assert.equal(calls[1].method,'PUT');
    assert.equal(calls[1].body.revision,'r1');
    reject({ok:false,json:async()=>({error:'ClientFailure',result:{code:'downloader_configuration_changed'}})}); await flush();
    assert.equal(field('password').value,'');
    assert.match(field('status').textContent,/Reload and review/);
    allow=false; field('respect').checked=false;
    field('form').onsubmit({preventDefault(){}}); assert.equal(calls.length,2);
    field('delete').onclick(); assert.equal(calls.length,2);
    for (const code of ['authentication', 'category_missing', 'timeout', 'invalid_response']) {
        field('test').onclick();
        reject({status:400,json:async()=>({error:'ClientFailure',result:{code}})}); await flush();
        assert.match(field('status').textContent,new RegExp(`Action blocked: ${code}\\.`));
    }
    for (const response of [new Error('private transport details'),
        {json:async()=>{throw new Error('private invalid JSON');}},
        {json:async()=>({error:'ClientFailure',result:{code:'private-secret'}})},
        {json:async()=>({error:'arbitrary',result:{code:'authentication'}})}]) {
        field('test').onclick(); reject(response); await flush();
        assert.equal(field('status').textContent,'Client action unavailable. Saved configuration and acquisitions are retained.');
    }
    field('new').onclick(); assert.equal(field('name').value,'');
    assert.equal(field('mode').value,'client_managed'); assert.equal(field('respect').checked,true);
    console.log('Managed clients: type fields, retention defaults, revision, secret clearing, pending lock, safe errors and deliberate destructive confirmation PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
