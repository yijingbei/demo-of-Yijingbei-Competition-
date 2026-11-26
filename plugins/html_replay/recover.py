"""Recover old observations by replaying archived actions on their exact source.

No participant policy is called. Every native receipt and final entity must match.
This optional command runs the engine; the normal HTML exporter never does.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys


def recover(case, source, output):
    from exporter import read_json, sha256, runtime_fingerprint, scenario_folder
    original = read_json(case/'result.json')
    from record_io import trace_path,verify,open_text
    original_trace=trace_path(case,original)
    if verify(original_trace,None,original.get('trace_storage_sha256')) != original.get('trace_sha256'):
        raise ValueError('Original trace SHA-256 mismatch')
    if runtime_fingerprint(source) != original.get('runtime_sha256'):
        raise ValueError('Recovery requires the exact frozen runtime; source mismatch is never allowed')
    sys.path[:0] = [str(source), str(source/'engine/source_codes')]
    from omd_competition.runtime import create_session
    from omd_competition.evaluate import submit
    from recording import ReplayRecorder
    from bundle import capture_scene, finish_replay
    output.mkdir(parents=True, exist_ok=False)
    session = gateway = recorder = None
    try:
        session, gateway = create_session(original['scenario'], original['seed'],
            'episode.'+original['scenario']+'.'+str(original['seed']), referee=True)
        slots = {s.id:s for s in session.resolved.controller_slots}
        recorder = ReplayRecorder(output, original['scenario'], original['runtime_sha256'], session.resolved.world.tick_seconds)
        recorder.capture(session)
        matched = 0
        with open_text(original_trace) as stream:
            for line in stream:
                row = json.loads(line)
                tick = session.world_view.tick
                if row['tick'] != tick+1:
                    raise ValueError('Archived action tick differs from session')
                for action in row.get('opponents', [])+row.get('participants', []):
                    if action.get('response') is not None:
                        submit(gateway, session, slots[action['controller_slot']], action['response'])
                receipt = session.step(operation_id=f'tick.{tick}', expected_tick=tick)
                if receipt.receipt_hash != row['receipt_hash']:
                    raise ValueError(f'Original receipt differs at tick {tick}; recovered data rejected')
                matched += 1
                recorder.capture(session)
        if session.world_view.tick != original['final_tick']:
            raise ValueError('Recovered final tick differs')
        expected = original.get('referee_final_entities', {})
        if not expected:
            raise ValueError('Original final snapshot required for recovery verification')
        final = {e.id:dict(faction_id=e.faction_id, position_m=list(e.state.position_m),
            health=e.state.health, lifecycle=e.state.lifecycle) for e in session.world_view.entities_stable()}
        if final != expected:
            raise ValueError('Recovered final entities differ; recovered data rejected')
        result = dict(original)
        result['replay_recording'] = recorder.close(session.world_view.tick, original['status']=='completed')
        recorder = None
        result['replay_scene'] = capture_scene(output, scenario_folder(source,original['scenario']), source,
            original['scenario'], original['runtime_sha256'])
        result['replay_recovery'] = dict(original_result_sha256=sha256(case/'result.json'),
            original_trace_sha256=original['trace_sha256'], all_receipts_identical=True,
            matched_receipts=matched, final_entities_identical=True, participant_policy_invoked=False)
        shutil.copy2(original_trace, output/original_trace.name)
        (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        report = finish_replay(output,result,source)
        if report['status']!='ready':
            raise ValueError(report['error'])
        return dict(output=str(output),matched_receipts=matched,verified=True)
    finally:
        if recorder is not None:recorder.stream.close()
        if gateway is not None and session is not None:gateway.close(session.session_id)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case',type=Path,help='Original result.json + trace.jsonl folder')
    parser.add_argument('--source-root',required=True,type=Path,help='Exact source that generated original receipts')
    parser.add_argument('-o','--output',required=True,type=Path,help='New folder; original records stay untouched')
    args=parser.parse_args(argv)
    try:
        print(json.dumps(recover(args.case.resolve(),args.source_root.resolve(),args.output.resolve())))
        return 0
    except Exception as error:
        print(f'Replay recovery failed: {type(error).__name__}: {error}',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
