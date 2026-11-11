import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Requires Node for the browser logic contract")
class ReviewFrontendTests(unittest.TestCase):
    def test_only_a_confirmed_click_posts_the_reviewed_revision_and_unknown_results_are_truthful(self):
        html = (Path(__file__).resolve().parents[1] / "desktop/review.html").read_text()
        script = re.search(r'<script nonce="[^"]+">(.*?)</script>', html, re.S)[1]
        script = script.replace("__SESSION_JSON__", '"synthetic-session"').replace("__BOOTSTRAP_JSON__", '{"project":"/fixture"}')
        harness = r'''
const vm = require('vm'), assert = require('assert');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.disabled = false; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
}
const ids = new Map();
function flatten(item) { return [item, ...item.children.flatMap(flatten)]; }
const document = {
  getElementById(id) { if (!ids.has(id)) ids.set(id, new Element('div')); return ids.get(id); },
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return [...ids.values()].flatMap(flatten).filter(item => item.tag === 'button'); }
};
let allow = false;
const calls = [];
const view = {project:'/fixture', counts:{pending:1}, review_revision:'b'.repeat(64), decisions:[], proposals:[{
  id:'11111111', revision:'a'.repeat(64), state:'pending', reason:'fixture', can_approve:true, can_reject:true,
  operations:[{action:'patch', before:'old fixture rule', after:'new fixture rule'}]
}]};
const context = {document, confirm:() => allow, fetch:async (path, options) => {
  calls.push({path, ...options});
  return {ok:true, json:async () => options.method === 'POST' ? {state:'not_found', applied:false} : view};
}};
vm.runInNewContext(JSON.parse(require('fs').readFileSync(0, 'utf8')), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await tick();
  assert(calls.every(call => call.method !== 'POST'));
  const approve = document.querySelectorAll().find(item => item.textContent === '批准此修改');
  assert(approve && !approve.disabled);
  approve.onclick(); await tick();
  assert(calls.every(call => call.method !== 'POST'));
  allow = true; approve.onclick(); await tick(); await tick();
  const posts = calls.filter(call => call.method === 'POST');
  assert.strictEqual(posts.length, 1);
  assert.deepStrictEqual(JSON.parse(posts[0].body), {action:'approve', id:'11111111', revision:'a'.repeat(64)});
  assert(!document.getElementById('message').textContent.includes('已写入'));
  process.stdout.write(JSON.stringify({readonly:true, confirmation_required:true, exact_revision:true, truthful:true}));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
'''
        result = subprocess.run([shutil.which("node"), "-e", harness], input=json.dumps(script),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(json.loads(result.stdout).values()))
