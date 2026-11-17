#!/usr/bin/python3 -I
"""Dump the BCM43602's SROM/OTP shadow and ChipCommon registers through PCI BAR0, with brcmfmac UNLOADED.

Apple's Sierra driver reads this card's calibration (board NVRAM) from the card itself; brcmfmac cannot, and
no Apple NVRAM file exists to download. This is the read half of getting it back: raw words go to --out, a
decoder comes later.

Run it only through dist/srom-dump-run.sh, which stops the Wi-Fi services, unloads brcmfmac, arms a restore timer, runs
this, and reloads the driver. This script refuses to run while a driver is bound to the device.

Reads only. The one write is the BAR0 window register (config 0x80), pointed at ChipCommon and put back; the PM
state is forced to D0 if the unload left it asleep. After every group of reads the PCI status register is
checked and the run stops at the first master/target abort or parity error.
"""
import argparse
import json
import mmap
import os
import struct
import sys
import time
from pathlib import Path

VENDOR, DEVICE = 0x14E4, 0x43BA
CHIP_ID = 43602                  # ChipCommon chipid[15:0]
CC_BASE = 0x18000000             # ChipCommon core on the backplane
WIN = 0x80                       # BAR0 window register in config space
STATUS_ERR = 0x8000 | 0x4000 | 0x2000 | 0x1000 | 0x0100    # parity, SERR, rx master abort, rx target abort, data parity


class Abort(RuntimeError):
    pass


class SysfsBus:
    """Config space and BAR0 of one PCI device through sysfs."""

    def __init__(self, bdf):
        self.dir = Path("/sys/bus/pci/devices") / bdf
        self.cfg = os.open(self.dir / "config", os.O_RDWR)
        fd = os.open(self.dir / "resource0", os.O_RDONLY)
        self.mm = mmap.mmap(fd, 32768, prot=mmap.PROT_READ)
        os.close(fd)
        self.v = memoryview(self.mm)
        self.h = self.v.cast("H")
        self.w = self.v.cast("I")

    def bound(self):
        return (self.dir / "driver").exists()

    def cr(self, off, n):
        return int.from_bytes(os.pread(self.cfg, n, off), "little")

    def cw(self, off, n, val):
        os.pwrite(self.cfg, val.to_bytes(n, "little"), off)

    def m16(self, off):
        return self.h[off // 2]

    def m32(self, off):
        return self.w[off // 4]


def run(bus, out, log=print, sleep=time.sleep):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if bus.bound():
        raise Abort("a driver is still bound to the device; unload brcmfmac first")
    if (bus.cr(0, 2), bus.cr(2, 2)) != (VENDOR, DEVICE):
        raise Abort(f"not a 14e4:43ba device (found {bus.cr(0, 2):04x}:{bus.cr(2, 2):04x})")
    info = {"started": time.strftime("%F %T")}

    # power: D0 and memory decoding on
    ptr = bus.cr(0x34, 1) if bus.cr(0x06, 2) & 0x10 else 0
    for _ in range(48):
        if not ptr:
            break
        if bus.cr(ptr, 1) == 0x01:
            pmcsr = bus.cr(ptr + 4, 2)
            info["pm_state_before"] = pmcsr & 3
            if pmcsr & 3:
                bus.cw(ptr + 4, 2, pmcsr & ~3)
                sleep(0.05)
                log("device was in D%d; put in D0" % (pmcsr & 3))
            break
        ptr = bus.cr(ptr + 1, 1)
    cmd = bus.cr(0x04, 2)
    if not cmd & 2:
        bus.cw(0x04, 2, cmd | 2)
        log("enabled memory decoding")

    def check(what):
        st = bus.cr(0x06, 2)
        if st & STATUS_ERR:
            bus.cw(0x06, 2, st & STATUS_ERR)          # write-1-to-clear
            raise Abort(f"PCI status {st:#06x} after {what}: stopping")

    old = bus.cr(WIN, 4)
    info["window_before"] = f"{old:#010x}"
    try:
        bus.cw(WIN, 4, CC_BASE)
        if bus.cr(WIN, 4) != CC_BASE:
            raise Abort(f"BAR0 window did not take {CC_BASE:#x} (reads back {bus.cr(WIN, 4):#x})")
        chip = bus.m32(0)
        info["chipid_reg"] = f"{chip:#010x}"
        if chip & 0xFFFF != CHIP_ID:
            raise Abort(f"ChipCommon chipid reads {chip:#010x}, expected ...{CHIP_ID:#x}: window is not ChipCommon, stopping")
        check("chipid")

        regs = [bus.m32(o) for o in range(0, 0x200, 4)]
        check("ChipCommon registers")
        (out / "cc-regs-0000.bin").write_bytes(struct.pack("<128I", *regs))
        info["cc_caps"] = f"{regs[1]:#010x}"
        info["cc_chipstatus"] = f"{regs[0x2C // 4]:#010x}"

        words = []
        for base in range(0x800, 0x1000, 0x80):
            words += [bus.m16(o) for o in range(base, base + 0x80, 2)]
            check(f"ChipCommon+{base:#x}")
        (out / "cc-0800.bin").write_bytes(struct.pack("<1024H", *words))

        shadow = []
        for base in range(0x1000, 0x2000, 0x100):
            shadow += [bus.m32(o) for o in range(base, base + 0x100, 4)]
            check(f"BAR0+{base:#x}")
        (out / "bar0-1000.bin").write_bytes(struct.pack("<1024I", *shadow))
        info["ok"] = True
    finally:
        try:
            bus.cw(WIN, 4, old)
        except OSError:
            pass
    info["finished"] = time.strftime("%F %T")
    (out / "info.json").write_text(json.dumps(info, indent=1))
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bdf", default="0000:02:00.0")
    a = ap.parse_args()
    if os.geteuid():
        sys.exit("run as root (through dist/srom-dump-run.sh)")
    out = Path(a.out)
    try:
        info = run(SysfsBus(a.bdf), out, log=lambda m: print(m, flush=True))
    except Abort as e:
        out.mkdir(parents=True, exist_ok=True)
        (out / "info.json").write_text(json.dumps({"ok": False, "error": str(e)}, indent=1))
        sys.exit(f"abort: {e}")
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
