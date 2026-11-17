#!/usr/bin/python3 -I
"""Offline tests for tools/fetch-brcm-fw.py: a synthetic xar/pbzx/cpio package served over a real local
HTTP server that honours Range. Nothing here touches the network beyond 127.0.0.1.

  python3 -I tests/test_fetch.py
"""
import hashlib
import http.client
import http.server
import importlib.util
import io
import lzma
import os
import random
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fetchfw", HERE.parent / "tools" / "fetch-brcm-fw.py")
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)

CHUNK = 256 * 1024


def odc(name, data=b"", mode=0o100644):
    n = name.encode() + b"\0"
    return (b"070707" + b"000000" + b"000001" + b"%06o" % mode + b"000000" * 3 + b"000000" + b"00000000000"
            + b"%06o" % len(n) + b"%011o" % len(data) + n + data)


def tree(rng, kext_payloads):
    """Sorted (depth-first, bytewise per component) entries with the target kexts in the middle."""
    paths = {}
    for i in range(400):
        paths[f"./Applications/App{i:03d}.app/Contents/Resources/blob.bin"] = rng.randbytes(rng.randint(100, 9000))
    for i in range(300):
        paths[f"./Library/Thing{i:03d}/data.bin"] = rng.randbytes(rng.randint(100, 6000))
    for i in range(300):
        paths[f"./System/Library/CoreServices/Cs{i:03d}.bundle/x.bin"] = rng.randbytes(rng.randint(100, 20000))
    base = "./System/Library/Extensions/IO80211Family.kext/Contents/PlugIns/"
    for k, v in kext_payloads.items():
        paths[base + k] = v
    paths[base + "AirPortAtheros40.kext/Contents/MacOS/AirPortAtheros40"] = rng.randbytes(50000)
    paths[base + "AirPortBroadcomOther/x"] = b"not ours"
    for i in range(300):
        paths[f"./usr/lib/lib{i:03d}.dylib"] = rng.randbytes(rng.randint(100, 30000))
    dirs = set()
    for p in paths:
        parts = p.split("/")
        for j in range(1, len(parts)):
            dirs.add("/".join(parts[:j]))
    allp = {d: None for d in dirs}
    allp.update(paths)
    return sorted(allp.items(), key=lambda kv: tuple(kv[0].encode().split(b"/")))


def build_cpio(entries):
    out = io.BytesIO()
    for name, data in entries:
        if data is None:
            out.write(odc(name, b"", 0o040755))
        else:
            out.write(odc(name, data))
    out.write(odc("TRAILER!!!"))
    return out.getvalue()


def build_pbzx(cpio, chunk=CHUNK, corrupt_chunk=None):
    out = io.BytesIO()
    out.write(b"pbzx" + struct.pack(">Q", chunk))
    for i, a in enumerate(range(0, len(cpio), chunk)):
        part = cpio[a:a + chunk]
        z = bytearray(lzma.compress(part, check=lzma.CHECK_CRC64, preset=1))
        if i == corrupt_chunk:
            z[len(z) // 2] ^= 0xFF
        out.write(struct.pack(">QQ", len(part), len(z)) + bytes(z))
    return out.getvalue()


def build_xar(payload):
    pk_sha1 = hashlib.sha1(payload).hexdigest()
    heap_files = [("Payload", payload)]
    # toc checksum lives at heap offset 0 (20 bytes); files follow
    off, entries = 20, []
    for i, (n, d) in enumerate(heap_files, 1):
        entries.append(f'<file id="{i}"><name>{n}</name><type>file</type><data><offset>{off}</offset>'
                       f'<size>{len(d)}</size><length>{len(d)}</length>'
                       f'<archived-checksum style="sha1">{hashlib.sha1(d).hexdigest()}</archived-checksum></data></file>')
        off += len(d)
    toc = ('<?xml version="1.0" encoding="UTF-8"?><xar><toc><checksum style="sha1"><offset>0</offset><size>20</size>'
           '</checksum>' + "".join(entries) + "</toc></xar>").encode()
    toc_z = zlib.compress(toc)
    head = struct.pack(">4sHHQQI", b"xar!", 28, 1, len(toc_z), len(toc), 1)
    return head + toc_z + hashlib.sha1(toc_z).digest() + payload, pk_sha1


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def make_handler(blob, etag, stats):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True

        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("ETag", etag)
            self.end_headers()

        def do_GET(self):
            rng = self.headers.get("Range")
            if not rng:
                self.send_response(200)
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                stats["bytes"] += len(blob)
                return
            a, b = rng.split("=")[1].split("-")
            a, b = int(a), int(b)
            body = blob[a:b + 1]
            stats["bytes"] += len(body)
            stats["requests"] += 1
            if stats.get("fail_next", 0) > 0:        # drop the connection mid-body once to exercise retries
                stats["fail_next"] -= 1
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(blob)}")
                self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body[:len(body) // 2])
                self.wfile.flush()
                self.close_connection = True
                return
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(blob)}")
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(body)
    return H


class LocalReader(fw.RangeReader):
    """Same code path as the real one, minus the https://*.apple.com guard."""
    port = 0

    def _connect(self):
        self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.path = "/pkg"


class Fetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = random.Random(7)
        cls.kext = {
            "AirPortBrcm4360.kext/Contents/Info.plist": b"<plist/>",
            "AirPortBrcm4360.kext/Contents/MacOS/AirPortBrcm4360": rng.randbytes(300000),
            "AirPortBrcmNIC.kext/Contents/Info.plist": b"<plist>nic</plist>",
            "AirPortBrcmNIC.kext/Contents/MacOS/AirPortBrcmNIC": rng.randbytes(500000),
        }
        cls.cpio = build_cpio(tree(rng, cls.kext))
        cls.payload = build_pbzx(cls.cpio)
        cls.pkg, cls.sha1 = build_xar(cls.payload)
        cls.etag = '"test-etag"'
        cls.stats = {"bytes": 0, "requests": 0}
        cls.srv = Server(("127.0.0.1", 0), make_handler(cls.pkg, cls.etag, cls.stats))
        LocalReader.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        fw.PROBE_BYTES = 4096
        fw.STEP = 1 << 20

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def reader(self, d, **kw):
        kw.setdefault("size", len(self.pkg))
        kw.setdefault("etag", self.etag)
        return LocalReader("http://x/pkg", d, **kw)

    def open(self, d, **kw):
        rd = self.reader(Path(d) / "cache", **kw)
        heap, files = fw.parse_xar(rd)
        off, length, sha1 = files["Payload"]
        return rd, fw.Pbzx(rd, heap + off, length, Path(d) / "cache" / "chunks.json"), (heap + off, length, sha1)

    def test_extracts_only_the_region_with_a_fraction_of_the_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            rd, px, _ = self.open(d)
            out = Path(d) / "kext"
            saved = fw.scan(px, [fw.PREFIX], out, rd)
            got = {s["path"]: s for s in saved if "size" in s}
            base = fw.PREFIX[2:].rsplit("AirPortBrcm", 1)[0]
            self.assertEqual({base + k for k in self.kext}, set(got))
            for k, v in self.kext.items():
                self.assertEqual((out / (base + k)).read_bytes(), v)
            self.assertFalse((out / base / "AirPortAtheros40.kext").exists())
            self.assertFalse((out / base / "AirPortBroadcomOther").exists())
            self.assertLess(rd.fetched, len(self.pkg) * 0.45, f"fetched {rd.fetched} of {len(self.pkg)}")
            self.assertGreater(len(px.chunks), 20)

    def test_resume_from_cache_needs_no_network(self):
        with tempfile.TemporaryDirectory() as d:
            rd, px, _ = self.open(d)
            fw.scan(px, [fw.PREFIX], Path(d) / "k1", rd)
            before = self.stats["requests"]
            rd2, px2, _ = self.open(d, allow_network=False)
            fw.scan(px2, [fw.PREFIX], Path(d) / "k2", rd2)
            self.assertEqual(before, self.stats["requests"])
            self.assertEqual(rd2.fetched, 0)

    def test_budget_stops_the_run_and_keeps_what_it_had(self):
        with tempfile.TemporaryDirectory() as d:
            rd, px, _ = self.open(d, limit=300_000)
            with self.assertRaises(fw.Budget):
                fw.scan(px, [fw.PREFIX], Path(d) / "k", rd)
            self.assertGreater(rd.covered_bytes(), 0)
            rd2, px2, _ = self.open(d)                       # a new run carries on and finishes
            saved = fw.scan(px2, [fw.PREFIX], Path(d) / "k", rd2)
            self.assertTrue([s for s in saved if "size" in s])

    def test_dropped_connection_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            self.stats["fail_next"] = 2
            fw.time.sleep, orig = (lambda s: None), fw.time.sleep
            try:
                rd, px, _ = self.open(d)
                saved = fw.scan(px, [fw.PREFIX], Path(d) / "k", rd)
            finally:
                fw.time.sleep = orig
            self.assertTrue([s for s in saved if "size" in s])
            self.assertEqual(self.stats["fail_next"], 0)

    def test_full_payload_verifies_against_the_xar_sha1(self):
        with tempfile.TemporaryDirectory() as d:
            rd, px, (poff, plen, sha1) = self.open(d)
            for a in range(poff, poff + plen, fw.STEP):
                rd.read(a, min(fw.STEP, poff + plen - a))
            fw.verify_payload(rd, poff, plen, sha1)
            os.pwrite(rd.fd, b"\xff", poff + 1000)
            with self.assertRaises(fw.FetchError):
                fw.verify_payload(rd, poff, plen, sha1)

    def test_changed_object_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(fw.FetchError):
                self.reader(Path(d) / "cache", etag='"other"').read(0, 28)

    def test_corrupt_chunk_is_caught_by_the_xz_crc(self):
        rng = random.Random(3)
        cpio = build_cpio(tree(rng, self.kext))
        pkg, _ = build_xar(build_pbzx(cpio, corrupt_chunk=None))
        bad, _ = build_xar(build_pbzx(cpio, corrupt_chunk=len(cpio) // CHUNK // 2))
        # find the corrupted chunk: scanning must raise, not silently save garbage
        stats = {"bytes": 0, "requests": 0}
        srv = Server(("127.0.0.1", 0), make_handler(bad, self.etag, stats))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            class R(LocalReader):
                port = srv.server_address[1]
            with tempfile.TemporaryDirectory() as d:
                rd = R("http://x/pkg", Path(d) / "c", size=len(bad), etag=self.etag)
                heap, files = fw.parse_xar(rd)
                off, length, _ = files["Payload"]
                px = fw.Pbzx(rd, heap + off, length, Path(d) / "c" / "chunks.json")
                with self.assertRaises((lzma.LZMAError, fw.FetchError)):
                    for k in range(len(px.chunks)):
                        px.raw(k)
        finally:
            srv.shutdown()


class Locate(unittest.TestCase):
    def kext(self, d, binary):
        k = Path(d) / "kext" / "AirPortBrcmX.kext" / "Contents"
        (k / "MacOS").mkdir(parents=True)
        import plistlib
        (k / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "AirPortBrcmX", "CFBundleVersion": "1",
            "IOKitPersonalities": {"p": {"IONameMatch": ["pci14e4,43ba", "pci14e4,4331"]}}}))
        (k / "MacOS" / "AirPortBrcmX").write_bytes(binary)
        return Path(d) / "kext"

    def test_format_strings_are_not_nvram_and_debug_hdr0_is_not_trx(self):
        fmt = b"\0".join(b"%s=%%x" % k for k in (b"sromrev", b"devid", b"boardtype", b"boardrev", b"aa2g", b"aa5g",
                                                   b"txchain", b"rxchain", b"regrev")) + b"\0DMACompletion HDR0 0x%04x \0"
        with tempfile.TemporaryDirectory() as d:
            f = fw.locate(self.kext(d, os.urandom(1000) + b"Mac-CAD6701F7CEA0921\0" + fmt))
            self.assertEqual(len(f), 1)
            self.assertTrue(f[0]["board_id_present"])
            self.assertEqual(f[0]["nvram_blobs"], [])
            self.assertEqual(f[0]["trx_headers"], [])

    def test_a_real_nvram_blob_and_trx_image_are_found(self):
        nv = b"\0".join(b"%s=%s" % kv for kv in ((b"sromrev", b"11"), (b"boardtype", b"0x61b"), (b"boardrev", b"0x1421"),
                                                  (b"macaddr", b"00:11:22:33:44:55"), (b"ccode", b"US"), (b"aa2g", b"7"),
                                                  (b"aa5g", b"7"), (b"txchain", b"7"), (b"rxchain", b"7"))) + b"\0"
        trx = b"HDR0" + struct.pack("<I", 0x20000) + os.urandom(0x20000)
        with tempfile.TemporaryDirectory() as d:
            f = fw.locate(self.kext(d, b"junk" * 50 + nv + b"\0\0" + trx))[0]
            self.assertEqual(len(f["nvram_blobs"]), 1)
            self.assertEqual(len(f["trx_headers"]), 1)

    def test_kext_for_another_card_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.kext(d, b"x")
            plist = next(root.glob("**/Info.plist"))
            import plistlib
            plist.write_bytes(plistlib.dumps({"CFBundleExecutable": "AirPortBrcmX",
                                              "IOKitPersonalities": {"p": {"IONameMatch": ["pci14e4,4331"]}}}))
            self.assertEqual(fw.locate(root), [])


class Pieces(unittest.TestCase):
    def test_resync_skips_the_tail_of_an_entry_and_ignores_fake_magic(self):
        junk = b"xx070707yy" + os.urandom(100) + b"070707" + b"9" * 70
        buf = junk + odc("./a/b", b"hello") + odc("./a/c", b"x" * 10) + odc("./a/d", b"") + odc("TRAILER!!!")
        p = fw.resync(buf)
        self.assertEqual(buf[p:p + 6], b"070707")
        self.assertEqual(p, len(junk))

    def test_sort_key_is_depth_first(self):
        names = [b"./a", b"./a/b", b"./a/b/c", b"./a/b-x", b"./a-b", b"./b"]
        self.assertEqual(sorted(names, key=fw.key), names)

    def test_cpio_walk_reads_across_block_boundaries(self):
        raw = odc("./d", b"", 0o040755) + odc("./d/f", b"A" * 1000) + odc("./d/g", b"B" * 5) + odc("TRAILER!!!")
        for step in (1, 7, 76, 500, len(raw)):
            blocks = [raw[i:i + step] for i in range(0, len(raw), step)]
            got = [(n, d) for n, m, d in fw.Cpio(blocks).entries(lambda n, m, s: n == b"./d/f")]
            self.assertEqual([n for n, _ in got], [b"./d", b"./d/f", b"./d/g"])
            self.assertEqual(got[1][1], b"A" * 1000)

    def test_unsafe_paths_are_refused(self):
        rng = random.Random(1)
        entries = [("./System/Library/Extensions/IO80211Family.kext/Contents/PlugIns/AirPortBrcm../x", b"evil")]
        entries[0] = ("./System/Library/Extensions/IO80211Family.kext/Contents/PlugIns/AirPortBrcm/../../../../../x", b"evil")
        cpio = build_cpio(entries)
        pkg, _ = build_xar(build_pbzx(cpio))

        class R(LocalReader):
            def _connect(self):
                raise AssertionError("no network")
        with tempfile.TemporaryDirectory() as d:
            rd = R("http://x/pkg", Path(d) / "c", size=len(pkg), etag="e")
            os.pwrite(rd.fd, pkg, 0)
            rd._add(0, len(pkg))
            heap, files = fw.parse_xar(rd)
            off, length, _ = files["Payload"]
            px = fw.Pbzx(rd, heap + off, length, Path(d) / "c" / "chunks.json")
            with self.assertRaises(fw.FetchError):
                fw.scan(px, [fw.PREFIX], Path(d) / "out", rd)
            self.assertFalse((Path(d) / "x").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
