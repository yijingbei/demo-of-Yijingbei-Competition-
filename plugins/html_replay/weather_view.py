"""Public global weather only; no entity, queue or referee state projection."""
from copy import deepcopy

KEYS = ('tick', 'environment_ref', 'label', 'weather', 'visibility_scale', 'sea_state',
        'sensor_range_multiplier', 'sensor_detection_probability_multiplier', 'active_from_tick')


def public_weather(value):
    return {key:deepcopy(value[key]) for key in KEYS if key in value}


def weather_episode(brief, recording):
    if 'weather_schedule' not in brief:
        return None
    timeline = []
    for frame in (recording or {}).get('frames', []):
        if 'weather' not in frame:
            continue
        current = public_weather(frame['weather'])
        if not timeline or {k:v for k,v in timeline[-1].items() if k!='tick'} != current:
            timeline.append(dict(tick=frame['tick'], **current))
    return dict(schedule=[public_weather(p) for p in brief['weather_schedule']], timeline=timeline)
