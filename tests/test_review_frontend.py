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
function text(item) { return (item.textContent || '') + item.children.map(text).join(''); }
function byClass(item, name) { return flatten(item).filter(node => node.className?.split(' ').includes(name)); }
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
const contextLines = Array.from({length: 6}, (_, index) => '本段未变第 ' + (index + 1) + ' 行');
const view = {project:'/fixture', counts:{pending:1}, review_revision:'b'.repeat(64), decisions:[], proposals:[{
  id:'11111111', revision:'a'.repeat(64), state:'pending', reason:'fixture', can_approve:true, can_reject:true,
  operations:[{action:'patch', before:[...contextLines, '每 30 分钟检查 👩🏽‍💻', '同时保留本行'].join('\n'),
    after:[...contextLines, '每 60 分钟检查 👨🏻‍💻', '同时保留本行'].join('\n')}]
}]};
const sharing = {candidates:[{name:'portable', version:'version-1', revision:'c'.repeat(64), sources:['/fixture'],
  previous_version:'version-0', resources:[{path:'SKILL.md',change:'changed'}, {path:'references/example.md',change:'added'},
    {path:'references/old.md',change:'removed'}, {path:'references/missing.md',change:'changed'}]}], decisions:[]};
const previous = '保留第一行\n旧行\n保留最后一行\n';
const next = '保留第一行\n<script>new body</script>\n新增第二行\n保留最后一行\n';
const files = new Map([['SKILL.md', {before:previous,after:next}], ['references/example.md', {before:null,after:''}],
  ['references/old.md', {before:'旧资料\n移除第二行',after:null}],
  ['references/missing.md', {before:null,after:'待审正文',previous_unavailable:true}]]);
const context = {document, TextEncoder, URLSearchParams, confirm:() => allow, window: {
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
    : route === '/api/publications' ? sharing : route === '/api/publication-file' ? files.get(new URLSearchParams(path.split('?')[1]).get('file')) : view;
  return {ok:true, json:async () => result};
}};
vm.runInNewContext(JSON.parse(require('fs').readFileSync(0, 'utf8')), context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await tick();
  assert.deepStrictEqual(calls.map(call => call.path), ['/api/proposals','/api/publications']);
  assert(document.getElementById('refresh-status').textContent.includes('最近更新'));
  assert(calls.every(call => call.method !== 'POST'));
  const proposals = document.getElementById('proposals');
  const marks = flatten(proposals).filter(item => item.tag === 'mark').map(text);
  assert.deepStrictEqual(marks, ['30', '👩🏽‍💻', '60', '👨🏻‍💻']);
  const folded = byClass(proposals, 'diff-context')[0];
  assert(folded && !folded.open && text(folded.children[0]).includes('3 行'));
  const hunk = byClass(proposals, 'diff-hunk')[1];
  assert.strictEqual(byClass(hunk, 'diff-row').length, 5);
  assert.deepStrictEqual(byClass(hunk, 'diff-mobile-label').map(text), ['当前记忆','待审内容']);
  folded.open = true;
  view.decisions.push({state:'rejected', at:1, pending_id:'00000000'});
  windowEvents.get('focus')(); await tick();
  assert.strictEqual(byClass(proposals, 'diff-context')[0], folded);
  assert(folded.open);
  for (const [a, b] of [['中'.repeat(44000), '短'], ['x\n'.repeat(2001), 'y'], ['a\n'.repeat(501), 'b\n'.repeat(500)]]) {
    context.sample = [a, b];
    const fallback = vm.runInNewContext('comparison(...sample, ["旧", "新"], "无文件", "空文件")', context);
    assert(fallback.tag === 'details' && text(fallback.children[0]).includes('完整原文'));
    assert.deepStrictEqual(flatten(fallback).filter(item => item.tag === 'pre').map(text), [a, b]);
  }
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
  const panels = publication.filter(item => item.tag === 'details');
  const [skill, empty, removed, missing] = panels;
  assert(skill && skill.open);
  await skill.ontoggle();
  assert(calls.some(call => call.path.includes('/api/publication-file?name=portable&revision=' + 'c'.repeat(64) + '&file=SKILL.md')));
  assert(byClass(skill, 'diff-code').some(item => text(item) === '<script>new body</script>'));
  assert(!flatten(skill).some(item => item.tag === 'script'));
  assert.deepStrictEqual(byClass(skill, 'diff-added').map(item => text(item.children[1])), ['+','+']);
  assert.deepStrictEqual(byClass(skill, 'diff-removed').map(item => text(item.children[1])), ['−']);
  for (const panel of [empty, removed, missing]) { panel.open = true; await panel.ontoggle(); }
  assert(text(empty).includes('已共享版本 · 无文件') && text(empty).includes('待审版本 · 空文件'));
  assert.strictEqual(byClass(removed, 'diff-removed').length, 2);
  assert(!byClass(missing, 'diff-added').length && text(missing).includes('已共享版本的文件不可用'));
  assert(flatten(missing).some(item => item.tag === 'pre' && text(item) === '待审正文'));
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
  assert(byClass(skill, 'diff-code').some(item => text(item).includes('new body')));
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
