# brcm43602-fix — investigating BCM43602 Wi-Fi on the MacBookPro14,2 under Linux

Tools and research notes for making the Broadcom **BCM43602** (`14e4:43ba`, driver `brcmfmac`, firmware
7.35.177.61) in the 2017 13" MacBook Pro (**MacBookPro14,2**) join 2.4 GHz networks and transmit at a normal
rate under Linux.

> **Status: not fixed.** As far as these experiments show, `brcmfmac` with firmware 7.35.177.61 cannot be fixed
> with an NVRAM file alone. The tools here are carefully guarded, tested building blocks (fetch Apple's driver, read the
> card's own calibration from its SROM, decode it, A/B-test candidate NVRAM files on the real card with an
> automatic rollback). They are published so others don't have to repeat the work. See
> [Findings](#findings) and [Limitations](#status-and-limitations).

## Why

On this laptop, with the stock Linux setup:

* no board-specific NVRAM, `clm_blob` or `txcap_blob` is loaded (the kernel logs that it falls back to the
  generic `brcmfmac43602-pcie.txt`);
* joins on 2.4 GHz fail (0 of 47 in one measurement) while scans still see the access point; 5 GHz works;
* TX rate is far below RX (e.g. 24 Mbit/s TX vs 175–234 Mbit/s RX on bad days).

The plan was to recover the board's real calibration data and give it to `brcmfmac`.

## What is in here

| Path | What |
|---|---|
| `tools/fetch-brcm-fw.py` | Ranged, resumable download of *only* the `AirPortBrcm*` kexts out of Apple's 1.69 GB macOS 10.12.6 combo update (xar → pbzx → cpio, binary search over the sorted cpio with HTTP Range requests). About 30 MB of network use. Integrity checks, a download cap, and a `locate` command that looks for NVRAM/CLM/firmware in the kext. Python stdlib only. |
| `tools/dump-srom.py`, `dist/srom-dump-run.sh` | Read the card's SROM through PCI BAR0 with `brcmfmac` unloaded (reads only). The wrapper arms a restore timer first and reloads the driver afterwards. |
| `tools/decode-srom.py` | SROM words + the Broadcom variable table found in Apple's kext → `name=value` NVRAM text, or an overlay of decoded values onto an existing NVRAM file. |
| `tools/nvram-lab.py` | Candidate-NVRAM lab: install each candidate, reload the driver, scan, optionally join, record, always restore. |
| `install.sh` | Installs candidate files under the exact names `brcmfmac` requests for this machine, with backup, boot failsafe and an optional live reload under a 10-minute self-reverting timer. |
| `rollback.sh` | Restores/removes every file `install.sh` recorded, disarms the failsafe, reloads the driver. |
| `dist/brcm-fw-failsafe.sh`, `dist/brcm43602-fix-failsafe.service` | Boot failsafe: reverts the firmware files if the first boot after an install has no working Wi-Fi. |
| `tests/` | Offline tests: a synthetic package over a local Range server, fake directories and stub commands, a fake PCI device, parser and decoder units. No root, no hardware, no network beyond 127.0.0.1. |

Nothing from Apple or Broadcom is included in this repository. `fetch-brcm-fw.py` downloads Apple's package
from Apple's CDN to your machine; the results stay in `work/` (git-ignored).

## Requirements

* **Hardware:** MacBookPro14,2 with the BCM43602 (`lspci -nn` shows `14e4:43ba`). The file names, PCI IDs, chip
  ID check and SROM table offset are specific to this card and machine. Other machines are untested.
* **OS:** Linux with systemd and the in-kernel `brcmfmac` driver plus linux-firmware's
  `brcmfmac43602-pcie.bin` / generic `brcmfmac43602-pcie.txt`. Developed on an Ubuntu 24.04 (noble)-based
  distribution with kernel 7.0.
* **Tools:** `bash`, `python3` (stdlib only), `sudo`, `iproute2` (`ip`), `iw`, `journalctl`, `jq` (failsafe),
  `coreutils`, `wpa_supplicant`/`wpa_cli` (only for `nvram-lab.py --join`).
* **A second way online** while you experiment: phone hotspot, USB tether or Bluetooth PAN. Every live step drops
  Wi-Fi for 6–15 s, and a rejected NVRAM leaves the Wi-Fi interface missing until it is rolled back.

## Configuration

All settings are optional. The scripts read them from the environment or from `/etc/default/brcm43602-fix`
(a shell file, sourced; values in the file win). Put them in the file if you want the boot failsafe and the
safety timers, which run without your environment, to use them too.

| Variable | Default | Meaning |
|---|---|---|
| `BRCM_NET_SERVICES` | *(empty)* | systemd units that manage Wi-Fi, e.g. `"wpa_supplicant.service NetworkManager.service"`. They are stopped (in reverse order) before the driver is unloaded and restarted (in order) afterwards. `rollback.sh` also restarts NetworkManager if it is active. |
| `BRCM_KNOWN_NETWORKS` | `/etc/brcm43602-fix/networks.json` | JSON `{"networks": [{"ssid": "...", "security": "psk", "psk": "..."}]}`. The failsafe uses the SSIDs ("a known network is in range but nothing joined" means roll back). `nvram-lab.py --join` uses the passphrase. Keep it `chmod 600`. Without it the known-network check is skipped. |
| `BRCM_IF` | `wlp2s0` | Wi-Fi interface name |
| `BRCM_FWDIR` | `/lib/firmware/brcm` | Firmware directory |
| `BRCM_STATE`, `BRCM_BACKUP`, `BRCM_LIBDIR`, `BRCM_UNITDIR` | `/var/lib/brcm43602-fix`, `/var/backups/brcm43602-fix`, `/usr/local/lib/brcm43602-fix`, `/etc/systemd/system` | Where `install.sh` keeps state, backups, the installed failsafe/rollback copies and the unit |
| `BRCM_TIMER_SECS` | `600` | `install.sh --try-now` safety timer |
| `BRCM_CONF` | `/etc/default/brcm43602-fix` | The config file itself |

Example `/etc/default/brcm43602-fix`:

```sh
BRCM_NET_SERVICES="NetworkManager.service"
BRCM_KNOWN_NETWORKS=/etc/brcm43602-fix/networks.json
```

## Usage

### 1. Fetch Apple's Broadcom kexts (no root)

```bash
python3 -I tools/fetch-brcm-fw.py info            # no network: what would be fetched, where it goes
python3 -I tools/fetch-brcm-fw.py probe           # one HEAD request: the package is still what the tool expects
python3 -I tools/fetch-brcm-fw.py fetch --yes     # ~30 MB; extracts into work/kext/ with a MANIFEST.json (sha256)
python3 -I tools/fetch-brcm-fw.py locate          # exit 1 = the kext has no per-board NVRAM (the expected result)
```

`--max-mb N` caps the download (default 120). `fetch --yes --full` downloads the whole 1.69 GB Payload and
verifies its sha1. The cache in `work/cache/` is a sparse file: it shows 1.69 GB but uses only what was
fetched. An interrupted run resumes; a repeat run needs no network. Apple's terms apply to what you download.

### 2. Dump the card's SROM (root; Wi-Fi down ~15 s)

```bash
sudo dist/srom-dump-run.sh work/srom/$(date +%Y%m%d-%H%M%S)
```

The script unloads `brcmfmac`, checks that the device is `14e4:43ba` and that ChipCommon reports chip 43602
before it reads the SROM area, stops on any PCI error status, puts the BAR0 window back, and reloads the driver.
A separate systemd timer reloads the driver after 4 minutes if the interface is still missing. The dump holds
your card's MAC address and calibration: **don't publish it.**

### 3. Decode it

```bash
K=work/kext/System/Library/Extensions/IO80211Family.kext/Contents/PlugIns/AirPortBrcm4360.kext/Contents/MacOS/AirPortBrcm4360
python3 -I tools/decode-srom.py --kext "$K" --dump work/srom/<stamp>/cc-0800.bin                       # NVRAM from the SROM alone
python3 -I tools/decode-srom.py --kext "$K" --dump work/srom/<stamp>/cc-0800.bin \
    --base /lib/firmware/brcm/brcmfmac43602-pcie.txt --out work/out/candidate.txt                      # overlay on the generic file
```

Overlay options for bisecting: `--only REGEX`, `--only-new`, `--keep a,b`, `--drop REGEX`, `--skip REGEX`,
`--sromrev N`, `--diff FILE`. The table offset (`TABLE_START`) is specific to `AirPortBrcm4360` 1150.12.1a1,
which is the version `fetch-brcm-fw.py` downloads.

### 4. Test candidates on the card (root)

```bash
sudo tools/nvram-lab.py --ssid MYNET --out work/lab/$(date +%Y%m%d-%H%M%S) [--join 3] A=work/out/a.txt B=work/out/b.txt
```

A baseline with the current files runs first. For each variant the lab installs the file under the
board-specific name, reloads the driver, runs plain `iw` scans and records the MAC, the strongest signal from
`--ssid` per band and the `brcmf` kernel lines. With `--join N` it also makes N throwaway `wpa_supplicant`
joins per band (needs the passphrase in `BRCM_KNOWN_NETWORKS`). Results go to `results.json` in `--out`. A
15-minute guard timer and the boot failsafe (armed with a `lab` flag) make sure no candidate stays installed,
even after a reboot in the middle of a run.

### 5. Install a file for real use

```bash
./install.sh --try-now "work/out/brcmfmac43602-pcie.Apple Inc.-MacBookPro14,2.txt"   # live; reverts itself in 10 min
./install.sh --keep                                                                   # cancel the timer = keep
```

* `install.sh` only accepts the names `brcmfmac43602-pcie.Apple Inc.-MacBookPro14,2.{txt,clm_blob,txcap_blob}`
  (the board-specific names `brcmfmac` tries first), non-empty files up to 1 MiB, and `.txt` files that look
  like NVRAM (`sromrev=` and `boardtype=` lines, no NUL bytes).
* Plain `./install.sh FILE…` changes nothing live: the files are read at the next driver load (reboot).
* The original files are backed up to `/var/backups/brcm43602-fix/<stamp>/`. The install record is
  `/var/lib/brcm43602-fix/installed.tsv`. A second install keeps the first, real original.
* The boot failsafe (`/var/lib/brcm43602-fix/trial` + `brcm43602-fix-failsafe.service`) checks the first boot
  after an install for 3 minutes. It reverts the files if the Wi-Fi interface is missing, the driver logged a
  firmware failure, or there is no default route while a known network is in range. A missing interface counts
  as a failure even when a USB tether provides a route. If nothing known is in range, the trial stays open.

## Uninstall / rollback

```bash
./rollback.sh               # restore/remove the files, disarm the failsafe, reload the driver
./rollback.sh --no-reload   # only fix the files; they take effect at the next boot
```

`rollback.sh` is safe to run more than once. To also remove the safety net itself:

```bash
sudo systemctl disable brcm43602-fix-failsafe.service
sudo rm -f /etc/systemd/system/brcm43602-fix-failsafe.service
sudo rm -rf /usr/local/lib/brcm43602-fix /var/lib/brcm43602-fix   # keep /var/backups/brcm43602-fix until you're sure
sudo systemctl daemon-reload
```

## Troubleshooting

* **Wi-Fi interface gone after an install or a lab run** (`brcmf_pcie_download_fw_nvram: FW failed to
  initialize`, `Dongle setup failed`): the firmware rejected the NVRAM. Run
  `sudo /usr/local/lib/brcm43602-fix/rollback.sh`. If that is not possible, delete
  `/lib/firmware/brcm/brcmfmac43602-pcie.Apple Inc.-MacBookPro14,2.*` by hand, then
  `sudo modprobe -r brcmfmac_wcc brcmfmac; sudo modprobe brcmfmac`. Use a phone hotspot or tether meanwhile.
* **`modprobe -r` fails:** something still holds the driver. Set `BRCM_NET_SERVICES` so the managing services
  are stopped first, or reboot (the failsafe and the files on disk decide what loads).
* **`fetch-brcm-fw.py probe` reports a different size or ETag:** Apple changed or removed the package. The tool
  refuses to continue rather than mix data from two objects.
* **`decode-srom.py` "cannot pick the SROM revision automatically":** pass `--rev 12` (this card) or check that
  the kext is `AirPortBrcm4360` 1150.12.1a1.
* **The failsafe never reverts at a new place:** by design. With no default route and no known network in range,
  the old files would not do better, so the trial stays open until a boot gets a route.
* **The `clm_blob` / `txcap_blob` "not found" kernel lines:** expected. Nothing here provides those files (see
  findings).

## Findings

### Apple's kext has no NVRAM, CLM blob or firmware

The plan assumed Apple's Broadcom kext carries per-board NVRAM and a CLM blob. The kext for this card
(`AirPortBrcm4360` 1150.12.1a1, macOS 10.12.6 combo update, `IONameMatch` `pci14e4,43ba`, names the board ID
`Mac-CAD6701F7CEA0921`) carries neither:

| Looked for | Found in the kext |
|---|---|
| per-board NVRAM text/blob | none; only `printf` formats (`sromrev=%d`, `boardtype=0x%x`, …) that print an NVRAM from the card's SROM |
| firmware image (TRX) | none; the MAC runs on the host (`wlc_*` code is in the kext) |
| CLM / TX-cap blob | base CLM compiled into the driver (`CLM DATA`, `ClmImport: 1.39.0`) and `clmload`/`txcapload` iovar support; no ready-made `clm_blob`/`txcap_blob` file |

Reproduce with `tools/fetch-brcm-fw.py locate`. The community is in the same state: everyone uses the one
generic `brcmfmac43602-pcie.txt` (kernel bugzilla 193121).

### The calibration is in the card's SROM, and it can be read

`BAR0+0x1000` (the usual "SPROM shadow") does not hold it on this card. With `brcmfmac` unloaded and the BAR0 window
(config `0x80`) pointed at ChipCommon (`0x18000000`), a read of `ChipCommon+0x800..0xFFF` returns it.

The image is **SROM revision 12**, ~776 words. Apple's kext holds Broadcom's variable table for it (24-byte
entries `{name, revmask, flags, offset, mask}`; a continuation entry points at an empty string), and
`tools/decode-srom.py` applies it. As a check, 164 of the 250 variables in the generic `brcmfmac43602-pcie.txt` decode
bit-identical, and the differences are exactly the board-specific ones. That confirms the table, the
continuation/array rules and the word order.

What the card's SROM says that the generic file (another Mac's dump) does not:

| variable | generic file | this card's SROM |
|---|---|---|
| `macaddr` | `00:90:4c:0d:f4:3e` (placeholder) | the unit's own address (Apple OUI) |
| `boardtype` / `boardrev` / `subvid` / `boardnum` | `0x61b` / `0x1421` / `0x14e4` / 62526 | `0x157` / `0x1108` / `0x106b` / unit-specific |
| `boardflags2` / `boardflags3` / `boardflags4` | `0x2` / `0x303` / absent | `0x800002` / `0xa000005` / `0x18` |
| `rssi_delta_2gb0`, `rssi_delta_5g*`, `gain_cal_temp` | other unit's | this unit's |
| rev-12 only: `pa5gbw*`, `tssifloor*`, `pdoffset*`, `pa2gccka0`, `txidxcap*` | absent | present |
| txcal block header (`length_txcal`, `caltype_txcal`, ...) | `65535` (unprogrammed) | programmed |

The SROM does not hold `swctrlmap_2g/5g`, `swctrlmapext_*` (the antenna-switch maps the generic file carries) or the
per-channel txcal arrays, so those stay as in the generic file.

### What the card did with about 45 candidate NVRAMs

Does the firmware boot? Each row is one or more driver reloads, with the same router and the same spot.

| candidate (overlay on the generic file) | boots |
|---|---|
| MAC only; board identity only; `sromrev=12` only; MAC + identity + RSSI deltas + `gain_cal_temp` | yes |
| `boardflags2` alone; `boardflags4` alone; `tssifloor*`+`txidxcap*`; the misc rev-12 flags | yes |
| each PA variable alone (`pa5gbw4080a1`, `pa5gbw4080a0`, `pa5gbw40a0`, `pa5gbw80a0`, `pa2gccka0`); each `pdoffset*` family alone | yes |
| `boardflags3`: `0x307`, `0x2000303`, `0x5` | yes |
| `boardflags3` with bit 27 (`0x8000303`, `0xa000303`, Apple's `0xa000005`) | **no** |
| the PA variables together; the `pdoffset*` family together; "everything that boots", combined | **no** (not isolated; the NVRAM may be near a size or content limit) |
| full SROM overlay, sromrev 11 or 12 | **no** (`FW failed to initialize`) |
| generic file without the donor unit's txcal data | **no** (the firmware needs those variables) |
| full SROM overlay minus donor txcal data, `boardflags3=0x2000005`, sromrev 11 or 12 | yes |

Do joins work? The test router's 2.4 GHz BSS was about -70 dBm and its 5 GHz BSS about -55 dBm. Each join went to the
strongest BSS per band.

| NVRAM | 2.4 GHz joins | 5 GHz joins | note |
|---|---|---|---|
| generic (baseline, 3 runs) | 0 of 2–3, each: 5 association rejects | all | 5 GHz TX 117–526 vs RX 390–585 Mbit/s |
| MAC + identity + RSSI deltas; `boardflags2`; `pa5gbw80a0` (three runs) | 0 of 2–3 | all | 2.4 GHz scan signal -70..-81 dBm, same as baseline |
| full Apple calibration (the last "yes" row above) | 0 of 2 | **0 of 2** | 2.4 GHz scan signal ~-55 dBm (~18 dB better than baseline) but nothing associates on either band: TX is broken |

**Reading:** the 2.4 GHz join failure survives every NVRAM the firmware accepts. The one NVRAM that changes the
2.4 GHz receive level breaks transmit. That points at the firmware (a 2015 Broadcom build that predates this SROM
revision and its board flags) rather than at a missing or wrong value that could still be supplied.

### Possible next steps

1. **broadcom-sta (`wl`) 6.30.223.271 as DKMS.** This is the host-MAC driver that Apple's kext is a sibling of.
   It reads the SROM itself and has the CLM built in. Ubuntu ships it as `broadcom-sta-dkms`. It needs porting
   to current kernels (cfg80211 API drift) and a clean switch against `brcmfmac` (never both loaded). It taints
   the kernel and is closed-source.
2. **A newer brcmfmac firmware for the 43602:** none is public beyond 7.35.177.61 (linux-firmware).
3. **Stay on 5 GHz:** it works with the generic file.

## Status and limitations

* **Does not fix the problem.** The tools are investigative, and no NVRAM found so far makes 2.4 GHz work.
* Specific to the MacBookPro14,2 / BCM43602 and to `AirPortBrcm4360` 1150.12.1a1 (`TABLE_START` in
  `decode-srom.py`). Other Macs with a BCM43602 might work with changes but are untested.
* `fetch-brcm-fw.py` depends on one Apple CDN URL with a pinned size and ETag. If Apple removes the package,
  it stops working.
* `dump-srom.py` writes to PCI config space (the BAR0 window register, and the PM state / memory-enable bits if
  needed). It reads only from the device, but use it at your own risk.
* The failsafe's "known network in range" check needs `BRCM_KNOWN_NETWORKS`. Without that file, only a missing
  interface or a firmware error triggers a revert when there is no route.
* Done criteria that were **not met:** ≥ 20/20 joins on 2.4 GHz, TX bitrate near RX, and the missing-blob kernel
  lines gone.

## Running the tests

```bash
python3 -I tests/test_fetch.py
python3 -I tests/test_srom.py
python3 -I tests/test_lab.py
bash tests/test_scripts.sh
```

All tests are offline and need neither root nor the hardware.

## Credits

* Broadcom's SROM variable layout and `srom_parsecis` output format, as found in Apple's `AirPortBrcm4360`
  kext. The table is read at runtime from your own download, and none of it is included here.
* linux-firmware's `brcmfmac43602-pcie.bin` and generic `brcmfmac43602-pcie.txt`, the Linux `brcmfmac`
  driver, and the community discussion in kernel bugzilla 193121.

## Legal notes and disclaimer

**Not affiliated.** This project is not affiliated with, authorized by, or endorsed by Apple Inc. or
Broadcom Inc. "Apple", "Mac", "MacBook Pro" and "macOS" are trademarks of Apple Inc. "Broadcom" is a
trademark of Broadcom Inc. These names are used only to identify the hardware and software this tool
works with.

**No Apple or Broadcom files are included.** This repository contains no Apple or Broadcom code,
firmware, kexts, or data. `tools/fetch-brcm-fw.py` downloads a macOS update package directly from
Apple's servers to your own computer. You are responsible for having the right to use that software
(for example, the macOS license that came with your Mac) and for complying with Apple's license terms,
which generally limit macOS software to Apple-branded hardware. Don't redistribute anything the tool
downloads or extracts (`work/`).

**Interoperability only.** The tools read hardware calibration data from your own card and use
Apple's driver only to understand that data's layout, so Linux can work with hardware you own. They
don't remove, disable or bypass any copy protection, encryption or license check.

**Radio regulations.** Changing a Wi-Fi card's NVRAM or calibration can change its transmit power and
behavior. You are responsible for making sure your device stays within the radio regulations of your
country (for example, FCC Part 15 in the US). Don't change the country code (`ccode`/`regrev`).

**Hardware risk. Use at your own risk.** These scripts run as root, write to PCI configuration
space, unload and reload kernel drivers, and replace files in `/lib/firmware`. That can make your
Wi-Fi stop working, hang or crash your system, cause data loss, or in the worst case damage
hardware. Keep a second way online and a backup. Read each script before you run it.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, INCLUDING
DAMAGE TO HARDWARE OR LOSS OF DATA, ARISING FROM THE USE OF THE SOFTWARE. See [LICENSE](LICENSE) (MIT).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Results from other MacBookPro14,2 units (with the MAC and `boardnum`
removed) are especially welcome.
