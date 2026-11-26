"""Standalone replay plugin CLI, usable from any working directory."""
import argparse
import json
from pathlib import Path
import sys

from exporter import (VERSION, ReplayError, discover, export_case, make_pack,
                      read_json, render_html, runtime_fingerprint, scenario_folder, write_html)


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'import-tuning':
        from import_tuning import main as import_tuning
        return import_tuning(argv[1:])
    if argv and argv[0] in {'viewer','pin','unpin','prune'}:
        from storage_cli import main as storage_main
        try:return storage_main(argv)
        except (OSError,ValueError) as error:
            print(f'Replay storage failed: {error}',file=sys.stderr);return 2
    parser = argparse.ArgumentParser(description='Convert recorded competition tests into offline interactive HTML. No simulation is started.')
    parser.add_argument('inputs', nargs='+', help='Single test folder/result.json/trace.jsonl, or matrix directory (recursive).')
    parser.add_argument('-o', '--output', required=True, type=Path, help='Standalone .html, or .json.gz replay data for the shared viewer')
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2], help='Matching . source root')
    parser.add_argument('--scenario-dir', type=Path, help='Explicit scenario.yaml + public_brief.json folder; only one scenario allowed')
    parser.add_argument('--scenario', action='append', help='Only include this scenario ID (repeatable)')
    parser.add_argument('--policy', action='append', help='Only include this policy (repeatable)')
    parser.add_argument('--allow-source-mismatch', action='store_true', help='Explicitly allow mismatched scenario version for diagnostic viewing; warning remains in HTML')
    parser.add_argument('--skip-invalid', action='store_true', help='Skip invalid cases, disclose skips in HTML and stderr')
    parser.add_argument('--force', action='store_true', help='Replace existing output HTML')
    parser.add_argument('--title', default='测试对局 · 交互回放')
    parser.add_argument('--participant-only', action='store_true',
                        help='Export a physically separate authorized-observation artifact for identification tasks')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args(argv)
    try:
        folders = discover(args.inputs)
        episodes = []; skipped = []; ids = set()
        source = args.source_root.resolve()
        fingerprint = None
        for folder in folders:
            try:
                result = read_json(folder / 'result.json')
                if args.scenario and result.get('scenario') not in args.scenario:
                    continue
                if args.policy and result.get('policy') not in args.policy:
                    continue
                ids.add(result['scenario'])
                if args.scenario_dir and len(ids) > 1:
                    raise ReplayError('--scenario-dir is only valid for one scenario; filter with --scenario or use --source-root')
                from bundle import recorded_scene
                captured = recorded_scene(folder, result)
                if captured:
                    scene = captured['folder']
                else:
                    if not args.scenario_dir and fingerprint is None:
                        fingerprint = runtime_fingerprint(source)
                    scene = args.scenario_dir.resolve() if args.scenario_dir else scenario_folder(source, result['scenario'])
                episodes.append(export_case(folder, scene, fingerprint, args.allow_source_mismatch, source_root=source))
            except (ReplayError, OSError, ValueError, KeyError) as error:
                if not args.skip_invalid:
                    raise ReplayError(f'{folder.name}: {error}') from error
                skipped.append({'case': folder.name, 'error': str(error)})
                print(f'SKIPPED {folder.name}: {error}', file=sys.stderr)
        if skipped:
            for episode in episodes:
                episode['meta']['warnings'].append(f'本文件跳过 {len(skipped)} 个无效测试；详见诊断的 skippedCases。')
                episode['checks']['skippedCases'] = skipped
        if args.participant_only:
            if any('engagement' not in episode for episode in episodes):
                raise ReplayError('Participant-only export requires an identification task')
            from participant import participant_episode
            episodes = [participant_episode(episode) for episode in episodes]
            # Input folder ordering may encode organizer deployment indices.
            episodes.sort(key=lambda episode: episode['meta']['key'])
        pack = make_pack(episodes, [] if args.participant_only else skipped)
        if args.output.name.endswith('.json.gz'):
            if args.output.exists() and not args.force:raise ReplayError('Output already exists; choose another path or use --force')
            from library import write_pack,ensure_viewer
            write_pack(args.output,pack);ensure_viewer(args.output.parent)
        else:write_html(args.output, render_html(pack, args.title), args.force)
        print(json.dumps({'output': str(args.output.resolve()), 'scenarios': len(pack['scenarios']),
                          'episodes': len(episodes), 'skipped': len(skipped),
                          'warnings': sum(len(e['meta']['warnings']) for e in episodes),
                          'simulations_started': 0}, ensure_ascii=False))
        return 0
    except (ReplayError, OSError, ValueError) as error:
        print(f'Replay export failed: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
