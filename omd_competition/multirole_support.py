"""Optional secondary UAV fire, isolated from the primary air-defence budget.

Only public contact reports and the UAV's own resource DTOs are used. The
primary planner continues to own aircraft navigation and air-to-air reserves.
"""
import math


class MultiroleSurfaceSupport:
    def __init__(self, brief, mode):
        self.config = brief.get('defence', {}).get('multirole_surface_support', {})
        self.enabled = mode == 'adaptive' and self.config.get('enabled') is True
        self.options = brief.get('own_weapon_options', {})
        self.pending = {}
        self.inflight = []
        self.spent = {}

    def confirm(self, obs):
        """A transport child never confirms a round; owned depletion does."""
        if not self.enabled:
            return
        owned = {unit['entity_id']: unit for unit in obs['own_entities']}
        waiting = {}
        for entity, shot in self.pending.items():
            own = owned.get(entity)
            consumed = (shot['rounds_before'] - own.get('ammunition', {}).get(
                shot['policy']['ammunition_ref'], 0)) if own is not None else 0
            if consumed > 0:
                launched = {**shot, 'shots': min(1, consumed)}
                self.inflight.append(launched)
                key = (entity, shot['policy']['weapon_ref'])
                self.spent[key] = self.spent.get(key, 0) + launched['shots']
            elif obs['tick'] < shot['verify_until_tick']:
                waiting[entity] = shot
        self.pending = waiting
        self.inflight = [s for s in self.inflight if s['expires_tick'] > obs['tick']]

    def reservations(self):
        return [*self.pending.values(), *self.inflight] if self.enabled else []

    def augment(self, obs, batch, planner, contacts, fire_handles, velocities):
        if not self.enabled:
            return batch
        occupied = {a['entity_id'] for a in (batch or {}).get('discrete_actions', [])
                    if a.get('action_type') == 'fire_weapon'}
        occupied.update(planner.pending)
        occupied.update(self.pending)
        committed = {}
        # Primary and secondary weapons share target reservations, while their
        # actual stocks, future-wave budgets and launch confirmation stay apart.
        for shot in [*planner.pending.values(), *planner.inflight, *self.reservations()]:
            key = planner._track_key(shot['contact_id'])
            committed[key] = committed.get(key, 0) + shot.get('shots', 1)
        age = self.config.get('maximum_fire_contact_age_ticks', 2)
        confidence = self.config.get('fire_contact_confidence', .4)
        cutoff = self.config.get('air_contact_min_altitude_m', 50.)
        surface = []
        for contact in contacts:
            declared = contact.get('estimated_domain', contact.get('domain'))
            is_surface = (declared == 'surface' if declared in {'air', 'surface', 'land'}
                          else contact['estimated_position_m'][2] <= cutoff)
            if (is_surface and contact['contact_id'] in fire_handles
                    and contact.get('detected') and contact.get('confirmed')
                    and 0 <= contact.get('age_ticks', 999) <= age
                    and contact.get('track_quality', 0) > confidence):
                surface.append(contact)
        limit = self.config.get('maximum_inflight_rounds_per_contact', 1)
        fires = []
        for own in sorted(obs['own_entities'], key=lambda unit: unit['entity_id']):
            entity = own['entity_id']
            primary = planner._policy(own)
            if (entity not in obs['controlled_entity_ids'] or entity in occupied
                    or primary is None or not planner._air(primary)
                    or not own.get('capability_flags', {}).get('weapon', False)):
                continue
            options = self.options.get(entity, [])
            choices = []
            for policy in options:
                domains = policy.get('target_domains', [])
                weapon = policy.get('weapon_ref')
                if (not weapon or weapon == primary['weapon_ref']
                        or 'surface' not in domains or 'air' in domains
                        or own.get('ammunition', {}).get(policy.get('ammunition_ref'), 0) <= 0
                        or own.get('cooldown_ticks', {}).get(weapon, 0) > 0
                        or not own.get('weapon_states', {}).get(weapon, {}).get('available', True)):
                    continue
                for contact in surface:
                    key = planner._track_key(contact['contact_id'])
                    distance = math.dist(own['position_m'], contact['estimated_position_m'])
                    if (committed.get(key, 0) >= limit
                            or not policy['minimum_range_m'] <= distance <= policy['maximum_range_m']):
                        continue
                    urgency = planner._urgency(contact, obs['own_entities'],
                                               velocities.get(contact['contact_id'], (0., 0., 0.)))
                    choices.append(((urgency, distance, weapon, contact['contact_id']), policy, contact))
            if not choices:
                continue
            _, policy, contact = min(choices, key=lambda row: row[0])
            key = contact['contact_id']
            distance = math.dist(own['position_m'], contact['estimated_position_m'])
            velocity = velocities.get(key, (0., 0., 0.))
            delta = [contact['estimated_position_m'][a] - own['position_m'][a] for a in range(3)]
            radial = sum(delta[a] * velocity[a] for a in range(3)) / max(1., distance)
            flight = min(policy['missile_max_flight_ticks'], math.ceil(distance /
                         max(5., policy['missile_cruise_speed_mps'] - radial)) + 8)
            self.pending[entity] = {'entity_id': entity, 'contact_id': key, 'policy': policy,
                'rounds_before': own['ammunition'][policy['ammunition_ref']],
                'expires_tick': obs['tick'] + flight + 2, 'verify_until_tick': obs['tick'] + 2}
            committed[planner._track_key(key)] = committed.get(planner._track_key(key), 0) + 1
            fires.append((entity, {'weapon_ref': policy['weapon_ref'], 'contact_id': key}))
            occupied.add(entity)
        if not fires:
            return batch
        from .agent import navigation_batch
        extra = navigation_batch(obs, {}, fires)
        if batch is None:
            return extra
        batch['discrete_actions'].extend(extra['discrete_actions'])
        return batch
