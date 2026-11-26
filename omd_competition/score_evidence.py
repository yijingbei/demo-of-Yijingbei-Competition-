"""Capture trusted scoring calls for the referee without changing plugin state."""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, is_dataclass
import hashlib
import json


def plain(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [plain(child) for child in value]
    return value


def digest(value):
    raw = json.dumps(plain(value), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


class ScoreEvidenceRecorder:
    """One session, one pending step. Snapshots are copied at the call boundary."""
    def __init__(self):
        self._calls = []
        self._snapshots = {}
        self._input = None
        self._input_hash = None
        self._sequence = 0
        self._previous_hash = None

    def capture_input(self, snapshot):
        # MissionEvaluationSnapshotV2 is immutable and shared by that scoring
        # operation. Retaining its identity avoids nine copies of the same facts.
        if snapshot is not self._input:
            copied = plain(snapshot)
            self._input_hash = digest(copied)
            self._snapshots[self._input_hash] = copied
            self._input = snapshot
        return self._input_hash

    def append(self, *, model_ref, artifact_sha256, input_hash, before, after, output):
        call = dict(sequence=self._sequence, model_ref=model_ref, artifact_sha256=artifact_sha256,
            metric_id=before['parameters']['metric_id'], input_snapshot_hash=input_hash,
            state_before=before, state_after=after, output=plain(output),
            state_hash_before=digest(before), state_hash_after=digest(after))
        call['call_hash'] = digest(call)
        self._calls.append(call)
        self._sequence += 1

    def drain(self, tick):
        if not self._calls or any(s['tick'] != tick for s in self._snapshots.values()):
            raise ValueError('missing or cross-tick score plugin evidence')
        evidence = dict(schema_version='competition-score-plugin-evidence@1', tick=tick,
            previous_evidence_hash=self._previous_hash, snapshots=self._snapshots, calls=self._calls)
        evidence['evidence_hash'] = digest(evidence)
        self._previous_hash = evidence['evidence_hash']
        self._calls, self._snapshots = [], {}
        self._input = self._input_hash = None
        return evidence


class RecordedScorePlugin:
    """Delegate the native plugin protocol and keep audit state outside snapshots."""
    def __init__(self, plugin, recorder, model_ref, artifact_sha256):
        self._plugin = plugin
        self._recorder = recorder
        self._model_ref = model_ref
        self._artifact_sha256 = artifact_sha256

    def __deepcopy__(self, memo):
        # ModelRegistry isolates every plugin product with deepcopy. Preserve
        # that isolation for the actual scoring state. The referee channel is
        # scoped to this create_session call and intentionally shared by its
        # metrics, so copies report to the same organizer-owned channel.
        copied = type(self)(deepcopy(self._plugin, memo), self._recorder,
                            self._model_ref, self._artifact_sha256)
        memo[id(self)] = copied
        return copied

    def bind_session(self, **kwargs):
        bound = self._plugin.bind_session(**kwargs)
        if bound is not None:
            self._plugin = bound
        return self

    def evaluate(self, snapshot):
        input_hash = self._recorder.capture_input(snapshot)
        before = plain(self._plugin.snapshot())
        output = self._plugin.evaluate(snapshot)
        after = plain(self._plugin.snapshot())
        self._recorder.append(model_ref=self._model_ref, artifact_sha256=self._artifact_sha256,
            input_hash=input_hash, before=before, after=after, output=output)
        return output

    def snapshot(self):
        return self._plugin.snapshot()

    def restore(self, state):
        return self._plugin.restore(state)

    def close(self):
        return self._plugin.close()
