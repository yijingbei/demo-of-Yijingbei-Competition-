"""LLM inspection scheduling over the IE15 participant rule policy."""

import json
import os
from pathlib import Path

from .agent import PROTOCOL
from .buoy_baselines import contact_policy
from .hybrid_llm_client import AuditedClient, extract_json


SYSTEM_PROMPT = """You schedule repeat observations in a simulated maritime survey.
Use the aggregate observation counts to choose one of two schedules:
- survey: use the normal inspection interval for previously visited contacts.
- revisit: shorten that interval when recently updated contacts merit a follow-up.
Use survey when no recent contacts are available. Your only output is one JSON
object with a single key, "mode", whose value is "survey" or "revisit"."""


class BuoyHybridCommander:
    def __init__(self, brief, *, plan_interval, max_fallback):
        self.policy = contact_policy(brief, "adaptive")
        normal = self.policy.config["inspection_revisit_ticks"]
        self.revisit_intervals = {"survey": normal, "revisit": max(1, normal // 2)}
        self.mode = "survey"
        self.plan_interval = plan_interval
        self.max_fallback = max_fallback
        self.next_plan_tick = 0
        self.fallback_streak = 0
        self.llm = AuditedClient()
        audit = os.environ.get("OPENMD_LLM_AUDIT")
        self.plans_path = Path(audit).with_name("scout-plans.jsonl") if audit else None

    def _plan(self, observation):
        tick = observation["tick"]
        self.next_plan_tick = tick + self.plan_interval
        contacts = {}
        for contact in observation["organic_contacts"] + observation["shared_contacts"]:
            if contact.get("confirmed") and contact.get("estimated_domain") == "surface":
                key = contact["contact_id"]
                contacts[key] = max(contacts.get(key, 0), contact["observed_tick"])
        recent = sum(0 <= tick - last <= self.plan_interval for last in contacts.values())
        digest = {
            "tick": tick,
            "recent_surface_contacts": recent,
            "older_surface_contacts": len(contacts) - recent,
            "last_schedule": self.mode,
        }
        directive = extract_json(self.llm.complete(SYSTEM_PROMPT, json.dumps(digest)))
        proposed = directive.get("mode") if directive else None
        accepted = (
            isinstance(proposed, str) and proposed in self.revisit_intervals
            and set(directive) == {"mode"}
        )
        if accepted:
            self.mode = proposed
            self.fallback_streak = 0
        else:
            self.fallback_streak += 1
            if self.fallback_streak >= self.max_fallback:
                self.mode = "survey"
        interval = self.revisit_intervals[self.mode]
        self.policy.config["inspection_revisit_ticks"] = interval
        if self.plans_path:
            with self.plans_path.open("a", encoding="utf-8") as audit:
                audit.write(json.dumps({
                    "tick": tick,
                    "accepted": accepted,
                    "effective_mode": self.mode,
                    "inspection_revisit_ticks": interval,
                }) + "\n")

    def act(self, observation, receipt):
        if observation["tick"] >= self.next_plan_tick:
            self._plan(observation)
        self.policy.observe_receipt(receipt)
        batch = self.policy.act(observation)
        summary = self.llm.summary()
        return {
            "protocol": PROTOCOL,
            "batch": batch,
            "usage": {
                "calls": summary["calls"],
                "input_tokens": summary["prompt_tokens"],
                "output_tokens": summary["completion_tokens"],
                "cost_usd": 0.0,
            },
        }
