from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is unavailable for dashboard request tests")
class DashboardTests(unittest.TestCase):
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
        result = subprocess.run(["node", "-e", script, str(path), str(path.with_name("desktop-stream.js"))],
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
