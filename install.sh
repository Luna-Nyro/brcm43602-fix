#!/bin/bash
# Install candidate BCM43602 firmware files for the MacBookPro14,2 under the exact names brcmfmac asks for,
# with a backup, a first-boot failsafe and (for --try-now) a 10-minute self-reverting timer.
#
#   ./install.sh [--try-now] FILE...        FILE names must be brcmfmac43602-pcie.Apple Inc.-MacBookPro14,2.{txt,clm_blob,txcap_blob}
#   ./install.sh --keep                     cancel the --try-now safety timer: keep the files you just tested
#   ./rollback.sh                           undo (also what the failsafe and the timer run)
#
# Without --try-now nothing live changes: the files are read at the next driver load (reboot), and the
# failsafe (/var/lib/brcm43602-fix/trial, brcm43602-fix-failsafe.service) reverts them if that boot has no
# network. With --try-now the driver is reloaded right away (Wi-Fi drops for ~10 s); keep a phone hotspot or
# USB tether ready. The timer reverts everything after 10 minutes unless you run --keep.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
TRY=0; KEEP=0; ARM=0; FILES=()
for a in "$@"; do case "$a" in --try-now) TRY=1;; --keep) KEEP=1;; --arm) ARM=1;; -*) echo "unknown option $a" >&2; exit 2;; *) FILES+=("$a");; esac; done
[ "$(id -u)" = 0 ] || [ -n "${BRCM_NO_SYSTEM:-}" ] || exec sudo -E "$0" "$@"

# Optional settings (see README "Configuration"); values in the file win over the environment.
CONF=${BRCM_CONF:-/etc/default/brcm43602-fix}
# shellcheck disable=SC1090
[ ! -r "$CONF" ] || . "$CONF"
NET_SERVICES=${BRCM_NET_SERVICES:-}   # units that manage Wi-Fi: stopped (reverse order) before the driver unload, restarted (in order) after

FWDIR=${BRCM_FWDIR:-/lib/firmware/brcm}
STATE=${BRCM_STATE:-/var/lib/brcm43602-fix}
BACKUP=${BRCM_BACKUP:-/var/backups/brcm43602-fix}
LIBDIR=${BRCM_LIBDIR:-/usr/local/lib/brcm43602-fix}
UNITDIR=${BRCM_UNITDIR:-/etc/systemd/system}
IF=${BRCM_IF:-wlp2s0}
TIMER_SECS=${BRCM_TIMER_SECS:-600}
NAME_RE='^brcmfmac43602-pcie\.Apple Inc\.-MacBookPro14,2\.(txt|clm_blob|txcap_blob)$'
sys() { if [ -n "${BRCM_NO_SYSTEM:-}" ]; then [ -z "${BRCM_SYS_LOG:-}" ] || echo "$*" >> "$BRCM_SYS_LOG"; else "$@"; fi; }
die() { echo "install: $*" >&2; exit 1; }

if [ "$KEEP" = 1 ]; then
  sys systemctl stop brcm43602-fix-restore.timer brcm43602-fix-restore.service 2>/dev/null || true
  echo "kept: the safety timer is cancelled. The first-boot failsafe stays armed until a boot gets a default route."
  exit 0
fi
[ "$ARM" = 1 ] || [ "${#FILES[@]}" -gt 0 ] || die "no files given"

for f in "${FILES[@]}"; do
  b=$(basename "$f")
  [[ "$b" =~ $NAME_RE ]] || die "'$b' is not one of the names brcmfmac requests for this machine"
  [ -f "$f" ] && [ -s "$f" ] || die "'$f' is missing or empty"
  [ "$(stat -c %s "$f")" -le 1048576 ] || die "'$f' is bigger than 1 MiB; not a firmware config/blob"
  if [[ "$b" == *.txt ]]; then
    grep -q '^sromrev=' "$f" && grep -q '^boardtype=' "$f" || die "'$b' lacks sromrev=/boardtype= lines; not an NVRAM file"
    ! grep -qP '\x00' "$f" || die "'$b' contains NUL bytes"
  fi
done

# failsafe first: no firmware change without it
install -d -m755 "$LIBDIR" "$STATE" "$UNITDIR"
install -m755 "$HERE/dist/brcm-fw-failsafe.sh" "$HERE/rollback.sh" "$LIBDIR/"
install -m644 "$HERE/dist/brcm43602-fix-failsafe.service" "$UNITDIR/"
sys systemctl daemon-reload
sys systemctl enable brcm43602-fix-failsafe.service

OWN=(); [ "$(id -u)" != 0 ] || OWN=(-o root -g root)
if [ "$ARM" = 1 ]; then
  # lab mode: install nothing, but record the board-specific .txt as "ours, was absent" and arm the boot failsafe,
  # so a reboot in the middle of an experiment still ends with the file removed and Wi-Fi back
  t="$FWDIR/brcmfmac43602-pcie.Apple Inc.-MacBookPro14,2.txt"
  if ! { [ -f "$STATE/installed.tsv" ] && awk -F'\t' -v t="$t" '$1 == t { f = 1 } END { exit !f }' "$STATE/installed.tsv"; }; then
    printf '%s\tNONE\n' "$t" >> "$STATE/installed.tsv"
  fi
  touch "$STATE/trial" "$STATE/lab"
  echo "armed: boot failsafe will remove '$t' and reload the driver if Wi-Fi is broken after a reboot"
  exit 0
fi
stamp=$(date +%Y%m%d-%H%M%S)
bdir="$BACKUP/$stamp"
install -d -m700 "$bdir"
# A file already recorded by an earlier install keeps its original backup (the first one is the real original).
prev() { [ -f "$STATE/installed.tsv" ] && awk -F'\t' -v t="$1" '$1 == t { print $2; exit }' "$STATE/installed.tsv"; }
: > "$STATE/installed.new"
for f in "${FILES[@]}"; do
  b=$(basename "$f"); t="$FWDIR/$b"
  bk=$(prev "$t" || true)
  if [ -z "$bk" ]; then
    if [ -e "$t" ]; then cp -p "$t" "$bdir/$b"; bk="$bdir/$b"; else bk=NONE; fi
  fi
  printf '%s\t%s\n' "$t" "$bk" >> "$STATE/installed.new"
  install -m644 "${OWN[@]}" "$f" "$t"
  echo "installed $t"
done
if [ -f "$STATE/installed.tsv" ]; then
  while IFS=$'\t' read -r t bk; do
    awk -F'\t' -v t="$t" '$1 == t { f = 1 } END { exit !f }' "$STATE/installed.new" || printf '%s\t%s\n' "$t" "$bk" >> "$STATE/installed.new"
  done < "$STATE/installed.tsv"
fi
mv "$STATE/installed.new" "$STATE/installed.tsv"
touch "$STATE/trial"
echo "failsafe armed ($STATE/trial); backup in $bdir"

if [ "$TRY" = 1 ]; then
  sys systemd-run --unit=brcm43602-fix-restore --on-active="$TIMER_SECS" "$LIBDIR/rollback.sh" --boot
  echo "safety timer: everything reverts in $((TIMER_SECS / 60)) min unless you run: $HERE/install.sh --keep"
  echo "reloading the driver now (Wi-Fi drops for ~10 s)..."
  for u in $(printf '%s\n' $NET_SERVICES | tac); do sys systemctl stop "$u" 2>/dev/null || true; done
  sys modprobe -r brcmfmac_wcc brcmfmac || echo "modprobe -r failed; reboot to use the new files"
  sleep 2
  sys modprobe brcmfmac
  for _ in $(seq 40); do ip link show "$IF" >/dev/null 2>&1 && break; sleep 1; done
  sleep 2
  for u in $NET_SERVICES; do sys systemctl restart "$u" 2>/dev/null || true; sleep 1; done
  ip link show "$IF" >/dev/null 2>&1 || echo "WARNING: $IF is missing; the timer (or: $HERE/rollback.sh) will revert"
else
  echo "nothing live changed. Reboot to try, or: $HERE/install.sh --try-now (same files)"
fi
