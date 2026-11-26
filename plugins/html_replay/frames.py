"""Validate recorded frames and keep faction observations distinct from truth."""
import hashlib
import json
import math


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Replay frame requires a finite number')
    return value


def vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('Replay frame requires a three-coordinate position')
    return [finite(x) for x in value]


def validate_own(entity):
    vector(entity['position_m'])
    finite(entity['heading_deg']); finite(entity['health'])
    if 'energy_state' in entity:
        energy = entity['energy_state']
        if energy.get('unit') != 'normalized' or energy.get('capacity_semantics') != 'fraction_of_declared_capacity':
            raise ValueError('Replay energy requires normalized capacity semantics')
        if energy['value'] is not None and not 0 <= finite(energy['value']) <= 1:
            raise ValueError('Replay normalized energy is outside [0, 1]')
    for count in entity['ammunition'].values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError('Replay frame ammunition must be a nonnegative integer')
    if not isinstance(entity['lifecycle_state'], str):
        raise ValueError('Replay frame lifecycle_state missing')


def view_from_observations(observations, tick, colors):
    buckets = {}
    for obs in observations:
        if obs['tick'] != tick:
            raise ValueError('Replay observation tick differs from frame tick')
        if obs['observer_faction_id'] not in colors:
            raise ValueError('Replay observation faction is undeclared')
        side = colors[obs['observer_faction_id']]
        bucket = buckets.setdefault(side, dict(tick=tick, own={}, contacts={}))
        for e in obs.get('own_entities', []):
            validate_own(e)
            own={k:e[k] for k in ('entity_id','position_m','heading_deg','health','ammunition','lifecycle_state')}
            if 'energy_state' in e:
                own['energy_state'] = e['energy_state']
            previous = bucket['own'].setdefault(e['entity_id'], own)
            if previous != own:
                raise ValueError('Conflicting replay own observations')
        # Contact IDs remain opaque and controller scoped; never match to truth.
        for c in obs.get('organic_contacts', []) + obs.get('shared_contacts', []):
            vector(c['estimated_position_m'])
            key = str(obs.get('controller_slot_id', '')) + ':' + c['contact_id']
            bucket['contacts'][key] = dict(c, replay_contact_key=key)
    return {side: dict(tick=tick, own=list(b['own'].values()), contacts=list(b['contacts'].values()))
            for side, b in buckets.items()}


def load_frames(folder, result, entities, end, colors):
    from record_io import record_path, verify, text_lines
    marker=folder/'replay.storage.json'
    if marker.is_file() and json.loads(marker.read_text(encoding='utf-8')).get('state') in {'expired','expiring'}:
        raise ValueError('本局完整回放已按保留策略过期；成绩与行动日志仍保留，不能伪造逐帧轨迹')
    metadata = result.get('replay_recording')
    path = record_path(folder, metadata.get('file') if metadata else 'replay.frames.jsonl', 'frames')
    if not metadata:
        if path.exists() or (folder/'replay.frames.jsonl.gz').exists():
            raise ValueError('Replay frames exist without result-bound recording metadata')
        return None
    if metadata.get('file') != path.name or metadata.get('error') or not isinstance(metadata.get('sha256'),str) or len(metadata['sha256'])!=64:
        raise ValueError('Replay recording metadata invalid or records a failure')
    if metadata.get('schema_version') != 'competition-replay-frame@1' or metadata.get('every_tick') is not True:
        raise ValueError('Replay recording does not declare every-tick schema')
    try:
        verify(path,metadata.get('sha256'),metadata.get('storage_sha256'))
    except ValueError as error:
        raise ValueError('Replay frame SHA-256 mismatch: '+str(error)) from error
    frames = []; views = {'blue': [], 'red': []}; truth = {i: [] for i in entities}
    views.update({side:[] for side in sorted(set(colors.values()) - set(views))})
    previous = -1
    retired = {}
    previous_ids = set()
    for no, line in enumerate(text_lines(path), 1):
        if not line.strip():
            raise ValueError(f'Replay frame {no}: empty row')
        frame = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite replay frame')))
        if frame.get('schema_version') != 'competition-replay-frame@1':
            raise ValueError('Unsupported replay frame schema')
        tick = frame['tick']
        if isinstance(tick, bool) or not isinstance(tick, int) or tick != previous + 1 or tick > end:
            raise ValueError('Replay frame ticks must be contiguous from zero and within final_tick')
        if frame['scenario'] != result['scenario'] or frame['runtime_sha256'] != result.get('runtime_sha256'):
            raise ValueError('Replay frame scenario/runtime does not match result')
        dt = finite(frame['tick_seconds'])
        if dt <= 0 or dt != metadata['tick_seconds'] or not math.isclose(finite(frame['sim_time_s']), tick*dt, abs_tol=1e-9):
            raise ValueError('Replay frame simulation clock mismatch')
        ids = set()
        for e in frame['entities']:
            i = e['entity_id']
            if i not in entities or i in ids:
                raise ValueError('Replay frame contains unknown or duplicate entity')
            if e['faction_id'] != entities[i]['factionId']:
                raise ValueError('Replay frame entity faction mismatch')
            ids.add(i); validate_own(e); vector(e['velocity_mps'])
            truth[i].append([tick, *e['position_m'], e['heading_deg'], e['health'],
                             sum(e['ammunition'].values()), e['lifecycle_state']])
        for p in frame.get('projectiles', []):
            vector(p['position_m']); vector(p['velocity_mps'])
        if ids & retired.keys():
            raise ValueError('Replay frame resurrects a removed entity')
        for entity_id in previous_ids - ids:
            retired[entity_id] = tick
        previous_ids = ids
        for side, snapshot in view_from_observations(frame.get('observations', []), tick, colors).items():
            for own in snapshot['own']:
                if own['entity_id'] not in ids or entities[own['entity_id']]['side'] != side:
                    raise ValueError('Replay observation owns an unknown or opposing entity')
            views[side].append(snapshot)
        frames.append(frame); previous = tick
    if len(frames) != metadata['frames'] or previous != metadata['last_tick'] or not frames:
        raise ValueError('Replay frame count/final tick mismatch')
    if metadata.get('complete') and previous != end:
        raise ValueError('Complete replay recording does not reach final_tick')
    if result.get('status') == 'completed' and (not metadata.get('complete') or previous != end):
        raise ValueError('Completed run requires a complete every-tick replay recording')
    final = result.get('referee_final_entities', {})
    if previous == end:
        if result.get('status') == 'completed' and set(final) != previous_ids:
            raise ValueError('Replay final frame entity set differs from referee final snapshot')
        for e in frames[-1]['entities']:
            f = final.get(e['entity_id'])
            if f and (e['position_m'] != f['position_m'] or e['health'] != f['health'] or e['lifecycle_state'] != f['lifecycle']):
                raise ValueError('Replay final frame differs from referee final snapshot')
            if f and 'energy_state' in f and e.get('energy_state') != f['energy_state']:
                raise ValueError('Replay final energy differs from referee final snapshot')
    for i, samples in truth.items():
        if not samples:
            continue
        entity = entities[i]
        entity['samples'] = samples
        entity['states'] = [[s[0], s[5], s[7]] for s in samples]
        if i in retired:
            # Full authoritative frames record live membership at every tick.
            # Removal has an exact time, but supplies no new position or health.
            # Keep the last true point; append only the membership transition.
            entity['states'].append([retired[i], samples[-1][5], 'despawned'])
            entity['removedAtTick'] = retired[i]
        entity['source'] = 'recorded'
        entity.pop('nativeSampleTicks', None)
        entity['sampleGaps'] = []
    return dict(metadata=metadata, frames=frames, views=views,
                ticks=[f['tick'] for f in frames], exact=True, retired=retired)
