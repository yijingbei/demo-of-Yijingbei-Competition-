"""Versioned nominal range envelopes; never import or execute the engine."""
import hashlib
import json
import math
from pathlib import Path

SCHEMA = 'competition-replay-ranges@1'


def unavailable(reason):
    return dict(schemaVersion=SCHEMA, status='unavailable', reason=reason, entities={})


def positive(value, label, zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (value < 0 if zero else value <= 0)):
        raise ValueError(f'Invalid {label}: {value!r}')
    return value


def load_ranges(source_root, scenario_id, scene):
    """Resolve exact mounted sensors and loadout weapons, including future spawns.

    These are catalogue values, not a reconstruction of weather, component state,
    visibility or detection probability at each tick. The caller binds the source.
    """
    if source_root is None:
        return unavailable('未提供对应版本资源目录')
    import yaml
    root = Path(source_root).resolve()
    suite_path = root/'scenarios/suite.json'
    if not suite_path.is_file():
        return unavailable('对应版本资源目录缺少 suite.json')
    suite = json.loads(suite_path.read_text(encoding='utf-8-sig'))
    row = next((r for r in suite['scenarios'] if r['id'] == scenario_id), None)
    if row is None:
        return unavailable('对应版本资源目录未包含此场景')
    catalog_path = (root/'scenarios/catalog'/(row['catalog']+'.yaml')).resolve()
    if not catalog_path.is_relative_to(root/'scenarios/catalog'):
        raise ValueError('Range catalog path escapes source root')
    raw = catalog_path.read_bytes()
    catalog = yaml.safe_load(raw.decode('utf-8-sig'))
    resources = {}
    for r in catalog['resources']:
        key = r['id']+'@'+r['version']
        if key in resources:
            raise ValueError(f'Duplicate range resource: {key}')
        resources[key] = r
    entities = {}; issues = []
    all_entities = list(scene['entities']) + [e['payload']['entity'] for e in scene.get('events', [])
                                             if e.get('event_type') == 'spawn']
    for e in all_entities:
        platform = resources.get(e.get('platform_ref'), {}).get('content', {})
        ptype = platform.get('platform_type', e.get('platform_ref', '')).lower()
        domain = platform.get('domain')
        category = ('uav' if domain == 'air' or 'uav' in ptype else
                    'usv' if domain == 'surface' and platform.get('mobile') or 'usv' in ptype else 'shore')
        mounted = list(e.get('component_refs', []))
        weapons = set()
        loadout_ref = e.get('loadout_ref')
        if loadout_ref:
            loadout = resources.get(loadout_ref)
            if loadout is None:
                issues.append(f'{e["id"]}: missing {loadout_ref}')
            else:
                weapons.update(loadout['content'].get('weapon_refs', []))
        profiles = []
        for ref in sorted(set(mounted) | weapons):
            resource = resources.get(ref)
            if resource is None:
                issues.append(f'{e["id"]}: missing {ref}')
                continue
            role = resource['resource_type']; c = resource['content']
            if role not in {'sensors', 'weapons'}:
                continue
            if role == 'sensors':
                radius = positive(c.get('range_m'), 'sensor range')
                kind = c.get('kind', 'sensor')
                item = dict(ref=ref, type='radar' if kind == 'radar' else 'sensor',
                            sensorKind=kind, category=category, maxM=radius, minM=0,
                            updateTicks=c.get('update_ticks', 1),
                            probability=c.get('detection_probability', c.get('probability', 1)),
                            targetDomains=c.get('target_domains', []))
                # Only explicit active altitude profiles have range semantics in v2.
                # Legacy low_altitude_range_m alone is not a second detection boundary.
                profile = c.get('altitude_range_profile')
                if profile:
                    item['altitudeProfile'] = dict(profile)
                    positive(profile['low_altitude_range_m'], 'low altitude range')
                    positive(profile['low_altitude_ceiling_m'], 'altitude ceiling', zero=True)
                visibility = c.get('visibility_profile')
                if visibility:
                    item['visibilityProfile'] = dict(visibility)
                    positive(visibility['mount_height_m'], 'sensor mount height', zero=True)
                    positive(visibility['earth_radius_m'], 'Earth radius')
                    positive(visibility['refraction_factor'], 'refraction factor')
                    item['targetHeightM'] = platform.get('sensing_height_m')
            else:
                maximum = positive(c.get('max_range_m', c.get('range_m')), 'weapon max range')
                minimum = positive(c.get('min_range_m', 0), 'weapon min range', zero=True)
                if minimum > maximum:
                    raise ValueError('Weapon minimum range exceeds maximum range')
                item = dict(ref=ref, type='weapon', category=category, minM=minimum, maxM=maximum,
                            targetDomains=c.get('target_domains', []))
                item['deliveryModel'] = c.get('delivery_model')
                missile = c.get('missile', {})
                if missile:
                    item['cruiseMps'] = positive(missile['cruise_speed_mps'], 'missile cruise speed')
                    item['launchMps'] = positive(missile['launch_speed_mps'], 'missile launch speed', zero=True)
            profiles.append(item)
        entities[e['id']] = profiles
    platforms = {ref:dict(domain=r['content'].get('domain'), mobile=r['content'].get('mobile',False))
                 for ref,r in resources.items() if r['resource_type']=='platforms'}
    return dict(schemaVersion=SCHEMA, status='partial' if issues else 'ready',
                source='scenario-catalog', catalog=row['catalog'],
                catalogSha256=hashlib.sha256(raw).hexdigest(), platforms=platforms,
                issues=issues, entities=entities)


def episode_ranges(captured, source_root, scenario_id, scene, expected_runtime):
    """Never reuse a newer catalogue for an old result, even under mismatch override."""
    if captured and captured.get('ranges') is not None:
        return {**captured['ranges'], 'binding': 'captured-scene'}
    if source_root is None or not expected_runtime:
        return unavailable('旧记录未保存范围资源；需要摘要匹配的原版本源码')
    from exporter import runtime_fingerprint
    if runtime_fingerprint(Path(source_root)) != expected_runtime:
        return unavailable('资源版本与本局摘要不匹配，未套用当前射程')
    return {**load_ranges(source_root, scenario_id, scene), 'binding': 'matching-runtime'}
