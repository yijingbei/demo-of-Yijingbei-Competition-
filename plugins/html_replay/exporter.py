"""Offline trace adapter. No engine imports, subprocesses, networking or simulation."""
from collections import Counter
import hashlib
from html import escape
import json
import math
import os
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
VERSION = '1.9.0-autonomous.6-viewfix.1-energy.1'
LABELS = {'facilities': '设施完整度', 'neutralization': '威胁中和',
          'mission': '防守任务完成', 'blue_win': '蓝方目标达成', 'stopped': '全部威胁停止',
          'protection': '保护目标', 'graded_protection': '分级保护',
          'survival': '己方存续', 'intercept_timeliness': '拦截及时性',
          'ammunition_efficiency': '弹药效率', 'recall': '发现比例', 'discovery': '目标发现',
          'coverage': '区域覆盖', 'freshness': '信息新鲜度', 'discovery_timeliness': '发现及时性',
          'information_timeliness': '信息及时性', 'delivered_recall': '报告投递覆盖',
          'arrival': '任务服务完成', 'timeliness': '响应时效', 'safety': '路线安全',
          'triage': '告警甄别', 'continuity': '持续监视', 'identity': '身份一致性'}
NAMES = {'red.base':'红方指挥所 / GX发射点', 'red.hy300':'红方HY300侦察机', 'red.dajiang':'红方大疆侦察机',
         'red.xunluo':'红方巡逻艇', 'red.tiance':'红方天策', 'red.radar-surface':'岸基对海雷达',
         'red.radar-air5':'岸基对空雷达5km', 'red.radar-air7':'岸基对空雷达7km', 'blue.hh-scout':'蓝方HH侦察机',
         'facility.command': '岸防指挥所', 'facility.berth': '港口码头',
         'facility.pier': '补给码头', 'facility.comms': '通信站',
         'facility.fuel': '燃料站', 'site.shore-radar': '岸基预警雷达'}


class ReplayError(ValueError):
    pass


def read_json(path):
    def invalid(value):
        raise ReplayError(f'{path}: non-finite JSON value {value}')
    return json.loads(path.read_text(encoding='utf-8-sig'), parse_constant=invalid)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReplayError(f'{label}: expected finite number')
    return value


def position(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ReplayError(f'Invalid position_m: {value!r}')
    return [number(v, 'position_m') for v in value]


def runtime_fingerprint(root):
    """Match integrity.py without importing engine/runtime or executing a test."""
    if not (root / 'engine/patches.json').is_file():
        return None
    files = {}
    excluded = {'__pycache__', '.git', '.venv', 'artifacts', 'dist', 'tests', 'docs'}
    groups = (('competition', root, {'omd_competition', 'scenarios', 'requirements-lock.txt', 'run.py'}),
              ('engine', root / 'engine/source_codes',
               {'openmdbench', 'catalog', 'scenarios', 'env', 'config', 'framework', 'model', 'assets'}))
    for prefix, folder, allowed in groups:
        for path in sorted(folder.rglob('*')):
            relative = path.relative_to(folder)
            if (not path.is_file() or any(p in excluded for p in relative.parts)
                    or relative.parts[0] not in allowed
                    or path.suffix not in {'.py', '.yaml', '.yml', '.json', '.txt'}):
                continue
            files[prefix + '/' + relative.as_posix()] = sha256(path)
    files['engine/patches.json'] = sha256(root / 'engine/patches.json')
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def discover(inputs):
    folders = set()
    for name in inputs:
        path = Path(name).resolve()
        if path.is_file() and path.name in {'result.json', 'trace.jsonl', 'trace.jsonl.gz'}:
            folders.add(path.parent)
        elif path.is_dir():
            folders.update(p.parent for p in path.rglob('result.json'))
        else:
            raise ReplayError(f'Not a result.json, trace.jsonl or test directory: {path}')
    if not folders:
        raise ReplayError('No result.json found. Keep result.json and trace.jsonl together.')
    return sorted(folders)


def scenario_folder(source, scenario):
    suite = read_json(source / 'scenarios/suite.json')
    row = next((s for s in suite['scenarios'] if s['id'] == scenario), None)
    if row is None:
        raise ReplayError(f'Scenario {scenario!r} is absent in {source}/scenarios/suite.json')
    folder = (source / row['package']).resolve()
    if not folder.is_relative_to(source.resolve()):
        raise ReplayError('Scenario package points outside source root')
    return folder


def engagement_side(event, colors, entities):
    """Use recorded ownership; ambiguous v2 damage has no blamed side."""
    if event['type'] == 'third_party_damage' and (
            'weapon_sources' in event or event.get('diagnostic_only')):
        owners = {source.get('faction_id') for source in event.get('weapon_sources', [])}
        if len(owners) == 1 and not event.get('mixed_causes'):
            return colors.get(next(iter(owners)), 'neutral')
        return 'neutral'
    faction = event.get('attacker_faction')
    if faction in colors:
        return colors[faction]
    attacker = entities.get(event.get('attacker_id'))
    if attacker:
        return attacker['side']
    # Historical identification audits recorded only red decisions. V2
    # diagnostics with missing ownership must never inherit that assumption.
    return 'neutral' if event.get('diagnostic_only') else 'red'


def export_case(folder, scene_folder, fingerprint=None, allow_source_mismatch=False, source_root=None):
    try:
        return _export_case(folder, scene_folder, fingerprint, allow_source_mismatch, source_root)
    except ReplayError:
        raise
    except (OSError, KeyError, TypeError, ValueError, IndexError, EOFError) as error:
        raise ReplayError(f'{folder.name}: incompatible or incomplete input: {error}') from error


def _export_case(folder, scene_folder, fingerprint, allow_source_mismatch, source_root):
    try:
        import yaml
    except ImportError as error:
        raise ReplayError('PyYAML required. Install project requirements or: python -m pip install PyYAML==6.0.3') from error
    result = read_json(folder / 'result.json')
    from bundle import recorded_scene
    captured = recorded_scene(folder, result)
    if captured:
        scene_folder = captured['folder']
        fingerprint = result.get('runtime_sha256')
    if result.get('schema_version') != 'competition-result@0.1':
        raise ReplayError('Expected schema_version=competition-result@0.1')
    warnings = []
    from record_io import trace_path, verify, open_text
    trace_file = trace_path(folder, result)
    try:
        actual_hash = verify(trace_file, None, result.get('trace_storage_sha256'))
    except ValueError as error:
        raise ReplayError('trace SHA-256 mismatch: '+str(error)) from error
    expected_hash = result.get('trace_sha256')
    if expected_hash and expected_hash != actual_hash:
        raise ReplayError(f'{folder.name}: trace SHA-256 mismatch; trace and result do not belong together')
    if not expected_hash:
        warnings.append('结果未记录轨迹摘要；只能计算 SHA-256，无法核对原结果。')
    expected_runtime = result.get('runtime_sha256')
    source_verified = bool(fingerprint and expected_runtime and fingerprint == expected_runtime)
    if fingerprint and expected_runtime and not source_verified and not allow_source_mismatch:
        raise ReplayError(f'{folder.name}: source runtime SHA-256 differs from result. Use the matching --source-root, '
                          'or explicitly --allow-source-mismatch for diagnostic viewing only.')
    if not source_verified:
        warnings.append('场景版本未被运行时摘要确认。部署 / 地图可能与实际运行不同，请使用同版本源码。')
    scene = yaml.safe_load((scene_folder / 'scenario.yaml').read_text(encoding='utf-8-sig'))['scenario']
    brief = read_json(scene_folder / 'public_brief.json')
    brief_id = brief.get('scenario_id', brief.get('id'))
    if brief_id and brief_id != result['scenario']:
        raise ReplayError(f'Scenario brief id {brief_id!r} differs from result {result["scenario"]!r}')
    status = result.get('status', 'unknown')
    if status != 'completed':
        warnings.append(f'本局状态 {status}；不是完整有效终局，不能作为正式得分或成败证据。')
    track = brief.get('track', 'interception' if result['scenario'].startswith('IE-') else 'unknown')
    from factions import convention
    from participant import public_task_fields
    factions = convention(scene, brief)
    participant = factions['participant_faction_id']
    own_side = factions['participant_side']
    public = dict(name=brief.get('name', scene.get('display_name', result['scenario'])),
                  task=brief.get('task'), opponent=brief.get('opponent_contract'))
    entities = {}
    from ranges import episode_ranges
    range_info = episode_ranges(captured, source_root, result['scenario'], scene, expected_runtime)
    # Catalogue domain and mobility define a platform, independent of its name.
    # The captured range record binds the exact catalogue bytes; newer catalogues
    # must never be silently applied to old recorded episodes.
    platform_content = dict(range_info.get('platforms',{}))
    if source_root and range_info.get('catalog') and range_info.get('catalogSha256'):
        catalog_root = Path(source_root).resolve()/'scenarios/catalog'
        catalog_path = (catalog_root/(range_info['catalog']+'.yaml')).resolve()
        if catalog_path.is_relative_to(catalog_root) and catalog_path.is_file():
            if sha256(catalog_path) == range_info['catalogSha256']:
                document = yaml.safe_load(catalog_path.read_text(encoding='utf-8-sig'))
                platform_content = {r['id']+'@'+r['version']:r['content']
                                    for r in document['resources'] if r['resource_type']=='platforms'}
    objective_cohort = next((set(m.get('plugin_parameters',{}).get('attacker_ids',()))
                             for m in scene.get('scoring',{}).get('metrics',[])
                             if m.get('id') == 'metric.neutralization'), None)
    energy_contract = brief.get('energy_contract') or {}
    energy_kinds = {
        's-series-energy@1': {kind for kind, field in [('air', 'uav'), ('surface', 'usv')]
                              if energy_contract.get(field)},
        'competition-energy@1': {'air'},
    }.get(energy_contract.get('schema_version'), set())

    def add(e, spawn=0):
        i = e['id']
        if i in entities:
            raise ReplayError(f'Duplicate entity id: {i}')
        st = e['initial_state']; tags = e.get('tags', [])
        side = factions['colors'][e['faction_id']]
        platform = e.get('platform_ref', '').lower()
        kind = 'air' if 'uav' in platform or 'uav' in tags else 'surface' if 'usv' in platform else 'facility'
        definition = platform_content.get(e.get('platform_ref'),{})
        if definition:
            kind = ('air' if definition.get('domain')=='air' else
                    'surface' if definition.get('domain')=='surface' and definition.get('mobile') else 'facility')
        name = e.get('display_name') or NAMES.get(i) or i
        health = number(st.get('health', 1), 'health')
        life = st.get('lifecycle_state', 'active')
        entities[i] = dict(id=i, name=name, short=name, side=side, factionId=e['faction_id'], kind=kind,
            energyTracked=kind in energy_kinds,
            decoy='decoy' in tags, armedThreat=(i in objective_cohort if objective_cohort is not None else
                track == 'interception' and side == 'blue' and 'decoy' not in tags and kind != 'facility'),
            protected=i in brief.get('facility_weights', {}) or (i.startswith('facility.') and side == 'red'),
            spawn=number(spawn, 'spawn'), source='anchors', initialAmmo=sum(e.get('ammunition', {}).values()),
            states=[[spawn, health, life]], samples=[], anchors={spawn: position(st['position_m'])}, sampleGaps=[],
            ranges=range_info.get('entities', {}).get(i, []))
    for e in scene['entities']:
        add(e)
    for event in scene.get('events', []):
        if event.get('event_type') == 'spawn':
            if 'tick' not in event.get('trigger', {}):
                raise ReplayError('Only tick-triggered spawns are supported by replay v1')
            add(event['payload']['entity'], event['trigger']['tick'])
    if not entities:
        raise ReplayError('Scenario has no entities to display')
    shots = []; hits = []; events = []; launches = {}; sample_counts = Counter()
    rows = 0; observed_end = 0; rejection_count = 0; unknown = set()
    score_timeline = []
    observation_rows = {}
    engagement_events = []
    engagement_timeline = []
    with open_text(trace_file) as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, parse_constant=lambda v: number(float(v), 'trace'))
            except (ValueError, ReplayError) as error:
                raise ReplayError(f'{folder.name}/trace.jsonl line {line_no}: {error}') from error
            trow = number(row['tick'], 'trace tick')
            if trow < observed_end:
                raise ReplayError(f'trace line {line_no}: tick order decreases')
            observed_end = trow; rows += 1
            if trow <= 1:
                for slot in row.get('participants', []):
                    request = slot.get('request') or {}
                    if (request.get('type') == 'reset' and
                            request.get('observation', {}).get('observer_faction_id') == participant):
                        public.update(public_task_fields(request.get('public_brief') or {}))
                        break
            if 'engagement_audit' in row:
                engagement_timeline.append(dict(t=trow, audit=row['engagement_audit'],
                    adjustment=row.get('score', {}).get('engagement_adjustment'),
                    points=100 * row['score']['total']))
                for event in row.get('engagement_events', []):
                    engagement_events.append(event)
                    # Keep every refresh in the evidence log without flooding the
                    # navigation timeline with per-measurement milestones.
                    if event['type'] == 'evidence_refresh':
                        continue
                    labels = {'identification':'设备特征识别', 'attack_request':'攻击请求',
                              'identification_expired':'识别证据过期',
                              'native_fire_result':'原生射击回执',
                              'action_feedback_delivered':'执行回执送达本方',
                              'attack_executed':'实际执行攻击', 'third_party_damage':'第三方损伤归因'}
                    events.append(dict(t=event['tick'], type='engagement',
                        side=engagement_side(event, factions['colors'], entities),
                        a=event.get('attacker_id') or event.get('target_entity_id'),
                        title=labels[event['type']] + (' · 违规' if event.get('violation') else ''),
                        detail=json.dumps(event, ensure_ascii=False)))
            if row.get('score'):
                score_timeline.append(dict(row['score'], tick=row['score'].get('tick', trow)))
            observation_inputs = [(participant.get('request') or {}).get('observation')
                or participant.get('observation') or {}
                for participant in row.get('participants', []) + row.get('opponents', [])]
            observation_inputs.extend(row.get('opponent_observations', []))
            for obs in observation_inputs:
                if obs.get('own_entities') is not None and 'tick' in obs:
                    observation_rows.setdefault(obs['tick'], []).append(dict(obs,
                        observer_faction_id=obs.get('observer_faction_id', participant)))
                for e in obs.get('own_entities', []):
                    ent = entities.get(e['entity_id'])
                    if ent is None:
                        unknown.add(e['entity_id']); continue
                    t = number(obs['tick'], 'observation tick')
                    sample = [t, *position(e['position_m']), number(e.get('heading_deg', 0), 'heading'),
                              number(e.get('health', 1), 'health'), sum(e.get('ammunition', {}).values()), e['lifecycle_state']]
                    if ent['samples'] and ent['samples'][-1][0] == t:
                        if ent['samples'][-1] != sample:
                            raise ReplayError(f'Conflicting observations for {ent["id"]} at tick {t}')
                    else:
                        ent['samples'].append(sample); sample_counts[ent['id']] += 1
                        ent['states'].append([t, sample[5], sample[7]])
                        ent['source'] = 'native'
            for combat in row.get('combat_receipts', []):
                ex = combat.get('execution')
                if not ex or combat.get('status') != 'executed':
                    rejection_count += 1
                    events.append(dict(t=trow, type='diagnostic', side=None, title='射击未执行',
                                       detail=str(combat.get('reason', combat.get('status', 'unknown')))[:400]))
                    continue
                t = number(ex['tick'], 'launch tick'); attacker = ex['attacker_id']; target = ex['target_id']
                if attacker not in entities or target not in entities:
                    raise ReplayError(f'Shot references unknown entity: {attacker} / {target}')
                origin = position(ex['launch_position_m']); toward = position(ex['target_position_at_launch_m'])
                entities[attacker]['anchors'][t] = origin; entities[target]['anchors'][t] = toward
                ids = ex.get('launched_missile_ids', [])
                pending_ids = ex.get('pending_impact_ids', [])
                for mid in [*ids, *pending_ids]:
                    if mid in launches:
                        raise ReplayError(f'Duplicate missile id: {mid}')
                    shot = dict(id=mid, t=t, a=attacker, b=target, **{'from': origin}, toward=toward,
                                side=entities[attacker]['side'], weapon=ex.get('weapon_ref', ''), end=None)
                    if mid in pending_ids:
                        shot['kind'] = 'delayed_effect'
                    launches[mid] = shot; shots.append(shot)
                events.append(dict(t=t, type='launch', side=entities[attacker]['side'], a=attacker, b=target,
                                   title=entities[attacker]['name'] + ('引信启动' if pending_ids and not ids else '发射'),
                                   detail='目标：' + entities[target]['name'], shots=len(ids)+len(pending_ids)))
            for receipt in row.get('damage_receipts', []):
                self_destructed = {intent['target_entity_id'] for intent in receipt.get('applied_intents', [])
                                   if intent.get('parameters', {}).get('self_destruction') is True}
                for intent in receipt.get('applied_intents', []):
                    par = intent.get('parameters', {}); mid = par.get('missile_id') or par.get('pending_impact_id'); target = intent['target_entity_id']
                    t = number(intent['tick'], 'damage tick') + number(par.get('closest_approach_time_fraction', 1), 'impact fraction')
                    pos = par.get('target_position_m') or par.get('impact_position_m')
                    if target in entities and pos is not None:
                        entities[target]['anchors'][t] = position(pos)
                        hits.append(dict(t=t, target=target, position=position(pos), missile=mid))
                    if mid in launches:
                        impact = par.get('impact_position_m') or pos
                        if impact is not None:
                            launches[mid].update(end=t, impact=position(impact))
                        else:
                            launches[mid]['resolutionTick'] = t
                for change in receipt.get('results', []):
                    target = change['target_entity_id']; t = number(change['tick'], 'damage result tick') + 1
                    if target not in entities:
                        unknown.add(target); continue
                    e = entities[target]
                    e['states'].append([t, number(change['health_after'], 'health'), change['lifecycle_after']])
                    damage = e['protected'] and change['health_after'] < change['health_before']
                    if damage or change['lifecycle_before'] != change['lifecycle_after']:
                        kind = 'detonation' if target in self_destructed else 'facility-hit' if damage else 'neutralized' if e['armedThreat'] else 'decoy' if e['decoy'] else 'loss'
                        label = {'active': '受损', 'disabled': '失能', 'destroyed': '被摧毁'}.get(change['lifecycle_after'], change['lifecycle_after'])
                        if target in self_destructed:
                            label = '自爆'
                        events.append(dict(t=t, type=kind, side=e['side'], a=target, title=e['name'] + label,
                                           detail=f'生命值 {change["health_after"] * 100:.0f}% · 裁判毁伤记录'))
    if not rows:
        if not result.get('replay_recording', {}).get('frames'):
            raise ReplayError('Trace is empty; no recorded simulation can be replayed')
        warnings.append('没有已执行决策；仅显示已绑定的初始 / 中断帧，不能视为完成实验。')
    if unknown:
        raise ReplayError('Trace entities absent from supplied scenario: ' + ', '.join(sorted(unknown)))
    end = number(result.get('final_tick', observed_end), 'final tick')
    end = max(1, end)
    if observed_end > end:
        raise ReplayError('Trace continues beyond result.final_tick')
    final = result.get('referee_final_entities', {})
    if set(final) - set(entities):
        raise ReplayError('Final snapshot contains entities absent from scenario; check source version')
    if status == 'completed' and ('referee_final_entities' not in result
                                  or not final and not result.get('replay_recording')):
        raise ReplayError('Completed result lacks referee_final_entities')
    if not final:
        warnings.append('无终局裁判快照；末位置 / 生命值为最后已知记录，未观测实体可能只有初始锚点。')
    for i, e in entities.items():
        f = final.get(i)
        if f:
            e['anchors'][end] = position(f['position_m'])
            e['states'].append([end, number(f['health'], 'final health'), f['lifecycle']])
        # Last receipt at a timestamp takes precedence; no fabricated state transitions.
        state_map = {s[0]: s for s in e['states']}
        e['states'] = [state_map[t] for t in sorted(state_map) if t <= end or e['spawn'] > end]
        if e['source'] == 'native':
            e['nativeSampleTicks'] = [sample[0] for sample in e['samples']]
            e['lastNativeTick'] = e['samples'][-1][0]
            first = e['samples'][0]
            if first[0] > e['spawn']:
                e['samples'].insert(0, [e['spawn'], *e['anchors'][e['spawn']], first[4], e['states'][0][1], e['initialAmmo'], e['states'][0][2]])
            if f:
                last = e['samples'][-1]
                terminal = [end, *f['position_m'], last[4], f['health'], last[6], f['lifecycle']]
                if last[0] == end:
                    e['samples'][-1] = terminal
                else:
                    e['samples'].append(terminal)
            for x, y in zip(e['samples'], e['samples'][1:]):
                if y[0] - x[0] > 1:
                    e['sampleGaps'].append([x[0], y[0]])
            if e['lastNativeTick'] < end:
                e['sampleGaps'].append([e['lastNativeTick'], end])
        else:
            e['samples'] = [[t, *p] for t, p in sorted(e['anchors'].items())]
        e.pop('anchors')
        if [s[0] for s in e['samples']] != sorted({s[0] for s in e['samples']}):
            raise ReplayError(f'Unordered / duplicate samples for {i}')
        if e['spawn'] and e['spawn'] <= end:
            events.append(dict(t=e['spawn'], type='spawn', side=e['side'], a=i, title=e['name'] + '入场', detail='场景预定波次'))
    from frames import load_frames
    recording = load_frames(folder, result, entities, end, factions['colors'])
    for i,e in entities.items():
        if status == 'completed' and i not in final and e['spawn'] <= end:
            if not recording or i not in recording['retired']:
                # Native self-destruction can remove an entity from the final
                # world. Accept its already-recorded terminal state; never
                # infer retirement merely from missing observations.
                terminal_state = e['states'][-1]
                if terminal_state[1] != 0 or terminal_state[2] not in {'destroyed', 'despawned', 'wreck'}:
                    raise ReplayError(f'Completed result lacks final entity {i}; check scenario version')
                warnings.append(f'{i}: absent from final snapshot; using recorded terminal state at tick {terminal_state[0]}.')
    from frames import view_from_observations
    perspectives = {'blue': [], 'red': []}
    perspectives.update({side:[] for side in sorted(set(factions['colors'].values()) - set(perspectives))})
    for tick, observations in sorted(observation_rows.items()):
        for side, snapshot in view_from_observations(observations, tick, factions['colors']).items():
            perspectives[side].append(snapshot)
    if recording:
        perspectives = recording['views']
    if not recording:
        warnings.append('历史日志没有完整逐 tick 真值；锚点插值不能当作逐帧真实轨迹。敌方观测视角可能不可用。')
    threats = [e for e in entities.values() if e['armedThreat']]
    facilities = [e for e in entities.values() if e['protected']]
    own = [e for e in entities.values() if e['side'] == own_side and e['kind'] != 'facility']
    def active(e):
        return e['spawn'] <= end and e['states'][-1][2] in {'active', 'degraded'}
    neutral = sum(e['spawn'] <= end and not active(e) for e in threats)
    operational = sum(active(e) for e in facilities); own_active = sum(active(e) for e in own)
    success = result.get('task_success') if status == 'completed' else None
    score = number(result['utility'], 'utility') * 100 if result.get('utility') is not None else None
    metrics = [dict(id=m['metric_id'], name=LABELS.get(m['metric_id'].split('.')[-1], m['metric_id']),
                    weight=number(m['effective_weight'], 'metric weight'), value=number(m['utility_value'], 'metric value'),
                    points=number(m['weighted_contribution'], 'metric contribution') * 100)
               for m in result.get('score_audit', {}).get('metric_receipts', []) if m.get('effective_weight', 0) > 0]
    adjustment = result.get('score_audit', {}).get('engagement_adjustment')
    if adjustment is not None:
        base = 100 * number(adjustment['base_total'], 'base score')
        penalty = (len(adjustment['violated_entity_ids']) * adjustment['violation_penalty_points']
                   + len(adjustment['attributable_third_party_loss_ids']) * adjustment['third_party_loss_penalty_points'])
        expected_points = max(0., base - penalty)
        if score is not None and not math.isclose(score, expected_points, abs_tol=1e-7):
            raise ReplayError('Engagement penalty differs from result.utility')
        metrics.append(dict(id='engagement.penalty', name='交战合规扣分（最低0分）',
                            weight=0., value=1., points=-min(base, penalty)))
    score_verified = bool(metrics and score is not None)
    if score_verified and not math.isclose(sum(m['points'] for m in metrics), score, abs_tol=1e-7):
        if status == 'completed':
            raise ReplayError('Score audit contributions differ from result.utility')
        score_verified = False
        warnings.append('异常 / 截断局的结果效用与最后一次阶段评分不同；只显示原值，不作为有效成绩。')
    if not score_verified:
        warnings.append('缺少完整评分凭据；得分只展示原结果，不声称已核对分项。')
    audit = result.get('combat_audit')
    count_verified = audit is not None
    if count_verified and len(shots) != sum(v.get('shots', 0) for v in audit.values()):
        raise ReplayError('Launch count differs from result.combat_audit')
    if not count_verified:
        warnings.append('无 combat_audit；发射数仅依据轨迹记录。')
    last_event = max([0] + [e['t'] for e in events])
    events.append(dict(t=end, type='terminal', side=own_side if success else ('blue' if own_side=='red' else 'red'),
                       title=('任务成功' if success else '任务未成功' if success is False else '测试记录结束') + ' · ' + status,
                       detail=f'已停止活动的武装威胁 {neutral}/{len(threats)}（含自爆）；最后已知可用设施 {operational}/{len(facilities)}'))
    events.sort(key=lambda e: e['t']); shots.sort(key=lambda s: s['t'])
    navigation = brief.get('navigation_map', {})
    from geography import load_geography
    geography = captured['geography'] if captured else load_geography(source_root, result['scenario'], scene.get('world',{}).get('map_ref'))
    if scene.get('world',{}).get('map_ref') and not geography:
        warnings.append('未取得地图 visualization_source，不绘制海岸线；场景碰撞多边形不作为海岸显示。请指定完整 --source-root。')
    land = navigation.get('land_regions', [])
    for region in land:
        if region.get('geometry_type', 'polygon') != 'polygon' or len(region['coordinates_m']) < 3:
            raise ReplayError('Replay coastlines require polygon land_regions with at least three vertices')
        for p in region['coordinates_m']:
            if len(p) < 2:
                raise ReplayError('Invalid land region vertex')
            number(p[0], 'land x'); number(p[1], 'land y')
    from tasks import task_data
    mission = task_data(scene, brief, recording, observation_rows, score_timeline, factions['colors'])
    coordinates = [s[1:4] for e in entities.values() for s in e['samples']]
    coordinates += [[p[0], p[1], 0] for region in land for p in region['coordinates_m']]
    coordinates += [[p[0], p[1], 0] for region in mission['zones'] for p in region['coordinates_m']]
    xs = [p[0] / 1000 for p in coordinates]; zs = [-p[1] / 1000 for p in coordinates]
    padding = 4 if track == 'interception' else .6
    overview = dict(focus=[(min(xs)+max(xs))/2, 0, (min(zs)+max(zs))/2],
                    span=max(22 if track == 'interception' else 3, (max(xs)-min(xs)+padding)*1.25, (max(zs)-min(zs)+padding)*1.6))
    if rejection_count:
        warnings.append(f'轨迹包含 {rejection_count} 条未执行射击，详见事件列表。')
    if result.get('decision_timeouts'):
        warnings.append(f'决策超时 {result["decision_timeouts"]} 次。')
    if status == 'completed' and success is None:
        warnings.append('完整结果未记录 task_success，不能据连续得分推断成功。')
    events.extend(mission['events'])
    events.sort(key=lambda e: e['t'])
    meta = dict(key=result['scenario'] + '-' + str(result.get('seed')) + '-' + folder.name, track=track,
        factionConvention=factions, participantSide=own_side,
        scenario=result['scenario'], seed=result.get('seed'), policy=result.get('policy', 'unknown'),
        score=score, end=end, success=success, status=status, terminal=result.get('terminal'),
        nativeTickSeconds=number(scene.get('world',{}).get('tick_seconds',1), 'tick seconds'),
        traceSha256=actual_hash, engineCommit=result.get('engine_commit'), runtimeSha256=expected_runtime,
        campaign=folder.parent.name, case=folder.name, casePath=folder.name,
        sourceVersion=str(result.get('package_version', 'unknown')) + ' / ' + str(expected_runtime or 'unverified')[:12],
        sourceFiles={p.name: sha256(p) for p in scene_folder.iterdir() if p.name in {'scenario.yaml', 'public_brief.json', 'opponent.json'}},
        plannedThreats=len(threats), plannedDecoys=sum(e['decoy'] for e in entities.values()),
        combatUnits=len(own), facilityCount=len(facilities), finalNeutral=neutral,
        finalOwnActive=own_active, finalOperational=operational, lastCombatEvent=last_event, quietAfter=last_event+18,
        blueShots=sum(s['side'] == 'blue' for s in shots), redShots=sum(s['side'] == 'red' for s in shots),
        overview=overview, facilityDamaged=any(e['type'] == 'facility-hit' for e in events) or any(e['states'][-1][1] < 1 for e in facilities),
        warnings=warnings, combatRejections=result.get('combat_rejections'),
        decisionTimeouts=result.get('decision_timeouts', 0), error=result.get('error'))
    score_label = f'{score:.2f}' if score is not None else '未结算'
    meta['label'] = f'{result["scenario"]} · {meta["policy"]} · seed {meta["seed"]} · {status} · {score_label}'
    from presentation import strategy_metadata, terminal_reason
    from weather_view import weather_episode
    meta['verdictReason'] = terminal_reason(result)
    weather = weather_episode(brief, recording)
    opponent = None; opponent_source = None
    if captured and 'opponent.json' in read_json(captured['folder']/'manifest.json')['files']:
        opponent = read_json(captured['folder']/'opponent.json')
        opponent_source = 'bound replay.scene/opponent.json'
    elif source_root and expected_runtime and runtime_fingerprint(Path(source_root)) == expected_runtime:
        try:
            opponent_path = scenario_folder(Path(source_root), result['scenario'])/'opponent.json'
            if opponent_path.is_file():
                opponent = read_json(opponent_path)
                opponent_source = 'runtime-hash matched opponent.json'
        except ReplayError:
            pass
    meta['strategies'] = strategy_metadata(folder, result, scene, brief, own_side, opponent, opponent_source)
    if track != 'interception':
        meta['quietAfter'] = end+1
        events[-1]['detail'] = '任务成败与得分均读取原始裁判；任务面板显示当时的投递记录与指标。'
    return dict(meta=meta, entities=list(entities.values()), shots=shots, hits=hits, events=events, metrics=metrics,
                **(dict(weather=weather) if weather is not None else {}),
                **(dict(engagement=dict(contract=brief.get('contact_judgment_contract', brief.get('identification_contract')), events=engagement_events,
                    timeline=engagement_timeline, final=result.get('engagement_audit')))
                    if 'identification_contract' in brief or 'contact_judgment_contract' in brief else {}),
                land=land, zones=navigation.get('protected_regions', []),
                geography=geography, recording=recording, perspectives=perspectives, mission=mission,
                rangeInfo={k:v for k,v in range_info.items() if k != 'entities'},
                terrain=dict(coastlineSource='map.visualization_source + navigation_map.land_regions', coastlineExact=True,
                             shelfSource='distance-to-coast schematic', shelfWidthMeters=1400,
                             bathymetryAvailable=False),
                public=public,
                checks=dict(traceHashVerified=bool(expected_hash), scoreVerified=score_verified,
                            mapVisualizationHashVerified=bool(geography),
                            sourceVersionVerified=source_verified, launchCountVerified=count_verified,
                            finalCoordinatesVerified=bool(final), traceRows=rows,
                            nativeSamplesPreserved=dict(sample_counts),
                            replayFrameHashVerified=bool(recording),
                            capturedSceneVerified=bool(captured),
                            everyTickRecorded=bool(recording and recording['metadata'].get('complete'))))


def make_pack(episodes, skipped=None):
    scenarios = []
    geographies = {}; packed_episodes = []
    for episode in episodes:
        packed = dict(episode)
        if packed.get('recording'):
            packed['recording']={k:v for k,v in packed['recording'].items() if k!='views'}
        geometry = packed.pop('geography', None)
        if geometry:
            key = geometry['mapRef']+'/'+geometry['sourceSha256']
            existing = geographies.setdefault(key, geometry)
            if existing != geometry:
                raise ReplayError('Conflicting map layers share the same source identity')
            packed['geographyRef'] = key
        packed_episodes.append(packed)
    for sid in dict.fromkeys(e['meta']['scenario'] for e in episodes):
        group = [e for e in episodes if e['meta']['scenario'] == sid]
        name = group[0]['public']['name']
        versions = len({e['meta']['runtimeSha256'] for e in group})
        scenarios.append(dict(id=sid, name=name, tag=f'{len(group)} 局测试', short='策略 / 种子与原始结果对照',
                              description=f'{sid} · {name}' + (' · 含不同运行时版本，请勿直接比较得分' if versions > 1 else '')))
    return dict(schema='competition-multiscene-replay@1', pluginVersion=VERSION,
                scenarios=scenarios, episodes=packed_episodes, geographies=geographies, skipped=skipped or [])


def embedded_pack(html):
    """Read either plain or gzip-compressed data from an exported offline HTML."""
    import base64
    import gzip
    import re
    match=re.search(r'<script id="replay-data"([^>]*)>(.*?)</script>',html,re.S)
    if not match:raise ReplayError('HTML has no embedded replay data')
    raw=match[2]
    if 'application/gzip-base64' in match[1]:
        raw=gzip.decompress(base64.b64decode(raw,validate=True)).decode('utf-8')
    pack = json.loads(raw)
    if pack.pop('lazyEpisodes', False):
        import base64
        import gzip
        pack['episodes'] = [json.loads(gzip.decompress(base64.b64decode(e['lazyData']))) for e in pack['episodes']]
    return pack


def render_html(pack, title='测试对局 · 交互回放', compress=None, lazy=None):
    if not pack['episodes']:
        raise ReplayError('No replayable episodes')
    if lazy is None and len(pack['episodes']) > 1 and sum(len(e.get('recording', {}).get('frames', [])) if e.get('recording') else 0 for e in pack['episodes']) > 2500:
        lazy = True
    if lazy is not True:
        data = json.dumps(pack, ensure_ascii=False, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
        if lazy is None:
            lazy = len(pack['episodes']) > 1 and len(data.encode('utf-8')) > 40_000_000
    if lazy:
        import base64
        import gzip
        shells = []
        for episode in pack['episodes']:
            raw = json.dumps(episode, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            shells.append(dict(meta=episode['meta'], public=episode['public'],
                lazyData=base64.b64encode(gzip.compress(raw, mtime=0)).decode('ascii')))
        data = json.dumps(dict(pack, episodes=shells, lazyEpisodes=True), ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).replace('<', '\\u003c')
    from presentation import TERMS, display_text
    player='\n'.join((HERE / name).read_text(encoding='utf-8') for name in ('presentation.js','controls.js','tasks.js','ranges.js','player.js','coordinates.js'))
    player=player.replace('__DISPLAY_TERMS__', json.dumps(sorted(TERMS.items(),key=lambda p:len(p[0]),reverse=True),ensure_ascii=False))
    compressed=compress if compress is not None else len(data.encode('utf-8'))>8_000_000
    if compressed:
        import base64
        import gzip
        data=base64.b64encode(gzip.compress(data.encode('utf-8'),mtime=0)).decode('ascii')
    if compressed or lazy or pack.get('lazyEpisodes'):
        script=json.dumps(player,ensure_ascii=False).replace('<','\\u003c')
        player="""(async()=>{
 const node=document.getElementById('replay-data');
 const inflate=async text=>{const bytes=Uint8Array.from(atob(text.trim()),c=>c.charCodeAt(0));return await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text()};
 if(node.type==='application/gzip-base64')node.textContent=await inflate(node.textContent);
 const pack=JSON.parse(node.textContent);
 if(pack.lazyEpisodes){const first=pack.episodes[0];pack.episodes[0]={...JSON.parse(await inflate(first.lazyData)),lazyData:first.lazyData};node.textContent=JSON.stringify(pack)}
 node.type='application/json';const script=document.createElement('script');script.textContent=PLAYER_SOURCE;document.body.appendChild(script);
})().catch(error=>{const message=document.createElement('div');message.className='fallback';message.textContent='回放数据加载失败，请使用新版 Edge 或 Chrome。'+error.message;document.getElementById('viewport').appendChild(message);console.error(error)});""".replace('PLAYER_SOURCE',script)
    values = dict(TITLE=escape(display_text(title)), SUMMARY=f'{len(pack["scenarios"])} 个场景 · {len(pack["episodes"])} 局测试 · 原始记录与诊断',
                  OPTIONS=''.join(f'<option value="{i}">{escape(display_text(e["meta"]["label"]))}</option>' for i, e in enumerate(pack['episodes'])),
                  THREE_JS=(HERE / 'vendor/three.min.js').read_text(encoding='utf-8'), REPLAY_DATA=data,
                  PLAYER_JS=player)
    import re
    template = display_text((HERE / 'template.html').read_text(encoding='utf-8'), english=False)
    if compressed:template=template.replace('id="replay-data" type="application/json"','id="replay-data" type="application/gzip-base64"')
    # One pass: user text containing placeholder-like strings is never interpreted.
    return re.sub(r'\{\{(TITLE|SUMMARY|OPTIONS|THREE_JS|REPLAY_DATA|PLAYER_JS)\}\}', lambda m: values[m[1]], template)


def write_html(path, html, force=False):
    path = path.resolve()
    if path.suffix.lower() != '.html':
        raise ReplayError('Output must have .html extension')
    if path.exists() and not force:
        raise ReplayError(f'Output already exists: {path}. Choose a new name or use --force.')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.replay-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(html)
        os.replace(tmp, path)
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink()
