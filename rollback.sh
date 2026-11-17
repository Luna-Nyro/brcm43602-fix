#!/bin/bash
# Put the original BCM43602 firmware set back: remove (or restore from backup) every file install.sh put
# into /lib/firmware/brcm, disarm the first-boot failsafe, and reload the driver so Wi-Fi comes back
# with the old files. Self-contained: the failsafe and the 10-minute safety timer run the copy in
# /usr/local/lib/brcm43602-fix.
#   ./rollback.sh [--boot] [--no-reload]
#     --no-reload  only fix the files; the driver keeps what it has loaded until the next boot/reload
#     --boot       called by the failsafe or the timer (no user session, no interactive hints)
set -u
BOOT=0; RELOAD=1
for a in "$@"; do case "$a" in --boot) BOOT=1;; --no-reload) RELOAD=0;; *) echo "usage: $0 [--boot] [--no-reload]" >&2; exit 2;; esac; done
[ "$(id -u)" = 0 ] || [ -n "${BRCM_NO_SYSTEM:-}" ] || exec sudo -E "$0" "$@"

CONF=${BRCM_CONF:-/etc/default/brcm43602-fix}
# shellcheck disable=SC1090
[ ! -r "$CONF" ] || . "$CONF"
NET_SERVICES=${BRCM_NET_SERVICES:-}

FWDIR=${BRCM_FWDIR:-/lib/firmware/brcm}
STATE=${BRCM_STATE:-/var/lib/brcm43602-fix}
IF=${BRCM_IF:-wlp2s0}
sys() { if [ -n "${BRCM_NO_SYSTEM:-}" ]; then [ -z "${BRCM_SYS_LOG:-}" ] || echo "$*" >> "$BRCM_SYS_LOG"; else "$@"; fi; }
say() { echo "rollback: $*"; logger -t brcm-fw-rollback "$*" 2>/dev/null; }

sys systemctl stop brcm43602-fix-restore.timer brcm43602-fix-restore.service 2>/dev/null   # a pending safety timer is moot now
if [ -f "$STATE/installed.tsv" ]; then
  while IFS=$'\t' read -r target backup; do
    [ -n "$target" ] || continue
    if [ "$backup" != NONE ] && [ -f "$backup" ]; then
      cp -p "$backup" "$target" && say "restored $target"
    else
      rm -f "$target" && say "removed $target"
    fi
  done < "$STATE/installed.tsv"
  mv "$STATE/installed.tsv" "$STATE/installed.tsv.reverted-$(date +%Y%m%d-%H%M%S)"
else
  say "no record of installed files; nothing to put back"
fi
rm -f "$STATE/trial" "$STATE/lab"

reload_driver() {
  for u in $(printf '%s\n' $NET_SERVICES | tac); do sys systemctl stop "$u" 2>/dev/null; done
  sys modprobe -r brcmfmac_wcc brcmfmac || say "modprobe -r failed (something still holds the driver?)"
  sleep 2
  sys modprobe brcmfmac
  for _ in $(seq 40); do ip link show "$IF" >/dev/null 2>&1 && break; sleep 1; done
  sleep 2
  for u in $NET_SERVICES; do sys systemctl restart "$u" 2>/dev/null; sleep 1; done
  if systemctl is-active --quiet NetworkManager.service 2>/dev/null; then sys systemctl restart NetworkManager.service; fi
  ip link show "$IF" >/dev/null 2>&1
}

if [ "$RELOAD" = 1 ]; then
  if reload_driver; then say "driver reloaded with the original firmware files; $IF is back"; else say "$IF did not come back after the reload; reboot (the files are already reverted)"; exit 1; fi
fi
[ "$BOOT" = 1 ] || echo "Reverted. Phone hotspot / USB tether remain the fallback if Wi-Fi misbehaves."
