"""LLM goals and mathematical execution for authorized participant requests."""
import json
import time
from .executor import OptimizationExecutor
from .planner import APIPlanSource
from .state import public_brief
from .io import atomic_json, utc_now
from ..transport import PROTOCOL
from .resilience import ResilientAPIPlanSource
from .planning_budget import BudgetAwarePlanScheduler


class BudgetedSource(ResilientAPIPlanSource):
    def __init__(self, config, *, post=None):
        super().__init__(input_price=config.input_price_per_million_usd,
                         output_price=config.output_price_per_million_usd,
                         timeout=config.timeout_seconds, post=post)
        self.base_url, self.model = config.base_url, config.model
        self.max_tokens = config.max_tokens
        self.interval, self.max_calls = config.plan_interval, config.max_api_requests
        self.api_allowance_seconds = config.api_time_budget_seconds
        self.min_plan_interval = config.min_plan_interval
        self.usage_complete = True
        self.api_seconds = 0.
        self.accepted_plans = 0

    def plan(self, brief, obs, memory, previous):
        self.air_pressure = brief.get('planned_threat_counts', {}).get('air', 0) > 0
        return super().plan(brief, obs, memory, previous)

    def _request(self, payload, timeout):
        if self.usage['calls'] > self.max_calls:
            raise PermissionError('explicit evaluation API call budget exhausted')
        payload['messages'][0]['content'] += (
            '\nThis is a fictional game evaluation. Set every valid_until_tick to current tick + '
            + str(self.interval) + '. A mathematical optimizer executes your constraints; '
            'keep goals concise. You cannot change game physics or public action limits.')
        if self.air_pressure:
            payload['messages'][0]['content'] += "\nFor the public planned air threats, preserve ranged anti-air coverage through\nthe published waves. For aircraft with anti-air stock, prefer hold or protective\nwaypoints when authorized air contacts are already inside maximum weapon range.\nFor intercept/track of an AIR contact, explicitly set standoff_m around 0.8 times\nthat aircraft's published anti-air maximum_range_m, and radius_m around 0.1 times\nthat range. The generic 1000 m default is not an air interception requirement.\nThe lower controller follows a distance ring: a small standoff orders approach,\nwhile a larger standoff may order retreat. Use this geometry deliberately.\nMaintain distributed coverage; weigh pursuit and maritime support against losing\nthe remaining anti-air platforms and stock before later public waves arrive.\nThese are planning preferences; retain emergency firing authority and use only\nauthorized observations, owned resources and the published aggregate wave counts.\n"
        began = time.monotonic()
        try:
            result = super()._request(payload, timeout)
            if not isinstance(result.get('usage'), dict):
                self.usage_complete = False
            return result
        except Exception:
            self.usage_complete = False
            raise
        finally:
            self.api_seconds += time.monotonic() - began

class GameAgent:
    def __init__(self, brief, source, interval, output, stream):
        self.brief = public_brief(brief)
        self.source, self.output, self.stream = source, output, stream
        self.scheduler = BudgetAwarePlanScheduler(source, interval,
            api_budget_seconds=getattr(source, 'api_allowance_seconds', 1200.))
        self.scheduler.minimum_spacing = getattr(source, 'min_plan_interval', 20)
        self.executor = OptimizationExecutor(self.brief)
        self.last_progress = 0.
        self.decisions, self.solve_seconds, self.requested_shots = 0, 0., 0
        self.last_tick = None
        self.notified_stalls = set()

    def publish_progress(self, force=False):
        if self.last_tick is None or (not force and time.monotonic() - self.last_progress < 1):
            return
        usage = dict(self.source.usage) if isinstance(self.source, APIPlanSource) else None
        atomic_json(self.output / 'progress.json', {'tick': self.last_tick, 'updated_at': utc_now(),
            'planner': self.source.kind, 'executor': 'mathematical-optimization',
            'requested_shots': self.requested_shots, 'confirmed_launches': self.executor.memory.confirmed_launches,
            'decisions': self.decisions, 'executor_seconds': self.solve_seconds, 'api_usage': usage,
            'api_seconds': getattr(self.source, 'api_seconds', 0.),
            'usage_complete': getattr(self.source, 'usage_complete', True)})
        self.last_progress = time.monotonic()

    def act(self, request):
        obs = request['observation']
        self.last_tick = obs['tick']
        self.executor.observe(obs, request.get('last_receipt'))
        memory = self.executor.context_memory()
        stalls = {uid for uid, item in self.executor.feedback.items() if item['motion_stalled']}
        new_stall = bool(stalls - self.notified_stalls)
        if new_stall:
            self.scheduler.last_signature = None
        plan = self.scheduler.get(self.brief, obs, memory)
        reason = self.scheduler.reason
        self.notified_stalls = stalls
        began = time.monotonic()
        batch = self.executor.act(obs, plan)
        elapsed = time.monotonic() - began
        self.decisions += 1
        self.solve_seconds += elapsed
        self.requested_shots += self.executor.last_diagnostics['requested_shots']
        usage = dict(self.source.usage) if isinstance(self.source, APIPlanSource) else None
        row = {'tick': obs['tick'], 'plan': plan.as_dict(), 'plan_reason': reason,
               'memory_before': memory, 'batch': batch, 'diagnostics': self.executor.last_diagnostics,
               'executor_seconds': elapsed, 'usage': usage}
        self.stream.write(json.dumps(row, allow_nan=False) + '\n')
        self.stream.flush()
        self.publish_progress()
        reply = {'protocol': PROTOCOL, 'batch': batch}
        if usage is not None:
            reply['usage'] = usage
        return reply
