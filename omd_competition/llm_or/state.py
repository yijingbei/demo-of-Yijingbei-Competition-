"""Participant-only state and per-weapon submission checks.

These checks are conservative public preflight checks, not native authorization.
No referee identity, projectile state, score, or opponent file is used here.
"""
from copy import deepcopy
import math


def live_units(brief, obs):
    armed = set(brief.get('own_weapon_options', {})) | set(brief.get('own_weapon_policies', {}))
    controlled = set(obs['controlled_entity_ids'])
    return sorted((u for u in obs['own_entities']
                   if u['entity_id'] in controlled & armed
                   and u.get('lifecycle_state', 'active') in {'active', 'degraded'}),
                  key=lambda u: u['entity_id'])


def weapon_options(brief, entity):
    options = brief.get('own_weapon_options', {}).get(entity)
    if options is None:
        primary = brief.get('own_weapon_policies', {}).get(entity)
        options = [primary] if primary else []
    result = {}
    for option in options:
        ref = option['weapon_ref']
        if ref in result:
            raise ValueError('duplicate weapon option: ' + ref)
        result[ref] = option
    return [result[k] for k in sorted(result)]


def contacts(obs):
    """Keep every current native handle; never truncate or decode opaque IDs."""
    result = {}
    for row in [*obs.get('shared_contacts', []), *obs.get('organic_contacts', [])]:
        key = row['contact_id']
        if key not in result or row.get('age_ticks', 999) <= result[key].get('age_ticks', 999):
            result[key] = row
    return [result[k] for k in sorted(result)]


def contact_domain(contact):
    declared = contact.get('estimated_domain', contact.get('domain'))
    if declared in {'air', 'surface', 'land'}:
        return declared
    # Matches the existing IE public baselines when the DTO omits a domain.
    return 'air' if contact['estimated_position_m'][2] > 50. else 'surface'


def platform_air(brief, entity):
    asset = next((a for a in brief.get('own_assets', []) if a['entity_id'] == entity), None)
    if asset:
        return asset['domain'] == 'air'
    return 'air' in brief['own_weapon_policies'][entity]['target_domains']


def speed_limit(brief, entity):
    values = [p['maximum_speed_mps'] for p in weapon_options(brief, entity)]
    if not values or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('invalid public speed limit: ' + entity)
    return min(values)


def contact_rejection(brief, obs, contact):
    tick, config = obs['tick'], brief['defence']
    age = contact.get('age_ticks')
    if contact.get('detected') is not True or contact.get('confirmed') is not True:
        return 'unconfirmed'
    if type(age) is not int or not 0 <= age <= config['maximum_contact_age_ticks']:
        return 'stale'
    observed = contact.get('observed_tick', tick - age)
    if type(observed) is not int or not 0 <= observed <= tick or tick - observed != age:
        return 'invalid_measurement_time'
    organic = any(c['contact_id'] == contact['contact_id'] for c in obs.get('organic_contacts', []))
    threshold = config['contact_confidence']
    if not organic:
        delivered = contact.get('delivered_tick')
        if type(delivered) is not int or not observed <= delivered <= tick:
            return 'not_delivered'
        if contact_domain(contact) == 'air' and age <= 2:
            threshold = min(threshold, config.get('fresh_shared_air_contact_confidence', .4))
    expiry = contact.get('expiry_tick')
    if 'expiry_tick' in contact and (type(expiry) is not int or tick > expiry):
        return 'expired'
    quality = contact.get('track_quality', contact.get('confidence', 0.))
    if type(quality) not in (int, float) or not math.isfinite(quality) or quality <= threshold:
        return 'low_quality'
    return None


def fire_candidates(brief, obs):
    """All unit/weapon/contact pairs with explicit public rejection reasons."""
    result = []
    for own in live_units(brief, obs):
        for weapon in weapon_options(brief, own['entity_id']):
            for contact in contacts(obs):
                reason = contact_rejection(brief, obs, contact)
                if reason is None and contact_domain(contact) not in weapon['target_domains']:
                    reason = 'incompatible_domain'
                if reason is None and not own.get('capability_flags', {}).get('weapon', False):
                    reason = 'weapon_disabled'
                if reason is None and not own.get('weapon_states', {}).get(weapon['weapon_ref'], {}).get('available', True):
                    reason = 'weapon_unavailable'
                if reason is None and own.get('ammunition', {}).get(weapon['ammunition_ref'], 0) <= 0:
                    reason = 'empty'
                if reason is None and own.get('cooldown_ticks', {}).get(weapon['weapon_ref'], 0) > 0:
                    reason = 'cooldown'
                distance = math.dist(own['position_m'], contact['estimated_position_m'])
                if reason is None and not weapon['minimum_range_m'] <= distance <= weapon['maximum_range_m']:
                    reason = 'range'
                result.append({'unit_id': own['entity_id'], 'weapon_ref': weapon['weapon_ref'],
                               'contact_id': contact['contact_id'], 'eligible': reason is None,
                               'reason': reason, 'distance_m': distance})
    return result


def public_brief(brief):
    from ..participant_brief import participant_brief_view
    result = participant_brief_view(deepcopy(brief))
    # The input is already the participant's public reset DTO. Some catalog
    # revisions use ammunition IDs absent from the shared projection's legacy
    # allowlist. Preserve only counts actually present in this DTO, and only
    # for the unit's explicitly declared own weapon/ammunition bindings.
    for uid in set(result.get('own_weapon_options', {})) | set(result.get('own_weapon_policies', {})):
        allowed = {w['ammunition_ref'] for w in weapon_options(result, uid)}
        counts = brief.get('own_initial_ammunition', {}).get(uid, {})
        result.setdefault('own_initial_ammunition', {})[uid] = {
            ref: count for ref, count in counts.items()
            if ref in allowed and type(count) is int and count >= 0}
    return result
