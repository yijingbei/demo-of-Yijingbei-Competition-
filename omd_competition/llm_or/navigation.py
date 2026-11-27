"""Bounded game motion optimization with speed-dependent surface yaw.

The kinematic prediction is an explicit approximation. No native worker, GPU,
external service, rule-policy executor or referee state is loaded here.
"""
import math


def angle_delta(target, current):
    return (target - current + 180.) % 360. - 180.


def predict(own, heading, speed, maximum, air, horizon=20):
    position = list(own['position_m'][:2])
    bearing = own['heading_deg']
    velocity = own.get('velocity_mps', (0., 0., 0.))
    forward = math.hypot(*velocity[:2])
    acceleration, yaw = (3., 18.) if air else (.5, 3.)
    path = [tuple(position)]
    traveled = 0.
    for _ in range(horizon):
        forward += max(-acceleration, min(acceleration, speed - forward))
        # A stationary surface hull has no predicted steering authority.
        rate = yaw if air else yaw * min(1., forward / max(.01, maximum * .3))
        turn = max(-rate, min(rate, angle_delta(heading, bearing)))
        middle = math.radians(bearing + turn / 2.)
        position = [position[0] + forward * math.sin(middle), position[1] + forward * math.cos(middle)]
        bearing = (bearing + turn) % 360.
        path.append(tuple(position))
        traveled += forward
    return path, bearing, traveled


def optimize_motion(own, target, maximum, air, router, physical_maximum=None):
    physical_maximum = maximum if physical_maximum is None else physical_maximum
    pos = own['position_m']
    distance = math.dist(pos[:2], target[:2])
    heading = (math.degrees(math.atan2(target[0] - pos[0], target[1] - pos[1])) % 360.
               if distance > 1. else own['heading_deg'])
    speeds = sorted({0., maximum * .1, maximum * .25, maximum * .5, maximum,
                     min(maximum, distance / 20.)}) if distance > 1. else [0.]
    options = []
    for speed in speeds:
        path, final_heading, traveled = predict(own, heading, speed, physical_maximum, air)
        if not air and any(not router.clear_segment(a, b) for a, b in zip(path, path[1:])):
            continue
        end = path[-1]
        residual = math.dist(end, target[:2])
        goal_heading = math.degrees(math.atan2(target[0] - end[0], target[1] - end[1])) % 360.
        # Approximate remaining travel and turning time, not position alone.
        turning = abs(angle_delta(goal_heading, final_heading)) / (18. if air else 3.) if residual > 1. else 0.
        objective = residual / max(.01, physical_maximum) + turning + .02 * traveled / max(.01, physical_maximum)
        options.append((objective, speed, end, final_heading))
    if not options:
        raise RuntimeError('no feasible trajectory in the approximate game motion model')
    objective, speed, end, final_heading = min(options)
    if not (math.isfinite(heading) and 0 <= heading < 360 and 0 <= speed <= maximum):
        raise RuntimeError('optimized motion failed command bounds')
    return heading, speed, {'model': 'speed-coupled-yaw@2', 'horizon_seconds': 20,
        'objective': objective, 'evaluated_controls': len(speeds), 'feasible_controls': len(options),
        'predicted_end': end, 'predicted_heading': final_heading, 'needs_motion': distance > 1.}
