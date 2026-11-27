"""Public-observation association; uncertain links share reservations only.

Neither handle names nor referee identities are decoded. Ambiguous matches and
tracks already distinguished by one observer remain separate.
"""
import math

from omd_competition.contact_aliases import PublicContactAliases
from .state import contact_domain, contacts


class DeferredAliases(PublicContactAliases):
    """Collect base-helper hypotheses; never merge greedily during ingestion."""
    def __init__(self, profile):
        super().__init__(profile)
        self.proposals = []

    def _join(self, first, second, **evidence):
        self.proposals.append((first, second, evidence))

    def commit(self, first, second, **evidence):
        super()._join(first, second, **evidence)


class PublicTracks:
    def __init__(self):
        self.aliases = DeferredAliases({'maximum_age_ticks': 8,
            'maximum_sample_gap_ticks': 8, 'position_tolerance_m': 5.,
            'velocity_tolerance_mps': 2.})
        self.domains, self.current, self.provisional = {}, [], []
        self.budget_parents = {}
        self.last_tick = -1

    def key(self, handle):
        return self.aliases.key(handle)

    def budget_key(self, handle):
        root = self.key(handle)
        while root in self.budget_parents:
            root = self.budget_parents[root]
        return root

    @staticmethod
    def fit(history):
        # Several time-separated observations avoid noisy one-step derivatives.
        times = sorted(history)[-8:]
        if len(times) < 3 or times[-1] - times[0] < 4:
            return None
        mean_t = sum(times) / len(times)
        mean_p = [sum(history[t][1][i] for t in times) / len(times) for i in range(3)]
        denominator = sum((t - mean_t) ** 2 for t in times)
        velocity = [sum((t - mean_t) * (history[t][1][i] - mean_p[i]) for t in times) / denominator for i in range(3)]
        if math.dist(velocity, (0., 0., 0.)) > 100.:
            return None
        return mean_t, mean_p, velocity

    def observe(self, obs):
        tick = obs['tick']
        if tick == self.last_tick:
            return
        if tick < self.last_tick:
            raise ValueError('track observations moved backwards')
        self.last_tick = tick
        self.current = contacts(obs)
        for row in self.current:
            self.domains[row['contact_id']] = contact_domain(row)
        self.aliases.proposals = []
        self.aliases.observe(self.current, tick)
        estimates, sources = {}, {}
        for handle, observer in self.aliases.source_observers.items():
            sources.setdefault(self.key(handle), set()).add(observer)
        for handle, history in self.aliases.motion_samples.items():
            if not history or tick - max(history) > 8:
                continue
            root = self.key(handle)
            source, point = history[max(history)]
            sources.setdefault(root, set()).add(source)
            estimates[handle] = (root, source, max(history), point, self.fit(history))
        edges = {}
        for first, second, evidence in self.aliases.proposals:
            if evidence.get('method') != 'equal_time_position' or first not in estimates or second not in estimates:
                continue
            a, b = estimates[first], estimates[second]
            ra, rb = a[0], b[0]
            if ra == rb or sources[ra] & sources[rb] or self.domains.get(first) != self.domains.get(second):
                continue
            if a[4] is not None or b[4] is not None:
                continue  # Fitted motion must pass the current-time residual/velocity gates below.
            key = tuple(sorted((ra, rb), key=self.aliases.order.get))
            edges[key] = (evidence['error_m'], first, second, False, max(a[2], b[2]))
        items = sorted(estimates.items())
        for i, (first, a) in enumerate(items):
            for second, b in items[i + 1:]:
                ra, sa, ta, pa, fa = a
                rb, sb, tb, pb, fb = b
                if (ra == rb or sa == sb or sources[ra] & sources[rb]
                        or self.domains.get(first) != self.domains.get(second)
                        or abs(ta - tb) > 8 or (fa is None and fb is None)):
                    continue
                if fa and fb:
                    when = max(ta, tb)
                    qa = [fa[1][k] + fa[2][k] * (when - fa[0]) for k in range(3)]
                    qb = [fb[1][k] + fb[2][k] * (when - fb[0]) for k in range(3)]
                    if math.dist(fa[2], fb[2]) > 2.:
                        continue
                else:
                    fit, when, point = (fa, tb, pb) if fa else (fb, ta, pa)
                    qa = [fit[1][k] + fit[2][k] * (when - fit[0]) for k in range(3)]
                    qb = point
                error = math.dist(qa, qb)
                if error <= 5.:
                    key = tuple(sorted((ra, rb), key=self.aliases.order.get))
                    value = (error, first, second, bool(fa and fb), max(ta, tb))
                    if key not in edges or value[0] < edges[key][0]:
                        edges[key] = value
        neighbors = {}
        for a, b in edges:
            neighbors.setdefault(a, set()).add(b)
            neighbors.setdefault(b, set()).add(a)
        self.provisional, self.budget_parents = [], {}
        for (a, b), (error, first, second, confirmed, when) in sorted(edges.items()):
            if len(neighbors[a]) != 1 or len(neighbors[b]) != 1:
                continue
            if confirmed:
                self.aliases.commit(first, second, when=when,
                                   method='mutual_unique_least_squares_motion', error_m=error)
            if self.key(first) != self.key(second):
                old, new = sorted((self.key(first), self.key(second)), key=self.aliases.order.get)
                self.budget_parents[new] = old
                self.provisional.append({'first': first, 'second': second, 'residual_m': error,
                                         'scope': 'reservation_only'})

    def estimate(self, handle, tick):
        root = self.key(handle)
        choices = []
        for other, history in self.aliases.motion_samples.items():
            if self.key(other) != root or not history:
                continue
            last = max(history)
            if tick-last > 8:
                continue
            fit = self.fit(history)
            position = history[last][1]
            velocity = fit[2] if fit else [0., 0., 0.]
            choices.append((last, bool(fit), position, velocity))
        if not choices:
            return None
        last, fitted, position, velocity = max(choices, key=lambda x:(x[0],x[1]))
        return {'position_m': [position[i]+velocity[i]*(tick-last) for i in range(3)],
                'velocity_mps': list(velocity), 'age_ticks': tick-last, 'motion_fitted': fitted}

    def snapshot(self):
        groups = {}
        for row in self.current:
            handle = row['contact_id']
            groups.setdefault(self.budget_key(handle), []).append(handle)
        return {'groups': list(groups.values()), 'reservation_only_links': list(self.provisional)}
