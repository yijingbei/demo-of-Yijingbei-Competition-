"""Referee-only boundary snapshots of the existing native transport state.

This observes public event_state_snapshot(), never advances or patches the world.
The native snapshot still copies retained history; delta output only limits disk
growth. Intermediate states pruned inside a native step are outside this record.
"""
from copy import deepcopy

from .score_evidence import digest, plain


GROUPS = ('message_queue', 'shared_contact_queue', 'command_queue',
          'shared_contacts_by_controller', 'controller_inboxes', 'inbox_evictions')


def project(snapshot):
    state = {}
    for group, identity in [('message_queue', 'message_id'),
                            ('shared_contact_queue', 'shared_contact_id'),
                            ('command_queue', 'transport_id')]:
        state[group] = {}
        for item in getattr(snapshot, group):
            record = plain(item)
            if group == 'command_queue':
                # This credential is unnecessary for evidence and must not be stored.
                record.pop('authority_token', None)
            key = record[identity]
            if key in state[group]:
                raise ValueError('duplicate native transport identity')
            state[group][key] = record
    for group in ('shared_contacts_by_controller', 'controller_inboxes'):
        state[group] = plain(getattr(snapshot, group))
    state['inbox_evictions'] = {str(i): plain(item)
        for i, item in enumerate(snapshot.inbox_evictions)}
    return state


class TransportEvidenceRecorder:
    def __init__(self, initial_snapshot, *, seed, resolved_hash, dt_seconds):
        self._state = project(initial_snapshot)
        self._initial = deepcopy(self._state)
        self._context = dict(seed=seed, resolved_hash=resolved_hash,
                             physics_dt_seconds=dt_seconds)
        self._tick = 0
        self._previous_hash = None

    def capture(self, tick, snapshot):
        if tick != self._tick + 1:
            raise ValueError('transport evidence tick gap')
        state = project(snapshot)
        upserts = {g: {k: v for k, v in state[g].items()
                      if k not in self._state[g] or self._state[g][k] != v} for g in GROUPS}
        removed = {g: sorted(set(self._state[g]) - set(state[g])) for g in GROUPS}
        evidence = dict(schema_version='competition-transport-evidence@1', tick=tick,
            phase='after_session_step', previous_evidence_hash=self._previous_hash,
            previous_state_hash=digest(self._state), state_hash=digest(state),
            upserts=upserts, removed=removed)
        if self._tick == 0:
            evidence.update(context=deepcopy(self._context), initial_state=self._initial)
        evidence['evidence_hash'] = digest(evidence)
        self._state = state
        self._initial = None
        self._tick = tick
        self._previous_hash = evidence['evidence_hash']
        return deepcopy(evidence)
