#!/usr/bin/python3 -I
"""Try candidate NVRAM files on the real card, one driver reload each, and measure what the card hears.

  sudo tools/nvram-lab.py --ssid MYNET --out DIR [--join N] NAME=FILE ...   (a baseline with the current files runs first)

For every variant: install FILE under the board-specific name brcmfmac asks for, unload/load brcmfmac,
run a few plain `iw scan`s (no supplicant) and record the MAC the firmware came up with, the strongest
signal seen from --ssid on 2.4 GHz and on 5 GHz, and the brcmfmac kernel lines. The router's own 5 GHz
signal is the yardstick: a healthy 2.4 GHz receiver hears the same router within ~10 dB of it, this card was
~25 dB deaf on 2.4 GHz.

--join needs the network's passphrase in the known-networks file (BRCM_KNOWN_NETWORKS, default
/etc/brcm43602-fix/networks.json): {"networks": [{"ssid": "MYNET", "security": "psk", "psk": "..."}]}.

Safety: the Wi-Fi services (BRCM_NET_SERVICES) are stopped and Wi-Fi is down for the whole run (~30 s per
variant). An independent systemd timer (armed first) deletes the lab file and reloads the driver after 15 minutes
whatever happens to this script or the session; the script itself restores in a `finally`. Nothing else is changed.
Settings come from /etc/default/brcm43602-fix (BRCM_CONF) first, then from the environment.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def conf_file(path):
    """KEY=VALUE lines of the shell config file the scripts source (only plain assignments are understood)."""
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip("'\"")
    except OSError:
        pass
    return out


CONF = conf_file(os.environ.get("BRCM_CONF", "/etc/default/brcm43602-fix"))


def setting(key, default):
    """Same precedence as the shell scripts: the config file, then the environment, then the default."""
    return CONF.get(key) or os.environ.get(key) or default


FW = Path(setting("BRCM_FWDIR", "/lib/firmware/brcm"))
TARGET = FW / "brcmfmac43602-pcie.Apple Inc.-MacBookPro14,2.txt"
IF = setting("BRCM_IF", "wlp2s0")
NET_SERVICES = setting("BRCM_NET_SERVICES", "").split()
GUARD = "brcm-lab-guard"


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)


def parse_scan(text):
    """[{bssid, freq, signal, ssid}] from `iw dev X scan` output."""
    out, cur = [], None
    for line in text.splitlines():
        m = re.match(r"BSS ([0-9a-f:]{17})", line)
        if m:
            cur = {"bssid": m.group(1), "freq": None, "signal": None, "ssid": ""}
            out.append(cur)
        elif cur is not None:
            s = line.strip()
            if s.startswith("freq:"):
                cur["freq"] = int(float(s.split()[1]))
            elif s.startswith("signal:"):
                cur["signal"] = float(s.split()[1])
            elif s.startswith("SSID:"):
                cur["ssid"] = s[5:].strip()
    return out


def summarize(scans, ssid):
    best = {"g24": None, "g5": None}
    count = {"g24": 0, "g5": 0}
    seen = {"g24": set(), "g5": set()}
    for scan in scans:
        for b in scan:
            band = "g24" if b["freq"] and b["freq"] < 3000 else "g5"
            seen[band].add(b["bssid"])
            if b["ssid"] == ssid and b["signal"] is not None and (best[band] is None or b["signal"] > best[band]):
                best[band] = b["signal"]
    count = {k: len(v) for k, v in seen.items()}
    return {"best_signal_dbm": best, "bss_seen": count}


def reload_driver(stop_services=True):
    if stop_services:
        for unit in reversed(NET_SERVICES):
            sh("systemctl", "stop", unit)
    sh("modprobe", "-r", "brcmfmac_wcc", "brcmfmac")
    time.sleep(2)
    t0 = time.time()
    sh("modprobe", "brcmfmac")
    for i in range(40):
        if sh("ip", "link", "show", IF).returncode == 0:
            time.sleep(2)
            sh("ip", "link", "set", IF, "up")
            time.sleep(1)
            return True
        if i % 2 and any(m in l for l in kernel_lines(t0) for m in ("FW failed to initialize", "Dongle setup failed")):
            return False                          # the firmware refused its NVRAM: no point waiting
        time.sleep(1)
    return False


def kernel_lines(since):
    r = sh("journalctl", "-k", "--no-pager", "-o", "cat", f"--since=@{int(since)}")
    return [l for l in r.stdout.splitlines() if "brcmf" in l and "arp ip table" not in l]


def measure(ssid, n=3):
    scans = []
    for _ in range(n):
        for attempt in range(5):
            r = sh("iw", "dev", IF, "scan")
            if r.returncode == 0:
                scans.append(parse_scan(r.stdout))
                break
            time.sleep(2)
    res = summarize(scans, ssid)
    res["scans_ok"] = len(scans)
    res["mac"] = Path(f"/sys/class/net/{IF}/address").read_text().strip() if Path(f"/sys/class/net/{IF}/address").exists() else None
    return res


STORE = setting("BRCM_KNOWN_NETWORKS", "/etc/brcm43602-fix/networks.json")
CTRL = "/run/brcm-lab-wpa"


def psk_hex(ssid, passphrase):
    """The 256-bit WPA2 PSK as hex (a 64-hex-digit value is already one)."""
    if re.fullmatch(r"[0-9a-fA-F]{64}", passphrase):
        return passphrase.lower()
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode(), ssid.encode(), 4096, 32).hex()


def supplicant_conf(ssid, passphrase, bssid, freq):
    return (f"ctrl_interface=DIR={CTRL} GROUP=netdev\nupdate_config=0\nap_scan=1\np2p_disabled=1\n"
            f"network={{\n ssid={ssid.encode().hex()}\n psk={psk_hex(ssid, passphrase)}\n bssid={bssid}\n"
            f" scan_freq={freq}\n key_mgmt=WPA-PSK\n}}\n")


def known_passphrase(ssid):
    try:
        for n in json.load(open(STORE))["networks"]:
            if n.get("ssid") == ssid and n.get("psk") and n.get("security") == "psk":
                return n["psk"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def best_bss(scans, ssid, band):
    best = None
    for scan in scans:
        for b in scan:
            if b["ssid"] == ssid and b["signal"] is not None and ((b["freq"] or 0) < 3000) == (band == "g24"):
                if best is None or b["signal"] > best["signal"]:
                    best = b
    return best


def try_join(bssid, freq, ssid, passphrase, timeout=18):
    os.makedirs(CTRL, exist_ok=True)
    conf, pid, logf = "/run/brcm-lab-wpa.conf", "/run/brcm-lab-wpa.pid", "/run/brcm-lab-wpa.log"
    fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, supplicant_conf(ssid, passphrase, bssid, freq).encode())
    os.close(fd)
    sh("wpa_supplicant", "-B", "-i", IF, "-c", conf, "-D", "nl80211", "-P", pid, "-f", logf)
    t0, state = time.time(), ""
    while time.time() - t0 < timeout:
        state = sh("wpa_cli", "-p", CTRL, "-i", IF, "status").stdout
        if "wpa_state=COMPLETED" in state:
            break
        time.sleep(1)
    ok = "wpa_state=COMPLETED" in state
    res = {"ok": ok, "secs": round(time.time() - t0, 1)}
    try:
        res["rejects"] = open(logf).read().count("ASSOC-REJECT")
    except OSError:
        pass
    if ok:
        time.sleep(2)
        st = sh("iw", "dev", IF, "station", "dump").stdout
        for key in ("signal", "tx bitrate", "rx bitrate", "tx failed", "tx retries"):
            m = re.search(rf"^\s*{key}:\s*(.+)$", st, re.M)
            if m:
                res[key.replace(" ", "_")] = m.group(1).split("\t")[0].strip()
    if Path(pid).exists():
        sh("kill", Path(pid).read_text().strip())
    time.sleep(1.5)
    for f in (conf, pid, logf):
        Path(f).unlink(missing_ok=True)
    return res


def join_tests(scans_src, ssid, n):
    """n joins to the strongest 2.4 GHz and n to the strongest 5 GHz BSS of `ssid`, one throwaway supplicant each."""
    pw = known_passphrase(ssid)
    if not pw:
        return {"error": f"no stored password for {ssid}"}
    scans = []
    for _ in range(2):
        r = sh("iw", "dev", IF, "scan")
        if r.returncode == 0:
            scans.append(parse_scan(r.stdout))
    out = {}
    for band in ("g24", "g5"):
        b = best_bss(scans, ssid, band)
        if not b:
            out[band] = {"error": "no BSS in range"}
            continue
        out[band] = {"bssid": b["bssid"], "freq": b["freq"], "scan_signal": b["signal"],
                     "joins": [try_join(b["bssid"], b["freq"], ssid, pw) for _ in range(n)]}
        out[band]["ok"] = sum(1 for j in out[band]["joins"] if j["ok"])
    return out


def restore():
    """rollback.sh removes the lab file (recorded by install.sh --arm), disarms the failsafe, reloads the driver,
    restarts the Wi-Fi services."""
    r = sh(str(HERE / "rollback.sh"), "--boot")
    ok = sh("ip", "link", "show", IF).returncode == 0
    log(f"restored via rollback.sh (interface back: {ok}) {r.stdout.strip()[-120:]}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssid", required=True, help="the network whose signal (and, with --join, joins) is measured")
    ap.add_argument("--out", required=True)
    ap.add_argument("--join", type=int, default=0, help="also try this many joins per band to --ssid for every variant")
    ap.add_argument("variants", nargs="+", help="NAME=FILE")
    a = ap.parse_args()
    if os.geteuid():
        sys.exit("run as root")
    if TARGET.exists():
        sys.exit(f"{TARGET} already exists: refusing to overwrite it (is something installed? run rollback.sh first)")
    variants = []
    for v in a.variants:
        name, _, f = v.partition("=")
        if not Path(f).is_file():
            sys.exit(f"no such file: {f}")
        variants.append((name, Path(f)))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    r = sh("systemd-run", f"--unit={GUARD}", "--collect", "--on-active=900", "/bin/sh", "-c",
           f'rm -f "{TARGET}"; exec "{HERE}/rollback.sh" --boot')
    if r.returncode:
        sys.exit(f"could not arm the guard timer ({r.stderr.strip()}); not touching the driver")
    r = sh(str(HERE / "install.sh"), "--arm")
    if r.returncode:
        sh("systemctl", "stop", f"{GUARD}.timer")
        sys.exit(f"could not arm the boot failsafe ({r.stderr.strip() or r.stdout.strip()}); not touching the driver")
    log("guard timer armed (15 min) and boot failsafe armed (survives a reboot)")
    results = {}
    try:
        for name, f in [("baseline", None)] + variants:
            t0 = time.time()
            if f is None:
                TARGET.unlink(missing_ok=True)
            else:
                shutil.copyfile(f, TARGET)
                os.chmod(TARGET, 0o644)
            log(f"--- {name}")
            up = reload_driver()
            res = {"interface_up": up}
            if up:
                res.update(measure(a.ssid))
                if a.join:
                    res["joins"] = join_tests(None, a.ssid, a.join)
            res["kernel"] = kernel_lines(t0)
            results[name] = res
            log(f"{name}: {json.dumps({k: v for k, v in res.items() if k != 'kernel'})}")
            (out / "results.json").write_text(json.dumps(results, indent=1))
    finally:
        results["_restored"] = restore()
        (out / "results.json").write_text(json.dumps(results, indent=1))
        sh("systemctl", "stop", f"{GUARD}.timer")
    os.system(f"chown -R {os.environ.get('SUDO_UID', 0)}:{os.environ.get('SUDO_GID', 0)} '{out}'")


if __name__ == "__main__":
    main()
