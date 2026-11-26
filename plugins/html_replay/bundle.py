"""Capture and verify portable replay scenery without importing the engine."""
import hashlib
import json
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_scene(output, scene_folder, source_root, scenario, runtime_sha256, *, engine_root=None):
    import yaml
    if __package__:
        from .geography import load_geography
        from .ranges import load_ranges
    else:
        from geography import load_geography
        from ranges import load_ranges
    target = Path(output) / 'replay.scene'
    target.mkdir(exist_ok=False)
    for name in ('scenario.yaml', 'public_brief.json'):
        (target/name).write_bytes((scene_folder/name).read_bytes())
    if (scene_folder/'opponent.json').is_file():
        (target/'opponent.json').write_bytes((scene_folder/'opponent.json').read_bytes())
    scene = yaml.safe_load((target/'scenario.yaml').read_text(encoding='utf-8-sig'))['scenario']
    geometry = load_geography(source_root, scenario, scene.get('world', {}).get('map_ref'),
                              engine_root=engine_root)
    (target/'geography.json').write_text(json.dumps(geometry, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    ranges = load_ranges(source_root, scenario, scene)
    (target/'ranges.json').write_text(json.dumps(ranges, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    manifest = dict(schema_version='competition-replay-scene@1', scenario=scenario,
        runtime_sha256=runtime_sha256, files={name: digest(target/name)
            for name in ('scenario.yaml', 'public_brief.json', 'geography.json', 'ranges.json', 'opponent.json')
            if (target/name).is_file()})
    path = target/'manifest.json'
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return dict(file='replay.scene/manifest.json', sha256=digest(path), schema_version=manifest['schema_version'])


def recorded_scene(folder, result):
    """A bound snapshot takes precedence over a changed or unavailable checkout."""
    metadata = result.get('replay_scene')
    if not metadata:
        return None
    if metadata.get('error') or metadata.get('file') != 'replay.scene/manifest.json':
        raise ValueError('Replay scene metadata invalid')
    target = Path(folder)/'replay.scene'
    manifest_path = target/'manifest.json'
    if digest(manifest_path) != metadata.get('sha256'):
        raise ValueError('Replay scene manifest SHA-256 mismatch')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('schema_version') != 'competition-replay-scene@1'
            or manifest.get('scenario') != result['scenario']
            or manifest.get('runtime_sha256') != result.get('runtime_sha256')):
        raise ValueError('Replay scene scenario/runtime mismatch')
    required = {'scenario.yaml', 'public_brief.json', 'geography.json'}
    members = set(manifest.get('files', {}))
    if not required.issubset(members) or members - (required | {'ranges.json','opponent.json'}):
        raise ValueError('Replay scene file manifest invalid')
    for name, expected in manifest['files'].items():
        if digest(target/name) != expected:
            raise ValueError(f'Replay scene {name} SHA-256 mismatch')
    ranges = json.loads((target/'ranges.json').read_text(encoding='utf-8')) if 'ranges.json' in manifest['files'] else None
    if ranges is not None:
        if __package__:
            from .ranges import SCHEMA
        else:
            from ranges import SCHEMA
        if ranges.get('schemaVersion') != SCHEMA:
            raise ValueError('Replay range schema invalid')
    return dict(folder=target, geography=json.loads((target/'geography.json').read_text(encoding='utf-8')), ranges=ranges)


def finish_replay(output, result, source_root, enabled=True, format='html'):
    """Export after result.json is closed. Replay errors never change the score."""
    if not enabled:
        return None
    import sys
    plugin = Path(__file__).resolve().parent
    if str(plugin) not in sys.path:
        sys.path.insert(0, str(plugin))
    from exporter import export_case, make_pack, render_html, write_html
    try:
        snapshot = recorded_scene(output, result)
        if snapshot is None:
            raise ValueError('No portable scene snapshot was recorded')
        episode = export_case(Path(output), snapshot['folder'], source_root=source_root)
        pack=make_pack([episode])
        if result.get('engagement_audit') is not None:
            from participant import participant_episode
            participant_path = Path(output) / 'participant-replay.html'
            write_html(participant_path, render_html(make_pack([participant_episode(episode)]),
                title='参赛者授权观察 · 浮标识别回放'), force=False)
        if format=='shared':
            from library import write_pack,ensure_viewer
            path=Path(output)/'replay.pack.json.gz'
            stored=write_pack(path,pack)
            viewer=ensure_viewer(Path(output).parent)
        elif format=='html':
            path = Path(output)/'replay.html'
            write_html(path, render_html(pack), force=False)
            stored={};viewer=None
        else:raise ValueError('Unsupported replay format')
        report = dict(**{k:v for k,v in stored.items() if k not in {'file','sha256'}},
                      status='ready', file=path.name, sha256=digest(path),format=format,
                      viewer='../'+viewer.name if viewer else None,
                      every_tick=bool(result.get('replay_recording')), portable=True)
    except Exception as error:
        report = dict(status='failed', error=f'{type(error).__name__}: {error}')
        print(f'Replay export failed for {output}: {report["error"]}', file=sys.stderr)
    (Path(output)/'replay-export.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return report


def matrix_index(output, rows):
    """Link per-case files without embedding all recordings a second time."""
    from html import escape
    entries=[]
    for row in sorted(rows,key=lambda r:r['case_index']):
        name=Path(row['output']).name if row.get('output') else f"case-{row['case_index']:03}"
        path=Path(output)/name/'replay.html'
        shared=Path(output)/name/'replay.pack.json.gz'
        label=escape(f"{row['scenario']} · {row['policy']} · {row['seed']} · {row['status']}")
        entries.append(f'<li><a href="replay-viewer.html?case={name}">{label}</a> · 共用播放器中选择结果文件夹</li>' if shared.is_file() else f'<li><a href="{name}/replay.html">{label}</a></li>' if path.is_file()
                       else f'<li>{label} · 未生成回放，请检查该局 replay-export.json</li>')
    (Path(output)/'replay-index.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<title>实验回放列表</title><style>body{background:#0c1924;color:#e5eff2;font:16px sans-serif;max-width:1000px;margin:50px auto;padding:24px}a{color:#74dbe5}li{margin:16px 0}</style>'
        '<h1>实验回放列表</h1><p>点击查看各局；失败和异常局同样列出。复制时保留各 case 文件夹。</p><ul>'
        +''.join(entries)+'</ul></html>',encoding='utf-8')
