'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/static/js/volumes.js', 'utf8');
const controls = new Map(), requests = [], rendered = [];
const field = () => ({value:'', textContent:'', disabled:false, checked:false, hidden:false,
    removeAttribute(){}, querySelectorAll(){return [];}, replaceChildren(){}});
const table = {...field(), querySelectorAll(){return [];}};
const context = {
    document:{querySelector(id){if (!controls.has(id)) controls.set(id,field()); return controls.get(id);}},
    library_els:{mass_edit:{progress:field(),button:field(),toggle:field(),select_all:field()},
        pages:{loading:'loading',empty:'empty',view:'view'},
        views:{list:{querySelectorAll(){return [];}},table},
        view_options:{sort:{value:'title'},filter:{value:''}},search:{input:field()}},
    showLibraryPage(page){context.page=page;},
    populateLibrary(rows){rendered.push(...rows);},
    fetchAPI(path,key,params){return new Promise((resolve,reject)=>requests.push({path,key,params,resolve,reject}));},
    setTimeout
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('let libraryOffset'),source.indexOf('function searchLibrary')),context);
const flush = () => new Promise(resolve=>setTimeout(resolve,5));
const waitRequests = async n => {for (let i=0;i<50 && requests.length<n;i++) await flush(); assert.ok(requests.length>=n);};
const volumes = (start,count) => Array.from({length:count},(_,index)=>({id:start+index}));
(async()=>{
    const first = vm.runInContext("fetchLibrary('fixture')",context);
    assert.equal(requests[0].params.limit,100);
    assert.equal(requests[0].params.offset,0);
    assert.equal(context.library_els.mass_edit.button.disabled,true);
    requests[0].resolve({result:volumes(1,100)});
    await waitRequests(2);
    assert.equal(rendered.length,100);
    assert.equal(context.page,'view');
    assert.equal(requests[1].params.offset,100);
    requests[1].resolve({result:volumes(101,75)});
    await first;
    assert.equal(rendered.length,175);
    assert.equal(new Set(rendered.map(row=>row.id)).size,175);
    assert.equal(controls.get('#library-status').textContent,'Showing 175 publications');
    assert.equal(context.library_els.mass_edit.button.disabled,false);
    assert.equal(context.library_els.mass_edit.select_all.checked,false);

    const old = vm.runInContext("fetchLibrary('fixture')",context);
    context.library_els.search.input.value='Batman';
    const newer = vm.runInContext("fetchLibrary('fixture')",context);
    assert.equal(requests[3].params.query,'Batman');
    requests[3].resolve({result:volumes(901,1)}); await newer;
    requests[2].resolve({result:volumes(201,100)}); await old;
    assert.deepEqual(rendered.at(-1),{id:901});
    assert.equal(controls.get('#library-status').textContent,'Showing 1 publications');

    context.library_els.search.input.value='';
    const failed = vm.runInContext("fetchLibrary('fixture')",context);
    requests[4].resolve({result:volumes(1,100)});
    await waitRequests(6);
    requests[5].reject(new Error('controlled')); await failed;
    assert.equal(context.page,'view');
    assert.equal(controls.get('#library-resume').hidden,false);
    assert.match(controls.get('#library-error').textContent,/remaining publications/);
    assert.equal(context.library_els.mass_edit.button.disabled,true);
    const resumed = vm.runInContext("fetchLibrary('fixture',true)",context);
    assert.equal(requests[6].params.offset,100);
    requests[6].resolve({result:volumes(101,75)}); await resumed;
    assert.equal(controls.get('#library-status').textContent,'Showing 175 publications');
    assert.equal(new Set(rendered.slice(-175).map(row=>row.id)).size,175);

    const empty = vm.runInContext("fetchLibrary('fixture')",context);
    requests[7].resolve({result:[]}); await empty;
    assert.equal(context.page,'empty');
    assert.equal(controls.get('#library-status').textContent,'Showing 0 publications');
    context.library_els.view_options.sort.value='recently_added';
    context.library_els.view_options.filter.value='wanted';
    const filtered = vm.runInContext("fetchLibrary('fixture')",context);
    assert.equal(requests[8].params.sort,'recently_added');
    assert.equal(requests[8].params.filter,'wanted');
    requests[8].resolve({result:volumes(42,1)}); await filtered;
    assert.equal(controls.get('#library-status').textContent,'Showing 1 publications');
    context.library_els.view_options.sort.value='title';
    context.library_els.view_options.filter.value='';
    const largeStart = requests.length, renderedStart = rendered.length;
    const large = vm.runInContext("fetchLibrary('fixture')",context);
    for (let index=0;index<25;index++) {
        await waitRequests(largeStart+index+1);
        requests[largeStart+index].resolve({result:volumes(index*100+1,100)});
    }
    await waitRequests(largeStart+26);
    requests[largeStart+25].resolve({result:[]}); await large;
    assert.equal(requests.length-largeStart,26);
    assert.equal(rendered.length-renderedStart,2500);
    assert.equal(new Set(rendered.slice(renderedStart).map(row=>row.id)).size,2500);
    assert.equal(controls.get('#library-status').textContent,'Showing 2500 publications');
    let confirmed='';
    context.window={confirm(message){confirmed=message; return false;}};
    table.querySelectorAll = selector => selector.endsWith(':checked')
        ? [{parentNode:{parentNode:{dataset:{id:'1234'}}}}] : [];
    vm.runInContext(source.slice(source.indexOf('function runAction'),
        source.indexOf('// code run on load')),context);
    vm.runInContext("runAction('fixture','delete',{delete_folder:false})",context);
    assert.match(confirmed,/Delete 1 explicitly selected volumes/);
    assert.match(confirmed,/files and folders will be kept/);
    vm.runInContext("runAction('fixture','delete',{delete_folder:true})",context);
    assert.match(confirmed,/folders and files will also be deleted/);
    console.log('Pullarr Library: continuous 175/2500 volume loads, stale query, partial retry and empty state PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
