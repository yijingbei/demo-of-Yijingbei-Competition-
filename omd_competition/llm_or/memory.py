"""Execution ledger driven by actual submitted actions and owned observations."""
from copy import deepcopy
import math

from .state import weapon_options


class ExecutionMemory:
    def __init__(self, brief, tick_seconds=1.):
        if not math.isfinite(tick_seconds) or tick_seconds <= 0:
            raise ValueError('positive tick_seconds required')
        self.brief, self.tick_seconds = brief, tick_seconds
        self.shots = {}
        self.last_tick = -1
        self.confirmed_launches = 0
        self.rejected_requests = 0
        self.unconfirmed_requests = 0

    def observe(self, obs, last_receipt=None):
        tick = obs['tick']
        if tick < self.last_tick:
            raise ValueError('execution observations must not move backwards')
        receipt = last_receipt or {}
        rejected = {r.get('child_id') for r in receipt.get('execution_receipts', [])
                    if r.get('status') in {'rejected', 'failed', 'cancelled'}}
        queue = receipt.get('queue_receipt') or {}
        if queue.get('status') == 'rejected':
            rejected.update(k for k, s in self.shots.items()
                            if s['status'] == 'pending' and s['batch_id'] == queue.get('batch_id'))
        own = {u['entity_id']: u for u in obs['own_entities']}
        for key, shot in list(self.shots.items()):
            if shot['status'] == 'pending':
                unit = own.get(shot['unit_id'])
                active = unit is not None and unit.get('lifecycle_state', 'active') in {'active', 'degraded'}
                if key in rejected:
                    self.rejected_requests += 1
                    del self.shots[key]
                    continue
                if active and unit.get('ammunition', {}).get(shot['ammunition_ref'], shot['rounds_before']) < shot['rounds_before']:
                    shot['status'] = 'confirmed'
                    shot['confirmed_tick'] = tick
                    self.confirmed_launches += 1
                elif not active or tick >= shot['verify_until_tick']:
                    self.unconfirmed_requests += 1
                    del self.shots[key]
                    continue
            if tick >= shot['expires_tick']:
                del self.shots[key]
        self.last_tick = tick

    def record_submission(self, obs, batch):
        own = {u['entity_id']: u for u in obs['own_entities']}
        visible = {c['contact_id']: c for c in [*obs.get('shared_contacts', []), *obs.get('organic_contacts', [])]}
        for action in (batch or {}).get('discrete_actions', []):
            if action['action_type'] != 'fire_weapon':
                continue
            key = action['action_id']
            if key in self.shots:
                raise ValueError('shot submitted twice: ' + key)
            uid, payload = action['entity_id'], action['payload']
            policy = next(w for w in weapon_options(self.brief, uid) if w['weapon_ref'] == payload['weapon_ref'])
            distance = math.dist(own[uid]['position_m'], visible[payload['contact_id']]['estimated_position_m'])
            maximum = policy['missile_max_flight_ticks']
            flight = min(maximum, math.ceil(distance / max(5., policy['missile_cruise_speed_mps']) / self.tick_seconds) + 8)
            if 'air' not in policy['target_domains']:
                flight = min(maximum, flight + self.brief.get('defence', {}).get('surface_inflight_grace_ticks', 0))
            self.shots[key] = {'unit_id': uid, 'weapon_ref': policy['weapon_ref'],
                               'ammunition_ref': policy['ammunition_ref'], 'contact_id': payload['contact_id'],
                               'rounds_before': own[uid]['ammunition'][policy['ammunition_ref']],
                               'batch_id': batch['batch_id'],
                               'issued_tick': obs['tick'], 'verify_until_tick': obs['tick'] + 2,
                               'expires_tick': obs['tick'] + flight + 2, 'status': 'pending'}

    def snapshot(self):
        return {'tick': self.last_tick, 'shots': deepcopy(self.shots),
                'confirmed_launches': self.confirmed_launches,
                'rejected_requests': self.rejected_requests,
                'unconfirmed_requests': self.unconfirmed_requests}
