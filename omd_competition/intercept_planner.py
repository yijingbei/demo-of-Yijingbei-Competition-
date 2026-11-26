"""Two participant baselines using public controller observations only.

Greedy pursues and shoots its nearest organic track independently. Adaptive
allocates a fleet to approaching threats and limits salvos while missiles are
estimated to be in flight. Neither receives enemy identities, health or hits.
"""
import math
from .contact_selection import without_flight_contacts
from .wave_schedule import normalize_public_waves
from .contact_aliases import PublicContactAliases
from .surface_patrol import SurfacePatrol
from .multirole_support import MultiroleSurfaceSupport


class InterceptPlanner:
    def __init__(self, brief, mode):
        if mode not in {'greedy', 'adaptive'}:
            raise ValueError('unknown interception planner')
        brief = normalize_public_waves(brief)
        self.brief, self.mode = brief, mode
        self.fresh_shared_air_confidence = brief['defence'].get(
            'fresh_shared_air_contact_confidence', .4)
        if mode == 'adaptive' and (
                type(self.fresh_shared_air_confidence) not in (int, float)
                or not math.isfinite(self.fresh_shared_air_confidence)
                or not 0. <= self.fresh_shared_air_confidence <= 1.):
            raise ValueError('fresh shared air contact confidence must be finite in [0, 1]')
        self.history, self.pending, self.inflight = {}, {}, []
        self.spent, self.initial_rounds = {}, None
        self.seen_threats = {'air': set(), 'surface': set()}
        self.seen_threats_by_observer = {}
        self.last_air_contact_tick = None
        self.regroup_base_altitude = None
        self.surface_detours = {}
        self.coastal_router = None
        self.contact_aliases = PublicContactAliases(
            brief['defence'].get('motion_contact_association') if mode == 'adaptive' else None)
        self.contact_first_seen = {}
        self.air_patrol_legs = {}
        self.air_patrol_stations = {}
        self.surface_patrol = SurfacePatrol(brief)
        self.multirole_support = MultiroleSurfaceSupport(brief, mode)

    def _track_key(self, handle):
        return self.contact_aliases.key(handle) if self.mode == 'adaptive' else handle

    def _spent_count(self, handle):
        key = self._track_key(handle)
        return sum(count for alias, count in self.spent.items() if self._track_key(alias) == key)

    def _policy(self, own):
        return self.brief['own_weapon_policies'].get(own['entity_id'])

    @staticmethod
    def _air(policy):
        return 'air' in policy.get('target_domains', [])

    @staticmethod
    def _rounds(own, policy):
        return own.get('ammunition', {}).get(policy['ammunition_ref'], 0)

    def _confirm_launches(self, obs):
        # An executed transport child is not proof that combat launched a round.
        # Queued native fire is applied after the next observation. Keep its
        # tentative reservation until the second observation can show depletion.
        own_by_id = {r['entity_id']: r for r in obs['own_entities']}
        waiting = {}
        for identifier, shot in self.pending.items():
            own = own_by_id.get(identifier)
            consumed = 0
            if own is not None:
                consumed = shot['rounds_before'] - self._rounds(own, shot['policy'])
                if consumed > 0:
                    self.inflight.append({**shot, 'shots': min(1, consumed)})
                    self.spent[shot['contact_id']] = self.spent.get(shot['contact_id'], 0) + min(1, consumed)
            if consumed <= 0 and obs['tick'] < shot['verify_until_tick']:
                waiting[identifier] = shot
        self.pending = waiting
        self.inflight = [s for s in self.inflight if s['expires_tick'] > obs['tick']]

    @staticmethod
    def _fresh_delivered_report(contact, tick):
        """Check public delivery evidence without manufacturing native authority."""
        observed, delivered, age = (contact.get(key) for key in
                                    ('observed_tick', 'delivered_tick', 'age_ticks'))
        if (contact.get('detected') is not True or contact.get('confirmed') is not True
                or any(type(value) is not int for value in (tick, observed, delivered, age))
                or not 0 <= observed <= delivered <= tick
                or not 0 <= age == tick - observed <= 2):
            return False
        # Native shared DTOs omit expiry. If a public adapter supplies it, never
        # use an explicitly expired or malformed report. Native gateway checks
        # remain the final authority when no expiry is exposed.
        expiry = contact.get('expiry_tick')
        return ('expiry_tick' not in contact
                or type(expiry) is int and tick <= expiry)

    def _contacts(self, obs):
        obs = without_flight_contacts(obs)
        config = self.brief['defence']
        contacts = {}
        for shared, c in [(True, c) for c in obs['shared_contacts']] + [
                (False, c) for c in obs['organic_contacts']]:
            threshold = config['contact_confidence']
            if (self.mode == 'adaptive' and shared
                    and c['estimated_position_m'][2] > 50
                    and self._fresh_delivered_report(c, obs['tick'])):
                # A confirmed delivered report decays during transport. Do not
                # discard the fleet's only legal air track merely because its
                # quality is below the controller endpoint's organic threshold.
                # Setting this option to contact_confidence restores that gate.
                threshold = min(threshold, self.fresh_shared_air_confidence)
            if (self.mode == 'adaptive' and self.surface_patrol.enabled
                    and c['estimated_position_m'][2] <= 50):
                threshold = self.surface_patrol.config.get('navigation_contact_confidence', threshold)
            if (c.get('detected', False) and c.get('confirmed', False)
                    and 0 <= c.get('age_ticks', 999) <= config['maximum_contact_age_ticks']
                    and c.get('track_quality', 0) > threshold):
                contacts[c['contact_id']] = c
                if self.mode == 'adaptive':
                    self.contact_first_seen.setdefault(c['contact_id'], obs['tick'])
                    observer = c.get('observer_entity_id')
                    if isinstance(observer, str) and observer:
                        domain = 'air' if c['estimated_position_m'][2] > 50 else 'surface'
                        seen = self.seen_threats_by_observer.setdefault(
                            observer, {'air': set(), 'surface': set()})
                        seen[domain].add(c['contact_id'])
        if self.mode == 'adaptive':
            self.contact_aliases.observe(list(contacts.values()), obs['tick'])
            # Native shared reports carry independent opaque handles for each
            # observer. Associate only from public position/time evidence;
            # never count several reports of one snapshot as several targets.
            organic_ids = {c['contact_id'] for c in obs['organic_contacts']}
            def preferred(c):
                organic = c['contact_id'] in organic_ids
                delivered = (type(c.get('delivered_tick')) is int
                             and 0 <= c['delivered_tick'] <= obs['tick']
                             and 0 <= c.get('age_ticks', 999) <= 2)
                return (not (organic or delivered), c['age_ticks'], not organic,
                        -c['track_quality'], c['contact_id'])
            selected = []
            for contact in sorted(contacts.values(), key=preferred):
                when = contact.get('observed_tick', obs['tick']-contact['age_ticks'])
                duplicate = False
                for existing in selected:
                    previous = existing.get('observed_tick', obs['tick']-existing['age_ticks'])
                    both_organic = (contact['contact_id'] in organic_ids
                                    and existing['contact_id'] in organic_ids)
                    associated = self._track_key(contact['contact_id']) == self._track_key(existing['contact_id'])
                    same_snapshot = (not both_organic and when == previous
                                     and math.dist(contact['estimated_position_m'], existing['estimated_position_m']) <= 1.)
                    if associated or same_snapshot:
                        duplicate = True
                        break
                if not duplicate:
                    selected.append(contact)
            contacts = {c['contact_id']: c for c in selected}
        velocities = {}
        for key, c in contacts.items():
            when = c.get('observed_tick', obs['tick'] - c['age_ticks'])
            position = c['estimated_position_m']
            previous = self.history.get(key)
            if previous and when-previous[0]>30:
                # Reacquisition after the retained motion window is a new
                # measurement, not evidence of continuous outward flight.
                previous=None
            velocity = previous[2] if previous else (0., 0., 0.)
            if previous and when > previous[0]:
                candidate = tuple((position[a] - previous[1][a]) / (when - previous[0]) for a in range(3))
                if math.dist(candidate, (0, 0, 0)) <= 100:
                    velocity = candidate
            if previous is None or when >= previous[0]:
                self.history[key] = (when, tuple(position), velocity)
            velocities[key] = velocity
        self.history = {k: v for k, v in self.history.items() if obs['tick'] - v[0] <= 30}
        return list(contacts.values()), velocities

    def _compatible(self, policy, contact):
        return (contact['estimated_position_m'][2] > 50) == self._air(policy)

    def _urgency(self, contact, own_entities, velocity):
        facilities = [r['position_m'] for r in own_entities
                      if r['entity_id'] in self.brief['facility_weights']]
        if not facilities:
            facilities = [self.brief['objective_m']]
        position = contact['estimated_position_m']
        closest = min(facilities, key=lambda p: math.dist(position[:2], p[:2]))
        distance = math.dist(position[:2], closest[:2])
        speed = max(10. if contact['estimated_position_m'][2] <= 50 else 43., math.hypot(*velocity[:2]))
        closing = sum((closest[a]-position[a])*velocity[a] for a in (0,1))/max(1.,distance)
        # A sustained departure is lower priority, not evidence of harmlessness.
        return distance / speed * (1+3*min(1.,max(0.,-closing/speed)))

    def _departing_air_contact(self, contact, own_entities, velocity):
        """Defer a distant outward track using measured motion, never its role."""
        if contact['estimated_position_m'][2]<=50:
            return False
        facilities=[r['position_m'] for r in own_entities
                    if r['entity_id'] in self.brief['facility_weights']]
        facilities=facilities or [self.brief['objective_m']]
        position=contact['estimated_position_m']
        closest=min(facilities,key=lambda p:math.dist(position[:2],p[:2]))
        distance=math.dist(position[:2],closest[:2])
        closing=sum((closest[a]-position[a])*velocity[a] for a in (0,1))/max(1.,distance)
        return distance>1000. and closing < -5.

    def _future_reserve(self, domain, tick):
        waves = self.brief.get('planned_waves', [])
        future = sum(wave.get(domain, 0) for wave in waves if wave['spawn_tick'] > tick)
        # Scoring normalization is not a physical minimum-hit requirement.
        nominal = self.brief.get('combat_reference', {}).get('planning_minimum_hits_per_threat')
        if nominal is None:
            nominal = self.brief.get('performance_scoring', {}).get('nominal_rounds_per_threat', {})
        return future * nominal.get(domain, 2 if domain=='air' else 1)

    def _salvo_limit(self, policy):
        # These are public planning reservations, never native missile truth.
        if self._air(policy):
            return 1
        return min(policy['salvo_rounds'], self.brief['defence'].get(
            'maximum_surface_inflight_rounds', policy['salvo_rounds']))

    def _commit_remaining_reserve(self, tick):
        """Optional clock-based late-wave tactic, never evidence of a kill."""
        delay = self.brief['defence'].get('reserve_commit_after_last_wave_ticks')
        waves = self.brief.get('planned_waves', [])
        return (delay is not None and bool(waves)
                and tick >= max(w['spawn_tick'] for w in waves) + delay)

    def _wait_for_contact_confirmation(self, handle, policy, tick, urgency_seconds):
        """Briefly defer a new handle while owned expenditure is already reserved.

        This is a firing heuristic, not target association or kill evidence.
        Exact aliases inherit their earliest public arrival. Distinct contacts
        remain available for navigation and can fire when the bounded wait ends;
        urgent threats bypass it. Unconfigured scenarios retain prior behavior.
        """
        defence = self.brief['defence']
        delay = defence.get('new_contact_fire_confirmation_ticks', 0)
        if not delay or urgency_seconds <= defence.get('new_contact_urgent_bypass_seconds', 30.):
            return False
        if not any(self._air(s['policy']) == self._air(policy) for s in self.inflight):
            return False
        key = self._track_key(handle)
        first = min((when for alias, when in self.contact_first_seen.items()
                     if self._track_key(alias) == key), default=tick)
        return tick - first < delay

    def _flight_level(self, identifier, aircraft):
        levels = self.brief.get('defence', {}).get('aircraft_flight_levels_m', {})
        return levels.get(identifier, self.regroup_base_altitude + 60.*aircraft.index(identifier))

    def _air_standoff_fraction(self):
        return self.brief.get('defence', {}).get('aircraft_intercept_standoff_fraction', .55)

    def _air_patrol_enabled(self):
        config = self.brief.get('defence', {}).get('aircraft_patrol', {})
        return config.get('enabled', False) and config.get('patrol_enabled', True)

    def _regroup_aircraft(self, own, policy, obs):
        # A blind aircraft left at the previous intercept point can be attacked
        # outside the team endpoint's sensor coverage. Return to an owned site;
        # never infer an unseen attacker's position or read referee state.
        sites = [r for r in obs['own_entities']
                 if r['entity_id'] in self.brief['facility_weights']
                 and r.get('lifecycle_state', 'active') in {'active', 'degraded'}]
        endpoint = next((r for r in sites if r.get('communication_state', {}).get(
            'is_controller_endpoint')), None)
        preferred = self.brief.get('defence', {}).get('aircraft_regroup_anchor_entity_id')
        anchor = next((r for r in sites if r['entity_id'] == preferred), None)
        anchor = anchor or endpoint or (max(sites, key=lambda r: (
            self.brief['facility_weights'][r['entity_id']], r['entity_id'])) if sites else None)
        center = anchor['position_m'] if anchor else self.brief['objective_m']
        aircraft = sorted(identifier for identifier, p in self.brief['own_weapon_policies'].items()
                          if self._air(p))
        rank = aircraft.index(own['entity_id'])
        angle = 2*math.pi*rank/max(1, len(aircraft))
        radius = min(self.brief.get('defence', {}).get('aircraft_regroup_radius_m', 1200.),
                     policy['maximum_range_m']*.15)
        patrol = self.brief.get('defence', {}).get('aircraft_patrol', {})
        if self._air_patrol_enabled():
            # Persistent waypoint legs advance on actual own-position arrival.
            # A clock-driven destination can outrun the aircraft indefinitely;
            # a fixed regroup destination instead strands it after arrival.
            radius = patrol['radius_m']
            key = own['entity_id']
            sector = patrol.get('pattern') == 'sector_racetrack' and self._rounds(own, policy)>0
            if sector:
                center=self.air_patrol_stations.setdefault(key,list(own['position_m'][:2]))
                objective=self.brief['objective_m']
                bearing=math.atan2(center[1]-objective[1],center[0]-objective[0])
                length=patrol['leg_length_m']/2
                # Opposite straight legs search a distinct owned defensive
                # station. Rounded ends retain the declared turn radius.
                offsets=[]
                for side in (1,-1):
                    for n in range(7):
                        a=(-math.pi/2+n*math.pi/6) if side==1 else (math.pi/2+n*math.pi/6)
                        offsets.append((side*length+radius*math.cos(a),radius*math.sin(a)))
                points=[[center[0]+x*math.cos(bearing)-y*math.sin(bearing),
                         center[1]+x*math.sin(bearing)+y*math.cos(bearing)] for x,y in offsets]
            else:
                points=[[center[0]+radius*math.cos(n*math.tau/8),center[1]+radius*math.sin(n*math.tau/8)] for n in range(8)]
            leg = self.air_patrol_legs.get(key)
            if leg is None or math.dist(leg['center'], center[:2]) > 1.:
                leg = {'center': list(center[:2]), 'index': min(range(len(points)),key=lambda i:math.dist(own['position_m'][:2],points[i])) if sector else rank*8//max(1,len(aircraft))}
                self.air_patrol_legs[key] = leg
            leg['index']%=len(points)
            waypoint = points[leg['index']]
            if math.dist(own['position_m'][:2], waypoint) <= patrol['arrival_radius_m']:
                leg['index'] = (leg['index']+1) % len(points)
            target=points[leg['index']]
        else:
            target = [center[0]+radius*math.cos(angle), center[1]+radius*math.sin(angle)]
        pos = own['position_m']
        distance = math.dist(pos[:2], target)
        heading = (math.degrees(math.atan2(target[0]-pos[0], target[1]-pos[1])) % 360
                   if distance > 20. else own['heading_deg'])
        error = abs((heading-own['heading_deg']+180) % 360-180)
        speed = (min(policy['maximum_speed_mps'], max(0., distance-20.)/3.)
                 if error <= 45 else 0.)
        if self._air_patrol_enabled():
            speed = min(policy['maximum_speed_mps'], patrol['speed_mps']) if error <= 45 else 0.
        # Crossing return paths keep the public separated flight plan, or the
        # legacy initial datum plus 60 m spacing. Repeated regrouping must not
        # climb another level each time. Native dynamics still limit climb rate.
        altitude = self._flight_level(own['entity_id'], aircraft)
        if abs(pos[2]-altitude)>5.:
            heading, speed = own['heading_deg'], 0.
        return {'speed_mps': speed, 'heading_deg': heading, 'altitude_m': altitude}

    def _deconflict_surface(self, obs, payloads, policies):
        """Yield early using authorized own positions, drift and public coastlines."""
        if self.brief.get('surface_safety',{}).get('enabled',False):return
        from .planning import navigate
        from .coastal_navigation import CoastalRouter
        tick = obs['tick']
        boats = [u for u in obs['own_entities'] if u['entity_id'] in payloads
                 and not self._air(policies[u['entity_id']]) and u['position_m'][2] <= 50]
        peers = [u for u in obs['own_entities'] if u['position_m'][2] <= 50]
        hazards = self.brief.get('navigation_map', {}).get('land_regions', [])
        if self.coastal_router is None:
            self.coastal_router=CoastalRouter(hazards)
        for own in boats:
            entity, pos = own['entity_id'], own['position_m']
            detour = self.surface_detours.get(entity)
            if detour and (tick > detour['expiry'] or math.dist(pos[:2], detour['goal']) <= 40.):
                self.surface_detours.pop(entity)
                detour = None
            if detour is None:
                conflicts = []
                velocity = own.get('velocity_mps', (0., 0., 0.))
                for peer in peers:
                    if peer['entity_id'] == entity:
                        continue
                    # Stable priority only for another maneuverable boat.
                    # Facilities and disabled hulls always retain right of way.
                    mobile = peer['entity_id'] in payloads and peer.get('capability_flags', {}).get('dynamics', True)
                    if mobile and peer['entity_id'] > entity:
                        continue
                    relative = [peer['position_m'][a]-pos[a] for a in (0, 1)]
                    delta = [peer.get('velocity_mps', (0., 0., 0.))[a]-velocity[a] for a in (0, 1)]
                    square = sum(v*v for v in delta)
                    when = max(0., min(60., -sum(relative[a]*delta[a] for a in (0, 1))/square)) if square else 0.
                    closest = math.hypot(*(relative[a]+when*delta[a] for a in (0, 1)))
                    if math.hypot(*relative) < 180. or closest < 180.:
                        conflicts.append(peer)
                if conflicts:
                    peer = min(conflicts, key=lambda u: (math.dist(pos[:2], u['position_m'][:2]), u['entity_id']))
                    away = [pos[a]-peer['position_m'][a] for a in (0, 1)]
                    length = math.hypot(*away)
                    if length < 1.:
                        angle = math.radians(own['heading_deg'])
                        away, length = [math.cos(angle), -math.sin(angle)], 1.
                    goal = [pos[a]+440.*away[a]/length for a in (0, 1)]
                    route, _ = self.coastal_router.route(pos, goal)
                    if route:
                        detour = {'goal': list(route[0]), 'expiry': tick+120}
                        self.surface_detours[entity] = detour
                    else:
                        payloads[entity]['speed_mps'] = 0.
            if detour:
                payloads[entity] = navigate(own, detour['goal'], min(6., policies[entity]['maximum_speed_mps']),
                                            altitude=pos[2], arrival_radius=40.)
                if math.dist(pos[:2],detour['goal'])>40. and payloads[entity]['speed_mps']==0.:
                    # A native surface hull has little rudder authority at rest.
                    # Keep steerage while turning, with the earlier prediction
                    # window allowing room for that maneuver; do not change
                    # aircraft navigation or request an impossible pivot.
                    payloads[entity]['speed_mps']=min(3.,policies[entity]['maximum_speed_mps'])

        # A surface pursuer must also respect public mainland geometry when
        # its contact/traffic goal lies across land. Do not rely on a native
        # grounding stop as the normal navigation policy.
        for own in boats:
            entity=own['entity_id'];nav=payloads[entity];pos=own['position_m']
            if nav['speed_mps']<=0 or not hazards:continue
            heading=math.radians(nav['heading_deg']);reach=max(600.,nav['speed_mps']*90.)
            goal=[pos[0]+reach*math.sin(heading),pos[1]+reach*math.cos(heading)]
            if self.coastal_router.clear_segment(pos,goal):continue
            candidates=[]
            for offset in (0,-15,15,-30,30,-60,60,-90,90,-120,120,-150,150,180):
                angle=heading+math.radians(offset)
                point=[pos[0]+reach*math.sin(angle),pos[1]+reach*math.cos(angle)]
                if self.coastal_router.clear_segment(pos,point):candidates.append((abs(offset),point))
            if candidates:
                point=min(candidates,key=lambda item:item[0])[1]
                angle=math.degrees(math.atan2(point[0]-pos[0],point[1]-pos[1]))%360
                error=abs((angle-own['heading_deg']+180)%360-180)
                payloads[entity]={**nav,'heading_deg':angle,'speed_mps':min(nav['speed_mps'],3.) if error>25 else nav['speed_mps']}
            else:nav['speed_mps']=0.

    def act(self, obs):
        from .agent import navigation_batch
        self.multirole_support.confirm(obs)
        self._confirm_launches(obs)
        contacts, velocities = self._contacts(obs)
        for contact in contacts:
            domain = 'air' if contact['estimated_position_m'][2] > 50 else 'surface'
            self.seen_threats[domain].add(contact['contact_id'])
        if self.mode=='adaptive':
            # An outward contact can turn back on the next observation. Retain
            # its history and reevaluate; this grants neither a decoy label nor
            # neutralization credit and never discards a near-perimeter track.
            contacts=[c for c in contacts if not self._departing_air_contact(
                c,obs['own_entities'],velocities[c['contact_id']])]
        if self.mode == 'adaptive' and self.brief.get('planned_waves'):
            released = {domain:sum(w.get(domain, 0) for w in self.brief['planned_waves']
                                   if w['spawn_tick'] <= obs['tick']) for domain in ('air','surface')}
            # A declared empty armed phase needs observation, not interception.
            # Keep all track histories without inferring any hidden identity.
            contacts = [c for c in contacts if released[
                'air' if c['estimated_position_m'][2] > 50 else 'surface'] > 0]
        if self.last_air_contact_tick is None or any(
                c['estimated_position_m'][2] > 50 for c in contacts):
            self.last_air_contact_tick = obs['tick']
        organic_ids = {c['contact_id'] for c in obs['organic_contacts']}
        fire_handles = set(organic_ids)
        if self.mode == 'adaptive':
            # A delivered native shared handle is fireable while its snapshot
            # is valid. Reports are immutable; use only very recent snapshots
            # in this baseline, and let native authority/expiry checks decide.
            fire_handles.update(c['contact_id'] for c in obs['shared_contacts']
                                if type(c.get('delivered_tick')) is int
                                and 0 <= c['delivered_tick'] <= obs['tick']
                                and 0 <= c.get('age_ticks', 999) <= 2)
        units = sorted((own for own in obs['own_entities']
                        if own['entity_id'] in obs['controlled_entity_ids'] and self._policy(own)),
                       key=lambda r: r['entity_id'])
        policies = {r['entity_id']: self._policy(r) for r in units}
        for own in units:
            if self._air(policies[own['entity_id']]):
                self.air_patrol_stations.setdefault(own['entity_id'],list(own['position_m'][:2]))
            elif self.mode == 'adaptive' and self.surface_patrol.enabled:
                self.surface_patrol.remember(own)
        if self.regroup_base_altitude is None:
            self.regroup_base_altitude = min(
                (r['position_m'][2] for r in units if self._air(policies[r['entity_id']])),
                default=0.)
        if self.initial_rounds is None:
            self.initial_rounds={domain:sum(self._rounds(u, policies[u['entity_id']]) for u in units
                if self._air(policies[u['entity_id']])==(domain=='air')) for domain in ('air','surface')}
        urgency = {c['contact_id']: self._urgency(c, obs['own_entities'], velocities[c['contact_id']])
                   for c in contacts}
        ordered = sorted(contacts, key=lambda c: (urgency[c['contact_id']], c['contact_id']))
        assignments = {}
        if self.mode == 'adaptive':
            # Give separate threats coverage before assigning spare interceptors.
            available = [u for u in units if u['capability_flags'].get('weapon', False)]
            while available:
                made_assignment = False
                for contact in ordered:
                    compatible = [u for u in available if self._compatible(policies[u['entity_id']], contact)
                                  and self._rounds(u, policies[u['entity_id']]) > 0]
                    if not compatible:
                        continue
                    def cost(unit):
                        p = policies[unit['entity_id']]
                        desired = (p['maximum_range_m'] * self._air_standoff_fraction()
                                   if self._air(p) else self.surface_patrol.standoff(p)
                                   if self.surface_patrol.enabled else p['maximum_range_m'] * .55)
                        return max(0., math.dist(unit['position_m'], contact['estimated_position_m'])
                                   - desired) / p['maximum_speed_mps']
                    unit = min(compatible, key=lambda u: (cost(u), u['entity_id']))
                    assignments[unit['entity_id']] = contact
                    available.remove(unit)
                    made_assignment = True
                if not made_assignment:
                    break
        else:
            for own in units:
                compatible = [c for c in contacts if self._compatible(policies[own['entity_id']], c)]
                if compatible:
                    assignments[own['entity_id']] = min(compatible, key=lambda c:
                        (math.dist(own['position_m'], c['estimated_position_m']), c['contact_id']))

        payloads, fires = {}, []
        for own in units:
            identifier = own['entity_id']; policy = policies[identifier]
            if not own['capability_flags'].get('dynamics', True):
                continue
            contact = assignments.get(identifier)
            position = own['position_m']
            if contact is None or self._rounds(own, policy) <= 0:
                if self.mode == 'adaptive' and not self._air(policy) and self.surface_patrol.enabled:
                    payloads[identifier] = self.surface_patrol.patrol(own, policy)
                elif (self._air(policy) and (self._air_patrol_enabled()
                        or self.mode == 'adaptive' and (self._rounds(own, policy) <= 0
                             or obs['tick']-self.last_air_contact_tick >= 15))):
                    payloads[identifier] = self._regroup_aircraft(own, policy, obs)
                else:
                    payloads[identifier] = {'speed_mps': 0., 'heading_deg': own['heading_deg'], 'altitude_m': position[2]}
                continue
            target = list(contact['estimated_position_m'])
            if self.mode == 'adaptive':
                lead = min(25., math.dist(position, target) / policy['missile_cruise_speed_mps'])
                target = [target[a] + velocities[contact['contact_id']][a] * lead for a in range(3)]
            if self.mode == 'adaptive' and not self._air(policy) and self.surface_patrol.enabled:
                payloads[identifier] = self.surface_patrol.intercept(own, policy, target)
                continue
            distance = math.dist(position[:2], target[:2])
            heading = math.degrees(math.atan2(target[0] - position[0], target[1] - position[1])) % 360
            desired = (policy['maximum_range_m'] *
                       (self._air_standoff_fraction() if self._air(policy) else .7)
                       if self.mode == 'adaptive' else 0.)
            speed = min(policy['maximum_speed_mps'], max(0., distance - desired) / 3.)
            error = abs((heading - own['heading_deg'] + 180) % 360 - 180)
            if error > 45:
                speed = 0. if self._air(policy) else min(speed, 3.)
            payloads[identifier] = {'speed_mps': speed, 'heading_deg': heading, 'altitude_m': position[2]}
            if (self.mode == 'adaptive' and self._air(policy) and self._air_patrol_enabled()
                    and distance <= desired+20.):
                # Keep an area defence patrol while a target is already inside
                # standoff range. Fire selection below still uses the original
                # scoped contacts, ammunition reservations and native authority.
                payloads[identifier] = self._regroup_aircraft(own, policy, obs)

        committed = {}
        requested_now = {}
        requested_by_domain = {'air':0, 'surface':0}
        # Pending requests are temporary planning reservations, not confirmed
        # expenditure or kill evidence. Release rejected requests after the
        # native application window, and confirm only from owned ammunition.
        for shot in self.pending.values():
            key = self._track_key(shot['contact_id'])
            committed[key] = committed.get(key, 0) + 1
            requested_now[key] = requested_now.get(key, 0) + 1
            domain = 'air' if self._air(shot['policy']) else 'surface'
            requested_by_domain[domain] += 1
        remaining_by_domain = {domain:sum(self._rounds(u,policies[u['entity_id']]) for u in units
            if u['capability_flags'].get('weapon', False)
            and self._air(policies[u['entity_id']])==(domain=='air')) for domain in ('air','surface')}
        for s in self.inflight:
            key = self._track_key(s['contact_id'])
            committed[key] = committed.get(key, 0) + s['shots']
        for shot in self.multirole_support.reservations():
            key = self._track_key(shot['contact_id'])
            committed[key] = committed.get(key, 0) + shot.get('shots', 1)
        ready = [own for own in units if own['capability_flags'].get('weapon', False)
                 and (self.mode != 'adaptive' or own['entity_id'] not in self.pending)
                 and own['entity_id'] not in self.multirole_support.pending
                 and own.get('weapon_states', {}).get(policies[own['entity_id']]['weapon_ref'], {}).get('available', True)
                 and own['cooldown_ticks'].get(policies[own['entity_id']]['weapon_ref'], 0) <= 0
                 and self._rounds(own, policies[own['entity_id']]) > 0]
        # Spend a nearby ready carrier's round before a distant carrier claims
        # the shared in-flight reservation. Explicit False retains legacy order.
        if self.mode == 'adaptive' and self.brief['defence'].get('air_fire_nearest_first', True):
            fire_age = self.brief['defence'].get('maximum_air_fire_contact_age_ticks', math.inf)
            air_contacts = [c for c in contacts if c['contact_id'] in fire_handles
                            and c['age_ticks'] <= fire_age and c['estimated_position_m'][2] > 50]
            def nearest(own):
                policy = policies[own['entity_id']]
                distances = [math.dist(own['position_m'], c['estimated_position_m'])
                             for c in air_contacts]
                distances = [d for d in distances if policy['minimum_range_m'] <= d <= policy['maximum_range_m']]
                return min(distances, default=math.inf), own['entity_id']
            aircraft = iter(sorted((u for u in ready if self._air(policies[u['entity_id']])), key=nearest))
            # Replace only aircraft slots, preserving surface allocation order.
            ready = [next(aircraft) if self._air(policies[u['entity_id']]) else u for u in ready]
        for own in ready:
            identifier = own['entity_id']; policy = policies[identifier]
            candidates = [c for c in contacts if c['contact_id'] in fire_handles
                          and self._compatible(policy, c)
                          and policy['minimum_range_m'] <= math.dist(own['position_m'], c['estimated_position_m']) <= policy['maximum_range_m']]
            if self.mode == 'adaptive':
                domain='air' if self._air(policy) else 'surface'
                if domain == 'surface' and self.surface_patrol.enabled:
                    surface = self.surface_patrol.config
                    candidates = [c for c in candidates
                        if c['track_quality'] > surface.get('fire_contact_confidence',
                            self.brief['defence']['contact_confidence'])
                        and c['age_ticks'] <= surface.get('maximum_fire_contact_age_ticks', 2)]
                fire_age = self.brief['defence'].get('maximum_air_fire_contact_age_ticks')
                if domain == 'air' and fire_age is not None:
                    # Retain older tracks for navigation, but require a recent
                    # public measurement before spending another air round.
                    # Silence is not a kill confirmation or fire authority.
                    candidates = [c for c in candidates if c['age_ticks'] <= fire_age]
                if remaining_by_domain[domain]-requested_by_domain[domain] <= self._future_reserve(domain,obs['tick']):
                    continue
                planned_count=self.brief.get('planned_threat_counts', {}).get(domain, 0)
                spare_air_retries = (domain == 'air' and self.brief['defence'].get(
                    'air_retry_from_spare_reserve', False))
                commit_reserve = self._commit_remaining_reserve(obs['tick'])
                if self.brief.get('planned_waves') and planned_count and not commit_reserve:
                    # Releasing a wave does not mean it has been engaged. Keep
                    # its first-attempt budget until launches are confirmed by
                    # owned ammunition depletion (the existing spent history).
                    # One source observer's handles avoid summing aliases.
                    engaged_count = max((sum(self._spent_count(key)>0 for key in {self._track_key(k) for k in seen[domain]})
                        for seen in self.seen_threats_by_observer.values()), default=0)
                    expected_count = planned_count + (self.brief.get('planned_decoy_count', 0)
                                                       if spare_air_retries else 0)
                    unattempted = max(0, expected_count-engaged_count)
                    nominal = self.brief.get('combat_reference', {}).get('planning_minimum_hits_per_threat')
                    if nominal is None:
                        nominal = self.brief.get('performance_scoring', {}).get('nominal_rounds_per_threat', {})
                    first_attempt_reserve = unattempted * nominal.get(domain, 2 if domain=='air' else 1)
                    if remaining_by_domain[domain]-requested_by_domain[domain] <= first_attempt_reserve:
                        # A first attempt can consume its reserved round. An
                        # already attempted track must wait for spare rounds.
                        candidates = [c for c in candidates if self._spent_count(c['contact_id'])==0]

                late_floor = self.brief['defence'].get('late_air_first_attempt_reserve_rounds', 0)
                if domain == 'air' and commit_reserve and planned_count and late_floor:
                    expected = planned_count + self.brief.get('planned_decoy_count', 0)
                    engaged = max((sum(self._spent_count(key)>0 for key in
                        {self._track_key(k) for k in seen['air']})
                        for seen in self.seen_threats_by_observer.values()), default=0)
                    reserve = min(late_floor, max(0, expected-engaged))
                    # Releasing the retry cap must not erase every first-attempt
                    # round before a distant final arrival can be observed. All
                    # unclassified air contacts, including declared decoys, count.
                    if remaining_by_domain[domain]-requested_by_domain[domain] <= reserve:
                        candidates = [c for c in candidates if self._spent_count(c['contact_id']) == 0]

                cap=(max(policy['salvo_rounds'],math.ceil(self.initial_rounds[domain]/planned_count))
                     if planned_count else math.inf)
                if spare_air_retries:
                    # First-attempt and future-wave budgets already gate retries.
                    # An average per-target allowance must not strand spare stock
                    # when a fresh surviving contact needs another attempt.
                    cap = math.inf
                # Once all publicly planned tracks have been observed, reuse
                # spare rounds instead of permanently abandoning survivors.
                # Missing contacts are only a planning heuristic, not kill credit.
                expected_tracks = planned_count+(self.brief.get('planned_decoy_count',0) if domain=='air' else 0)
                all_released = all(w['spawn_tick']<=obs['tick'] for w in self.brief.get('planned_waves', []))
                # One native target has separate stable handles at different observers.
                # A count within a single observer gives a conservative lower bound;
                # summing observer aliases cannot prove that an unseen wave was observed.
                observed_count = max((len({self._track_key(k) for k in seen[domain]}) for seen in
                                      self.seen_threats_by_observer.values()), default=0)
                if commit_reserve or (planned_count and all_released and observed_count >= expected_tracks):
                    cap = math.inf
                # A first wave cannot consume the rounds budgeted for future,
                # publicly declared threats. Failed launch requests cost nothing.
                candidates = [c for c in candidates if committed.get(self._track_key(c['contact_id']), 0) < self._salvo_limit(policy)
                              and self._spent_count(c['contact_id'])+requested_now.get(self._track_key(c['contact_id']), 0)<cap]
                candidates = [c for c in candidates if not self._wait_for_contact_confirmation(
                    c['contact_id'], policy, obs['tick'], urgency[c['contact_id']])]
                assigned = assignments.get(identifier)
                candidates.sort(key=lambda c: (assigned is None or c['contact_id'] != assigned['contact_id'],
                                               urgency[c['contact_id']], committed.get(self._track_key(c['contact_id']), 0),
                                               math.dist(own['position_m'], c['estimated_position_m']), c['contact_id']))
            else:
                candidates.sort(key=lambda c: (math.dist(own['position_m'], c['estimated_position_m']), c['contact_id']))
            if not candidates:
                continue
            c = candidates[0]; key = c['contact_id']
            fires.append((identifier, {'weapon_ref': policy['weapon_ref'], 'contact_id': key}))
            delta = [c['estimated_position_m'][a] - own['position_m'][a] for a in range(3)]
            distance = math.dist(delta, (0, 0, 0))
            radial = sum(delta[a] * velocities[key][a] for a in range(3)) / max(1., distance)
            flight = min(policy['missile_max_flight_ticks'], math.ceil(distance / max(5., policy['missile_cruise_speed_mps'] - radial)) + 8)
            grace = self.brief['defence'].get('surface_inflight_grace_ticks', 0)
            if self.mode == 'adaptive' and not self._air(policy) and grace:
                # Straight-line travel can underestimate a turning pursuit.
                # A bounded wait saves stock, but may delay a necessary retry.
                # The two ticks cover request application, not a kill report.
                flight = min(policy['missile_max_flight_ticks'] + 2, flight + grace)
            self.pending[identifier] = {'contact_id': key, 'rounds_before': self._rounds(own, policy),
                                        'policy': policy, 'expires_tick': obs['tick'] + flight,
                                        'verify_until_tick': obs['tick'] + 2}
            track_key = self._track_key(key)
            committed[track_key] = committed.get(track_key, 0) + 1
            requested_now[track_key] = requested_now.get(track_key, 0) + 1
            requested_by_domain['air' if self._air(policy) else 'surface'] += 1
        if self.mode == 'adaptive':
            self._deconflict_surface(obs, payloads, policies)
        batch = navigation_batch(obs, payloads, fires) if payloads or fires else None
        return self.multirole_support.augment(obs, batch, self, contacts, fire_handles, velocities)


class AirLanePlanner(InterceptPlanner):
    def act(self, obs):
        batch = super().act(obs)
        if batch is None:
            return batch
        policies = self.brief['own_weapon_policies']
        aircraft = sorted(key for key, policy in policies.items() if self._air(policy))
        own_by_id = {unit['entity_id']: unit for unit in obs['own_entities']}
        for command in batch['persistent_commands']:
            identifier = command['entity_id']
            if identifier not in aircraft:
                continue
            own = own_by_id[identifier]
            # Maneuver and regroup share the declared flight plan (or legacy
            # 60 m spacing), retaining ranks when a teammate is lost.
            altitude = self._flight_level(identifier, aircraft)
            command['payload']['altitude_m'] = altitude
            if abs(own['position_m'][2] - altitude) > 5.:
                command['payload'].update(speed_mps=0., heading_deg=own['heading_deg'])
        return batch

