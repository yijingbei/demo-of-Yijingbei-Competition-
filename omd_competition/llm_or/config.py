"""Public, credential-free strategy settings."""
from dataclasses import asdict, dataclass, fields
import json
import math
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class StrategyConfig:
    base_url: str = 'https://delta-router-test.yangtzeailab.com/v1'
    model: str = 'DeepSeek-V4-Flash'
    plan_interval: int = 100
    min_plan_interval: int = 20
    timeout_seconds: float = 25.
    max_tokens: int = 4096
    max_api_requests: int = 600
    api_time_budget_seconds: float = 1200.
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None

    @classmethod
    def parse(cls, value):
        if type(value) is not dict or set(value) - {f.name for f in fields(cls)}:
            raise ValueError('Unknown strategy setting; credentials belong in OPENMD_LLM_API_KEY')
        config = cls(**value)
        address = urlsplit(config.base_url) if isinstance(config.base_url, str) else None
        if (address is None or address.scheme not in ('https', 'http') or not address.hostname
                or address.username or address.password or address.query or address.fragment):
            raise ValueError('base_url must be an HTTP(S) endpoint without credentials/query/fragment')
        if not isinstance(config.model, str) or not config.model.strip():
            raise ValueError('model is required')
        for name in ('plan_interval', 'min_plan_interval', 'max_tokens', 'max_api_requests'):
            if type(getattr(config, name)) is not int or getattr(config, name) < 1:
                raise ValueError(name + ' must be a positive integer')
        if config.min_plan_interval > config.plan_interval:
            raise ValueError('min_plan_interval must not exceed plan_interval')
        for name, lower, upper in [('timeout_seconds', 0., 29.),
                                   ('api_time_budget_seconds', 25., 1500.)]:
            value = getattr(config, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not lower < value <= upper:
                raise ValueError(f'{name} must be > {lower} and <= {upper}')
        prices = (config.input_price_per_million_usd, config.output_price_per_million_usd)
        if (prices[0] is None) != (prices[1] is None):
            raise ValueError('Supply both token prices or leave both null')
        if prices[0] is not None and any(type(p) not in (int, float)
                or not math.isfinite(p) or p < 0 for p in prices):
            raise ValueError('Token prices must be finite and nonnegative')
        return config

    @classmethod
    def load(cls, path):
        return cls.parse(json.loads(Path(path).read_text(encoding='utf-8-sig')))

    def as_dict(self):
        return asdict(self)

    @property
    def prices_known(self):
        return self.input_price_per_million_usd is not None
