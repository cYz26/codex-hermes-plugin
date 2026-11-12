"""Native automatic feedback learning and a controlled recall/plan experiment.

All dialogue and capabilities are synthetic. --live-settings borrows the existing
provider/model settings; it never imports production knowledge or App histories.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
GOAL="Evaluate policy evaluation frameworks for enterprise services and recommend a shortlist."
BEFORE="Reviewed public documentation only. Public Framework A ranked first; existing internal capabilities were not examined."
ADDITION="Also check the existing internal solutions and compare their capabilities with the public choices."
AFTER="The internal shared evaluation service already meets the requirements. Reusing that capability changes the preferred option; public Framework A is now a fallback."
METHOD="---\nname: learned-enterprise-technical-research\ndescription: Compare public options with existing internal capabilities.\nlearning_scope: global_candidate\n---\nApplicability: technical research and selection in an enterprise environment.\n1. Start with both public external sources and authorized internal documentation and capabilities.\n2. Compare industry alternatives with existing internal capabilities and revise the ranking when reuse is viable.\n3. State source coverage gaps and uncertainty.\nLimits: respect a user's explicit source restrictions and authorization boundaries. Do not infer access from proximity.\nVerification: show which evidence changed the comparison; reading a skill does not prove execution.\n"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--live-settings',type=Path)
    parser.add_argument('--sidechat',action='store_true')
    parser.add_argument('--routed',action='store_true',help='Scripted cold-model native fork; never overrides a live provider')
    args=parser.parse_args()
    if args.routed and args.live_settings:parser.error('--routed is a scripted-provider contract only')
    os.umask(0o077)
    workspace=args.workspace.resolve();workspace.mkdir(parents=True)
    home,project,other=workspace/'home',workspace/'A',workspace/'B'
    for path in (home,project,other):path.mkdir()
    prior_project=workspace/'prior-library';prior_project.mkdir()
    sys.path[:0]=[str(ROOT/'src'),str(args.source.resolve())]
    os.environ['HERMES_HOME']=str(home)
    requests=[]
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def reply(self,request,delta,finish):
            self.send_response(200)
            if request.get('stream') is False:
                self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'id':'fixture','model':'fixture','choices':[{'index':0,'message':delta,'finish_reason':finish}],
                    'usage':{'prompt_tokens':200,'completion_tokens':50,'total_tokens':250}}).encode())
                return
            self.send_header('Content-Type','text/event-stream');self.end_headers()
            for part in ({'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                         {'choices':[{'index':0,'delta':{},'finish_reason':finish}],
                          'usage':{'prompt_tokens':200,'completion_tokens':50,'total_tokens':250}}):
                self.wfile.write(('data: '+json.dumps({'id':'fixture','model':'fixture',**part})+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            if 'messages' not in request:
                self.send_response(404);self.end_headers();self.wfile.write(b'{}');return
            history=request['messages']
            if any('CONTROLLED_RESEARCH_PLAN' in str(message.get('content')) for message in history):
                recalled=any('RECALLED_METHOD' in str(message.get('content')) for message in history)
                restricted=any('PUBLIC_ONLY_CONSTRAINT' in str(message.get('content')) for message in history)
                content=json.dumps({'sources':[{'kind':'public','planned':True}]+([{'kind':'authorized_internal','planned':True}] if recalled and not restricted else []),
                                    'coverage_gaps':['internal coverage not assessed'] if restricted else ['report unqueried sources'],
                                    'reason':'Respect source scope and compare reusable capabilities.'})
                delta={'role':'assistant','content':content};finish='stop'
            elif not any(message.get('role')=='tool' and 'success' in str(message.get('content')) for message in history):
                if ADDITION not in str(history) or AFTER not in str(history):
                    delta={'role':'assistant','content':'This revision does not yet demonstrate a change in research method.'};finish='stop'
                    self.reply(request,delta,finish)
                    return
                labels=[]
                for message in history:
                    text=message.get('content') or ''
                    if not isinstance(text,str):continue
                    for match in re.finditer(r'\[Evidence (\{[^\n]+\})\]\n',text):
                        label=json.loads(match[1]);labels.append((label,text[match.end():]))
                own=[(label,text) for label,text in labels if label.get('scope')=='own']
                basis=[]
                for label,text in own:
                    quote=next((q for q in (ADDITION,AFTER,BEFORE) if q in text),None)
                    if quote:basis.append({'ref':label['ref'],'quote':quote})
                proof={'classification':'procedure','basis':basis[:6],
                       'rationale':'Comparing existing internal capabilities with public alternatives changed the preferred option; enterprise technical research should compare both source sets and report gaps.'}
                operations=[{'action':'patch','name':'learned-delimited-text-ingestion','content':METHOD,'evidence':proof},
                            {'action':'create','name':'learned-enterprise-technical-research','content':METHOD,'evidence':proof}]
                calls=[{'index':i,'id':f'write-{i}','type':'function','function':{'name':'skill_manage',
                       'arguments':json.dumps({'operations':[operation]})}} for i,operation in enumerate(operations)]
                delta={'role':'assistant','tool_calls':calls};finish='tool_calls'
            else:
                delta={'role':'assistant','content':'Saved a source-scoped research procedure; unrelated CSV work was not changed.'};finish='stop'
            self.reply(request,delta,finish)
    server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    from hermes_cli.config import DEFAULT_CONFIG,atomic_config_write
    configuration={'_config_version':DEFAULT_CONFIG['_config_version'],
        'model':{'provider':'custom','default':'gpt-4.1','base_url':f'http://127.0.0.1:{server.server_port}/v1'},
        'agent':{'reasoning_effort':'max'},'memory':{'memory_enabled':True,'user_profile_enabled':True},
        'auxiliary':{'background_review':{'enabled':True,'provider':'auto','model':''}},
        'plugins':{'isolation':'in_process','enabled':['codex-learning-bridge'],
                   'entries':{'codex-learning-bridge':{'enabled':True,'settings':{'learning':{'enabled':True}}}}}}
    if args.live_settings:
        from hermes_codex_learning.config import Settings
        from ruamel.yaml import YAML
        live=Settings.load(args.live_settings)
        original=YAML(typ='safe').load((live.home/'config.yaml').read_text())
        configuration['model']={key:value for key,value in original['model'].items() if key in
                                {'provider','default','api_mode','base_url','max_tokens','context_length'}}
        configuration['providers']={name:{key:value for key,value in entry.items() if key in
            {'base_url','api_mode','session_affinity_header','max_tokens','transport'}} for name,entry in original.get('providers',{}).items()}
        configuration['agent']={key:value for key,value in original.get('agent',{}).items() if key in {'reasoning_effort','max_tokens'}}
        configuration['auxiliary']['background_review']=deepcopy(original.get('auxiliary',{}).get('background_review',configuration['auxiliary']['background_review']))
        os.environ.update(live.network_environment)
    else:
        os.environ['OPENAI_API_KEY']='scripted-loopback-fixture'
        if args.routed:configuration['auxiliary']['background_review'].update(provider='custom',model='gpt-4.1-mini')
    atomic_config_write(home/'config.yaml',configuration)
    csv=home/'skills/learned-delimited-text-ingestion/SKILL.md';csv.parent.mkdir(parents=True)
    csv.write_text('---\nname: learned-delimited-text-ingestion\ndescription: Parse CSV files and validate delimited headers.\nlearning_scope: global_candidate\n---\nApplicability: CSV header ingestion.\nLimits: delimited text only.\nVerification: duplicate headers are rejected.\n')
    csv_before=hashlib.sha256(csv.read_bytes()).hexdigest()
    from hermes_cli.plugins import PluginContext,PluginManifest,get_plugin_manager
    from hermes_codex_learning.config import Settings
    from hermes_codex_learning.daemon import Bridge
    from hermes_codex_learning.capture import hook
    from hermes_codex_learning.semantic_review import install_semantic_policy
    from hermes_codex_learning.review_input import install_review_policy
    from hermes_codex_learning.knowledge import reconcile
    from hermes_codex_learning.store import atomic_json
    settings=Settings(home,project,args.source.resolve(),Path(sys.executable),capture_scope='global',
                      proactive=True,idle_seconds=0,idle_interval=0,checked_publication=True,quality_checks=True,
                      data_policy={'enabled':True,'synthetic_roots':[str(workspace)]})
    ctx=PluginContext(PluginManifest(name='codex-learning-bridge',source='user'),get_plugin_manager())
    api=ctx.learning(version=1)
    native_agent_factory=api._service.runtime.agent
    guard=install_semantic_policy(api,settings,ctx);install_review_policy(api,settings)
    bridge=Bridge(settings,api)
    def turn(chat,turn_id,user,answer,*,transcript=None):
        events=[{'hook_event_name':'UserPromptSubmit','prompt':user}]
        if args.routed and turn_id=='prior':
            for i in range(40):
                events.extend([{'hook_event_name':'PreToolUse','tool_use_id':f'lookup-{i}','tool_name':'synthetic_lookup','tool_input':{'source':'public','index':i}},
                    {'hook_event_name':'PostToolUse','tool_use_id':f'lookup-{i}','tool_name':'synthetic_lookup','tool_input':{'source':'public','index':i},
                     'tool_response':{'detail':'repeated synthetic public detail '*1000,'index':i}}])
        events.append({'hook_event_name':'Stop','last_assistant_message':answer})
        for event in events:
            hook({**event,'cwd':str(project),'session_id':chat,'turn_id':turn_id,
                  **({'transcript_path':str(transcript)} if transcript else {})},settings)
        bridge.ingest()
    plan_index=0
    def plan(method,restriction=False):
        nonlocal plan_index
        plan_index+=1
        # Reuse the worker's complete configured provider/API mode and budgets.
        # Constructing a default AIAgent can select a different transport.
        agent=native_agent_factory(f'controlled-plan-{plan_index}',{'project':str(other),'executor':'synthetic plan experiment'})
        agent._memory_store=None;agent._memory_manager=None;agent._user_profile_enabled=False;agent.tools=[];agent.valid_tool_names=set()
        agent._cached_system_prompt='Return JSON with sources, coverage_gaps and reason. Each sources entry must be an object with kind (public, authorized_internal, or user_requirements), planned (boolean), and detail. List planned source types; describe excluded sources in coverage_gaps. This is a controlled planning experiment, not research execution. Keep the supplied existing plan unless recalled guidance changes it; always honor user source constraints.'
        agent._build_system_prompt=lambda *a,**kw:agent._cached_system_prompt
        prompt=('CONTROLLED_RESEARCH_PLAN: Compare workflow orchestration technologies for enterprise services. '
                'Existing plan: consult public documentation and rank publicly documented alternatives. '
                + ('PUBLIC_ONLY_CONSTRAINT: For this task use public documentation only; do not query internal systems. ' if restriction else '')
                + ('RECALLED_METHOD:\n'+method if method else 'No recalled method supplied.'))
        try:
            response=agent.run_conversation(prompt)
            text=response.get('final_response') or ''
            clean=re.sub(r'^```(?:json)?\s*|\s*```$','',text.strip())
            from agent.background_review import _snapshot_review_usage
            usage=_snapshot_review_usage(agent)
            return {'text':text,'json':json.loads(clean),'completed':response.get('completed',False),
                    'usage':{key:usage.get(key) for key in ('model','provider','input_tokens','output_tokens','cache_read_tokens','api_calls')}}
        finally:
            agent.tools=[];agent.valid_tool_names=set()
    try:
        initial=reconcile({},api.read(kind='context'),bootstrap_project=str(prior_project))
        initial['skills']['learned-delimited-text-ingestion']={'projects':[str(prior_project)]}
        bridge.store.set('knowledge-index-v1',initial)
        bridge.publish()
        baseline=plan('')
        if args.sidechat:
            sessions=workspace/'codex/sessions';sessions.mkdir(parents=True)
            os.environ['CODEX_HOME']=str(workspace/'codex')
            parent=sessions/'parent.jsonl'
            rows=[{'type':'session_meta','payload':{'id':'parent','cwd':str(project),'cli_version':'0.167.0'}},
                  {'type':'response_item','ordinal':10,'payload':{'role':'user','id':'parent-request','internal_chat_message_metadata_passthrough':{'turn_id':'prior'}}},
                  {'type':'response_item','ordinal':20,'payload':{'role':'assistant','id':'parent-result','internal_chat_message_metadata_passthrough':{'turn_id':'prior'}}}]
            parent.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            turn('parent','prior',GOAL,BEFORE,transcript=parent)
            child=sessions/'sidechat.jsonl'
            child.write_text(json.dumps({'type':'session_meta','payload':{'id':'sidechat','cwd':str(project),'cli_version':'0.167.0',
                 'forked_from_id':'parent','forked_from_ordinal_exclusive':25,'attached_thread':True,
                 'history_base':{'thread_id':'parent','end_ordinal_exclusive':25}}})+'\n')
            chat='sidechat';turn(chat,'addition',ADDITION,AFTER,transcript=child)
        else:
            chat='research';turn(chat,'prior',GOAL,BEFORE)
            turn(chat,'addition',ADDITION,AFTER)
        for _ in range(100):
            bridge.ingest()
            if bridge.store.pending():
                continue
            bridge.idle_reviews();bridge.reviews();bridge.last_publish=0;bridge.publish()
            pending_feedback=bridge.store.db.execute('SELECT 1 FROM review_priority WHERE requested IS NULL LIMIT 1').fetchone()
            if not api.read(kind='status',detail='summary')['intents'] and not pending_feedback:break
        context=bridge.rpc('context',{'cwd':str(other),'query':'technical research comparison of enterprise orchestration alternatives'})
        names=[name for name in context['recommended_skills'] if name.startswith('learned-') and name!='learned-delimited-text-ingestion']
        if not names:
            atomic_json(workspace/'result.json',{'passed':False,'reason':'no_published_research_candidate','actual_app':False,
                       'native_status':api.read(kind='status'),'progress':bridge.rpc('learning_status',{'cwd':str(project),'chat':chat})['progress']})
            raise RuntimeError('No published reusable research candidate; inspect the native outcome, not the ready label')
        loaded=bridge.rpc('skill_load',{'cwd':str(other),'name':names[0]})
        method=loaded['content'];(workspace/'learned-method.md').write_text(method)
        recalled=plan(method);restricted=plan(method,True)
        events=[json.loads(row[0]) for row in bridge.store.db.execute('SELECT payload FROM notices')]
        reviews=[event for event in events if event.get('kind')=='review.finished']
        def planned_sources(value):
            sources=value['json'].get('sources')
            if not isinstance(sources,list) or not all(isinstance(source,dict) and source.get('kind') in
                    {'public','authorized_internal','user_requirements'} and type(source.get('planned')) is bool for source in sources):
                raise ValueError('Planning response does not follow the explicit source schema')
            return {source['kind'] for source in sources if source['planned']}
        actual_sources={name:planned_sources(value) for name,value in (('baseline',baseline),('recalled',recalled),('public_only',restricted))}
        checks={'native_review_completed':bool(reviews) and all(event.get('run_status')=='completed' for event in reviews),
            'csv_unchanged':hashlib.sha256(csv.read_bytes()).hexdigest()==csv_before,
            'published_research_candidate':bool(names),'cross_project_memory_empty':not context['memories'],
            'cross_project_recommended':names[0] in context['recommended_skills'],
            'plan_changed_to_include_internal':'authorized_internal' in actual_sources['recalled'] and 'authorized_internal' not in actual_sources['baseline'],
            'explicit_public_only_respected':'authorized_internal' not in actual_sources['public_only'],
            'all_plan_calls_completed':all(plan.get('completed') is True for plan in (baseline,recalled,restricted)),
            'source_limits_in_skill':bool(re.search(
                r'authoriz|source[^\n.!?。！？]{0,120}(?:coverage|restrict|limit)|coverage gap|授权|来源[^\n。！？]{0,80}(?:限制|缺口)',
                method,re.I))}
        if args.routed:
            checks['native_digest_used']=any(json.loads(row[0]).get('native_digest',{}).get('used') for row in
                bridge.store.db.execute("SELECT value FROM meta WHERE key LIKE 'review-input:%'"))
        result={'scope':'actual provider with synthetic feedback and controlled plan experiment; not App or executed research' if args.live_settings else 'scripted native contracts; not an actual model or App',
                'sidechat':args.sidechat,'routed':args.routed,'checks':checks,'passed':all(checks.values()),
                'published_names':names,'plans':{'baseline':baseline,'recalled':recalled,'public_only':restricted},
                'learning_progress':bridge.rpc('learning_status',{'cwd':str(project),'chat':chat})['progress'],
                'native_usage':api.read(kind='status').get('usage'),'actual_app':False,'source_queries_executed':False,
                'model':configuration['model'].get('default'),'provider':configuration['model'].get('provider')}
        atomic_json(workspace/'result.json',result)
        print(json.dumps({key:result[key] for key in ('scope','sidechat','routed','checks','passed','published_names','actual_app','source_queries_executed')}))
        return 0 if result['passed'] else 1
    finally:
        bridge.store.close();guard.dispose();api.close();server.shutdown()


if __name__=='__main__':raise SystemExit(main())
