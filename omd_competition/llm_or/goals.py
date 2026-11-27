"""Strict, versioned task and engagement instructions, shared by all IE tasks."""
from dataclasses import asdict, dataclass, field
import math

from . import GOAL_SCHEMA
from .state import contact_domain, contacts, live_units, weapon_options

GOAL_TYPES = ('intercept', 'track', 'hold', 'reserve', 'waypoint', 'return', 'disengage')
ENGAGEMENT_MODES = ('cease_fire', 'allow', 'engage')


def number(value, name, minimum=0.):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError('invalid ' + name)
    return float(value)


@dataclass(frozen=True)
class Goal:
    unit_id: str
    goal_type: str
    engagement_mode: str
    valid_until_tick: int
    target_id: str | None = None
    position_m: tuple | None = None
    preferred_weapon_ref: str | None = None
    radius_m: float = 100.
    standoff_m: float = 1000.
    speed_fraction: float = 1.
    priority: float = 1.
    salvo_cap: int = 1
    reserve_rounds: dict = field(default_factory=dict)

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Plan:
    issued_tick: int
    goals: tuple

    def as_dict(self):
        return {'schema_version': GOAL_SCHEMA, 'issued_tick': self.issued_tick,
                'goals': [g.as_dict() for g in self.goals]}

    def by_unit(self):
        return {g.unit_id: g for g in self.goals}

    def valid(self, brief, obs):
        try:
            parse_plan(self.as_dict(), brief, obs)
            return True
        except ValueError:
            return False


def parse_plan(data, brief, obs):
    if type(data) is not dict or set(data) != {'schema_version', 'issued_tick', 'goals'}:
        raise ValueError('plan needs schema_version, issued_tick and goals only')
    if data['schema_version'] != GOAL_SCHEMA:
        raise ValueError('unsupported goal schema')
    issued = data['issued_tick']
    if type(issued) is not int or not 0 <= issued <= obs['tick']:
        raise ValueError('invalid issued_tick')
    if type(data['goals']) is not list:
        raise ValueError('goals must be a list')
    units = {u['entity_id']: u for u in live_units(brief, obs)}
    visible = {c['contact_id']: c for c in contacts(obs)}
    allowed = set(Goal.__dataclass_fields__)
    required = {'unit_id', 'goal_type', 'engagement_mode', 'valid_until_tick'}
    goals = {}
    for raw in data['goals']:
        if type(raw) is not dict or not required <= set(raw) or set(raw) - allowed:
            raise ValueError('unknown or missing goal fields')
        row = dict(raw)
        uid = row['unit_id']
        if type(uid) is not str or uid not in units or uid in goals:
            raise ValueError('duplicate or unauthorized goal unit: ' + str(uid))
        if row['goal_type'] not in GOAL_TYPES or row['engagement_mode'] not in ENGAGEMENT_MODES:
            raise ValueError('unknown goal or engagement mode')
        expiry = row['valid_until_tick']
        if type(expiry) is not int or not obs['tick'] < expiry or expiry <= issued:
            raise ValueError('goal expired or invalid deadline')
        options = weapon_options(brief, uid)
        refs = {w['weapon_ref'] for w in options}
        preferred = row.get('preferred_weapon_ref')
        if preferred is not None and (type(preferred) is not str or preferred not in refs):
            raise ValueError('unknown preferred weapon')
        target = row.get('target_id')
        needs_target = row['goal_type'] in {'intercept', 'track'} or row['engagement_mode'] == 'engage'
        if target is not None or needs_target:
            if type(target) is not str or target not in visible:
                raise ValueError('goal target is not a current authorized contact')
            if not any(contact_domain(visible[target]) in w['target_domains'] for w in options):
                raise ValueError('goal target has no compatible weapon')
        position = row.get('position_m')
        if position is None and row['goal_type'] in {'hold', 'reserve'}:
            position = units[uid]['position_m']
        if position is None and row['goal_type'] == 'return':
            position = [*brief['objective_m'][:2], units[uid]['position_m'][2]]
        if position is None and row['goal_type'] in {'waypoint', 'disengage'}:
            raise ValueError('waypoint/disengage needs an explicit position')
        if position is not None:
            if not isinstance(position, (list, tuple)) or len(position) not in (2, 3):
                raise ValueError('invalid position_m')
            position = tuple(number(v, 'position_m', -1e9) for v in position)
            if len(position) == 2:
                position += (units[uid]['position_m'][2],)
            row['position_m'] = position
        for key, default in [('radius_m', 100.), ('standoff_m', 1000.),
                             ('speed_fraction', 1.), ('priority', 1.)]:
            row[key] = number(row.get(key, default), key)
        if row['speed_fraction'] > 1.:
            raise ValueError('speed_fraction exceeds one')
        cap = row.get('salvo_cap', 1)
        if type(cap) is not int or not 1 <= cap <= 16:
            raise ValueError('invalid salvo_cap')
        reserves = row.get('reserve_rounds', {})
        if type(reserves) is not dict or any(k not in refs or type(v) is not int or v < 0
                                             for k, v in reserves.items()):
            raise ValueError('invalid per-weapon reserve_rounds')
        row['reserve_rounds'] = dict(reserves)
        goals[uid] = Goal(**row)
    if set(goals) != set(units):
        raise ValueError('plan must cover every live controlled combat unit exactly once')
    return Plan(issued, tuple(goals[k] for k in sorted(goals)))
