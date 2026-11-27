"""Spend the existing native decision allowance across the published episode.

This strategy-side admission guard does not change any native time limit.
The executor still reacts every tick; only expensive LLM refreshes are paced.
"""
import math

from .resilience import ResilientPlanScheduler, recovery_plan


class BudgetAwarePlanScheduler(ResilientPlanScheduler):
    def __init__(self, source, interval=20, api_budget_seconds=1200.):
        super().__init__(source, interval)
        self.api_budget_seconds = api_budget_seconds
        self.next_full_plan_tick = 0
        self.planning_events = 0
        self.budget_exhausted_recorded = False

    def get(self, brief, obs, memory):
        tick = obs['tick']
        spent = getattr(self.source, 'api_seconds', 0.)
        exhausted = spent + 25. > self.api_budget_seconds
        if exhausted or tick < self.next_full_plan_tick:
            # Revalidate every tick. Expired instructions cannot authorize fire;
            # a vanished target affects only that unit. Other valid LLM goals
            # continue through the unchanged mathematical executor.
            horizon = 10 if exhausted else max(1, self.next_full_plan_tick - tick)
            self.current, changes = recovery_plan(self.last_llm_plan, brief, obs, horizon)
            self.recovery_active = True
            self.reason = 'native_time_budget_guard' if exhausted else 'paced_llm_refresh'
            if exhausted and not self.budget_exhausted_recorded:
                self.source.record_event('llm_time_reserve_reached', brief, obs,
                    api_seconds=spent, api_allowance_seconds=self.api_budget_seconds,
                    native_episode_limit_unchanged=True)
                self.budget_exhausted_recorded = True
            return self.current
        before = self.source.usage['calls']
        plan = super().get(brief, obs, memory)
        if self.source.usage['calls'] > before:
            self.planning_events += 1
            spent = getattr(self.source, 'api_seconds', 0.)
            horizon = max(tick + 1, brief.get('duration_ticks', tick + self.interval),
                          brief.get('adjudication_tick', tick + self.interval))
            estimate = max(15., 1.25 * spent / self.planning_events)
            remaining_events = max(1, int(max(0., self.api_budget_seconds-spent-25.) / estimate))
            spacing = max(getattr(self, 'minimum_spacing', 20), math.ceil((horizon-tick) / remaining_events))
            self.next_full_plan_tick = tick + spacing
        return plan
