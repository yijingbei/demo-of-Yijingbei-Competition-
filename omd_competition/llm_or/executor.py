"""Finite assignment and short-horizon movement for IE game DTOs only.

Uses pure-Python assignment with independent objective verification. No rule-policy executor,
organizer state, learned checkpoint or real-device interface is used.
"""
from collections import Counter
import math
from types import SimpleNamespace
from collections import deque

from .tracks import PublicTracks
from .flight_levels import optimize_flight_levels
from .beliefs import FlightBeliefMemory
from .navigation import optimize_motion, angle_delta

from .optimization.model import AssignmentProblem, Edge
from .optimization.solvers import SizeAwareSolver, Status, validate_result
from .actions import navigation_batch
from omd_competition.coastal_navigation import CoastalRouter
from .goals import parse_plan
from .memory import ExecutionMemory
from .state import (contacts, fire_candidates, live_units,
                                        platform_air, speed_limit, weapon_options)


class OptimizationExecutor:
    def __init__(self, brief, budget=.03):
        self.brief = brief
        self.solver = SizeAwareSolver(time_limit=budget, node_limit=20000)
        self.router = CoastalRouter(brief.get('navigation_map', {}).get('land_regions', []))
        self.last_diagnostics = {}
        self.tracks = PublicTracks()
        self.memory = FlightBeliefMemory(brief, self.tracks)
        self.motion_samples, self.feedback = {}, {}

    def observe(self, obs, receipt=None):
        if self.tracks.last_tick == obs['tick']:
            return
        self.tracks.observe(obs)
        self.memory.observe(obs, receipt)
        for own in live_units(self.brief, obs):
            uid = own['entity_id']
            history = self.motion_samples.setdefault(uid, deque(maxlen=12))
            history.append((obs['tick'], tuple(own['position_m']), own['heading_deg']))
            last = self.last_diagnostics.get('navigation', {}).get(uid, {})
            stalled = (len(history) >= 10 and history[-1][0] - history[0][0] >= 8
                       and last.get('needs_motion', False)
                       and max(math.dist(h[1], history[0][1]) for h in history) < .5
                       and max(abs(angle_delta(h[2], history[0][2])) for h in history) < 1.)
            self.feedback[uid] = {'motion_stalled': bool(stalled),
                                  'no_static_route': last.get('no_static_route', False)}

    def context_memory(self):
        return {**self.memory.snapshot(), 'contact_associations': self.tracks.snapshot(),
                'navigation_feedback': dict(self.feedback)}

    def _assignment(self, obs, goals):
        units = live_units(self.brief, obs)
        own = {u['entity_id']: u for u in units}
        policies = {uid: {w['weapon_ref']: w for w in weapon_options(self.brief, uid)} for uid in own}
        committed = Counter(self.tracks.budget_key(s['contact_id']) for s in self.memory.shots.values())
        pending = {s['unit_id'] for s in self.memory.shots.values() if s['status'] == 'pending'}
        pending_ammo = Counter((s['unit_id'], s['ammunition_ref']) for s in self.memory.shots.values()
                               if s['status'] == 'pending')
        eligible, rejected = [], Counter()
        for row in fire_candidates(self.brief, obs):
            uid, cid, ref = row['unit_id'], row['contact_id'], row['weapon_ref']
            goal, weapon = goals[uid], policies[uid][ref]
            reason = row['reason']
            if reason is None and goal.engagement_mode == 'cease_fire':
                reason = 'llm_cease_fire'
            if reason is None and goal.engagement_mode == 'engage' and self.tracks.key(goal.target_id) != self.tracks.key(cid):
                reason = 'llm_assigned_other_contact'
            if reason is None and uid in pending:
                reason = 'awaiting_execution_receipt'
            remaining = own[uid].get('ammunition', {}).get(weapon['ammunition_ref'], 0)
            remaining -= pending_ammo[uid, weapon['ammunition_ref']]
            if reason is None and remaining <= goal.reserve_rounds.get(ref, 0):
                reason = 'llm_ammunition_reserve'
            if reason is None and committed[self.tracks.budget_key(cid)] >= goal.salvo_cap:
                reason = 'llm_inflight_cap'
            if reason:
                rejected[reason] += 1
            else:
                eligible.append(row)
        # Contact slots represent simultaneous game commands, not invented targets.
        slots, priorities, edges, payloads = [], {}, {}, {}
        coverage_scale = 5 * len(units) + 1
        for track in sorted({self.tracks.budget_key(r['contact_id']) for r in eligible}):
            rows = [r for r in eligible if self.tracks.budget_key(r['contact_id']) == track]
            cap = max(goals[r['unit_id']].salvo_cap for r in rows) - committed[track]
            cap = min(cap, len({r['unit_id'] for r in rows}))
            for index in range(cap):
                tid = f'slot-{len(slots)}'
                slots.append(SimpleNamespace(task_id=tid, mandatory=False))
                weight = max(1, min(5, math.ceil(max(goals[r['unit_id']].priority for r in rows))))
                # A new target's first allocation outranks extra allocations to
                # already-covered tracks, even if their flight time is shorter.
                priorities[tid] = weight * (coverage_scale if committed[track] == 0 and index == 0 else 1)
                for row in rows:
                    uid, ref = row['unit_id'], row['weapon_ref']
                    goal, weapon = goals[uid], policies[uid][ref]
                    cid = row['contact_id']
                    if committed[track] + index >= goal.salvo_cap:
                        continue
                    flight = row['distance_m'] / max(1., weapon['missile_cruise_speed_mps'])
                    cost = math.ceil(flight * 10) + (50 if goal.preferred_weapon_ref and ref != goal.preferred_weapon_ref else 0)
                    cost += index
                    key = uid, tid
                    if key not in edges or cost < edges[key].cost:
                        edges[key] = Edge(uid, tid, obs['tick'], 0, 0., 0, cost, 0)
                        payloads[key] = {'weapon_ref': ref, 'contact_id': cid}
        # Retain original per-unit permission for ordered team slots.
        # Bound the declared finite problem during saturated observations.
        keep = set(sorted(priorities, key=lambda t: (-priorities[t], t))[:16])
        slots = [s for s in slots if s.task_id in keep]
        priorities = {t:v for t,v in priorities.items() if t in keep}
        edges = {k:v for k,v in edges.items() if k[1] in keep}
        agents = [SimpleNamespace(agent_id=uid) for uid in sorted(own)]
        upper = sum(max([0, *[e.cost for e in edges.values() if e.agent_id == a.agent_id]]) for a in agents)
        problem = AssignmentProblem(SimpleNamespace(agents=agents, tasks=slots),
                                    SimpleNamespace(task_priorities=priorities), edges, [],
                                    {a.agent_id: 0 for a in agents}, upper + 1, 0,
                                    travel_model='ie-game-command-slots@1')
        result = self.solver.solve(problem)
        validate_result(problem, result)
        if result.status not in (Status.FEASIBLE, Status.OPTIMAL):
            raise RuntimeError('game action optimization did not find a feasible assignment')
        fires = [(uid, payloads[uid, tid]) for uid, tid in result.assignments.items()]
        # Recheck against the original public candidate table, not just solver edges.
        valid = {(r['unit_id'], r['weapon_ref'], r['contact_id']) for r in eligible}
        if any((uid, p['weapon_ref'], p['contact_id']) not in valid for uid, p in fires):
            raise RuntimeError('optimized game action failed public validation')
        return fires, {'assignment': result.as_dict(), 'eligible_pairs': len(eligible),
                       'rejected_pairs': dict(rejected), 'candidate_slots': len(slots),
                       'contact_associations': self.tracks.snapshot(),
                       'objective_version': 'bounded-coverage-original-cap-permissions@11'}

    def _movement(self, own, goal, visible):
        uid, pos = own['entity_id'], own['position_m']
        air = platform_air(self.brief, uid)
        target = goal.position_m or pos
        if goal.goal_type in {'intercept', 'track'}:
            center = visible[goal.target_id]['estimated_position_m']
            delta = [pos[i] - center[i] for i in (0, 1)]
            distance = math.hypot(*delta)
            radius = min(max(distance, max(0., goal.standoff_m - goal.radius_m)), goal.standoff_m + goal.radius_m)
            target = [center[i] + delta[i] * radius / max(1e-9, distance) for i in (0, 1)]
        elif math.dist(pos[:2], target[:2]) <= goal.radius_m:
            target = pos
        else:
            distance = math.dist(pos[:2], target[:2])
            target = [target[i] + (pos[i] - target[i]) * goal.radius_m / distance for i in (0, 1)]
        no_route = False
        if not air and math.dist(pos[:2], target[:2]) > 1.:
            route, _ = self.router.route(pos, target)
            no_route = not route
            target = route[0] if route else pos
        physical = speed_limit(self.brief, uid)
        maximum = physical * goal.speed_fraction
        heading, speed, diagnostics = optimize_motion(own, target, maximum, air, self.router, physical)
        altitude = (self.brief['defence'].get('aircraft_flight_levels_m', {}).get(uid, pos[2]) if air else pos[2])
        return {'heading_deg': heading, 'speed_mps': speed, 'altitude_m': altitude}, {**diagnostics, 'no_static_route': no_route}

    def propose(self, obs, plan):
        """Query against known history; proposing a command never reserves it."""
        self.observe(obs)
        plan = parse_plan(plan.as_dict(), self.brief, obs)
        goals = plan.by_unit()
        fires, diagnostics = self._assignment(obs, goals)
        visible = {c['contact_id']: c for c in contacts(obs)}
        payloads, navigation = {}, {}
        for own in live_units(self.brief, obs):
            uid = own['entity_id']
            if own.get('capability_flags', {}).get('dynamics', True):
                payloads[uid], navigation[uid] = self._movement(own, goals[uid], visible)
        levels, flight_diagnostics = optimize_flight_levels(self.brief, obs)
        for uid, height in levels.items():
            if uid in payloads:
                payloads[uid]['altitude_m'] = height
                navigation[uid]['flight_levels'] = flight_diagnostics
        batch = navigation_batch(obs, payloads, fires)
        self.last_diagnostics = {**diagnostics, 'requested_shots': len(fires), 'navigation': navigation}
        return batch

    def act(self, obs, plan):
        batch = self.propose(obs, plan)
        self.memory.record_submission(obs, batch)
        return batch
