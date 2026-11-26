"""Continuous defense quality from native lifecycle and ammunition facts only."""
from copy import deepcopy
import hashlib
import json
import math
from .legacy_plugins.recon_metrics_v1 import plain

MODEL_ID = 'models.competition-defense-quality'
MODEL_VERSION = '1.0.0'
MODEL_REF = MODEL_ID+'@'+MODEL_VERSION


class DefenseQualityMetric:
    def bind_session(self, *, session_id, seed, parameters):
        del session_id,seed
        p = plain(parameters)
        if set(p)!={'metric_id','kind','threats','ammunition','start_tick','end_tick','unit'}:
            raise ValueError('explicit defense quality schema required')
        if p['kind'] not in {'neutralization_timeliness','ammunition_efficiency'} or p['unit']!='1':
            raise ValueError('unknown dimensionless defense quality')
        if not isinstance(p['metric_id'],str) or not p['metric_id']:
            raise ValueError('named metric required')
        if any(type(p[k]) is not int for k in ('start_tick','end_tick')) or not 0<=p['start_tick']<p['end_tick']:
            raise ValueError('invalid score window')
        if not isinstance(p['threats'],list) or not p['threats']:
            raise ValueError('planned threat denominator required')
        ids = []
        for row in p['threats']:
            if set(row)!={'entity_id','activation_tick','deadline_tick','weight'}:
                raise ValueError('invalid threat schema')
            if (not isinstance(row['entity_id'],str) or not row['entity_id']
                or any(type(row[k]) is not int for k in ('activation_tick','deadline_tick'))
                or not 0<=row['activation_tick']<row['deadline_tick']<p['end_tick']
                or type(row['weight']) not in (int,float) or not math.isfinite(row['weight']) or row['weight']<=0):
                raise ValueError('invalid threat identity/timing/weight')
            ids.append(row['entity_id'])
        if len(set(ids))!=len(ids):
            raise ValueError('duplicate threats')
        keys = []
        if not isinstance(p['ammunition'],list) or not p['ammunition']:
            raise ValueError('explicit ammunition cohort required')
        for row in p['ammunition']:
            if (set(row)!={'entity_id','resource_ref','initial_rounds'}
                or not all(isinstance(row[k],str) and row[k] for k in ('entity_id','resource_ref'))
                or type(row['initial_rounds']) is not int or row['initial_rounds']<1):
                raise ValueError('invalid ammunition baseline')
            keys.append((row['entity_id'],row['resource_ref']))
        if len(set(keys))!=len(keys):
            raise ValueError('duplicate ammunition resources')
        self.parameters = deepcopy(p)
        self.destroyed, self.minimum_rounds = {}, {json.dumps(key):row['initial_rounds'] for key,row in zip(keys,p['ammunition'])}
        self.last_tick, self.last_input_hash, self.last_value = -1,None,None
        self.samples = 0

    def evaluate(self, snapshot):
        p,tick = self.parameters,snapshot.tick
        states = {}
        for row in p['threats']:
            state = snapshot.entity_states.get(row['entity_id'])
            if state is None:
                # The native snapshot does not materialize a future spawn in
                # entity_states. Its declared future duty still counts below.
                if tick<row['activation_tick']:
                    states[row['entity_id']]='scheduled'
                    continue
                raise ValueError('planned active native threat state missing')
            states[row['entity_id']] = state['lifecycle']
        resources = {(r.entity_id,r.resource_ref):r.value for r in snapshot.resource_facts if r.field=='ammunition'}
        rounds = {}
        for row in p['ammunition']:
            key = row['entity_id'],row['resource_ref']
            count = resources.get(key)
            if type(count) not in (int,float) or not math.isfinite(count) or count<0:
                raise ValueError('required native ammunition fact missing/invalid')
            rounds[json.dumps(key)] = count
        anchor = hashlib.sha256(json.dumps([tick,states,rounds],sort_keys=True).encode()).hexdigest()
        if tick<self.last_tick:
            raise ValueError('restore required before going backwards')
        if tick==self.last_tick:
            if anchor!=self.last_input_hash:
                raise ValueError('conflicting same-tick defense facts')
            return {p['metric_id']:self.last_value}
        first = max(p['start_tick'],self.last_tick+1)
        if tick>first and first<p['end_tick']:
            raise ValueError('every authoritative score tick required')
        if p['start_tick']<=tick<p['end_tick']:
            self.samples += 1
            for row in p['threats']:
                if row['activation_tick']<=tick and states[row['entity_id']]=='destroyed':
                    self.destroyed.setdefault(row['entity_id'],tick)
            for key,count in rounds.items():
                self.minimum_rounds[key] = min(self.minimum_rounds[key],count)
            total = sum(r['weight'] for r in p['threats'])
            if p['kind']=='neutralization_timeliness':
                self.last_value = sum(r['weight']*max(0.,1-(self.destroyed[r['entity_id']]-r['activation_tick'])/
                    (r['deadline_tick']-r['activation_tick'])) for r in p['threats'] if r['entity_id'] in self.destroyed)/total
            else:
                neutralized = sum(r['weight'] for r in p['threats'] if r['entity_id'] in self.destroyed)/total
                used = sum(r['initial_rounds']-self.minimum_rounds[json.dumps((r['entity_id'],r['resource_ref']))] for r in p['ammunition'])
                self.last_value = neutralized*min(1.,len(self.destroyed)/max(1.,used))
        self.last_tick,self.last_input_hash = tick,anchor
        return {p['metric_id']:self.last_value}

    def snapshot(self):
        return deepcopy({k:getattr(self,k) for k in ('parameters','destroyed','minimum_rounds','last_tick','last_input_hash','last_value','samples')})

    def restore(self,state):
        state = plain(state)
        p = self.parameters
        if set(state)!=set(self.snapshot()) or state['parameters']!=p:
            raise ValueError('defense checkpoint schema mismatch')
        tick = state['last_tick']
        if type(tick) is not int or tick < -1:
            raise ValueError('invalid checkpoint tick')
        samples = max(0,min(tick,p['end_tick']-1)-p['start_tick']+1)
        if type(state['samples']) is not int or state['samples']!=samples:
            raise ValueError('invalid checkpoint sample count')
        expected = {r['entity_id']:r for r in p['threats']}
        if not isinstance(state['destroyed'],dict) or not set(state['destroyed'])<=set(expected):
            raise ValueError('checkpoint threat identities differ')
        if any(type(t) is not int or not max(p['start_tick'],expected[i]['activation_tick'])<=t<=min(tick,p['end_tick']-1)
               for i,t in state['destroyed'].items()):
            raise ValueError('invalid neutralization tick')
        if not isinstance(state['minimum_rounds'],dict) or set(state['minimum_rounds'])!=set(self.minimum_rounds):
            raise ValueError('checkpoint ammunition identities differ')
        for row in p['ammunition']:
            value = state['minimum_rounds'][json.dumps((row['entity_id'],row['resource_ref']))]
            if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=row['initial_rounds']:
                raise ValueError('invalid ammunition checkpoint')
        if ((samples==0)!=(state['last_value'] is None)
            or state['last_value'] is not None and (type(state['last_value']) not in (int,float)
                or not math.isfinite(state['last_value']) or not 0<=state['last_value']<=1)):
            raise ValueError('invalid checkpoint value')
        anchor = state['last_input_hash']
        if (tick==-1 and anchor is not None) or (tick>=0 and (not isinstance(anchor,str) or len(anchor)!=64
                or not set(anchor)<=set('0123456789abcdef'))):
            raise ValueError('invalid checkpoint fingerprint')
        for key,value in state.items():
            setattr(self,key,deepcopy(value))

    def close(self):
        pass
