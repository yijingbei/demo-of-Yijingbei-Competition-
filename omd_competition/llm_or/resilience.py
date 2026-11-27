"""Audited LLM contract recovery; tactical actions remain with the OR executor.

Only participant-visible DTOs are used. Recovery never selects a replacement
target, enlarges a firing permission, drops a reserve, or renews expired fire
authority. A unit whose instruction cannot be used waits without firing while
the other units keep their valid LLM instructions.
"""
from copy import deepcopy
import json
import math
import time
import urllib.error

from .goals import parse_plan
from .planner import (
    APIPlanSource, PlanScheduler, SYSTEM_PROMPT, canonical_api_plan, planning_context,
)
from .state import contacts, contact_domain, live_units, weapon_options
from omd_competition.transport import strict_loads


RECOVERY_PROMPT = """
Optional numeric fields MUST be omitted or finite numbers, never null:
radius_m and standoff_m >= 0, speed_fraction in [0,1], priority >= 0.
salvo_cap MUST be omitted or an integer 1..16, never null.
Only target_id, position_m and preferred_weapon_ref may explicitly be null.
The current compatible_target_ids_by_unit lists are authoritative for targets.
IDs in execution_memory and previous_plan are history, not current authority.
When a former target has disappeared, choose a fresh task using current IDs.
A temporary no-fire recovery instruction in previous_plan is not a strategic
request to stop defending: reconsider those units using the current observation.
"""


def normalized_plan(data, brief, obs):
    """Fix explicit optional nulls and isolate unavailable target instructions.

    A missing target is never mapped to another ID. Its one affected unit gets
    a short no-fire hold, retaining the original reserve and salvo constraints.
    All other numeric/permission errors still require a corrected LLM response.
    """
    value = deepcopy(data)
    changes = []
    if type(value) is not dict or type(value.get('goals')) is not list:
        return canonical_api_plan(value, brief, obs)
    own = {u['entity_id']: u for u in live_units(brief, obs)}
    visible = {c['contact_id']: c for c in contacts(obs)}
    for index, row in enumerate(value['goals']):
        if (type(row) is not dict or type(row.get('unit_id')) is not str
                or row['unit_id'] not in own):
            continue
        uid = row['unit_id']
        path = f'goals[{index}] unit={uid!r}'
        for key in ('radius_m', 'standoff_m', 'speed_fraction', 'priority', 'salvo_cap'):
            if key in row and row[key] is None:
                del row[key]
                changes.append({'path': path + '.' + key, 'before': None,
                                'after': 'omitted; declared schema default'})
        for key in ('radius_m', 'standoff_m', 'speed_fraction', 'priority'):
            if key in row and (type(row[key]) not in (int, float)
                              or not math.isfinite(row[key]) or row[key] < 0
                              or (key == 'speed_fraction' and row[key] > 1)):
                raise ValueError(f'{path}.{key}: expected a finite nonnegative number'
                                 + (' <= 1' if key == 'speed_fraction' else '')
                                 + f'; got {row[key]!r}')
        target = row.get('target_id')
        if target is None:
            continue  # A missing required target is not silently invented.
        if type(target) is not str:
            raise ValueError(f'{path}.target_id: expected a current contact ID or null')
        valid_target = target in visible and any(
            contact_domain(visible[target]) in w['target_domains']
            for w in weapon_options(brief, uid))
        if not valid_target:
            before = {k: row.get(k) for k in ('goal_type', 'engagement_mode', 'target_id')}
            row.update(goal_type='hold', engagement_mode='cease_fire', target_id=None,
                       position_m=list(own[uid]['position_m']))
            expiry = row.get('valid_until_tick')
            if type(expiry) is int and expiry > obs['tick']:
                row['valid_until_tick'] = min(expiry, obs['tick'] + 10)
            changes.append({'path': path, 'kind': 'unavailable_target_no_fire_hold',
                            'before': before,
                            'after': {k: row[k] for k in before}})
    value, reserve_changes = canonical_api_plan(value, brief, obs)
    return value, changes + reserve_changes


def recovery_plan(previous, brief, obs, horizon):
    """Retain only unexpired LLM authority, otherwise make a no-fire wait.

    This is a protocol recovery contract, not a target-selection policy. It has
    no authority to enable a new shot, retarget a unit, or extend an expired
    engagement. The mathematical executor still produces every game action.
    """
    prior = previous.by_unit() if previous else {}
    visible = {c['contact_id']: c for c in contacts(obs)}
    goals, changed = [], []
    for own in live_units(brief, obs):
        uid = own['entity_id']
        old = prior.get(uid)
        if old is None:
            row = {'unit_id': uid, 'goal_type': 'hold', 'engagement_mode': 'cease_fire',
                   'valid_until_tick': obs['tick'] + horizon}
            changed.append({'unit_id': uid, 'reason': 'no_prior_llm_instruction'})
        else:
            row = old.as_dict()
            target = row.get('target_id')
            target_valid = target is None or (target in visible and any(
                contact_domain(visible[target]) in w['target_domains']
                for w in weapon_options(brief, uid)))
            if old.valid_until_tick <= obs['tick'] or not target_valid:
                row.update(goal_type='hold', engagement_mode='cease_fire', target_id=None,
                           position_m=list(own['position_m']),
                           valid_until_tick=obs['tick'] + horizon)
                changed.append({'unit_id': uid, 'reason': 'expired_instruction' if
                                old.valid_until_tick <= obs['tick'] else 'target_unavailable'})
            else:
                row['valid_until_tick'] = min(old.valid_until_tick, obs['tick'] + horizon)
        goals.append(row)
    result = parse_plan({'schema_version': 'ie-goals@1', 'issued_tick': obs['tick'],
                         'goals': goals}, brief, obs)
    return result, changed


class ResilientAPIPlanSource(APIPlanSource):
    """At most two requests in the existing 25-second planning budget."""

    def plan(self, brief, obs, memory, previous):
        context = planning_context(brief, obs, memory, previous)
        # Legacy S tasks publish own_weapon_policies instead of own_weapon_options.
        # Derive a common view only from the participant's own published catalog.
        context['public_brief']['own_weapon_options'] = {
            u['entity_id']: weapon_options(brief, u['entity_id']) for u in live_units(brief, obs)}
        if previous:
            sanitized, changes = recovery_plan(previous, brief, obs, 10)
            context['previous_plan'] = sanitized.as_dict()
            context['temporary_recovery_units'] = changes
        self.last_repaired_units = []
        payload = {'model': self.model, 'temperature': 0, 'max_tokens': getattr(self, 'max_tokens', 4096),
                   'response_format': {'type': 'json_object'},
                   'messages': [{'role': 'system', 'content': SYSTEM_PROMPT + RECOVERY_PROMPT},
                                {'role': 'user', 'content': json.dumps(context, allow_nan=False,
                                                                      separators=(',', ':'))}]}
        import os
        if self.model.casefold().startswith('deepseek-'):
            payload['thinking'] = {'type': 'disabled'}
        deadline = time.monotonic() + self.timeout
        last_error = 'LLM did not return a usable plan'
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= .1 or (attempt and remaining < 5):
                break
            self.usage['calls'] += 1
            try:
                # BudgetedSource adds its tactical hints to the HTTP payload;
                # do not accumulate the same hints across correction attempts.
                response = self._post(deepcopy(payload), remaining)
            except urllib.error.HTTPError as error:
                self.record_event('request_error', brief, obs, attempt=attempt + 1,
                                  http_status=error.code, error_type='HTTPError')
                if error.code in (401, 403):
                    raise PermissionError('LLM authentication/authorization failed') from None
                if error.code not in (408, 429, 500, 502, 503, 504):
                    raise RuntimeError('LLM HTTP status ' + str(error.code)) from None
                last_error = 'LLM temporary HTTP status ' + str(error.code)
                continue
            except (urllib.error.URLError, TimeoutError) as error:
                self.record_event('request_error', brief, obs, attempt=attempt + 1,
                                  error_type=type(error).__name__)
                last_error = 'LLM transport timeout/unavailable'
                continue
            usage = response.get('usage', {}) if type(response) is dict else {}
            for provider, local in [('prompt_tokens', 'input_tokens'), ('completion_tokens', 'output_tokens')]:
                count = usage.get(provider)
                if type(count) is not int or count < 0:
                    raise ValueError('provider must return actual token usage')
                self.usage[local] += count
            self.usage['cost_usd'] = (self.usage['input_tokens'] * self.input_price +
                                      self.usage['output_tokens'] * self.output_price) / 1e6
            try:
                content = response['choices'][0]['message']['content']
            except (KeyError, IndexError, TypeError):
                raise ValueError('provider response needs choices[0].message.content') from None
            try:
                if type(content) is not str:
                    raise ValueError('provider message.content must be a JSON string')
                data, changes = normalized_plan(strict_loads(content), brief, obs)
                plan = parse_plan(data, brief, obs)
            except ValueError as error:
                last_error = str(error)
                self.record_event('validation_error', brief, obs, attempt=attempt + 1,
                                  error=last_error, response_content=content,
                                  request=payload, provider_usage=usage)
                payload['messages'].extend([
                    {'role': 'assistant', 'content': content},
                    {'role': 'user', 'content': 'Correct this specific field error: ' + last_error
                     + '. Omit optional numeric fields instead of null. Use only CURRENT '
                     'compatible_target_ids_by_unit from the original request. Preserve '
                     'other task intent and positive ammunition reserves. Return the '
                     'complete corrected JSON plan.'}])
                continue
            if changes:
                self.record_event('contract_normalized', brief, obs, attempt=attempt + 1,
                                  changes=changes, response_content=content, provider_usage=usage)
            self.last_repaired_units = [c['path'] for c in changes
                                       if c.get('kind') == 'unavailable_target_no_fire_hold']
            self.accepted_plans = getattr(self, 'accepted_plans', 0) + 1
            self.record_event('plan_accepted', brief, obs, attempt=attempt + 1,
                              provider_usage=usage, reported_model=response.get('model'),
                              response_id=response.get('id'), normalized=bool(changes))
            return plan
        raise RuntimeError(last_error)


class ResilientPlanScheduler(PlanScheduler):
    """Recover transient planning failures without terminating an episode."""

    def __init__(self, source, interval=20):
        super().__init__(source, interval)
        self.recovery_active = False
        self.next_retry_tick = 0
        self.failure_streak = 0
        self.last_llm_plan = None

    def get(self, brief, obs, memory):
        tick = obs['tick']
        signature = (tuple(sorted(c['contact_id'] for c in contacts(obs))),
                     tuple((u['entity_id'], tuple((w['weapon_ref'],
                           u.get('ammunition', {}).get(w['ammunition_ref'], 0) > 0)
                          for w in weapon_options(brief, u['entity_id'])))
                           for u in live_units(brief, obs)))
        valid = self.current is not None and self.current.valid(brief, obs)
        if self.recovery_active and tick < self.next_retry_tick:
            if not valid:
                self.current, _ = recovery_plan(self.last_llm_plan, brief, obs,
                                                max(1, self.next_retry_tick - tick))
            self.reason = 'recovery_waiting_to_replan'
            return self.current
        due = tick - self.last_plan_tick >= self.interval
        changed = signature != self.last_signature
        if valid and not due and not changed and not self.recovery_active:
            self.reason = 'unchanged'
            return self.current
        self.reason = 'missing_or_invalid_plan' if not valid else 'public_state_changed' if changed else 'periodic'
        try:
            plan = self.source.plan(brief, obs, memory, self.last_llm_plan)
            plan = parse_plan(plan.as_dict(), brief, obs)
        except (RuntimeError, TimeoutError, ValueError) as error:
            self.failure_streak += 1
            delay = min(20, 5 * (2 ** min(self.failure_streak - 1, 2)))
            plan, changes = recovery_plan(self.last_llm_plan, brief, obs, delay)
            self.recovery_active = True
            self.next_retry_tick = tick + delay
            self.reason = 'transient_planning_failure_recovered'
            if isinstance(self.source, APIPlanSource):
                self.source.record_event('planning_degraded', brief, obs,
                    error_type=type(error).__name__, error=str(error),
                    failure_streak=self.failure_streak, next_retry_tick=self.next_retry_tick,
                    changes=changes, recovered_plan=plan.as_dict())
        else:
            self.failure_streak = 0
            self.last_llm_plan = plan
            self.recovery_active = bool(getattr(self.source, 'last_repaired_units', []))
            self.next_retry_tick = tick + 10 if self.recovery_active else tick
            if self.recovery_active:
                self.reason = 'llm_plan_with_isolated_unavailable_targets'
        self.current = plan
        self.last_plan_tick = tick
        self.last_signature = signature
        return plan
