"""Local research entry point through the unchanged participant protocol."""
import argparse, hashlib, json, os, sys
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]

def read_config(path):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    fields={'base_url','model','plan_interval','timeout_seconds','max_tokens','max_fallback'}
    if set(data)!=fields: raise ValueError('Config must contain exactly: '+', '.join(sorted(fields)))
    for key in ('plan_interval','max_tokens','max_fallback'):
        if type(data[key]) is not int or data[key]<1: raise ValueError(key+' must be a positive integer')
    t=data['timeout_seconds']
    if type(t) not in (int,float) or not 0<t<30: raise ValueError('timeout_seconds must be >0 and <30 (API participant deadline)')
    url=urlsplit(data['base_url'])
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('base_url must be an HTTP(S) endpoint without credentials/query/fragment')
    if not isinstance(data['model'],str) or not data['model'].strip(): raise ValueError('model required')
    return data

def configure(data, output):
    mapping={'base_url':'OPENMD_LLM_BASE','model':'OPENMD_LLM_MODEL','plan_interval':'OPENMD_LLM_PLAN_INTERVAL',
             'timeout_seconds':'OPENMD_LLM_TIMEOUT','max_tokens':'OPENMD_LLM_MAX_TOKENS','max_fallback':'OPENMD_LLM_MAX_FALLBACK'}
    for key,env in mapping.items():os.environ[env]=str(data[key])
    os.environ['OPENMD_LLM_AUDIT']=str(output/'llm-audit.jsonl')

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'configs/hybrid.example.json')
    parser.add_argument('--scenario',required=True)
    parser.add_argument('--seed',type=int,default=2026102801)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--max-ticks',type=int,help='Optional bounded diagnostic, not a full episode')
    parser.add_argument('--replay-format',choices=['html','shared','none'],default='html')
    args=parser.parse_args(argv);data=read_config(args.config)
    if args.max_ticks is not None and args.max_ticks<1:parser.error('--max-ticks must be positive')
    output=args.output.resolve()
    if output.exists():parser.error('Output already exists; choose a new path to preserve previous evidence')
    from .runtime import entry, public_brief
    try:
        row = entry(args.scenario)
    except StopIteration:
        parser.error(f'Unknown demo scenario: {args.scenario}')
    if row['track'] != 'interception':
        parser.error('This hybrid baseline is for interception scenarios only')
    buoy_policy = 'contact_judgment_contract' in public_brief(args.scenario)
    configure(data,output)
    # Hash implementation plus effective configuration, not a fictitious rule baseline.
    names=['run_hybrid.py','omd_competition/hybrid_runner.py','omd_competition/hybrid_agent.py',
           'omd_competition/hybrid_llm_client.py','omd_competition/hybrid_brief_compat.py',
           'omd_competition/agent.py','omd_competition/intercept_planner.py','omd_competition/wave_schedule.py']
    if buoy_policy:
        names.extend(('omd_competition/hybrid_buoy.py', 'omd_competition/buoy_baselines.py',
                      'omd_competition/trajectory_judgment.py', 'omd_competition/action_feedback.py'))
    hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    provenance={'architecture':'hybrid','config':data,'source_sha256':hashes,
                'thinking':False,'scheduling':'strict_interval','source':'IE15 inspection scheduling' if buoy_policy else '20261028 experiment; 20261030 integration',
                'official_sandbox_certified':False}
    digest=hashlib.sha256(json.dumps(provenance,sort_keys=True).encode()).hexdigest()
    from .evaluate import run_episode
    try:
        result=run_episode(args.scenario,'hybrid',args.seed,output,
            command=[sys.executable,'-B','-m','omd_competition.hybrid_agent'],
            max_ticks=args.max_ticks,submission_sha256=digest,resource_group='api',
            replay_format=args.replay_format,replay_keep=0,keep_replay=True)
    finally:
        if output.is_dir():
            (output/'hybrid-provenance.json').write_text(json.dumps(provenance,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=[]
    if (output/'llm-audit.jsonl').exists():
        rows=[json.loads(s) for s in (output/'llm-audit.jsonl').read_text(encoding='utf-8').splitlines() if s.strip()]
    transport=sum(r['failures_delta'] for r in rows)
    invalid=sum(not r['parsed_json'] for r in rows)
    executor_errors=sum(p.read_text(encoding='utf-8',errors='replace').count('executor_error') for p in output.glob('*.stderr*'))
    summary={'status':result.get('status'),'final_tick':result.get('final_tick'),
             'utility':result.get('utility'),'llm_calls':len(rows),'transport_failures':transport,
             'invalid_json_responses':invalid,'executor_errors':executor_errors,
             'model_called_successfully':any(r['parsed_json'] and not r['failures_delta'] for r in rows),
             'validation_scope':'bounded_probe' if args.max_ticks else 'natural_terminal'}
    summary['reported_models'] = sorted({row['reported_model'] for row in rows if row.get('reported_model')})
    directives_invalid = 0
    if buoy_policy:
        plans_path = output/'scout-plans.jsonl'
        plans = [json.loads(line) for line in plans_path.read_text(encoding='utf-8').splitlines()] if plans_path.is_file() else []
        directives_invalid = sum(not plan['accepted'] for plan in plans)
        summary['scout_plans'] = {
            'accepted': len(plans) - directives_invalid, 'rejected': directives_invalid,
            'survey': sum(p['effective_mode'] == 'survey' for p in plans),
            'revisit': sum(p['effective_mode'] == 'revisit' for p in plans),
        }
        summary['model_called_successfully'] &= summary['scout_plans']['accepted'] > 0
    (output/'hybrid-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return int(result.get('status') not in ('completed','bounded_probe') or not summary['model_called_successfully'] or transport or invalid or directives_invalid or executor_errors)
