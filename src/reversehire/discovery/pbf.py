"""Minimal reader of OpenStreetMap .osm.pbf files: standard library + numpy only.

Why: the compiled `osmium` wheel cannot be loaded on every PC (on the owner's
Windows, Smart App Control blocks its unsigned DLL). This reader decodes just what
the discovery index needs, fast enough for a regional extract because the big
packed arrays (dense node ids, coordinates, tags) are decoded with numpy.

Format (https://wiki.openstreetmap.org/wiki/PBF_Format): a sequence of
[4-byte big-endian length][BlobHeader][Blob]; a Blob holds a zlib-compressed
PrimitiveBlock with a string table, coordinate granularity/offsets and groups of
nodes, dense nodes, ways and relations. Only zlib and raw blobs are supported
(what every common extract uses).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

MAX_HEADER = 64 * 1024
MAX_BLOB = 64 * 1024 * 1024


class PbfError(Exception):
    """The file is not a readable .osm.pbf (truncated, unsupported compression...)."""


# --- protobuf primitives ----------------------------------------------------------------


def varint(buf, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise PbfError("varint too long")


def fields(buf) -> Iterator[tuple[int, int, object]]:
    """(field number, wire type, value): int for varints, a memoryview slice for bytes."""
    view = buf if isinstance(buf, memoryview) else memoryview(buf)
    pos = 0
    end = len(view)
    while pos < end:
        key, pos = varint(view, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = varint(view, pos)
            yield number, 0, value
        elif wire == 2:
            length, pos = varint(view, pos)
            yield number, 2, view[pos:pos + length]
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise PbfError(f"unsupported wire type {wire}")


def zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def packed(buf) -> list[int]:
    """Packed varints decoded in Python (short arrays)."""
    values = []
    pos = 0
    end = len(buf)
    while pos < end:
        value, pos = varint(buf, pos)
        values.append(value)
    return values


_EMPTY_U64 = np.zeros(0, dtype=np.uint64)


def packed_np(buf) -> np.ndarray:
    """Packed varints decoded with numpy (uint64), for long arrays."""
    raw = np.frombuffer(buf, dtype=np.uint8)
    if raw.size == 0:
        return _EMPTY_U64
    last = raw < 0x80
    ends = np.flatnonzero(last)
    if ends.size == 0:
        return _EMPTY_U64
    if ends[-1] != raw.size - 1:
        raw = raw[: ends[-1] + 1]
    starts = np.empty_like(ends)
    starts[0] = 0
    starts[1:] = ends[:-1] + 1
    lengths = ends - starts + 1
    if int(lengths.max()) == 1:
        return raw.astype(np.uint64)
    position = np.arange(raw.size, dtype=np.int64) - np.repeat(starts, lengths)
    parts = (raw & 0x7F).astype(np.uint64) << (position.astype(np.uint64) * np.uint64(7))
    return np.add.reduceat(parts, starts)


def zigzag_np(values: np.ndarray) -> np.ndarray:
    return (values >> np.uint64(1)).astype(np.int64) ^ -((values & np.uint64(1)).astype(np.int64))


def delta_np(buf) -> np.ndarray:
    """Packed sint64 delta-coded array -> absolute int64 values."""
    values = packed_np(buf)
    if values.size == 0:
        return np.zeros(0, dtype=np.int64)
    return np.cumsum(zigzag_np(values))


# --- blocks ---------------------------------------------------------------------------


@dataclass
class Block:
    strings: list[bytes]
    granularity: int
    lat_offset: int
    lon_offset: int
    groups: list[tuple[int, memoryview]]   # (1 nodes | 2 dense | 3 ways | 4 relations, payload)

    def string(self, index: int) -> str:
        try:
            return self.strings[index].decode("utf-8", "replace")
        except IndexError:
            return ""

    def ids_of(self, wanted: set[bytes]) -> set[int]:
        return {i for i, s in enumerate(self.strings) if s in wanted}

    def kinds(self) -> set[int]:
        return {kind for kind, _ in self.groups}


def parse_block(data: bytes) -> Block:
    strings: list[bytes] = []
    granularity, lat_offset, lon_offset = 100, 0, 0
    groups: list[tuple[int, memoryview]] = []
    for number, wire, value in fields(data):
        if number == 1 and wire == 2:
            strings = [bytes(v) for n, w, v in fields(value) if n == 1 and w == 2]
        elif number == 2 and wire == 2:
            # A PrimitiveGroup holds one kind of entity: keep the whole group, typed by
            # its first field (dense: the DenseNodes message itself).
            for kind, w, payload in fields(value):
                if w == 2 and kind in (1, 2, 3, 4):
                    groups.append((kind, payload if kind == 2 else value))
                break
        elif number == 17 and wire == 0:
            granularity = value
        elif number == 19 and wire == 0:
            lat_offset = _signed(value)
        elif number == 20 and wire == 0:
            lon_offset = _signed(value)
    return Block(strings, granularity, lat_offset, lon_offset, groups)


def _signed(value: int) -> int:
    """int64 (not zigzag) varint as a signed number."""
    return value - (1 << 64) if value >= (1 << 63) else value


class PbfFile:
    """Sequential reader of the blobs of a .osm.pbf file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.size = self.path.stat().st_size

    def data_blobs(self, offsets: list[int] | None = None) -> Iterator[tuple[int, bytes]]:
        """(offset, decompressed PrimitiveBlock bytes) of every OSMData blob, or only of
        the blobs starting at `offsets` (from a previous pass)."""
        with self.path.open("rb") as handle:
            if offsets is None:
                while True:
                    offset = handle.tell()
                    item = self._read_blob(handle)
                    if item is None:
                        return
                    kind, data = item
                    if kind == "OSMData":
                        yield offset, data
            else:
                for offset in offsets:
                    handle.seek(offset)
                    item = self._read_blob(handle)
                    if item is not None and item[0] == "OSMData":
                        yield offset, item[1]

    @staticmethod
    def _read_blob(handle) -> tuple[str, bytes] | None:
        head = handle.read(4)
        if not head:
            return None
        if len(head) < 4:
            raise PbfError("truncated file (blob length)")
        (length,) = struct.unpack(">I", head)
        if length > MAX_HEADER:
            raise PbfError("blob header too large: not a .osm.pbf file?")
        header = handle.read(length)
        if len(header) < length:
            raise PbfError("truncated file (blob header)")
        kind, size = "", 0
        for number, wire, value in fields(header):
            if number == 1 and wire == 2:
                kind = bytes(value).decode("ascii", "replace")
            elif number == 3 and wire == 0:
                size = value
        if size <= 0 or size > MAX_BLOB:
            raise PbfError("invalid blob size")
        blob = handle.read(size)
        if len(blob) < size:
            raise PbfError("truncated file (blob)")
        if kind != "OSMData":
            return kind, b""
        raw = zlib_data = None
        for number, wire, value in fields(blob):
            if number == 1 and wire == 2:
                raw = bytes(value)
            elif number == 3 and wire == 2:
                zlib_data = value
            elif number in (4, 5, 6, 7) and wire == 2:
                raise PbfError("unsupported blob compression (only zlib and raw are supported)")
        if raw is not None:
            return kind, raw
        if zlib_data is None:
            raise PbfError("empty blob")
        try:
            return kind, zlib.decompress(zlib_data)
        except zlib.error as error:
            raise PbfError(f"corrupt blob: {error}") from error


# --- entities -----------------------------------------------------------------------------


def dense_arrays(payload) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(ids int64, raw lat int64, raw lon int64, keys_vals int64) of a DenseNodes group."""
    ids = lat = lon = kv = None
    for number, wire, value in fields(payload):
        if wire != 2:
            continue
        if number == 1:
            ids = delta_np(value)
        elif number == 8:
            lat = delta_np(value)
        elif number == 9:
            lon = delta_np(value)
        elif number == 10:
            kv = packed_np(value).astype(np.int64)
    empty = np.zeros(0, dtype=np.int64)
    return (ids if ids is not None else empty, lat if lat is not None else empty,
            lon if lon is not None else empty, kv if kv is not None else empty)


def degrees(block: Block, raw: np.ndarray | int, offset: int):
    return (offset + block.granularity * raw) * 1e-9


def dense_tagged(block: Block, payload, key_ids: set[int]):
    """Nodes of a DenseNodes group that have at least one key in `key_ids`:
    yields (id, lat, lon, {key: value})."""
    ids, lat, lon, kv = dense_arrays(payload)
    if kv.size == 0 or not key_ids or ids.size == 0:
        return
    is_zero = kv == 0
    zeros = np.flatnonzero(is_zero)
    if zeros.size == 0:
        return
    node_of = np.cumsum(is_zero) - is_zero
    seg_start = np.empty_like(zeros)
    seg_start[0] = 0
    seg_start[1:] = zeros[:-1] + 1
    valid = node_of < seg_start.size
    position = np.zeros(kv.size, dtype=np.int64)
    position[valid] = np.arange(kv.size)[valid] - seg_start[node_of[valid]]
    is_key = (~is_zero) & valid & (position % 2 == 0)
    wanted = np.fromiter(key_ids, dtype=np.int64)
    hits = is_key & np.isin(kv, wanted)
    nodes = np.unique(node_of[hits])
    for node in nodes.tolist():
        if node >= ids.size:
            continue
        start, end = int(seg_start[node]), int(zeros[node])
        pairs = kv[start:end].tolist()
        tags = {block.string(pairs[i]): block.string(pairs[i + 1]) for i in range(0, len(pairs) - 1, 2)}
        yield (int(ids[node]), float(degrees(block, int(lat[node]), block.lat_offset)),
               float(degrees(block, int(lon[node]), block.lon_offset)), tags)


def plain_nodes(block: Block, payload, key_ids: set[int]):
    """Non-dense Node messages with a key in `key_ids` (rare in extracts)."""
    for number, wire, node in fields(payload):
        if number != 1 or wire != 2:
            continue
        node_id = lat = lon = 0
        keys: list[int] = []
        vals: list[int] = []
        for n, w, v in fields(node):
            if n == 1 and w == 0:
                node_id = zigzag(v)
            elif n == 2 and w == 2:
                keys = packed(v)
            elif n == 3 and w == 2:
                vals = packed(v)
            elif n == 8 and w == 0:
                lat = zigzag(v)
            elif n == 9 and w == 0:
                lon = zigzag(v)
        if not any(k in key_ids for k in keys):
            continue
        tags = {block.string(k): block.string(v) for k, v in zip(keys, vals)}
        yield node_id, degrees(block, lat, block.lat_offset), degrees(block, lon, block.lon_offset), tags


def plain_node_coords(block: Block, payload):
    """(id, lat, lon) of every non-dense node."""
    for number, wire, node in fields(payload):
        if number != 1 or wire != 2:
            continue
        node_id = lat = lon = 0
        for n, w, v in fields(node):
            if n == 1 and w == 0:
                node_id = zigzag(v)
            elif n == 8 and w == 0:
                lat = zigzag(v)
            elif n == 9 and w == 0:
                lon = zigzag(v)
        yield node_id, degrees(block, lat, block.lat_offset), degrees(block, lon, block.lon_offset)


def ways(block: Block, payload, key_ids: set[int] | None = None, wanted_ids=None,
         with_refs: bool = True):
    """Ways of a group, as (id, tags, refs ndarray | None). A way is returned when it
    has a key in `key_ids`, or its id is in `wanted_ids` (a set). Others are skipped
    before their node list is decoded."""
    for number, wire, way in fields(payload):
        if number != 3 or wire != 2:
            continue
        way_id = 0
        keys_buf = vals_buf = refs_buf = None
        for n, w, v in fields(way):
            if n == 1 and w == 0:
                way_id = v
            elif n == 2 and w == 2:
                keys_buf = v
            elif n == 3 and w == 2:
                vals_buf = v
            elif n == 8 and w == 2:
                refs_buf = v
        keys = packed(keys_buf) if keys_buf is not None else []
        keep = bool(key_ids) and any(k in key_ids for k in keys)
        if not keep and wanted_ids is not None and way_id in wanted_ids:
            keep = True
        if not keep:
            continue
        vals = packed(vals_buf) if vals_buf is not None else []
        tags = {block.string(k): block.string(v) for k, v in zip(keys, vals)}
        refs = delta_np(refs_buf) if (with_refs and refs_buf is not None) else None
        yield way_id, tags, refs


def relations(block: Block, payload, key_ids: set[int]):
    """Relations with a key in `key_ids`: (id, tags, [(type, member id, role)]),
    type 0 node, 1 way, 2 relation."""
    for number, wire, relation in fields(payload):
        if number != 4 or wire != 2:
            continue
        rel_id = 0
        keys_buf = vals_buf = roles_buf = ids_buf = types_buf = None
        for n, w, v in fields(relation):
            if n == 1 and w == 0:
                rel_id = v
            elif n == 2 and w == 2:
                keys_buf = v
            elif n == 3 and w == 2:
                vals_buf = v
            elif n == 8 and w == 2:
                roles_buf = v
            elif n == 9 and w == 2:
                ids_buf = v
            elif n == 10 and w == 2:
                types_buf = v
        keys = packed(keys_buf) if keys_buf is not None else []
        if not any(k in key_ids for k in keys):
            continue
        vals = packed(vals_buf) if vals_buf is not None else []
        tags = {block.string(k): block.string(v) for k, v in zip(keys, vals)}
        roles = packed(roles_buf) if roles_buf is not None else []
        member_ids = delta_np(ids_buf).tolist() if ids_buf is not None else []
        types = packed(types_buf) if types_buf is not None else []
        members = [(types[i] if i < len(types) else 0, int(member_ids[i]),
                    block.string(roles[i]) if i < len(roles) else "")
                   for i in range(len(member_ids))]
        yield rel_id, tags, members


# --- writer (tests and fixtures) ---------------------------------------------------------


def _w_varint(value: int) -> bytes:
    out = bytearray()
    value &= (1 << 64) - 1
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _w_zig(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def _w_field(number: int, payload: bytes) -> bytes:
    return _w_varint((number << 3) | 2) + _w_varint(len(payload)) + payload


def _w_int(number: int, value: int) -> bytes:
    return _w_varint(number << 3) + _w_varint(value)


def _w_packed(number: int, values, signed_delta: bool = False) -> bytes:
    out = bytearray()
    previous = 0
    for value in values:
        if signed_delta:
            out += _w_varint(_w_zig(value - previous))
            previous = value
        else:
            out += _w_varint(value)
    return _w_field(number, bytes(out))


def write_pbf(path: str | Path, nodes: list, ways_: list, relations_: list,
              granularity: int = 100) -> None:
    """Write a small valid .osm.pbf (one dense-node block, one way block, one
    relation block). nodes: (id, lat, lon, tags); ways_: (id, tags, refs);
    relations_: (id, tags, [(type, id, role)])."""
    strings: list[str] = [""]
    index: dict[str, int] = {"": 0}

    def sid(text: str) -> int:
        if text not in index:
            index[text] = len(strings)
            strings.append(text)
        return index[text]

    groups: list[bytes] = []
    if nodes:
        kv: list[int] = []
        for _, _, _, tags in nodes:
            for k, v in tags.items():
                kv += [sid(k), sid(v)]
            kv.append(0)
        dense = (_w_packed(1, [n[0] for n in nodes], True)
                 + _w_packed(8, [round(n[1] * 1e9 / granularity) for n in nodes], True)
                 + _w_packed(9, [round(n[2] * 1e9 / granularity) for n in nodes], True)
                 + _w_packed(10, kv))
        groups.append(_w_field(2, dense))
    if ways_:
        body = b""
        for way_id, tags, refs in ways_:
            way = (_w_int(1, way_id) + _w_packed(2, [sid(k) for k in tags])
                   + _w_packed(3, [sid(v) for v in tags.values()]) + _w_packed(8, refs, True))
            body += _w_field(3, way)
        groups.append(body)
    if relations_:
        body = b""
        for rel_id, tags, members in relations_:
            rel = (_w_int(1, rel_id) + _w_packed(2, [sid(k) for k in tags])
                   + _w_packed(3, [sid(v) for v in tags.values()])
                   + _w_packed(8, [sid(m[2]) for m in members])
                   + _w_packed(9, [m[1] for m in members], True)
                   + _w_packed(10, [m[0] for m in members]))
            body += _w_field(4, rel)
        groups.append(body)
    table = b"".join(_w_field(1, s.encode("utf-8")) for s in strings)

    def blob(kind: str, data: bytes) -> bytes:
        compressed = zlib.compress(data)
        payload = _w_int(2, len(data)) + _w_field(3, compressed)
        header = _w_field(1, kind.encode("ascii")) + _w_int(3, len(payload))
        return struct.pack(">I", len(header)) + header + payload

    out = blob("OSMHeader", _w_field(4, b"OsmSchema-V0.6") + _w_field(4, b"DenseNodes"))
    for group in groups:
        block = _w_field(1, table) + _w_field(2, group) + _w_int(17, granularity)
        out += blob("OSMData", block)
    Path(path).write_bytes(out)
