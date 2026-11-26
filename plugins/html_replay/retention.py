"""Expire optional debug replay files only; never delete results or action logs."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import time

SCHEMA='competition-replay-storage@1'
FRAME_FILES={'replay.frames.jsonl','replay.frames.jsonl.gz'}
OPTIONAL={'replay.pack.json.gz','replay.html'} | FRAME_FILES


def utc():return datetime.now(timezone.utc).isoformat()


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def write(path,value):
    if __package__:
        from .library import atomic_bytes
    else:
        from library import atomic_bytes
    atomic_bytes(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))


def pin(folder,enabled=True):
    folder=Path(folder).resolve()
    if not (folder/'result.json').is_file():raise ValueError('Pin requires an existing result.json')
    path=folder/'replay.keep.json'
    if path.is_symlink():raise ValueError('Replay pin must not be a symlink')
    if enabled:write(path,dict(schema_version='competition-replay-pin@1',pinned_utc=utc()))
    elif path.exists():path.unlink()


def complete_recording(result):
    """Only a result-bound, complete tick sequence is eligible for expiration."""
    recording=result.get('replay_recording') or {}
    final_tick=result.get('final_tick')
    return (isinstance(recording,dict) and type(final_tick) is int and final_tick>=0
            and recording.get('schema_version')=='competition-replay-frame@1'
            and recording.get('every_tick') is True and recording.get('complete') is True
            and recording.get('initial_tick')==0 and recording.get('last_tick')==final_tick
            and type(recording.get('frames')) is int and recording['frames']==final_tick+1
            and recording.get('file') in FRAME_FILES)


def register(folder,result,report,keep_last=20,important=False,scope='debug'):
    folder=Path(folder).resolve()
    if keep_last<0:raise ValueError('Replay keep-last must be nonnegative')
    files={}
    names=[report.get('file')] if report.get('status')=='ready' else []
    # format=none intentionally has no export report. Its original full recording
    # still occupies disk and must obey the same opt-in debug retention policy.
    # Export failures and incomplete recordings remain protected diagnostic data.
    if (not report or report.get('status')=='ready') and complete_recording(result):
        if __package__:
            from .record_io import record_path,verify
        else:
            from record_io import record_path,verify
        recording=result['replay_recording']
        expected=recording.get('sha256')
        if not isinstance(expected,str) or len(expected)!=64:
            raise ValueError('Replay recording content SHA-256 is missing or invalid')
        path=record_path(folder,recording['file'],'frames')
        verify(path,expected,recording.get('storage_sha256'))
        names.append(path.name)
    for name in names:
        if name in OPTIONAL and (folder/name).is_file():
            files[name]=hashlib.sha256((folder/name).read_bytes()).hexdigest()
    marker=dict(schema_version=SCHEMA,state='available' if files else 'unavailable',scope=scope,
                collection=str(folder.parent),created_ns=time.time_ns(),created_utc=utc(),keep_last=keep_last,
                result_sha256=hashlib.sha256((folder/'result.json').read_bytes()).hexdigest(),optional_files=files)
    write(folder/'replay.storage.json',marker)
    if important:pin(folder)


def prune(root,keep_last=20,apply=False):
    root=Path(root).resolve()
    if keep_last<0:raise ValueError('Replay keep-last must be nonnegative')
    report=dict(schema_version='competition-replay-retention@1',root=str(root),keep_last=keep_last,
                applied=apply,removed_bytes=0,candidates=[],skipped=[],errors=[])
    if keep_last==0:
        report['disabled']=True;return report
    if not root.is_dir():raise ValueError('Retention root must be an existing result collection')
    # Batch collections are reproducibility evidence, never rolling debug storage.
    if any((root/n).exists() for n in ('plan.json','summary.json')):
        report['protected_collection']=True;return report
    lock=root/'.replay-maintenance.lock'
    try:lock.mkdir()
    except FileExistsError:
        report['busy']=True;return report
    try:
        candidates=[]
        for folder in root.iterdir():
            if not folder.is_dir() or folder.is_symlink() or folder.resolve().parent!=root:continue
            try:
                marker_path=folder/'replay.storage.json'
                if not marker_path.is_file() or marker_path.is_symlink():continue
                marker=read(marker_path)
                if marker.get('schema_version')!=SCHEMA or marker.get('state')!='available' or marker.get('scope')!='debug' or marker.get('collection')!=str(root):continue
                if marker.get('keep_last')==0:
                    report['skipped'].append(dict(case=folder.name,reason='expiration-disabled'));continue
                if (folder/'replay.keep.json').exists():report['skipped'].append(dict(case=folder.name,reason='pinned'));continue
                result_path=folder/'result.json'
                if result_path.is_symlink():raise ValueError('Result file is a symlink')
                if hashlib.sha256(result_path.read_bytes()).hexdigest()!=marker['result_sha256']:raise ValueError('Result changed after registration')
                result=read(result_path)
                if result.get('status')!='completed' or result.get('task_success') is not True or result.get('competition_accepted'):
                    report['skipped'].append(dict(case=folder.name,reason='failure-incomplete-or-accepted'));continue
                if not complete_recording(result):
                    report['skipped'].append(dict(case=folder.name,reason='incomplete-recording'));continue
                candidates.append((marker['created_ns'],folder,marker,result))
            except (OSError,ValueError,KeyError) as error:report['errors'].append(dict(case=folder.name,error=str(error)))
        candidates.sort(key=lambda x:(x[0],x[1].name),reverse=True)
        for _,folder,marker,result in candidates[keep_last:]:
            try:
                if __package__:
                    from .record_io import trace_path,verify
                else:
                    from record_io import trace_path,verify
                verify(trace_path(folder,result),result['trace_sha256'],result.get('trace_storage_sha256'))
                targets=[]
                for name,expected in marker['optional_files'].items():
                    if name not in OPTIONAL:raise ValueError('Retention marker includes a non-replay file')
                    path=folder/name
                    if path.is_symlink() or path.resolve().parent!=folder:raise ValueError('Retention target escapes case directory')
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('Optional replay changed after registration')
                    targets.append(path)
                item=dict(case=folder.name,files=[p.name for p in targets],bytes=sum(p.stat().st_size for p in targets))
                report['candidates'].append(item)
                if apply:
                    marker.update(state='expiring',expired_utc=utc(),expired_reason=f'超过最近 {keep_last} 局成功调试回放保留量；成绩与行动日志仍保留')
                    write(folder/'replay.storage.json',marker)
                    for path in targets:path.unlink()
                    marker['state']='expired';write(folder/'replay.storage.json',marker)
                    report['removed_bytes']+=item['bytes']
            except (OSError,ValueError,KeyError,EOFError) as error:report['errors'].append(dict(case=folder.name,error=str(error)))
        if apply:write(root/'replay-retention.json',report)
        return report
    finally:
        lock.rmdir()
