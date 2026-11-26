"""OpenAI-compatible LLM client for the Hybrid agents.

Reuses the paper project's planner/executor split: this module only provides the
transport to the configured endpoint. It only receives inputs supplied by the
participant process.
"""
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("OPENMD_LLM_BASE", "http://127.0.0.1:8101/v1")
DEFAULT_MODEL = os.environ.get("OPENMD_LLM_MODEL", "Qwen3.8-27B")
DEFAULT_TIMEOUT = float(os.environ.get("OPENMD_LLM_TIMEOUT", "25"))
DEFAULT_MAX_TOKENS = int(os.environ.get("OPENMD_LLM_MAX_TOKENS", "256"))


def extract_json(text):
    """Pull the first JSON object out of a model reply (tolerates code fences)."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


class LLMClient:
    def __init__(self, base=None, model=None, timeout=None, max_tokens=None):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        self.max_tokens = DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens
        self.reported_model = None
        self.stats = {"calls": 0, "failures": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "latency_s": []}

    def complete(self, system, user, max_tokens=None, temperature=0.0):
        self.reported_model = None
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "temperature": temperature,
        }
        if self.model.casefold().startswith("deepseek-"):
            payload["thinking"] = {"type": "disabled"}
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Content-Type": "application/json"}
        key = os.environ.get("OPENMD_LLM_API_KEY")
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        began = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            self.stats["failures"] += 1
            self.stats["calls"] += 1
            return None
        elapsed = time.monotonic() - began
        self.stats["calls"] += 1
        self.stats["latency_s"].append(round(elapsed, 3))
        reported_model = body.get("model")
        if isinstance(reported_model, str):
            self.reported_model = reported_model
        usage = body.get("usage") or {}
        self.stats["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.stats["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            self.stats["failures"] += 1
            return None
        return message.get("content") or ""

    def summary(self):
        lat = self.stats["latency_s"]
        return {
            "calls": self.stats["calls"],
            "failures": self.stats["failures"],
            "prompt_tokens": self.stats["prompt_tokens"],
            "completion_tokens": self.stats["completion_tokens"],
            "latency_mean_s": round(sum(lat) / len(lat), 3) if lat else None,
            "latency_max_s": max(lat) if lat else None,
        }


class AuditedClient(LLMClient):
    """Record actual requests; credentials are never written to the audit."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from pathlib import Path
        audit = os.environ.get('OPENMD_LLM_AUDIT')
        self.audit = Path(audit) if audit else None
        if self.audit:
            self.audit.parent.mkdir(parents=True, exist_ok=True)
            (self.audit.parent/'replay-strategies.json').write_text(json.dumps({
                'schema_version':'competition-replay-strategies@1',
                'red':{'kind':'hybrid','mode':'LLM+adaptive / strict-'+os.environ.get('OPENMD_LLM_PLAN_INTERVAL','30')+'tick',
                       'driver':'omd_competition.hybrid_agent','large_model':self.model}},ensure_ascii=False,indent=2),encoding='utf-8')

    def complete(self, system, user, max_tokens=None, temperature=0.0):
        started=time.monotonic(); before=self.summary()
        response=super().complete(system,user,max_tokens=max_tokens,temperature=temperature)
        after=self.summary()
        try: tick=json.loads(user).get('tick')
        except (ValueError,AttributeError): tick=None
        if self.audit:
            row={'tick':tick,'pid':os.getpid(),'model':self.model,'endpoint':self.base,
                 'reported_model':self.reported_model,'thinking':False,'temperature':temperature,'elapsed_seconds':time.monotonic()-started,
                 'request_system':system,'request_user':user,'response':response,
                 'parsed_json':bool(extract_json(response)),
                 'calls_delta':after['calls']-before['calls'],
                 'failures_delta':after['failures']-before['failures'],
                 'input_tokens':after['prompt_tokens']-before['prompt_tokens'],
                 'output_tokens':after['completion_tokens']-before['completion_tokens']}
            with self.audit.open('a',encoding='utf-8') as f:
                f.write(json.dumps(row,ensure_ascii=False)+'\n')
        return response
