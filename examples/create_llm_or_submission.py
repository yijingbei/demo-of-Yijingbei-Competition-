"""Create a self-contained local API-group submission under local-results/."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omd_competition.llm_or.config import StrategyConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/llm-or.example.json')
    parser.add_argument('--team-id', default='llm-or-example')
    parser.add_argument('--output', type=Path, default=ROOT / 'local-results/llm-or-submission')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'local-results'):
        parser.error('Generated submission files belong under local-results/')
    if output.exists():
        parser.error('Output exists; choose a new directory')
    if not args.team_id.strip():
        parser.error('--team-id must not be empty')
    config = StrategyConfig.load(args.config)
    output.mkdir(parents=True)
    # These helpers serialize or validate public DTOs; no rules baseline,
    # scenario configuration, engine, replay, or secret is included.
    names = ['omd_competition/__init__.py',
             'omd_competition/transport.py', 'omd_competition/participant_brief.py',
             'omd_competition/wave_schedule.py', 'omd_competition/coastal_navigation.py',
             'omd_competition/contact_aliases.py']
    names.extend(p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'omd_competition/llm_or').rglob('*.py'))
                 if p.name != 'runner.py' and 'tests' not in p.parts and '__pycache__' not in p.parts)
    for name in names:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (output / 'participant.py').write_text(
        'from omd_competition.llm_or.agent import main\n\nif __name__ == "__main__":\n    main()\n', encoding='utf-8')
    effective = output / 'strategy-config.json'
    effective.write_text(json.dumps(config.as_dict(), indent=2) + '\n', encoding='utf-8')
    artifacts = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(output.rglob('*')) if p.is_file()}
    submission = {'team_id': args.team_id, 'track': 'interception', 'resource_group': 'api',
        'protocol': 'openmd-competition@0.1',
        'api_models': [{'provider': urlsplit(config.base_url).hostname, 'model': config.model}],
        'entrypoint': [sys.executable, '-B', str(output / 'participant.py'), '--config', str(effective)],
        'artifacts': artifacts}
    path = output / 'submission.json'
    path.write_text(json.dumps(submission, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(path)


if __name__ == '__main__':
    main()
