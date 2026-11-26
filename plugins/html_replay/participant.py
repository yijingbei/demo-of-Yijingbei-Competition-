"""Build a physically separate authorized-observation replay, without truth."""
from copy import deepcopy
import hashlib
import json
if __package__:
    from .weather_view import public_weather
else:
    from weather_view import public_weather


def public_task_fields(brief):
    """Keep only the recorded public task and the published fields it refers to."""
    card = {k: brief[k] for k in ('name', 'task', 'adjudication_tick', 'duration_ticks') if k in brief}
    for key, fields in (('protected_facilities', ('entity_id', 'weight', 'position_m')),
                        ('task_success_criteria', ('metric_id', 'comparison', 'value'))):
        if key in brief:
            card[key] = [{k: deepcopy(item[k]) for k in fields if k in item} for item in brief[key]]
    return card


def participant_episode(episode):
    source = episode
    side = source['meta']['participantSide']
    own = [deepcopy(e) for e in source['entities'] if e['side'] == side]
    ids = {e['id'] for e in own}
    meta_keys = ('track', 'scenario', 'seed', 'policy', 'status', 'end', 'nativeTickSeconds',
                 'participantSide', 'plannedThreats', 'combatUnits', 'facilityCount')
    meta = {k: deepcopy(source['meta'][k]) for k in meta_keys if k in source['meta']}
    meta.update(participantOnly=True, observationScope='participant-trace', runtimeSha256=None, score=None,
        success=None, finalNeutral=None, finalOwnActive=None,
        finalOperational=None, quietAfter=meta['end'] + 1,
        warnings=['仅包含本方授权范围内的观察、识别特征与己方状态，含结算期末状态；裁判真值在独立回放中。'],
        sourceVersion='authorized-observations-only', strategies={}, error=None,
        factionConvention=dict(colors={'red':'red'}, roles={'red':'defender'}, participant_faction_id='red'))
    if source['engagement']['contract'].get('schema_version')=='autonomous-contact-judgment@2':
        meta['warnings']=['仅本方实际授权观测、历史轨迹及己方状态；没有目标类别或阵营答案。裁判真值保存在独立文件。']
    recording = deepcopy(source.get('recording'))
    if recording:
        # Construct whitelisted frames. Do not retain mission checkpoints,
        # opposing controller reports, projectile target IDs or retired truth.
        recording['frames'] = [dict(tick=f['tick'], sim_time_s=f['sim_time_s'],
            entities=[deepcopy(e) for e in f['entities'] if e['entity_id'] in ids],
            observations=[deepcopy(o) for o in f.get('observations', []) if o['observer_faction_id'] == side],
            projectiles=[]) for f in source['recording']['frames']]
        for original, filtered in zip(source['recording']['frames'], recording['frames']):
            if 'weather' in original:
                filtered['weather'] = public_weather(original['weather'])
        recording.pop('views', None)
        original_metadata = recording['metadata']
        recording['metadata'] = dict(schema_version='authorized-replay-derived@1',
            frames=original_metadata['frames'], tick_seconds=original_metadata['tick_seconds'],
            complete=original_metadata.get('complete', False))
        recording['retired'] = {k:v for k,v in recording.get('retired', {}).items() if k in ids}
    # Judge labels include the final score; judge keys encode case/layout
    # numbers. Generate both from public content rather than copying them.
    public_samples = recording['frames'] if recording else source.get('perspectives', {}).get(side, [])
    digest = hashlib.sha256(json.dumps(public_samples, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    if recording:
        recording['metadata']['public_frames_sha256'] = digest
    meta['key'] = 'authorized-' + digest[:24]
    meta['label'] = (f"{source['public']['name']} · {meta.get('policy', 'reference')} · "
                     f"seed {meta.get('seed', 'unknown')} · 授权观察")
    # A camera fitted to all judge entities can reveal unseen positions.
    # Fit only public geography and owned tracks.
    points = [s[1:3] for e in own for s in e.get('samples', [])]
    points.extend(p for region in source.get('land', []) for p in region.get('coordinates_m', []))
    if points:
        xs = [p[0]/1000 for p in points]; zs = [-p[1]/1000 for p in points]
        meta['overview'] = dict(focus=[(min(xs)+max(xs))/2, 0, (min(zs)+max(zs))/2],
            span=max(22, (max(xs)-min(xs)+4)*1.25, (max(zs)-min(zs)+4)*1.6))
    else:
        meta['overview'] = dict(focus=[0,0,0], span=22)
    public = public_task_fields(source['public'])
    public_events = []
    for event in source['engagement']['events']:
        if (source['engagement']['contract'].get('schema_version')=='autonomous-contact-judgment@2'
                and event.get('attacker_faction')!=side and event.get('attacker_id') not in ids):
            continue
        keys = ('type', 'tick', 'action_id', 'contact_id', 'attacker_id', 'status',
                'fire_authorized', 'observed_features', 'evidence_tick', 'valid_until_tick', 'reidentified', 'shots', 'error_code', 'execution_tick')
        item = {key:deepcopy(event[key]) for key in keys if key in event}
        if event['type'] == 'third_party_damage':
            continue
        if event['type'] == 'attack_request':
            item['response'] = 'engagement.authorization_required' if event.get('violation',False) else 'native_submitted'
        public_events.append(item)
    return dict(meta=meta, entities=own, shots=[], hits=[], events=[], metrics=[],
        **(dict(weather={key:[public_weather(p) for p in source['weather'].get(key, [])]
                         for key in ('schedule','timeline')}) if 'weather' in source else {}),
        land=deepcopy(source.get('land', [])), zones=deepcopy(source.get('zones', [])),
        geography=deepcopy(source.get('geography')), recording=recording,
        perspectives={side:deepcopy(source.get('perspectives', {}).get(side, []))},
        mission=None, rangeInfo={}, terrain=deepcopy(source.get('terrain')), public=public,
        engagement=dict(contract=deepcopy(source['engagement']['contract']), events=public_events, timeline=[], final=None),
        checks=dict(authorizedObservationsOnly=True, refereeTruthIncluded=False))
