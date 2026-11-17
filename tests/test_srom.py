#!/usr/bin/python3 -I
"""Tests for tools/dump-srom.py against a fake PCI device (no hardware, no root)."""
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("dump_srom", Path(__file__).resolve().parent.parent / "tools" / "dump-srom.py")
ds = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ds)


class FakeBus:
    """Config space as a bytearray, BAR0 as a function of the current window."""

    def __init__(self, chipid=ds.CHIP_ID, bound=False, pm=0, status_after=None, ids=(ds.VENDOR, ds.DEVICE)):
        self.cfg = bytearray(256)
        self.cfg[0:4] = struct.pack("<HH", *ids)
        self.cfg[6:8] = struct.pack("<H", 0x0010)       # capabilities list present
        self.cfg[0x34] = 0x48
        self.cfg[0x48:0x4A] = bytes([0x01, 0x00])        # PM capability, end of list
        self.cfg[0x4C:0x4E] = struct.pack("<H", pm)
        self.cfg[4:6] = struct.pack("<H", 0x0000)        # memory decoding off after "unload"
        self.cfg[WIN:WIN + 4] = struct.pack("<I", 0x18003000)
        self.chipid, self._bound, self.status_after, self.reads, self.cwrites = chipid, bound, status_after, [], []

    def bound(self):
        return self._bound

    def cr(self, off, n):
        if off == 6 and self.status_after is not None and len(self.reads) >= self.status_after:
            return 0x2000                                  # received master abort
        return int.from_bytes(self.cfg[off:off + n], "little")

    def cw(self, off, n, val):
        self.cwrites.append((off, n, val))
        if off == 6:
            return
        self.cfg[off:off + n] = val.to_bytes(n, "little")

    def window(self):
        return int.from_bytes(self.cfg[WIN:WIN + 4], "little")

    def m16(self, off):
        self.reads.append((self.window(), off))
        return 0x0634 if off == 0x800 + 2 * 64 else (off & 0xFFFF)

    def m32(self, off):
        self.reads.append((self.window(), off))
        if off == 0:
            return 0x10000000 | self.chipid
        return off


WIN = ds.WIN


class Dump(unittest.TestCase):
    def go(self, bus):
        with tempfile.TemporaryDirectory() as d:
            try:
                info = ds.run(bus, d, log=lambda m: None, sleep=lambda s: None)
                files = {p.name: p.read_bytes() for p in Path(d).iterdir()}
            except ds.Abort:
                files, info = {p.name: p.read_bytes() for p in Path(d).iterdir()}, None
                raise
            return info, files

    def test_happy_path_writes_all_three_dumps_and_restores_the_window(self):
        bus = FakeBus()
        info, files = self.go(bus)
        self.assertTrue(info["ok"])
        self.assertEqual(sorted(files), ["bar0-1000.bin", "cc-0800.bin", "cc-regs-0000.bin", "info.json"])
        self.assertEqual(len(files["cc-regs-0000.bin"]), 512)
        self.assertEqual(len(files["cc-0800.bin"]), 2048)
        self.assertEqual(len(files["bar0-1000.bin"]), 4096)
        self.assertEqual(struct.unpack_from("<H", files["cc-0800.bin"], 128)[0], 0x0634)
        self.assertEqual(bus.window(), 0x18003000)          # put back
        self.assertTrue(all(w == ds.CC_BASE for w, _ in bus.reads))
        self.assertEqual(bus.cr(4, 2) & 2, 2)               # memory decoding was switched on

    def test_wakes_a_device_left_in_d3(self):
        bus = FakeBus(pm=3)
        info, _ = self.go(bus)
        self.assertEqual(info["pm_state_before"], 3)
        self.assertEqual(bus.cr(0x4C, 2) & 3, 0)

    def test_refuses_while_a_driver_is_bound(self):
        bus = FakeBus(bound=True)
        with self.assertRaises(ds.Abort):
            self.go(bus)
        self.assertEqual(bus.reads, [])
        self.assertEqual(bus.cwrites, [])

    def test_refuses_another_device(self):
        with self.assertRaises(ds.Abort):
            self.go(FakeBus(ids=(0x8086, 0x1234)))

    def test_wrong_chipid_stops_before_the_srom_area_and_restores_the_window(self):
        bus = FakeBus(chipid=0x4350)
        with self.assertRaises(ds.Abort):
            self.go(bus)
        self.assertEqual(len(bus.reads), 1)                  # only the chipid register
        self.assertEqual(bus.window(), 0x18003000)

    def test_pci_error_mid_read_stops_and_restores_the_window(self):
        bus = FakeBus(status_after=140)                      # after the register block, inside the 0x800 area
        with self.assertRaises(ds.Abort):
            self.go(bus)
        self.assertLess(len(bus.reads), 400)
        self.assertEqual(bus.window(), 0x18003000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
