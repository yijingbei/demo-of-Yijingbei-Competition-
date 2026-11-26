"""Lossless record streams with separate content and stored-byte SHA-256 values."""
import gzip
import hashlib
from pathlib import Path


def digest(path, decoded=False):
    h=hashlib.sha256()
    opener=gzip.open if decoded and path.suffix=='.gz' else open
    with opener(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


class RecordWriter:
    """Write each original UTF-8 byte once; never subsample or round positions."""
    def __init__(self, path, compression='gzip'):
        if compression not in {'gzip','none'}:raise ValueError('Unsupported record compression')
        self.path=Path(str(path)+('.gz' if compression=='gzip' else ''))
        self.compression=compression
        self.raw=self.path.open('xb')
        self.stream=gzip.GzipFile(fileobj=self.raw,mode='wb',filename='',mtime=0,compresslevel=6) if compression=='gzip' else self.raw
        self.hash=hashlib.sha256();self.bytes=0;self.closed=False

    def write(self, data):
        if isinstance(data,str):data=data.encode('utf-8')
        count=self.stream.write(data);self.hash.update(data);self.bytes+=len(data);return count

    def close(self):
        if not self.closed:
            try:self.stream.close()
            finally:self.raw.close();self.closed=True

    def metadata(self):
        if not self.closed:raise ValueError('Close record before binding its stored bytes')
        return dict(file=self.path.name,encoding=self.compression,sha256=self.hash.hexdigest(),
                    storage_sha256=digest(self.path),uncompressed_bytes=self.bytes,stored_bytes=self.path.stat().st_size)

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def record_path(folder, file, kind):
    names={'trace':{'trace.jsonl','trace.jsonl.gz'},'frames':{'replay.frames.jsonl','replay.frames.jsonl.gz'}}
    if file not in names[kind]:raise ValueError(f'Invalid {kind} record file')
    root=Path(folder).resolve();path=root/file
    if path.is_symlink() or path.resolve().parent!=root:raise ValueError('Record path escapes result folder')
    return path


def trace_path(folder,result):
    return record_path(folder,result.get('trace_file','trace.jsonl'),'trace')


def open_text(path):
    return gzip.open(path,'rt',encoding='utf-8-sig') if path.suffix=='.gz' else path.open(encoding='utf-8-sig')


def text_lines(path):
    with open_text(path) as stream:
        yield from stream


def verify(path,expected,stored=None):
    if stored and digest(path)!=stored:raise ValueError(f'{path.name} stored SHA-256 mismatch')
    actual=digest(path,decoded=True)
    if expected and actual!=expected:raise ValueError(f'{path.name} content SHA-256 mismatch')
    return actual
