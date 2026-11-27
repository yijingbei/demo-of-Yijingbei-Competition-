"""Run the independent LLM + OR strategy through the existing evaluator."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys

from . import STRATEGY_REVISION
from .config import StrategyConfig
from .io import atomic_json

ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/llm-or.example.json')
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--seed', type=int, default=2026102302)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--replay-format', choices=('html', 'shared', 'none'), default='html')
    parser.add_argument('--max-ticks', type=int, help='Truncated diagnostic only; not a full-game score')
    parser.add_argument('--prompt-key', action='store_true', help='Read the API key without terminal echo')
    args = parser.parse_args(argv)
    config = StrategyConfig.load(args.config)
    output = args.output.resolve()
    if output.exists():
        parser.error('Output already exists; use a fresh directory to preserve evidence')
    if args.max_ticks is not None and args.max_ticks < 1:
        parser.error('--max-ticks must be positive')
    from ..runtime import entry
    try:
        scenario = entry(args.scenario)
    except StopIteration:
        parser.error('Unknown demo scenario: ' + args.scenario)
    if scenario['track'] != 'interception':
        parser.error('This strategy requires the interception track')
    if args.prompt_key:
        os.environ['OPENMD_LLM_API_KEY'] = getpass.getpass('Model API key: ')
    if not os.environ.get('OPENMD_LLM_API_KEY'):
        parser.error('Set OPENMD_LLM_API_KEY or use --prompt-key')
    names = ['run_llm_or.py', 'omd_competition/transport.py',
             'omd_competition/participant_brief.py',
             'omd_competition/coastal_navigation.py', 'omd_competition/contact_aliases.py']
    names.extend(p.relative_to(ROOT).as_posix() for p in sorted(Path(__file__).parent.rglob('*.py'))
                 if 'tests' not in p.parts and '__pycache__' not in p.parts)
    provenance = {'strategy_revision': STRATEGY_REVISION, 'config': config.as_dict(),
        'api_prices_known': config.prices_known, 'architecture': 'LLM + operations research',
        'rule_action_fallback': False, 'native_rules_changed': False,
        'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}}
    digest = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
    # The evaluator owns exclusive creation of the result directory. Pass a
    # credential-free configuration snapshot in the child environment instead
    # of creating that directory before run_episode.
    os.environ['OPENMD_LLM_OR_CONFIG_JSON'] = json.dumps(config.as_dict())
    command = [sys.executable, '-B', '-m', 'omd_competition.llm_or.agent',
               '--log-dir', str(output / 'strategy')]
    from ..evaluate import run_episode
    try:
        result = run_episode(args.scenario, 'llm-or', args.seed, output,
            command=command, submission_sha256=digest, resource_group='api', max_ticks=args.max_ticks,
            replay_format=args.replay_format, replay_keep=0, keep_replay=True)
    finally:
        if output.is_dir():
            atomic_json(output / 'effective-config.json', config.as_dict())
            atomic_json(output / 'strategy-provenance.json', provenance)
    summary_path = output / 'strategy/strategy-summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
    print(json.dumps({**{k: result.get(k) for k in ('scenario', 'status', 'utility', 'task_success', 'final_tick', 'error')},
        'api_prices_known': config.prices_known, 'api_usage': summary.get('api_usage'),
        'llm_plans_accepted': summary.get('llm_plans_accepted', 0),
        'validation_scope': 'bounded_probe' if args.max_ticks else 'natural_terminal',
        'output': str(output)}, ensure_ascii=False), flush=True)
    return int(result['status'] not in ('completed', 'bounded_probe')
               or not summary.get('llm_plans_accepted'))


if __name__ == '__main__':
    raise SystemExit(main())
