"""Public weather declarations and read-only native weather projection.

These helpers never apply weather or touch identification evidence. The native
typed weather_change events own capability modifiers for every faction.
"""
from collections.abc import Mapping

FIELDS = ('weather', 'visibility_scale', 'sea_state', 'sensor_range_multiplier',
          'sensor_detection_probability_multiplier')


def validate_weather_schedule(brief, resolved, catalog):
    schedule = brief.get('weather_schedule')
    if schedule is None:
        return
    if not schedule or schedule[0]['tick'] != 0:
        raise ValueError('public weather schedule must start at tick zero')
    previous = -1
    declared = []
    for phase in schedule:
        tick = phase['tick']
        if type(tick) is not int or not previous < tick < resolved.world.duration_ticks:
            raise ValueError('weather phase ticks must be increasing and inside the world budget')
        previous = tick
        content = catalog.resolve('environments', phase['environment_ref']).content
        if any(phase.get(key) != content.get(key) for key in FIELDS):
            raise ValueError('public weather differs from its native environment resource')
        declared.append((tick, phase['environment_ref']))
    events = sorted((e.trigger.tick, e.payload.values['environment_binding'].exact_ref)
                    for e in resolved.events if e.event_type == 'weather_change')
    if declared != events:
        raise ValueError('public weather schedule differs from native event times')


def weather_view(state, schedule):
    """Project only the published weather scalars, never transport queues."""
    current = state.get('current', {})
    binding = current.get('environment_binding', {})
    if hasattr(binding, 'model_dump'):
        binding = binding.model_dump(mode='json')
    if not isinstance(binding, Mapping):
        raise ValueError('native weather binding is missing')
    reference = current.get('environment_ref') or binding.get('exact_ref')
    content = binding.get('normalized_content', binding.get('content', {}))
    phase = next((p for p in reversed(schedule) if p['environment_ref'] == reference
                  and p['tick'] <= state.get('active_from_tick', 0)), None)
    if phase is None:
        raise ValueError('active native weather is not in the public schedule')
    return dict(environment_ref=reference, active_from_tick=state['active_from_tick'],
                label=phase['label'], **{key:content[key] for key in FIELDS})
