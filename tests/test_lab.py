#!/usr/bin/python3 -I
"""Parser/summary tests for tools/nvram-lab.py and the NVRAM merge in tools/decode-srom.py."""
import importlib.util
import struct
import unittest
from pathlib import Path

T = Path(__file__).resolve().parent.parent / "tools"


def load(name, fn):
    spec = importlib.util.spec_from_file_location(name, T / fn)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


lab = load("nvram_lab", "nvram-lab.py")
dec = load("decode_srom", "decode-srom.py")

SCAN = """BSS 02:00:00:00:00:e6(on wlp2s0)
\tlast seen: 1 ms
\tfreq: 5180.0
\tsignal: -53.00 dBm
\tSSID: HomeNet
BSS 02:00:00:00:00:e5(on wlp2s0) -- associated
\tfreq: 2412.0
\tsignal: -80.00 dBm
\tSSID: HomeNet
BSS 02:00:00:00:01:14(on wlp2s0)
\tfreq: 2437
\tsignal: -95.00 dBm
\tSSID: Neighbour
BSS 02:00:00:00:00:e7(on wlp2s0)
\tfreq: 2412.0
\tsignal: -79.00 dBm
\tSSID:
"""


class Lab(unittest.TestCase):
    def test_parse_scan(self):
        b = lab.parse_scan(SCAN)
        self.assertEqual(len(b), 4)
        self.assertEqual((b[1]["bssid"], b[1]["freq"], b[1]["signal"], b[1]["ssid"]), ("02:00:00:00:00:e5", 2412, -80.0, "HomeNet"))
        self.assertEqual(b[2]["freq"], 2437)
        self.assertEqual(b[3]["ssid"], "")

    def test_summary_takes_the_strongest_per_band_over_several_scans(self):
        s2 = SCAN.replace("-80.00", "-62.00")
        r = lab.summarize([lab.parse_scan(SCAN), lab.parse_scan(s2)], "HomeNet")
        self.assertEqual(r["best_signal_dbm"], {"g24": -62.0, "g5": -53.0})
        self.assertEqual(r["bss_seen"], {"g24": 3, "g5": 1})

    def test_summary_without_the_ssid(self):
        r = lab.summarize([lab.parse_scan(SCAN)], "Nope")
        self.assertEqual(r["best_signal_dbm"], {"g24": None, "g5": None})


class Join(unittest.TestCase):
    def test_psk_matches_the_ieee_test_vector(self):
        self.assertEqual(lab.psk_hex("IEEE", "password"), "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")

    def test_raw_psk_is_kept_and_quotes_in_names_are_safe(self):
        raw = "ab" * 32
        self.assertEqual(lab.psk_hex("x", raw), raw)
        conf = lab.supplicant_conf('Joe\'s "Wi-Fi"', 'pa"ss word!', "aa:bb:cc:dd:ee:ff", 2412)
        self.assertIn("ssid=" + 'Joe\'s "Wi-Fi"'.encode().hex(), conf)
        self.assertNotIn("pa\"ss", conf)
        self.assertIn("bssid=aa:bb:cc:dd:ee:ff", conf)
        self.assertIn("scan_freq=2412", conf)

    def test_best_bss_picks_the_right_band(self):
        scans = [lab.parse_scan(SCAN)]
        self.assertEqual(lab.best_bss(scans, "HomeNet", "g24")["bssid"], "02:00:00:00:00:e5")
        self.assertEqual(lab.best_bss(scans, "HomeNet", "g5")["bssid"], "02:00:00:00:00:e6")
        self.assertIsNone(lab.best_bss(scans, "Nope", "g5"))


class Merge(unittest.TestCase):
    BASE = "sromrev=11\nboardtype=0x61b\nmacaddr=00:90:4c:0d:f4:3e\nccode=00\nswctrlmap_2g=0x1,0x2\nlength_txcal=65535\n"

    def test_overlay_keeps_what_the_srom_lacks_and_skips(self):
        pairs = [("boardtype", "0x157"), ("macaddr", "02:11:22:33:44:55"), ("ccode", "X0"), ("length_txcal", "259"), ("newvar", "5")]
        out = dec.merge(self.BASE, pairs, 12, r"_txcal|^ccode$", set(), 11)
        self.assertEqual(out.splitlines()[:6], ["sromrev=11", "boardtype=0x157", "macaddr=02:11:22:33:44:55", "ccode=00",
                                                  "swctrlmap_2g=0x1,0x2", "length_txcal=65535"])
        self.assertIn("newvar=5", out)

    def test_keep_and_sromrev_override(self):
        out = dec.merge(self.BASE, [("boardtype", "0x157"), ("macaddr", "aa:bb:cc:dd:ee:ff")], 12, "", {"boardtype"}, 12)
        self.assertIn("sromrev=12", out)
        self.assertIn("boardtype=0x61b", out)
        self.assertIn("macaddr=aa:bb:cc:dd:ee:ff", out)


class Decode(unittest.TestCase):
    def table(self):
        E = dec.Entry
        return [
            E("boardflags", 0xFFFFFF00, dec.MORE | dec.PRHEX, 2, 0xFFFF), E(None, 0, 0, 3, 0xFFFF),
            E("macaddr", 0xFFFFFF00, dec.ETHADDR, 4, 0xFFFF),
            E("ccode", 0xFFFFFF00, dec.CCODE, 7, 0xFFFF),
            E("aa2g", 0xFFFFFF00, 0, 8, 0x00FF),
            E("noise", 0xFFFFFF00, dec.ARRAY, 9, 0x00FF), E(None, 0, dec.ARRAY, 9, 0xFF00), E(None, 0, 0, 10, 0x00FF),
            E("signed", 0xFFFFFF00, dec.PRSIGN, 11, 0x00FF),
            E("skipme", 0x00000001, 0, 12, 0xFFFF),
            E("novar", 0xFFFFFF00, dec.NOVAR, 13, 0xFFFF),
            E("noffs", 0xFFFFFF00, dec.NOFFS, 14, 0xFFFF),
            E("shifted", 0xFFFFFF00, 0, 15, 0x0F00),
        ]

    def test_decode(self):
        w = [0] * 16
        w[2], w[3] = 0x1001, 0x1040
        w[4], w[5], w[6] = 0x0211, 0x2233, 0x4455
        w[7] = 0x5830
        w[8] = 0x0007
        w[9], w[10] = 0x1211, 0x0013
        w[11] = 0x00FE
        w[14] = 0xFFFF
        w[15] = 0x0500
        d = dict(dec.decode(self.table(), w, 12))
        self.assertEqual(d["boardflags"], "0x10401001")
        self.assertEqual(d["macaddr"], "02:11:22:33:44:55")
        self.assertEqual(d["ccode"], "X0")
        self.assertEqual(d["aa2g"], "7")
        self.assertEqual(d["noise"], "17,18,19")
        self.assertEqual(d["signed"], "-2")
        self.assertEqual(d["shifted"], "5")
        for gone in ("skipme", "novar", "noffs"):
            self.assertNotIn(gone, d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
