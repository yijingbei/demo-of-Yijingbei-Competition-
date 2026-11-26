"""Package saved tuning JSON traces for the HTML plugin, using the standard library.

No engine/product imports, YAML execution, simulation, score replacement or guessed
target tracks. The original result, trace and candidate assets remain untouched.
"""
import argparse
import base64
from collections import Counter
import copy
import gzip
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re

VERSION = '1.8.9'
NAMES = {
    'MD-REC-001':'港口接近区巡查', 'MD-REC-002':'分区协同搜索', 'MD-REC-003':'海岸跨域侦察',
    'MD-REC-004':'低可观测目标搜索', 'MD-REC-005':'多域信息报告', 'MD-REC-006':'雾天监视',
    'MD-REC-007':'间歇目标搜索', 'MD-REC-008':'空海岸协同感知',
    'MD-ER-001':'天气绕行', 'MD-ER-002':'设备故障与监视接替', 'MD-ER-003':'并发呼救调度',
    'MD-ER-004':'通信中断与西侧任务', 'MD-ER-005':'告警核实与状态更新', 'MD-ER-006':'多事件并发与监视交接',
}
METRICS = {'recall':'目标发现率', 'discovery':'目标发现率', 'coverage':'区域覆盖率',
    'discovery_timeliness':'发现及时性', 'freshness':'新鲜观测', 'information_timeliness':'信息连续性',
    'delivered_recall':'报告送达覆盖', 'delivered_freshness':'送达信息新鲜度',
    'arrival':'任务服务完成', 'timeliness':'响应及时性', 'safety':'路线安全',
    'triage':'告警纪律', 'continuity':'持续监视'}


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def metric_name(identifier):
    text = identifier.removeprefix('metric.')
    if text in METRICS:
        return METRICS[text]
    for prefix, label in METRICS.items():
        if text.startswith(prefix):
            return label+' · '+text[len(prefix):].strip('._')
    return identifier


def sensor_resources(path):
    """Read only exact scalar fields from this flat catalog format, never YAML code."""
    result = {}
    for block in path.read_text(encoding='utf-8').split("- schema_version: '2.0'\n")[1:]:
        if '\n  resource_type: sensors\n' not in '\n'+block:
            continue
        def value(field, indent=2):
            match = re.search(r'(?m)^'+' '*indent+re.escape(field)+r': ([^\n]+)$', block)
            return match[1].strip("'\"") if match else None
        identifier, version, radius = value('id'), value('version'), value('range_m', 4)
        if not all((identifier, version, radius)):
            continue
        kind = value('kind', 4) or 'sensor'
        result[identifier+'@'+version] = dict(ref=identifier+'@'+version,
            type='radar' if kind=='radar' else 'sensor', sensorKind=kind,
            maxM=float(radius), minM=0, updateTicks=int(value('update_ticks',4) or 1), targetDomains=[])
    return result


def episode(candidate, result_folder, row, record):
    result = read(result_folder/'result.json')
    identifier = result['scenario']
    assert identifier == record['scenario'] == row['scenario']
    for field in ['policy','seed','runtime_sha256','utility','task_success','final_tick','trace_sha256']:
        assert result.get(field) == row.get(field), (identifier, field)
    assert result['runtime_sha256'] == record['validation']['runtime_sha256']
    assert result['status'] == 'completed' and result.get('decision_timeouts',0) == 0
    assert sha(result_folder/'trace.jsonl') == result['trace_sha256']
    scene_folder = candidate/'config/assets/scenarios'/identifier.lower()
    source_hashes = {p.name:sha(p) for p in scene_folder.iterdir() if p.is_file()}
    brief = read(scene_folder/'public_brief.json')
    track = 'reconnaissance' if identifier.startswith('MD-REC-') else 'emergency'
    catalog = candidate/'config/assets/catalog/competition_four_categories.yaml'
    assert sha(catalog) == record['baseline']['asset_sha256']['catalog/competition_four_categories.yaml']
    resources = sensor_resources(catalog)
    public_assets = {x['entity_id']:x for x in brief.get('own_assets',brief.get('response_assets',[]))}
    entities, snapshots, messages, zones, scores, events = {}, {}, {}, {}, [], []
    samples_count = Counter()

    def add_zone(value, kind, tick=0):
        if isinstance(value,dict) and value.get('coordinates_m'):
            key = value['id']
            old = zones.get(key)
            if old is None or old['availableTick'] > tick:
                zones[key] = dict(value, kind=kind, availableTick=tick)
    for z in brief.get('search_zones',[]): add_zone(z,'search')
    for sector in brief.get('search_sectors',[]):
        for z in sector.get('cells',[]): add_zone(z,'search')
    for z in brief.get('risk_regions',[]): add_zone(z,'risk')
    for task in brief.get('initial_requests',[]): add_zone(task.get('destination'),'service')
    for duty in brief.get('standing_missions',[]): add_zone(duty.get('search_region'),'watch')
    trace_rows = 0
    with (result_folder/'trace.jsonl').open('rb') as stream:
        for line in stream:
            frame = json.loads(line)
            trace_rows += 1
            assert frame['tick'] == trace_rows, (identifier, 'trace gap')
            score = frame.get('score')
            if score:
                assert score.get('tick', frame['tick']) == frame['tick']
                scores.append(dict(tick=frame['tick'], values={m['metric_id']:m.get('value') for m in score.get('metrics',[])}))
            assert not any(x.get('execution') for x in frame.get('combat_receipts',[])), 'tuning import is non-combat'
            for participant in frame.get('participants',[]):
                obs = participant['request']['observation']
                tick = obs['tick']
                assert tick == frame['tick']-1
                assert obs['observer_faction_id'] == 'red'
                snapshot = snapshots.setdefault(tick, dict(tick=tick, own={}, contacts={}))
                for own in obs.get('own_entities',[]):
                    i = own['entity_id']
                    asset = public_assets.get(i,{})
                    domain = asset.get('domain')
                    if not domain:
                        domain = 'air' if own['position_m'][2]>15 else 'shore' if asset.get('maximum_speed_mps')==0 else 'surface'
                    kind = 'facility' if domain in ('shore','land') else domain
                    category = {'air':'uav','surface':'usv','facility':'shore'}[kind]
                    profiles = [dict(resources[ref],category=category) for ref in own.get('sensor_states',{}) if ref in resources]
                    name = ('无人机' if kind=='air' else '无人船' if kind=='surface' else '岸站')+' '+i.rsplit('.',1)[-1]
                    ent = entities.setdefault(i, dict(id=i,name=name,short=name,side='red',factionId='red',kind=kind,
                        decoy=False,armedThreat=False,protected=False,spawn=tick,source='native',initialAmmo=0,
                        states=[],samples=[],sampleGaps=[],ranges=profiles))
                    sample = [tick,*own['position_m'],own['heading_deg'],own['health'],sum(own['ammunition'].values()),own['lifecycle_state']]
                    if ent['samples'] and ent['samples'][-1][0] == tick:
                        assert ent['samples'][-1] == sample
                    else:
                        ent['samples'].append(sample)
                        ent['states'].append([tick,own['health'],own['lifecycle_state']])
                        samples_count[i] += 1
                    selected = {k:own[k] for k in ['entity_id','position_m','heading_deg','health','ammunition','lifecycle_state']}
                    old = snapshot['own'].setdefault(i,selected)
                    assert old == selected
                for contact in obs.get('organic_contacts',[])+obs.get('shared_contacts',[]):
                    key = obs['controller_slot_id']+':'+contact['contact_id']
                    snapshot['contacts'][key] = dict(contact,replay_contact_key=key)
                for msg in obs.get('received_messages',[]):
                    key = (obs['controller_slot_id'],msg['message_id'])
                    if key in messages:
                        continue
                    try:
                        body = json.loads(msg.get('payload',''))
                    except (ValueError,TypeError):
                        body = {}
                    if not isinstance(body,dict): body = {}
                    delivered = msg['delivered_tick']
                    assert 0 <= delivered <= tick
                    item = dict(tick=delivered,availableTick=tick,controller=obs['controller_slot_id'],
                        recipientFaction='red',side='red',sender=msg.get('sender_entity_id'),id=msg['message_id'],
                        sentTick=msg.get('sent_tick'),payload=msg.get('payload',''),body=body,
                        trustedSource=msg.get('sender_entity_id') in brief.get('trusted_dispatch_sources',[]))
                    messages[key] = item
                    dispatch = body.get('schema_version')=='dispatch-task@1.0'
                    if dispatch:
                        add_zone(body.get('destination'),'service',tick)
                        for z in body.get('caution_regions',[]): add_zone(z,'risk',tick)
                    events.append(dict(t=tick,type='dispatch' if dispatch else 'message',side='red',
                        a=msg.get('sender_entity_id'),title=('收到任务 · '+str(body.get('request_id'))+' · '+str(body.get('status'))) if dispatch else '消息实际收件',
                        detail=str(msg.get('sender_entity_id'))+' → '+obs['controller_slot_id']+'；原生送达 tick '+str(delivered)+'，首次观测 tick '+str(tick)))
    end = result['final_tick']
    assert trace_rows == end and sorted(snapshots) == list(range(end))
    for i, ent in entities.items():
        ent['lastNativeTick'] = ent['samples'][-1][0]
        ent['nativeSampleTicks'] = [s[0] for s in ent['samples']]
        final = result['referee_final_entities'][i]
        prior = ent['samples'][-1]
        ent['samples'].append([end,*final['position_m'],prior[4],final['health'],prior[6],final['lifecycle']])
        ent['states'].append([end,final['health'],final['lifecycle']])
        ent['finalHeadingSource'] = 'last authorized observation; final snapshot records no heading'
    metrics = [dict(id=m['metric_id'],name=metric_name(m['metric_id']),weight=m['effective_weight'],
        value=m['utility_value'],points=100*m['weighted_contribution'])
        for m in result['score_audit']['metric_receipts'] if m.get('effective_weight',0)>0]
    assert math.isclose(sum(m['points'] for m in metrics),100*result['utility'],abs_tol=1e-7)
    assert scores[-1]['tick']==end
    # Referee event times are diagnostic annotations, not participant foreknowledge.
    for event in record.get('scene',{}).get('events',[]):
        tick = event.get('tick')
        if isinstance(tick,int) and 0<=tick<=end:
            events.append(dict(t=tick,type='scenario-event',side=None,title=event.get('label',event.get('type','场景事件')),
                detail='原版本组织方事件注释；不表示选手提前获知。'))
    events.append(dict(t=end,type='terminal',side='red',title='任务成功' if result['task_success'] else '任务未成功',
        detail='原始裁判自然终局；总分与综合成功分别记录。'))
    events.sort(key=lambda e:e['t'])
    convention = dict(schema_version='competition-factions@1',colors={'red':'red','blue':'blue'},
        roles={'red':'reconnaissance' if track=='reconnaissance' else 'responder',
               'blue':'opposing_targets' if track=='reconnaissance' else 'environment'},participant_faction_id='red',participant_side='red')
    coords = [s[1:4] for e in entities.values() for s in e['samples']]
    coords += [[p[0],p[1],0] for z in zones.values() for p in z['coordinates_m']]
    xs,zs = [p[0]/1000 for p in coords],[-p[1]/1000 for p in coords]
    version = candidate.name
    warning = '原调优 '+version+' / v05 真实日志；不是 v27/v28 整合版重跑。未保存目标全程真值，蓝色目标仅显示实际授权接触；终帧仅补原裁判最终位置。'
    meta = dict(key=identifier+'-'+str(result['seed'])+'-'+row['policy'],track=track,
        factionConvention=convention,participantSide='red',scenario=identifier,seed=result['seed'],policy=result['policy'],
        score=result['utility']*100,end=end,success=result['task_success'],status=result['status'],terminal=result['terminal'],
        nativeTickSeconds=brief.get('physics_dt_seconds',1),traceSha256=result['trace_sha256'],engineCommit=result['engine_commit'],
        runtimeSha256=result['runtime_sha256'],campaign=version,case=result_folder.name,casePath=result_folder.name,
        sourceVersion=version+' / '+result['package_version']+' / '+result['runtime_sha256'][:12],sourceFiles=source_hashes,
        plannedThreats=0,plannedDecoys=0,combatUnits=sum(e['kind']!='facility' for e in entities.values()),facilityCount=0,
        finalNeutral=0,finalOwnActive=sum(e['states'][-1][2] in ('active','degraded') for e in entities.values() if e['kind']!='facility'),
        finalOperational=0,lastCombatEvent=0,quietAfter=end+1,blueShots=0,redShots=0,
        overview=dict(focus=[(min(xs)+max(xs))/2,0,(min(zs)+max(zs))/2],span=max(3,(max(xs)-min(xs)+.6)*1.25,(max(zs)-min(zs)+.6)*1.6)),
        facilityDamaged=False,warnings=[warning],combatRejections={},decisionTimeouts=0,error=None,
        observationScope='participant-trace',tuningVersion=version,
        label=identifier+' · '+version+' · '+result['policy']+' · seed '+str(result['seed'])+' · '+('成功' if result['task_success'] else '未成功')+' · '+f'{100*result["utility"]:.2f}')
    mission = dict(track=track,task=brief.get('task'),initialRequests=brief.get('initial_requests',[]),
        standingMissions=brief.get('standing_missions',[]),zones=list(zones.values()),messages=sorted(messages.values(),key=lambda m:m['availableTick']),
        scores=scores,metricNames={m['id']:m['name'] for m in metrics},events=[],
        notice='历史逐 tick 授权观察；目标显示为接触估计。通知按首次出现在收件观察的时刻显示，抵达不等于物理救援。')
    perspectives = dict(red=[dict(tick=t,own=list(v['own'].values()),contacts=list(v['contacts'].values())) for t,v in sorted(snapshots.items())],blue=[])
    return dict(meta=meta,entities=list(entities.values()),shots=[],hits=[],events=events,metrics=metrics,land=[],zones=[],
        recording=None,perspectives=perspectives,mission=mission,rangeInfo=dict(status='ready',binding='original-candidate-catalog',
        catalog='competition_four_categories',catalogSha256=sha(catalog),source='Original catalog digest verified against tuning record'),
        terrain=dict(coastlineExact=False,bathymetryAvailable=False),
        public=dict(name=NAMES[identifier],task=brief.get('task'),opponent=None),
        checks=dict(traceHashVerified=True,scoreVerified=True,sourceVersionVerified=False,sourceAssetsVerified=True,
            sourceRuntimeClaimMatchesRecord=True,launchCountVerified=True,finalCoordinatesVerified=True,traceRows=trace_rows,
            nativeSamplesPreserved=dict(samples_count),replayFrameHashVerified=False,capturedSceneVerified=False,everyTickRecorded=False))


def add_coastal_context(data, plugin_root):
    """Add explicitly display-only IE coastline context to mapless old logs.

    No scenario map_ref, collision geometry, coordinates or referee data change.
    Context references are separate from native scenario geography references.
    """
    source = read(Path(plugin_root)/'data/weihai-coastal-context.json')
    assert source['schema'] == 'replay-coastal-context@1'
    geography = source['geography']
    assert geography['displayOnly'] and not geography['scenarioMapBound']
    groups = {}
    for ep in data['episodes']:
        if (ep['meta'].get('observationScope') == 'participant-trace'
                and ep['meta'].get('track') in ('reconnaissance', 'emergency')
                and not ep.get('geography') and not ep.get('geographyRef')):
            groups.setdefault(ep['meta']['scenario'], []).append(ep)
    if not groups:
        return data
    existing = data.setdefault('geographies', {}).get(source['id'])
    assert existing is None or existing == geography, 'Conflicting coastline context'
    data['geographies'][source['id']] = geography
    for episodes in groups.values():
        points = [s[1:3] for ep in episodes for e in ep['entities'] for s in e['samples']]
        points += [p for ep in episodes for z in ep.get('mission', {}).get('zones', [])
                   for p in z['coordinates_m']]
        # Keep the nearby source coast and closed island 023 visible at startup.
        # These are viewport bounds, not fabricated terrain or collision rings.
        x0 = min(-1700, *(p[0] for p in points))
        x1 = max(2100, *(p[0] for p in points))
        y0 = min(-2150, *(p[1] for p in points))
        y1 = max(1200, *(p[1] for p in points))
        overview = dict(focus=[(x0+x1)/2000, 0, -(y0+y1)/2000],
                        span=max(6.4, (x1-x0+800)/1000*1.25, (y1-y0+1000)/1000*1.9))
        for ep in episodes:
            ep['visualization'] = dict(role='background-only',
                geographyContextRef=source['id'], overview=overview,
                note=source['notice'], originalScenarioMapRef=None,
                coordinateTransform='identity', collisionEnabled=False)
    return data


def pack(episodes, plugin_root=None):
    scenarios=[]
    for identifier in dict.fromkeys(e['meta']['scenario'] for e in episodes):
        group=[e for e in episodes if e['meta']['scenario']==identifier]
        name=group[0]['public']['name']
        scenarios.append(dict(id=identifier,name=name,tag=str(len(group))+' 局真实记录',short='策略 / 种子与原始结果对照',description=identifier+' · '+name))
    data = dict(schema='competition-multiscene-replay@1',pluginVersion=VERSION,scenarios=scenarios,episodes=episodes,geographies={},skipped=[])
    return add_coastal_context(data, plugin_root or Path(__file__).resolve().parent)


def render(pack_data,plugin,title):
    shells=[dict(meta=e['meta'],public=e['public'],lazyData=base64.b64encode(gzip.compress(encoded(e),mtime=0)).decode('ascii')) for e in pack_data['episodes']]
    data=base64.b64encode(gzip.compress(encoded(dict(pack_data,episodes=shells,lazyEpisodes=True)),mtime=0)).decode('ascii')
    js='\n'.join((plugin/n).read_text(encoding='utf-8') for n in ['controls.js','tasks.js','ranges.js','player.js'])
    bootstrap='''(async()=>{
const node=document.getElementById('replay-data');
const inflate=async s=>new Response(new Blob([Uint8Array.from(atob(s.trim()),c=>c.charCodeAt(0))]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
const pack=JSON.parse(await inflate(node.textContent));
const first=pack.episodes[0];pack.episodes[0]={...JSON.parse(await inflate(first.lazyData)),lazyData:first.lazyData};
node.type='application/json';node.textContent=JSON.stringify(pack);
const script=document.createElement('script');script.textContent=PLAYER_SOURCE;document.body.appendChild(script);
})().catch(error=>{document.getElementById('viewport').textContent='回放加载失败：'+error.message;console.error(error)});'''.replace('PLAYER_SOURCE',json.dumps(js,ensure_ascii=False).replace('<','\\u003c'))
    values=dict(TITLE=escape(title),SUMMARY=f'{len(pack_data["scenarios"])} 个场景 · {len(shells)} 局真实记录 · REC/ER 为原调优版本',
        OPTIONS=''.join(f'<option value="{i}">{escape(e["meta"]["label"])}</option>' for i,e in enumerate(pack_data['episodes'])),
        THREE_JS=(plugin/'vendor/three.min.js').read_text(encoding='utf-8'),REPLAY_DATA=data,PLAYER_JS=bootstrap)
    template=(plugin/'template.html').read_text(encoding='utf-8').replace('id="replay-data" type="application/json"','id="replay-data" type="application/gzip-base64"')
    return re.sub(r'\{\{(TITLE|SUMMARY|OPTIONS|THREE_JS|REPLAY_DATA|PLAYER_JS)\}\}',lambda m:values[m[1]],template)


def embedded(path):
    match=re.search(r'<script id="replay-data"([^>]*)>(.*?)</script>',path.read_text(encoding='utf-8'),re.S)
    assert match,'missing embedded replay data'
    raw=gzip.decompress(base64.b64decode(match[2])) if 'application/gzip-base64' in match[1] else match[2]
    value=json.loads(raw)
    if value.pop('lazyEpisodes',False):
        value['episodes']=[json.loads(gzip.decompress(base64.b64decode(e['lazyData']))) for e in value['episodes']]
    return value


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rec-root',required=True,type=Path)
    parser.add_argument('--er-root',required=True,type=Path)
    parser.add_argument('--plugin-root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--ie-html',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    sets={}
    audit=[]
    for track,count,parent in [('rec',8,args.rec_root),('er',6,args.er_root)]:
        episodes=[]
        for i in range(1,count+1):
            version=f'{i:03d}-v02' if track=='er' and i==4 else f'{i:03d}-v01'
            candidate=parent/version
            record=read(candidate/'record.json')
            # Verify every original candidate asset against its preserved change record.
            changes={x['path']:x['new_sha256'] for x in record['validation']['changed_assets']}
            for rel,digest in record['baseline']['asset_sha256'].items():
                assert sha(candidate/'config/assets'/rel)==changes.get(rel,digest),(version,rel)
            summary=read(candidate/'results/run-01/summary.json')
            assert summary['collection_complete'] and len(summary['results'])==15
            rows=summary['results']
            assert {(r['policy'],r['seed']) for r in rows}=={(p,s) for p in ['idle','naive','coordinated','greedy','adaptive'] for s in [2026101503,2026101504,2026101505]}
            # Open with a competent active policy; retain every failed and idle episode.
            rows=sorted(rows,key=lambda r:(r['policy']!='adaptive',r['seed'],r['policy']))
            for row in rows:
                folder=candidate/'results/run-01'/f'case-{row["case_index"]:03d}'
                ep=episode(candidate,folder,row,record)
                episodes.append(ep)
                audit.append(dict(scenario=ep['meta']['scenario'],version=version,policy=row['policy'],seed=row['seed'],
                    source=str(folder),result_sha256=sha(folder/'result.json'),trace_sha256=row['trace_sha256'],
                    source_assets=ep['meta']['sourceFiles'],runtime_sha256=row['runtime_sha256'],frames=ep['checks']['traceRows'],
                    score=row['utility'],task_success=row['task_success']))
            print('Imported',record['scenario'],version,'15 / 15 verified',flush=True)
        sets[track]=pack(episodes, args.plugin_root)
        (output/(track+'-replays.pack.json.gz')).write_bytes(gzip.compress(encoded(sets[track]),mtime=0))
        (output/(track+'-replays.html')).write_text(render(sets[track],args.plugin_root,('侦察搜索' if track=='rec' else '应急响应')+' · 调优场景真实回放'),encoding='utf-8',newline='\n')
    if args.ie_html:
        combined=embedded(args.ie_html)
        assert len(combined['scenarios'])==14
        for key in ['rec','er']:
            combined['scenarios'].extend(sets[key]['scenarios'])
            combined['episodes'].extend(sets[key]['episodes'])
            combined.setdefault('geographies', {}).update(sets[key]['geographies'])
        combined['pluginVersion']=VERSION
        assert len({s['id'] for s in combined['scenarios']})==28
        (output/'all-28-scenes.html').write_text(render(combined,args.plugin_root,'IE · REC · ER / 28 场景真实回放'),encoding='utf-8',newline='\n')
    report=dict(schema_version='tuning-replay-import@1',scenarios=14,episodes=len(audit),
        policies=5,seeds=3,all_original_assets_and_trace_hashes_verified=True,
        adapter='standard-library JSON/HTML packaging; no product imports',simulations_started=0,
        server_browser_validation='not_run_connection_unavailable',records=audit)
    (output/'import-audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Completed',len(audit),'episodes;',output,flush=True)


if __name__=='__main__':
    main()
