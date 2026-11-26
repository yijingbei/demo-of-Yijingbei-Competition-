"""End a declared finite assault once neither side has usable combat rounds.

This metric does not award neutralizations or replace the normal success rules.
Future spawns and native pending combat prevent early completion.
"""
from copy import deepcopy
import hashlib,json
from openmdbench.missions.engine_v2 import combat_settlement_ready_v2
from .legacy_plugins.recon_metrics_v1 import plain

MODEL_ID='models.competition-combat-exhaustion'
MODEL_VERSION='1.0.0'
MODEL_REF=MODEL_ID+'@'+MODEL_VERSION
LIVE={'active','degraded'}
TERMINAL={'disabled','destroyed','wreck','despawned'}

def declared_time_limit_reached(terminal,brief,tick,duration):
    limit=brief.get('terminal_contract',{}).get('hard_time_limit_ticks')
    return (type(limit) is int and limit>0 and terminal is not None
            and terminal.get('rule_id')=='rule.hard-time-limit'
            and tick==limit and duration==limit)

def combat_exhausted(snapshot,participants,require_drained=True):
    states=snapshot.entity_states
    # An absent, scheduled or unknown future actor is still an unresolved threat.
    if any(p['entity_id'] not in states or states[p['entity_id']].get('lifecycle') not in LIVE|TERMINAL for p in participants):
        return False
    # Keep navigation and missile evasion live until every physical projectile
    # has resolved. Only navigation/controller requests may drain in phase two.
    activity=plain(snapshot.combat_activity)
    if not isinstance(activity,dict) or any(type(activity.get(k)) is not int or activity[k]!=0
            for k in ('active_missiles','pending_impacts','scheduled_effects')):
        return False
    if require_drained:
        try:
            drained=combat_settlement_ready_v2(activity)
        except ValueError:
            return False
        if not drained:
            return False
    targets=[p for p in participants if states[p['entity_id']].get('lifecycle') in LIVE]
    for actor in targets:
        resources=snapshot.resources.get(actor['entity_id'])
        if actor['weapons'] and resources is None:
            return False
        for weapon in actor['weapons']:
            rounds=(resources or {}).get(weapon['ammunition_ref'])
            if rounds is None:
                return False
            if rounds>0 and any(target['faction_id']!=actor['faction_id'] and target['domain'] in weapon['target_domains'] for target in targets):
                return False
    return True

class CombatExhaustionMetric:
    def bind_session(self,*,session_id,seed,parameters):
        self.parameters=deepcopy(plain(parameters))
        if set(self.parameters)-{'require_drained'}!={'metric_id','participants','unit'} or self.parameters['unit']!='1':
            raise ValueError('invalid combat exhaustion parameters')
        if type(self.parameters.get('require_drained',True)) is not bool:
            raise ValueError('require_drained must be boolean')
        actors=self.parameters['participants']
        if not actors or len({p['entity_id'] for p in actors})!=len(actors):
            raise ValueError('explicit unique participants required')
        for actor in actors:
            if set(actor)!={'entity_id','faction_id','domain','weapons'}:
                raise ValueError('invalid combat participant')
            for weapon in actor['weapons']:
                if set(weapon)!={'ammunition_ref','target_domains'} or not weapon['target_domains']:
                    raise ValueError('invalid combat weapon')
        self.last_tick=-1;self.last_hash=None;self.last_value=None

    def evaluate(self,snapshot):
        actors=self.parameters['participants']
        facts=[snapshot.tick,plain(snapshot.entity_states),plain(snapshot.resources),plain(snapshot.combat_activity)]
        digest=hashlib.sha256(json.dumps(facts,sort_keys=True).encode()).hexdigest()
        if snapshot.tick<self.last_tick or snapshot.tick==self.last_tick and digest!=self.last_hash:
            raise ValueError('conflicting combat exhaustion facts')
        self.last_tick=snapshot.tick;self.last_hash=digest
        self.last_value=float(combat_exhausted(snapshot,actors,self.parameters.get('require_drained',True)))
        return {self.parameters['metric_id']:self.last_value}

    def snapshot(self):
        return {'parameters':deepcopy(self.parameters),'last_tick':self.last_tick,'last_hash':self.last_hash,'last_value':self.last_value}

    def restore(self,state):
        state=plain(state)
        if set(state)!=set(self.snapshot()) or state['parameters']!=self.parameters:
            raise ValueError('combat exhaustion checkpoint differs')
        self.last_tick=state['last_tick'];self.last_hash=state['last_hash'];self.last_value=state['last_value']

    def close(self):pass
