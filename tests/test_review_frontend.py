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
  constructor(tag) { this.tag = tag; this.children = []; this.disabled = false; this.attributes = {}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
}
const ids = new Map();
function flatten(item) { return [item, ...item.children.flatMap(flatten)]; }
const documentEvents = new Map();
const windowEvents = new Map();
const timers = new Map();
let timerId = 0;
const document = {
  hidden: false,
  addEventListener(name, callback) { documentEvents.set(name, callback); },
  getElementById(id) { if (!ids.has(id)) ids.set(id, new Element('div')); return ids.get(id); },
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return [...ids.values()].flatMap(flatten).filter(item => item.tag === 'button'); }
};
for (const id of ['refresh','cleanup','selftest','close']) ids.set(id, new Element('button'));
let allow = false;
let releaseApproval;
let approvalGate;
const calls = [];
const view = {project:'/fixture', counts:{pending:1}, review_revision:'b'.repeat(64), decisions:[], proposals:[{
  id:'11111111', revision:'a'.repeat(64), state:'pending', reason:'fixture', can_approve:true, can_reject:true,
  operations:[{action:'patch', before:'old fixture rule', after:'new fixture rule'}]
}]};
const sharing = {candidates:[{name:'portable', version:'version-1', revision:'c'.repeat(64), sources:['/fixture'],
  previous_version:'version-0', resources:[{path:'SKILL.md',change:'changed'}, {path:'references/example.md',change:'added'}]}], decisions:[]};
const context = {document, URLSearchParams, confirm:() => allow, window: {
  addEventListener(name, callback) { windowEvents.set(name, callback); }
}, setTimeout(callback, delay) {
  assert.strictEqual(delay, 8000); timers.set(++timerId, callback); return timerId;
}, clearTimeout(id) { timers.delete(id); }, fetch:async (path, options) => {
  calls.push({path, ...options});
  assert.strictEqual(options.cache, 'no-store');
  const route = path.split('?')[0];
  const action = options.method === 'POST' && JSON.parse(options.body).action;
  if (action === 'approve_generic') await approvalGate;
  const result = options.method === 'POST' ? {state:action === 'approve_generic' ? 'queued' : 'not_found'}
    : route === '/api/publications' ? sharing : route === '/api/publication-file' ? {before:'old body',after:'<script>new body</script>'} : view;
  return {ok:true, json:async () => result};
}};
vm.runInNewContext(JSON.parse(require('fs').readFileSync(0, 'utf8')), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await tick();
  assert.deepStrictEqual(calls.map(call => call.path), ['/api/proposals','/api/publications']);
  assert(document.getElementById('refresh-status').textContent.includes('最近更新'));
  assert(calls.every(call => call.method !== 'POST'));
  const approve = document.querySelectorAll().find(item => item.textContent === '批准此修改');
  assert(approve && !approve.disabled);
  approve.onclick(); await tick();
  assert(calls.every(call => call.method !== 'POST'));
  assert(document.getElementById('message').textContent.includes('已取消'));
  allow = true; approve.onclick(); await tick(); await tick();
  const posts = calls.filter(call => call.method === 'POST');
  assert.strictEqual(posts.length, 1);
  assert.deepStrictEqual(JSON.parse(posts[0].body), {action:'approve', id:'11111111', revision:'a'.repeat(64)});
  assert(!document.getElementById('message').textContent.includes('已写入'));
  const publication = flatten(document.getElementById('publications'));
  const skill = publication.find(item => item.tag === 'details');
  assert(skill && skill.open);
  await skill.ontoggle();
  assert(calls.some(call => call.path.includes('/api/publication-file?name=portable&revision=' + 'c'.repeat(64) + '&file=SKILL.md')));
  assert(flatten(skill).some(item => item.tag === 'pre' && item.textContent === '<script>new body</script>'));
  const share = publication.find(item => item.textContent === '批准跨项目共享');
  const keep = publication.find(item => item.textContent === '保留在本项目');
  allow = false; share.onclick(); keep.onclick(); await tick();
  assert.strictEqual(calls.filter(call => call.method === 'POST').length, 1);
  document.hidden = true; documentEvents.get('visibilitychange')();
  assert.strictEqual(timers.size, 0);
  sharing.candidates.push({...sharing.candidates[0], name:'another', revision:'d'.repeat(64)});
  document.hidden = false; documentEvents.get('visibilitychange')(); windowEvents.get('focus')();
  await tick();
  assert.strictEqual(flatten(document.getElementById('publications')).filter(item => item.tag === 'article').length, 2);
  assert(flatten(document.getElementById('publications')).includes(skill) && skill.open);
  assert(flatten(skill).some(item => item.tag === 'pre' && item.textContent.includes('new body')));
  const refresh = document.getElementById('refresh');
  refresh.onclick();
  assert(refresh.disabled && refresh.textContent === '正在刷新…');
  await tick();
  assert(!refresh.disabled && document.getElementById('message').textContent.includes('已刷新'));
  approvalGate = new Promise(resolve => { releaseApproval = resolve; });
  allow = true; share.onclick();
  assert(share.disabled && share.textContent === '正在提交批准…' && share.attributes['aria-busy'] === 'true');
  assert(timers.size === 0);
  releaseApproval(); await tick(); await tick();
  const publicationPosts = calls.filter(call => call.method === 'POST');
  assert.strictEqual(publicationPosts.length, 2);
  assert.deepStrictEqual(JSON.parse(publicationPosts[1].body), {action:'approve_generic', name:'portable', revision:'c'.repeat(64)});
  assert(document.getElementById('message').textContent.includes('等待后台'));
  sharing.candidates.shift();
  sharing.decisions.push({name:'portable', version:'version-1', state:'published'});
  const [[id, auto]] = [...timers]; timers.delete(id); auto(); await tick();
  assert(calls.some(call => call.path === '/api/publications?poll=1'));
  assert(document.getElementById('message').textContent.includes('已共享'));
  assert(flatten(document.getElementById('publication-history')).some(item => item.textContent?.includes('已共享')));
  const close = document.getElementById('close');
  close.onclick({currentTarget: close});
  assert(close.disabled && close.textContent === '正在关闭…');
  await tick();
  assert(timers.size === 0 && document.getElementById('message').textContent.includes('会话已关闭'));
  process.stdout.write(JSON.stringify({readonly:true, confirmation_required:true, exact_revision:true, truthful:true}));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
'''
        result = subprocess.run([shutil.which("node"), "-e", harness], input=json.dumps(script),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(json.loads(result.stdout).values()))
