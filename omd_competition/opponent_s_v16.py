"""Observation-driven opponent controller for the two S-series examples."""

import hashlib
import math
from .agent import navigation_batch
from .contact_selection import without_flight_contacts
from .contact_time_contract import validated_contact
from .coastal_navigation import CoastalRouter
from .blue_fire_control import BlueFireControl


# Red unit sensor envelopes used to decide when a harassing blue unit has
# entered the *detection* range of the nearest red unit.  These are the
# nominal radar/EO envelopes from the competition sensor catalog, not the
# red weapon envelope used by the old run-in rule.
# (nominal, low-altitude) ranges in metres.  The low-altitude value is used
# when the *blue target* is at or below the competition sensor ceiling.
_RED_CONTACT_DETECTION_RANGE_M = {
    'air': (5000.0, 3000.0),       # sensor.interceptor-radar.competition-v02@2.2.1
    'surface': (3000.0, 2000.0),   # patrol-boat radar nominal/low
    'underwater': (3000.0, 2000.0),
}
_RED_FACILITY_DETECTION_RANGE_M = {
    'red.site.radar.air.01': (7000.0, 3111.111111),  # shore early warning
    'red.facility.command': (5000.0, 3750.0),       # gap-filler radar
    'red.facility.berth.01': (3000.0, 2000.0),      # pier patrol radar
}
_RED_DEFAULT_DETECTION_RANGE_M = (5000.0, 3000.0)


def _dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class AttackPolicy:
    def __init__(self, plan, entity_tags, seed=7):
        self.plan = plan
        self.tags = entity_tags
        self.last_attempt = {}
        self.seed = seed
        self.assigned_engaged = set()
        self.executed_shots = {}
        self.shot_count = {}
        self.pending = {}
        self.fire_control = BlueFireControl(plan['attack'].get('fire_control', {}),
            plan['facility_positions'], plan.get('facility_domains', {}))
        self.waypoint_indices = {}
        self.surface_reports = {}
        self.surface_detours = {}
        self.coastal_router = None
        self.speed_limits = {}
        self.altitude_limits = {}
        self.intent_sink = None
        self.decision_details = {}
        self._logged_phase = {}
        self._init_tactics()

    # ------------------------------------------------------------- tactics
    def _init_tactics(self):
        attack = self.plan.get('attack') or {}
        self.tactics = attack.get('tactics') or None
        self.role_spec = {}
        self.role_by_entity = {}
        self.phase = {}
        self.phase_tick = {}
        self.goal = {}
        self.speed = {}
        self.axis_choice = {}
        self.reversals = {}
        self.own_health = {}
        self.lost_entities = set()
        self._previous_active_entities = set()
        self.pursuit_tracks = {}
        self.pursuit_signals = {}
        self.bb_pos = {}
        self.bb_contacts = {}
        self.bb_contact_details = {}
        self.passed_tactic_points = set()
        self.seen_facility_targets = set()
        self._prepared_tick = None
        self.intent_log = []
        self._dir_tick = None
        self.stale_ticks = 3
        # V4 deception state; only the new behaviours below touch these.
        self.cycles = {}
        self.harass_retreat_goals = {}
        self.harass_cycle_shots = {}
        self.harass_fire_since = {}
        self.point_holds = {}
        self.withdraw_accum = {}
        self.keep_out = {}
        self.withdraw_notice = set()
        # V6 state: fire gating (silent hold / start_tick wait) and the
        # latched damage evidence used by the ambush trigger.
        self.fire_hold = set()
        self.damaged_entities = set()
        # Final-press search fallback after a previously seen facility contact
        # is lost. Never use referee destruction truth or interrupt deception.
        self.target_index = {}
        self.target_stall = {}
        self.target_order = {}
        self.escape_mode = set()
        if not self.tactics:
            return
        self.role_spec = {spec['name']: spec for spec in (self.tactics.get('roles') or [])}
        for entity, tags in self.tags.items():
            for spec in self.role_spec.values():
                if any(tag in tags for tag in spec.get('match_tags', ())):
                    self.role_by_entity[entity] = spec['name']
                    break
        self.stale_ticks = int((self.tactics.get('coordination') or {})
                               .get('contact_staleness_ticks', 3))

    def _objective(self, entity):
        target_tag = next((t[7:] for t in self.tags.get(entity, ())
                           if t.startswith('target.')), None)
        return self.plan['facility_positions'].get(target_tag)

    def _facility_reachable(self, entity, own, point):
        """Reject shore objectives a short-range surface weapon cannot reach."""
        if 'boat' not in self.tags[entity]:
            return True
        if not self.coastal_router.clear_point(point[:2]):
            return False
        seen = set()
        for policy in self.plan['attack']['weapon_policies']:
            if policy['match_tag'] not in self.tags[entity] or policy['weapon_ref'] in seen:
                continue
            seen.add(policy['weapon_ref'])
            domain = self._facility_domain(point)
            if domain and policy.get('target_domains') and domain not in policy['target_domains']:
                continue
            if policy['shots'] > 0 and abs(point[2]-own['position_m'][2]) <= policy['max_range_m']:
                return True
        return False

    def _facility_domain(self, point):
        """Use declared objective data, never referee health or live identities."""
        for identifier, position in self.plan['facility_positions'].items():
            if math.dist(position, point) <= 150.:
                return self.plan.get('facility_domains', {}).get(identifier)
        return None

    def _surface_contact_domain(self, contact):
        domain = contact.get('estimated_domain')
        if domain in {'static_facility', 'shore', 'land'} or contact.get('estimated_contact_kind') in {'facility', 'static_facility'}:
            return self._facility_domain(contact['estimated_position_m']) or ('land' if domain in {'shore', 'land'} else None)
        return domain

    def _surface_weapon_choice(self, own, contacts, objective, tick, defensive_only, prefer_mobile, detail):
        """Choose targets per weapon before selecting the hybrid USV weapon."""
        entity = own['entity_id']
        if any(e == entity and 'suicide-warhead' in weapon and count > 0
               for (e, weapon), count in self.shot_count.items()):
            detail['reason'] = 'surface_detonation_committed'
            return None
        if any(e == entity for e, _, _ in self.pending.values()):
            detail['reason'] = 'surface_fire_pending'
            return None
        policies = []
        seen = set()
        for policy in self.plan['attack']['weapon_policies']:
            if policy['match_tag'] not in self.tags[entity] or policy['weapon_ref'] in seen:
                continue
            seen.add(policy['weapon_ref']);policies.append(policy)
        missile_stock = any('suicide-warhead' not in p['weapon_ref']
            and own['ammunition'].get(p['ammunition_ref'], 0) > 0 for p in policies)
        options = []
        for policy in policies:
            weapon = policy['weapon_ref'];warhead = 'suicide-warhead' in weapon
            rounds = own['ammunition'].get(policy['ammunition_ref'], 0)
            fired = self.shot_count.get((entity, weapon), 0)
            maximum = policy['shots']
            if not self._terminal_attack(entity) and not warhead:
                params = self.role_spec.get(self.role_by_entity.get(entity), {}).get('params', {})
                maximum = min(maximum, params.get('precommit_shots', maximum))
            check = dict(weapon_ref=weapon,match_tag=policy['match_tag'],rounds=rounds,
                confirmed_shots=fired,pending_shots=0,maximum_shots=maximum,
                total_maximum_shots=policy['shots'],cooldown_ticks=own['cooldown_ticks'].get(weapon,0))
            detail['policy_checks'].append(check)
            if (not own['capability_flags'].get('weapon') or rounds <= 0 or fired >= maximum
                    or own['cooldown_ticks'].get(weapon, 0) > 0):
                continue
            energy = own.get('energy_state', {}).get('value', own.get('energy'))
            if energy is not None and energy < policy.get('energy_per_shot', 0.):
                check['blocked'] = 'insufficient_energy';continue
            if warhead and not (self._terminal_attack(entity) or defensive_only):
                continue
            assigned, mobile = [], []
            for contact in contacts.values():
                if self._surface_contact_domain(contact) not in policy['target_domains']:
                    continue
                distance = math.dist(own['position_m'], contact['estimated_position_m'])
                if not policy['min_range_m'] <= distance <= policy['max_range_m']:
                    continue
                key = (entity, weapon, contact['contact_id'])
                if tick-self.last_attempt.get(key,-1000) < policy['retry_interval_ticks']:
                    continue
                if not self.fire_control.available(entity, contact, tick, detail):
                    continue
                # Native harbor piers are surface/platform contacts, just like
                # vessels. Match their declared stationary point explicitly;
                # nearby guard boats must still remain mobile targets.
                declared_facility = any(math.dist(contact['estimated_position_m'], point) <= 1.
                                        for point in self.plan['facility_positions'].values())
                is_mobile = not (declared_facility
                    or contact.get('estimated_contact_kind') in {'facility','static_facility'}
                    or contact.get('estimated_domain') in {'static_facility','shore','land'})
                if defensive_only and not is_mobile:
                    continue
                if not is_mobile and math.dist(contact['estimated_position_m'], objective) <= 100.:
                    assigned.append(contact)
                elif is_mobile:
                    mobile.append(contact)
            mobile_objective = (prefer_mobile and self._terminal_attack(entity)
                and all(math.dist(objective, point) > 1. for point in self.plan['facility_positions'].values()))
            reserve = 0 if entity in self.assigned_engaged or mobile_objective else policy['reserve_for_assigned']
            if warhead:
                # Terminal facility detonation remains available with missiles
                # aboard. Mobile-target suicide requires exhausted missiles;
                # a cooldown or role quota is never an exhaustion signal.
                targets = assigned or (mobile if not missile_stock else [])
            else:
                targets = ((mobile if rounds > reserve else []) or assigned) if prefer_mobile else (assigned or (mobile if rounds > reserve else []))
            check.update(assigned_candidates=len(assigned),interception_candidates=len(mobile),reserved_rounds=reserve)
            if targets:
                contact = min(targets,key=lambda c:(math.dist(own['position_m'],c['estimated_position_m']),c['contact_id']))
                options.append((0 if warhead and assigned else 1 if not warhead else 2,policy,contact,bool(assigned and contact in assigned),check))
        if not options:
            detail['reason'] = ('await_target_attack_assessment' if detail.get('fire_control_holds')
                                else 'no_eligible_surface_weapon_target')
            return None
        _, policy, contact, assigned, check = min(options,key=lambda option:option[0])
        check['selected'] = True
        detail.update(selected_weapon=policy['weapon_ref'],reason='request_assigned' if assigned else 'request_interception')
        self.last_attempt[entity, policy['weapon_ref'], contact['contact_id']] = tick
        self.pending[f'fire.{entity}.{tick}'] = (entity,policy['weapon_ref'],assigned)
        self.fire_control.reserve(f'fire.{entity}.{tick}', entity, own, contact, policy['weapon_ref'], tick)
        return policy,contact

    def _hold_point_speed(self, entity, pos, point, speed, mode, entered, radius=100.):
        key = (mode, entered, tuple(point[:2]), radius)
        distance = _dist2(pos, point)
        held = self.point_holds.get(entity) == key
        if held or distance <= radius:
            self.point_holds[entity] = key
            return 0.
        return min(speed, max(0., distance-radius)/3.)

    def _role_entities(self, role):
        return [e for e, r in self.role_by_entity.items() if r == role]

    def _red_contacts(self, tick):
        return [(pos, t) for (src, cid), (t, pos) in self.bb_contacts.items()
                if 0 <= tick - t <= self.stale_ticks]

    def _nearest_facility_id(self, position):
        """Nearest declared facility to a static contact, if identifiable."""
        positions = self.plan.get('facility_positions') or {}
        if not positions:
            return None
        return min(positions, key=lambda entity_id: _dist2(positions[entity_id], position))

    def _mobile_contact(self, contact):
        return (contact.get('estimated_contact_kind') not in {'flight', 'facility', 'static_facility'}
                and contact.get('estimated_domain') not in {'static_facility', 'shore', 'land'}
                and all(_dist2(contact['estimated_position_m'], point) > 150.
                        for point in self.plan['facility_positions'].values()))

    def _update_pursuit(self, tick):
        """Infer a northern pursuit from scoped measurement histories only."""
        for role, spec in self.role_spec.items():
            if spec.get('behavior') != 'lure_pursuit':
                continue
            params = spec.get('params', {})
            signal = self.pursuit_signals.setdefault(role, {'reacted': False, 'ready': False})
            lure_positions = [self.bb_pos[e] for e in self._role_entities(role) if e in self.bb_pos]
            pursuing, guards, reacting = set(), set(), set()
            facilities = self.plan['facility_positions'].values()
            center_y = sum(p[1] for p in facilities) / len(self.plan['facility_positions'])
            for key, (observed, pos) in self.bb_contacts.items():
                detail = self.bb_contact_details[key]
                if not 0 <= tick-observed <= self.stale_ticks or not self._mobile_contact(detail):
                    continue
                track_key = (role, *key)
                track = self.pursuit_tracks.get(track_key)
                if track is None:
                    track = self.pursuit_tracks[track_key] = dict(origin=pos, last=pos, tick=observed, north_speed=0.)
                elif observed > track['tick']:
                    track['north_speed'] = (pos[1]-track['last'][1])/(observed-track['tick'])
                    track.update(last=pos, tick=observed)
                progress = pos[1]-track['origin'][1]
                northbound = track['north_speed'] >= self._num(params, 'reaction_speed_mps', 3.)
                ahead = [p for p in lure_positions if p[1] > pos[1]]
                visible = any(_dist2(pos, p) <= self._red_detection_range(detail, p)
                              * self._num(params, 'detection_trigger_fraction', .95) for p in ahead)
                cell = (round(pos[0]/500.), round(pos[1]/500.))
                if northbound and visible and progress >= self._num(params, 'reaction_displacement_m', 120.):
                    reacting.add(cell)
                if (northbound and ahead and progress >= self._num(params, 'pursuit_displacement_m', 1500.)
                        and pos[1]-center_y >= self._num(params, 'pursuit_north_m', 4000.)):
                    pursuing.add(cell)
                if min(_dist2(pos, p) for p in self.plan['facility_positions'].values()) <= self._num(params, 'guard_radius_m', 3500.):
                    guards.add(cell)
            signal.update(pursuer_cells=len(pursuing), guard_cells=len(guards), reaction_cells=len(reacting))
            if reacting and not signal['reacted']:
                signal.update(reacted=True, reaction_tick=tick)
            if (signal['reacted'] and len(pursuing) >= self._num(params, 'minimum_pursuers', 2)
                    and len(guards) <= self._num(params, 'maximum_observed_guards', 1)):
                signal.update(ready=True, ready_tick=signal.get('ready_tick', tick))
        # A contact handle is observer scoped. Retain its original position
        # across short gaps, but bound history when long episodes replace tracks.
        self.pursuit_tracks = {k:v for k,v in self.pursuit_tracks.items() if tick-v['tick'] <= 120}

    def _pursuit_commit(self, role, tick):
        params = self.role_spec.get(role, {}).get('params', {})
        opening_role = params.get('commit_on_defender_opening')
        if opening_role and self.pursuit_signals.get(opening_role, {}).get('ready'):
            return 'commit_defender_opening'
        lure_role = params.get('commit_on_lure_pursuit')
        if lure_role and self.pursuit_signals.get(lure_role, {}).get('ready'):
            return 'commit_pursuit_opening'
        if tick >= self._num(params, 'commit_tick', 10 ** 9):
            return 'commit_timer'
        return None

    def _update_defender_opening(self, tick):
        """Observe defenders departing east; no enemy orders or truth states."""
        for role, spec in self.role_spec.items():
            params = spec.get('params', {})
            if spec.get('behavior') != 'opening_attack' or params.get('opening_role', role) != role:
                continue
            signal = self.pursuit_signals.setdefault(role, {'reacted':False, 'ready':False})
            opening_roles = [r for r,s in self.role_spec.items() if s.get('behavior')=='opening_attack'
                             and s.get('params', {}).get('opening_role', r)==role]
            positions = [self.bb_pos[e] for r in opening_roles for e in self._role_entities(r) if e in self.bb_pos]
            reacted, departed, guards = set(), set(), set()
            facilities = list(self.plan['facility_positions'].values())
            center_x = sum(p[0] for p in facilities)/len(facilities)
            for key, (observed, pos) in self.bb_contacts.items():
                contact = self.bb_contact_details[key]
                if not 0 <= tick-observed <= self.stale_ticks or not self._mobile_contact(contact):
                    continue
                track_key = ('opening', role, *key)
                track = self.pursuit_tracks.get(track_key)
                if track is None:
                    track = self.pursuit_tracks[track_key] = dict(origin=pos,last=pos,tick=observed,east_speed=0.)
                elif observed > track['tick']:
                    track['east_speed'] = (pos[0]-track['last'][0])/(observed-track['tick'])
                    track.update(last=pos,tick=observed)
                displacement = pos[0]-track['origin'][0]
                outward = track['east_speed'] >= self._num(params,'reaction_speed_mps',3.)
                distance = min(_dist2(pos,p) for p in facilities)
                initial_distance = min(_dist2(track['origin'],p) for p in facilities)
                guard_radius = self._num(params,'guard_radius_m',3500.)
                if initial_distance>guard_radius and distance<=guard_radius:
                    track['origin']=pos
                    initial_distance=distance
                    displacement=0.
                ahead = [p for p in positions if p[0]>pos[0]]
                visible = any(_dist2(pos,p)<=self._red_detection_range(contact,p)*.95 for p in ahead)
                cell = (round(pos[0]/500.),round(pos[1]/500.))
                if (outward and visible and displacement>=self._num(params,'reaction_displacement_m',120.)
                        and distance-initial_distance>=self._num(params,'reaction_displacement_m',120.)):
                    reacted.add(cell)
                if (outward and ahead and displacement>=self._num(params,'departure_distance_m',1500.)
                        and initial_distance<=guard_radius
                        and pos[0]-center_x>=self._num(params,'opening_east_distance_m',4000.)
                        and distance-initial_distance>=self._num(params,'departure_distance_m',1500.)):
                    departed.add(cell)
                if distance<=guard_radius:guards.add(cell)
            signal.update(reaction_cells=len(reacted),departing_cells=len(departed),guard_cells=len(guards))
            if reacted and not signal['reacted']:signal.update(reacted=True,reaction_tick=tick)
            if (signal['reacted'] and len(departed)>=self._num(params,'minimum_departing_defenders',2)
                    and len(guards)<=self._num(params,'maximum_observed_guards',1)):
                signal.update(ready=True,ready_tick=signal.get('ready_tick',tick))

    def _red_detection_range(self, contact, target_position):
        """Red sensor range for a contact against this blue target.

        The participant observation gives a coarse ``estimated_domain`` rather
        than the red platform identity.  Air contacts are therefore treated as
        interceptor radar (5 km nominal / 3 km low), surface contacts as
        patrol-boat radar (3 km / 2 km), and static contacts are resolved
        against the nearest declared facility.  ``target_position`` is the
        blue unit's position, so the low-altitude sensor profile is applied
        when that unit is at or below 120 m.
        """
        low = float(target_position[2]) <= 120.0
        domain = str(contact.get('estimated_domain') or
                     contact.get('domain') or '').strip().lower()
        if domain in ('static_facility', 'shore', 'land'):
            facility_id = self._nearest_facility_id(contact['estimated_position_m'])
            ranges = _RED_FACILITY_DETECTION_RANGE_M.get(
                facility_id, _RED_DEFAULT_DETECTION_RANGE_M)
        else:
            ranges = _RED_CONTACT_DETECTION_RANGE_M.get(
                domain, _RED_DEFAULT_DETECTION_RANGE_M)
        return float(ranges[1 if low else 0])

    def _nearest_with_detection(self, tick, position):
        """Closest fresh red contact plus its estimated detection range."""
        best = (float('inf'), None, None)
        for key, (observed_tick, contact_pos) in self.bb_contacts.items():
            if 0 <= tick - observed_tick <= self.stale_ticks:
                distance = _dist2(contact_pos, position)
                if distance < best[0]:
                    best = (distance, contact_pos,
                            self._red_detection_range(
                                self.bb_contact_details[key], position))
        return best

    def _decoy_trigger(self, tick, position, params, configured_range, default_fraction=0.8):
        """Whether a decoy has penetrated the nearest red sensor envelope.

        A decoy must get *inside* the red detection range before it turns or
        pulls away, otherwise the red side never observes the bait.  The
        configured range remains an upper bound; the detection envelope is
        scaled inward by ``detection_trigger_fraction`` (default 0.8).
        """
        near, contact, detection_range = self._nearest_with_detection(tick, position)
        if detection_range is None:
            return near, contact, float(configured_range), False
        fraction = self._num(params, 'detection_trigger_fraction', default_fraction)
        fraction = min(1.0, max(0.0, fraction))
        trigger = min(float(configured_range), float(detection_range) * fraction)
        return near, contact, trigger, near <= trigger

    # --------------------------------------- V4 deception behaviour helpers
    @staticmethod
    def _num(params, key, default):
        """Numeric tactic parameter; missing/None/bad values use ``default``."""
        value = params.get(key)
        if value is None:
            return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @classmethod
    def _point(cls, params, key, default):
        """2-D tactic point; missing/None/bad values use ``default``."""
        value = params.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                return (float(value[0]), float(value[1]))
            except (TypeError, ValueError):
                return default
        return default

    @staticmethod
    def _nearest(red, pos):
        """Closest fresh red contact as ``(distance, position)``.

        ``red`` is the shared blackboard list from :meth:`_red_contacts`, i.e.
        estimated positions from this slot's confirmed organic contacts only.
        Returns ``(inf, None)`` when no contact is fresh.
        """
        best = (float('inf'), None)
        for contact_pos, _ in red:
            distance = _dist2(contact_pos, pos)
            if distance < best[0]:
                best = (distance, contact_pos)
        return best

    @staticmethod
    def _away_unit(pos, contact):
        """Unit vector pointing from ``contact`` through ``pos``."""
        dx, dy = pos[0] - contact[0], pos[1] - contact[1]
        norm = math.hypot(dx, dy)
        if norm < 1e-6:
            # Degenerate overlap: a fixed deterministic escape bearing.
            return 0.0, -1.0
        return dx / norm, dy / norm

    @classmethod
    def _keep_out_goal(cls, pos, contact, distance):
        """Point ``distance`` m from ``pos`` along the away-from-contact ray."""
        ux, uy = cls._away_unit(pos, contact)
        return (pos[0] + ux * distance, pos[1] + uy * distance)

    @classmethod
    def _retreat_goal(cls, pos, contact, distance):
        """Point ``distance`` m from ``contact`` along the away-from-contact ray."""
        ux, uy = cls._away_unit(pos, contact)
        return (contact[0] + ux * distance, contact[1] + uy * distance)

    @staticmethod
    def _contact_targets(red, center, radius):
        """Unique 500 m contact cells inside a trigger circle.

        One physical target can be reported by several observers under
        different opaque handles; counting cells keeps alias duplication from
        faking target density (same convention as ``rank_axes``).
        """
        cells = {(round(pos[0] / 500.0), round(pos[1] / 500.0))
                 for pos, _ in red
                 if _dist2(pos, center) <= radius}
        return len(cells)

    def _ingest(self, obs):
        tick = obs['tick']
        controlled = set(obs.get('controlled_entity_ids') or ())
        for own in obs.get('own_entities') or []:
            entity = own['entity_id']
            if entity not in controlled:
                continue
            self.bb_pos[entity] = tuple(own['position_m'])
            health = own.get('health', 1.0)
            previous = self.own_health.get(entity)
            if previous is not None and health < previous - 1e-6:
                # Latched so behaviours evaluated later (or next tick) still
                # see that this entity was hit; no referee truth is read.
                self.damaged_entities.add(entity)
            self.own_health[entity] = health
            self.fire_control.observe(entity, obs.get('organic_contacts', []), tick)
            for contact in obs.get('organic_contacts') or []:
                if not contact.get('detected', True) or not contact.get('confirmed', False):
                    continue
                measured = validated_contact(contact, tick, self.stale_ticks)
                if measured is None:
                    continue
                key = (entity, contact.get('contact_id'))
                previous = self.bb_contacts.get(key)
                if previous is None or measured['observed_tick'] >= previous[0]:
                    contact_position = tuple(contact['estimated_position_m'])
                    self.bb_contacts[key] = (
                        measured['observed_tick'], contact_position)
                    self.bb_contact_details[key] = measured
        if len(self.bb_contacts) > 4000:
            keep = sorted(self.bb_contacts.items(), key=lambda kv: -kv[1][0])[:2000]
            self.bb_contacts = dict(keep)
            self.bb_contact_details = {
                key: value for key, value in self.bb_contact_details.items()
                if key in self.bb_contacts
            }

    def prepare_tick(self, observations):
        """Collect all authorized blue observations before directing the team."""
        rows = [without_flight_contacts(obs) for obs in observations]
        if not rows:
            return
        tick = rows[0]['tick']
        if any(obs['tick'] != tick for obs in rows):
            raise ValueError('blue observations must share one tick')
        if self._prepared_tick == tick:
            return
        # Removed/uncontrolled units must not continue advancing role phases.
        self.bb_pos.clear()
        for obs in sorted(rows, key=lambda row: row['controller_slot_id']):
            self._ingest(obs)
            self._surface_peers(obs)
        # The evaluator supplies the complete blue controller snapshot. Only
        # previously observed units can be lost; planned future spawns cannot.
        active = set()
        for obs in rows:
            for own in obs.get('own_entities', []):
                entity = own['entity_id']
                if entity not in obs.get('controlled_entity_ids', []):
                    continue
                if own.get('health', 1) <= 0 or own.get('lifecycle_state') in {'destroyed', 'despawned', 'wreck', 'disabled'}:
                    self.lost_entities.add(entity)
                    self.bb_pos.pop(entity, None)
                else:
                    active.add(entity)
        self.lost_entities.update(self._previous_active_entities - active)
        self._previous_active_entities = active
        self._direct(tick)
        self._prepared_tick = tick

    def _past_tactic_point(self, entity, point, radius, position):
        """Latch arrival so continued ingress cannot revert to the old point."""
        if _dist2(position, point) <= radius:
            self.passed_tactic_points.add(entity)
        return entity in self.passed_tactic_points

    def _terminal_attack(self, entity):
        return (entity not in self.role_by_entity
                or self.phase.get(entity) == 'press')

    def _log(self, tick, entity, role, phase, goal, axis=None, reason=None, extra=None):
        record = {'tick': tick, 'entity': entity, 'role': role, 'phase': phase,
                  'axis': axis, 'reason': reason,
                  'goal': [round(float(goal[0]), 1), round(float(goal[1]), 1)]}
        if extra:
            record.update(extra)
        record['previous_phase'] = self._logged_phase.get(entity, 'start')
        self._logged_phase[entity] = phase
        if self.intent_sink is None:
            self.intent_log.append(record)
        else:
            self.intent_sink(record)

    def _direct(self, tick):
        if not self.tactics or self._dir_tick == tick:
            return
        self._dir_tick = tick
        self._update_pursuit(tick)
        self._update_defender_opening(tick)
        red = self._red_contacts(tick)
        axes = {a['id']: a for a in (self.tactics.get('axes') or [])}

        def rank_axes(ids, radius):
            # One physical target has separate stable handles per observer; count
            # unique 500 m cells so alias duplication cannot fake density.
            unique = {(round(c[0] / 500.0), round(c[1] / 500.0)) for c, _ in red}
            rows, counts = [], {}
            for axis_id in ids:
                ref = (axes.get(axis_id) or {}).get('objective_ref')
                obj = self.plan['facility_positions'].get(ref)
                if obj is None:
                    continue
                count = sum(1 for cell in unique
                            if _dist2((cell[0] * 500.0, cell[1] * 500.0), obj) <= radius)
                counts[axis_id] = count
                rows.append((count, axis_id, obj))
            rows.sort(key=lambda r: (r[0], r[1]))
            return (rows[0] if rows else None), counts

        for entity, role in sorted(self.role_by_entity.items()):
            spec = self.role_spec.get(role) or {}
            behaviour = spec.get('behavior')
            params = spec.get('params') or {}
            pos = self.bb_pos.get(entity)
            objective = self._objective(entity)
            if pos is None or objective is None:
                continue
            phase = self.phase.get(entity, 'start')
            entered = self.phase_tick.setdefault(entity, tick)
            # V6: universal start_tick gate (applies to every behaviour).
            start_tick = self._num(params, 'start_tick', 0.0)
            if tick < start_tick:
                if phase != 'wait':
                    self._log(tick, entity, role, 'wait', pos, reason='wait')
                self.phase[entity] = 'wait'
                self.goal[entity] = pos
                self.speed[entity] = 0.0
                self.fire_hold.add(entity)
                continue
            if phase == 'wait':
                # Activation tick: restart the phase machine from 'start' so
                # phase timers are measured from start_tick.
                phase = 'start'
                entered = tick
                self.phase[entity] = 'start'
                self.phase_tick[entity] = entered
            self.fire_hold.discard(entity)
            if behaviour == 'press':
                if phase != 'press':
                    self._log(tick, entity, role, 'press', objective, reason='immediate_attack')
                    entered = tick
                phase = 'press'
                self.goal[entity] = objective
                self.speed[entity] = self._num(params, 'speed_press', 30.0)
            elif behaviour == 'press_late':
                # V4: wait offshore, commit on the clock or once the feint role
                # has broken off, then press the objective.
                staging = self._point(params, 'staging', pos)
                staging_radius = self._num(params, 'staging_radius_m', 1200.0)
                if phase in ('start', 'stage'):
                    if phase == 'start':
                        phase = 'stage'
                        self._log(tick, entity, role, phase, staging, reason='start')
                    self.goal[entity] = staging
                    if params.get('hold_position_until_commit'):
                        self.goal[entity] = pos
                        self.speed[entity] = 0.
                    elif _dist2(pos, staging) <= staging_radius:
                        self.speed[entity] = 0.0
                    else:
                        self.speed[entity] = self._num(params, 'speed_stage', 18.0)
                    commit_role = params.get('commit_on_role') or 'feint'
                    feint_withdrawing = any(
                        self.phase.get(other) == 'withdraw'
                        for other in self._role_entities(commit_role))
                    commit_pair = params.get('commit_on_role_phase') or {}
                    commit_role_name = commit_pair.get('role')
                    commit_phase_name = commit_pair.get('phase')
                    role_phase_reached = bool(
                        commit_role_name and commit_phase_name) and any(
                        self.phase.get(other) == commit_phase_name
                        for other in self._role_entities(commit_role_name))
                    reason = None
                    lost_role = params.get('commit_on_role_loss')
                    casualties = sorted(self.lost_entities.intersection(self._role_entities(lost_role))) if lost_role else []
                    if casualties:
                        reason = 'commit_role_loss'
                    elif self._pursuit_commit(role, tick):
                        reason = self._pursuit_commit(role, tick)
                    elif params.get('commit_on_feint_engaged') and feint_withdrawing:
                        reason = 'commit_feint_engaged'
                    elif role_phase_reached:
                        reason = 'commit_role_phase'
                    if reason:
                        phase, entered = 'press', tick
                        self._log(tick, entity, role, phase, objective,
                                  reason=reason, extra=({'lost_entities': casualties} if casualties else
                                      dict(self.pursuit_signals.get(params.get('commit_on_lure_pursuit')
                                          or params.get('commit_on_defender_opening'), {}))))
                if phase == 'press':
                    self.goal[entity] = objective
                    self.speed[entity] = self._num(params, 'speed_press', 40.0)
                elif not params.get('defensive_fire'):
                    self.fire_hold.add(entity)
            elif behaviour == 'lure_pursuit':
                signal = self.pursuit_signals[role]
                strike_role = params.get('reengage_on_role', 'strike')
                mobile = [p for key, (t, p) in self.bb_contacts.items()
                          if 0 <= tick-t <= self.stale_ticks and self._mobile_contact(self.bb_contact_details[key])]
                nearest = min(mobile, key=lambda p: _dist2(pos, p), default=None)
                distance = _dist2(pos, nearest) if nearest is not None else float('inf')
                if phase == 'start':
                    phase, entered = 'probe', tick
                    self._log(tick, entity, role, phase, self._point(params, 'probe_point', objective), reason='start')
                commit = self._pursuit_commit(strike_role, tick)
                committed = any(self.phase.get(e)=='press' for e in self._role_entities(strike_role))
                if phase != 'press' and (commit or committed):
                    phase, entered = 'press', tick
                    self._log(tick, entity, role, phase, nearest or objective, reason='strike_committed_reengage', extra=dict(signal))
                if phase == 'probe' and (signal['reacted'] or entity in self.damaged_entities
                                         or distance <= self._num(params, 'emergency_range_m', 2000.)):
                    phase, entered = 'withdraw', tick
                    reason = 'observed_red_reaction' if signal['reacted'] else 'safety_withdraw'
                    self._log(tick, entity, role, phase, self._point(params, 'retreat_point', pos), reason=reason, extra=dict(signal))
                if phase == 'probe':
                    point = self._point(params, 'probe_point', objective)
                    self.goal[entity] = objective if self._past_tactic_point(entity, point, 300., pos) else point
                    self.speed[entity] = self._num(params, 'speed_probe', 30.)
                elif phase == 'withdraw':
                    retreat = self._point(params, 'retreat_point', pos)
                    self.goal[entity] = (retreat[0], max(retreat[1], pos[1]+6000.))
                    limit = self.speed_limits.get(entity, 15.433332 if 'boat' in self.tags[entity] else 30.)
                    cruise = min(self._num(params, 'withdraw_speed', 12.), limit*.65)
                    if distance < self._num(params, 'keep_out_m', 3000.):
                        speed = limit
                    elif distance > self._num(params, 'follow_band_m', 5000.):
                        speed = min(cruise, self._num(params, 'slow_withdraw_speed', 6.))
                    else:
                        speed = cruise
                    self.speed[entity] = speed
                else:
                    self.goal[entity] = nearest or objective
                    self.speed[entity] = self._num(params, 'speed_press', 30.)
            self.phase[entity] = phase
            self.phase_tick[entity] = entered

    def intent_snapshot(self):
        if not self.tactics:
            return None
        return {'coordination': (self.tactics or {}).get('coordination'),
                'roles': dict(self.role_by_entity),
                'final': {e: {'role': self.role_by_entity.get(e),
                              'phase': self.phase.get(e),
                              'axis': self.axis_choice.get(e)} for e in self.role_by_entity},
                'log': self.intent_log}

    # -------------------------------------------------------------- legacy
    def _surface_peers(self, obs):
        """Retain fresh own-state reports across individual native controllers."""
        tick, faction = obs['tick'], obs['observer_faction_id']
        self.surface_reports = {
            key: report for key, report in self.surface_reports.items()
            if 0 <= tick-report['tick'] <= 1}
        controlled = set(obs['controlled_entity_ids'])
        for own in obs['own_entities']:
            entity = own['entity_id']
            if 'boat' not in self.tags.get(entity, ()):
                continue
            key = (faction, entity)
            lifecycle = own.get('lifecycle_state', 'active')
            if lifecycle == 'despawned':
                self.surface_reports.pop(key, None)
                continue
            if lifecycle in {'disabled', 'destroyed', 'wreck'}:
                # A non-controllable own hull still occupies native water space.
                # Its claim-scoped DTO remains available after losing control.
                # Keep the same fresh-report window and always yield to it.
                self.surface_reports[key] = {
                    'tick': tick, 'entity_id': entity,
                    'position_m': tuple(own['position_m']),
                    'velocity_mps': tuple(own.get('velocity_mps', (0.,0.,0.))),
                    'force_yield': True}
                continue
            if entity not in controlled:
                continue
            self.surface_reports[key] = {
                'tick': tick, 'entity_id': entity,
                'position_m': tuple(own['position_m']),
                'velocity_mps': tuple(own.get('velocity_mps', (0.,0.,0.)))}
        return [report for (side, _), report in self.surface_reports.items()
                if side == faction]

    def _surface_evasion(self, own, peers, tick):
        """Steer around friendly reports and currently observed surface traffic."""
        radius=self.plan['attack'].get('surface_avoidance_m',0.)
        if not radius:
            return None
        entity,pos=own['entity_id'],own['position_m']
        detour=self.surface_detours.get(entity)
        if detour and tick<=detour['expiry'] and math.dist(pos[:2],detour['goal'])>40.:
            return detour['goal']
        self.surface_detours.pop(entity,None)
        velocity=own.get('velocity_mps',(0.,0.,0.))
        hazards=[]
        for peer in peers:
            # Stable pair priority prevents both boats swapping priority as
            # they converge or drift. Each report is already faction-scoped.
            if not peer.get('force_yield', False) and peer['entity_id']>=entity:
                continue
            relative=[peer['position_m'][a]-pos[a] for a in (0,1)]
            delta=[peer['velocity_mps'][a]-velocity[a] for a in (0,1)]
            norm=sum(v*v for v in delta)
            when=max(0.,min(30.,-sum(relative[a]*delta[a] for a in (0,1))/norm)) if norm else 0.
            closest=math.hypot(*(relative[a]+when*delta[a] for a in (0,1)))
            if math.hypot(*relative)<2*radius or closest<radius:
                hazards.append(peer)
        if not hazards:
            return None
        peer=min(hazards,key=lambda p:math.dist(pos[:2],p['position_m'][:2]))
        away=[pos[a]-peer['position_m'][a] for a in (0,1)]
        length=math.hypot(*away)
        if length<1.:
            heading=math.radians(own.get('heading_deg',0.))
            away,length=[math.cos(heading),-math.sin(heading)],1.
        goal=[pos[a]+away[a]/length*(2*radius+80.) for a in (0,1)]
        route,_=self.coastal_router.route(pos,goal)
        if not route:
            return None
        goal=list(route[0])
        self.surface_detours[entity]={'goal':goal,'expiry':tick+120}
        return goal

    def _route_for(self, tags, tick, entity=None):
        routes = sorted(self.plan['routes'], key=lambda r: (
            r['match_tag'] != 'decoy', not bool(r.get('entity_ids'))))
        return next((r for r in routes if r['match_tag'] in tags
                     and (not r.get('entity_ids') or entity in r['entity_ids'])
                     and tick >= r.get('start_tick', 0)
                     and (r.get('until_tick') is None or tick < r['until_tick'])), {})

    def _navigation(self, entity, pos, objective, route, tick):
        attack = self.plan['attack']
        heading = math.degrees(math.atan2(objective[0]-pos[0], objective[1]-pos[1])) % 360
        pattern = route.get('pattern', attack.get('pattern', 'direct'))
        digest = hashlib.sha256(entity.encode()).digest()
        sign = -1 if digest[-1] % 2 else 1
        phase = int.from_bytes(digest[1:5], 'big') / 2**32
        if pattern != 'direct':
            heading += sign * route.get('split_angle_deg', attack.get('split_angle_deg', 0))
            if pattern != 'split_evasion':
                period = max(1, route.get('serpentine_period_ticks', attack.get('serpentine_period_ticks', 1)))
                heading += route.get('serpentine_angle_deg', attack.get('serpentine_angle_deg', 0)) * math.sin(2*math.pi*(tick/period+phase))
        values = route.get('speed_cycle_mps', attack.get('speed_cycle_mps')) or [route.get('speed_mps', attack['speed_mps'])]
        period = max(1, route.get('speed_cycle_ticks', attack.get('speed_cycle_ticks', 1)))
        phase = int.from_bytes(hashlib.sha256(f'{self.seed}:{entity}'.encode()).digest()[:4], 'big') % len(values)
        return {'speed_mps': values[(phase+tick//period) % len(values)], 'heading_deg': heading % 360, 'altitude_m': pos[2]}

    def observe_execution(self, dispatches):
        for dispatch in dispatches:
            self.fire_control.confirm(dispatch['request_id'], dispatch['status'])
            pending = self.pending.pop(dispatch['request_id'], None)
            if pending and dispatch['status'] == 'executed':
                entity, weapon, assigned = pending
                rounds = dispatch.get('execution', {}).get('requested_shots', 1)
                self.shot_count[entity, weapon] = self.shot_count.get((entity, weapon), 0) + rounds
                self.executed_shots[entity] = self.executed_shots.get(entity, 0) + rounds
                if assigned:
                    self.assigned_engaged.add(entity)

    def act(self, obs):
        obs = without_flight_contacts(obs)
        self.decision_details = {}
        facility_search_hold = set()
        facility_backoff = set()
        tick = obs['tick']
        if self.coastal_router is None:
            self.coastal_router=CoastalRouter(
                self.plan.get('surface_navigation_map', {}).get('land_regions', []))
        if self.tactics and self._prepared_tick != tick:
            self._ingest(obs)
            self._direct(tick)
        payloads, fires = {}, []
        peers = (self._surface_peers(obs)
                 if self.plan['attack'].get('surface_separation_m', 0.) else [])
        contacts = {c['contact_id']: measured for c in obs['organic_contacts']
                    if (measured := validated_contact(c, tick, 2)) is not None}
        # Do not ram a passive defender on the route to the assigned facility.
        # These are scoped, confirmed sensor positions, never referee identities.
        # Known fixed objectives remain approach/fire targets, not traffic.
        traffic = [{'entity_id': 'observed.'+c['contact_id'],
                    'position_m': tuple(c['estimated_position_m']),
                    'velocity_mps': (0., 0., 0.), 'force_yield': True}
                   for c in contacts.values()
                   if c['estimated_position_m'][2] <= 50
                   and all(math.dist(c['estimated_position_m'][:2], point[:2]) > 80.
                           for point in self.plan['facility_positions'].values())]
        for own in obs['own_entities']:
            entity = own['entity_id']
            if entity not in obs['controlled_entity_ids']:
                continue
            tags = self.tags[entity]
            self.fire_control.observe(entity, obs.get('organic_contacts', []), tick)
            prefer_mobile = self.role_spec.get(self.role_by_entity.get(entity), {}).get('params', {}).get('prefer_mobile_targets', False)
            defensive_fire = bool(self.role_spec.get(self.role_by_entity.get(entity), {}).get('params', {}).get('defensive_fire'))
            defensive_only = defensive_fire and not self._terminal_attack(entity)
            detail = self.decision_details[entity] = {
                'selected_weapon': None, 'reason': 'no_usable_weapon_policy',
                'policy_checks': [], 'defensive_only': defensive_only}
            route = self._route_for(tags, tick, entity)
            target_tag = next((t[7:] for t in tags if t.startswith('target.')), None)
            pos = own['position_m']
            objective = (route.get('objective_m', self.plan['objective_m']) if 'decoy' in tags
                         else self.plan['facility_positions'][target_tag])
            # Search other facilities after sustained loss of a previously
            # observed target, only in final press. Absence is not kill proof.
            terminal = self._terminal_attack(entity)
            mobile_pursuit = (prefer_mobile and terminal and self.goal.get(entity) is not None
                              and all(_dist2(self.goal[entity], p) > 1.
                                      for p in self.plan['facility_positions'].values()))
            if mobile_pursuit:
                self.escape_mode.discard(entity)
                self.target_stall[entity] = 0
                self.point_holds.pop(entity, None)
            if ('uav' in tags or 'boat' in tags) and not mobile_pursuit:
                def _vec3(point):
                    values = [float(value) for value in point[:3]]
                    while len(values) < 3:
                        values.append(0.0)
                    return values

                if entity not in self.target_order:
                    order = [_vec3(objective)]
                    for name, point in self.plan['facility_positions'].items():
                        candidate = _vec3(point)
                        if all(math.dist(candidate, existing) > 1. for existing in order):
                            order.append(candidate)
                    self.target_order[entity] = order
                    self.target_index[entity] = 0
                    self.target_stall[entity] = 0
                order = self.target_order[entity]
                index = self.target_index.get(entity, 0)
                while terminal and index < len(order) and not self._facility_reachable(entity, own, order[index]):
                    self._log(tick, entity, self.role_by_entity.get(entity), self.phase.get(entity), order[index],
                              reason='facility_unreachable_search_next')
                    index += 1
                    self.target_stall[entity] = 0
                    self.surface_detours.pop(entity, None)
                self.target_index[entity] = index
                if terminal and index >= len(order):
                    self.escape_mode.add(entity)
                if entity in self.escape_mode:
                    objective = (32000.0, 0.0, 0.0)
                else:
                    objective = order[index]
                    near_target = [c for c in contacts.values()
                                   if (c.get('estimated_domain') in {'static_facility', 'shore', 'land'}
                                       or c.get('estimated_contact_kind') in {'facility', 'static_facility'}
                                       or ('boat' in tags and c.get('estimated_domain') == 'surface'
                                           and math.dist(c['estimated_position_m'], objective) <= 1.))
                                   and math.dist(c['estimated_position_m'], objective) <= 150.]
                    target_key = (entity, index)
                    if near_target:
                        self.seen_facility_targets.add(target_key)
                    seen = target_key in self.seen_facility_targets
                    distance_to_objective = math.dist(pos[:2], objective[:2])
                    searching = (self._terminal_attack(entity) and not near_target
                                 and distance_to_objective <= (3000. if seen else 600.))
                    if searching:
                        self.target_stall[entity] = self.target_stall.get(entity, 0) + 1
                    else:
                        self.target_stall[entity] = 0
                    if self.target_stall.get(entity, 0) >= 60:
                        index += 1
                        while index < len(order) and not self._facility_reachable(entity, own, order[index]):
                            self._log(tick, entity, self.role_by_entity.get(entity), self.phase.get(entity), order[index],
                                      reason='facility_unreachable_search_next')
                            index += 1
                        self.target_index[entity] = index
                        self.surface_detours.pop(entity, None)
                        self.target_stall[entity] = 0
                        if self.target_index[entity] >= len(order):
                            self.escape_mode.add(entity)
                            objective = (32000.0, 0.0, 0.0)
                        else:
                            objective = order[self.target_index[entity]]
                        self._log(tick, entity, self.role_by_entity.get(entity),
                                  self.phase.get(entity), objective,
                                  reason=('target_contact_lost_search_next' if seen
                                          else 'target_unobserved_search_next'))
                    elif searching and distance_to_objective <= 300.:
                        facility_search_hold.add(entity)
                    detail['facility_search'] = dict(seen=seen, searching=searching,
                        absence_ticks=self.target_stall.get(entity, 0),
                        holding=entity in facility_search_hold,
                        target_index=self.target_index.get(entity))
            if mobile_pursuit:
                objective = self.goal.get(entity, objective)
            navigation_goal = objective
            waypoints = route.get('waypoints', [])
            index = self.waypoint_indices.get(entity, 0)
            while index < len(waypoints) and math.dist(
                    pos[:2], waypoints[index]['objective_m'][:2]) <= waypoints[index].get('arrival_radius_m', 80.):
                index += 1
            self.waypoint_indices[entity] = index
            if index < len(waypoints):
                navigation_goal = waypoints[index]['objective_m']
            tactic_goal = self.goal.get(entity) if self.tactics else None
            if tactic_goal is not None:
                navigation_goal = tactic_goal
            if (self._terminal_attack(entity)
                    and (entity in self.escape_mode or self.target_index.get(entity, 0) > 0)):
                # 临时目标切换/撤离必须优先于战术层的原目标，否则飞机会飞回原设施。
                navigation_goal = objective
            detail['navigation_goal_m'] = list(navigation_goal)
            payloads[entity] = self._navigation(entity, pos, navigation_goal, route, tick)
            if entity in self.escape_mode:
                navigation_goal = objective
            if self.tactics and entity in self.role_by_entity and self.speed.get(entity) is not None:
                payloads[entity]['speed_mps'] = self.speed[entity]
            if entity in self.escape_mode:
                payloads[entity]['speed_mps'] = self.speed_limits.get(entity, 15.433332 if 'boat' in tags else 30.)
            if 'boat' in tags:
                tactic_speed = payloads[entity]['speed_mps']
                evasion=self._surface_evasion(own,[*peers,*traffic],tick)
                # A detour changes the next straight approach: route that
                # approach around the actual coast, even after the detour ends.
                # Bounding boxes can wrongly block a navigable concave harbor.
                if evasion is None and self.coastal_router.polygons:
                    coastal_route,_=self.coastal_router.route(pos,navigation_goal)
                    if coastal_route:
                        payloads[entity]=self._navigation(entity,pos,coastal_route[0],
                            {**route,'pattern':'direct'},tick)
                        payloads[entity]['speed_mps'] = tactic_speed
                    else:
                        payloads[entity]['speed_mps']=0.
                if evasion is not None:
                    payloads[entity]=self._navigation(entity,pos,evasion,
                        {**route,'pattern':'direct'},tick)
                    payloads[entity]['speed_mps']=min(6.,tactic_speed)
                arrival = route.get('arrival_standoff_m', 0.)
                if (evasion is None and arrival and index == len(waypoints)
                        and terminal and entity not in self.escape_mode
                        and _dist2(navigation_goal, objective) < 1.):
                    # Remain inside the native warhead range without repeatedly
                    # driving through a disabled facility and the other boats.
                    payloads[entity]['speed_mps'] = min(
                        payloads[entity]['speed_mps'],
                        max(0., math.dist(pos[:2], objective[:2])-arrival) /
                        max(3., float(route.get('arrival_braking_ticks', 3.))))
                separation = self.plan['attack'].get('surface_separation_m', 0.)
                if separation and evasion is None:
                    for peer in peers:
                        peer_id = peer['entity_id']
                        if peer_id == entity:
                            continue
                        if math.dist(pos[:2], peer['position_m'][:2]) >= separation:
                            continue
                        # Yield to the boat ahead, using own-faction positions
                        # reported by this faction's individual controllers in
                        # this or the preceding tick; never referee truth.
                        priority = (math.dist(pos[:2], objective[:2]), entity)
                        peer_priority = (math.dist(peer['position_m'][:2], objective[:2]), peer_id)
                        if priority > peer_priority:
                            payloads[entity]['speed_mps'] = 0.
                            break
            if 'boat' in tags and (waypoints or self.coastal_router.polygons):
                error = abs((payloads[entity]['heading_deg'] - own.get(
                    'heading_deg', payloads[entity]['heading_deg']) + 180) % 360 - 180)
                if error > 25:
                    payloads[entity]['speed_mps'] = min(3., payloads[entity]['speed_mps'])
            if not terminal and any(_dist2(navigation_goal, p) < 1. for p in self.plan['facility_positions'].values()):
                radius = 24. if 'boat' in tags else 300.
                payloads[entity]['speed_mps'] = self._hold_point_speed(entity,pos,navigation_goal,
                    payloads[entity]['speed_mps'],'precommit_facility',self.phase_tick.get(entity),radius)
            if entity in self.escape_mode:
                payloads[entity]['speed_mps'] = self._hold_point_speed(entity,pos,objective,
                    payloads[entity]['speed_mps'],'escape',self.phase_tick.get(entity))
                detail['reason'] = 'escape'
                continue
            if 'decoy' in tags:
                detail['reason'] = 'decoy'
                continue
            if entity in self.fire_hold:
                # V6: silent ambush hold or pre-start_tick wait.
                detail['reason'] = 'phase_fire_hold'
                continue
            if 'boat' in tags and any(p['match_tag'] in tags and 'surface-missile' in p['weapon_ref']
                                      for p in self.plan['attack']['weapon_policies']):
                choice = self._surface_weapon_choice(own,contacts,objective,tick,defensive_only,prefer_mobile,detail)
                if choice is not None:
                    selected, contact = choice
                    fires.append((entity,dict(weapon_ref=selected['weapon_ref'],contact_id=contact['contact_id'])))
                continue
            # Preserve role quotas; cooldown never enables a suicide fallback.
            policy = None
            seen_weapons = set()
            missile_pending_or_available = False
            for candidate in self.plan['attack']['weapon_policies']:
                if candidate['match_tag'] not in tags:
                    continue
                reference = candidate['weapon_ref']
                # Specific role policy is authoritative, including shots=0.
                if reference in seen_weapons:
                    continue
                seen_weapons.add(reference)
                rounds = own['ammunition'].get(candidate['ammunition_ref'], 0)
                fired = self.shot_count.get((entity, reference), 0)
                pending = sum(1 for e, w, _ in self.pending.values()
                              if e == entity and w == reference)
                maximum_shots = candidate['shots']
                role_params = self.role_spec.get(self.role_by_entity.get(entity), {}).get('params', {})
                if not self._terminal_attack(entity) and 'suicide-warhead' not in reference:
                    maximum_shots = min(maximum_shots, role_params.get('precommit_shots', maximum_shots))
                check = dict(weapon_ref=reference, match_tag=candidate['match_tag'],
                             rounds=rounds, confirmed_shots=fired, pending_shots=pending,
                             maximum_shots=maximum_shots, total_maximum_shots=candidate['shots'],
                             cooldown_ticks=own['cooldown_ticks'].get(reference, 0))
                detail['policy_checks'].append(check)
                if 'suicide-warhead' in reference:
                    if ((not self._terminal_attack(entity) and not defensive_only)
                            or ('uav' in tags and missile_pending_or_available)):
                        check['blocked'] = 'terminal_phase_or_missile_not_exhausted'
                        continue
                elif 'uav' in tags and (pending or (rounds > 0 and fired < candidate['shots'])):
                    missile_pending_or_available = True
                if (own['capability_flags'].get('weapon') and rounds > 0
                        and fired + pending < maximum_shots
                        and own['cooldown_ticks'].get(reference, 0) <= 0):
                    policy = candidate
                    check['selected'] = True
                    break
            if policy is None:
                if 'uav' in tags and terminal and not mobile_pursuit and _dist2(pos, objective) <= 300.:
                    facility_search_hold.add(entity)
                    detail['facility_arrival_hold'] = True
                continue
            weapon = policy['weapon_ref']
            detail['selected_weapon'] = weapon
            detail['reason'] = 'no_eligible_target'
            facility_target = terminal and not mobile_pursuit and any(
                math.dist(objective, p) < 1. for p in self.plan['facility_positions'].values())
            visible_facility = facility_target and any(
                (c.get('estimated_domain') in {'static_facility', 'shore', 'land'}
                 or c.get('estimated_contact_kind') in {'facility', 'static_facility'})
                and math.dist(c['estimated_position_m'], objective) <= 100. for c in contacts.values())
            if 'uav' in tags and visible_facility and 'suicide-warhead' not in weapon:
                # Do not overfly a live facility while carrying a weapon whose
                # minimum range prevents firing at the facility center.
                horizontal = _dist2(pos, objective)
                altitude = pos[2]-objective[2]
                stand_off = math.sqrt(max(0., policy['min_range_m']**2-altitude**2))+100.
                if horizontal < stand_off:
                    goal = self._keep_out_goal(pos, objective, stand_off-horizontal+100.)
                    payloads[entity] = self._navigation(entity, pos, goal, {**route, 'pattern':'direct'}, tick)
                    payloads[entity]['speed_mps'] = min(12., payloads[entity]['speed_mps'])
                    facility_backoff.add(entity)
                    detail['facility_backoff'] = True
                elif horizontal < stand_off+300.:
                    payloads[entity]['speed_mps'] = min(payloads[entity]['speed_mps'], max(0.,horizontal-stand_off)/3.)
            if 'suicide-warhead-air' in weapon and not defensive_only:
                # 末端自杀攻击：俯冲到设施附近再引爆（原生自毁 + despawn）。
                horizontal = _dist2(pos, objective)
                if mobile_pursuit:
                    payloads[entity]['altitude_m'] = objective[2]
                elif horizontal < 6000.:
                    payloads[entity]['altitude_m'] = 80. if horizontal < 1500. else 120.
                    payloads[entity]['speed_mps'] = max(payloads[entity]['speed_mps'], 24.)
            assigned, interception = [], []
            for c in contacts.values():
                mobile_contact = (c.get('estimated_contact_kind') != 'static_facility'
                                  and all(math.dist(c['estimated_position_m'][:2], point[:2]) > 150.
                                          for point in self.plan['facility_positions'].values()))
                if defensive_only and not mobile_contact:
                    continue
                distance = math.dist(pos, c['estimated_position_m'])
                if not policy['min_range_m'] <= distance <= policy['max_range_m']:
                    continue
                if tick-self.last_attempt.get((entity, c['contact_id']), -1000) < policy['retry_interval_ticks']:
                    continue
                if not self.fire_control.available(entity, c, tick, detail):
                    continue
                if math.dist(c['estimated_position_m'], objective) <= 100:
                    assigned.append(c)
                elif (not defensive_fire or mobile_contact) and (c['estimated_position_m'][2] > 50) == ('uav' in tags):
                    interception.append(c)
            reserve = 0 if entity in self.assigned_engaged else policy['reserve_for_assigned']
            detail.update(reserved_rounds=reserve, assigned_candidates=len(assigned),
                          interception_candidates=len(interception))
            candidates = ((interception if rounds > reserve else []) or assigned) if prefer_mobile else (assigned or (interception if rounds > reserve else []))
            if not assigned and interception and rounds <= reserve:
                detail['reason'] = 'reserve_for_assigned_facility'
            if not candidates and detail.get('fire_control_holds'):
                detail['reason'] = 'await_target_attack_assessment'
            if candidates:
                contact = min(candidates, key=lambda c: (math.dist(pos, c['estimated_position_m']), c['contact_id']))
                fires.append((entity, {'weapon_ref': weapon, 'contact_id': contact['contact_id']}))
                self.last_attempt[entity, contact['contact_id']] = tick
                self.pending[f'fire.{entity}.{tick}'] = (entity, weapon, bool(assigned))
                self.fire_control.reserve(f'fire.{entity}.{tick}', entity, own, contact, weapon, tick)
                detail['reason'] = 'request_assigned' if assigned else 'request_interception'
            elif ('uav' in tags and terminal and not mobile_pursuit
                  and _dist2(pos, objective) <= 300. and entity not in facility_backoff):
                facility_search_hold.add(entity)
                detail['facility_arrival_hold'] = True
        for entity, payload in payloads.items():
            if entity in facility_search_hold:
                payload['speed_mps'] = 0.
            if entity in self.speed_limits:
                payload['speed_mps'] = min(payload['speed_mps'], self.speed_limits[entity])
            if entity in self.altitude_limits:
                minimum, maximum = self.altitude_limits[entity]
                payload['altitude_m'] = max(minimum, min(payload['altitude_m'], maximum))
        return navigation_batch(obs, payloads, fires) if payloads or fires else None
