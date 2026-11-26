"""Continuous interception quality from immutable native contact/resource facts."""
from copy import deepcopy
import hashlib
import json
import math

from .engagement_metrics import LOST
from .legacy_plugins.recon_metrics_v1 import plain

MODEL_ID = 'models.competition-intercept-performance'
MODEL_VERSION = '0.7.0'
MODEL_REF = MODEL_ID+'@'+MODEL_VERSION


class InterceptPerformanceMetric:
    def bind_session(self, *, session_id, seed, parameters):
        del session_id, seed
        p = plain(parameters)
        required = {'metric_id','kind','attacker_ids','defender_ids','facility_weights',
                    'ammunition_budgets','threat_half_lives','nominal_rounds_per_threat','unit'}
        if set(p) != required or p['unit'] != '1' or p['kind'] not in {
                'interception_timeliness','ammunition_efficiency'}:
            raise ValueError('invalid interception performance contract')
        for key in ('attacker_ids','defender_ids'):
            values = p[key]
            if not values or len(values) != len(set(values)) or any(not isinstance(v,str) or not v for v in values):
                raise ValueError('explicit unique performance cohorts required')
        if set(p['threat_half_lives']) != set(p['attacker_ids']) or set(p['nominal_rounds_per_threat']) != set(p['attacker_ids']):
            raise ValueError('all planned threats need performance parameters')
        if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in p['threat_half_lives'].values()):
            raise ValueError('positive finite half lives required')
        if any(type(v) is not int or v < 1 for v in p['nominal_rounds_per_threat'].values()):
            raise ValueError('positive exact nominal round counts required')
        if (not p['facility_weights'] or not set(p['facility_weights']) <= set(p['defender_ids'])
                or any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in p['facility_weights'].values())):
            raise ValueError('explicit positively weighted facilities required')
        budgets = p['ammunition_budgets']
        if (not budgets or not set(budgets) <= set(p['defender_ids']) or
                any(not ammo or any(type(v) is not int or v < 0 for v in ammo.values()) for ammo in budgets.values())):
            raise ValueError('declared own ammunition budgets required')
        self.parameters = deepcopy(p)
        self.first_contacts, self.neutralized_ticks = {}, {}
        self.remaining = deepcopy(budgets)
        self.last_tick, self.last_input_hash, self.last_value = -1, None, None

    def evaluate(self, snapshot):
        p, tick = self.parameters, snapshot.tick
        states = {e: dict(snapshot.entity_states.get(e, {})) for e in [*p['attacker_ids'],*p['defender_ids']]}
        facts = [(f.owner_entity_id,f.target_entity_id,f.observed_tick,f.max_age_ticks,
                  f.confidence,f.minimum_confidence) for f in snapshot.contact_facts]
        resources = {e: dict(snapshot.resources.get(e, {})) for e in p['ammunition_budgets']}
        anchor = hashlib.sha256(json.dumps([tick,states,sorted(facts),resources],sort_keys=True).encode()).hexdigest()
        if tick == self.last_tick:
            if anchor != self.last_input_hash:
                raise ValueError('conflicting performance facts for same tick')
            return {p['metric_id']: self.last_value}
        if tick < self.last_tick or (self.last_tick >= 0 and tick != self.last_tick+1) or (self.last_tick < 0 and tick > 1):
            raise ValueError('performance scoring requires every authoritative tick')
        # Sensor evidence, never participant reports or opaque-ID disappearance.
        for owner,target,observed,max_age,confidence,minimum in facts:
            if (owner in p['defender_ids'] and target in p['attacker_ids']
                    and 0 <= observed <= tick and tick-observed <= max_age and confidence >= minimum):
                self.first_contacts[target] = min(observed,self.first_contacts.get(target,observed))
        for target in p['attacker_ids']:
            if states[target].get('self_detonated',False):
                self.neutralized_ticks.pop(target,None)
            elif states[target].get('lifecycle') in LOST:
                self.neutralized_ticks.setdefault(target,tick)
        for entity, ammo in p['ammunition_budgets'].items():
            for reference, initial in ammo.items():
                remaining = resources[entity].get(reference)
                if remaining is None:
                    if states[entity].get('lifecycle') in LOST:
                        remaining = 0
                    else:
                        raise ValueError('native own ammunition fact missing')
                if type(remaining) not in (int,float) or not math.isfinite(remaining) or not 0 <= remaining <= initial or int(remaining) != remaining:
                    raise ValueError('invalid native ammunition fact')
                self.remaining[entity][reference] = min(self.remaining[entity][reference],int(remaining))
        if p['kind'] == 'interception_timeliness':
            value = math.fsum(2**(-max(0,self.neutralized_ticks[e]-self.first_contacts[e])/p['threat_half_lives'][e])
                              for e in p['attacker_ids'] if e in self.first_contacts and e in self.neutralized_ticks
                              and self.first_contacts[e] <= self.neutralized_ticks[e])/len(p['attacker_ids'])
        else:
            weights = p['facility_weights']
            if any('health' not in states[e] for e in weights):
                raise ValueError('native facility health fact missing')
            integrity = math.fsum(w*(0 if states[e].get('lifecycle') in LOST else states[e]['health'])
                                   for e,w in weights.items())/math.fsum(weights.values())
            spent = sum(initial-self.remaining[e][ref] for e,ammo in p['ammunition_budgets'].items() for ref,initial in ammo.items())
            nominal = sum(p['nominal_rounds_per_threat'][e] for e in self.neutralized_ticks)
            completion = len(self.neutralized_ticks)/len(p['attacker_ids'])
            value = completion*integrity*min(1.,nominal/max(1,spent))
        self.last_tick, self.last_input_hash, self.last_value = tick, anchor, value
        return {p['metric_id']: value}

    def snapshot(self):
        return {'parameters': deepcopy(self.parameters), 'first_contacts': dict(self.first_contacts),
                'neutralized_ticks': dict(self.neutralized_ticks), 'remaining': deepcopy(self.remaining),
                'last_tick': self.last_tick, 'last_input_hash': self.last_input_hash, 'last_value': self.last_value}

    def restore(self, state):
        state = plain(state)
        if set(state) != set(self.snapshot()) or state['parameters'] != self.parameters:
            raise ValueError('performance checkpoint contract differs')
        if not set(state['first_contacts']) <= set(self.parameters['attacker_ids']) or not set(state['neutralized_ticks']) <= set(self.parameters['attacker_ids']):
            raise ValueError('performance checkpoint cohort differs')
        if set(state['remaining']) != set(self.parameters['ammunition_budgets']):
            raise ValueError('performance checkpoint ammunition cohort differs')
        for field in ('first_contacts','neutralized_ticks','remaining','last_tick','last_input_hash','last_value'):
            setattr(self,field,deepcopy(state[field]))

    def close(self):
        pass
