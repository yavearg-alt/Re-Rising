"""Decode every master table into JSON (one file per sub-table) so lookups don't re-run the slow XXTEA decoding.

usage: dump_masters.py <master_final dir> <out dir> [workers]
Output: <out>/<table>.<sub>.json = list of rows ({field index: value}).
"""
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flatmaster import Master  # noqa: E402


def dump(args):
    src, out, sub = args
    name = os.path.basename(src)[:-4]
    dst = os.path.join(out, f"{name}.{sub}.json")
    if os.path.exists(dst):
        return dst, -1
    m = Master(open(src, "rb").read())
    rows = list(m.rows(sub))
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, dst)
    return dst, len(rows)


def main():
    src_dir, out = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count()
    os.makedirs(out, exist_ok=True)
    jobs = []
    for fn in sorted(os.listdir(src_dir), key=lambda f: -os.path.getsize(os.path.join(src_dir, f))):
        if fn.endswith(".bin"):
            path = os.path.join(src_dir, fn)
            jobs += [(path, out, i) for i in range(len(Master(open(path, "rb").read()).tables()))]
    with ProcessPoolExecutor(workers) as ex:
        for dst, n in ex.map(dump, jobs):
            print(os.path.basename(dst), n, flush=True)


if __name__ == "__main__":
    main()
