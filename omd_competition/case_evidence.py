"""Bind collected summary rows to existing result and trace bytes.

This verifies record consistency, not the authenticity of execution. A writer
that can replace all records together still needs an independent trusted receipt.
Only the standard library is used; no engine or replay module is imported.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import zlib


_CHUNK = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TRACE_ENCODINGS = {"trace.jsonl": "none", "trace.jsonl.gz": "gzip"}
_COLLECTOR_FIELDS = frozenset({"case_index", "deployment"})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: " + key)
        result[key] = value
    return result


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _invalid_constant(value):
    raise ValueError("nonfinite JSON constant: " + value)


def decode_json(raw, *, label="evidence"):
    """Decode UTF-8 JSON without duplicate keys or nonfinite numbers."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            raise ValueError("JSON input must be bytes or text")
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_float=_finite_float, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError(f"{label}: invalid JSON: {error}") from error


def _check_json_value(value):
    kind = type(value)
    if kind is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _check_json_value(item)
    elif kind is list:
        for item in value:
            _check_json_value(item)
    elif kind is float:
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON number")
    elif kind not in (str, int, bool, type(None)):
        raise ValueError("unsupported JSON value")


def _canonical(value):
    try:
        _check_json_value(value)
        return json.dumps(value, sort_keys=True, ensure_ascii=True,
                          allow_nan=False, separators=(",", ":")).encode("ascii")
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError(f"invalid evidence value: {error}") from error


def _same_value(left, right):
    # Canonical JSON preserves bool/int/float distinctions that Python == loses.
    return _canonical(left) == _canonical(right)


def _identity(row):
    if type(row) is not dict:
        raise ValueError("case identity must be an object")
    scenario, policy = row.get("scenario"), row.get("policy")
    seed, deployment = row.get("seed"), row.get("deployment", 0)
    if (type(scenario) is not str or not scenario or type(policy) is not str or not policy
            or type(seed) is not int or seed < 0
            or type(deployment) is not int or deployment < 0):
        raise ValueError("invalid case scenario/policy/seed/deployment identity")
    return scenario, deployment, seed, policy


def _is_link(info):
    # Windows junctions and other reparse points must not bypass is_symlink().
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400))


def _guarded_stat(root, relative):
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in (".", "..") for part in path.parts):
        raise ValueError("case evidence path must stay inside its matrix")
    current = root
    try:
        info = current.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("case evidence matrix root changed or is linked")
        for number, part in enumerate(path.parts):
            current = current / part
            info = current.lstat()
            if _is_link(info):
                raise ValueError("linked case evidence path is not allowed")
            required = stat.S_ISREG if number == len(path.parts) - 1 else stat.S_ISDIR
            if not required(info.st_mode):
                raise ValueError("case evidence path has the wrong file type")
        return current, info
    except OSError as error:
        raise ValueError(f"missing or unreadable case evidence: {relative}") from error


def _stat_token(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _guarded_file(root, relative):
    """Check path components and detect ordinary replacement during a read."""
    path, before = _guarded_stat(root, relative)
    stream = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _stat_token(before) != _stat_token(opened):
            raise ValueError("case evidence changed while opening")
        yield stream
        _, after_path = _guarded_stat(root, relative)
        after_open = os.fstat(stream.fileno())
        if (_stat_token(opened) != _stat_token(after_open)
                or _stat_token(opened) != _stat_token(after_path)):
            raise ValueError("case evidence changed while reading")
    except OSError as error:
        raise ValueError(f"unreadable case evidence: {relative}") from error
    finally:
        if stream is not None:
            stream.close()


@dataclass(frozen=True)
class _FileRecord:
    relative_path: str
    sha256: str
    size: int


def _hash_file(root, relative):
    digest, size = hashlib.sha256(), 0
    with _guarded_file(root, relative) as stream:
        while block := stream.read(_CHUNK):
            digest.update(block)
            size += len(block)
    return _FileRecord(relative, digest.hexdigest(), size)


@dataclass(frozen=True)
class CaseEvidenceSnapshot:
    """Original-byte consistency snapshot; not a trusted execution attestation."""
    digest: str
    case_count: int
    _matrix_root: Path = field(repr=False)
    _files: tuple = field(repr=False)

    def require_unchanged(self):
        for original in self._files:
            current = _hash_file(self._matrix_root, original.relative_path)
            if current != original:
                raise ValueError("case evidence changed during audit: " + original.relative_path)


def _read_result(root, relative):
    with _guarded_file(root, relative) as stream:
        raw = stream.read()
    result = decode_json(raw, label=relative)
    if type(result) is not dict or result.get("schema_version") != "competition-result@0.1":
        raise ValueError("unsupported or missing original result schema")
    if _COLLECTOR_FIELDS.intersection(result):
        raise ValueError("original result must not contain collector-only fields")
    return result, _FileRecord(relative, hashlib.sha256(raw).hexdigest(), len(raw))


def _trace_metadata(result):
    filename = result.get("trace_file")
    if type(filename) is not str or filename not in _TRACE_ENCODINGS:
        raise ValueError("trace filename must be trace.jsonl or trace.jsonl.gz")
    encoding = result.get("trace_encoding")
    if type(encoding) is not str or encoding != _TRACE_ENCODINGS[filename]:
        raise ValueError("trace filename and encoding disagree")
    for key in ("trace_sha256", "trace_storage_sha256"):
        value = result.get(key)
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise ValueError("explicit trace SHA-256 metadata required: " + key)
    for key in ("trace_uncompressed_bytes", "trace_stored_bytes"):
        value = result.get(key)
        if type(value) is not int or value <= 0:
            raise ValueError("positive integer trace size required: " + key)
    return filename, encoding


def _audit_trace(root, relative, result, encoding):
    final_tick = result.get("final_tick")
    terminal, score = result.get("terminal"), result.get("score_audit")
    if (result.get("status") != "completed" or type(final_tick) is not int or final_tick <= 0
            or type(terminal) is not dict or terminal.get("latched") is not True
            or type(terminal.get("tick")) is not int or terminal["tick"] != final_tick
            or type(score) is not dict or type(score.get("tick")) is not int
            or score["tick"] != final_tick):
        raise ValueError("completed original result needs final tick, terminal and score")
    expected_size = result["trace_uncompressed_bytes"]
    stored_hash, content_hash = hashlib.sha256(), hashlib.sha256()
    stored_size, content_size, ticks = 0, 0, 0
    pending = bytearray()

    def record(raw):
        nonlocal ticks
        if not raw.strip():
            raise ValueError("blank trace record")
        row = decode_json(raw, label=f"{relative} record {ticks + 1}")
        ticks += 1
        if (type(row) is not dict or type(row.get("tick")) is not int
                or row["tick"] != ticks or ticks > final_tick
                or "terminal" not in row or "score" not in row):
            raise ValueError("trace ticks must be exactly 1 through final_tick")
        if (type(row["score"]) is not dict or type(row["score"].get("tick")) is not int
                or row["score"]["tick"] != ticks):
            raise ValueError("trace score tick must match its record tick")
        if ticks < final_tick:
            if row["terminal"] is not None:
                raise ValueError("trace has a terminal before its final tick")
        elif not _same_value(row["terminal"], terminal) or not _same_value(row["score"], score):
            raise ValueError("final trace score or terminal disagrees with original result")

    def consume(block):
        nonlocal content_size
        content_size += len(block)
        if content_size > expected_size:
            raise ValueError("trace exceeds its declared uncompressed size")
        content_hash.update(block)
        pending.extend(block)
        while True:
            boundary = pending.find(b"\n")
            if boundary < 0:
                break
            line = bytes(pending[:boundary])
            del pending[:boundary + 1]
            record(line)

    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    try:
        with _guarded_file(root, relative) as stream:
            while block := stream.read(_CHUNK):
                stored_size += len(block)
                if stored_size > result["trace_stored_bytes"]:
                    raise ValueError("trace exceeds its declared stored size")
                stored_hash.update(block)
                if decoder is None:
                    consume(block)
                    continue
                compressed = block
                while True:
                    decoded = decoder.decompress(compressed, _CHUNK)
                    consume(decoded)
                    if decoder.unused_data:
                        raise ValueError("gzip trace has trailing data or additional members")
                    compressed = decoder.unconsumed_tail
                    if not compressed and len(decoded) < _CHUNK:
                        break
            if decoder is not None and not decoder.eof:
                raise ValueError("truncated gzip trace")
    except zlib.error as error:
        raise ValueError("invalid gzip trace") from error
    if pending:
        record(bytes(pending))
    if ticks != final_tick:
        raise ValueError("trace must contain every tick through final_tick")
    if (stored_size != result["trace_stored_bytes"]
            or stored_hash.hexdigest() != result["trace_storage_sha256"]):
        raise ValueError("trace stored byte size or SHA-256 mismatch")
    if (content_size != expected_size or content_hash.hexdigest() != result["trace_sha256"]):
        raise ValueError("trace content byte size or SHA-256 mismatch")
    return _FileRecord(relative, stored_hash.hexdigest(), stored_size)


def read_episode_result(directory):
    """Read and hash the original result bytes without opening a trace."""
    result, original = _read_result(Path(directory), "result.json")
    return result, original.sha256


def read_episode_record(directory, *, expected_result_sha256=None):
    """Read an original result and verify every trace tick for a completed episode."""
    root = Path(directory)
    result, digest = read_episode_result(root)
    if expected_result_sha256 is not None and digest != expected_result_sha256:
        raise ValueError("original result changed after sample selection")
    if result.get("status") == "completed":
        filename, encoding = _trace_metadata(result)
        _audit_trace(root, filename, result, encoding)
    return result, digest


def audit_case_records(matrix_root, plan, rows, *, case_width=3):
    """Verify every planned case's existing original result and trace.

    The caller selects the known collector layout: public width 3, hidden width
    4. Paths in plans/results never select directories. Summary order may vary;
    each explicit case_index must match the identity at that plan position.
    """
    if type(case_width) is not int or case_width not in (3, 4):
        raise ValueError("trusted case directory width must be 3 or 4")
    if type(plan) is not dict or type(plan.get("cases")) is not list or not plan["cases"]:
        raise ValueError("nonempty planned case list required")
    if type(rows) is not list or len(rows) != len(plan["cases"]):
        raise ValueError("complete case evidence collection required")
    try:
        root = Path(matrix_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as error:
        raise ValueError("missing or invalid case evidence matrix root") from error
    identities = [_identity(case) for case in plan["cases"]]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate planned case identity")
    indexed = {}
    for row in rows:
        if type(row) is not dict:
            raise ValueError("summary result must be an object")
        index = row.get("case_index")
        if (type(index) is not int or index < 0 or index >= len(identities)
                or index in indexed):
            raise ValueError("unique explicit case_index in planned range required")
        if _identity(row) != identities[index]:
            raise ValueError("summary case_index does not match its planned identity")
        indexed[index] = row
    files = []
    for index in range(len(identities)):
        directory = f"case-{index:0{case_width}d}"
        result, original = _read_result(root, directory + "/result.json")
        projected = {key: value for key, value in indexed[index].items()
                     if key not in _COLLECTOR_FIELDS}
        if not _same_value(projected, result):
            raise ValueError("summary row disagrees with its original result: " + directory)
        filename, encoding = _trace_metadata(result)
        trace = _audit_trace(root, directory + "/" + filename, result, encoding)
        files.extend((original, trace))
    payload = {"schema_version": "case-evidence-snapshot@1", "case_count": len(identities),
               "files": [{"path": item.relative_path, "sha256": item.sha256, "bytes": item.size}
                         for item in files]}
    snapshot = CaseEvidenceSnapshot(hashlib.sha256(_canonical(payload)).hexdigest(),
                                    len(identities), root, tuple(files))
    snapshot.require_unchanged()
    return snapshot
