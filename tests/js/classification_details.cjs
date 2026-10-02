const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
class Node {
    constructor() { this.children = []; this.handlers = {}; }
    append(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
    addEventListener(name, fn) { this.handlers[name] = fn; }
    set innerHTML(_) { throw Error('Provider HTML forbidden'); }
}
const panel = new Node(), body = new Node(), button = new Node();
const context = {document:{createElement: () => new Node(), querySelector: selector => ({
    '#classification-details':panel, '#classification-details-body':body, '#classification-evaluate':button
})[selector]}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/static/js/classification_details.js','utf8') + '\nglobalThis.subject=ClassificationDetails;', context);
const data = {stored:{value:'hard-cover',locked:true}, provenance:{status:'unavailable'}, last_control_action:null,current_evaluation:null};
context.subject.render(body,data);
assert.ok(body.children.some(n=>n.textContent.includes('Reason not recorded')));
const hostile = '<img src=x onerror=alert(1)>雪';
data.provenance = {status:'recorded',application_kind:'automatic_decision',recorded_at:'utc',source:'provider_physical_format',
    reason:'sole_issue_physical_evidence',policy_id:'kapowarr-special-version/v1',evaluated_at:'local',input_scope:'decision_time_inputs',
    replay_status:'explanation_complete_replay_incomplete',evidence:[{axis:'physical',availability:'available_with_value',disposition:'accepted',
    provider:'metron',provider_id:'8',source_field:'series_type.name',raw_value:hostile,normalized_value:'hardcover'}]};
data.current_evaluation = {status:'evaluated',value:'tpb',input_scope:'durable_fields_only',reason:'aged_single_issue_tpb',evaluated_at:'later'};
context.subject.render(body,data);
assert.ok(body.children.some(n=>n.textContent.includes(hostile)));
assert.ok(body.children.some(n=>n.textContent==='Stored classification: hard-cover'));
assert.ok(body.children.some(n=>n.textContent==='Would classify now (not applied): tpb'));
(async () => {
    const requests = [];
    context.fetchAPI = async (path, key, params) => { requests.push({path,key,params}); return {result:data}; };
    context.subject.setup(7,'fixture-key');
    assert.equal(requests.length,0);
    panel.open = true; panel.handlers.toggle();
    await new Promise(resolve=>setImmediate(resolve));
    button.handlers.click();
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(requests.length,2);
    assert.equal(requests[0].path,'/volumes/7/classification');
    assert.equal(requests[0].key,'fixture-key');
    assert.equal(requests[1].params.evaluate,'true');
    assert.ok(requests.every(r=>!r.path.includes('refresh')));
    console.log('Classification details: lazy read-only API, separate history/current, hostile text PASS');
})();
