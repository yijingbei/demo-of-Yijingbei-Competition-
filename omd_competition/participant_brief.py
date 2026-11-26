"""Participant reset contracts, separate from organizer deployment diagnostics.

IE uses an explicit recursive allowlist. Unknown fields never become public by
being added to an internal brief. Other tracks retain their existing contracts;
this projection is not an information-boundary review of those tracks.
"""
from copy import deepcopy
from dataclasses import dataclass
import math
from .wave_schedule import normalize_public_waves


_SCALAR = object()
_NUMBERS = object()


@dataclass(frozen=True)
class _KeyedMap:
    keys: str
    value: object


def _fields(names='', **nested):
    return {**{name: _SCALAR for name in names.split()}, **nested}


_AMMUNITION = frozenset({
    'ammunition.air-item.speed-trial@1.0.0',
    'ammunition.air-item.speed-trial@1.1.0',
    'ammunition.air-item.speed-trial@1.2.0',
    'ammunition.air-item.speed-trial@1.1.0',
    'ammunition.air-item.speed-trial@1.2.0',
    'ammunition.air-item.speed-trial@1.1.0',
    'ammunition.air-item.speed-trial@1.2.0',
    'ammunition.air-interceptor-munition@2.0.0',
    'ammunition.loitering-munition.competition-maritime@2.1.0',
    'ammunition.surface-missile@2.2.0',
})
_AMMO_COUNTS = _KeyedMap('ammunition', _SCALAR)
_DOMAIN_COUNTS = _fields('air surface')
_ENERGY_PROFILE = _fields('initial_energy idle_rate_per_second motion_rate_per_meter idle_budget_s motion_budget_m')
_WEAPON = _fields(
    'weapon_ref ammunition_ref minimum_range_m maximum_range_m cooldown_ticks '
    'maximum_speed_mps missile_cruise_speed_mps missile_max_flight_ticks '
    'nominal_hit_probability nominal_health_loss_per_hit salvo_rounds '
    'minimum_hits_to_disable_reference_target',
    target_domains=[_SCALAR], selector_tags=[_SCALAR])
_GEOMETRY = _fields('schema_version id geometry_type', coordinates_m=_NUMBERS)
_CARRIER = _fields('platform_ref dynamics_ref loadout_ref',
                   sensor_refs=[_SCALAR], ammunition=_AMMO_COUNTS)
_OPPONENT_WEAPON_CONTRACT = _fields(
    'schema_version scope counting_rule facility_reserve_rule retry_rule',
    parameters=[_fields(
        'carrier_type weapon_ref ammunition_ref initial_rounds_per_carrier '
        'maximum_confirmed_launches_per_carrier reserved_rounds_for_facilities retry_interval_ticks',
        target_domains=[_SCALAR])])
_FIRE_FEEDBACK = _fields('maximum_retries initial_backoff_ticks maximum_backoff_ticks '
                         'exhausted_backoff_ticks receipt_timeout_ticks')
_SURFACE_ARRIVAL = _fields(
    'capture_radius_m settled_radius_m release_radius_m capture_speed_mps settled_speed_mps '
    'settled_yaw_rate_deg_s stable_ticks steering_speed_mps braking_margin_m command_delay_ticks '
    'braking_drag_per_m speed_hysteresis_mps turn_tolerance_deg tracking_yaw_rate_deg_s '
    'yaw_lead_ticks heading_correction_deg linear_drag_per_s low_speed_damping_mps maximum_turning_speed_mps control_radius_m')
_SURFACE_COAST = _fields('schema_version model linear_drag_per_s angular_drag_per_s '
    'rest_speed_mps rest_yaw_rate_deg_s stable_seconds healthy_zero_thrust_below_mps')

_IE_VIEW = _fields(
    'scenario_id track difficulty control_mode duration_ticks name package_version '
    'score_formula adjudication_tick task task_revision status difficulty_rationale '
    'planned_decoy_count planned_feint_count sensor_revision pressure_revision parameter_revision',
    task_contract_revision=_SCALAR,
    surface_arrival=_SURFACE_ARRIVAL,
    surface_coast=_SURFACE_COAST,
    surface_safety=_fields('enabled model prediction_ticks clearance_m lookahead_m braking_drag_per_m '
        'settled_speed_mps braking_margin_m maximum_contact_age_ticks stuck_ticks recovery_ticks '
        'steerage_speed_mps surface_maximum_altitude_m'),
    task_success_criteria=[_fields('metric_id comparison value')],
    weather_schedule=[_fields('tick environment_ref label weather visibility_scale sea_state '
                             'sensor_range_multiplier sensor_detection_probability_multiplier')],
    contact_judgment_contract=_fields('schema_version observation_rule engagement_rule scoring_rule',
        fire_feedback_policy=_FIRE_FEEDBACK,
        reference_policy=_fields('strategy_family history_ticks navigation_max_age_ticks fire_max_age_ticks '
            'minimum_track_span_ticks minimum_progress_m minimum_closing_mps minimum_directionality '
            'scout_entity_id scout_speed_mps scout_altitude_m scout_standoff_m inspection_radius_m '
            'inspection_revisit_ticks coordinated_retry_ticks flight_margin_ticks'),
        blue_reference_policy=_fields('mode air_contact_policy weapon_domain_matching history_ticks navigation_max_age_ticks fire_max_age_ticks '
            'minimum_track_span_ticks objective_match_radius_m stationary_speed_mps',public_objectives_m=_NUMBERS,
            fire_feedback_policy=_FIRE_FEEDBACK,
            surface_self_defence=_fields('history_ticks minimum_track_span_ticks maximum_range_m urgent_range_m '
                'minimum_progress_m minimum_closing_mps minimum_directionality'))),
    navigation_contract=_fields('third_party_count slow_attacker_speed_mps surface_traffic_max_altitude_m '
        'buoy_transfer_observation_radius_m buoy_transfer_settled_speed_mps buoy_transfer_dwell_ticks '
        'buoy_braking_drag_reference_per_m buoy_braking_margin_m',
        attacker_phase_budget=_fields('transit_speed_mps transit_reference_distance_m staging_braking_radius_m '
            'staging_arrival_radius_m loiter_ticks_after_slow_arrival loiter_max_actual_speed_mps '
            'loiter_and_attack_speed_mps loiter_speed_mps attack_cruise_speed_mps live_decision_ticks maximum_missile_flight_ticks '
            'settlement_budget_ticks release_rule note')),
    objective_m=_NUMBERS,
    available_actions=[_SCALAR], protected_zone_ids=[_SCALAR],
    own_assets=[_fields('entity_id platform_ref domain', initial_position_m=_NUMBERS)],
    protected_facilities=[_fields('entity_id weight', position_m=_NUMBERS)],
    own_initial_ammunition=_KeyedMap('own', _AMMO_COUNTS),
    own_weapon_options=_KeyedMap('own', [_WEAPON]),
    own_weapon_policies=_KeyedMap('own', _WEAPON),
    facility_weights=_KeyedMap('own', _SCALAR),
    defence=_fields(
        'faction_id contact_confidence fresh_shared_air_contact_confidence maximum_contact_age_ticks intercept_speed_mps '
        'aircraft_deployment_note flight_level_note aircraft_regroup_anchor_entity_id '
        'aircraft_regroup_radius_m aircraft_intercept_standoff_fraction '
        'new_contact_fire_confirmation_ticks new_contact_urgent_bypass_seconds '
        'reserve_commit_after_last_wave_ticks maximum_air_fire_contact_age_ticks '
        'maximum_surface_inflight_rounds surface_inflight_grace_ticks '
        'air_fire_nearest_first air_retry_from_spare_reserve late_air_first_attempt_reserve_rounds',
        weapon_policies=[_WEAPON],
        aircraft_flight_levels_m=_KeyedMap('own', _SCALAR),
        aircraft_patrol=_fields('enabled patrol_enabled radius_m speed_mps arrival_radius_m pattern station_mode leg_length_m'),
        motion_contact_association=_fields(
            'maximum_age_ticks maximum_sample_gap_ticks position_tolerance_m velocity_tolerance_mps'),
        surface_patrol=_fields(
            'enabled patrol_enabled pattern radius_m speed_mps arrival_radius_m intercept_standoff_m '
            'maximum_station_excursion_m station_source contact_source navigation_contact_confidence '
            'fire_contact_confidence maximum_fire_contact_age_ticks'),
        multirole_surface_support=_fields(
            'enabled maximum_fire_contact_age_ticks fire_contact_confidence maximum_inflight_rounds_per_contact')),
    planned_threat_counts=_DOMAIN_COUNTS,
    # The participant guide explicitly publishes wave times and aggregate counts.
    # There are no individual identities, entry points or routes in this contract.
    planned_waves=[_fields('spawn_tick air surface decoy')],
    navigation_map=_fields('schema_version coordinate_frame description',
        land_constraint_domains=[_SCALAR], land_regions=[_GEOMETRY], protected_regions=[_GEOMETRY]),
    visibility_contract=_fields('schema_version fidelity sea_level_m note',
        terrain_regions=[_fields('id elevation_m', vertices_m=_NUMBERS)]),
    ingress_contract=_fields(
        'schema_version deployment_rule minimum_nominal_sensor_clearance_m meaning '
        'visibility reinforcement_validation geography_revision'),
    pressure_contract=_fields(
        'schema_version revision status coordination timing_allowance limitation '
        'live_deadline_tick common_reserve_ticks'),
    # attack_deployment_summary is deliberately absent: it describes realized
    # attack azimuths, rotations and vertical separation, rather than game rules.
    opponent_contract=_fields(
        'kind assigned_facility_priority '
        'uses_authorized_contacts_only navigation maximum_contact_age_ticks provenance '
        'faction_id controller_endpoints non_suicidal equipment feints maritime_loitering_weapon_ref',
        facility_attack_objectives=[_SCALAR],
        weapon_policy_contract=_OPPONENT_WEAPON_CONTRACT),
    terminal_contract=_fields(
        'breach_ends_episode settlement_ticks success settlement_phase settlement_mode '
        'settlement_ticks_role terminal_guard no_inactivity_shortcut live_deadline_ticks '
        'maximum_world_ticks armed_carrier_exhaustion hard_time_limit_ticks '
        'hard_time_limit_policy combat_exhaustion_policy assault_withdrawal_policy'),
    performance_scoring=_fields(
        'version timeliness ammunition_efficiency unspent_ammunition_without_neutralization_earns_zero',
        contact_to_neutralization_half_life_ticks=_DOMAIN_COUNTS,
        nominal_rounds_per_threat=_DOMAIN_COUNTS),
    combat_reference=_fields(
        'schema_version reference_initial_health disabled_health_threshold description',
        planning_minimum_hits_per_threat=_DOMAIN_COUNTS),
    score_symbols=_fields('F N P G H T A'),
    score_components=[_fields('metric_id kind weight direction')],
    communication_contract=_fields(
        'resource_ref controller_endpoint_ref delay_seconds snapshot_ttl_seconds loss_probability '
        'organic_contacts shared_contacts native_fire example_baselines minimum_report_interval_ticks'),
    communication_reporting=_fields('resource_ref minimum_report_interval_ticks observation availability'),
    faction_convention=_fields('schema_version participant_faction_id', roles=_fields('red blue neutral')),
    altitude_contract=_fields('reference low_ceiling_m higher_band_note'),
    energy_contract=_fields(
        'schema_version unit initial_energy formula depletion_behavior revision applies_to '
        'initial_normalized_energy energy_ref active_time_budget_seconds world_time_budget_seconds '
        'idle_rate_per_second motion_rate_per_meter motion_reference_speed_mps motion_speed_exponent '
        'speed_semantics firing_energy_per_shot total_score_weights_changed requires_engine_overlay',
                            uav=_ENERGY_PROFILE, usv=_ENERGY_PROFILE),
    detonation_contract=_fields('enabled source_lifecycle rule'),
    parameter_reference=_fields('mode', changed=[_SCALAR], limitations=[_SCALAR], unchanged_mechanisms=[_SCALAR]),
    difficulty_contract=_fields(
        'schema_version tier focus basis validation_status minimum_route_time_allowance_fraction idle_zero_neutralization_score_upper_bound'),
    homogeneous_contract=_fields(
        'schema_version revision self_detonation mine_support allow_unarmed_decoys speed_definition',
        uav=_CARRIER, usv=_CARRIER),
)


def _project(value, schema, own_ids):
    if schema is _SCALAR:
        if type(value) not in (str, int, float, bool, type(None)):
            raise ValueError('participant brief scalar must not contain implementation data')
        if type(value) is float and not math.isfinite(value):
            raise ValueError('participant brief numbers must be finite')
        return value
    if schema is _NUMBERS:
        if isinstance(value, list):
            return [_project(item, _NUMBERS, own_ids) for item in value]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('participant geometry must contain finite numbers only')
        return value
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise ValueError('participant brief list has an invalid shape')
        return [_project(item, schema[0], own_ids) for item in value]
    if not isinstance(value, dict):
        raise ValueError('participant brief mapping has an invalid shape')
    if isinstance(schema, _KeyedMap):
        allowed = own_ids if schema.keys == 'own' else _AMMUNITION
        return {key: _project(child, schema.value, own_ids)
                for key, child in value.items() if key in allowed}
    return {key: _project(value[key], child_schema, own_ids)
            for key, child_schema in schema.items() if key in value}


def participant_brief_view(brief):
    """Return detached reset data, without IE organizer deployment realizations.

    Full internal briefs remain available to compilation, scoring, auditing and
    replay capture. Only this view should cross the participant reset boundary.
    Unknown IE fields, including nested fields, are omitted by default.
    """
    if not isinstance(brief, dict):
        raise ValueError('participant brief must be a mapping')
    track = brief.get('track')
    if track in {'reconnaissance', 'emergency', 'denial', 'tracking'}:
        return deepcopy(brief)
    if track != 'interception':
        raise ValueError('unknown participant brief track')
    assets = brief.get('own_assets')
    if not isinstance(assets, list) or any(
            not isinstance(asset, dict) or not isinstance(asset.get('entity_id'), str)
            for asset in assets):
        raise ValueError('IE participant brief requires declared own asset identities')
    own_ids = {asset['entity_id'] for asset in assets}
    if not own_ids or len(own_ids) != len(assets):
        raise ValueError('IE own asset identities must be nonempty and unique')
    view = _project(normalize_public_waves(brief), _IE_VIEW, own_ids)
    if 'task_contract_revision' in view:
        _add_task_card(view)
    return view


def _add_task_card(view):
    """Build the task summary from public fields of the current deployment.

    Own objective positions are derived again rather than copied from a stale
    summary, so a transformed deployment keeps one authoritative public source.
    Organizer-only fields never enter this function.
    """
    if view['task_contract_revision'] != 'ie-public-task@1':
        raise ValueError('unsupported IE public task contract')
    assets = {asset['entity_id']: asset for asset in view['own_assets']}
    weights = view.get('facility_weights', {})
    targets = []
    for name, weight in sorted(weights.items()):
        if (name not in assets or type(weight) not in (int, float)
                or weight <= 0):
            raise ValueError('task objective requires an owned entity and positive weight')
        try:
            finite = math.isfinite(weight)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('task objective weight must be finite')
        targets.append({'entity_id': name, 'weight': weight,
                        'position_m': deepcopy(assets[name]['initial_position_m'])})
    if not targets:
        raise ValueError('task card requires declared scoring objectives')
    conditions = view.get('task_success_criteria')
    if not conditions or len({item['metric_id'] for item in conditions}) != len(conditions):
        raise ValueError('task card requires unique explicit success criteria')
    known_metrics = {item['metric_id'] for item in view['score_components']}
    for item in conditions:
        value = item.get('value')
        if (item.get('metric_id') not in known_metrics or item.get('comparison') != '>='
                or type(value) not in (int, float)):
            raise ValueError('invalid public task success criterion')
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('task success criterion must be finite')
    deadline, duration = view.get('adjudication_tick'), view.get('duration_ticks')
    if (type(deadline) is not int or type(duration) is not int
            or not 0 < deadline <= duration):
        raise ValueError('task card requires explicit decision and world deadlines')
    view['protected_facilities'] = targets
    view['task_card'] = {
        'schema_version': 'competition-task-card@1',
        'scenario_id': view['scenario_id'], 'title': view['name'],
        'task': view['task'], 'difficulty': view['difficulty'],
        'difficulty_validation': view['difficulty_contract']['validation_status'],
        'initial_owned_entity_count': len(assets),
        'protected_objective_count': len(targets),
        'public_schedule_entry_count': len(view['planned_waves']),
        'decision_deadline_tick': deadline, 'maximum_world_tick': duration,
        'success_requires_all_conditions': True,
        'success_criteria': deepcopy(conditions),
        'success_requires_native_settlement': True,
        'score_is_success_threshold': False,
        'score_formula': view['score_formula'],
        'scoring_targets_field': 'protected_facilities',
        'scoring_components_field': 'score_components',
    }

