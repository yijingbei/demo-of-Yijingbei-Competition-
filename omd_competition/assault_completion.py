"""Finish a finite assault after authenticated, irreversible withdrawal reports.

Reports come through the native transport, from each attacker's own endpoint.
This metric never changes lifecycle, ammunition, damage or kill credit.
"""
from copy import deepcopy
import hashlib
import json
from .agent import navigation_batch
from .combat_completion import LIVE, TERMINAL
from .legacy_plugins.recon_metrics_v1 import plain
from openmdbench.missions.engine_v2 import combat_settlement_ready_v2

MODEL_ID='models.competition-assault-withdrawal'
MODEL_VERSION='1.0.0'
MODEL_REF=MODEL_ID+'@'+MODEL_VERSION
PROTOCOL='competition.assault-withdrawal.v1'
REASONS={'search_complete','strike_expended','all_rounds_expended'}


def append_withdrawal_reports(policy, obs, batch):
    if not policy.plan['attack'].get('mission_completion',{}).get('enabled'):
        return batch
    if not hasattr(policy,'withdrawal_report_ticks'):
        policy.withdrawal_report_ticks={}
    tick=obs['tick']
    for own in obs['own_entities']:
        eid=own['entity_id']
        record=policy.assault_withdrawals.get(eid)
        comm=own.get('communication_state',{})
        if (record is None or eid not in obs['controlled_entity_ids']
                or not own.get('capability_flags',{}).get('communication')
                or comm.get('state')!='available'
                or not comm.get('is_controller_endpoint')
                or tick-policy.withdrawal_report_ticks.get(eid,-1000)<10):
            continue
        if batch is None:
            batch=navigation_batch(obs,{})
        body={'protocol':PROTOCOL,'actor':eid,**record}
        batch['discrete_actions'].append({
            'schema_version':'2.0','entity_id':eid,
            'faction_id':obs['observer_faction_id'],'based_on_tick':tick,
            'valid_until_tick':tick+1,'action_id':f'withdraw.{eid}.{tick}',
            'action_type':'send_message','payload':{
                'recipient_controller_slots':[obs['controller_slot_id']],
                'message':json.dumps(body,sort_keys=True)}})
        policy.withdrawal_report_ticks[eid]=tick
    return batch


def delivery(raw, parameters, tick):
    """Validate native envelope and body together; payload IDs alone are untrusted."""
    eid=raw.get('sender_entity_id')
    if (eid not in parameters['controllers'] or raw.get('transport_status')!='delivered'
            or raw.get('recipient_entity_id')!=eid or raw.get('zero_hop') is not True
            or parameters['controllers'][eid] not in raw.get('recipient_controller_slots',())):
        return None
    generated,delivered,expiry=[raw.get(k) for k in ('generated_tick','delivered_tick','expiry_tick')]
    if (any(type(t) is not int for t in (generated,delivered,expiry))
            or not parameters['spawn_ticks'][eid]<=generated<=delivered<=min(expiry,tick)):
        return None
    try:
        body=json.loads(raw.get('payload',''))
    except (ValueError,TypeError):
        return None
    if (not isinstance(body,dict) or set(body)!={'protocol','actor','commit_tick','reason'}
            or body['protocol']!=PROTOCOL or body['actor']!=eid
            or body['reason'] not in REASONS or type(body['commit_tick']) is not int
            or not parameters['spawn_ticks'][eid]<=body['commit_tick']<=generated):
        return None
    identifier=raw.get('message_id')
    if not isinstance(identifier,str) or not identifier:
        return None
    return {'actor':eid,'commit_tick':body['commit_tick'],'reason':body['reason'],
            'delivered_tick':delivered,'message_id':identifier}


class AssaultWithdrawalMetric:
    def bind_session(self,*,session_id,seed,parameters):
        self.parameters=deepcopy(plain(parameters))
        p=self.parameters
        if set(p)!={'metric_id','controllers','spawn_ticks','confirmation_ticks','require_drained','unit'}:
            raise ValueError('invalid assault withdrawal parameters')
        if (p['unit']!='1' or not p['controllers'] or set(p['controllers'])!=set(p['spawn_ticks'])
                or type(p['confirmation_ticks']) is not int or p['confirmation_ticks']<0
                or type(p['require_drained']) is not bool
                or any(type(t) is not int or t<0 for t in p['spawn_ticks'].values())):
            raise ValueError('invalid declared withdrawal cohort')
        self.commitments={};self.last_tick=-1;self.last_hash=None;self.last_value=None

    def evaluate(self,snapshot):
        p=self.parameters
        reports=[row for raw in snapshot.communications
                 if (row:=delivery(raw,p,snapshot.tick)) is not None]
        facts=[snapshot.tick,plain(snapshot.entity_states),plain(snapshot.combat_activity),reports]
        digest=hashlib.sha256(json.dumps(facts,sort_keys=True).encode()).hexdigest()
        if snapshot.tick<self.last_tick or snapshot.tick==self.last_tick and digest!=self.last_hash:
            raise ValueError('conflicting assault withdrawal facts')
        for row in sorted(reports,key=lambda r:(r['delivered_tick'],r['message_id'])):
            self.commitments.setdefault(row['actor'],row)
        confirmed=[];eligible=True
        for eid in p['controllers']:
            state=snapshot.entity_states.get(eid,{})
            lifecycle=state.get('lifecycle')
            if snapshot.tick<p['spawn_ticks'][eid] or lifecycle not in LIVE|TERMINAL:
                eligible=False;break
            if lifecycle in TERMINAL:
                continue
            record=self.commitments.get(eid)
            if record is None:
                eligible=False;break
            confirmed.append(record['delivered_tick'])
        if confirmed and snapshot.tick-max(confirmed)<p['confirmation_ticks']:
            eligible=False
        activity=plain(snapshot.combat_activity)
        if not isinstance(activity,dict) or any(type(activity.get(k)) is not int or activity[k]!=0
                for k in ('active_missiles','pending_impacts','scheduled_effects')):
            eligible=False
        if eligible and p['require_drained']:
            try:
                eligible=combat_settlement_ready_v2(activity)
            except ValueError:
                eligible=False
        self.last_tick=snapshot.tick;self.last_hash=digest;self.last_value=float(eligible)
        return {p['metric_id']:self.last_value}

    def snapshot(self):
        return {'parameters':deepcopy(self.parameters),'commitments':deepcopy(self.commitments),
                'last_tick':self.last_tick,'last_hash':self.last_hash,'last_value':self.last_value}

    def restore(self,state):
        state=plain(state)
        if set(state)!=set(self.snapshot()) or state['parameters']!=self.parameters:
            raise ValueError('withdrawal checkpoint differs')
        for field in ('commitments','last_tick','last_hash','last_value'):
            setattr(self,field,deepcopy(state[field]))

    def close(self):
        pass
