"""Adapted from the paper project's AttackProfileDriverV2.

Route selection, deterministic lanes, heading patterns, speed cycles and
assigned-target ammunition reservation retain the original logic. Observation
and submission use the new principal-scoped DTOs; raw IDs are never decoded.
"""
import hashlib
import math
from .agent import navigation_batch
from .coastal_navigation import CoastalRouter


class AttackPolicy:
    def __init__(self, plan, entity_tags, seed=7):
        self.plan = plan
        from .surface_safety import SurfaceSafety
        self.safety=SurfaceSafety(plan.get('surface_safety'),plan.get('surface_navigation_map',{}).get('land_regions',[]))
        self.tags = entity_tags
        self.last_attempt = {}
        self.seed = seed
        self.assigned_engaged = set()
        self.executed_shots = {}
        self.pending = {}
        self.waypoint_indices = {}
        self.surface_reports = {}
        self.surface_detours = {}
        self.coastal_router = None
        self.weapon_shots = {}
        self.assigned_weapon_engaged = set()
        self.assigned_weapon_shots = {}
        self.initial_positions = {}
        self.firing_stations = {}
        self.air_station_legs = {}
        self.facility_search_indices = {}
        self.facility_search_since = {}
        self.completed_facility_search = set()
        from .surface_arrival import SurfaceArrival
        self.surface_arrival=SurfaceArrival(plan['surface_arrival']) if plan.get('surface_arrival') else None
        self.assault_withdrawals = {}
        self.station_clearance_launches = []

    def _surface_peers(self, obs):
        """Retain fresh own-state reports across individual native controllers."""
        if self.safety.enabled:
            return [dict(tick=obs['tick'],entity_id=u['entity_id'],position_m=tuple(u['position_m']),
                         velocity_mps=tuple(u.get('velocity_mps',(0.,0.,0.))),
                         force_yield=u.get('lifecycle_state','active') not in {'active','degraded'})
                    for u in obs['own_entities'] if 'boat' in self.tags.get(u['entity_id'],())]
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
        if self.safety.enabled:return None
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
        if self.plan['attack'].get('non_suicidal', False):
            for dispatch in dispatches:
                request = dispatch.get('request_id')
                pending = self.pending.get(request)
                if pending is None:
                    continue
                if dispatch.get('status') == 'rejected':
                    self.pending.pop(request, None)
                    continue
                execution = dispatch.get('execution') or {}
                # Transport acceptance is not a native launch. Only the combat
                # execution's weapon and expenditure can release this reserve.
                if (dispatch.get('status') == 'executed'
                        and execution.get('weapon_ref') == pending['weapon_ref']
                        and execution.get('attacker_id') == pending['entity_id']
                        and execution.get('requested_shots', 0) > 0):
                    self.pending.pop(request, None)
                    self._record_launch(pending, execution['requested_shots'])
            return
        for dispatch in dispatches:
            pending = self.pending.pop(dispatch['request_id'], None)
            if pending and dispatch['status'] == 'executed':
                entity, assigned = pending
                self.executed_shots[entity] = self.executed_shots.get(entity, 0) + 1
                if assigned:
                    self.assigned_engaged.add(entity)

    def _record_launch(self, pending, shots):
        entity, weapon = pending['entity_id'], pending['weapon_ref']
        if pending.get('clears_blocked_station'):
            self.station_clearance_launches.append({'entity_id':entity,'weapon_ref':weapon,
                'requested_tick':pending['requested_tick'],'native_shots':shots})
        key = (entity, weapon)
        self.weapon_shots[key] = self.weapon_shots.get(key, 0) + shots
        self.executed_shots[entity] = self.executed_shots.get(entity, 0) + shots
        if pending['assigned']:
            self.assigned_weapon_engaged.add(key)
            self.assigned_weapon_shots[key] = self.assigned_weapon_shots.get(key, 0) + shots
            self.assigned_engaged.add(entity)

    def _confirm_owned_launches(self, obs):
        owned = {unit['entity_id']: unit for unit in obs['own_entities']}
        for request, pending in list(self.pending.items()):
            unit = owned.get(pending['entity_id'])
            if unit is not None:
                spent = pending['rounds_before'] - unit.get('ammunition', {}).get(
                    pending['ammunition_ref'], 0)
                if spent > 0:
                    self._record_launch(pending, min(1, spent))
                    self.pending.pop(request, None)
                    continue
            if obs['tick'] > pending['verify_until_tick']:
                self.pending.pop(request, None)

    def _weapon_policies(self, entity):
        tags = self.tags.get(entity, ())
        return [p for p in self.plan['attack']['weapon_policies']
                if (not p.get('match_tag') or p['match_tag'] in tags)
                and (not p.get('entity_ids') or entity in p['entity_ids'])]

    def _contact_domain(self, contact):
        # Public fixed objectives are declarations, not hidden entity IDs. A
        # declared coordinate never creates a contact or grants fire authority.
        attack = self.plan['attack']
        point = contact['estimated_position_m']
        declared = contact.get('estimated_domain', contact.get('domain'))
        radius = attack.get('facility_match_radius_m', 100.)
        domains = attack.get('facility_domains', {})
        facilities = sorted(((name, position) for name, position in
                             self.plan.get('facility_positions', {}).items()
                             if declared not in {'air', 'surface', 'land'}
                             or declared == domains.get(name, 'land')),
                            key=lambda row: math.dist(point, row[1]))
        if facilities and math.dist(point, facilities[0][1]) <= radius:
            name = facilities[0][0]
            return domains.get(name, 'land'), name
        if declared in {'air', 'surface', 'land'}:
            return declared, None
        # Native raw-contact DTOs currently omit classification. This declared
        # measurement-height heuristic is explicit, never a truth lookup.
        return ('air' if point[2] > attack.get('air_contact_min_altitude_m', 50.)
                else 'surface'), None

    def _remaining(self, own, policy):
        rounds = own.get('ammunition', {}).get(policy['ammunition_ref'], 0)
        cap = policy.get('shots', math.inf)
        spent = self.weapon_shots.get((own['entity_id'], policy['weapon_ref']), 0)
        return max(0, min(rounds, cap - spent))

    def _firing_station(self, own, objective, route, surface):
        explicit = route.get('firing_station', {}).get('objective_m')
        if explicit is not None:
            return list(explicit[:2])
        entity = own['entity_id']
        key = (entity, tuple(objective))
        if key in self.firing_stations:
            return self.firing_stations[key]
        origin = self.initial_positions[entity]
        distance = route.get('arrival_standoff_m', 1800. if surface else 2200.)
        bearing = math.atan2(origin[1] - objective[1], origin[0] - objective[0])
        for scale in (1., 1.15, .85, 1.35):
            for offset in (0, 15, -15, 30, -30, 60, -60, 90, -90, 120, -120, 180):
                angle = bearing + math.radians(offset)
                point = [objective[0] + distance * scale * math.cos(angle),
                         objective[1] + distance * scale * math.sin(angle)]
                if not surface or self.coastal_router.clear_point(point):
                    path, _ = (self.coastal_router.route(origin, point)
                               if surface else ([point], 0.))
                    if path:
                        self.firing_stations[key] = point
                        return point
        # No valid water station is an explicit hold, never a route through land.
        self.firing_stations[key] = list(origin[:2])
        return self.firing_stations[key]

    def _station_patrol(self, own, station, route, tick):
        entity, pos = own['entity_id'], own['position_m']
        radius = route.get('air_station_radius_m', 220.)
        points = [[station[0] + radius * math.cos(n * math.tau / 12),
                   station[1] + radius * math.sin(n * math.tau / 12)] for n in range(12)]
        leg = self.air_station_legs.setdefault(entity, min(
            range(len(points)), key=lambda n: math.dist(pos[:2], points[n])))
        if math.dist(pos[:2], points[leg]) <= 50.:
            leg = (leg + 1) % len(points)
            self.air_station_legs[entity] = leg
        nav = self._navigation(entity, pos, points[leg], {**route, 'pattern': 'direct'}, tick)
        nav['speed_mps'] = min(nav['speed_mps'], route.get('air_station_speed_mps', 8.))
        return nav

    def _facility_search_route(self, own, primary, route, contacts, tick):
        """Follow declared search stations after observation silence, not a kill.

        The static scene audit owns reachability/visibility of every fallback
        station. No enemy health, lifecycle or contact-to-truth IDs are used.
        """
        entity = own['entity_id']
        facilities = self.plan.get('facility_positions', {})
        search = [{'target_tag': primary, 'firing_station': route.get('firing_station', {})}]
        search += [entry for entry in route.get('facility_search', [])
                   if entry.get('target_tag') in facilities
                   and entry.get('firing_station', {}).get('objective_m') is not None]
        index = self.facility_search_indices.get(entity, 0)
        if entity in self.completed_facility_search:
            return primary, {**route, '_search_complete': True}
        current = search[index]
        current_tag = current['target_tag']
        effective = route if index == 0 else {**route,
            'firing_station': current['firing_station'], 'waypoints': []}
        objective = facilities.get(current_tag, route.get('objective_m', own['position_m']))
        station = self._firing_station(own, objective, effective, 'boat' in self.tags.get(entity, ()))
        radius = 100. if 'boat' in self.tags.get(entity, ()) else route.get('air_station_radius_m', 220.) + 100.
        visible = any(self._contact_domain(c)[1] == current_tag for c in contacts)
        if visible or math.dist(own['position_m'][:2], station) > radius:
            self.facility_search_since.pop(entity, None)
            return current_tag, effective
        since = self.facility_search_since.setdefault(entity, tick)
        wait = max(30, self.plan['attack'].get('facility_search_wait_ticks', 30))
        if tick - since < wait:
            return current_tag, effective
        self.facility_search_since.pop(entity, None)
        index += 1
        self.facility_search_indices[entity] = index
        self.waypoint_indices[entity] = 0
        if index >= len(search):
            self.completed_facility_search.add(entity)
            return current_tag, {**effective, '_search_complete': True}
        current = search[index]
        return current['target_tag'], {**route,
            'firing_station': current['firing_station'], 'waypoints': []}

    def _surviving_navigation(self, own, tags, route, objective, policies, peers, traffic, tick):
        entity, pos = own['entity_id'], own['position_m']
        surface = 'boat' in tags
        self.initial_positions.setdefault(entity, tuple(pos))
        strike = [p for p in policies if {'land', 'surface'} & set(p.get('target_domains', []))]
        exhausted = (route.get('_search_complete', False)
                     or not any(self._remaining(own, p) > 0 for p in policies)
                     or bool(strike) and route.get('withdraw_after_strike',
                         self.plan['attack'].get('withdraw_after_strike', True))
                     and not any(self._remaining(own, p) > 0 for p in strike))
        if self.plan['attack'].get('mission_completion',{}).get('enabled'):
            if exhausted:
                reason = ('search_complete' if route.get('_search_complete',False)
                          else 'all_rounds_expended' if not any(self._remaining(own,p)>0 for p in policies)
                          else 'strike_expended')
                self.assault_withdrawals.setdefault(entity,{'commit_tick':tick,'reason':reason})
            exhausted = entity in self.assault_withdrawals
        if exhausted:
            goal = route.get('withdrawal', {}).get('objective_m', self.initial_positions[entity])[:2]
        else:
            goal = self._firing_station(own, objective, route, surface)
        waypoints = [] if exhausted else route.get('waypoints', [])
        index = 0 if exhausted else self.waypoint_indices.get(entity, 0)
        while index < len(waypoints) and math.dist(
                pos[:2], waypoints[index]['objective_m'][:2]) <= waypoints[index].get('arrival_radius_m', 80.):
            index += 1
        self.waypoint_indices[entity] = index
        if index < len(waypoints):
            goal = waypoints[index]['objective_m'][:2]
        if not surface:
            if index == len(waypoints) and math.dist(pos[:2], goal) <= route.get('air_station_radius_m', 220.) + 100.:
                return self._station_patrol(own, goal, route, tick)
            return self._navigation(entity, pos, goal, route, tick)
        evasion = self._surface_evasion(own, [*peers, *traffic], tick)
        actual_goal = evasion if evasion is not None else goal
        path, _ = self.coastal_router.route(pos, actual_goal)
        if exhausted and self.surface_arrival is not None and path:
            cruise=self._navigation(entity,pos,path[0],{**route,'pattern':'direct'},tick)['speed_mps']
            if self.surface_arrival.should_control(own,goal,cruise):
                return self.surface_arrival.command(own,goal,path[0],cruise,tick)
        if not path or math.dist(pos[:2], actual_goal) <= 35.:
            return {'speed_mps': 0., 'heading_deg': own.get('heading_deg', 0.), 'altitude_m': pos[2]}
        nav = self._navigation(entity, pos, path[0], {**route, 'pattern': 'direct'}, tick)
        nav['speed_mps'] = min(nav['speed_mps'], math.dist(pos[:2], path[0]) / 3.)
        error = abs((nav['heading_deg'] - own.get('heading_deg', nav['heading_deg']) + 180) % 360 - 180)
        if error > 25. or evasion is not None:
            nav['speed_mps'] = min(nav['speed_mps'], 3.)
        # Even a bad waypoint must not turn a surviving armed hull into a ram.
        exclusion = self.plan['attack'].get('facility_collision_clearance_m', 180.)
        for facility in self.plan.get('facility_positions', {}).values():
            dx, dy = facility[0] - pos[0], facility[1] - pos[1]
            heading = math.radians(nav['heading_deg'])
            approaching = dx * math.sin(heading) + dy * math.cos(heading) > 0
            nav['speed_mps'] = min(nav['speed_mps'], max(0., math.hypot(dx, dy) - exclusion) / 3.) if approaching else nav['speed_mps']
        return nav

    def _act_homogeneous(self, obs):
        tick, attack = obs['tick'], self.plan['attack']
        if self.coastal_router is None:
            self.coastal_router = CoastalRouter(self.plan.get('surface_navigation_map', {}).get('land_regions', []))
        self._confirm_owned_launches(obs)
        contacts = [c for c in obs['organic_contacts']
                    if c.get('detected') and c.get('confirmed')
                    and 0 <= c.get('age_ticks', 999) <= attack.get('maximum_fire_contact_age_ticks', 2)
                    and c.get('track_quality', 0) > attack.get('contact_confidence', .4)]
        peers = self._surface_peers(obs)
        traffic = [{'entity_id': 'observed.' + c['contact_id'],
                    'position_m': tuple(c['estimated_position_m']),
                    'velocity_mps': (0., 0., 0.), 'force_yield': True}
                   for c in contacts if self._contact_domain(c) == ('surface', None)]
        payloads, fires = {}, []
        for own in obs['own_entities']:
            entity = own['entity_id']
            if entity not in obs['controlled_entity_ids']:
                continue
            tags = self.tags.get(entity, ())
            route = self._route_for(tags, tick, entity)
            target_tag = next((t[7:] for t in tags if t.startswith('target.')), None)
            objective = self.plan.get('facility_positions', {}).get(
                target_tag, route.get('objective_m', self.plan.get('objective_m', own['position_m'])))
            if 'decoy' in tags:
                payloads[entity] = self._navigation(entity, own['position_m'],
                    route.get('objective_m', objective), route, tick)
                continue
            self.initial_positions.setdefault(entity, tuple(own['position_m']))
            current_tag, route = self._facility_search_route(own, target_tag, route, contacts, tick)
            objective = self.plan.get('facility_positions', {}).get(current_tag, objective)
            policies = self._weapon_policies(entity)
            payloads[entity] = self._surviving_navigation(
                own, tags, route, objective, policies, peers, traffic, tick)
            if (entity in self.assault_withdrawals
                    or not own.get('capability_flags', {}).get('weapon', False)
                    or any(p['entity_id'] == entity for p in self.pending.values())):
                continue
            candidates = []
            for policy in policies:
                weapon = policy['weapon_ref']
                rounds = self._remaining(own, policy)
                if (rounds <= 0 or own.get('cooldown_ticks', {}).get(weapon, 0) > 0
                        or not own.get('weapon_states', {}).get(weapon, {}).get('available', True)):
                    continue
                # Keep the still-owed objective quota even if the first round
                # launched or its contact vanished. A launch is not a kill.
                reserve = max(0, policy.get('reserve_for_assigned', 0)
                              - self.assigned_weapon_shots.get((entity, weapon), 0))
                for contact in contacts:
                    domain, facility = self._contact_domain(contact)
                    if domain not in policy.get('target_domains', []):
                        continue
                    if attack.get('assigned_facility_only', False) and (
                            facility is None or facility != current_tag):
                        continue
                    # The quota belongs to declared fixed objectives. A fresh
                    # alternate facility may use it; an observed patrol hull may
                    # not consume it merely because the primary went silent.
                    assigned = facility is not None
                    distance = math.dist(own['position_m'], contact['estimated_position_m'])
                    if not policy.get('min_range_m', 0.) <= distance <= policy['max_range_m']:
                        continue
                    from .surface_approach import can_clear_blocked_station
                    clears_station = can_clear_blocked_station(attack,tags,own,contact,domain,facility,
                        self._firing_station(own,objective,route,True)) if 'boat' in tags else False
                    if not assigned and rounds <= reserve and not clears_station:
                        continue
                    if tick - self.last_attempt.get((entity, weapon, contact['contact_id']), -1000) < policy.get('retry_interval_ticks', 5):
                        continue
                    priority = (0 if facility == target_tag and facility is not None
                                else 1 if assigned else 2, policy.get('priority', 0), distance,
                                weapon, contact['contact_id'])
                    candidates.append((priority, policy, contact, assigned, clears_station))
            if not candidates:
                continue
            _, policy, contact, assigned, clears_station = min(candidates, key=lambda row: row[0])
            weapon = policy['weapon_ref']
            fires.append((entity, {'weapon_ref': weapon, 'contact_id': contact['contact_id']}))
            self.last_attempt[entity, weapon, contact['contact_id']] = tick
            self.pending[f'fire.{entity}.{tick}'] = {
                'entity_id': entity, 'weapon_ref': weapon, 'ammunition_ref': policy['ammunition_ref'],
                'rounds_before': own.get('ammunition', {}).get(policy['ammunition_ref'], 0),
                'assigned': assigned, 'verify_until_tick': tick + 2,
                'clears_blocked_station':clears_station,'requested_tick':tick}
        return self.safety.apply(obs,navigation_batch(obs, payloads, fires) if payloads or fires else None)

    def _act_without_evasion(self, obs):
        if self.plan['attack'].get('non_suicidal', False):
            return self._act_homogeneous(obs)
        tick = obs['tick']
        if self.coastal_router is None:
            self.coastal_router=CoastalRouter(
                self.plan.get('surface_navigation_map', {}).get('land_regions', []))
        payloads, fires = {}, []
        peers = (self._surface_peers(obs)
                 if self.plan['attack'].get('surface_separation_m', 0.) else [])
        contacts = {c['contact_id']: c for c in obs['organic_contacts']
                    if c.get('detected') and c.get('confirmed')
                    and c.get('age_ticks', 999) <= 2
                    and c.get('track_quality', 0) > 0}
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
            route = self._route_for(tags, tick, entity)
            target_tag = next((t[7:] for t in tags if t.startswith('target.')), None)
            objective = (route.get('objective_m', self.plan['objective_m']) if 'decoy' in tags
                         else self.plan['facility_positions'][target_tag])
            pos = own['position_m']
            navigation_goal = objective
            waypoints = route.get('waypoints', [])
            index = self.waypoint_indices.get(entity, 0)
            while index < len(waypoints) and math.dist(
                    pos[:2], waypoints[index]['objective_m'][:2]) <= waypoints[index].get('arrival_radius_m', 80.):
                index += 1
            self.waypoint_indices[entity] = index
            if index < len(waypoints):
                navigation_goal = waypoints[index]['objective_m']
            payloads[entity] = self._navigation(entity, pos, navigation_goal, route, tick)
            if 'boat' in tags:
                evasion=self._surface_evasion(own,[*peers,*traffic],tick)
                # A detour changes the next straight approach: route that
                # approach around the actual coast, even after the detour ends.
                # Bounding boxes can wrongly block a navigable concave harbor.
                if evasion is None and self.coastal_router.polygons:
                    coastal_route,_=self.coastal_router.route(pos,navigation_goal)
                    if coastal_route:
                        payloads[entity]=self._navigation(entity,pos,coastal_route[0],
                            {**route,'pattern':'direct'},tick)
                    else:
                        payloads[entity]['speed_mps']=0.
                if evasion is not None:
                    payloads[entity]=self._navigation(entity,pos,evasion,
                        {**route,'pattern':'direct'},tick)
                    payloads[entity]['speed_mps']=min(6.,payloads[entity]['speed_mps'])
                arrival = route.get('arrival_standoff_m', 0.)
                if evasion is None and arrival and index == len(waypoints):
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
            if 'decoy' in tags:
                continue
            policy = next(p for p in self.plan['attack']['weapon_policies'] if p['match_tag'] in tags)
            weapon = policy['weapon_ref']
            rounds = own['ammunition'].get(policy['ammunition_ref'], 0)
            if (not own['capability_flags'].get('weapon') or rounds <= 0
                or self.executed_shots.get(entity, 0) >= policy['shots']
                or own['cooldown_ticks'].get(weapon, 0) > 0):
                continue
            assigned, interception = [], []
            for c in contacts.values():
                distance = math.dist(pos, c['estimated_position_m'])
                if not policy['min_range_m'] <= distance <= policy['max_range_m']:
                    continue
                if tick-self.last_attempt.get((entity, c['contact_id']), -1000) < policy['retry_interval_ticks']:
                    continue
                if math.dist(c['estimated_position_m'], objective) <= 100:
                    assigned.append(c)
                elif (c['estimated_position_m'][2] > 50) == ('uav' in tags):
                    interception.append(c)
            reserve = 0 if entity in self.assigned_engaged else policy['reserve_for_assigned']
            candidates = assigned or (interception if rounds > reserve else [])
            if candidates:
                contact = min(candidates, key=lambda c: (math.dist(pos, c['estimated_position_m']), c['contact_id']))
                fires.append((entity, {'weapon_ref': weapon, 'contact_id': contact['contact_id']}))
                self.last_attempt[entity, contact['contact_id']] = tick
                self.pending[f'fire.{entity}.{tick}'] = (entity, bool(assigned))
        return self.safety.apply(obs,navigation_batch(obs, payloads, fires) if payloads or fires else None)

    def act(self, obs):
        batch = self._act_without_evasion(obs)
        from .assault_completion import append_withdrawal_reports
        batch = append_withdrawal_reports(self,obs,batch)
        config = self.plan['attack'].get('missile_evasion', {})
        if not config.get('enabled') or not batch:
            return batch
        if not hasattr(self, 'evasion_layer'):
            from .opponent_evasion import EvasionLayer
            self.evasion_layer = EvasionLayer(config.get('variant', 'drag25'), config,
                CoastalRouter(self.plan.get('surface_navigation_map', {}).get('land_regions', []), clearance=30.))
            self.evasion_entity_ids = frozenset(config.get('entity_ids', ()))
        own = {unit['entity_id']: unit for unit in obs['own_entities']}
        tracks = self.evasion_layer.observe(obs)
        peers = [unit for unit in obs['own_entities'] if 'uav' in self.tags.get(unit['entity_id'], ())]
        for command in batch.get('persistent_commands', ()):
            identifier = command['entity_id']
            if (command.get('command_type') != 'navigation'
                    or identifier not in self.evasion_entity_ids
                    or identifier not in obs['controlled_entity_ids'] or identifier not in own):
                continue
            command['payload'] = self.evasion_layer.apply(obs, own[identifier],
                command['payload'], tracks, self.tags.get(identifier, ()), peers)
        return batch
