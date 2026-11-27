"""Diagnostic and API task sources with the same strict output contract."""

from copy import deepcopy

from datetime import datetime, timezone

import json

import math

import os

from pathlib import Path

import re

import time

import urllib.error

import urllib.request

from . import GOAL_SCHEMA

from .goals import parse_plan

from .state import contact_domain, contact_rejection, contacts, live_units, public_brief, weapon_options

from ..transport import strict_loads

SYSTEM_PROMPT = """You allocate the defender's controlled units in an IE simulation.
Use only the provided public brief, authorized contacts and owned execution history.
Return one JSON object with exactly schema_version, issued_tick, goals.
schema_version is ie-goals@1; issued_tick is the current observation tick.
Provide exactly one goal for every live controlled combat unit. Each goal needs
unit_id, goal_type, engagement_mode, valid_until_tick (exclusive absolute tick).
goal_type: intercept, track, hold, reserve, waypoint, return, disengage.
engagement_mode: cease_fire, allow, engage. engage requires a current target_id;
intercept/track also require target_id. hold/reserve default to current position;
return defaults to the public objective. waypoint/disengage need position_m.
Optional fields: target_id, position_m, preferred_weapon_ref, radius_m,
standoff_m, speed_fraction in [0,1], priority >= 0, salvo_cap integer 1..16,
reserve_rounds (map owned weapon_ref to nonnegative retained rounds).
reserve_rounds MUST be an object, e.g. {}, or {"exact-owned-weapon-ref": 2}.
Use keys from allowed_reserve_weapon_refs_by_unit[unit_id], never ammunition
names, short labels, or another unit's weapons. Values are JSON integers,
not strings, floats, booleans, lists, or null. Omit it or use {} for no reserve.
Navigation type alone never prohibits fire. Use cease_fire to prohibit fire.
The executor selects a valid weapon and current native contact handle; queueing
does not prove launch or hit. Track disappearance does not prove neutralization.
Do not invent enemy identities, hidden health, future routes or weapon options.
For each unit, target_id MUST come from compatible_target_ids_by_unit[unit_id].
When that list is empty, choose a target-free hold/reserve/return/waypoint goal
and omit target_id or set it to null. Never assign an air contact to a unit
whose public weapons only support surface/land targets, even for optional fields.
"""

def planning_context(brief, obs, memory, previous):
    """Keep tactical DTO fields; omit bulky subsystem diagnostics and duplicate catalogs."""
    projected = public_brief(brief)
    fields = ('scenario_id', 'track', 'objective_m', 'protected_facilities',
              'own_weapon_options', 'own_initial_ammunition', 'planned_waves',
              'energy_contract', 'contact_judgment_contract',
              'planned_threat_counts', 'adjudication_tick', 'duration_ticks',
              'task_success_criteria', 'navigation_map')
    task = {key: projected[key] for key in fields if key in projected}
    own_fields = ('entity_id', 'position_m', 'heading_deg', 'velocity_mps', 'health',
                  'lifecycle_state', 'ammunition', 'cooldown_ticks', 'weapon_states', 'capability_flags')
    contact_fields = ('contact_id', 'estimated_position_m', 'estimated_domain', 'domain',
                      'detected', 'confirmed', 'age_ticks', 'observed_tick', 'delivered_tick',
                      'expiry_tick', 'track_quality', 'confidence', 'observer_entity_id')
    observation = {key: obs[key] for key in ('tick', 'controlled_entity_ids')}
    observation['combat_unit_ids'] = [u['entity_id'] for u in live_units(brief, obs)]
    observation['own_entities'] = [{key: u[key] for key in own_fields if key in u} for u in obs['own_entities']]
    for name in ('organic_contacts', 'shared_contacts'):
        observation[name] = [{key: c[key] for key in contact_fields if key in c} for c in obs.get(name, [])]
    context = {'public_brief': task, 'observation': observation, 'execution_memory': memory,
               'previous_plan': previous.as_dict() if previous else None}
    context['compatible_target_ids_by_unit'] = {
        unit['entity_id']: [c['contact_id'] for c in contacts(obs)
                           if any(contact_domain(c) in w['target_domains']
                                  for w in weapon_options(brief, unit['entity_id']))]
        for unit in live_units(brief, obs)}
    context['allowed_reserve_weapon_refs_by_unit'] = {
        unit['entity_id']: [w['weapon_ref'] for w in weapon_options(brief, unit['entity_id'])]
        for unit in live_units(brief, obs)}
    def compact(value):
        if isinstance(value, dict):
            return {k: compact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [compact(v) for v in value]
        return round(value, 3) if type(value) is float else value
    return compact(context)

def canonical_api_plan(data, brief, obs):
    """Lossless wire-format repairs only; never discard an unknown constraint.

    The persisted goal schema and training parser stay strict and unchanged.
    Every conversion is returned for audit. A positive scalar is ambiguous for
    multi-weapon units and must be corrected by the LLM, not guessed here.
    """
    result, changes = deepcopy(data), []
    if type(result) is not dict or type(result.get('goals')) is not list:
        return result, changes
    live = {u['entity_id'] for u in live_units(brief, obs)}
    for index, row in enumerate(result['goals']):
        if (type(row) is not dict or type(row.get('unit_id')) is not str
                or row['unit_id'] not in live or 'reserve_rounds' not in row):
            continue
        uid, original = row['unit_id'], deepcopy(row['reserve_rounds'])
        location = f'goals[{index}] unit={uid!r}.reserve_rounds'
        options = weapon_options(brief, uid)
        refs = {w['weapon_ref'] for w in options}
        value = row['reserve_rounds']
        if value is None or (type(value) in (int, float) and value == 0) or value == '0':
            value = {}
        if type(value) is not dict:
            raise ValueError(f'{location}: expected object mapping owned weapon_ref to nonnegative integer; '
                             f'got {type(value).__name__} {value!r}; allowed keys={sorted(refs)!r}')
        normalized = {}
        for key, count in value.items():
            canonical = key
            if canonical not in refs:
                aliases = [w['weapon_ref'] for w in options if w['ammunition_ref'] == key]
                if len(aliases) == 1:
                    canonical = aliases[0]
                else:
                    raise ValueError(f'{location}[{key!r}]: unknown or ambiguous weapon key; '
                                     f'allowed keys={sorted(refs)!r}')
            if type(count) is float and math.isfinite(count) and count.is_integer():
                count = int(count)
            elif type(count) is str and re.fullmatch(r'[0-9]+', count.strip()):
                count = int(count.strip())
            if type(count) is not int or count < 0:
                raise ValueError(f'{location}[{key!r}]: expected nonnegative integer; '
                                 f'got {type(count).__name__} {count!r}')
            if canonical in normalized and normalized[canonical] != count:
                raise ValueError(f'{location}: conflicting counts for {canonical!r}')
            normalized[canonical] = count
        row['reserve_rounds'] = normalized
        # JSON distinguishes an integral float from an integer, unlike Python equality.
        if json.dumps(original, sort_keys=True) != json.dumps(normalized, sort_keys=True):
            changes.append({'path': location, 'before': original, 'after': normalized})
    return result, changes

class APIPlanSource:
    kind = 'api-llm'

    def __init__(self, input_price=None, output_price=None, timeout=25.0, post=None, diagnostic_path=None):
        self.pricing_known = input_price is not None and output_price is not None
        if (input_price is None) != (output_price is None):
            raise ValueError('provide both token rates or leave both unpriced')
        for price in (input_price, output_price) if self.pricing_known else (0.0, 0.0):
            if type(price) not in (int, float) or not math.isfinite(price) or price < 0:
                raise ValueError('declare nonnegative input/output USD per million token rates')
        if not 0 < timeout < 30:
            raise ValueError('LLM timeout must leave margin within the 30-second act budget')
        self.key = os.environ.get('OPENMD_LLM_API_KEY')
        if not self.key and post is None:
            raise ValueError('OPENMD_LLM_API_KEY is required')
        self.base_url = os.environ.get('OPENMD_LLM_BASE_URL', 'https://delta-router.yangtzeailab.com/v1').rstrip('/')
        self.model = os.environ.get('OPENMD_LLM_MODEL', 'DeepSeek-V4-Flash')
        self.input_price, self.output_price, self.timeout = (input_price or 0.0, output_price or 0.0, timeout)
        self.usage = {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0}
        self._post = post or self._request
        self.diagnostic_path = Path(diagnostic_path) if diagnostic_path else None

    def record_event(self, kind, brief, obs, **fields):
        """Keep failed model text and public request evidence, never HTTP headers/key."""
        if self.diagnostic_path is None:
            return

        def redact(value):
            if isinstance(value, dict):
                return {k: '[REDACTED]' if k.lower() in {'authorization', 'api_key', 'access_token'} else redact(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(v) for v in value]
            if isinstance(value, str) and self.key:
                return value.replace(self.key, '[REDACTED]')
            return value
        event = {'schema_version': 'ie-planner-diagnostic@1', 'time': datetime.now(timezone.utc).isoformat(), 'kind': kind, 'scenario': brief.get('scenario_id'), 'tick': obs['tick'], **fields}
        self.diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        with self.diagnostic_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(redact(event), ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()

    def _request(self, payload, timeout):
        request = urllib.request.Request(self.base_url + '/chat/completions', data=json.dumps(payload, allow_nan=False).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError('LLM response exceeds size limit')
            return strict_loads(body.decode('utf-8'))

class PlanScheduler:

    def __init__(self, source, interval=20):
        if type(interval) is not int or interval < 1:
            raise ValueError('positive planning interval required')
        self.source, self.interval = (source, interval)
        self.current = None
        self.last_plan_tick = -1
        self.last_signature = None
        self.reason = None
