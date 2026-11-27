"""Rolling flight occupancy from owned launches and public contact motion only."""
import math
from .memory import ExecutionMemory
from .state import weapon_options


def intercept_seconds(relative, velocity, speed):
    a = sum(v*v for v in velocity) - speed*speed
    b = 2*sum(r*v for r,v in zip(relative, velocity))
    c = sum(r*r for r in relative)
    if c < 1e-9:
        return 0.
    if abs(a) < 1e-9:
        return max(0., -c/b) if b < -1e-9 else math.sqrt(c)/speed
    delta = b*b-4*a*c
    roots = [(-b+s*math.sqrt(max(0., delta)))/(2*a) for s in (-1,1)] if delta >= 0 else []
    positive = [t for t in roots if t >= 0]
    return min(positive) if positive else math.sqrt(c)/speed


class FlightBeliefMemory(ExecutionMemory):
    def __init__(self, brief, tracks):
        super().__init__(brief)
        self.tracks = tracks
        self.expired_unknown = []

    def refresh(self, obs):
        tick = obs['tick']
        for shot in self.shots.values():
            if 'launch_position_m' not in shot:
                continue
            estimate = self.tracks.estimate(shot['contact_id'], tick)
            if estimate is None:
                continue
            weapon = next(w for w in weapon_options(self.brief, shot['unit_id']) if w['weapon_ref'] == shot['weapon_ref'])
            elapsed = tick-shot['issued_tick']
            velocity = estimate['velocity_mps']
            # Reconstruct target position at launch using a public motion fit.
            relative = [estimate['position_m'][i]-velocity[i]*elapsed-shot['launch_position_m'][i] for i in range(3)]
            flight = intercept_seconds(relative, velocity, max(5., weapon['missile_cruise_speed_mps']))
            margin = min(16., max(6., flight*.1+2*estimate['age_ticks']))
            end = shot['issued_tick']+math.ceil(min(weapon['missile_max_flight_ticks'], flight+margin))+2
            shot['expires_tick'] = max(shot['verify_until_tick']+1, end)
            shot['flight_belief'] = {'model': 'public-moving-target-intercept@3',
                'expected_arrival_tick': shot['issued_tick']+flight, 'uncertainty_ticks': margin,
                'effect': 'unknown', 'updated_tick': tick}

    def observe(self, obs, last_receipt=None):
        self.refresh(obs)
        expiring = [{**s, 'action_id': k, 'effect': 'unknown'} for k,s in self.shots.items()
                    if s['status'] == 'confirmed' and obs['tick'] >= s['expires_tick']]
        super().observe(obs, last_receipt)
        self.expired_unknown = (self.expired_unknown+expiring)[-16:]

    def record_submission(self, obs, batch):
        super().record_submission(obs, batch)
        own = {u['entity_id']:u for u in obs['own_entities']}
        for action in batch.get('discrete_actions', []):
            shot = self.shots.get(action['action_id'])
            if shot is not None:
                shot['launch_position_m'] = list(own[shot['unit_id']]['position_m'])
                shot['legacy_expiry_tick'] = shot['expires_tick']
        self.refresh(obs)

    def snapshot(self):
        return {**super().snapshot(), 'expired_effect_unknown': list(self.expired_unknown),
                'occupancy_semantics': 'predicted flight window, never proof of hit or miss'}
