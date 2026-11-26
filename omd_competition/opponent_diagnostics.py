"""Organizer-only S-series configuration checks and streaming decision evidence."""
from collections import Counter
import hashlib
import json
import math

import yaml

from .contact_time_contract import validated_contact
from plugins.html_replay.record_io import RecordWriter


IGNORED_PARAMETERS = {
    'diversion': {'lead_ticks'},
    'lure_withdraw': {'probe_ticks'},
    'harass_cycle': {'run_in_range_m'},
    'bait_pull': {'dash_ticks', 'pull_ticks'},
}
SUPPORTED = {'diversion', 'press', 'press_late', 'lure_withdraw', 'lure_pursuit', 'opening_attack', 'harass_cycle', 'bait_pull'}
SPEED_FIELDS = {'withdraw_speed', 'dash_speed', 'pull_speed'}
PARAMETERS = {
    'press': 'speed_press',
    'diversion': 'lead_point withdraw_point speed_advance speed_withdraw speed_press engaged_range_m lead_radius_m withdraw_ticks detection_trigger_fraction',
    'press_late': 'staging staging_radius_m speed_stage commit_on_role commit_on_role_phase commit_tick commit_on_feint_engaged speed_press commit_on_lure_pursuit commit_on_defender_opening hold_position_until_commit precommit_shots',
    'opening_attack': 'opening_role assault_role speed_advance speed_draw speed_slow_draw speed_press keep_out_m follow_band_m emergency_range_m reaction_speed_mps reaction_displacement_m departure_distance_m opening_east_distance_m guard_radius_m minimum_departing_defenders maximum_observed_guards prefer_mobile_targets precommit_shots',
    'lure_pursuit': 'probe_point retreat_point speed_probe withdraw_speed slow_withdraw_speed keep_out_m follow_band_m emergency_range_m detection_trigger_fraction reaction_speed_mps reaction_displacement_m pursuit_displacement_m pursuit_north_m guard_radius_m minimum_pursuers maximum_observed_guards reengage_on_role speed_press prefer_mobile_targets precommit_shots',
    'lure_withdraw': 'probe_point retreat_point speed_probe withdraw_speed speed_evade keep_out_m detect_proxy_m probe_radius_m turn_ticks max_withdraw_ticks reapproach detection_trigger_fraction',
    'harass_cycle': 'staging staging_radius_m speed_in speed_out speed_press fire_ticks retreat_range_m retreat_ticks max_cycles detection_range_margin_m final_press_tick speed_stage',
    'bait_pull': 'bait_point dash_speed pull_point pull_speed pull_trigger_m bait_radius_m detection_trigger_fraction',
}


def inspect_configuration(root, row):
    """Read declared resources only; no live world/referee facts enter policy."""
    folder = root / row['package']
    plan = json.loads((folder/'opponent.json').read_bytes())
    scene = yaml.safe_load((folder/'scenario.yaml').read_bytes())['scenario']
    resources = yaml.safe_load((root/'scenarios/catalog'/(row['catalog']+'.yaml')).read_bytes())['resources']
    dynamics = {r['id']+'@'+r['version']: r['content'] for r in resources
                if r['resource_type'] == 'dynamics'}
    entities = scene['entities'] + [e['payload']['entity'] for e in scene.get('events', [])
                                    if e['event_type'] == 'spawn']
    roles = plan.get('attack', {}).get('tactics', {}).get('roles', [])
    errors, warnings, ignored, limits, assigned = [], [], [], {}, {}
    altitude_limits = {}
    names = [r['name'] for r in roles]
    if len(names) != len(set(names)):
        errors.append('duplicate role names')
    for role in roles:
        behavior, params = role.get('behavior'), role.get('params', {})
        if behavior not in SUPPORTED:
            errors.append(f"unsupported S-series behavior: {behavior}")
        for key, value in params.items():
            allowed = set(PARAMETERS.get(behavior, '').split()) | {'start_tick', 'defensive_fire', 'commit_on_role_loss'} | IGNORED_PARAMETERS.get(behavior, set())
            if key not in allowed:
                errors.append(f"{role['name']}.{key}: unknown parameter")
            if key in IGNORED_PARAMETERS.get(behavior, set()):
                ignored.append(dict(role=role['name'], field=key, value=value,
                                    status='ignored_by_current_behavior'))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(value) or value < 0:
                    errors.append(f"{role['name']}.{key}: expected finite nonnegative value")
            if key.startswith('speed_') or key in SPEED_FIELDS or key.endswith(('_ticks', '_tick', '_m', '_mps')) or key in {'max_cycles', 'detection_trigger_fraction'}:
                if type(value) not in (int, float):
                    errors.append(f"{role['name']}.{key}: expected number")
            if key.endswith('_point') or key == 'staging':
                if (not isinstance(value, (list, tuple)) or len(value) != 2
                        or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
                    errors.append(f"{role['name']}.{key}: expected finite 2-D point")
        pair = params.get('commit_on_role_phase', {})
        for key in ('precommit_shots', 'minimum_pursuers', 'maximum_observed_guards', 'minimum_departing_defenders'):
            if key in params and (type(params[key]) is not int or params[key] < (1 if key in {'minimum_pursuers','minimum_departing_defenders'} else 0)):
                errors.append(f"{role['name']}.{key}: invalid count")
        if params.get('commit_on_role_loss') and params['commit_on_role_loss'] not in names:
            errors.append(f"{role['name']}: unknown loss trigger role")
        if 'defensive_fire' in params and type(params['defensive_fire']) is not bool:
            errors.append(f"{role['name']}.defensive_fire: expected boolean")
        for key in ('commit_on_lure_pursuit', 'reengage_on_role', 'opening_role', 'assault_role', 'commit_on_defender_opening'):
            if key in params and params[key] not in names:
                errors.append(f"{role['name']}.{key}: unknown role")
        if params.get('commit_on_lure_pursuit'):
            referenced = next((r for r in roles if r['name']==params['commit_on_lure_pursuit']), {})
            if referenced.get('behavior') != 'lure_pursuit':
                errors.append(f"{role['name']}: pursuit trigger requires lure_pursuit role")
        for key in ('opening_role','commit_on_defender_opening'):
            if params.get(key):
                referenced = next((r for r in roles if r['name']==params[key]), {})
                if referenced.get('behavior')!='opening_attack' or referenced.get('params',{}).get('opening_role',referenced.get('name'))!=params[key]:
                    errors.append(f"{role['name']}.{key}: requires canonical opening_attack role")
        if 'hold_position_until_commit' in params and type(params['hold_position_until_commit']) is not bool:
            errors.append(f"{role['name']}.hold_position_until_commit: expected boolean")
        if behavior=='opening_attack':
            assault=next((r for r in roles if r['name']==params.get('assault_role')), {})
            if (assault.get('behavior')!='press_late' or assault.get('params',{}).get('commit_on_defender_opening')!=params.get('opening_role')):
                errors.append(f"{role['name']}: assault role must reference the same defender opening")
        if pair and pair.get('role') not in names:
            errors.append(f"{role['name']}: unknown commit role")
    for e in entities:
        if e['faction_id'] != plan['attack']['faction_id']:
            continue
        matches = [r for r in roles if set(r.get('match_tags', ())) & set(e['tags'])]
        if len(matches) != 1:
            errors.append(f"{e['id']}: expected exactly one role, found {len(matches)}")
            continue
        role = matches[0]
        assigned[e['id']] = role['name']
        target = next((t[7:] for t in e['tags'] if t.startswith('target.')), None)
        if target not in plan['facility_positions']:
            errors.append(f"{e['id']}: missing assigned facility")
        model = dynamics.get(e['dynamics_ref'], {})
        if 'uav' in e['tags']:
            minimum = model.get('min_altitude_m', 0.0)
            maximum = model.get('max_altitude_m', 3000.0)
            if (any(type(value) not in (int, float) or not math.isfinite(value)
                    for value in (minimum, maximum)) or minimum > maximum):
                errors.append(f"{e['id']}: invalid native altitude limits")
            else:
                altitude_limits[e['id']] = (minimum, maximum)
        limit = model.get('max_speed_mps')
        if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
            errors.append(f"{e['id']}: missing positive native speed limit")
            continue
        limits[e['id']] = limit
        for key, value in role.get('params', {}).items():
            if (key.startswith('speed_') or key in SPEED_FIELDS) and isinstance(value, (int, float)):
                if value > limit:
                    warnings.append(dict(entity=e['id'], role=role['name'], field=key,
                                         requested=value, maximum=limit,
                                         handling='clamp_final_navigation_to_declared_native_limit'))
    return dict(schema_version='competition-opponent-configuration@1', scenario=row['id'],
                ok=not errors, errors=errors, warnings=warnings, ignored_parameters=ignored,
                entity_speed_limits=limits, entity_altitude_limits=altitude_limits, entity_roles=assigned,
                facility_positions=plan['facility_positions'],
                source_sha256={name: hashlib.sha256((folder/name).read_bytes()).hexdigest()
                               for name in ('scenario.yaml', 'opponent.json')})


class OpponentIntentRecorder:
    """Append evidence to gzip JSONL; never expose this recorder to participants."""
    def __init__(self, output, binding, configuration):
        self.writer = RecordWriter(output/'opponent_intent.jsonl', 'gzip')
        self.counts = Counter()
        self.action_owners = {}
        self.closed = False
        self.write('header', schema_version='competition-opponent-intent@1',
                   visibility='organizer_only', binding=binding, configuration=configuration)

    def write(self, event_type, **payload):
        self.writer.write(json.dumps(dict(type=event_type, **payload),
                                     ensure_ascii=False, allow_nan=False)+'\n')
        self.counts[event_type] += 1

    def transition(self, record):
        self.write('transition', **record)

    def decision(self, policy, obs, batch, queue_receipt):
        commands = {c['entity_id']: c for c in (batch or {}).get('persistent_commands', [])}
        fires = {}
        for action in (batch or {}).get('discrete_actions', []):
            fires.setdefault(action['entity_id'], []).append(action)
        for action in [*(batch or {}).get('persistent_commands', []),
                       *(batch or {}).get('discrete_actions', [])]:
            self.action_owners[action.get('action_id', action.get('command_id'))] = action['entity_id']
        evidence = []
        for c in obs.get('organic_contacts', []):
            evidence.append(dict(contact_id=c.get('contact_id'),
                                 estimated_contact_kind=c.get('estimated_contact_kind'),
                                 estimated_position_m=c.get('estimated_position_m'),
                                 observed_tick=c.get('observed_tick'), age_ticks=c.get('age_ticks'),
                                 expiry_tick=c.get('expiry_tick'),
                                 tactic_valid=c.get('estimated_contact_kind') != 'flight' and validated_contact(c, obs['tick'], policy.stale_ticks) is not None,
                                 fire_valid=c.get('estimated_contact_kind') != 'flight' and validated_contact(c, obs['tick'], 2) is not None,
                                 excluded_flight=c.get('estimated_contact_kind') == 'flight'))
        for own in obs.get('own_entities', []):
            entity = own['entity_id']
            if entity not in obs['controlled_entity_ids']:
                continue
            command = commands.get(entity)
            shared = [dict(source_entity=key[0], contact_id=key[1],
                           observed_tick=t, age_ticks=obs['tick']-t,
                           estimated_position_m=pos)
                      for key, (t, pos) in policy.bb_contacts.items()
                      if 0 <= obs['tick']-t <= policy.stale_ticks]
            self.write('decision', tick=obs['tick'], controller_slot=obs['controller_slot_id'],
                       entity=entity, role=policy.role_by_entity.get(entity),
                       phase=policy.phase.get(entity), phase_since=policy.phase_tick.get(entity),
                       position_m=own['position_m'], velocity_mps=own.get('velocity_mps'),
                       tactical_goal=policy.goal.get(entity), tactical_speed_mps=policy.speed.get(entity),
                       native_speed_limit_mps=policy.speed_limits.get(entity),
                       fire_hold=entity in policy.fire_hold,
                       target_index=policy.target_index.get(entity),
                       escape=entity in policy.escape_mode,
                       contacts=evidence, tactical_blackboard=shared,
                       weapon_decision=policy.decision_details.get(entity),
                       pursuit_signals=policy.pursuit_signals,
                       navigation_command=command, fire_requests=fires.get(entity, []),
                       queue_receipt=queue_receipt)

    def execution(self, tick, child_receipts, combat_receipts):
        # Keep fire ownership until the combat receipt, even if a child receipt
        # for the same action is present. Navigation needs only its child receipt.
        for receipt in child_receipts:
            identifier = receipt.get('child_id')
            if identifier in self.action_owners:
                self.write('execution', tick=tick, entity=self.action_owners[identifier],
                           request_id=identifier, kind='command', receipt=receipt)
                if identifier.startswith('nav.'):
                    self.action_owners.pop(identifier, None)
        for receipt in combat_receipts:
            identifier = receipt['request_id']
            entity = self.action_owners.pop(identifier, None)
            if entity is not None:
                self.write('execution', tick=tick, entity=entity, request_id=identifier,
                           kind='combat', receipt=receipt)

    def finish(self, status, final_tick):
        if not self.closed:
            try:
                self.write('footer', status=status, final_tick=final_tick,
                           unmatched_action_ids=sorted(self.action_owners))
            finally:
                self.writer.close()
                self.closed = True
        return dict(**self.writer.metadata(), schema_version='competition-opponent-intent@1',
                    visibility='organizer_only', rows=dict(self.counts))

