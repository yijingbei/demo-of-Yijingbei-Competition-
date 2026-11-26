"""Deterministic low-speed navigation for independent noncombatant buoys."""
import math
from .agent import navigation_batch


class BuoyNavigation:
    def __init__(self, plans, safety=None):
        self.plans = plans
        self.safety=safety
        for plan in plans.values():
            dwell = plan.get('dwell_ticks_by_point')
            if dwell is not None and (len(dwell) != len(plan['points_m'])
                    or any(type(n) is not int or n < 0 for n in dwell)):
                raise ValueError('per-point dwell must match navigation points')
            control = plan.get('coasting_control')
            if control is not None:
                fields = ('heading_lead_ticks','heading_tolerance_deg','maximum_yaw_rate_deg_per_tick','steering_speed_mps')
                if (any(not isinstance(control.get(k),(int,float)) or isinstance(control.get(k),bool)
                        or not math.isfinite(control[k]) or control[k]<=0 for k in fields)
                        or control['steering_speed_mps']>plan['speed_mps']):
                    raise ValueError('invalid opt-in coasting control')
        self.legs = {}
        self.arrivals = {}
        self.coasting = set()
        self.completed = set()
        self.previous_headings = {}

    def _transfer(self, own, plan, tick):
        entity = own['entity_id']
        points = plan['points_m']
        leg = self.legs.get(entity, 0)
        goal = points[leg]
        pos = own['position_m']
        distance = math.dist(pos[:2], goal)
        actual_speed = math.hypot(*own['velocity_mps'][:2])
        radius = plan['arrival_radius_m']
        settled_speed = plan['settled_speed_mps']
        holding = False
        # Count only a continuous, actually slow visit inside the observation
        # area. Crossing its boundary while coasting is not a completed dwell.
        if distance <= radius and actual_speed <= settled_speed:
            since = self.arrivals.setdefault(entity, tick)
            dwell = plan.get('dwell_ticks_by_point', [plan['dwell_ticks']] * len(points))[leg]
            if tick - since >= dwell:
                if plan.get('hold_at_last_point') and leg == len(points)-1:
                    self.completed.add(entity)
                    holding = True
                else:
                    leg = (leg + 1) % len(points)
                    self.legs[entity] = leg
                    self.arrivals.pop(entity, None)
                    self.coasting.discard(entity)
                    goal = points[leg]
                    distance = math.dist(pos[:2], goal)
            else:
                holding = True
        else:
            self.arrivals.pop(entity, None)
        desired = math.degrees(math.atan2(goal[0] - pos[0], goal[1] - pos[1])) % 360
        error = (desired - own['heading_deg'] + 180) % 360 - 180
        control = plan.get('coasting_control')
        previous = self.previous_headings.get(entity)
        yaw_rate = ((own['heading_deg']-previous[1]+180)%360-180)/(tick-previous[0]) if previous and tick>previous[0] else 0.
        self.previous_headings[entity] = (tick,own['heading_deg'])
        steering_error = error - control['heading_lead_ticks']*yaw_rate if control else error
        heading = (own['heading_deg'] + max(-4., min(4., steering_error))) % 360
        speed = plan['speed_mps']
        # The unchanged native MMG has forward thrust only. Its measured
        # low-speed coast law needs a long braking distance; zero thrust at
        # the waypoint itself cannot stop this vessel in the observation area.
        braking_distance = math.log(max(actual_speed / settled_speed, 1.)) / plan['braking_drag_per_m']
        if distance <= braking_distance + plan['braking_margin_m'] and abs(error) < 20.:
            self.coasting.add(entity)
        if entity in self.coasting:
            speed = 0.
            if control:
                # Native MMG needs propeller wash to counter residual yaw.
                # Short legal thrust corrections retain rudder authority while
                # the native solver alone determines deceleration and arrival.
                if distance>radius and (abs(error)>control['heading_tolerance_deg']
                        or abs(yaw_rate)>control['maximum_yaw_rate_deg_per_tick']):
                    speed = control['steering_speed_mps']
            else:
                heading = own['heading_deg']
            if actual_speed <= settled_speed and distance > radius:
                self.coasting.discard(entity)
                speed = min(settled_speed, max(0., distance - 10.) / 100.)
        if abs(error) > 35.:
            speed = min(speed, .35)
        if holding:
            speed = 0.
            heading = own['heading_deg']
        return dict(speed_mps=speed, heading_deg=heading, altitude_m=0.)

    def act(self, obs):
        payloads = {}
        for own in obs['own_entities']:
            entity = own['entity_id']
            if entity not in obs['controlled_entity_ids'] or entity not in self.plans:
                continue
            plan = self.plans[entity]
            if plan['kind'] == 'transfer':
                payloads[entity] = self._transfer(own, plan, obs['tick'])
                continue
            points = plan['points_m']
            leg = self.legs.get(entity, 0)
            goal = points[leg]
            pos = own['position_m']
            distance = math.dist(pos[:2], goal)
            speed = plan['speed_mps']
            if distance <= plan.get('arrival_radius_m', 20.):
                since = self.arrivals.setdefault(entity, obs['tick'])
                if plan['kind'] == 'station' or obs['tick'] - since < plan.get('dwell_ticks', 0):
                    speed = 0.
                else:
                    leg = (leg + 1) % len(points)
                    self.legs[entity] = leg
                    self.arrivals.pop(entity, None)
                    goal = points[leg]
            desired = math.degrees(math.atan2(goal[0] - pos[0], goal[1] - pos[1])) % 360
            error = (desired - own['heading_deg'] + 180) % 360 - 180
            # Native MMG still owns turn/acceleration response. The director
            # itself issues a continuous bounded heading, without random turns.
            heading = (own['heading_deg'] + max(-4., min(4., error))) % 360
            speed = min(speed, max(0., distance - 5.) / 15.)
            if abs(error) > 35.:
                speed = min(speed, .35)
            payloads[entity] = dict(speed_mps=speed, heading_deg=heading, altitude_m=0.)
        batch=navigation_batch(obs, payloads) if payloads else None
        return self.safety.apply(obs,batch) if self.safety else batch


def attack_policy_class():
    # Delayed import avoids agent -> planner -> navigation import cycles.
    from .opponent import AttackPolicy

    class BuoyScenarioAttackPolicy(AttackPolicy):
        def __init__(self, plan, entity_tags, seed=7):
            super().__init__(plan, entity_tags, seed)
            self.buoys = BuoyNavigation(plan['third_party_navigation'],self.safety)
            self.stages = {}
            self.stage_history = []
            self.blue_navigation = None
            if 'public_engagement_policy' in plan:
                from copy import deepcopy
                from .trajectory_judgment import PublicObjectiveSelector
                # A separate navigation-only object has no neutral plans,
                # foreign IDs or target tags. The selector gets public data only.
                public_nav={k:deepcopy(plan[k]) for k in ('surface_navigation_map','surface_staging','surface_arrival','routes') if k in plan}
                public_nav['third_party_navigation']={}
                public_nav['attack']={k:deepcopy(plan['attack'][k]) for k in
                    ('surface_avoidance_m','surface_separation_m','weapon_policies','speed_mps','surface_traffic_max_altitude_m') if k in plan['attack']}
                public_nav['attack']['withdraw_after_strike']=True
                for route in public_nav.get('routes',[]):route.pop('facility_search',None)
                own_tags={e:{t for t in tags if not t.startswith('target.')} for e,tags in entity_tags.items()}
                public_nav['surface_safety']=deepcopy(plan.get('surface_safety',{}))
                self.blue_navigation=attack_policy_class()(public_nav,own_tags,seed)
                self.public_blue_navigation=deepcopy(public_nav)
                self.public_blue_engagement=deepcopy(plan['public_engagement_policy'])
                self.blue_navigations={};self.fire_selectors={};self.fire_feedback={}
            for config in plan.get('surface_staging', {}).values():
                for point in config.get('post_loiter_route', []):
                    if (len(point['offset_m']) != 2 or point['arrival_radius_m'] <= 0
                            or point['maximum_actual_speed_mps'] <= 0
                            or type(point['dwell_ticks']) is not int or point['dwell_ticks'] < 0):
                        raise ValueError('invalid post-loiter navigation gate')

        def _change_stage(self, entity, state, phase, tick):
            state.update(phase=phase, since_tick=tick)
            self.stage_history.append(dict(entity_id=entity, phase=phase, tick=tick))

        def _staged_navigation(self, own, phase, obs):
            """Reach the outer staging area before starting the slow loiter clock.

            Only native own-state and current contacts drive movement. The
            attack phase delegates firing back to the unchanged native policy.
            A late arrival cannot silently skip transit or the low-speed visit.
            """
            from .coastal_navigation import CoastalRouter
            entity, tick = own['entity_id'], obs['tick']
            state = self.stages.setdefault(entity, dict(phase='transit', since_tick=tick))
            if self.coastal_router is None:
                self.coastal_router = CoastalRouter(self.plan['surface_navigation_map']['land_regions'])
            pos = own['position_m']
            speed = math.hypot(*own['velocity_mps'][:2])
            goal = phase['transit_goal_m']
            if state['phase'] == 'transit' and math.dist(pos[:2], goal) <= phase['braking_radius_m']:
                self._change_stage(entity, state, 'settling', tick)
            if (state['phase'] == 'settling' and speed <= phase['loiter_max_actual_speed_mps']
                    and math.dist(pos[:2], goal) <= phase['arrival_radius_m']):
                bearing = math.radians(phase['loiter_heading_deg'])
                state['loiter_goal_m'] = [pos[0] + math.sin(bearing) * phase['loiter_distance_m'],
                                        pos[1] + math.cos(bearing) * phase['loiter_distance_m']]
                self._change_stage(entity, state, 'loiter', tick)
            if state['phase'] == 'loiter' and tick - state['since_tick'] >= phase['loiter_ticks']:
                if phase.get('post_loiter_route'):
                    state.update(route_origin_m=list(pos[:2]), route_index=0)
                    self._change_stage(entity, state, 'post_loiter', tick)
                else:
                    self._change_stage(entity, state, 'attack', tick)
            if state['phase'] == 'post_loiter':
                point = phase['post_loiter_route'][state['route_index']]
                goal = [a+b for a,b in zip(state['route_origin_m'], point['offset_m'])]
                if (math.dist(pos[:2], goal) <= point['arrival_radius_m']
                        and speed <= point['maximum_actual_speed_mps']):
                    since = state.setdefault('route_arrival_tick', tick)
                    if tick-since >= point['dwell_ticks']:
                        self.stage_history.append(dict(entity_id=entity, phase='waypoint_arrived',
                            waypoint_index=state['route_index'], tick=tick, goal_m=list(goal)))
                        state['route_index'] += 1
                        state.pop('route_arrival_tick', None)
                        if state['route_index'] == len(phase['post_loiter_route']):
                            self._change_stage(entity, state, 'attack', tick)
                        else:
                            point = phase['post_loiter_route'][state['route_index']]
                            goal = [a+b for a,b in zip(state['route_origin_m'], point['offset_m'])]
                    else:
                        return dict(speed_mps=0., heading_deg=own['heading_deg'], altitude_m=0.)
                else:
                    state.pop('route_arrival_tick', None)
            if state['phase'] == 'attack':
                return None
            if state['phase'] == 'loiter':
                goal = state['loiter_goal_m']
            peers = self._surface_peers(obs)
            traffic = [dict(entity_id='observed.' + c['contact_id'],
                position_m=c['estimated_position_m'], velocity_mps=(0., 0., 0.), force_yield=True)
                for c in obs['organic_contacts'] if c.get('confirmed') and c.get('age_ticks', 999) <= 2
                and self._contact_domain(c) == ('surface', None)]
            detour = self._surface_evasion(own, [*peers, *traffic], tick)
            actual_goal = detour or goal
            path, _ = self.coastal_router.route(pos, actual_goal)
            if not path or math.dist(pos[:2], actual_goal) <= 35.:
                return dict(speed_mps=0., heading_deg=own['heading_deg'], altitude_m=0.)
            command_speed = phase['transit_speed_mps'] if state['phase'] == 'transit' else phase['loiter_speed_mps']
            nav = self._navigation(entity, pos, path[0], dict(pattern='direct', speed_mps=command_speed), tick)
            error = (nav['heading_deg'] - own['heading_deg'] + 180.) % 360. - 180.
            nav['heading_deg'] = (own['heading_deg'] + max(-4., min(4., error))) % 360.
            nav['speed_mps'] = min(nav['speed_mps'], math.dist(pos[:2], path[0]) / 3.)
            if abs(error) > 25. or detour is not None:
                nav['speed_mps'] = min(nav['speed_mps'], 3.)
            return nav

        def autonomous_navigation(self,obs):
            from .coastal_navigation import CoastalRouter
            if self.coastal_router is None:self.coastal_router=CoastalRouter(self.plan['surface_navigation_map']['land_regions'])
            payloads={};held=set()
            peers=self._surface_peers(obs)
            traffic=[dict(entity_id='observed.'+c['contact_id'],position_m=c['estimated_position_m'],
                velocity_mps=(0.,0.,0.),force_yield=True) for c in obs['organic_contacts']+obs['shared_contacts']
                if c.get('confirmed') and obs['tick']-c['observed_tick']<=120
                and c['estimated_position_m'][2]<=self.plan['attack'].get('surface_traffic_max_altitude_m',math.inf)]
            for own in obs['own_entities']:
                entity=own['entity_id']
                if entity not in obs['controlled_entity_ids']:continue
                phase=self.plan.get('surface_staging',{}).get(entity)
                nav=self._staged_navigation(own,phase,obs) if phase else None
                if nav is not None:payloads[entity]=nav;held.add(entity);continue
                route=self._route_for(self.tags.get(entity,()),obs['tick'],entity)
                policies=self._weapon_policies(entity)
                payloads[entity]=self._surviving_navigation(own,self.tags.get(entity,()),route,
                    route.get('objective_m',own['position_m']),policies,peers,traffic,obs['tick'])
            return payloads,held

        def autonomous_fire(self,obs,held,navigation):
            # No hidden facility matching, assigned-target reserve or neutral
            # exclusion. Both facility and buoy contacts pass the same selector.
            from .trajectory_judgment import PublicObjectiveSelector
            slot=obs['controller_slot_id']
            if slot not in self.fire_selectors:self.fire_selectors[slot]=PublicObjectiveSelector(self.public_blue_engagement)
            selector=self.fire_selectors[slot]
            candidates=selector.candidates(obs);fires=[]
            for own in obs['own_entities']:
                entity=own['entity_id']
                if entity not in obs['controlled_entity_ids']:continue
                for p in navigation._weapon_policies(entity):
                    if (own['ammunition'].get(p['ammunition_ref'],0)<=0
                            or own['cooldown_ticks'].get(p['weapon_ref'],0)>0
                            or not own.get('capability_flags',{}).get('weapon')):continue
                    for c in candidates:
                        if entity in held and c['contact_id'] not in selector.self_defence_contacts:continue
                        if self.public_blue_engagement.get('weapon_domain_matching'):
                            domain=c.get('estimated_domain') or ('air' if c['estimated_position_m'][2]>50 else 'surface')
                            if domain not in p['target_domains']:continue
                        distance=math.dist(own['position_m'],c['estimated_position_m'])
                        if not p.get('min_range_m',0.)<=distance<=p['max_range_m']:continue
                        key=(entity,c['contact_id'])
                        if obs['tick']-self.last_attempt.get(key,-10000)<p.get('retry_interval_ticks',5):continue
                        fires.append((entity,dict(weapon_ref=p['weapon_ref'],contact_id=c['contact_id'])))
                        self.last_attempt[key]=obs['tick'];break
                    if (not self.public_blue_engagement.get('weapon_domain_matching')
                            or fires and fires[-1][0]==entity):break
            return fires

        def _feedback_for_slot(self,slot):
            from .action_feedback import FireFeedback
            if slot not in self.fire_feedback:
                self.fire_feedback[slot]=FireFeedback(self.public_blue_engagement.get('fire_feedback_policy'))
            return self.fire_feedback[slot]

        def observe_receipt(self,receipt,controller_slot_id=None):
            if self.blue_navigation is not None and controller_slot_id is not None:
                self._feedback_for_slot(controller_slot_id).observe_receipt(receipt)

        def act(self, obs):
            if obs['observer_faction_id'] == 'neutral':
                return self.buoys.act(obs)
            if self.blue_navigation is not None:
                slot=obs['controller_slot_id']
                if slot not in self.blue_navigations:
                    self.blue_navigations[slot]=(self.blue_navigation if not self.blue_navigations else
                        attack_policy_class()(self.public_blue_navigation,self.blue_navigation.tags,self.seed))
                navigation=self.blue_navigations[slot]
                payloads,held=navigation.autonomous_navigation(obs)
                # Preserve private, judge-only movement evidence from the
                # navigation object; it never enters contact selection.
                self.stages={e:s for n in self.blue_navigations.values() for e,s in n.stages.items()}
                self.stage_history=sorted([s for n in self.blue_navigations.values() for s in n.stage_history],key=lambda s:(s['tick'],s['entity_id']))
                batch=navigation_batch(obs,payloads,self.autonomous_fire(obs,held,navigation))
                return self.safety.apply(obs,self._feedback_for_slot(slot).apply(obs,batch))
            held = {}
            for own in obs['own_entities']:
                if own['entity_id'] not in obs['controlled_entity_ids']:
                    continue
                phase = self.plan.get('surface_staging', {}).get(own['entity_id'])
                if phase:
                    nav = self._staged_navigation(own, phase, obs)
                    if nav is not None:
                        held[own['entity_id']] = nav
            active = {**obs, 'controlled_entity_ids': [e for e in obs['controlled_entity_ids'] if e not in held]}
            batch = super().act(active)
            if held:
                staged_batch = navigation_batch(obs, held)
                if batch is None:
                    return self.safety.apply(obs,staged_batch)
                for key in ('persistent_commands', 'discrete_actions'):
                    batch[key].extend(staged_batch[key])
            return self.safety.apply(obs,batch)

    return BuoyScenarioAttackPolicy
