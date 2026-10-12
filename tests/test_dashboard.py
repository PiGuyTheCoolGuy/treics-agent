from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node is unavailable for dashboard request tests")
class DashboardTests(unittest.TestCase):
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
  document: {getElementById: element, createElement: () => ({append(){}})},
  sessionStorage: {getItem: () => '', setItem(){}, removeItem(){}},
  setInterval(){}, setTimeout: fn => setTimeout(fn, 20), clearTimeout,
  AbortController, Date, URL,
});
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
        result = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
