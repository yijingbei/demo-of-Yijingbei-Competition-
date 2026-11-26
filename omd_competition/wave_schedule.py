"""Normalize declared public wave windows without consulting deployment truth."""
from copy import deepcopy


def normalize_public_waves(brief):
    """Preserve explicit spawn_tick; missing values use the public window end.

    Valid existing schedules return the same object. Only compatibility cases
    are copied, so caller-owned declarations are never mutated.
    """
    waves = brief.get('planned_waves')
    if not isinstance(waves, list):
        return brief
    replacements = {}
    for index, wave in enumerate(waves):
        if not isinstance(wave, dict):
            raise ValueError(f'planned_waves[{index}] must be a mapping')
        if 'spawn_tick' in wave:
            if type(wave['spawn_tick']) is not int or wave['spawn_tick'] < 0:
                raise ValueError(f'planned_waves[{index}].spawn_tick must be a nonnegative integer')
            continue
        window = wave.get('tick_window')
        if (not isinstance(window, (list, tuple)) or len(window) != 2
                or any(type(tick) is not int for tick in window)
                or not 0 <= window[0] <= window[1]):
            raise ValueError(f'planned_waves[{index}] requires spawn_tick or an ordered nonnegative integer tick_window')
        replacements[index] = window[1]
    if not replacements:
        return brief
    result = deepcopy(brief)
    for index, tick in replacements.items():
        result['planned_waves'][index]['spawn_tick'] = tick
    return result
