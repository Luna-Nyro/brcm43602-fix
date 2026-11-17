#!/bin/bash
# Run tools/dump-srom.py with brcmfmac unloaded and put Wi-Fi back afterwards. Wi-Fi is down for ~15 s.
#   sudo dist/srom-dump-run.sh OUTDIR
# An independent systemd timer (armed first, outlives this script and the calling session) reloads the
# driver and restarts the Wi-Fi services (BRCM_NET_SERVICES) if the interface is not back after 4 minutes.
# Persistent state is never changed, so a reboot also restores everything.
set -u
OUT=${1:?usage: srom-dump-run.sh OUTDIR}
HERE=$(cd "$(dirname "$0")/.." && pwd)
[ "$(id -u)" = 0 ] || exec sudo -E "$0" "$@"
CONF=${BRCM_CONF:-/etc/default/brcm43602-fix}
# shellcheck disable=SC1090
[ ! -r "$CONF" ] || . "$CONF"
IF=${BRCM_IF:-wlp2s0}
NET_SERVICES=${BRCM_NET_SERVICES:-}
say() { echo "srom-dump: $*"; logger -t brcm-srom-dump "$*" 2>/dev/null; }

restart_services=""
for u in $NET_SERVICES; do restart_services="$restart_services systemctl restart '$u'; sleep 1;"; done
systemd-run --unit=brcm-srom-guard --collect --on-active=240 /bin/sh -c \
  "ip link show '$IF' >/dev/null 2>&1 && exit 0; modprobe brcmfmac; sleep 8; $restart_services" >/dev/null || { say "could not arm the guard timer; not touching the driver"; exit 1; }

reload() {
  modprobe brcmfmac
  for _ in $(seq 40); do ip link show "$IF" >/dev/null 2>&1 && break; sleep 1; done
  sleep 2
  for u in $NET_SERVICES; do systemctl restart "$u"; sleep 1; done
}

say "stopping the Wi-Fi services and unloading brcmfmac"
for u in $(printf '%s\n' $NET_SERVICES | tac); do systemctl stop "$u"; done
if modprobe -r brcmfmac_wcc brcmfmac; then
  sleep 1
  timeout 60 python3 -I "$HERE/tools/dump-srom.py" --out "$OUT"; rc=$?
  say "dump finished with status $rc"
else
  say "modprobe -r failed; not touching the hardware"; rc=1
fi
reload
ip link show "$IF" >/dev/null 2>&1 && say "$IF is back" || say "$IF is NOT back; the guard timer will retry, or reboot"
chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$OUT" 2>/dev/null
systemctl stop brcm-srom-guard.timer 2>/dev/null
exit "$rc"
