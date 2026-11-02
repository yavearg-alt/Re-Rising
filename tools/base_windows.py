"""Force release windows of base.bin open or closed (sub-table 15: field 1 label, 5 start, 6 end date).

usage: base_windows.py <base.enc> <out.enc> <label>=open|closed [...]

The client opens a feature (season, banner, event) while the game clock is inside its release window. The server
clock loops over the last weeks of the service (config.GAME_WINDOW, Nov 2022 - Jan 2023), after Season 9 ended
(2022/10/28): without a patch there is no Season Pass at all. The patch is done in place: each date is an XXTEA blob
of 6 words (19 characters), rewritten with the same size, so the FlatBuffer offsets and the file size don't move.
An empty date (no limit) is an 8-byte blob and is left empty.

  open    start no later than OPEN_FROM, end no earlier than SERVICE_END (an empty bound stays unlimited)
  closed  a window that ended long before the game clock; a start-only window starts in the far future
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flatmaster import Master  # noqa: E402
from saoub_crypto import decrypt, encrypt, xxtea_decrypt, xxtea_encrypt  # noqa: E402

RELEASE_SUB = 15
LABEL, START, END = 1, 5, 6
SERVICE_END = "2023/01/16 05:59:59"  # the last scouts ("Until 1/16 5:59 (UTC)")
OPEN_FROM = "2022/11/20 06:00:00"  # first day of the game clock (config.GAME_WINDOW / RAID_WINDOW)
PAST = ("2020/01/01 00:00:00", "2020/01/01 00:00:01")
FAR_FUTURE = "2099/01/01 00:00:00"


class _Dates:
    """The start / end date blobs of one release row, read and rewritten in place."""

    def __init__(self, m: Master, buf: bytearray, table: int):
        self.buf = buf
        offs, _ = m.fields(table)
        self.slots = {}
        for field in (START, END):
            if field < len(offs) and offs[field]:
                p = table + offs[field]
                target = p + m.u32(p)
                size = m.u32(target)
                plain = xxtea_decrypt(bytes(buf[target + 4:target + 4 + size]))
                self.slots[field] = (target + 4, size, (plain or b"").rstrip(b"\0").decode("utf-8", "replace"))

    def get(self, field) -> str:
        return self.slots.get(field, (0, 0, ""))[2]

    def set(self, field, value: str) -> bool:
        """False when the field has no room for a date (empty, or absent)."""
        if field not in self.slots or not self.get(field):
            return False
        pos, size, _ = self.slots[field]
        self.buf[pos:pos + size] = xxtea_encrypt(value.encode(), words=size // 4)
        self.slots[field] = (pos, size, value)
        return True


def set_windows(flat: bytes, states: dict) -> tuple[bytes, dict]:
    """{label: "open" | "closed"} -> (patched FlatBuffer, {label: state} actually applied)."""
    m = Master(flat)
    buf = bytearray(flat)
    vec = m.tables()[RELEASE_SUB]
    done = {}
    for i in range(m.u32(vec)):
        q = vec + 4 + 4 * i
        table = q + m.u32(q)
        offs, _ = m.fields(table)
        label = m.value(table, offs[LABEL], 4) if LABEL < len(offs) and offs[LABEL] else None
        state = states.get(label)
        if state not in ("open", "closed"):
            continue
        dates = _Dates(m, buf, table)
        start, end = dates.get(START), dates.get(END)
        if state == "open":
            if start and start > OPEN_FROM:
                dates.set(START, OPEN_FROM)
            if end and end < SERVICE_END:
                dates.set(END, SERVICE_END)
            done[label] = "open"
        elif end:
            dates.set(START, PAST[0])
            dates.set(END, PAST[1])
            done[label] = "closed"
        elif start:
            dates.set(START, FAR_FUTURE)
            done[label] = "closed"
        else:
            done[label] = "no date to close"
    assert len(buf) == len(flat)
    return bytes(buf), done


def set_windows_enc(raw: bytes, states: dict) -> tuple[bytes, dict]:
    """Same on the encrypted file (master/divided/base.enc as the CDN serves it)."""
    flat, done = set_windows(decrypt(raw), states)
    return encrypt(flat), done


def main(argv) -> int:
    if len(argv) < 3 or not all("=" in a for a in argv[2:]):
        print(__doc__)
        return 1
    states = dict(a.split("=", 1) for a in argv[2:])
    with open(argv[0], "rb") as f:
        data, done = set_windows_enc(f.read(), states)
    with open(argv[1], "wb") as f:
        f.write(data)
    print(done)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
