"""Minimal pure-Python LevelDB reader (no plyvel / snappy needed): the game's local user data
(`files/ldb` of the /data/data dump). Reads the .ldb/.sst tables and the .log journal, keeps the newest value
of each key and drops deleted ones.

    from leveldb_read import read_db
    pairs = read_db("…/files/ldb")   # {key bytes: value bytes}
"""
import glob
import os
import struct


def _varint(buf: bytes, pos: int):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def snappy_decompress(buf: bytes) -> bytes:
    size, pos = _varint(buf, 0)
    out = bytearray()
    while pos < len(buf):
        tag = buf[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:  # literal
            n = tag >> 2
            if n >= 60:
                extra = n - 59
                n = int.from_bytes(buf[pos:pos + extra], "little")
                pos += extra
            n += 1
            out += buf[pos:pos + n]
            pos += n
            continue
        if kind == 1:
            length = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | buf[pos]
            pos += 1
        elif kind == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(buf[pos:pos + 2], "little")
            pos += 2
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        start = len(out) - offset
        for i in range(length):  # copies may overlap their own output
            out.append(out[start + i])
    if len(out) != size:
        raise ValueError(f"snappy: {len(out)} bytes instead of {size}")
    return bytes(out)


def _block(data: bytes, offset: int, size: int) -> bytes:
    raw = data[offset:offset + size]
    kind = data[offset + size]
    return snappy_decompress(raw) if kind == 1 else raw


def _block_entries(block: bytes):
    restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * restarts
    pos, key = 0, b""
    while pos < end:
        shared, pos = _varint(block, pos)
        unshared, pos = _varint(block, pos)
        vlen, pos = _varint(block, pos)
        key = key[:shared] + block[pos:pos + unshared]
        pos += unshared
        yield key, block[pos:pos + vlen]
        pos += vlen


def _table(path: str):
    """(user key, sequence, type, value) of an SSTable; type 1 = value, 0 = deletion."""
    data = open(path, "rb").read()
    footer = data[-48:]
    pos = 0
    _, pos = _varint(footer, pos)  # metaindex offset
    _, pos = _varint(footer, pos)  # metaindex size
    index_off, pos = _varint(footer, pos)
    index_size, pos = _varint(footer, pos)
    for _, handle in _block_entries(_block(data, index_off, index_size)):
        off, p = _varint(handle, 0)
        size, _ = _varint(handle, p)
        for ikey, value in _block_entries(_block(data, off, size)):
            tag = int.from_bytes(ikey[-8:], "little")
            yield ikey[:-8], tag >> 8, tag & 0xFF, value


def _log(path: str):
    data = open(path, "rb").read()
    records, pending, pos = [], b"", 0
    while pos + 7 <= len(data):
        block_left = 32768 - pos % 32768
        if block_left < 7:
            pos += block_left
            continue
        length, rtype = struct.unpack_from("<HB", data, pos + 4)
        chunk = data[pos + 7:pos + 7 + length]
        pos += 7 + length
        if rtype == 0 and length == 0:
            pos += 32768 - pos % 32768 if pos % 32768 else 0
            continue
        if rtype == 1:  # full
            records.append(chunk)
        elif rtype == 2:  # first
            pending = chunk
        elif rtype == 3:  # middle
            pending += chunk
        elif rtype == 4:  # last
            records.append(pending + chunk)
            pending = b""
    for rec in records:
        seq, count = struct.unpack_from("<QI", rec, 0)
        p = 12
        for i in range(count):
            kind = rec[p]
            p += 1
            klen, p = _varint(rec, p)
            key = rec[p:p + klen]
            p += klen
            value = b""
            if kind == 1:
                vlen, p = _varint(rec, p)
                value = rec[p:p + vlen]
                p += vlen
            yield key, seq + i, kind, value


def read_db(folder: str) -> dict:
    newest = {}
    files = sorted(glob.glob(os.path.join(folder, "*.ldb")) + glob.glob(os.path.join(folder, "*.sst")))
    sources = [_table(f) for f in files] + [_log(f) for f in sorted(glob.glob(os.path.join(folder, "*.log")))]
    for source in sources:
        for key, seq, kind, value in source:
            if key not in newest or seq > newest[key][0]:
                newest[key] = (seq, kind, value)
    return {k: v for k, (s, kind, v) in newest.items() if kind == 1}
