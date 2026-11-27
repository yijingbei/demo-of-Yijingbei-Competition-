"""Finite joint altitude optimization using owned and public aggregate facts.

The height penalty is an exposure proxy, not a native detection probability.
Horizontal LLM goals, firing constraints and game physics are untouched.
"""
import math

from .state import live_units, platform_air


def optimize_flight_levels(brief, obs):
    aircraft = [u for u in live_units(brief, obs) if platform_air(brief, u['entity_id'])]
    references = brief.get('defence', {}).get('aircraft_flight_levels_m', {})
    aircraft.sort(key=lambda u: (references.get(u['entity_id'], u['position_m'][2]), u['entity_id']))
    ground = max([0., *[r['elevation_m'] for r in brief.get('visibility_contract', {}).get('terrain_regions', [])]])
    floor, ceiling, separation = ground + 20., 3000., 20.
    if len(aircraft) > 8 or floor + separation * max(0, len(aircraft)-1) > ceiling:
        raise ValueError('flight level model has no declared feasible height envelope')
    reference = {u['entity_id']: max(floor, min(ceiling, references.get(u['entity_id'], u['position_m'][2]))) for u in aircraft}
    air_threats = max(0, brief.get('planned_threat_counts', {}).get('air', 0))
    # A surface-only task gets exactly the original published flight plan.
    if not air_threats:
        return reference, {'model': 'joint-altitude-exposure-proxy@1', 'status': 'REFERENCE',
                           'exposure_weight': 0., 'levels': reference}
    if not aircraft:
        return {}, {'model': 'joint-altitude-exposure-proxy@1', 'status': 'OPTIMAL', 'levels': {}}
    weight = air_threats / (air_threats + len(aircraft))
    low_anchor = ground + 30.
    grid = sorted({*reference.values(), *[min(ceiling, low_anchor + separation*i) for i in range(len(aircraft))]})

    def loss(uid, height):
        return (weight * ((height-low_anchor)/100.)**2
                + .02 * ((height-reference[uid])/100.)**2)

    # Dynamic programming over ordered flight levels; no Cartesian explosion.
    previous = {None: (0., ())}
    for unit in aircraft:
        uid = unit['entity_id']
        current = {}
        for height in grid:
            if not floor <= height <= ceiling:
                continue
            candidates = [(cost + loss(uid, height), levels+(height,))
                          for prior, (cost, levels) in previous.items()
                          if prior is None or height-prior >= separation-1e-9]
            if candidates:
                current[height] = min(candidates)
        previous = current
    if not previous:
        raise ValueError('flight level discretization is infeasible')
    value, levels = min(previous.values())
    commands = {u['entity_id']: z for u, z in zip(aircraft, levels)}
    # Independent command-space validation; no enemy state is consulted.
    if (any(not math.isfinite(z) or not floor <= z <= ceiling for z in levels)
            or any(b-a < separation-1e-9 for a, b in zip(levels, levels[1:]))):
        raise RuntimeError('optimized flight levels failed public bounds')
    return commands, {'model': 'joint-altitude-exposure-proxy@1', 'status': 'OPTIMAL',
                      'objective': value, 'exposure_weight': weight, 'candidate_heights_m': grid,
                      'terrain_floor_m': floor, 'minimum_separation_m': separation,
                      'levels': commands, 'probability_calibrated': False}
