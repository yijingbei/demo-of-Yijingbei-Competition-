"""Native JSONL action serialization only; no baseline-policy imports."""

def navigation_batch(obs, payloads, fires=()):
    tick, faction = obs["tick"], obs["observer_faction_id"]
    persistent, discrete = [], []
    for entity, payload in sorted(payloads.items()):
        payload=dict(payload)
        # A tiny negative atan2 angle can round to exactly 360.0 after modulo.
        # Canonicalize that equivalent bearing in the example agent; arbitrary
        # invalid participant actions still go through native validation.
        if payload.get('heading_deg')==360.:
            payload['heading_deg']=0.
        persistent.append({"schema_version": "2.0", "entity_id": entity, "faction_id": faction,
            "based_on_tick": tick, "valid_until_tick": tick + 1,
            "command_id": f"nav.{entity}.{tick}", "command_type": "navigation", "payload": payload})
    for entity, payload in fires:
        discrete.append({"schema_version": "2.0", "entity_id": entity, "faction_id": faction,
            "based_on_tick": tick, "valid_until_tick": tick + 1,
            "action_id": f"fire.{entity}.{tick}", "action_type": "fire_weapon", "payload": payload})
    return {"schema_version": "2.0", "session_id": obs["session_id"],
        "batch_id": f"batch.{obs['controller_slot_id']}.{tick}",
        "idempotency_key": f"decision.{obs['controller_slot_id']}.{tick}",
        "faction_id": faction, "based_on_tick": tick, "valid_until_tick": tick + 1,
        "persistent_commands": persistent, "discrete_actions": discrete}
