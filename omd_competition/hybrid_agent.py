"""Hybrid red defence agent: LLM tactical planner (upper) + rule executor (lower).

Architecture mirrors the paper project's ``llm_planner`` + ``GOAIExecutorV2`` split:
the LLM only issues *goals/params* every plan interval; a deterministic rule layer
(``AirLanePlanner``, the competition adaptive baseline) turns them into native
navigation/fire actions every tick. No engine state is exposed to the model; the
digest is built from the participant observation only.

Directive surface (deliberately small and auditable):
  posture                  garrison | forward | conserve
  engage_non_threatening   true | false      (hold fire on contacts that are not
                                              closing on the protected objective)
  reserve_release_delay    0..600 | null     (late-wave reserve release delay)
  salvo                    single | double | default

Fallback semantics follow the paper planner: keep the last directive on transport
or parse failure; after ``OPENMD_LLM_MAX_FALLBACK`` consecutive failures fall back
to the deterministic garrison posture. Every plan cycle is logged to stderr.
"""
import argparse
import json
import math
import os
import sys

from .contact_selection import without_flight_contacts
from .agent import navigation_batch  # noqa: F401  (kept for parity / reuse)
from .hybrid_brief_compat import CompatiblePlanner as AirLanePlanner
from .hybrid_llm_client import AuditedClient as LLMClient, extract_json

PROTOCOL = "openmd-competition@0.1"
PLAN_INTERVAL = int(os.environ.get("OPENMD_LLM_PLAN_INTERVAL", "30"))
MAX_FALLBACK = int(os.environ.get("OPENMD_LLM_MAX_FALLBACK", "3"))

DEFAULT_DIRECTIVE = {
    "posture": "garrison",
    "engage_non_threatening": True,
    "reserve_release_delay": None,
    "salvo": "default",
    "reason": "deterministic default",
}

POSTURES = {
    "garrison": {"surface_max_excursion_m": 2500, "surface_standoff_m": 900,
                 "air_patrol_speed_mps": 22.0, "air_fire_age_ticks": None},
    "forward": {"surface_max_excursion_m": 6000, "surface_standoff_m": 2500,
                "air_patrol_speed_mps": 25.0, "air_fire_age_ticks": None},
    "conserve": {"surface_max_excursion_m": 2500, "surface_standoff_m": 900,
                 "air_patrol_speed_mps": 22.0, "air_fire_age_ticks": 3},
}

SYSTEM_PROMPT = """You are the RED defence tactical planner. You do NOT control units directly;
a deterministic rule executor turns your directive into native actions.

Mission: keep every protected facility intact and neutralise all planned threats,
including future waves. Score rewards facility integrity, neutralisation, survival,
interception timeliness and ammunition efficiency.

You receive a compact situation report: your own units (position, ammunition,
cooldown), confirmed hostile contacts (position, velocity, age, whether observed by
this controller), the protected objective, public wave windows, and your previous
directive.

Output ONLY one JSON object:
{"posture":"garrison|forward|conserve",
 "engage_non_threatening":true|false,
 "reserve_release_delay":<int 0-600 or null>,
 "salvo":"single|double|default",
 "reason":"<max 24 chinese characters>"}

Guidance:
- Contacts far from the objective and not closing on it may be a diversion: setting
  engage_non_threatening=false makes the executor hold fire on them.
- posture=forward extends surface patrols and speeds up air patrol (more coverage,
  more exposure); garrison keeps units on station; conserve tightens fire freshness
  and limits surface salvo size (good under ammunition pressure).
- Use reserve_release_delay when future waves are still pending; null keeps the
  scenario's own default.
- Do not invent contacts or facilities; use only the report."""


def compact_number(value, digits=0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits) if digits else int(round(value))


class HybridCommander:
    def __init__(self, brief, obs):
        self.brief = brief
        self.defence = brief.setdefault("defence", {})
        self.objective = tuple(brief.get("objective_m") or (0.0, 0.0))
        self.base_objective = self.objective
        self.planner = AirLanePlanner(brief, "adaptive")
        self.llm = LLMClient()
        self.directive = dict(DEFAULT_DIRECTIVE)
        self.last_plan_tick = -(10 ** 9)
        self.last_contact_count = 0
        self.fallback_streak = 0
        self.plan_count = 0
        self.fallback_count = 0
        self.directive_log = []
        self._surface_signature = None

    # ---------------------------------------------------------------- digest
    def _contacts(self, obs):
        obs = without_flight_contacts(obs)
        rows = []
        for item in list(obs.get("organic_contacts") or []) + list(obs.get("shared_contacts") or []):
            if not item.get("detected", True) or not item.get("confirmed", False):
                continue
            if item.get("age_ticks", 999) > 8:
                continue
            rows.append(item)
        rows.sort(key=lambda c: (math.dist(c["estimated_position_m"][:2], self.objective),
                                 c.get("contact_id", "")))
        return rows[:12]

    def _digest(self, obs, contacts):
        own = []
        for unit in obs.get("own_entities") or []:
            if unit["entity_id"] not in obs.get("controlled_entity_ids", ()):
                continue
            ammo = sum(int(v) for v in (unit.get("ammunition") or {}).values())
            own.append({
                "id": unit["entity_id"],
                "pos": [compact_number(v) for v in unit["position_m"][:2]],
                "alt": compact_number(unit["position_m"][2]),
                "ammo": ammo,
                "health": compact_number(unit.get("health", 1.0), 2),
            })
        contact_rows = []
        for c in contacts:
            pos = c["estimated_position_m"]
            vel = c.get("estimated_velocity_mps") or c.get("velocity_mps") or (0.0, 0.0, 0.0)
            distance = math.dist(pos[:2], self.objective[:2])
            closing = -sum((self.objective[i] - pos[i]) * vel[i] for i in (0, 1)) / max(1.0, distance)
            contact_rows.append({
                "id": c.get("contact_id"),
                "pos": [compact_number(v) for v in pos[:2]],
                "alt": compact_number(pos[2]),
                "speed": compact_number(math.hypot(vel[0], vel[1]), 1),
                "dist_obj": compact_number(distance),
                "closing_mps": compact_number(closing, 1),
                "age": int(c.get("age_ticks", 0)),
                "by_self": bool(c.get("observed_by_controlled", True)),
            })
        waves = []
        for wave in (self.brief.get("planned_waves") or [])[:6]:
            waves.append({
                "window": wave.get("tick_window") or [wave.get("spawn_tick")],
                "air": wave.get("air", 0),
                "surface": wave.get("surface", 0),
                "decoy": wave.get("decoy", 0),
            })
        return {
            "tick": obs["tick"],
            "objective": [compact_number(v) for v in self.base_objective],
            "facility_weights": self.brief.get("facility_weights") or {},
            "own": own,
            "contacts": contact_rows,
            "planned_waves": waves,
            "previous_directive": {k: v for k, v in self.directive.items() if k != "reason"},
        }

    # ------------------------------------------------------------- planning
    def _plan(self, obs):
        contacts = self._contacts(obs)
        digest = self._digest(obs, contacts)
        text = self.llm.complete(SYSTEM_PROMPT, json.dumps(digest, ensure_ascii=False), max_tokens=self.llm.max_tokens)
        parsed = extract_json(text) if text is not None else None
        ok = False
        if parsed:
            candidate = {
                "posture": str(parsed.get("posture", "garrison")).lower(),
                "engage_non_threatening": bool(parsed.get("engage_non_threatening", True)),
                "reserve_release_delay": parsed.get("reserve_release_delay"),
                "salvo": str(parsed.get("salvo", "default")).lower(),
                "reason": str(parsed.get("reason", ""))[:60],
            }
            if candidate["posture"] not in POSTURES:
                candidate["posture"] = "garrison"
            delay = candidate["reserve_release_delay"]
            if delay is not None:
                try:
                    delay = int(delay)
                except (TypeError, ValueError):
                    delay = None
                if delay is not None and not 0 <= delay <= 600:
                    delay = None
            candidate["reserve_release_delay"] = delay
            if candidate["salvo"] not in {"single", "double", "default"}:
                candidate["salvo"] = "default"
            self.directive = candidate
            ok = True
            self.fallback_streak = 0
        else:
            self.fallback_count += 1
            self.fallback_streak += 1
            if self.fallback_streak >= MAX_FALLBACK:
                self.directive = dict(DEFAULT_DIRECTIVE)
        self._apply(self.directive)
        self.plan_count += 1
        self.last_plan_tick = obs["tick"]
        self.last_contact_count = len(contacts)
        self.directive_log.append({"tick": obs["tick"], "directive": self.directive,
                                   "ok": ok, "contacts": len(contacts)})
        sys.stderr.write(json.dumps({"plan": self.directive_log[-1],
                                     "llm": self.llm.summary()}, ensure_ascii=False) + "\n")
        sys.stderr.flush()

    def _apply(self, directive):
        posture = POSTURES.get(directive["posture"], POSTURES["garrison"])
        surface = self.defence.setdefault("surface_patrol", {})
        air = self.defence.setdefault("aircraft_patrol", {})
        surface["maximum_station_excursion_m"] = posture["surface_max_excursion_m"]
        surface["intercept_standoff_m"] = posture["surface_standoff_m"]
        if air.get("enabled"):
            air["speed_mps"] = posture["air_patrol_speed_mps"]
        if posture["air_fire_age_ticks"] is None:
            self.defence.pop("maximum_air_fire_contact_age_ticks", None)
        else:
            self.defence["maximum_air_fire_contact_age_ticks"] = posture["air_fire_age_ticks"]
        if directive["reserve_release_delay"] is not None:
            self.defence["reserve_commit_after_last_wave_ticks"] = directive["reserve_release_delay"]
        self.defence["maximum_surface_inflight_rounds"] = (
            1 if directive["salvo"] == "single" else 2)
        signature = (directive["posture"], directive["reserve_release_delay"], directive["salvo"])
        if signature != self._surface_signature:
            # SurfacePatrol snapshots its config at construction; rebuild on change.
            self._surface_signature = signature
            self.planner = AirLanePlanner(self.brief, "adaptive")

    # ------------------------------------------------------------- executing
    def _gate(self, batch, obs, contacts):
        if not batch or self.directive.get("engage_non_threatening", True):
            return batch
        by_id = {c.get("contact_id"): c for c in contacts}
        kept = []
        dropped = 0
        for action in batch.get("discrete_actions") or []:
            if action.get("action_type") != "fire_weapon":
                kept.append(action)
                continue
            contact = by_id.get((action.get("payload") or {}).get("contact_id"))
            if contact is None:
                kept.append(action)
                continue
            pos = contact["estimated_position_m"]
            vel = contact.get("estimated_velocity_mps") or contact.get("velocity_mps") or (0.0, 0.0, 0.0)
            distance = math.dist(pos[:2], self.objective[:2])
            closing = -sum((self.objective[i] - pos[i]) * vel[i] for i in (0, 1)) / max(1.0, distance)
            threatening = distance <= 14000.0 or closing >= 3.0
            if threatening:
                kept.append(action)
            else:
                dropped += 1
        if dropped:
            sys.stderr.write(json.dumps({"gate": "hold_fire_non_threatening",
                                         "tick": obs["tick"], "dropped": dropped},
                                        ensure_ascii=False) + "\n")
        batch = dict(batch)
        batch["discrete_actions"] = kept
        return batch

    def _should_plan(self, obs, contacts):
        # Frozen experiment used strict scheduling, without contact-count triggers.
        return obs["tick"] - self.last_plan_tick >= PLAN_INTERVAL

    def act(self, obs):
        contacts = self._contacts(obs)
        if self._should_plan(obs, contacts):
            try:
                self._plan(obs)
            except Exception as error:  # noqa: BLE001 - never break the episode for planning
                self.fallback_count += 1
                sys.stderr.write(json.dumps({"plan_error": repr(error)}, ensure_ascii=False) + "\n")
        try:
            batch = self.planner.act(obs)
        except Exception as error:  # noqa: BLE001
            sys.stderr.write(json.dumps({"executor_error": repr(error)}, ensure_ascii=False) + "\n")
            batch = None
        batch = self._gate(batch, obs, contacts)
        summary = self.llm.summary()
        usage = {
            "calls": summary["calls"],
            "input_tokens": summary["prompt_tokens"],
            "output_tokens": summary["completion_tokens"],
            "cost_usd": 0.0,
        }
        return {"protocol": PROTOCOL, "batch": batch, "usage": usage}

    def close(self):
        sys.stderr.write(json.dumps({
            "hybrid_close": True,
            "plans": self.plan_count,
            "fallbacks": self.fallback_count,
            "llm": self.llm.summary(),
            "directives": self.directive_log,
        }, ensure_ascii=False) + "\n")
        sys.stderr.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-interval", type=int, default=None)
    args = parser.parse_args()
    if args.plan_interval:
        global PLAN_INTERVAL
        PLAN_INTERVAL = max(1, args.plan_interval)
    commander = None
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("protocol") != PROTOCOL:
            raise ValueError("unsupported protocol")
        if request["type"] == "close":
            if commander is not None:
                commander.close()
            return
        observation = request["observation"]
        if request["type"] == "reset":
            commander = HybridCommander(request["public_brief"], observation)
        if commander is None:
            raise ValueError("reset required")
        response = commander.act(observation)
        print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
