"""One offline player per result collection, with losslessly compressed match data."""
import gzip
import hashlib
from html import escape
import json
from pathlib import Path
import re
import tempfile
import os

HERE=Path(__file__).resolve().parent


def atomic_bytes(path,raw):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.replay-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw)
        os.replace(tmp,path)
    finally:
        if Path(tmp).exists():Path(tmp).unlink()


def write_pack(path,pack):
    raw=json.dumps(pack,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    compressed=gzip.compress(raw,compresslevel=6,mtime=0)
    atomic_bytes(path,compressed)
    return dict(file=Path(path).name,encoding='gzip',sha256=hashlib.sha256(compressed).hexdigest(),
                content_sha256=hashlib.sha256(raw).hexdigest(),stored_bytes=len(compressed),uncompressed_bytes=len(raw))


def render_viewer():
    from exporter import VERSION
    from presentation import TERMS, display_text
    template=display_text((HERE/'template.html').read_text(encoding='utf-8'), english=False)
    source='\n'.join((HERE/name).read_text(encoding='utf-8') for name in ('presentation.js','controls.js','tasks.js','ranges.js','player.js'))
    source=source.replace('__DISPLAY_TERMS__',json.dumps(sorted(TERMS.items(),key=lambda p:len(p[0]),reverse=True),ensure_ascii=False))
    values=dict(THREE_JS=(HERE/'vendor/three.min.js').read_text(encoding='utf-8'),PLAYER_JS=source)
    template=re.sub(r'\{\{(THREE_JS|PLAYER_JS)\}\}',lambda m:values[m[1]],template)
    shell=display_text((HERE/'library.html').read_text(encoding='utf-8'), english=False)
    values=dict(VERSION=escape(VERSION),TEMPLATE=json.dumps(template,ensure_ascii=False).replace('<','\\u003c'),
                LOADER=(HERE/'library.js').read_text(encoding='utf-8'))
    return re.sub(r'\{\{(VERSION|TEMPLATE|LOADER)\}\}',lambda m:values[m[1]],shell)


def ensure_viewer(folder):
    path=Path(folder)/'replay-viewer.html'
    html=render_viewer()
    if path.exists():
        existing=path.read_text(encoding='utf-8')
        if existing==html:return path
        if '<!-- competition-shared-replay-viewer@1 -->' not in existing:
            raise ValueError('Refusing to replace an unrelated replay-viewer.html')
    atomic_bytes(path,html.encode('utf-8'))
    return path
