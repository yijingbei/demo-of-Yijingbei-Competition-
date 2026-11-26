"""Example participant executable. It consumes only stdin DTOs, never engine state."""
import argparse
import json
import math
import sys
from .intercept_planner import InterceptPlanner, AirLanePlanner
from .contact_selection import without_flight_contacts

PROTOCOL = "openmd-competition@0.1"


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


class InterceptPolicy:
    def __init__(self, brief, mode):
        self.brief, self.mode = brief, mode
        self.last_fired = {}

    def act(self, obs):
        if self.mode == "idle": return None
        obs = without_flight_contacts(obs)
        payloads, fires = {}, []
        objective = self.brief["objective_m"]
        config = self.brief["defence"]
        contacts = [item for item in obs["organic_contacts"] + obs["shared_contacts"]
                    if item.get("track_quality", item.get("confidence", 0)) >= config["contact_confidence"]
                    and item.get("age_ticks", 999) <= config["maximum_contact_age_ticks"]]
        contacts.sort(key=lambda item: (math.dist(item["estimated_position_m"][:2], objective),
                                        item["contact_id"]))
        for own in obs["own_entities"]:
            entity = own["entity_id"]
            if entity not in obs["controlled_entity_ids"]: continue
            policy = self.brief["own_weapon_policies"].get(entity)
            if policy is None: continue
            position = own["position_m"]
            air = 'air' in policy.get('target_domains', ['surface'] if 'surface' in policy['weapon_ref'] else ['air'])
            compatible = [c for c in contacts if (c['estimated_position_m'][2] > 50) == air]
            rank = sorted(self.brief['own_weapon_policies']).index(entity)
            if self.mode == 'coordinated' and compatible:
                shift = rank % len(compatible)
                compatible = compatible[shift:]+compatible[:shift]
            target = compatible[0]["estimated_position_m"] if compatible else objective
            if self.mode == "coordinated" and not compatible: target = position
            distance = math.dist(position[:2], target[:2])
            heading = math.degrees(math.atan2(target[0]-position[0], target[1]-position[1])) % 360
            error = (heading-own["heading_deg"]+180) % 360-180
            maximum_speed = config['intercept_speed_mps'] if air else 10.
            speed = min(maximum_speed, distance / 3)
            if self.mode == "coordinated":
                speed = min(maximum_speed, max(0., distance-policy["maximum_range_m"]*.75)/3)
            if self.mode == "coordinated" and abs(error) > 30: speed = 0.
            payloads[entity] = {"speed_mps": speed,
                "heading_deg": heading,
                "altitude_m": position[2]}
            weapon = policy["weapon_ref"]
            if own["cooldown_ticks"].get(weapon, 0) > 0: continue
            if not own["capability_flags"].get("weapon", False): continue
            if not any(value > 0 for value in own.get("ammunition", {}).values()): continue
            organic_ids = {item["contact_id"] for item in obs["organic_contacts"]}
            targets = [item for item in compatible if item["contact_id"] in organic_ids]
            for contact in targets:
                key = (entity, contact["contact_id"])
                if obs["tick"]-self.last_fired.get(key, -1000) < (40 if self.mode == 'coordinated' else 5): continue
                distance = math.dist(position, contact["estimated_position_m"])
                if policy["minimum_range_m"] <= distance <= policy["maximum_range_m"]:
                    fires.append((entity, {"weapon_ref": weapon, "contact_id": contact["contact_id"]}))
                    self.last_fired[key] = obs["tick"]
                    break
        return navigation_batch(obs, payloads, fires) if payloads or fires else None


class SafeBaselinePolicy(InterceptPolicy):
    def __init__(self, brief, mode):
        super().__init__(brief, mode)
        self.traffic = InterceptPlanner(brief, 'greedy')

    def act(self, obs):
        obs = without_flight_contacts(obs)
        batch = super().act(obs)
        if not batch:
            return batch
        config = self.brief['defence']
        contacts = [c for c in obs['organic_contacts'] + obs['shared_contacts']
                    if c.get('track_quality', c.get('confidence', 0)) >= config['contact_confidence']
                    and c.get('age_ticks', 999) <= config['maximum_contact_age_ticks']]
        surface = [c for c in contacts if c['estimated_position_m'][2] <= 50]
        own = {u['entity_id']: u for u in obs['own_entities']}
        policies = self.brief['own_weapon_policies']
        payloads = {c['entity_id']: c['payload'] for c in batch['persistent_commands']}
        for entity, payload in payloads.items():
            policy = policies[entity]
            if self.traffic._air(policy):
                continue
            unit = own[entity]
            rounds = unit.get('ammunition', {}).get(policy['ammunition_ref'], 0)
            if not rounds or not unit.get('capability_flags', {}).get('weapon', False) or not surface:
                payload.update(speed_mps=0., heading_deg=unit['heading_deg'])
                continue
            nearest = min(math.dist(unit['position_m'][:2], c['estimated_position_m'][:2])
                          for c in surface)
            payload['speed_mps'] = min(payload['speed_mps'], max(0., nearest-600.)/3.)
        self.traffic._deconflict_surface(obs, payloads, policies)
        for command in batch['persistent_commands']:
            command['payload'] = payloads[command['entity_id']]
            if command['payload'].get('heading_deg') == 360.:
                command['payload']['heading_deg'] = 0.
        return batch

class SafeAirBaselinePolicy(SafeBaselinePolicy):
    def __init__(self, brief, mode):
        super().__init__(brief, mode)
        self.aircraft = sorted(k for k, p in brief['own_weapon_policies'].items()
                               if InterceptPlanner._air(p))
        self.base_altitude = None
        self.initial_levels_ready = False

    def act(self, obs):
        obs = without_flight_contacts(obs)
        batch = super().act(obs)
        if not batch:
            return batch
        own = {u['entity_id']: u for u in obs['own_entities']}
        commands = {c['entity_id']: c for c in batch['persistent_commands']
                    if c['entity_id'] in self.aircraft}
        if not commands:
            return batch
        if self.base_altitude is None:
            self.base_altitude = min(own[k]['position_m'][2] for k in commands)
        declared = self.brief.get('defence', {}).get('aircraft_flight_levels_m', {})
        levels = {k: declared.get(k, self.base_altitude + 60.*self.aircraft.index(k)) for k in commands}
        # Establish separated levels before the first aircraft moves across a
        # teammate's climb path. The observation supplies every position used.
        if all(abs(own[k]['position_m'][2]-level) <= 5. for k, level in levels.items()):
            self.initial_levels_ready = True
        config = self.brief['defence']
        air_contacts = [c for c in obs['organic_contacts'] + obs['shared_contacts']
                        if c['estimated_position_m'][2] > 50
                        and c.get('track_quality', c.get('confidence', 0)) >= config['contact_confidence']
                        and c.get('age_ticks', 999) <= config['maximum_contact_age_ticks']]
        for entity, command in commands.items():
            unit, payload = own[entity], command['payload']
            policy = self.brief['own_weapon_policies'][entity]
            payload['altitude_m'] = levels[entity]
            rounds = unit.get('ammunition', {}).get(policy['ammunition_ref'], 0)
            error = abs((payload['heading_deg']-unit['heading_deg']+180.) % 360.-180.)
            if (not self.initial_levels_ready or abs(unit['position_m'][2]-levels[entity]) > 5.
                    or not rounds or not unit.get('capability_flags', {}).get('weapon', False)
                    or not air_contacts):
                payload.update(speed_mps=0., heading_deg=unit['heading_deg'])
            elif error > 30.:
                payload['speed_mps'] = 0.
        return batch


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('idle','naive','coordinated','greedy','adaptive'),default='coordinated')
    args=parser.parse_args()
    policy=None
    for line in sys.stdin:
        request=json.loads(line)
        if request.get('protocol') != PROTOCOL: raise ValueError('unsupported protocol')
        if request['type']=='close': return
        obs=request['observation']
        if request['type']=='reset':
            brief=request['public_brief']
            if brief['track'] != 'interception':
                raise ValueError('the demo agent expects an interception brief')
            if 'contact_judgment_contract' in brief:
                from .buoy_baselines import contact_policy
                policy = contact_policy(brief, args.mode)
            elif args.mode in {'adaptive', 'greedy'}:
                policy = AirLanePlanner(brief, args.mode)
            elif args.mode in {'naive', 'coordinated'}:
                policy = SafeAirBaselinePolicy(brief, args.mode)
            else:
                policy = InterceptPolicy(brief, args.mode)
        if policy is None: raise ValueError('reset required')
        if hasattr(policy, 'observe_receipt'):
            policy.observe_receipt(request.get('last_receipt'))
        batch = policy.act(obs)
        print(json.dumps({'protocol':PROTOCOL,'batch':batch},allow_nan=False),flush=True)

if __name__=='__main__': main()
