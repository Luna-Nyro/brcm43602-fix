#!/usr/bin/python3 -I
"""Get the Broadcom Wi-Fi firmware pieces for the MacBookPro14,2 out of Apple's macOS Sierra update.

The BCM43602 card in this laptop ships with board calibration data (NVRAM text, regulatory CLM blob,
TX-cap blob) that Linux does not have. Apple's AirPortBrcm kexts carry them. Apple only hands them out
inside big macOS packages, so this tool fetches *just the parts of one package it needs*:

  macOSUpdCombo10.12.6Auto.pkg   (1.69 GB)   swcdn.apple.com
    = xar archive -> Payload (pbzx: independent 16 MiB xz chunks) -> cpio, sorted by path

The cpio is sorted, so the tool binary-searches the pbzx chunks with HTTP Range requests, reads the few
chunks that hold IO80211Family.kext/Contents/PlugIns/AirPortBrcm*, and stops. Expect tens of MB, not
1.69 GB. Everything fetched is kept in a sparse cache file, so an interrupted run resumes where it stopped
and a repeat run needs no network.

Integrity (what is and is not checked):
  * TLS to swcdn.apple.com, the pinned object size and ETag, the xar table of contents against its stored sha1;
  * every pbzx chunk read in full is an xz stream with its own CRC; the cpio chain is validated header by header;
  * `fetch --full` downloads the whole Payload and checks it against the sha1 in the xar table of contents.

Commands (all of them keep to the work directory; nothing here touches /lib/firmware or needs root):
  info                    no network: what would be fetched, where it goes
  probe                   one HTTP HEAD: size and ETag still match the pinned values
  fetch --yes             ranged fetch + extract the AirPortBrcm kexts into <work>/kext        [--max-mb N] [--full]
  ls --yes [--prefix P]   list cpio entries under a prefix (uses the same cache)
  verify                  with a fully cached Payload: sha1 check against the xar table of contents
  locate                  look through the extracted kexts for this board's NVRAM / CLM / TX-cap data

Downloads need an explicit --yes. Python runs with -I because everything it reads is untrusted data.
"""
import argparse
import hashlib
import http.client
import json
import lzma
import os
import plistlib
import re
import ssl
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from urllib.parse import urlsplit

URL = ("https://swcdn.apple.com/content/downloads/00/46/041-90440-A_ZHEYENBB0V/"
       "pqqf64h7ogreqvjq2q65291f51ozgj5cbs/macOSUpdCombo10.12.6Auto.pkg")
PKG_NAME = "macOSUpdCombo10.12.6Auto.pkg"
PKG_SIZE = 1694512608
PKG_ETAG = '"4108EAB7E7BAEC70BF8FA927C2B7D54B-26"'
PREFIX = "./System/Library/Extensions/IO80211Family.kext/Contents/PlugIns/AirPortBrcm"
BOARD_ID = "Mac-CAD6701F7CEA0921"
MODEL = "MacBookPro14,2"
VENDOR = "Apple Inc."
PCI_ID = 0x43BA14E4          # IOPCIMatch form of 14e4:43ba, the BCM43602
OUT_NAMES = {k: f"brcmfmac43602-pcie.{VENDOR}-{MODEL}.{k}" for k in ("txt", "clm_blob", "txcap_blob")}
DEFAULT_WORK = Path(__file__).resolve().parent.parent / "work"
DEFAULT_MAX_MB = 120
STEP = 4 << 20               # largest single Range request
PROBE_BYTES = 192 << 10      # head of a chunk read while binary-searching


class FetchError(RuntimeError):
    pass


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- HTTP range reader + cache

class Budget(FetchError):
    pass


class RangeReader:
    """Random-access reads of a remote object through HTTP Range, backed by a sparse cache file.

    Covered intervals are persisted after every request, so a killed run loses at most one request.
    `limit` caps the bytes fetched from the network by this process (None = unlimited)."""

    def __init__(self, url, cache_dir, size=PKG_SIZE, etag=PKG_ETAG, limit=None, allow_network=True):
        self.url, self.size, self.etag, self.limit = url, size, etag, limit
        self.allow_network = allow_network
        self.fetched = 0
        self.requests = 0
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data_path = self.dir / "package.sparse"
        self.cov_path = self.dir / "coverage.json"
        self.cov = self._load_cov()
        if not self.data_path.exists():
            with open(self.data_path, "wb") as f:
                f.truncate(size)
        self.fd = os.open(self.data_path, os.O_RDWR)
        self.conn = None

    # coverage: sorted, disjoint [start, end) pairs
    def _load_cov(self):
        try:
            c = json.loads(self.cov_path.read_text())
            if c.get("size") == self.size and c.get("etag") == self.etag:
                return [tuple(x) for x in c["ranges"]]
        except (OSError, ValueError, KeyError):
            pass
        return []

    def _save_cov(self):
        tmp = self.cov_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"size": self.size, "etag": self.etag, "ranges": self.cov}))
        os.replace(tmp, self.cov_path)

    def _add(self, a, b):
        out, placed = [], False
        for s, e in self.cov:
            if e < a or s > b:
                if s > b and not placed:
                    out.append((a, b))
                    placed = True
                out.append((s, e))
            else:
                a, b = min(a, s), max(b, e)
        if not placed:
            out.append((a, b))
        self.cov = sorted(out)

    def missing(self, a, b):
        gaps, pos = [], a
        for s, e in self.cov:
            if e <= pos:
                continue
            if s >= b:
                break
            if s > pos:
                gaps.append((pos, s))
            pos = max(pos, e)
        if pos < b:
            gaps.append((pos, b))
        return gaps

    def covered_bytes(self):
        return sum(e - s for s, e in self.cov)

    # network
    def _connect(self):
        u = urlsplit(self.url)
        if u.scheme != "https" or not (u.hostname or "").endswith(".apple.com"):
            raise FetchError(f"refusing to fetch from {u.scheme}://{u.hostname}: only https://*.apple.com")
        self.conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=25,
                                                context=ssl.create_default_context())
        self.path = u.path + (f"?{u.query}" if u.query else "")

    def head(self):
        if not self.allow_network:
            raise FetchError("network not allowed")
        self._connect()
        self.conn.request("HEAD", self.path)
        r = self.conn.getresponse()
        r.read()
        if r.status != 200:
            raise FetchError(f"HEAD {self.url}: HTTP {r.status}")
        return int(r.getheader("Content-Length", "-1")), r.getheader("ETag")

    def _get(self, a, b):
        """Fetch [a, b) once; returns bytes or raises."""
        if self.conn is None:
            self._connect()
        self.conn.request("GET", self.path, headers={"Range": f"bytes={a}-{b - 1}", "Accept-Encoding": "identity"})
        r = self.conn.getresponse()
        if r.status != 206:
            r.read()
            raise FetchError(f"GET bytes={a}-{b - 1}: HTTP {r.status} (the server did not honour Range)")
        if self.etag and r.getheader("ETag") not in (None, self.etag):
            r.read()
            raise FetchError(f"the object changed on the server: ETag {r.getheader('ETag')} != {self.etag}")
        m = re.match(r"bytes (\d+)-(\d+)/(\d+)$", r.getheader("Content-Range", ""))
        if not m or int(m.group(1)) != a or int(m.group(3)) != self.size:
            r.read()
            raise FetchError(f"unexpected Content-Range {r.getheader('Content-Range')!r}")
        data = r.read()
        if len(data) != b - a:
            raise FetchError(f"short body: {len(data)} of {b - a} bytes")
        return data

    def _fetch(self, a, b):
        if not self.allow_network:
            raise FetchError(f"bytes {a}-{b} are not in the cache and the network is off")
        if self.limit is not None and self.fetched + (b - a) > self.limit:
            raise Budget(f"download budget of {self.limit >> 20} MB reached (fetched {self.fetched >> 20} MB so far); "
                         "raise it with --max-mb if that is what you want")
        delay = 1.0
        for attempt in range(1, 9):
            try:
                data = self._get(a, b)
                break
            except (OSError, http.client.HTTPException, FetchError) as e:
                self.conn = None
                if isinstance(e, FetchError) and ("ETag" in str(e) or "Range" in str(e) or "refusing" in str(e)):
                    raise
                if attempt == 8:
                    raise FetchError(f"giving up on bytes {a}-{b} after {attempt} tries: {e}")
                log(f"retry {attempt} for bytes {a}-{b - 1}: {e}")
                time.sleep(delay)
                delay = min(delay * 2, 30)
        os.pwrite(self.fd, data, a)
        self._add(a, b)
        self._save_cov()
        self.fetched += b - a
        self.requests += 1

    def read(self, off, n):
        if off < 0 or n < 0 or off + n > self.size:
            raise FetchError(f"read {off}+{n} outside the {self.size}-byte object")
        for ga, gb in self.missing(off, off + n):
            for a in range(ga, gb, STEP):
                self._fetch(a, min(gb, a + STEP))
        return os.pread(self.fd, n, off)

    def have(self, off, n):
        return not self.missing(off, off + n)


# --------------------------------------------------------------------------- xar / pbzx / cpio

def parse_xar(rd):
    """Return (heap_start, {name: (offset_in_heap, length, sha1_hex_or_None)}) after checking the TOC sha1."""
    head = rd.read(0, 28)
    magic, hlen, ver, tclen, tulen, alg = struct.unpack(">4sHHQQI", head)
    if magic != b"xar!":
        raise FetchError("not a xar archive")
    toc_z = rd.read(hlen, tclen)
    toc = zlib.decompress(toc_z)
    if len(toc) != tulen:
        raise FetchError("xar table of contents has the wrong length")
    root = ET.fromstring(toc)
    heap = hlen + tclen
    ck = root.find("./toc/checksum")
    if ck is not None and ck.get("style") == "sha1":
        off, size = int(ck.findtext("offset")), int(ck.findtext("size"))
        want = rd.read(heap + off, size)
        if hashlib.sha1(toc_z).digest() != want:
            raise FetchError("xar table of contents does not match its stored sha1")
    files = {}
    for f in root.iter("file"):
        d = f.find("data")
        if d is None or f.findtext("type") != "file":
            continue
        a = d.find("archived-checksum")
        files[f.findtext("name")] = (int(d.findtext("offset")), int(d.findtext("length")),
                                     a.text.strip().lower() if a is not None and a.get("style") == "sha1" else None)
    return heap, files


class Pbzx:
    """Chunk index of a pbzx stream: chunk k is one standalone xz stream holding stream bytes [k*C, (k+1)*C)."""

    def __init__(self, rd, start, length, index_path):
        self.rd, self.start, self.length = rd, start, length
        hdr = rd.read(start, 12)
        if hdr[:4] != b"pbzx":
            raise FetchError("Payload is not pbzx")
        self.chunk = struct.unpack(">Q", hdr[4:])[0]
        self.index_path = Path(index_path)
        self.chunks = self._load_index()
        self._walk()

    def _load_index(self):
        try:
            j = json.loads(self.index_path.read_text())
            if j.get("start") == self.start and j.get("length") == self.length:
                return [tuple(c) for c in j["chunks"]]
        except (OSError, ValueError, KeyError):
            pass
        return []

    def _walk(self):
        """Hop from header to header (16-byte reads) until the Payload ends."""
        end = self.start + self.length
        pos = self.start + 12 if not self.chunks else self.chunks[-1][1] + self.chunks[-1][2]
        new = 0
        while pos < end:
            flags, clen = struct.unpack(">QQ", self.rd.read(pos, 16))
            if clen <= 0 or pos + 16 + clen > end:
                raise FetchError(f"corrupt pbzx chunk header at {pos}")
            self.chunks.append((flags, pos + 16, clen))
            pos += 16 + clen
            new += 1
            if new % 64 == 0:
                self._save_index()
        if new:
            self._save_index()
            log(f"indexed {len(self.chunks)} pbzx chunks")

    def _save_index(self):
        self.index_path.write_text(json.dumps({"start": self.start, "length": self.length, "chunks": self.chunks}))

    def raw(self, k):
        """Whole chunk k, decompressed (xz CRC verified)."""
        flags, off, clen = self.chunks[k]
        data = self.rd.read(off, clen)
        if data[:6] == b"\xfd7zXZ\x00":
            out = lzma.decompress(data)
        else:
            out = data            # pbzx stores a chunk as is when xz would not shrink it
        return out

    def head(self, k, want):
        """First `want` compressed bytes of chunk k, decompressed as far as they go (no CRC; for probing only)."""
        flags, off, clen = self.chunks[k]
        part = self.rd.read(off, min(clen, want))
        if part[:6] != b"\xfd7zXZ\x00":
            return part, len(part) == clen
        try:
            d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
            return d.decompress(part), len(part) == clen
        except lzma.LZMAError:
            return b"", len(part) == clen


ODC = 76


def _oct(b):
    return int(b, 8) if b and all(48 <= c <= 55 for c in b) else None


def odc_header(buf, p):
    """Parse an odc cpio header at buf[p:]; (mode, namesize, filesize) or None."""
    if buf[p:p + 6] != b"070707" or p + ODC > len(buf):
        return None
    mode, ns, fs = _oct(buf[p + 18:p + 24]), _oct(buf[p + 59:p + 65]), _oct(buf[p + 65:p + 76])
    for fld in (buf[p + 6:p + 12], buf[p + 12:p + 18], buf[p + 24:p + 30], buf[p + 30:p + 36], buf[p + 36:p + 42],
                buf[p + 42:p + 48], buf[p + 48:p + 54], buf[p + 54:p + 59]):
        if _oct(fld) is None:
            return None
    if mode is None or ns is None or fs is None or not 2 <= ns <= 4096:
        return None
    return mode, ns, fs


def odc_name(buf, p, ns):
    raw = bytes(buf[p + ODC:p + ODC + ns])
    if len(raw) == ns and raw[-1:] == b"\0" and raw[:1] == b"." and b"\0" not in raw[:-1]:
        return raw[:-1]
    return None


def resync(buf, start=0, depth=3):
    """Offset of the first cpio header at or after `start` that is followed by a consistent chain of `depth`
    headers (or by the end of the data). A chunk usually begins in the middle of an entry."""
    p = buf.find(b"070707", start)
    while p != -1:
        q, ok = p, 0
        while ok < depth:
            h = odc_header(buf, q)
            if h is None or odc_name(buf, q, h[1]) is None:
                break
            ok += 1
            q += ODC + h[1] + h[2]
            if q + 6 > len(buf):
                ok = depth          # ran out of data: nothing more to check
        if ok >= depth:
            return p
        p = buf.find(b"070707", p + 1)
    return -1


def key(name):
    """cpio sort key: depth-first, bytewise by path component (verified on 14k entries of this very payload)."""
    return tuple(name.split(b"/"))


class Cpio:
    """Walk a cpio stream that arrives as an iterator of byte blocks; the first block begins at an entry."""

    def __init__(self, blocks):
        self.it, self.buf, self.eof = iter(blocks), bytearray(), False

    def _fill(self, n):
        while len(self.buf) < n and not self.eof:
            try:
                self.buf += next(self.it)
            except StopIteration:
                self.eof = True
        return len(self.buf) >= n

    def _take(self, n):
        if not self._fill(n):
            raise FetchError("cpio stream ended in the middle of an entry")
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _skip(self, n):
        while n > 0:
            if not self.buf and not self._fill(1):
                raise FetchError("cpio stream ended in the middle of an entry")
            k = min(n, len(self.buf))
            del self.buf[:k]
            n -= k

    def entries(self, want):
        """Yield (name, mode, data_or_None) for each entry; data is read only when want(name, mode, size)."""
        while True:
            if not self._fill(ODC):
                return
            head = bytes(self.buf[:ODC])
            h = odc_header(head, 0)
            if h is None:
                raise FetchError("bad cpio header (not odc?)")
            mode, ns, fs = h
            self._take(ODC)
            name = self._take(ns)
            if name[-1:] != b"\0":
                raise FetchError("cpio name is not NUL terminated")
            name = name[:-1]
            if name == b"TRAILER!!!":
                return
            if want(name, mode, fs):
                yield name, mode, self._take(fs)
            else:
                self._skip(fs)
                yield name, mode, None


def is_reg(mode):
    return mode & 0o170000 == 0o100000


def is_dir(mode):
    return mode & 0o170000 == 0o040000


# --------------------------------------------------------------------------- locating the region

def first_key(px, k):
    """Sort key of the first complete cpio entry in chunk k (fetches only the head of the chunk)."""
    want = PROBE_BYTES
    while True:
        data, whole = px.head(k, want)
        p = resync(data) if data else -1
        if p >= 0:
            h = odc_header(data, p)
            return key(odc_name(data, p, h[1]))
        if whole:
            return None
        want *= 2


def find_start_chunk(px, prefix):
    """Last chunk whose first entry sorts before `prefix`, minus one for entries that straddle a boundary."""
    k0 = key(prefix.encode())
    lo, hi = 0, len(px.chunks) - 1       # invariant: answer in [lo, hi]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        fk, m = first_key(px, mid), mid
        while fk is None and m < hi:      # a chunk that is the middle of one huge file
            m += 1
            fk = first_key(px, m)
        if fk is None or fk >= k0:
            hi = mid - 1
        else:
            lo = mid
    return max(0, lo - 1)


def scan(px, prefixes, dest, rd):
    """Stream the cpio from the right chunk, save entries under `prefixes`, stop after the region ends."""
    pfx = [p.encode() for p in prefixes]
    kmin, kmax = min(map(key, pfx)), max(map(key, pfx))
    k = find_start_chunk(px, prefixes[0] if len(pfx) == 1 else min(prefixes, key=lambda s: key(s.encode())))
    log(f"region starts near chunk {k} of {len(px.chunks)} (fetched so far {rd.fetched / 1e6:.1f} MB in {rd.requests} requests)")

    def blocks():
        first = True
        for i in range(k, len(px.chunks)):
            data = px.raw(i)
            if first:
                p = resync(data)
                if p < 0:
                    raise FetchError(f"no cpio header found in chunk {i}")
                data, first = data[p:], False
            yield data

    def wanted(name, mode, size):
        return any(name.startswith(p) for p in pfx) and size <= (256 << 20)

    saved, seen_hit = [], False
    for name, mode, data in Cpio(blocks()).entries(wanted):
        hit = any(name.startswith(p) for p in pfx)
        if hit:
            seen_hit = True
        elif seen_hit and key(name) > kmax:
            break                         # sorted: nothing under the prefixes can follow
        elif key(name) > kmax and not hit:
            break
        if not hit:
            continue
        rel = name.decode("utf-8", "surrogateescape")[2:]
        parts = rel.split("/")
        if rel.startswith("/") or any(x in ("", "..") for x in parts):
            raise FetchError(f"unsafe path in package: {rel!r}")
        target = Path(dest).joinpath(*parts)
        if is_dir(mode):
            target.mkdir(parents=True, exist_ok=True)
        elif is_reg(mode):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            os.chmod(target, 0o644)
            saved.append({"path": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        else:
            saved.append({"path": rel, "skipped": f"mode {mode:o} (symlink or special)"})
    return saved


# --------------------------------------------------------------------------- commands

def open_package(args, allow_network=True):
    work = Path(args.work)
    limit = None if getattr(args, "full", False) or not getattr(args, "max_mb", 0) else args.max_mb << 20
    rd = RangeReader(args.url, work / "cache", limit=limit, allow_network=allow_network)
    heap, files = parse_xar(rd)
    if "Payload" not in files:
        raise FetchError(f"no Payload in the package (found {sorted(files)})")
    off, length, sha1 = files["Payload"]
    px = Pbzx(rd, heap + off, length, work / "cache" / "chunks.json")
    return rd, px, (heap + off, length, sha1)


def require_yes(args, what):
    if not args.yes:
        sys.exit(f"{what} downloads from {urlsplit(args.url).hostname}; add --yes to confirm "
                 f"(object {PKG_NAME}, {PKG_SIZE / 1e9:.2f} GB in total, this run is capped at {args.max_mb} MB "
                 f"unless --full)")


def cmd_info(args):
    w = Path(args.work)
    print(f"package   {PKG_NAME}\nurl       {args.url}\nsize      {PKG_SIZE} bytes ({PKG_SIZE / 1e9:.2f} GB)\netag      {PKG_ETAG}")
    print(f"region    {args.prefix}*\nboard     {BOARD_ID} ({MODEL}), PCI {PCI_ID:#010x}\nwork dir  {w}")
    cov = RangeReader(args.url, w / "cache", allow_network=False).covered_bytes() if (w / "cache" / "coverage.json").exists() else 0
    print(f"cached    {cov / 1e6:.1f} MB")
    print("outputs   " + ", ".join(OUT_NAMES.values()))


def cmd_probe(args):
    rd = RangeReader(args.url, Path(args.work) / "cache")
    size, etag = rd.head()
    print(f"size {size} etag {etag}")
    if (size, etag) != (PKG_SIZE, PKG_ETAG):
        sys.exit(f"does not match the pinned values (size {PKG_SIZE}, etag {PKG_ETAG}): the package changed upstream")


def cmd_fetch(args):
    require_yes(args, "fetch")
    work = Path(args.work)
    rd, px, (poff, plen, sha1) = open_package(args)
    if args.full:
        log(f"--full: downloading the whole Payload ({plen / 1e6:.0f} MB, {len(px.chunks)} chunks)")
        done = 0
        for a in range(poff, poff + plen, STEP):
            rd.read(a, min(STEP, poff + plen - a))
            done += 1
            if done % 32 == 0:
                log(f"  {(a - poff) / plen:.0%}")
        verify_payload(rd, poff, plen, sha1)
    dest = work / "kext"
    dest.mkdir(parents=True, exist_ok=True)
    saved = scan(px, args.prefixes or [args.prefix], dest, rd)
    files = [s for s in saved if "size" in s]
    if not files:
        raise FetchError("nothing under the prefix was found; try `ls --yes` on a shorter prefix, or --full")
    (dest / "MANIFEST.json").write_text(json.dumps({"package": PKG_NAME, "etag": PKG_ETAG, "files": saved}, indent=1))
    log(f"saved {len(files)} files ({sum(f['size'] for f in files) / 1e6:.1f} MB) under {dest}; "
        f"network use this run: {rd.fetched / 1e6:.1f} MB in {rd.requests} requests, cache holds {rd.covered_bytes() / 1e6:.1f} MB")
    for s in saved:
        if "skipped" in s:
            log(f"  skipped {s['path']}: {s['skipped']}")


def verify_payload(rd, poff, plen, sha1):
    if not sha1:
        raise FetchError("the package lists no sha1 for its Payload")
    if rd.missing(poff, poff + plen):
        raise FetchError("the Payload is not fully cached; run `fetch --yes --full` first")
    h = hashlib.sha1()
    for a in range(poff, poff + plen, 8 << 20):
        h.update(os.pread(rd.fd, min(8 << 20, poff + plen - a), a))
    if h.hexdigest() != sha1:
        raise FetchError(f"Payload sha1 {h.hexdigest()} does not match the package's {sha1}")
    log(f"Payload sha1 verified ({sha1})")


def cmd_verify(args):
    rd, px, (poff, plen, sha1) = open_package(args, allow_network=False)
    verify_payload(rd, poff, plen, sha1)


def cmd_ls(args):
    require_yes(args, "ls")
    rd, px, _ = open_package(args)
    pfx = args.prefix.encode()
    k = find_start_chunk(px, args.prefix)

    def blocks():
        first = True
        for i in range(k, len(px.chunks)):
            data = px.raw(i)
            if first:
                data, first = data[resync(data):], False
            yield data

    kmax, n = key(pfx), 0
    for name, mode, _ in Cpio(blocks()).entries(lambda *a: False):
        if name.startswith(pfx):
            print(f"{mode:06o} {name.decode(errors='replace')}")
            n += 1
        elif key(name) > kmax:
            break
        if n >= args.limit:
            break
    log(f"network use: {rd.fetched / 1e6:.1f} MB")


# ---- locate: what the extracted kext does (and does not) carry for this board

NVRAM_RUN = re.compile(rb"(?:[A-Za-z][A-Za-z0-9_]{1,24}=[^%\0\r\n]{1,80}\0){8,}")  # Broadcom NVRAM blob: key=value NUL ... (format strings have %)
NVRAM_TEXT = re.compile(rb"^sromrev=\d+\r?\n", re.M)                              # or a plain .txt
HDR_TRX = re.compile(rb"HDR0")                                                       # Broadcom TRX firmware header


def trx_headers(data):
    """TRX images: 'HDR0', then a little-endian length that fits the file (not the 'HDR0 0x%04x' debug string)."""
    out = []
    for m in HDR_TRX.finditer(data):
        n = struct.unpack_from("<I", data, m.start() + 4)[0] if m.start() + 8 <= len(data) else 0
        if 0x10000 <= n <= 0x400000 and m.start() + n <= len(data):
            out.append(m.start())
    return out


def locate(root):
    """Inspect every extracted kext that matches this card's PCI id. Returns a list of finding dicts."""
    want = f"pci{PCI_ID & 0xFFFF:x},{PCI_ID >> 16:x}"
    out = []
    for plist in sorted(Path(root).glob("**/Contents/Info.plist")):
        info = plistlib.loads(plist.read_bytes())
        names = [n for pers in info.get("IOKitPersonalities", {}).values() for n in pers.get("IONameMatch", [])]
        if want not in names:
            continue
        exe = plist.parent / "MacOS" / info.get("CFBundleExecutable", plist.parent.parent.stem)
        data = exe.read_bytes()
        out.append({
            "kext": plist.parent.parent.name, "version": info.get("CFBundleVersion"), "match": want,
            "board_id_present": BOARD_ID.encode() in data,
            "nvram_blobs": [m.start() for m in NVRAM_RUN.finditer(data)] + [m.start() for m in NVRAM_TEXT.finditer(data)],
            "nvram_format_strings": data.count(b"sromrev=%d"),
            "clm_markers": [m.start() for m in re.finditer(rb"CLM DATA", data)],
            "trx_headers": trx_headers(data),
        })
    return out


def cmd_locate(args):
    root = Path(args.work) / "kext"
    found = locate(root)
    if not found:
        raise FetchError(f"no extracted kext under {root} matches pci14e4,{PCI_ID >> 16:x}; run `fetch --yes` first")
    rc = 0
    for f in found:
        print(f"{f['kext']} {f['version']}: matches {f['match']}; names {BOARD_ID}: {f['board_id_present']}")
        print(f"  NVRAM text/blobs: {len(f['nvram_blobs'])}   firmware (TRX) images: {len(f['trx_headers'])}   "
              f"CLM markers: {len(f['clm_markers'])}   NVRAM format strings: {f['nvram_format_strings']}")
        if not f["nvram_blobs"]:
            rc = 1
            print("  verdict: no per-board NVRAM file in this kext. It looks like Apple's host-side (SoftMAC) driver: it only\n"
                  "  has format strings that print an NVRAM from the card's SROM, no firmware image, and its CLM data is the\n"
                  "  compiled-in base set (\"CLM DATA\", ClmImport), not a ready-made clm_blob for brcmfmac's firmware.")
    sys.exit(rc)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=str(DEFAULT_WORK), help="work directory (cache, extracted files)")
    ap.add_argument("--url", default=URL)
    ap.add_argument("--prefix", default=PREFIX)
    ap.add_argument("--yes", action="store_true", help="confirm that downloading from Apple is OK")
    ap.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB, help="network budget for one run (default %(default)s)")
    ap.add_argument("--full", action="store_true", help="fetch: download the whole Payload and verify its sha1")
    ap.add_argument("--limit", type=int, default=200, help="ls: max entries")
    ap.add_argument("--prefixes", nargs="*", help="fetch: several prefixes instead of --prefix")
    ap.add_argument("cmd", choices=["info", "probe", "fetch", "ls", "verify", "locate"])
    args = ap.parse_args(argv)
    try:
        {"info": cmd_info, "probe": cmd_probe, "fetch": cmd_fetch, "ls": cmd_ls, "verify": cmd_verify,
         "locate": cmd_locate}[args.cmd](args)
    except FetchError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
