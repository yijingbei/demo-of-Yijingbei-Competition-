"""Entrypoint and packaging regressions; no model service is contacted."""
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from omd_competition.llm_or.config import StrategyConfig
from omd_competition.llm_or.controller import BudgetedSource
from omd_competition.llm_or import runner

ROOT = Path(__file__).resolve().parents[3]


class EntrypointTests(unittest.TestCase):
    def test_evaluator_creates_result_directory_before_participant_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'episode'
            def run_episode(scenario, policy, seed, destination, **kwargs):
                self.assertFalse(destination.exists())
                self.assertEqual(kwargs['resource_group'], 'api')
                settings = json.loads(os.environ['OPENMD_LLM_OR_CONFIG_JSON'])
                self.assertEqual(settings['min_plan_interval'], 20)
                (destination / 'strategy').mkdir(parents=True)
                (destination / 'strategy/strategy-summary.json').write_text(
                    json.dumps({'llm_plans_accepted': 1, 'api_usage': {}}), encoding='utf-8')
                return {'status': 'bounded_probe', 'scenario': scenario, 'final_tick': 3}
            modules = {'omd_competition.runtime': SimpleNamespace(entry=lambda _: {'track': 'interception'}),
                       'omd_competition.evaluate': SimpleNamespace(run_episode=run_episode)}
            with patch.dict(sys.modules, modules), patch.dict(os.environ, {'OPENMD_LLM_API_KEY': 'offline-placeholder'}), redirect_stdout(StringIO()):
                result = runner.main(['--config', str(ROOT / 'configs/llm-or.example.json'),
                    '--scenario', 'IE-01-SINGLE-TARGET', '--output', str(output), '--max-ticks', '3'])
            self.assertEqual(result, 0)
            self.assertTrue((output / 'strategy-provenance.json').is_file())
            self.assertTrue((output / 'effective-config.json').is_file())

    def test_generated_participant_imports_without_source_checkout_on_pythonpath(self):
        results = ROOT / 'local-results'
        results.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='llm-or-packet-test-', dir=results) as temporary:
            output = Path(temporary) / 'submission'
            run = subprocess.run([sys.executable, '-B', str(ROOT / 'examples/create_llm_or_submission.py'),
                '--output', str(output)], cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            packet = json.loads((output / 'submission.json').read_bytes())
            self.assertEqual(packet['resource_group'], 'api')
            self.assertTrue(packet['api_models'])
            self.assertFalse(any('/tests/' in p for p in packet['artifacts']))
            env = dict(os.environ)
            env.pop('PYTHONPATH', None)
            env['OPENMD_LLM_API_KEY'] = 'offline-placeholder'
            run = subprocess.run(packet['entrypoint'], cwd=temporary, env=env,
                input=json.dumps({'protocol': 'openmd-competition@0.1', 'type': 'close'})+'\n',
                capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, '')

    def test_diagnostics_redact_the_current_key(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'OPENMD_LLM_API_KEY': 'offline-private-value'}):
            source = BudgetedSource(StrategyConfig(), post=lambda *_: {})
            source.diagnostic_path = Path(temporary) / 'events.jsonl'
            source.record_event('offline_test', {'scenario_id': 'offline'}, {'tick': 0}, note=source.key)
            text = source.diagnostic_path.read_text(encoding='utf-8')
            self.assertNotIn(source.key, text)
            self.assertIn('[REDACTED]', text)


if __name__ == '__main__':
    unittest.main()
