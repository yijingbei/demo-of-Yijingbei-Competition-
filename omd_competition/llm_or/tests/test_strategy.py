"""Offline strategy checks: no paid model requests and no native episodes."""
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import random
from types import SimpleNamespace
import unittest
import urllib.error

from omd_competition.llm_or.config import StrategyConfig
from omd_competition.llm_or.controller import BudgetedSource, GameAgent
from omd_competition.llm_or.goals import parse_plan
from omd_competition.llm_or.optimization.model import AssignmentProblem, Edge
from omd_competition.llm_or.optimization.solvers import ExactSolver, MatchingSolver, SizeAwareSolver, validate_result
from omd_competition.llm_or.planning_budget import BudgetAwarePlanScheduler
from omd_competition.llm_or.resilience import normalized_plan, recovery_plan, ResilientPlanScheduler
from omd_competition.llm_or.state import live_units, public_brief, weapon_options
from omd_competition.llm_or.executor import OptimizationExecutor

ROOT = Path(__file__).resolve().parents[3]


def fixture(slug='ie-s2-lure'):
    brief = public_brief(json.loads((ROOT / 'scenarios/ie' / slug / 'public_brief.json').read_bytes()))
    obs = {'tick': 0, 'session_id': 'offline-test', 'controller_slot_id': 'controller.team',
           'observer_faction_id': 'red', 'own_entities': [], 'organic_contacts': [], 'shared_contacts': []}
    obs['controlled_entity_ids'] = [a['entity_id'] for a in brief['own_assets']]
    for asset in brief['own_assets']:
        uid = asset['entity_id']
        obs['own_entities'].append({'entity_id': uid, 'position_m': list(asset['initial_position_m']),
            'heading_deg': 0., 'velocity_mps': [0., 0., 0.], 'health': 1., 'lifecycle_state': 'active',
            'ammunition': dict(brief['own_initial_ammunition'].get(uid, {})), 'cooldown_ticks': {},
            'weapon_states': {}, 'capability_flags': {'weapon': True, 'dynamics': True}})
    goals = []
    for i, own in enumerate(live_units(brief, obs)):
        w = weapon_options(brief, own['entity_id'])[0]
        pos = list(own['position_m'])
        pos[0] += (w['minimum_range_m'] + w['maximum_range_m']) / 2
        obs['organic_contacts'].append({'contact_id': f'offline.contact.{i}',
            'estimated_position_m': pos, 'estimated_domain': w['target_domains'][0],
            'observer_entity_id': own['entity_id'], 'observed_tick': 0, 'age_ticks': 0,
            'detected': True, 'confirmed': True, 'track_quality': 1., 'confidence': 1.})
        goals.append({'unit_id': own['entity_id'], 'goal_type': 'hold', 'engagement_mode': 'allow',
                      'valid_until_tick': 100, 'salvo_cap': 1, 'reserve_rounds': {}})
    return brief, obs, {'schema_version': 'ie-goals@1', 'issued_tick': 0, 'goals': goals}


def response(plan):
    return {'id': 'offline-response', 'model': 'offline-model',
            'choices': [{'message': {'content': json.dumps(plan)}}],
            'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}


class PlanSource:
    def __init__(self, plan, seconds=0., fail=False):
        self.template, self.seconds, self.fail = plan, seconds, fail
        self.api_seconds = 0.
        self.usage = {'calls': 0}
        self.ticks = []

    def record_event(self, *args, **kwargs):
        pass

    def plan(self, brief, obs, memory, previous):
        self.usage['calls'] += 1
        self.api_seconds += self.seconds
        self.ticks.append(obs['tick'])
        if self.fail:
            raise TimeoutError('offline timeout')
        plan = deepcopy(self.template)
        plan['issued_tick'] = obs['tick']
        for g in plan['goals']:
            g['valid_until_tick'] = obs['tick'] + 100
        return parse_plan(plan, brief, obs)


class ContractTests(unittest.TestCase):
    def test_new_ammunition_and_energy_contract_survive_public_projection(self):
        suite = json.loads((ROOT / 'scenarios/suite.json').read_bytes())
        for scene in suite['scenarios']:
            raw = json.loads((ROOT / scene['package'] / 'public_brief.json').read_bytes())
            public = public_brief(raw)
            self.assertEqual(public['own_initial_ammunition'], raw['own_initial_ammunition'])
            self.assertEqual(public['energy_contract']['energy_ref'], raw['energy_contract']['energy_ref'])
            self.assertNotIn('attack_deployment_summary', public)

    def test_optional_null_is_defaulted_without_dropping_a_positive_reserve(self):
        brief, obs, data = fixture()
        uid = data['goals'][0]['unit_id']
        reserve = {weapon_options(brief, uid)[0]['weapon_ref']: 2}
        data['goals'][0].update(radius_m=None, standoff_m=None, salvo_cap=None, reserve_rounds=reserve)
        fixed, changes = normalized_plan(data, brief, obs)
        goal = parse_plan(fixed, brief, obs).goals[0]
        self.assertEqual((goal.radius_m, goal.standoff_m, goal.salvo_cap), (100., 1000., 1))
        self.assertEqual(goal.reserve_rounds, reserve)
        self.assertEqual(len(changes), 3)

    def test_stale_target_isolated_without_retargeting_other_units(self):
        brief, obs, data = fixture()
        data['goals'][0].update(goal_type='intercept', engagement_mode='engage', target_id='gone.contact')
        fixed, changes = normalized_plan(data, brief, obs)
        plan = parse_plan(fixed, brief, obs)
        self.assertEqual((plan.goals[0].goal_type, plan.goals[0].engagement_mode, plan.goals[0].target_id),
                         ('hold', 'cease_fire', None))
        self.assertEqual(fixed['goals'][1:], data['goals'][1:])
        self.assertTrue(changes)

    def test_negative_numbers_are_not_silently_clamped(self):
        brief, obs, data = fixture()
        data['goals'][0]['standoff_m'] = -5
        with self.assertRaisesRegex(ValueError, 'standoff_m'):
            normalized_plan(data, brief, obs)

    def test_ambiguous_positive_reserve_is_not_discarded(self):
        brief, obs, data = fixture()
        data['goals'][0]['reserve_rounds'] = 5
        with self.assertRaisesRegex(ValueError, 'reserve_rounds'):
            normalized_plan(data, brief, obs)

    def test_expired_fire_authority_is_not_extended(self):
        brief, obs, data = fixture()
        old = parse_plan(data, brief, obs)
        obs['tick'] = 100
        plan, _ = recovery_plan(old, brief, obs, 5)
        self.assertTrue(all(g.engagement_mode == 'cease_fire' for g in plan.goals))
        self.assertTrue(all(g.target_id is None for g in plan.goals))

    def test_one_lost_target_does_not_stop_all_other_legal_fire(self):
        brief, obs, data = fixture()
        data['goals'][0].update(goal_type='intercept', engagement_mode='engage', target_id='offline.contact.0')
        old = parse_plan(data, brief, obs)
        obs['organic_contacts'] = obs['organic_contacts'][1:]
        plan, _ = recovery_plan(old, brief, obs, 5)
        batch = OptimizationExecutor(brief).act(obs, plan)
        self.assertGreater(len(batch['discrete_actions']), 0)
        self.assertTrue(all(a['entity_id'] != plan.goals[0].unit_id for a in batch['discrete_actions']))

    def test_no_prior_plan_waits_without_authorizing_fire(self):
        brief, obs, _ = fixture()
        plan, _ = recovery_plan(None, brief, obs, 5)
        self.assertEqual(OptimizationExecutor(brief).act(obs, plan)['discrete_actions'], [])

    def test_all_cease_fire_and_reserve_constraints_are_obeyed(self):
        brief, obs, data = fixture()
        for goal in data['goals']:
            goal['engagement_mode'] = 'cease_fire'
        self.assertEqual(OptimizationExecutor(brief).act(obs, parse_plan(data, brief, obs))['discrete_actions'], [])
        for goal in data['goals']:
            goal['engagement_mode'] = 'allow'
            own = next(u for u in obs['own_entities'] if u['entity_id'] == goal['unit_id'])
            goal['reserve_rounds'] = {w['weapon_ref']: own['ammunition'].get(w['ammunition_ref'], 0)
                                      for w in weapon_options(brief, goal['unit_id'])}
        self.assertEqual(OptimizationExecutor(brief).act(obs, parse_plan(data, brief, obs))['discrete_actions'], [])


class ApiAndBudgetTests(unittest.TestCase):
    def test_model_output_normalized_in_one_call_with_actual_token_accounting(self):
        brief, obs, data = fixture()
        data['goals'][0].update(radius_m=None, salvo_cap=None)
        seen = []
        source = BudgetedSource(StrategyConfig(), post=lambda payload, timeout: (seen.append(payload) or response(data)))
        plan = source.plan(brief, obs, {}, None)
        self.assertTrue(plan.valid(brief, obs))
        self.assertEqual(source.usage, {'calls': 1, 'input_tokens': 7, 'output_tokens': 3, 'cost_usd': 0.})
        self.assertEqual(source.accepted_plans, 1)
        context = json.loads(seen[0]['messages'][1]['content'])
        self.assertIn('own_weapon_options', context['public_brief'])
        self.assertIn('energy_contract', context['public_brief'])

    def test_invalid_output_has_two_attempt_cap(self):
        brief, obs, data = fixture()
        data['goals'][0]['standoff_m'] = -5
        source = BudgetedSource(StrategyConfig(), post=lambda payload, timeout: response(data))
        with self.assertRaisesRegex(RuntimeError, 'standoff_m'):
            source.plan(brief, obs, {}, None)
        self.assertEqual(source.usage['calls'], 2)

    def test_auth_failure_is_visible(self):
        brief, obs, _ = fixture()
        def post(payload, timeout):
            raise urllib.error.HTTPError('https://offline.invalid', 403, 'denied', {}, None)
        source = BudgetedSource(StrategyConfig(), post=post)
        with self.assertRaises(PermissionError):
            ResilientPlanScheduler(source, 100).get(brief, obs, {})
        self.assertEqual(source.usage['calls'], 1)

    def test_timeout_recovers_and_cooldown_prevents_a_retry_every_tick(self):
        brief, obs, data = fixture()
        source = PlanSource(data, fail=True)
        scheduler = ResilientPlanScheduler(source, 100)
        plan = scheduler.get(brief, obs, {})
        self.assertTrue(all(g.engagement_mode == 'cease_fire' for g in plan.goals))
        obs['tick'] = 1
        scheduler.get(brief, obs, {})
        self.assertEqual(source.usage['calls'], 1)
        source.fail = False
        obs['tick'] = 5
        scheduler.get(brief, obs, {})
        self.assertFalse(scheduler.recovery_active)

    def test_contact_churn_spends_budget_across_the_whole_episode(self):
        for seconds in (12., 25.):
            brief, obs, data = fixture()
            source = PlanSource(data, seconds=seconds)
            scheduler = BudgetAwarePlanScheduler(source, 100)
            for tick in range(1801):
                obs['tick'] = tick
                obs['organic_contacts'][0]['contact_id'] = f'offline.changing.{tick}'
                self.assertTrue(scheduler.get(brief, obs, {}).valid(brief, obs))
            self.assertLessEqual(source.api_seconds, 1200.)
            self.assertTrue(all(b-a >= 20 for a,b in zip(source.ticks, source.ticks[1:])))

    def test_time_reserve_does_not_start_another_expensive_call(self):
        brief, obs, data = fixture()
        source = PlanSource(data)
        source.api_seconds = 1180.
        scheduler = BudgetAwarePlanScheduler(source, 100)
        self.assertTrue(scheduler.get(brief, obs, {}).valid(brief, obs))
        self.assertEqual(source.usage['calls'], 0)


class ConfigurationAndSolverTests(unittest.TestCase):
    def test_config_rejects_credentials_and_invalid_budgets(self):
        for bad in [{'api_key': 'never-store-credentials'}, {'base_url': 'https://name:secret@example.invalid/v1'},
                    {'timeout_seconds': 30}, {'api_time_budget_seconds': 1800},
                    {'min_plan_interval': 101}, {'input_price_per_million_usd': 1}]:
            with self.assertRaises(ValueError):
                StrategyConfig.parse(bad)
        self.assertFalse(StrategyConfig.parse({}).prices_known)

    def problem(self, n, m, seed):
        rng = random.Random(seed)
        agents = [SimpleNamespace(agent_id=f'a{i}') for i in range(n)]
        tasks = [SimpleNamespace(task_id=f't{i}', mandatory=False) for i in range(m)]
        edges = {(a.agent_id,t.task_id): Edge(a.agent_id,t.task_id,0,0,0.,0,rng.randrange(20),0)
                 for a in agents for t in tasks if rng.random() > .25}
        return AssignmentProblem(SimpleNamespace(agents=agents,tasks=tasks),
            SimpleNamespace(task_priorities={t.task_id: 1 for t in tasks}), edges, [],
            {a.agent_id:0 for a in agents}, 100, 0)

    def test_matching_agrees_with_independent_small_exact_search(self):
        for seed in range(20):
            problem = self.problem(4, 5, seed)
            exact = ExactSolver(time_limit=2).solve(problem)
            matched = MatchingSolver().solve(problem)
            self.assertEqual(exact.objective, matched.objective)
            validate_result(problem, matched)

    def test_ten_unit_fleet_uses_polynomial_matching(self):
        problem = self.problem(10, 12, 3)
        result = SizeAwareSolver().solve(problem)
        self.assertEqual(result.backend, 'min-cost-matching')
        self.assertEqual(len(set(result.assignments.values())), len(result.assignments))
        validate_result(problem, result)


if __name__ == '__main__':
    unittest.main()
