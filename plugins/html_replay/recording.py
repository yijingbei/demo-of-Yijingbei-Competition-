"""Stream read-only referee states and authorized observations, one row per tick.

No engine imports: the evaluator supplies its existing public session facade.
This file never submits commands and never sends referee states to a participant.
"""
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path


def plain(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


def observation_snapshot(value):
    """Keep display evidence for either side without duplicating static resources."""
    obs = plain(value)
    snapshot = {k: obs[k] for k in ('tick', 'controller_slot_id',
        'observer_faction_id', 'controlled_entity_ids', 'own_entities',
        'organic_contacts', 'shared_contacts')}
    fields = ('entity_id','position_m','velocity_mps','heading_deg','health',
              'lifecycle_state','ammunition','component_states','energy_state')
    snapshot['own_entities'] = [{k:e[k] for k in fields if k in e}
                                 for e in obs['own_entities']]
    snapshot['received_messages'] = obs.get('received_messages', [])
    return snapshot


class ReplayRecorder:
    def __init__(self, output, scenario, runtime_sha256, tick_seconds, compression='none', weather_schedule=None):
        if __package__:
            from .record_io import RecordWriter
        else:
            from record_io import RecordWriter
        self.stream = RecordWriter(output/'replay.frames.jsonl', compression)
        self.path = self.stream.path
        self.scenario = scenario
        self.runtime_sha256 = runtime_sha256
        self.tick_seconds = float(tick_seconds)
        self.digest = hashlib.sha256()
        self.count = 0
        self.last_tick = None
        self.weather_schedule = weather_schedule

    def capture(self, session):
        tick = session.world_view.tick
        if self.last_tick is None and tick != 0:
            raise ValueError('Replay capture must start at tick zero')
        if self.last_tick is not None and tick != self.last_tick + 1:
            raise ValueError('Replay capture must run exactly once at every tick')
        entities = []
        for e in session.world_view.entities_stable():
            state = e.state
            entities.append(dict(entity_id=e.id, faction_id=e.faction_id,
                position_m=list(state.position_m), velocity_mps=list(state.velocity_mps),
                heading_deg=state.heading_deg, health=state.health,
                lifecycle_state=state.lifecycle, ammunition=dict(state.ammunition),
                energy_state=dict(value=state.energy, unit='normalized',
                                  capacity_semantics='fraction_of_declared_capacity')))
        observations = []
        for slot in session.resolved.controller_slots:
            obs = plain(session.world_view.controller_observation(controller_slot_id=slot.id))
            identification = getattr(session.world_factory, 'contact_projection', getattr(session.world_factory, 'identification', None))
            if identification is not None:
                obs = identification.observation(obs)
            observations.append(observation_snapshot(obs))
        projectiles = []
        if hasattr(session.world_factory, 'projectile_snapshot'):
            for flight in session.world_factory.projectile_snapshot()['missile_flights']:
                p = plain(flight)
                projectiles.append({k: p[k] for k in ('missile_id', 'launcher_id',
                    'target_id', 'faction_id', 'position_m', 'velocity_mps', 'heading_deg')})
        frame = dict(schema_version='competition-replay-frame@1',
            scenario=self.scenario, session_id=session.session_id, tick=tick,
            sim_time_s=tick*self.tick_seconds, tick_seconds=self.tick_seconds,
            runtime_sha256=self.runtime_sha256, entities=entities,
            observations=observations, projectiles=projectiles)
        if hasattr(session.world_view, 'presentation_snapshot'):
            frame['mission'] = plain(session.world_view.presentation_snapshot().mission_scoring_checkpoint)
        if self.weather_schedule is not None:
            from omd_competition.weather_contract import weather_view
            frame['weather'] = weather_view(plain(session.world_factory.weather_snapshot()), self.weather_schedule)
        encoded = (json.dumps(frame, ensure_ascii=False, allow_nan=False,
                              separators=(',', ':')) + '\n').encode('utf-8')
        self.stream.write(encoded)
        self.digest.update(encoded)
        self.count += 1
        self.last_tick = tick

    def close(self, final_tick, completed):
        self.stream.close()
        return dict(**self.stream.metadata(), schema_version='competition-replay-frame@1',
            frames=self.count, initial_tick=0,
            last_tick=self.last_tick, tick_seconds=self.tick_seconds,
            every_tick=True, complete=bool(completed and self.last_tick == final_tick),
            recorder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
