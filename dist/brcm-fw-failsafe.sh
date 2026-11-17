#!/bin/bash
# First boot after install.sh put Apple-board firmware files for the BCM43602 into /lib/firmware/brcm.
# If after 3 minutes the Wi-Fi interface is missing, the firmware failed to boot, or there is no default route
# AND the new files could be the cause, put the
# original firmware back (rollback.sh --boot) so the laptop is never stranded. The flag is removed as soon as
# any default route exists.
#
# "Could be the cause" = the Wi-Fi interface is missing, the driver logged a firmware boot failure, or a
# network we know the password of is in range (scan cache) and still nothing joined. Otherwise (nothing to
# join here, e.g. a new place) the trial just carries on: the old firmware would not do better.
# "Known networks" come from BRCM_KNOWN_NETWORKS, a JSON file {"networks": [{"ssid": ...}, ...]}; without it that
# check is skipped.
CONF=${BRCM_CONF:-/etc/default/brcm43602-fix}
# shellcheck disable=SC1090
[ ! -r "$CONF" ] || . "$CONF"
FLAG=${BRCM_FLAG:-/var/lib/brcm43602-fix/trial}
LABFLAG=${BRCM_LAB_FLAG:-/var/lib/brcm43602-fix/lab}   # set by install.sh --arm: an experiment was running, nothing of it may stay installed
IF=${BRCM_IF:-wlp2s0}
TRIES=${BRCM_FAILSAFE_TRIES:-18}
STEP=${BRCM_FAILSAFE_STEP:-10}
KNOWN=${BRCM_KNOWN_NETWORKS:-/etc/brcm43602-fix/networks.json}
ROLLBACK=${BRCM_ROLLBACK:-/usr/local/lib/brcm43602-fix/rollback.sh}
FWERR=${BRCM_FWERR:-'brcmf_(pcie_probe|pcie_download|fw_|bus_started|c_preinit).*(fail|error|timed? ?out)|firmware.*(boot|download).*fail'}

iface_present() { [ -e "${BRCM_NET_DIR:-/sys/class/net}/$IF" ]; }
fw_error() { journalctl -k -b --no-pager 2>/dev/null | grep -Eiq "$FWERR"; }
known_ssids() { jq -r '.networks[]?.ssid // empty' "$KNOWN" 2>/dev/null; }
visible_ssids() { iw dev "$IF" scan dump 2>/dev/null | sed -n 's/^[[:space:]]*SSID: //p'; }
known_in_range() {
  local v k
  v=$(visible_ssids) || return 1
  [ -n "$v" ] || return 1
  k=$(known_ssids)
  [ -n "$k" ] && printf '%s\n' "$k" | grep -qxFf - <(printf '%s\n' "$v")
}
decide() {   # prints: pass | restore | keep
  # The trial is about the Wi-Fi firmware, so a missing interface or a firmware boot failure is a failure even when
  # another link (USB tether, Bluetooth phone) provides the default route. Learned the hard way: a rejected NVRAM
  # left wlp2s0 absent while a tether kept "the network up".
  if ! iface_present; then echo restore
  elif fw_error; then echo restore
  elif ip -4 route show default | grep -q .; then echo pass
  elif known_in_range; then echo restore
  else echo keep
  fi
}

[ -n "${BRCM_FAILSAFE_SOURCE_ONLY:-}" ] && return 0 2>/dev/null
[ -f "$FLAG" ] || exit 0
if [ -f "$LABFLAG" ]; then
  # A lab run (tools/nvram-lab.py) was cut short by a reboot. Its candidate files are experiments, not an install:
  # a candidate that happens to boot must not "pass" and stay for good (it did once, for a day).
  logger -t brcm-fw-failsafe "a lab run was interrupted by this boot; removing its candidate files"
  [ -n "${BRCM_FAILSAFE_DRY:-}" ] && { echo restore-lab; exit 0; }
  exec "$ROLLBACK" --boot
fi
for _ in $(seq "$TRIES"); do
  [ "$(decide)" = pass ] && break
  sleep "$STEP"
done
case "$(decide)" in
  pass)
    rm -f "$FLAG"
    logger -t brcm-fw-failsafe "default route exists ($(ip -4 route show default | awk '{print $5; exit}')); trial passed"
    ;;
  keep)
    logger -t brcm-fw-failsafe "no network yet, but $IF is up with nothing known in range; the old firmware would not do better, so the trial continues"
    ;;
  restore)
    logger -t brcm-fw-failsafe "no network 3 minutes after the first boot with the new Wi-Fi firmware files and they may be at fault; reverting"
    [ -n "${BRCM_FAILSAFE_DRY:-}" ] && { echo restore; exit 0; }
    exec "$ROLLBACK" --boot
    ;;
esac
