"""JSONL participant entry point. Stdout contains protocol responses only."""
import argparse
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile

from ..transport import MAX_LINE, PROTOCOL, strict_loads
from .config import StrategyConfig
from .controller import BudgetedSource, GameAgent
from .io import atomic_json


class NullWriter:
    def write(self, value):
        return len(value)

    def flush(self):
        pass

    def close(self):
        pass


class Participant:
    def __init__(self, config, log_dir, *, source=None):
        self.config = config
        self.output = Path(log_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        self.source = source or BudgetedSource(config)
        self.source.diagnostic_path = self.output / 'planner-events.jsonl'
        self.stream = gzip.open(self.output / 'decisions.jsonl.gz', 'xt', encoding='utf-8')
        self.agent = None

    def act(self, request):
        if request.get('protocol') != PROTOCOL:
            raise ValueError('Unsupported participant protocol')
        kind = request.get('type')
        if kind == 'reset':
            if self.agent is not None:
                raise ValueError('Only one episode is allowed per participant process')
            self.agent = GameAgent(request['public_brief'], self.source,
                                   self.config.plan_interval, self.output, self.stream)
        elif kind != 'act' or self.agent is None:
            raise ValueError('An act request must follow reset')
        return self.agent.act(request)

    def close(self):
        if self.agent is not None:
            self.agent.publish_progress(force=True)
        atomic_json(self.output / 'strategy-summary.json', {
            'architecture': 'LLM + mathematical optimization',
            'api_usage': self.source.usage,
            'api_prices_known': self.config.prices_known,
            'api_seconds': self.source.api_seconds,
            'llm_plans_accepted': self.source.accepted_plans,
            'api_usage_complete': self.source.usage_complete,
            'decisions': self.agent.decisions if self.agent else 0,
            'confirmed_launches': self.agent.executor.memory.confirmed_launches if self.agent else 0,
            'rule_action_fallback': False})
        self.stream.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='Config file; the local runner supplies an in-memory snapshot')
    parser.add_argument('--log-dir', type=Path)
    args = parser.parse_args(argv)
    if args.config is not None:
        config = StrategyConfig.load(args.config)
    else:
        raw = os.environ.get('OPENMD_LLM_OR_CONFIG_JSON')
        if raw is None:
            parser.error('Supply --config or launch with run_llm_or.py')
        config = StrategyConfig.parse(json.loads(raw))
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    temporary = tempfile.TemporaryDirectory(prefix='llm-or-') if args.log_dir is None else None
    output = args.log_dir or Path(temporary.name)
    participant = Participant(config, output)
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                raise ValueError('Participant request exceeds the protocol line limit')
            request = strict_loads(line.decode('utf-8'))
            if type(request) is not dict or request.get('protocol') != PROTOCOL:
                raise ValueError('Unsupported participant request')
            if request.get('type') == 'close':
                break
            response = participant.act(request)
            print(json.dumps(response, ensure_ascii=False, allow_nan=False), flush=True)
    finally:
        participant.close()
        if temporary is not None:
            temporary.cleanup()


if __name__ == '__main__':
    main()
