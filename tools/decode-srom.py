#!/usr/bin/python3 -I
"""Turn a raw SROM dump (tools/dump-srom.py) into a brcmfmac NVRAM .txt, using Apple's own variable map.

Apple's AirPortBrcm4360 kext (fetch-brcm-fw.py puts it under work/kext) contains Broadcom's SROM variable
table: 24-byte entries {char *name; u32 revmask; u32 flags; u16 offset; u16 mask}, one run per SROM revision,
a NULL-named entry continuing the one before it when that has SRFL_MORE. This reads the table from the Mach-O
(vmaddr == file offset in this kext), applies it to the words dumped from the card, and prints name=value
lines the way Broadcom's srom_parsecis does.

  decode-srom.py --kext AirPortBrcm4360 --dump work/srom/<ts>/cc-0800.bin [--rev N] [--diff generic.txt]
"""
import argparse
import re
import struct
import sys
from pathlib import Path

MORE, NOFFS, PRHEX, PRSIGN, CCODE, ETHADDR, LEDDC, NOVAR, ARRAY = 1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100
TABLE_START = 0x545030           # first entry ('devid') in AirPortBrcm4360 1150.12.1a1
IDENT = re.compile(rb"[A-Za-z][A-Za-z0-9_]{1,40}$")


class Entry:
    __slots__ = ("name", "revmask", "flags", "off", "mask")

    def __init__(self, name, revmask, flags, off, mask):
        self.name, self.revmask, self.flags, self.off, self.mask = name, revmask, flags, off, mask

    def __repr__(self):
        return f"Entry({self.name!r}, {self.revmask:#x}, {self.flags:#x}, {self.off}, {self.mask:#x})"


def load_table(binary, start=TABLE_START):
    """Entries from `start` up to the first NAMED entry whose name pointer is not a plain identifier."""
    out, o = [], start
    while o + 24 <= len(binary):
        ptr, rm, fl, off, mask = struct.unpack_from("<QIIHH", binary, o)
        if ptr == 0:
            name = None
        else:
            if not 0 < ptr < len(binary):
                break
            end = binary.find(b"\0", ptr, ptr + 64)
            s = binary[ptr:end] if end > ptr else b""
            if end == ptr:
                name = None                       # a continuation entry points at an empty string
            elif IDENT.match(s):
                name = s.decode()
            else:
                break
        out.append(Entry(name, rm, fl, off, mask))
        o += 24
    return out


def lsb(mask):
    return (mask & -mask).bit_length() - 1 if mask else 0


def part(words, off, mask):
    w = words[off] if off < len(words) else 0xFFFF
    return (w & mask) >> lsb(mask), bin(mask).count("1"), w & mask == mask


def fmt(val, flags, width):
    if flags & PRSIGN and val & (1 << (width - 1)):
        val -= 1 << width
    return f"{val:#x}" if flags & PRHEX else str(val)


def decode(entries, words, rev, array_rule="last"):
    """[(name, 'text')] for one SROM revision, in table order.

    An element is an entry plus its SRFL_MORE continuations (more bits of the same number). An element that
    carries SRFL_ARRAY is followed by one more element of the same variable (the continuation entries have an
    empty name); the last element of an array has no ARRAY flag. `array_rule` says whether ARRAY is read from the
    element's last part ("last") or from any part ("any")."""
    out, i = [], 0
    n = len(entries)

    def element():
        nonlocal i
        parts = [entries[i]]
        i += 1
        while parts[-1].flags & MORE and i < n:
            parts.append(entries[i])
            i += 1
        return parts

    def is_array(parts):
        return bool(parts[-1].flags & ARRAY) if array_rule == "last" else any(p.flags & ARRAY for p in parts)

    while i < n:
        if entries[i].name is None:
            i += 1                                  # continuation of something that was skipped
            continue
        first = entries[i]
        elems = [element()]
        while is_array(elems[-1]) and i < n and entries[i].name is None:
            elems.append(element())
        if not (first.revmask >> rev) & 1 or first.flags & NOVAR:
            continue
        vals, skip = [], False
        for parts in elems:
            if first.flags & ETHADDR:
                ws = [words[parts[0].off + k] for k in range(3)]
                vals.append(":".join(f"{b:02x}" for w in ws for b in (w >> 8, w & 0xFF)))
                continue
            val, width, allones = 0, 0, True
            for c in parts:
                v, bits, ones = part(words, c.off, c.mask)
                val |= v << width
                width += bits
                allones &= ones
            if first.flags & NOFFS and allones:
                skip = True
                break
            if first.flags & CCODE:
                vals.append("0" if val in (0, 0xFFFF) else chr((val >> 8) & 0xFF) + chr(val & 0xFF))
            elif first.flags & LEDDC:
                vals.append(f"{val:#06x}")
            else:
                vals.append(fmt(val, first.flags, width))
        if not skip:
            out.append((first.name, ",".join(vals)))
    return out


def to_nvram(pairs, rev):
    seen, lines = set(), [f"sromrev={rev}"]
    for name, text in pairs:
        if name == "sromrev":
            continue
        lines.append(f"{name}={text}")
    return "\n".join(lines) + "\n"


def merge(base_text, pairs, rev, skip_re, keep, sromrev, only_re=None, only_new=False, drop_re=None):
    """Overlay decoded SROM values onto an existing NVRAM file, keeping its order and anything the SROM lacks
    (swctrlmap*, vendid/devid, ...). `skip_re` and `keep` name variables whose base value must stay."""
    skip = re.compile(skip_re) if skip_re else None
    base_keys = {l.split("=", 1)[0].strip() for l in base_text.splitlines() if "=" in l and not l.startswith("#")}
    only = re.compile(only_re) if only_re else None
    if only:
        pairs = [(k, v) for k, v in pairs if only.search(k)]
    if only_new:
        pairs = [(k, v) for k, v in pairs if k not in base_keys]
    dec = {k: v for k, v in pairs}
    lines, done = [], set()
    drop = re.compile(drop_re) if drop_re else None
    for line in base_text.splitlines():
        if "=" not in line or line.startswith("#"):
            lines.append(line)
            continue
        k = line.split("=", 1)[0].strip()
        if drop and drop.search(k):
            continue                              # e.g. another unit's txcal data: frees room (the firmware caps the NVRAM size)
        if k in dec and k not in keep and not (skip and skip.search(k)):
            lines.append(f"{k}={dec[k]}")
            done.add(k)
        else:
            lines.append(line)
    extra = [(k, v) for k, v in pairs if k not in done and k not in keep and k not in {l.split("=", 1)[0].strip() for l in lines if "=" in l}
             and not (skip and skip.search(k))]
    if extra:
        lines.append("")
        lines.append("# from the card's SROM (rev %d), not in the base file" % rev)
        lines += [f"{k}={v}" for k, v in extra]
    out = "\n".join(lines) + "\n"
    return re.sub(r"^sromrev=.*$", f"sromrev={sromrev}", out, flags=re.M)


def words_of(blob):
    return list(struct.unpack(f"<{len(blob) // 2}H", blob[: len(blob) // 2 * 2]))


def parse_nvram(text):
    d = {}
    for l in text.splitlines():
        if "=" in l and not l.startswith("#"):
            k, v = l.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def pick_rev(entries, words):
    """Revisions whose table decodes this card's known identity: devid 0x43ba, boardflags 0x10401001, a sane MAC."""
    good = []
    for rev in range(8, 14):
        d = dict(decode_all(entries, words, rev))
        mac = d.get("macaddr", "")
        if d.get("devid", "").lower() == "0x43ba" and d.get("boardflags", "").lower() == "0x10401001" \
                and re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) and mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            good.append(rev)
    return good


def decode_all(entries, words, rev, **kw):
    # devid is NOVAR; include it for the rev check
    saved = [(e, e.flags) for e in entries]
    for e, f in saved:
        e.flags &= ~NOVAR
    try:
        return decode(entries, words, rev, **kw)
    finally:
        for e, f in saved:
            e.flags = f


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kext", required=True, help="the AirPortBrcm4360 Mach-O binary")
    ap.add_argument("--dump", required=True, help="cc-0800.bin from dump-srom.py")
    ap.add_argument("--rev", type=int, help="SROM revision (default: the one that decodes to this board's known values)")
    ap.add_argument("--diff", help="compare against an existing NVRAM .txt")
    ap.add_argument("--out", help="write the NVRAM here instead of stdout")
    ap.add_argument("--base", help="overlay the decoded values onto this NVRAM file instead of printing them alone")
    ap.add_argument("--skip", default=r"_txcal|^ccode$|^regrev$", help="with --base: variables (regex) whose base value stays")
    ap.add_argument("--keep", default="", help="with --base: comma list of variables whose base value stays")
    ap.add_argument("--only", help="with --base: overlay only variables matching this regex (for bisecting)")
    ap.add_argument("--drop", help="with --base: remove base variables matching this regex")
    ap.add_argument("--only-new", action="store_true", help="with --base: add only variables the base file lacks")
    ap.add_argument("--sromrev", type=int, help="with --base: the sromrev line to write (default: the base file's)")
    a = ap.parse_args()
    entries = load_table(Path(a.kext).read_bytes())
    words = words_of(Path(a.dump).read_bytes())
    rev = a.rev
    if rev is None:
        good = pick_rev(entries, words)
        print(f"revisions that decode to devid 0x43ba / boardflags 0x10401001 / a MAC: {good}", file=sys.stderr)
        if len(good) != 1:
            sys.exit("cannot pick the SROM revision automatically; pass --rev")
        rev = good[0]
    pairs = decode(entries, words, rev)
    if a.base:
        base = Path(a.base).read_text()
        sr = a.sromrev or int(re.search(r"^sromrev=(\d+)", base, re.M).group(1))
        text = merge(base, pairs, rev, a.skip, set(filter(None, a.keep.split(","))), sr, a.only, a.only_new, a.drop)
    else:
        text = to_nvram(pairs, rev)
    if a.out:
        Path(a.out).write_text(text)
    else:
        sys.stdout.write(text)
    if a.diff:
        old, new = parse_nvram(Path(a.diff).read_text()), parse_nvram(text)
        print(f"\n# vs {a.diff}: {len(new)} vars decoded, {len(old)} in the old file", file=sys.stderr)
        for k in sorted(set(old) | set(new)):
            if old.get(k) != new.get(k):
                print(f"#  {k}: old={old.get(k)} new={new.get(k)}", file=sys.stderr)


if __name__ == "__main__":
    main()
