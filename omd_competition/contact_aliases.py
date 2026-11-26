"""Conservative participant-side association from authorized sensor reports.

An opaque handle belongs to a source observer, not necessarily to a unique
physical target. Only matching position/time evidence links observers here.
The ledger never receives referee identities, health, hits or future spawns.
It is a planning heuristic; it creates neither fire authority nor kill credit.
"""
import math


def validate_motion_association(profile):
    fields = {'maximum_age_ticks', 'maximum_sample_gap_ticks',
              'position_tolerance_m', 'velocity_tolerance_mps'}
    if not isinstance(profile, dict) or set(profile) != fields:
        raise ValueError('motion association needs four explicit bounded fields')
    for key in ('maximum_age_ticks', 'maximum_sample_gap_ticks'):
        if type(profile[key]) is not int or not 1 <= profile[key] <= 12:
            raise ValueError('motion association age/gap must be integers 1..12')
    for key, maximum in (('position_tolerance_m', 5.), ('velocity_tolerance_mps', 2.)):
        value = profile[key]
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError('motion association tolerance is invalid')
    return dict(profile)


class PublicContactAliases:
    def __init__(self, motion_profile=None):
        self.parents = {}
        self.order = {}
        self.samples = {}
        self.associations = []
        self.motion_profile = (None if motion_profile is None
                               else validate_motion_association(motion_profile))
        self.motion_samples = {}
        self.source_observers = {}

    def key(self, handle):
        if handle not in self.parents:
            self.order[handle] = len(self.order)
            self.parents[handle] = handle
        root = handle
        while self.parents[root] != root:
            root = self.parents[root]
        while self.parents[handle] != handle:
            parent = self.parents[handle]
            self.parents[handle] = root
            handle = parent
        return root

    def _join(self, first, second, *, when=None, method=None, error_m=None):
        a, b = self.key(first), self.key(second)
        if a != b:
            if self.motion_profile is not None:
                owners = {}
                for handle, observer in self.source_observers.items():
                    if self.key(handle) not in {a, b}: continue
                    if observer in owners and owners[observer] != handle:
                        return  # One source has already distinguished these tracks.
                    owners[observer] = handle
            self.associations.append(dict(first=first, second=second, observed_tick=when,
                                          method=method, error_m=error_m))
            older, newer = sorted((a, b), key=self.order.__getitem__)
            self.parents[newer] = older

    def observe(self, contacts, tick):
        # Retain a bounded measurement window so a delayed report can meet an
        # earlier same-time observation. Associations persist for this episode.
        self.samples = {t: rows for t, rows in self.samples.items()
                        if 0 <= tick-t <= 30}
        for c in sorted(contacts, key=lambda c: (
                c.get('observed_tick', tick-c['age_ticks']), c['contact_id'])):
            handle = c['contact_id']
            self.key(handle)
            when = c.get('observed_tick', tick-c['age_ticks'])
            if not 0 <= tick-when <= 30:
                continue
            observer = c.get('observer_entity_id')
            if self.motion_profile is not None and observer:
                self.source_observers[handle] = observer
            point = tuple(c['estimated_position_m'])
            rows = self.samples.setdefault(when, {})
            if observer:
                for other, (source, position, domain) in rows.items():
                    if source and source != observer and domain == (point[2] > 50) and math.dist(point, position) <= 1.:
                        self._join(handle, other, when=when, method='equal_time_position', error_m=math.dist(point, position))
            rows[handle] = (observer, point, point[2] > 50)
            if self.motion_profile is not None and observer:
                history = self.motion_samples.setdefault(handle, {})
                history[when] = (observer, point)
        if self.motion_profile is not None:
            self._associate_motion(tick)

    def _associate_motion(self, tick):
        """Mutual unique motion hypotheses, never truth or fire authorization.

        Two measurements are required from each source. The residual gate never
        grows with contact age. Tracks known separately at any retained observer
        cannot join, and ambiguous root-to-root matches remain separate.
        """
        config = self.motion_profile
        self.motion_samples = {handle: {when: value for when, value in history.items()
                                       if 0 <= tick-when <= 30}
                               for handle, history in self.motion_samples.items()}
        self.motion_samples = {k: v for k, v in self.motion_samples.items() if v}
        sources, estimates = {}, {}
        for handle, history in sorted(self.motion_samples.items()):
            root = self.key(handle)
            latest = max(history)
            observer = history[latest][0]
            sources.setdefault(root, {}).setdefault(observer, set()).add(handle)
            if len(history) < 2 or tick-latest > config['maximum_age_ticks']:
                continue
            previous = sorted(history)[-2]
            if not 1 <= latest-previous <= config['maximum_sample_gap_ticks']:
                continue
            point, before = history[latest][1], history[previous][1]
            velocity = tuple((point[a]-before[a])/(latest-previous) for a in range(3))
            if math.dist(velocity, (0., 0., 0.)) > 100.:
                continue
            estimates[handle] = (root, observer, latest, point, velocity)
        edges = {}
        handles = sorted(estimates)
        for index, first in enumerate(handles):
            a, source_a, when_a, point_a, velocity_a = estimates[first]
            for second in handles[index+1:]:
                b, source_b, when_b, point_b, velocity_b = estimates[second]
                if (a == b or source_a == source_b or set(sources[a]) & set(sources[b])
                        or (point_a[2] > 50) != (point_b[2] > 50)
                        or abs(when_a-when_b) > config['maximum_sample_gap_ticks']
                        or math.dist(velocity_a, velocity_b) > config['velocity_tolerance_mps']):
                    continue
                common = max(when_a, when_b)
                predicted_a = tuple(point_a[n]+velocity_a[n]*(common-when_a) for n in range(3))
                predicted_b = tuple(point_b[n]+velocity_b[n]*(common-when_b) for n in range(3))
                error = math.dist(predicted_a, predicted_b)
                if error <= config['position_tolerance_m']:
                    key = tuple(sorted((a, b), key=self.order.__getitem__))
                    value = (error, first, second, common)
                    if key not in edges or value < edges[key]: edges[key] = value
        neighbors = {}
        for a, b in edges:
            neighbors.setdefault(a, set()).add(b)
            neighbors.setdefault(b, set()).add(a)
        for (a, b), (error, first, second, common) in sorted(edges.items()):
            if len(neighbors[a]) == len(neighbors[b]) == 1:
                self._join(first, second, when=common,
                           method='mutual_unique_public_motion_hypothesis', error_m=error)
