from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is unavailable for dashboard request tests")
class DashboardTests(unittest.TestCase):
    def test_stall_controls_keep_manual_default_preserve_drafts_and_apply_custom_counts(self):
        script = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{value:id==='stallMode'?'manual':id==='stallAttempts'?'3':'',textContent:'',disabled:false});return elements.get(id)};
const context=vm.createContext({document:{getElementById:element}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
let fail=false,saved=0,finish;
const calls=[];
const controls=new context.StallControls(async(path,body)=>{
  calls.push(JSON.parse(JSON.stringify({path,body})));
  if(fail)throw new Error('disk error');
  return new Promise(resolve=>{finish=()=>resolve({stall_attempts:body.stall_attempts})});
},async()=>{saved++});
(async()=>{
  controls.update({stall_attempts:0,task_active:true});
  assert.equal(element('stallMode').value,'manual');
  assert.equal(element('stallAttempts').disabled,true);
  assert.match(element('stallStatus').textContent,/Manual control/);
  element('stallMode').value='auto';element('stallMode').onchange();
  element('stallAttempts').value='7';element('stallAttempts').oninput();
  controls.update({stall_attempts:0,task_active:true});
  assert.equal(element('stallAttempts').value,'7','Polling erased the chosen repeat count');
  const pending=controls.save();
  assert.equal(element('saveStall').disabled,true);
  finish();await pending;
  assert.deepEqual(calls[0],{path:'/api/stall',body:{stall_attempts:7}});
  assert.equal(controls.current,7);
  assert.equal(saved,1);
  for(const value of ['','0','1.5','1001']){
    element('stallAttempts').value=value;element('stallAttempts').oninput();
    await controls.save();
    assert.equal(calls.length,1,'An invalid count reached the server');
  }
  element('stallMode').value='manual';element('stallMode').onchange();
  fail=true;await controls.save();
  assert.equal(controls.current,7,'Failed save changed the active policy');
  assert.equal(element('saveStall').disabled,false,'Failed save could not be retried');
  assert.match(element('stallStatus').textContent,/disk error/);
  fail=false;const manual=controls.save();finish();await manual;
  assert.equal(controls.current,0);
  assert.deepEqual(calls.at(-1),{path:'/api/stall',body:{stall_attempts:0}});
  assert.match(element('stallStatus').textContent,/Resume if the task is already paused/);
  controls.update({stall_attempts:0,model_switching:true});
  assert.equal(element('stallMode').disabled,true);
  controls.update({stall_attempts:20});
  assert.equal(Number(element('stallAttempts').value),20);
  assert.match(element('stallStatus').textContent,/Automatic pause after 20/,'External changes left a stale saved message');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        path = Path(__file__).resolve().parents[1] / "treics_agent/static/stall-controls.js"
        result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_model_selection_preserves_drafts_locks_active_tasks_and_saves_changes(self):
        script = r'''
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const elements=new Map();
function element(id) {
  if(!elements.has(id)) elements.set(id,{value:'',children:[],disabled:false,textContent:'',
    replaceChildren(){this.children=[]},append(item){this.children.push(item)}});
  return elements.get(id);
}
const context=vm.createContext({document:{getElementById:element,createElement:()=>({})}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const calls=[], current='qwen3-vl:30b-a3b-instruct', smaller='qwen3-vl:8b';
let finishSwitch, changes=0;
const selector=new context.ModelSelector(async(path,body)=>{
  calls.push({path,body});
  if(path==='/api/models') return {model:current,models:[current,smaller]};
  return new Promise(resolve=>{finishSwitch=()=>resolve({model:body.model})});
},async()=>{changes++});
const idle={model:current,task_active:false,model_switching:false,current:null};
(async()=>{
  selector.update(idle);
  await selector.refresh();
  assert.deepEqual(element('modelSelect').children.map(option=>option.textContent),[current,smaller]);
  assert.equal(element('useModel').disabled,true);
  element('modelSelect').value=smaller;
  element('modelSelect').onchange();
  selector.update(idle);
  assert.equal(element('modelSelect').value,smaller,'Polling overwrote the staged selection');
  assert.equal(element('useModel').disabled,false);
  for(const status of ['running','pausing','paused','stopping']) {
    selector.update({model:current,current:{status}});
    assert.equal(element('useModel').disabled,true);
    await selector.apply();
  }
  // A completed database row can still have a controller thread finishing cleanup.
  selector.update({...idle,task_active:true,current:{status:'completed'}});
  assert.equal(element('useModel').disabled,true);
  selector.update({...idle,model_switching:true});
  assert.equal(element('start').disabled,true);
  selector.update(idle);
  assert.equal(calls.length,1,'State polling retriggered model discovery or switching');
  const switching=selector.apply();
  assert.equal(element('start').disabled,true,'Start remained enabled while switching');
  assert.equal(element('refreshModels').disabled,true);
  selector.update(idle);
  assert.equal(element('start').disabled,true,'A status poll unlocked an in-flight switch');
  finishSwitch(); await switching;
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1])),{path:'/api/model',body:{model:smaller}});
  assert.equal(selector.current,smaller);
  assert.equal(element('modelSelect').value,smaller);
  assert.equal(element('start').disabled,false);
  assert.equal(element('useModel').disabled,true);
  assert.match(element('modelStatus').textContent,/Saved/);
  assert.equal(changes,1);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        path = Path(__file__).resolve().parents[1] / "treics_agent/static/model-selector.js"
        result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_model_list_and_switch_errors_recover_without_clearing_current_model(self):
        script = r'''
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{value:'',children:[],textContent:'',
  replaceChildren(){this.children=[]},append(option){this.children.push(option)}});return elements.get(id)};
const context=vm.createContext({document:{getElementById:element,createElement:()=>({})}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const current='qwen3-vl:30b-a3b-instruct', other='qwen3-vl:8b';
let failList=true;
const selector=new context.ModelSelector(async(path)=>{
  if(path==='/api/models') {
    if(failList)throw new Error('gateway offline');
    return {model:current,models:[other]};
  }
  throw new Error('model cannot read screenshots');
},async()=>{throw new Error('Unexpected model change')});
(async()=>{
  selector.update({model:current,task_active:false});
  await selector.refresh();
  assert.match(element('modelStatus').textContent,/gateway offline/);
  assert.equal(element('refreshModels').disabled,false);
  assert.equal(element('start').disabled,false);
  assert.equal(selector.current,current);
  failList=false;await selector.refresh();
  assert.equal(element('modelSelect').children.at(-1).disabled,true,'Unavailable model was offered as a choice');
  element('modelSelect').value=other;element('modelSelect').onchange();
  await selector.apply();
  assert.equal(selector.current,current);
  assert.match(element('modelStatus').textContent,/cannot read screenshots/);
  assert.equal(element('start').disabled,false);
  assert.equal(element('useModel').disabled,false,'Failed switch could not be retried');
  selector.api=async()=>({models:[],model:current});
  await selector.refresh();
  assert.equal(element('modelSelect').disabled,true);
  assert.match(element('modelStatus').textContent,/No models available/);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        path = Path(__file__).resolve().parents[1] / "treics_agent/static/model-selector.js"
        result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_live_stream_reconnects_after_disconnect_and_cancels_cleanly(self):
        script = r'''
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
const caption={textContent:''};
let attempts=0, cancelled=0;
const context=vm.createContext({document:{hidden:false,addEventListener(){},getElementById:()=>caption},
  window:{addEventListener(){}}, AbortController, TextDecoder, Blob, Date, URL,
  setTimeout:(fn,ms)=>setTimeout(fn,ms===1500?10:500),clearTimeout});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.fetch=async(path,options)=>{
  attempts++;
  const disconnected=attempts===1;
  return {ok:true,headers:{get:()=> 'multipart/x-mixed-replace; boundary=treic-frame'},
    body:{getReader:()=>({read:()=>disconnected?Promise.resolve({done:true}):new Promise(resolve=>{
      options.signal.addEventListener('abort',()=>resolve({done:true}),{once:true});
    }),cancel:async()=>{cancelled++}})}};
};
const view=new context.DesktopLiveView(()=> 'token',()=>null);
(async()=>{
  view.start();
  for(let i=0;i<30&&attempts<2;i++) await new Promise(resolve=>setTimeout(resolve,5));
  assert.equal(attempts,2,'The dropped stream did not reconnect');
  assert.equal(cancelled,1,'The disconnected reader leaked');
  view.pause();
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(cancelled,2);
  assert.equal(view.retry,null);
  assert.equal(view.controller,null);
})().catch(error=>{console.error(error);process.exitCode=1;view.pause()});
'''
        path = Path(__file__).resolve().parents[1] / "treics_agent/static/desktop-stream.js"
        result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_requests_abort_stalled_headers_and_bodies_and_polling_recovers(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.split('<script>')[1].split('</script>')[0];
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {
    style: {}, children: [], replaceChildren(){}, append(){}, querySelector(){return null},
  });
  return elements.get(id);
};
const context = vm.createContext({
  document: {getElementById: element, createElement: () => ({append(){}}), addEventListener(){}, hidden: false},
  window: {addEventListener(){}},
  sessionStorage: {getItem: () => '', setItem(){}, removeItem(){}},
  setInterval(){}, setTimeout: fn => setTimeout(fn, 20), clearTimeout,
  AbortController, Date, URL, TextDecoder, Blob,
});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), context);
vm.runInContext(fs.readFileSync(process.argv[4], 'utf8'), context);
vm.runInContext(source, context);
function stall(signal) {
  return new Promise((resolve, reject) => signal.addEventListener('abort', () => {
    const error = new Error('aborted'); error.name = 'AbortError'; reject(error);
  }, {once: true}));
}
(async () => {
  context.fetch = async (path, options) => stall(options.signal);
  await assert.rejects(() => context.api('/api/state'), /Dashboard request timed out/);
  context.fetch = async (path, options) => ({ok: true, json: () => stall(options.signal)});
  await assert.rejects(() => context.api('/api/state'), /Dashboard request timed out/);
  context.fetch = async (path, options) => ({ok: true, blob: () => stall(options.signal)});
  await assert.rejects(() => context.api('/api/screen', undefined, 'blob'), /Dashboard request timed out/);
  // A hung poll must release its polling flag so a later healthy poll can run.
  vm.runInContext('token="test-token"', context);
  context.fetch = async (path, options) => stall(options.signal);
  await context.poll();
  assert.match(element('error').textContent, /Dashboard request timed out/);
  const paths = [];
  context.fetch = async path => {
    paths.push(path);
    if (path === '/api/screen') return {ok: false, status: 404, json: async () => ({error: 'No capture'})};
    return {ok: true, json: async () => ({runs: [], current: null, phase: 'idle',
      model: 'test', workspace: '/tmp/work', last_model_seconds: null})};
  };
  await context.poll();
  assert.ok(paths.includes('/api/state'), 'Polling stayed stuck after a failed request');
  assert.equal(element('phase').textContent, 'idle');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        path = Path(__file__).resolve().parents[1] / "treics_agent/static/index.html"
        result = subprocess.run(["node", "-e", script, str(path), str(path.with_name("desktop-stream.js")),
                                 str(path.with_name("model-selector.js")), str(path.with_name("stall-controls.js"))],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stream_parser_fragmentation_rendering_authentication_and_pause(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map(), drawn = [], listeners = {};
const element = id => {
  if (!elements.has(id)) elements.set(id, {hidden: true, width: 0, height: 0, textContent: '',
    getContext: () => ({drawImage: () => drawn.push('image'), beginPath(){}, arc(){}, moveTo(){}, lineTo(){}, stroke(){drawn.push('marker')}})});
  return elements.get(id);
};
let closed = 0, fetches = [], resolveRead;
const context = vm.createContext({
  document: {getElementById: element, hidden: false, addEventListener: (event, fn) => {listeners[event]=fn}},
  window: {addEventListener(){}}, Uint8Array, TextDecoder, Blob, AbortController, Date, URL,
  setTimeout, clearTimeout,
  createImageBitmap: async () => ({width: 320, height: 240, close(){closed++}}),
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const encode = s => new TextEncoder().encode(s);
const part = (type, body, extra='') => encode('--treic-frame\r\nContent-Type: '+type+'\r\nContent-Length: '+body.length+'\r\n'+extra+'\r\n'+body+'\r\n');
const jpeg = part('image/jpeg', 'jpeg-data', 'X-Desktop-Width: 800\r\nX-Desktop-Height: 600\r\nX-Captured-At: 1000\r\nX-Frame-Rate: 5\r\n');
const status = part('application/json', '{"message":"waiting"}');
const wire = new Uint8Array(jpeg.length+status.length); wire.set(jpeg); wire.set(status, jpeg.length);
const parser = new context.DesktopFrameParser(), decoded=[];
for (let i=0;i<wire.length;i+=3) decoded.push(...parser.push(wire.slice(i,i+3)));
assert.equal(decoded.length, 2);
assert.equal(new TextDecoder().decode(decoded[0].bytes), 'jpeg-data');
assert.equal(decoded[1].headers['content-type'], 'application/json');
const bad = encode('--treic-frame\r\nContent-Type: image/jpeg\r\nContent-Length: 99999999\r\n\r\n');
assert.throws(() => new context.DesktopFrameParser().push(bad), /frame length/);
const view = new context.DesktopLiveView(() => 'private-token', () => ({last_click: {x: 400, y: 300}, click_marker_error: 'unavailable'}));
context.fetch = async (path, options) => {
  fetches.push({path, options});
  let first=true;
  return {ok: true, headers: {get: () => 'multipart/x-mixed-replace; boundary=treic-frame'},
    body: {getReader: () => ({read: () => {
      if (first) {first=false; return Promise.resolve({done:false, value:jpeg})}
      return new Promise(resolve => {resolveRead=resolve; options.signal.addEventListener('abort',()=>resolve({done:true}),{once:true})});
    }, cancel: async () => {}})}};
};
(async () => {
  view.start();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(fetches[0].path, '/api/desktop/stream');
  assert.equal(fetches[0].options.headers.Authorization, 'Bearer private-token');
  assert.ok(!fetches[0].path.includes('private-token'), 'Token was placed in the URL');
  assert.equal(element('liveScreen').hidden, false);
  assert.equal(element('screen').hidden, true);
  assert.match(element('screenTime').textContent, /Live/);
  assert.ok(drawn.includes('image') && drawn.includes('marker'));
  assert.equal(closed, 1, 'Decoded bitmap leaked');
  view.pause();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.ok(fetches[0].options.signal.aborted);
  assert.equal(view.retry, null);
  assert.equal(view.controller, null);
  assert.match(element('screenTime').textContent, /task continues/);
  view.resume(); await new Promise(resolve => setTimeout(resolve, 10));
  context.document.hidden=true; listeners.visibilitychange();
  assert.ok(fetches[1].options.signal.aborted);
  context.document.hidden=false; listeners.visibilitychange();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(fetches.length, 3);
  view.pause();
})().catch(error => { console.error(error); process.exitCode=1; view.pause(); });
'''
        path = Path(__file__).resolve().parents[1] / "treics_agent/static/desktop-stream.js"
        result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
