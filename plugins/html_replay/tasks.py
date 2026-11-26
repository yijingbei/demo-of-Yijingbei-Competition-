"""Task display evidence for every track. Never recompute competition scores."""
import json


def task_data(scene, brief, recording, observation_rows, scores, colors):
    zones = {}

    def zone(value, kind, tick=0):
        if not isinstance(value, dict) or value.get('geometry_type', 'polygon') != 'polygon':
            return
        if len(value.get('coordinates_m', [])) < 3:
            return
        key = value.get('id', str(len(zones)))
        old = zones.get(key)
        if old is None or old['availableTick'] > tick:
            zones[key] = dict(value, kind=kind, availableTick=tick)

    for z in brief.get('search_zones', []): zone(z, 'search')
    for sector in brief.get('search_sectors', []):
        for z in sector.get('cells', []): zone(z, 'search')
    for z in brief.get('risk_regions', []): zone(z, 'risk')
    for task in brief.get('initial_requests', []): zone(task.get('destination'), 'service')
    for mission in brief.get('standing_missions', []): zone(mission.get('search_region'), 'watch')
    frames = recording['frames'] if recording else []
    observations = [(frame['tick'], obs) for frame in frames for obs in frame.get('observations', [])]
    if not frames:
        observations = [(tick, obs) for tick, rows in sorted(observation_rows.items()) for obs in rows]
    messages = {}; events = []
    for tick, obs in observations:
        for raw in obs.get('received_messages', []):
            delivered = raw.get('delivered_tick')
            if type(delivered) is not int or not 0 <= delivered <= tick:
                continue
            key = (obs.get('controller_slot_id'), raw.get('message_id'))
            try:
                body = json.loads(raw.get('payload', ''))
            except (ValueError, TypeError):
                body = None
            body = body if isinstance(body, dict) else {}
            message = dict(tick=delivered, controller=obs.get('controller_slot_id'),
                recipientFaction=obs.get('observer_faction_id'),
                side=colors[obs['observer_faction_id']],
                sender=raw.get('sender_entity_id'), id=raw.get('message_id'),
                sentTick=raw.get('sent_tick'), payload=raw.get('payload'), body=body,
                trustedSource=raw.get('sender_entity_id') in brief.get('trusted_dispatch_sources', []))
            if key in messages:
                if messages[key] != message:
                    raise ValueError('Conflicting recorded delivered message')
                continue
            messages[key] = message
            protocol = body.get('schema_version', '')
            is_dispatch = protocol == 'dispatch-task@1.0'
            if is_dispatch:
                title = '收到任务通知 · '+str(body.get('request_id', ''))+' · '+str(body.get('status', ''))
                zone(body.get('destination'), 'service', delivered)
                for z in body.get('caution_regions', []): zone(z, 'risk', delivered)
            else:
                title = '报告 / 消息实际投递'
            events.append(dict(t=delivered, type='dispatch' if is_dispatch else 'message',
                side=message['side'], a=message['sender'], title=title,
                detail=str(message['sender'])+' → '+str(message['controller'])+'；实际收件凭据，投递不等于裁判验收。'))
    scoring = []
    for frame in frames:
        mission = frame.get('mission', {})
        if mission.get('score_state'):
            scoring.append(dict(tick=frame['tick'], values=mission['score_state']))
    if not scoring:
        scoring = [dict(tick=s['tick'], values={m['metric_id']:m.get('value') for m in s.get('metrics', [])}) for s in scores]
    return dict(track=brief.get('track'), task=brief.get('task'),
        initialRequests=brief.get('initial_requests', []), standingMissions=brief.get('standing_missions', []),
        zones=list(zones.values()), messages=sorted(messages.values(), key=lambda m:m['tick']),
        scores=scoring, metricNames={m['id']:m.get('display_name', m['id']) for m in scene.get('scoring', {}).get('metrics', [])},
        events=events, notice='投递与区域停留用于检查过程；任务完成和得分以裁判指标为准，抵达不等于物理救援。')
