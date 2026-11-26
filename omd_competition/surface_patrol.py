"""Surface defence manoeuvres from owned positions and public coastlines only."""
import math

from .coastal_navigation import CoastalRouter


class SurfacePatrol:
    def __init__(self, brief):
        self.config = brief.get('defence', {}).get('surface_patrol', {})
        self.enabled = self.config.get('enabled', False)
        self.objective = brief['objective_m']
        self.router = CoastalRouter(brief.get('navigation_map', {}).get('land_regions', []))
        self.stations, self.paths, self.legs = {}, {}, {}

    def remember(self, own):
        self.stations.setdefault(own['entity_id'], list(own['position_m'][:2]))

    def standoff(self, policy):
        # Sensor tracking / hull safety governs manoeuvre, not missile reach.
        return min(policy['maximum_range_m'] * .7,
                   max(policy['minimum_range_m'] + 150.,
                       self.config.get('intercept_standoff_m', 900.)))

    def _path(self, own):
        key = own['entity_id']
        if key in self.paths:
            return self.paths[key]
        self.remember(own)
        station = self.stations[key]
        angle = math.atan2(station[1] - self.objective[1], station[0] - self.objective[0])
        radial = (math.cos(angle), math.sin(angle))
        tangent = (-radial[1], radial[0])
        radius = self.config.get('radius_m', 300.)
        # Search for a closed water-only loop near this ship's initial station.
        # Shifting seaward avoids routing a perimeter patrol through an island.
        for shift in (0., radius, 2 * radius):
            for scale in (1., .75, .5):
                center = [station[a] + shift * radial[a] for a in (0, 1)]
                points = [[center[a] + scale * radius * (
                    1.5 * math.cos(n * math.tau / 12) * tangent[a]
                    + math.sin(n * math.tau / 12) * radial[a]) for a in (0, 1)]
                    for n in range(12)]
                if not all(self.router.clear_segment(a, b)
                           for a, b in zip(points, points[1:] + points[:1])):
                    continue
                choices = [i for i, p in enumerate(points)
                           if self.router.clear_segment(own['position_m'], p)]
                if not choices:
                    continue
                self.paths[key] = points
                self.legs[key] = min(choices, key=lambda i: math.dist(station, points[i]))
                return points
        self.paths[key] = []
        return []

    def _navigate(self, own, goal, speed, arrival=40.):
        pos = own['position_m']
        route, _ = self.router.route(pos, goal)
        if not route or math.dist(pos[:2], goal) <= arrival:
            return {'heading_deg': own['heading_deg'], 'speed_mps': 0., 'altitude_m': pos[2]}
        point = route[0]
        heading = math.degrees(math.atan2(point[0] - pos[0], point[1] - pos[1])) % 360
        error = abs((heading - own['heading_deg'] + 180) % 360 - 180)
        speed = min(speed, math.dist(pos[:2], point) / 3.)
        # MMG hulls need steerage; they cannot pivot at zero speed like aircraft.
        if error > 35:
            speed = min(speed, 3.)
        return {'heading_deg': heading, 'speed_mps': speed, 'altitude_m': pos[2]}

    def patrol(self, own, policy):
        if not self.config.get('patrol_enabled', True):
            return {'heading_deg': own['heading_deg'], 'speed_mps': 0.,
                    'altitude_m': own['position_m'][2]}
        path = self._path(own)
        key = own['entity_id']
        if not path:
            return {'heading_deg': own['heading_deg'], 'speed_mps': 0.,
                    'altitude_m': own['position_m'][2]}
        index = self.legs[key]
        if math.dist(own['position_m'][:2], path[index]) <= self.config.get('arrival_radius_m', 100.):
            index = (index + 1) % len(path)
            self.legs[key] = index
        return self._navigate(own, path[index], min(policy['maximum_speed_mps'],
            self.config.get('speed_mps', 6.)), arrival=20.)

    def intercept(self, own, policy, target):
        self.remember(own)
        station = self.stations[own['entity_id']]
        pos = own['position_m']
        delta = [target[a] - pos[a] for a in (0, 1)]
        distance = math.hypot(*delta)
        standoff = self.standoff(policy)
        # A ship may hold a safe firing station; a nearby suicide boat instead
        # makes it back away. Neither decision grants fire authorization.
        goal = [target[a] - standoff * delta[a] / max(1., distance) for a in (0, 1)]
        excursion = math.dist(station, goal)
        leash = self.config.get('maximum_station_excursion_m', 2500.)
        if excursion > leash:
            goal = [station[a] + (goal[a] - station[a]) * leash / excursion for a in (0, 1)]
        return self._navigate(own, goal, policy['maximum_speed_mps'], arrival=80.)


