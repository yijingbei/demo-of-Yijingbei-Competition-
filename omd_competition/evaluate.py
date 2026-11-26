"""Referee-owned lockstep evaluation over the actual frozen engine."""
import argparse
from collections import Counter
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

from pydantic import ValidationError
from openmdbench.schemas.interface_v2 import ActionBatchV2, DiscreteActionV2
from openmdbench.sessions.gateway_v2 import GatewayContractFailureV2, AuthorizationFailureV2
from .agent import navigation_batch
from .runtime import ROOT, ENGINE_COMMIT, PACKAGE_VERSION, create_session, public_brief, sha256, profile_id, opponent_plan, entry
from .opponent import AttackPolicy
from .legacy_plugins.message_policy import ScheduledMessagePolicy, sample_schedule
from .integrity import runtime_fingerprint
from .transport import Participant, AgentFailure, PROTOCOL
from .participant_brief import participant_brief_view
from openmdbench.sessions.lifecycle_v2 import SessionFailureV2


def plain(value):
    if hasattr(value, "model_dump"): return value.model_dump(mode="json")
    if is_dataclass(value): return {item.name: plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping): return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [plain(item) for item in value]
    return value


def verify_score(score):
    """Reconstruct utility-v1 and reject inconsistent native metric receipts.

    This verifies internal arithmetic and identities. The organizer's separate
    source audit binds metric definitions to the selected scenario bytes.
    """
    if isinstance(score, dict) and 'engagement_adjustment' in score:
        from copy import deepcopy
        from .surface_identification import adjusted_score
        adjustment = score['engagement_adjustment']
        if not isinstance(adjustment, dict) or adjustment.get('schema_version') != 'engagement-points@1':
            raise ValueError('unsupported engagement score adjustment')
        for key in ('violation_penalty_points', 'third_party_loss_penalty_points'):
            value = adjustment.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('invalid engagement score penalty')
        for key in ('violated_entity_ids', 'attributable_third_party_loss_ids'):
            values = adjustment.get(key)
            if (not isinstance(values, list) or any(type(v) is not str or not v for v in values)
                    or len(values) != len(set(values))):
                raise ValueError('engagement score must deduplicate physical entities')
        native = deepcopy(score)
        native.pop('engagement_adjustment')
        native['total'] = adjustment.get('base_total')
        verify_score(native)
        expected = adjusted_score(native, adjustment, adjustment)['total']
        if type(score.get('total')) not in (int, float) or not math.isclose(score['total'], expected, abs_tol=1e-10):
            raise ValueError('engagement score does not reconcile')
        return expected

    def number(value):
        if type(value) not in (int, float):
            raise ValueError("score numbers must be finite and cannot be boolean")
        try:
            converted = float(value)
        except OverflowError as error:
            raise ValueError("score number overflow") from error
        if not math.isfinite(converted):
            raise ValueError("score numbers must be finite")
        return converted

    def indexed(rows):
        if type(rows) is not list or not rows:
            raise ValueError("nonempty metric audit list required")
        result = {}
        for row in rows:
            if type(row) is not dict:
                raise ValueError("metric audit must be an object")
            name = row.get("metric_id")
            if type(name) is not str or not name or name in result:
                raise ValueError("duplicate or invalid metric audit identity")
            result[name] = row
        return result

    def close(actual, expected, message):
        if not math.isclose(number(actual), expected, rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError(message)

    if type(score) is not dict or score.get("aggregation_version") != "utility-v1":
        raise ValueError("unsupported score version")
    if score.get("aggregation") != "sum" or score.get("direction") != "maximize":
        raise ValueError("utility-v1 requires sum aggregation and maximize total")
    metrics = indexed(score.get("metrics"))
    receipts = indexed(score.get("metric_receipts"))
    if set(receipts) != set(metrics):
        raise ValueError("missing or unexpected metric audit")
    for name, item in metrics.items():
        audit = receipts[name]
        if "value" not in item or type(item.get("missing_inputs")) is not list:
            raise ValueError("explicit metric value and input evidence required")
        if item["missing_inputs"] or audit.get("data_status") == "missing":
            raise ValueError("required score input missing")
        weight = number(item.get("weight"))
        if weight < 0 or number(audit.get("weight")) < 0 or audit["weight"] != item["weight"]:
            raise ValueError("invalid or inconsistent declared score weight")
        if item.get("direction") not in ("maximize", "minimize"):
            raise ValueError("invalid score direction")
        if (type(item.get("unit")) is not str or not item["unit"]
                or item.get("aggregation") not in ("sum", "mean", "min", "max", "count")
                or any(audit.get(key) != item[key] for key in ("unit", "aggregation", "direction"))
                or audit.get("aggregation_version") != "utility-v1"):
            raise ValueError("metric receipt definition mismatch")

    available = [item for item in metrics.values() if item["value"] is not None]
    try:
        weight = math.fsum(number(item["weight"]) for item in available)
    except OverflowError as error:
        raise ValueError("score weight sum overflow") from error
    if not math.isfinite(weight) or weight <= 0:
        raise ValueError("no available scoring weight")
    contributions = []
    for name, item in metrics.items():
        audit = receipts[name]
        if item["value"] is None:
            reason = item.get("not_applicable_reason")
            if (audit.get("data_status") != "not_applicable"
                    or type(reason) is not str or not reason
                    or audit.get("not_applicable_reason") != reason):
                raise ValueError("N/A lacks consistent evidence")
            if (any(key not in audit or audit[key] is not None for key in
                    ("value", "raw_value", "utility_value", "weighted_contribution"))
                    or number(audit.get("effective_weight")) != 0):
                raise ValueError("N/A cannot carry numeric score credit")
            continue
        if (audit.get("data_status") != "available"
                or item.get("not_applicable_reason") is not None
                or audit.get("not_applicable_reason") is not None):
            raise ValueError("available metric status mismatch")
        raw = number(item["value"])
        for key in ("value", "raw_value"):
            number(audit.get(key))
            if audit[key] != item["value"]:
                raise ValueError("raw score audit mismatch")
        lower, upper = number(item.get("normalization_lower_bound")), number(item.get("normalization_upper_bound"))
        if upper <= lower or not math.isfinite(upper-lower) or not math.isfinite(raw-lower):
            raise ValueError("invalid score normalization")
        normalized = min(1., max(0., (raw-lower)/(upper-lower)))
        utility = normalized if item["direction"] == "maximize" else 1-normalized
        effective = number(item["weight"])/weight
        close(audit.get("utility_value"), utility, "utility audit mismatch")
        close(audit.get("effective_weight"), effective, "weight audit mismatch")
        contribution = utility*effective
        close(audit.get("weighted_contribution"), contribution, "weighted contribution audit mismatch")
        contributions.append(contribution)
    total = math.fsum(contributions)
    reported = number(score.get("total"))
    if not 0 <= reported <= 1:
        raise ValueError("score total outside utility range")
    close(reported, total, "score total mismatch")
    return total


def observation(gateway, session, slot):
    return gateway.controller_observation(session.session_id, principal_id="principal." + slot.id,
        controller_slot_id=slot.id).model_dump(mode="json")


def submit(gateway, session, slot, batch):
    batch = dict(batch)
    batch["batch_id"] = 'controller.' + slot.id + '.' + str(session.world_view.tick)
    return gateway.submit_as_principal(session.session_id, principal_id="principal." + slot.id,
        controller_slot_id=slot.id, batch=ActionBatchV2.model_validate(batch),
        operation_id=f"submit.{slot.id}.{session.world_view.tick}", expected_tick=session.world_view.tick)


def source_batches(rows, source_obs, slots, controller_slots, tick):
    """One batch per controller and tick, including all independently due notices."""
    by_slot = {slot.id: slot for slot in slots}
    grouped = {}
    for row in rows:
        slot_id = controller_slots[row["sender_id"]]
        slot = by_slot[slot_id]
        batch = grouped.setdefault(slot_id, navigation_batch(source_obs[row["sender_id"]], {}))
        batch["discrete_actions"].append({"schema_version": "2.0", "entity_id": row["sender_id"],
            "faction_id": slot.faction_id, "based_on_tick": tick, "valid_until_tick": tick+1,
            "action_id": row["message_id"], "action_type": "send_message",
            "payload": {"recipient_controller_slots": row["recipient_controller_slots"],
                        "message": row["message"]}})
    return [(by_slot[slot_id], batch) for slot_id, batch in grouped.items()]


def validate_participant_batch(batch, obs, scenario, brief=None):
    if batch is None: return
    validated = ActionBatchV2.model_validate(batch)
    if validated.based_on_tick != obs["tick"] or validated.valid_until_tick < obs["tick"]+1:
        raise AgentFailure("participant batch does not cover the current decision window")
    allowed = set(brief['available_actions']) if brief else ({"navigation", "hold", "send_message"} | (set() if scenario.startswith("MD-REC-") else {"fire_weapon"}))
    contacts = {item["contact_id"] for item in obs["organic_contacts"]+obs["shared_contacts"]}
    speed_limits = None
    if brief and brief.get('track') in {'reconnaissance', 'emergency'}:
        asset_field = 'own_assets' if brief['track'] == 'reconnaissance' else 'response_assets'
        assets = brief.get(asset_field, ())
        speed_limits = {asset['entity_id']: asset['maximum_speed_mps'] for asset in assets}
        if not speed_limits or len(speed_limits) != len(assets) or any(
                type(cap) not in (int, float) or not math.isfinite(cap) or cap < 0
                for cap in speed_limits.values()):
            raise ValueError('invalid competition navigation speed contract')
    for child in (*validated.persistent_commands, *validated.discrete_actions):
        kind = getattr(child, "action_type", getattr(child, "command_type", None))
        if kind not in allowed: raise AgentFailure("unsupported competition action: "+str(kind))
        if child.based_on_tick != obs["tick"] or child.valid_until_tick < obs["tick"]+1:
            raise AgentFailure("participant child action does not cover the current decision window")
        if kind == 'navigation' and speed_limits is not None:
            if child.entity_id not in speed_limits:
                raise AgentFailure('navigation requires a declared competition asset')
            speed = child.payload.get('speed_mps', 0.)
            if type(speed) not in (int, float) or not math.isfinite(speed) or speed < 0:
                raise AgentFailure('navigation requires a finite nonnegative speed')
            if speed > speed_limits[child.entity_id]:
                raise AgentFailure('navigation speed exceeds the declared competition limit')
        if kind == "fire_weapon" and child.payload.get("contact_id") not in contacts:
            raise AgentFailure("fire requires an opaque contact in the current authorized observation")
        if kind == 'fire_weapon' and brief and brief.get('track')=='interception':
            options = brief.get('own_weapon_options', {}).get(child.entity_id)
            if options is None:
                options = [brief.get('own_weapon_policies', {}).get(child.entity_id, {})]
            declared = {option.get('weapon_ref') for option in options if option.get('weapon_ref')}
            if child.payload.get('weapon_ref') not in declared:
                raise AgentFailure('fire requires the declared weapon of the owned competition entity')


def opponent_navigation(obs, profile, seed):
    attack, tick = profile.attack, obs["tick"]
    payloads = {}
    for own in obs["own_entities"]:
        entity = own["entity_id"]
        if entity not in obs["controlled_entity_ids"]: continue
        pos = own["position_m"]
        base = math.degrees(math.atan2(profile.objective_m[0]-pos[0], profile.objective_m[1]-pos[1])) % 360
        digest = hashlib.sha256(entity.encode()).digest()
        sign, phase = (1 if digest[0] % 2 else -1), int.from_bytes(digest[1:5], "big") / 2**32
        trigger = attack.turn_on_first_contact and bool(obs["organic_contacts"] or obs["shared_contacts"])
        offset = sign * (attack.split_angle_deg + (attack.contact_turn_deg if trigger else 0))
        if attack.pattern == "multi_axis_serpentine":
            offset += attack.serpentine_angle_deg * math.sin(2*math.pi*(tick/attack.serpentine_period_ticks+phase))
        if attack.pattern == "direct": offset = 0
        values = attack.speed_cycle_mps or (attack.speed_mps,)
        shift = int.from_bytes(hashlib.sha256(f"{seed}:{entity}".encode()).digest()[:4], "big") % len(values)
        speed = values[(shift+tick//attack.speed_cycle_ticks) % len(values)]
        payloads[entity] = {"speed_mps": speed, "heading_deg": (base+offset) % 360, "altitude_m": pos[2]}
    return navigation_batch(obs, payloads) if payloads else None


def planned_entity_metadata(resolved):
    """Own strategy metadata includes entities declared in future spawn events."""
    tags = {e.id: set(e.tags) for e in resolved.entities}
    factions = {e.id: e.faction_id for e in resolved.entities}
    for event in resolved.events:
        if event.event_type == 'spawn':
            entity = plain(event.payload)['entity']
            tags[entity['id']] = set(entity.get('tags', ()))
            factions[entity['id']] = entity['faction_id']
    return tags, factions


def interception_settlement_reason(tick, brief, score, attacker_ids, states):
    """Stop new requests from referee lifecycle facts; native rules still finish.

    Self-detonation removes a threat without earning interception credit. Missing
    or scheduled identities remain unresolved, regardless of ammunition/contact.
    Pending actions, missiles and damage are subsequently drained by the engine.
    """
    if brief.get('track') != 'interception' or 'adjudication_tick' not in brief:
        return None
    if tick >= brief['adjudication_tick']:
        return 'adjudication_deadline'
    metrics = {m['metric_id']: m['value'] for m in (score or {}).get('metrics', [])}
    if metrics.get('metric.assault_withdrawal_candidate') == 1:
        return 'assault_withdrawn'
    if metrics.get('metric.combat_exhaustion_candidate') == 1:
        return 'combat_exhausted'
    if metrics.get('metric.facilities') == 0:
        return 'all_protected_facilities_lost'
    if attacker_ids and all(states.get(e, {}).get('lifecycle') in
                           {'disabled', 'destroyed', 'wreck', 'despawned'} for e in attacker_ids):
        return 'all_planned_threats_stopped'
    return None


def run_episode(scenario, mode, seed, output, command=None, max_ticks=None, submission_sha256=None, capture_checkpoint=False, resource_group="cpu", record_replay=True, replay_html=None, record_compression='gzip', replay_format='shared', replay_keep=20, keep_replay=False, storage_scope='debug'):
    # A reused hidden-matrix worker can switch deployment roots between cases.
    from .runtime import ENGINE_ROOT, SCENE_ROOT
    if record_compression not in {'gzip','none'} or replay_format not in {'shared','html','none'} or replay_keep<0:
        raise ValueError('Invalid replay storage options')
    if replay_html is not None:replay_format='html' if replay_html else 'none'
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    session = gateway = recorder = None
    participants = {}
    began = time.monotonic()
    result = {"schema_version": "competition-result@0.1", "engine_commit": ENGINE_COMMIT,
        "package_version": PACKAGE_VERSION, "scenario": scenario, "seed": seed, "policy": mode,
        "status": "infrastructure_failure", "utility": None, "terminal": None,
        "competition_accepted": False}
    result["runtime_sha256"] = runtime_fingerprint()
    patch_path = ROOT / "engine/patches.json"
    result["engine_patchset"] = json.loads(patch_path.read_text(encoding="utf-8"))["patchset"] if patch_path.exists() else "upstream-unmodified"
    result["resource_group"] = resource_group
    budgets = {"cpu": (30., 1., 120.), "api": (120., 30., 1800.)}
    reset_seconds, act_seconds, cumulative_seconds = budgets[resource_group]
    result["decision_budget_seconds"] = {"reset": reset_seconds, "act": act_seconds, "episode": cumulative_seconds}
    result["submission_sha256"] = submission_sha256 or hashlib.sha256(json.dumps(command or {
        "baseline": mode, "agent_sha256": sha256(ROOT / "omd_competition/agent.py")},
        sort_keys=True).encode()).hexdigest()
    from plugins.html_replay.record_io import RecordWriter
    trace_writer = None
    opponent_recorder = None
    classic_ie = entry(scenario)['catalog'] == 'ie_set'
    participant_attempt = None
    try:
        opponent_configuration = None
        if entry(scenario).get('opponent_variant') == 's_v16':
            from .opponent_diagnostics import inspect_configuration
            opponent_configuration = inspect_configuration(SCENE_ROOT, entry(scenario))
            result['opponent_configuration'] = opponent_configuration
            if not opponent_configuration['ok']:
                raise ValueError('invalid opponent configuration: '+str(opponent_configuration['errors']))
        session, gateway = create_session(scenario, seed, "episode." + scenario + "." + str(seed), referee=True)
        resolved = session.resolved
        brief = public_brief(scenario)
        from plugins.html_replay.recording import observation_snapshot
        if record_replay:
            from plugins.html_replay.recording import ReplayRecorder
            from plugins.html_replay.bundle import capture_scene
            result['replay_scene'] = capture_scene(output, SCENE_ROOT/entry(scenario)['package'],
                SCENE_ROOT, scenario, result['runtime_sha256'], engine_root=ENGINE_ROOT)
            recorder = ReplayRecorder(output, scenario, result['runtime_sha256'], resolved.world.tick_seconds,
                compression=record_compression, weather_schedule=brief.get('weather_schedule'))
        result.update(resolved_hash=resolved.resolved_hash, catalog_hash=resolved.catalog_hash,
            model_registry_hash=resolved.model_registry_hash,
            evaluator_sha256=sha256(__file__), agent_sha256=sha256(ROOT / "omd_competition/agent.py"))
        identification = None
        if 'identification_contract' in brief:
            raise ValueError('Optical-authorization v1 requires its archived runtime; it is not autonomous-v2 evidence')
        if 'contact_judgment_contract' in brief:
            from .anonymous_contacts import AnonymousContacts
            identification = AnonymousContacts(brief, resolved, session.world_factory.catalog,
                                                session.world_factory.resolve_contact)
            session.world_factory.contact_projection = identification
        if recorder is not None:
            recorder.capture(session)
        from .action_feedback import ActionReceiptRouter
        from .surface_identification import public_receipt
        receipt_router = ActionReceiptRouter() if identification is not None else None
        participant_brief = participant_brief_view(brief)
        from .factions import contract as faction_contract
        result['faction_convention'] = faction_contract(brief)
        plan=opponent_plan(scenario)
        evaluated_faction=brief.get('defence',{}).get('faction_id',brief.get('evaluated_side'))
        controlled=set(brief.get('participant_entity_ids', ()))
        slots=[s for s in resolved.controller_slots if s.faction_id==evaluated_faction
               and (not controlled or set(s.selector.entity_ids) & controlled)]
        participant_slot_ids={s.id for s in slots}
        schedule=None
        if plan.get('message_schedule'):
            realized=sample_schedule(plan['message_schedule'],seed=seed,
                maximum_shift_ticks=brief.get('notice_timing_variation_ticks',0))
            schedule=ScheduledMessagePolicy(realized)
            result['source_plan_sha256']=hashlib.sha256(json.dumps(realized,sort_keys=True).encode()).hexdigest()
        entity_tags, entity_factions = planned_entity_metadata(resolved)
        attacker_tags = {entity: tags for entity, tags in entity_tags.items()
                         if entity_factions.get(entity) == plan.get('attack', {}).get('faction_id')}
        from .opponent_registry import select_policy
        attacker = select_policy(entry(scenario), AttackPolicy)(plan, attacker_tags, seed=seed) if plan['kind'] == 'ie_attack' else None
        if attacker is not None and opponent_configuration is not None:
            from .opponent_diagnostics import OpponentIntentRecorder
            attacker.speed_limits = opponent_configuration['entity_speed_limits']
            attacker.altitude_limits = opponent_configuration['entity_altitude_limits']
            opponent_recorder = OpponentIntentRecorder(output,
                {key: result[key] for key in ('scenario', 'seed', 'runtime_sha256', 'resolved_hash')},
                opponent_configuration)
            attacker.intent_sink = opponent_recorder.transition
        combat_audit = {f: {'requested': 0, 'executed': 0, 'rejected': 0, 'shots': 0}
                        for f in sorted(set(entity_factions.values()))}
        fire_owners = {}
        issued_delayed_impacts = set()
        combat_errors = Counter()
        for slot in slots:
            participants[slot.id] = Participant(command or [sys.executable, "-m", "omd_competition.agent", "--mode", mode],
                output / (slot.id + ".stderr.log"), resource_group=resource_group)
        last = {}
        score = terminal = None
        settlement_started_tick = None
        settlement_start_reason = None
        last_decision_tick = None
        settlement_attacker_ids = ()
        if brief['track'] == 'interception' and 'adjudication_tick' in brief:
            cohort_metric = next(m for m in resolved.scoring.metrics if m.id == 'metric.neutralization')
            settlement_attacker_ids = tuple(cohort_metric.plugin_parameters['attacker_ids'])
        ticks = int(resolved.world.duration_ticks)
        with RecordWriter(output/'trace.jsonl', record_compression) as trace:
            trace_writer=trace
            for tick in range(ticks if max_ticks is None else min(ticks, max_ticks)):
                engagement_denials = {}
                public_rows = []
                opponent_rows = []
                opponent_observations = []
                # Score credit excludes self-detonated attackers. Use the native
                # complete planned cohort so new navigation cannot keep the
                # pending-action queue occupied after physical combat has ended.
                states = (session.world_factory.mission_snapshot().entity_states
                          if settlement_attacker_ids else {})
                reason = interception_settlement_reason(
                    tick, brief, score, settlement_attacker_ids, states)
                settling = reason is not None
                if settling and settlement_started_tick is None:
                    settlement_started_tick = tick
                    settlement_start_reason = reason
                if not settling:
                    last_decision_tick = tick
                if schedule is not None:
                    source_obs={entity:observation(gateway,session,next(s for s in resolved.controller_slots if s.id==slot_id))
                        for entity,slot_id in schedule.plan['controller_slots'].items()}
                    opponent_observations.extend(observation_snapshot(obs) for obs in source_obs.values())
                    for source_slot,source_batch in source_batches(schedule.actions(tick,source_obs),source_obs,
                            resolved.controller_slots,schedule.plan['controller_slots'],tick):
                        submit(gateway,session,source_slot,source_batch)
                        opponent_rows.append({'controller_slot':source_slot.id,'response':source_batch})
                prepared_opponent_obs = {}
                if not settling and identification is None and attacker is not None and hasattr(attacker, 'prepare_tick'):
                    prepared_opponent_obs = {
                        slot.id: observation(gateway, session, slot)
                        for slot in resolved.controller_slots
                        if slot.id not in participant_slot_ids
                        and (schedule is None or slot.id not in schedule.plan['controller_slots'].values())}
                    attacker.prepare_tick(list(prepared_opponent_obs.values()))
                for slot in resolved.controller_slots:
                    if settling: continue
                    if slot.id in participant_slot_ids: continue
                    if schedule is not None and slot.id in schedule.plan['controller_slots'].values(): continue
                    obs = (prepared_opponent_obs[slot.id] if slot.id in prepared_opponent_obs
                           else observation(gateway, session, slot))
                    if identification is not None:
                        obs = identification.observation(obs)
                    opponent_observations.append(plain(obs) if receipt_router is not None else observation_snapshot(obs))
                    if attacker is not None:
                        feedback = public_receipt(last.get(slot.id)) if receipt_router is not None else None
                        if receipt_router is not None:
                            identification.feedback(slot.id, feedback, obs['tick'])
                        if receipt_router is not None and hasattr(attacker, 'observe_receipt'):
                            attacker.observe_receipt(feedback, controller_slot_id=slot.id)
                        batch = attacker.act(obs)
                        if batch:
                            for action in batch['discrete_actions']:
                                if classic_ie and action['action_type'] != 'fire_weapon':
                                    continue
                                fire_owners[action['action_id']] = slot.faction_id
                                combat_audit[slot.faction_id]['requested'] += 1
                            native_batch=identification.filter_batch(batch,obs)[0] if identification is not None else batch
                            queued = submit(gateway, session, slot, native_batch)
                            if opponent_recorder is not None:
                                opponent_recorder.decision(attacker, obs, batch, plain(queued))
                            if receipt_router is not None:
                                receipt_router.submitted(slot.id, batch)
                        if receipt_router is not None:
                            last[slot.id] = public_receipt({'queue_receipt': plain(queued) if batch else None,
                                                          'execution_receipts': []})
                            opponent_rows.append({'controller_slot': slot.id, 'response': batch, 'last_receipt': feedback})
                        elif batch:
                            opponent_rows.append({'controller_slot': slot.id, 'response': batch})
                        continue
                    payloads={}
                    for own in obs['own_entities']:
                        entity=own['entity_id']
                        if entity not in obs['controlled_entity_ids']: continue
                        if plan['kind']=='segments':
                            segment=next((r for r in plan['segments'] if r['entity_id']==entity and r['start_tick']<=tick<r['end_tick']),None)
                            if segment: payloads[entity]=segment['payload']
                        else:
                            attack=plan['attack']
                            tags=entity_tags.get(entity,set())
                            route=next((r for r in plan.get('routes',[]) if r['match_tag'] in tags),{})
                            target=route.get('objective_m',plan['objective_m'])
                            target_tag=next((tag.removeprefix('target.') for tag in tags if tag.startswith('target.')),None)
                            if target_tag in plan.get('facility_positions',{}): target=plan['facility_positions'][target_tag][:2]
                            pos=own['position_m']
                            heading=math.degrees(math.atan2(target[0]-pos[0],target[1]-pos[1]))%360
                            payloads[entity]={'speed_mps':route.get('speed_mps',attack['speed_mps']),'heading_deg':heading,'altitude_m':pos[2]}
                    if payloads:
                        batch = navigation_batch(obs,payloads)
                        submit(gateway,session,slot,batch)
                        opponent_rows.append({'controller_slot':slot.id,'response':batch})
                for slot in slots:
                    if settling:
                        last[slot.id]={'queue_receipt':None,'execution_receipts':[]}
                        public_rows.append({'controller_slot':slot.id,'request':None,'response':None,
                                            'queue_receipt':None,'phase':'settlement'})
                        continue
                    obs = observation(gateway, session, slot)
                    if identification is not None:
                        obs = identification.observation(obs)
                    request = {"protocol": PROTOCOL, "type": "reset" if tick == 0 else "act",
                        "episode_id": session.session_id, "controller_slot_id": slot.id,
                        "faction_convention": result['faction_convention'],
                        "observation": obs, "last_receipt": last.get(slot.id)}
                    if identification is not None:
                        request['last_receipt'] = public_receipt(request['last_receipt'])
                        identification.feedback(slot.id, request['last_receipt'], obs['tick'])
                    if tick == 0: request["public_brief"] = participant_brief
                    participant_attempt = {
                        'decision_tick': tick, 'controller_slot': slot.id,
                        'phase': 'transport', 'request': request,
                        'response_available': False, 'response': None,
                        'timeout_seconds': reset_seconds if tick == 0 else act_seconds}
                    batch = participants[slot.id].request(request, timeout=reset_seconds if tick == 0 else act_seconds)
                    participant_attempt.update(phase='episode_budget', response_available=True, response=batch)
                    if sum(p.elapsed for p in participants.values()) > cumulative_seconds:
                        raise AgentFailure("episode cumulative decision time exceeded resource-group budget")
                    participant_attempt['phase'] = 'action_validation'
                    try:
                        validate_participant_batch(batch, obs, scenario, brief)
                        for action in (batch or {}).get('discrete_actions', []):
                            if action['action_type'] == 'fire_weapon':
                                fire_owners[action['action_id']] = slot.faction_id
                                combat_audit[slot.faction_id]['requested'] += 1
                        participant_attempt['phase'] = 'action_submission'
                        native_batch = batch
                        if identification is not None:
                            native_batch, _ = identification.filter_batch(batch, obs)
                        queued = submit(gateway, session, slot, native_batch) if native_batch is not None else None
                        if receipt_router is not None:
                            receipt_router.submitted(slot.id, batch)
                    except (ValidationError, GatewayContractFailureV2, AuthorizationFailureV2, SessionFailureV2, ValueError) as error:
                        raise AgentFailure("invalid participant action: " + str(error)) from error
                    last[slot.id] = {'queue_receipt': plain(queued), 'execution_receipts': []}
                    if identification is not None:
                        last[slot.id] = public_receipt(last[slot.id])
                    public_rows.append({"controller_slot": slot.id, "request": request, "response": batch,
                                        "queue_receipt": last[slot.id]})
                    participant_attempt = None
                receipt = session.step(operation_id=f"tick.{tick}", expected_tick=tick)
                if recorder is not None:
                    recorder.capture(session)
                if receipt_router is not None:
                    for slot_id, children in receipt_router.route(plain(receipt.child_receipts)).items():
                        last.setdefault(slot_id, {'queue_receipt': None, 'execution_receipts': []})['execution_receipts'].extend(children)
                for slot in (() if receipt_router is not None else slots):
                    row = next(r for r in public_rows if r['controller_slot'] == slot.id)
                    batch = row['response'] or {}
                    children = {a.get('action_id', a.get('command_id')) for a in
                                [*batch.get('persistent_commands', []), *batch.get('discrete_actions', [])]}
                    last[slot.id]['execution_receipts'] = [plain(c) for c in receipt.child_receipts if c.child_id in children]
                    if identification is not None:
                        last[slot.id]['execution_receipts'] = public_receipt(last[slot.id]['execution_receipts']) + engagement_denials.get(slot.id, [])
                combat = plain(receipt.world_receipt.combat_receipts)
                if attacker is not None and identification is None:
                    attacker.observe_execution(combat)
                if opponent_recorder is not None:
                    opponent_recorder.execution(receipt.tick,
                        plain(receipt.child_receipts), combat)
                for dispatch in combat:
                    faction = fire_owners[dispatch['request_id']]
                    combat_audit[faction][dispatch['status']] += 1
                    if dispatch['status'] == 'executed':
                        combat_audit[faction]['shots'] += dispatch['execution']['requested_shots']
                        issued_delayed_impacts.update(dispatch['execution']['pending_impact_ids'])
                    else:
                        combat_errors[dispatch['error_code']] += 1
                score = plain(receipt.world_receipt.score_receipts[-1])
                terminal = plain(receipt.world_receipt.mission_receipts[-1].terminal_result)
                engagement_events = None
                if identification is not None:
                    identification.combat(combat)
                    identification.damage(plain(receipt.world_receipt.damage_receipts))
                    native_terminal = terminal
                    if terminal is not None:
                        result['native_terminal'] = terminal
                    engagement_events = identification.drain()
                trace_row = {"tick": receipt.tick, "participants": public_rows,
                    "receipt_hash": receipt.receipt_hash, "score": score, "terminal": terminal,
                    "child_receipts": plain(receipt.child_receipts), 'opponents': opponent_rows,
                    'opponent_observations': opponent_observations,
                    'combat_receipts': combat, 'damage_receipts': plain(receipt.world_receipt.damage_receipts),
                    # Referee-only evidence explains blocked motion without
                    # exposing collision counterparts or geometry to agents.
                    'motion_receipts': plain(receipt.world_receipt.motion_receipts),
                    'dynamics_receipts': plain(receipt.world_receipt.dynamics_receipts)}
                if identification is not None:
                    trace_row.update(engagement_events=engagement_events,
                        engagement_audit=identification.audit(), native_terminal=native_terminal)
                if plan.get('record_navigation_phases') and attacker is not None:
                    trace_row['navigation_evidence'] = dict(decision_tick=tick,
                        staged=plain(attacker.stages), stage_history=plain(attacker.stage_history),
                        buoy_legs=dict(attacker.buoys.legs), buoy_arrivals=dict(attacker.buoys.arrivals),
                        buoy_completed=sorted(attacker.buoys.completed))
                if brief['track'] == 'interception':
                    # Organizer evidence stays in the trace, outside participant
                    # requests and replay scopes. Keep per-step event receipts;
                    # the fact reader deliberately omits growing event history.
                    trace_row.update(schema_version='competition-interception-trace@5',
                        transport_evidence=session.world_factory.transport_evidence.capture(
                            receipt.tick, session.world_factory.transport_snapshot()),
                        score_plugin_evidence=session.world_factory.score_plugin_evidence.drain(receipt.tick),
                        # Already-produced audit receipts include missed detections,
                        # measurements and RNG anchors; never add them to observations.
                        subsystem_receipts=plain(receipt.world_receipt.subsystem_receipts),
                        mission_receipts=plain(receipt.world_receipt.mission_receipts),
                        world_event_receipts=plain(receipt.world_receipt.event_receipts),
                        lifecycle_receipts=plain(receipt.world_receipt.lifecycle_receipts),
                        referee_facts_phase='after_session_step',
                        referee_mission_facts=plain(session.world_factory.mission_snapshot()))
                trace.write(json.dumps(trace_row, ensure_ascii=False, allow_nan=False) + "\n")
                if terminal is not None: break
        result.update(final_tick=session.world_view.tick, terminal=terminal, score_audit=score)
        result['combat_audit'] = combat_audit
        result['combat_rejections'] = dict(combat_errors)
        if identification is not None:
            result['engagement_audit'] = identification.audit()
            result['score_points'] = None if score is None else 100 * score['total']
        if combat_errors.get('combat.weapon_unknown'):
            raise RuntimeError('native combat registry rejected a declared weapon; scene adaptation is invalid')
        if brief['track']=='interception' and 'adjudication_tick' in brief:
            snapshot = session.world_factory.projectile_snapshot()
            active = snapshot['missile_flights']
            delayed = snapshot['pending_impacts']
            consumed = {r.impact_id for r in snapshot['pending_impact_receipts']}
            pending_ids = {r.impact_id for r in delayed}
            if snapshot['tick'] != session.world_view.tick or issued_delayed_impacts != consumed | pending_ids:
                raise RuntimeError('native delayed impact evidence does not reconcile')
            result['pending_projectile_audit']={'active_at_final_tick':len(active),
                'pending_impacts_at_final_tick':len(delayed),
                'issued_delayed_impacts':len(issued_delayed_impacts),
                'consumed_delayed_impacts':len(consumed),
                'evidence_source':'native_world_public_combat_properties',
                'settlement_ticks':brief['terminal_contract']['settlement_ticks'],
                'adjudication_tick':brief['adjudication_tick']}
            result['referee_missile_terminal_receipts'] = plain(snapshot['missile_terminal_receipts'])
            result['termination_audit'] = {
                'mode': brief['terminal_contract'].get('settlement_mode', 'fixed_window'),
                'rule_id': None if terminal is None else terminal['rule_id'],
                'last_decision_tick': last_decision_tick,
                'settlement_started_tick': settlement_started_tick,
                'settlement_start_reason': settlement_start_reason,
                'final_combat_activity': plain(snapshot['combat_activity']),
                'hard_limit_tick': ticks,
            }
            if terminal is not None and (active or delayed):
                from .combat_completion import declared_time_limit_reached
                intentional_cutoff = classic_ie and declared_time_limit_reached(
                    terminal, brief, session.world_view.tick, ticks)
                if classic_ie:
                    result['termination_audit']['projectiles_at_declared_time_limit'] = intentional_cutoff
                if not intentional_cutoff:
                    raise RuntimeError('natural terminal still has active native projectiles')
        result["referee_final_entities"] = {e.id: {"faction_id":e.faction_id, "position_m":list(e.state.position_m),
            "health":e.state.health, "lifecycle":e.state.lifecycle,
            "energy_state":{"value":e.state.energy,"unit":"normalized",
                "capacity_semantics":"fraction_of_declared_capacity"}} for e in session.world_view.entities_stable()}
        result["referee_scoring_state"] = plain(session.world_view.presentation_snapshot().mission_scoring_checkpoint)
        result["checkpoint_extraction"] = {"status": "disabled", "restore_equivalence": "not_supported_in_competition_v04"}
        if capture_checkpoint:
            try:
                checkpoint = session.checkpoint()
                result["checkpoint_extraction"] = {"status": "passed", "hash": checkpoint.checkpoint_hash,
                    "restore_equivalence": "not_tested"}
                (output / "checkpoint.json").write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
            except Exception as error:
                result["checkpoint_extraction"] = {"status": "failed", "error": repr(error)}
        if terminal is None:
            result.update(status="bounded_probe" if max_ticks is not None else "infrastructure_failure",
                          error="natural terminal not reached")
        else:
            if resource_group == "api" and any(p.usage is None for p in participants.values()):
                raise AgentFailure("API group requires cumulative usage from every controller")
            result.update(status="completed", utility=verify_score(score))
            if scenario.startswith("MD-REC-"):
                result["task_success"] = terminal["outcome"] == "objective_complete"
            else:
                metrics = {m["metric_id"]: m["value"] for m in score["metrics"]}
                result["task_success"] = terminal['outcome'] == 'objective_complete'
    except AgentFailure as error:
        result.update(status="algorithm_failure", utility=0.0, error=str(error))
        if participant_attempt is not None:
            # An interrupted decision has no native step receipt. Preserve it
            # separately; completed-tick traces and public replay stay unchanged.
            evidence = dict(schema_version='competition-participant-failure@1',
                runtime_sha256=result['runtime_sha256'], scenario=scenario, seed=seed,
                session_tick=session.world_view.tick, error=str(error),
                **participant_attempt)
            try:
                raw = (json.dumps(evidence, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')
                evidence_path = output / 'participant.failure.json'
                evidence_path.write_bytes(raw)
                result['participant_failure_evidence'] = dict(status='written', file=evidence_path.name,
                    sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                    decision_tick=participant_attempt['decision_tick'],
                    controller_slot=participant_attempt['controller_slot'], phase=participant_attempt['phase'])
            except (OSError, ValueError, TypeError) as evidence_error:
                result['participant_failure_evidence'] = dict(status='failed', error=repr(evidence_error))
    except Exception as error:
        result.update(status="infrastructure_failure", utility=None, error=repr(error))
    finally:
        clean = all([participant.close() for participant in participants.values()])
        result['execution_isolation'] = {}
        for slot_id, participant in participants.items():
            audit = dict(participant.isolation_audit)
            if command is None and audit.get('execution_kind') == 'trusted_subprocess':
                audit['execution_kind'] = 'trusted_builtin'
            result['execution_isolation'][slot_id] = audit
        result['external_sandbox_verified'] = bool(participants) and all(
            audit.get('external_sandbox_verified') is True
            for audit in result['execution_isolation'].values())
        protocol_errors = {slot_id: participant.protocol_error for slot_id, participant in participants.items()
                           if participant.protocol_error}
        if protocol_errors and result['status'] == 'completed':
            result.update(status='algorithm_failure', utility=0.,
                          error='participant transport failed: '+json.dumps(protocol_errors, sort_keys=True))
        if not clean:
            result.update(status='infrastructure_failure', utility=None, error='participant cleanup did not complete')
        result["algorithm_seconds"] = sum(p.elapsed for p in participants.values())
        result["decision_timeouts"] = sum(p.timeouts for p in participants.values())
        if resource_group == "api":
            result["api_usage"] = {key: sum((p.usage or {}).get(key, 0) for p in participants.values())
                                   for key in ("calls", "input_tokens", "output_tokens", "cost_usd")}
            result["api_usage_status"] = "participant_declared_requires_billing_review"
        result["elapsed_wall_seconds"] = time.monotonic()-began
        if trace_writer is not None:
            trace_meta=trace_writer.metadata()
            result.update(trace_file=trace_meta['file'],trace_sha256=trace_meta['sha256'],
                trace_encoding=trace_meta['encoding'],trace_storage_sha256=trace_meta['storage_sha256'],
                trace_uncompressed_bytes=trace_meta['uncompressed_bytes'],trace_stored_bytes=trace_meta['stored_bytes'])
        if recorder is not None:
            result.setdefault('final_tick', session.world_view.tick)
            try:
                result['replay_recording'] = recorder.close(session.world_view.tick, result['status'] == 'completed')
            except Exception as error:
                result.update(status='infrastructure_failure', utility=None,
                              replay_recording={'error': repr(error)}, error='replay recording failed: '+repr(error))
        if opponent_recorder is not None:
            try:
                result['opponent_intent'] = opponent_recorder.finish(
                    result['status'], session.world_view.tick if session is not None else None)
            except Exception as error:
                result.update(status='infrastructure_failure', utility=None,
                              error='opponent intent recording failed: '+repr(error))
        if gateway is not None and session is not None: gateway.close(session.session_id)
        (output / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")
    if recorder is not None:
        from plugins.html_replay.bundle import finish_replay
        report=finish_replay(output, result, SCENE_ROOT, enabled=replay_format!='none', format=replay_format)
        try:
            from plugins.html_replay.retention import register,prune
            register(output,result,report or {},keep_last=replay_keep,important=keep_replay,scope=storage_scope)
            if storage_scope=='debug' and replay_keep:prune(output.parent,replay_keep,apply=True)
        except Exception as error:
            (output/'replay-storage-error.json').write_text(json.dumps({'error':str(error)},ensure_ascii=False)+'\n',encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--mode", choices=("idle", "naive", "coordinated", "greedy", "adaptive"), default="coordinated")
    parser.add_argument("--seed", type=int, default=2026101201)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-ticks", type=int)
    parser.add_argument("--capture-checkpoint", action="store_true", help="organizer diagnostic; restore is not a competition feature")
    parser.add_argument("--record-replay", action=argparse.BooleanOptionalAction, default=True, help="record every tick and both sides' observations (default on; --no-record-replay disables)")
    parser.add_argument("--replay-html", action=argparse.BooleanOptionalAction, default=None, help="compatibility: --replay-html makes standalone HTML; --no-replay-html keeps only raw records")
    parser.add_argument('--replay-format',choices=['shared','html','none'],default='shared',help='default shared: compressed match data and one reusable offline player')
    parser.add_argument('--record-compression',choices=['gzip','none'],default='gzip',help='lossless trace/frame storage (default gzip)')
    parser.add_argument('--replay-keep',type=int,default=20,help='keep latest N successful unpinned debug replays; 0 disables expiration; never deletes results/action logs')
    parser.add_argument('--keep-replay',action='store_true',help='pin this replay against automatic expiration')
    parser.add_argument("--submission", type=Path, help="JSON file with entrypoint argument array")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = args.output or ROOT / "artifacts" / f"{args.scenario}-{args.mode}-{args.seed}-{stamp}"
    command = None
    submission_hash = None
    resource_group = "cpu"
    if args.submission:
        from .submission import load_submission
        command, submission_hash, team_id, resource_group = load_submission(args.submission, args.scenario)
    result = run_episode(args.scenario, args.mode, args.seed, destination, command, args.max_ticks, submission_hash, args.capture_checkpoint, resource_group, record_replay=args.record_replay, replay_html=args.replay_html,
        record_compression=args.record_compression,replay_format=args.replay_format,replay_keep=args.replay_keep,keep_replay=args.keep_replay)
    print(json.dumps({key: result.get(key) for key in ("scenario", "status", "utility", "final_tick", "error")}, ensure_ascii=False))
    print(destination)
    if (destination/'replay.pack.json.gz').is_file():print('Replay: open',destination.parent/'replay-viewer.html','and choose',destination/'replay.pack.json.gz')
    raise SystemExit(int(result["status"] not in {"completed", "bounded_probe"}))


if __name__ == "__main__": main()
