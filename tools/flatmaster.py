"""Schema-less decoder for the master data FlatBuffers (master/divided/*.enc, already AES/zlib-decrypted).

Layout observed: root table = N vectors of tables (one per master table). Every scalar is stored inline as
8 bytes XXTEA-encrypted ([value][length]); strings/byte arrays are length-prefixed XXTEA blobs; nested
vectors of scalars/tables also exist. Field names are unknown: fields are reported by index.

usage: flatmaster.py <table.bin> [sub_table_index] [max_rows]
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from saoub_crypto import xxtea_decrypt  # noqa: E402


class Master:
    def __init__(self, data: bytes):
        self.d = data

    def u32(self, p):
        return struct.unpack_from("<I", self.d, p)[0]

    def fields(self, table):
        vt = table - struct.unpack_from("<i", self.d, table)[0]
        vlen, tlen = struct.unpack_from("<HH", self.d, vt)
        offs = [struct.unpack_from("<H", self.d, vt + 4 + 2 * i)[0] for i in range((vlen - 4) // 2)]
        return offs, tlen

    def scalar(self, p, size=8):
        raw = None
        for n in (size, 12, 8, 16):
            if n % 4 == 0 and n >= 8:
                raw = xxtea_decrypt(self.d[p:p + n])
                if raw is not None:
                    break
        if raw is None:
            return None
        if len(raw) == 4:
            i = struct.unpack("<i", raw)[0]
            f = struct.unpack("<f", raw)[0]
            # heuristics: small magnitudes with fractional part are probably floats
            if abs(i) > 0x3F000000 and 1e-6 < abs(f) < 1e7 and f != int(f):
                return round(f, 6)
            return i
        if len(raw) == 8:
            q = struct.unpack("<q", raw)[0]
            dbl = struct.unpack("<d", raw)[0]
            if abs(q) > 1 << 52 and 1e-9 < abs(dbl) < 1e15:
                return dbl
            return q
        if len(raw) == 1:
            return raw[0]
        if len(raw) == 2:
            return struct.unpack("<h", raw)[0]
        return raw.hex()

    def blob(self, p):
        n = self.u32(p)
        if n % 4 or n < 8 or p + 4 + n > len(self.d):
            return None
        raw = xxtea_decrypt(self.d[p + 4:p + 4 + n])
        if raw is None:
            return None
        try:
            return raw.rstrip(b"\0").decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()

    def value(self, table, off, size):
        p = table + off
        if size >= 8:
            v = self.scalar(p, min(size, 16) // 4 * 4)
            if v is not None:
                return v
        target = p + self.u32(p)
        if not 0 < target < len(self.d):
            return None
        s = self.blob(target)
        if s is not None:
            return s
        n = self.u32(target)
        if 0 <= n < 100000:  # vector: of tables (offsets) or of inline encrypted scalars (8 bytes each)
            items = []
            for i in range(min(n, 200)):
                q = target + 4 + 4 * i
                inner = q + self.u32(q)
                if 0 < inner < len(self.d) - 4:
                    try:
                        items.append(self.row(inner))
                        continue
                    except Exception:
                        pass
                items.append(self.scalar(target + 4 + 8 * i))
            return items
        return None

    def row(self, table):
        offs, tlen = self.fields(table)
        present = sorted((o, i) for i, o in enumerate(offs) if o)
        out = {}
        for k, (o, i) in enumerate(present):
            end = present[k + 1][0] if k + 1 < len(present) else tlen
            out[i] = self.value(table, o, end - o)
        return out

    def tables(self):
        root = self.u32(0)
        offs, _ = self.fields(root)
        res = []
        for o in offs:
            p = root + o
            vec = p + self.u32(p)
            res.append(vec)
        return res

    def rows(self, sub, limit=None):
        vec = self.tables()[sub]
        n = self.u32(vec)
        for i in range(n if limit is None else min(n, limit)):
            q = vec + 4 + 4 * i
            yield self.row(q + self.u32(q))


if __name__ == "__main__":
    m = Master(open(sys.argv[1], "rb").read())
    subs = [int(sys.argv[2])] if len(sys.argv) > 2 else range(len(m.tables()))
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    for s in subs:
        vec = m.tables()[s]
        print(f"## sub-table {s}: {m.u32(vec)} rows")
        for r in m.rows(s, lim):
            print("  ", json.dumps(r, ensure_ascii=False)[:600])
