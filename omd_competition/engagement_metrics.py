"""Trusted interception metrics over immutable native mission facts."""
from copy import deepcopy
import hashlib
import json
import math
from .legacy_plugins.recon_metrics_v1 import plain
from openmdbench.missions.engine_v2 import combat_settlement_ready_v2

MODEL_ID = 'models.competition-interception'
MODEL_VERSION = '0.5.0'
MODEL_REF = MODEL_ID + '@' + MODEL_VERSION
LOST = {'disabled', 'destroyed', 'wreck', 'despawned'}


class InterceptionMetric:
    def bind_session(self, *, session_id, seed, parameters):
        parameters = plain(parameters)
        self.parameters = deepcopy(parameters)
        required = {'metric_id', 'kind', 'attacker_ids', 'defender_ids',
                    'protected_zone_ids', 'facility_weights', 'settlement_ticks', 'unit'}
        if set(parameters) - {'settlement_mode'} != required or parameters['unit'] != '1':
            raise ValueError('invalid interception parameters')
        if parameters.get('settlement_mode', 'fixed_window') not in {'fixed_window', 'queue_drained'}:
            raise ValueError('invalid interception settlement mode')
        if parameters['kind'] not in {'protection', 'neutralization', 'graded_protection', 'survival', 'facilities', 'settled', 'assets_lost_settled'}:
            raise ValueError('unknown interception metric')
        for key in ('attacker_ids', 'defender_ids', 'protected_zone_ids'):
            values = parameters[key]
            if not values or len(set(values)) != len(values):
                raise ValueError('explicit unique metric cohort required')
        weights = parameters['facility_weights']
        if not weights or any(v <= 0 or not math.isfinite(v) for v in weights.values()):
            raise ValueError('positive facility weights required')
        if not set(weights) <= set(parameters['defender_ids']) or parameters['settlement_ticks'] < 80:
            raise ValueError('invalid facility cohort or missile settlement window')
        self.breached = set()
        self.neutralized_since = None
        self.assets_lost_since = None
        self.last_tick = -1
        self.last_input_hash = None
        self.last_value = None

    def evaluate(self, snapshot):
        p = self.parameters
        states = {entity: dict(snapshot.entity_states.get(entity, {}))
                  for entity in [*p['attacker_ids'], *p['defender_ids']]}
        breaches = {entity for entity in p['attacker_ids']
                    if set(snapshot.zone_membership.get(entity, ())) & set(p['protected_zone_ids'])}
        breaches.update(x.entity_id for x in snapshot.zone_transitions
                        if x.entity_id in p['attacker_ids'] and x.zone_id in p['protected_zone_ids']
                        and x.transition == 'entered')
        queue_drained = None
        activity = None
        if p.get('settlement_mode') == 'queue_drained' and p['kind'] in {'settled', 'assets_lost_settled'}:
            activity = plain(getattr(snapshot, 'combat_activity', None))
            queue_drained = combat_settlement_ready_v2(activity)
        anchor = hashlib.sha256(json.dumps([snapshot.tick, states, sorted(breaches), activity], sort_keys=True).encode()).hexdigest()
        if snapshot.tick == self.last_tick:
            if anchor != self.last_input_hash:
                raise ValueError('conflicting score facts for same tick')
            return {p['metric_id']: self.last_value}
        if snapshot.tick < self.last_tick:
            raise ValueError('score clock regressed')
        self.breached.update(breaches)
        stopped = {e for e in p['attacker_ids'] if states[e].get('lifecycle') in LOST}
        neutralized = sum(not states[e].get('self_detonated',False) for e in stopped)
        if len(stopped) == len(p['attacker_ids']):
            if self.neutralized_since is None:
                self.neutralized_since = snapshot.tick
        else:
            self.neutralized_since = None
        if all(states[e].get('lifecycle') in LOST for e in p['facility_weights']):
            if self.assets_lost_since is None:
                self.assets_lost_since = snapshot.tick
        else:
            self.assets_lost_since = None
        kind = p['kind']
        if kind == 'protection':
            value = float(not self.breached)
        elif kind == 'graded_protection':
            value = 1-len(self.breached)/len(p['attacker_ids'])
        elif kind == 'neutralization':
            value = neutralized/len(p['attacker_ids'])
        elif kind == 'settled':
            value = float(self.neutralized_since is not None and (
                queue_drained if queue_drained is not None else snapshot.tick-self.neutralized_since >= p['settlement_ticks']))
        elif kind == 'assets_lost_settled':
            value = float(self.assets_lost_since is not None and (
                queue_drained if queue_drained is not None else snapshot.tick-self.assets_lost_since >= p['settlement_ticks']))
        else:
            cohort = p['facility_weights'] if kind == 'facilities' else {e: 1 for e in p['defender_ids']}
            if any('health' not in states[e] for e in cohort):
                raise ValueError('health facts missing')
            value = math.fsum(w*(0 if states[e].get('lifecycle') in LOST else states[e]['health'])
                              for e, w in cohort.items()) / math.fsum(cohort.values())
        self.last_tick, self.last_input_hash, self.last_value = snapshot.tick, anchor, value
        return {p['metric_id']: value}

    def snapshot(self):
        return {'parameters': deepcopy(self.parameters), 'breached': sorted(self.breached),
                'neutralized_since': self.neutralized_since, 'assets_lost_since': self.assets_lost_since, 'last_tick': self.last_tick,
                'last_input_hash': self.last_input_hash, 'last_value': self.last_value}

    def restore(self, state):
        state = plain(state)
        if set(state) != set(self.snapshot()) or state['parameters'] != self.parameters:
            raise ValueError('score checkpoint contract differs')
        if not set(state['breached']) <= set(self.parameters['attacker_ids']):
            raise ValueError('invalid breach cohort')
        for field in ('neutralized_since', 'assets_lost_since', 'last_tick', 'last_input_hash', 'last_value'):
            setattr(self, field, state[field])
        self.breached = set(state['breached'])

    def close(self):
        pass
