#!/bin/bash
# Tests for install.sh / rollback.sh / dist/brcm-fw-failsafe.sh against fake directories and stub
# commands. Needs no root and touches nothing outside a temp dir.
#   bash tests/test_scripts.sh
set -u
HERE=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok()   { pass=$((pass + 1)); echo "ok   $*"; }
bad()  { fail=$((fail + 1)); echo "FAIL $*"; }
check() { local what=$1; shift; if "$@"; then ok "$what"; else bad "$what"; fi; }

export BRCM_NO_SYSTEM=1 BRCM_SYS_LOG=$T/sys.log BRCM_CONF=/dev/null BRCM_NET_SERVICES="net-a.service net-b.service"
export BRCM_FWDIR=$T/fw BRCM_STATE=$T/state BRCM_BACKUP=$T/backup BRCM_LIBDIR=$T/lib BRCM_UNITDIR=$T/units
mkdir -p "$BRCM_FWDIR" "$T/bin"
for c in logger; do printf '#!/bin/sh\nexit 0\n' > "$T/bin/$c"; done
printf '#!/bin/sh\nexit 0\n' > "$T/bin/ip"      # `ip link show wlp2s0` succeeds
printf '#!/bin/sh\nexit 3\n' > "$T/bin/systemctl"  # is-active => inactive
chmod +x "$T"/bin/*
export PATH=$T/bin:$PATH

N="brcmfmac43602-pcie.Apple Inc.-MacBookPro14,2"
mkdir -p "$T/src"
printf 'sromrev=11\nboardtype=0x61b\nmacaddr=11:22:33:44:55:66\n' > "$T/src/$N.txt"
head -c 300 /dev/urandom > "$T/src/$N.clm_blob"

# ---- install: validation
bash "$HERE/install.sh" "$T/src/wrong-name.txt" >/dev/null 2>&1; check "refuses a wrong file name" test $? -ne 0
mkdir -p "$T/src/empty"; : > "$T/src/empty/$N.txt"
bash "$HERE/install.sh" "$T/src/empty/$N.txt" >/dev/null 2>&1; check "refuses an empty file" test $? -ne 0
mkdir -p "$T/src/nonvram"; echo 'hello' > "$T/src/nonvram/$N.txt"
bash "$HERE/install.sh" "$T/src/nonvram/$N.txt" >/dev/null 2>&1; check "refuses a txt without sromrev/boardtype" test $? -ne 0
check "nothing installed after refusals" test ! -e "$BRCM_STATE/installed.tsv" -a -z "$(ls "$BRCM_FWDIR")"

# ---- install over an existing file, then rollback
echo "old generic nvram" > "$BRCM_FWDIR/$N.txt"
bash "$HERE/install.sh" "$T/src/$N.txt" "$T/src/$N.clm_blob" >"$T/out.log" 2>&1; check "install succeeds" test $? -eq 0
check "txt replaced" cmp -s "$T/src/$N.txt" "$BRCM_FWDIR/$N.txt"
check "clm_blob installed" cmp -s "$T/src/$N.clm_blob" "$BRCM_FWDIR/$N.clm_blob"
check "failsafe armed" test -f "$BRCM_STATE/trial"
check "failsafe + rollback copied" test -x "$BRCM_LIBDIR/brcm-fw-failsafe.sh" -a -x "$BRCM_LIBDIR/rollback.sh"
check "unit installed" test -f "$BRCM_UNITDIR/brcm43602-fix-failsafe.service"
check "unit enabled via systemctl" grep -q 'systemctl enable brcm43602-fix-failsafe.service' "$BRCM_SYS_LOG"
check "no driver reload without --try-now" bash -c "! grep -q modprobe '$BRCM_SYS_LOG'"
check "backup holds the old file" bash -c "grep -rq 'old generic nvram' '$BRCM_BACKUP'"

bash "$HERE/install.sh" "$T/src/$N.txt" >/dev/null 2>&1   # second install must not lose the first backup
bash "$BRCM_LIBDIR/rollback.sh" --no-reload >"$T/rb.log" 2>&1; check "rollback succeeds" test $? -eq 0
check "old file restored" grep -q 'old generic nvram' "$BRCM_FWDIR/$N.txt"
check "new-only file removed" test ! -e "$BRCM_FWDIR/$N.clm_blob"
check "failsafe disarmed" test ! -e "$BRCM_STATE/trial"
check "no driver reload with --no-reload" bash -c "! grep -q modprobe '$BRCM_SYS_LOG'"
bash "$BRCM_LIBDIR/rollback.sh" --no-reload >/dev/null 2>&1; check "rollback twice is harmless" test $? -eq 0

# ---- --arm (lab mode): nothing installed, but a file dropped at the board name is removed by rollback
rm -rf "$BRCM_STATE"; mkdir -p "$BRCM_STATE"
bash "$HERE/install.sh" --arm >/dev/null 2>&1; check "--arm succeeds" test $? -eq 0
check "--arm records the board .txt as absent-before" grep -q "$N.txt.NONE" "$BRCM_STATE/installed.tsv"
check "--arm arms the failsafe" test -f "$BRCM_STATE/trial"
check "--arm marks a lab run" test -f "$BRCM_STATE/lab"
bash "$HERE/install.sh" --arm >/dev/null 2>&1; check "--arm twice records it once" test "$(grep -c "$N.txt" "$BRCM_STATE/installed.tsv")" -eq 1
echo "lab candidate" > "$BRCM_FWDIR/$N.txt"
bash "$BRCM_LIBDIR/rollback.sh" --no-reload >/dev/null 2>&1
check "rollback removes the lab file" test ! -e "$BRCM_FWDIR/$N.txt"
check "rollback disarms after a lab" test ! -e "$BRCM_STATE/trial" -a ! -e "$BRCM_STATE/lab"

# ---- --try-now arms a timer before reloading, rollback --boot reloads
: > "$BRCM_SYS_LOG"
bash "$HERE/install.sh" --try-now "$T/src/$N.txt" >/dev/null 2>&1
check "timer scheduled before reload" bash -c "grep -n -e systemd-run -e 'modprobe -r' '$BRCM_SYS_LOG' | head -1 | grep -q systemd-run"
check "driver reloaded" grep -q 'modprobe brcmfmac' "$BRCM_SYS_LOG"
: > "$BRCM_SYS_LOG"
bash "$HERE/install.sh" --keep >/dev/null 2>&1
check "--keep stops the timer" grep -q 'systemctl stop brcm43602-fix-restore.timer' "$BRCM_SYS_LOG"
: > "$BRCM_SYS_LOG"
bash "$BRCM_LIBDIR/rollback.sh" --boot >/dev/null 2>&1; check "rollback --boot with reload succeeds" test $? -eq 0
check "rollback reloads and restarts the Wi-Fi services" bash -c "grep -q 'modprobe brcmfmac' '$BRCM_SYS_LOG' && grep -q 'restart net-b.service' '$BRCM_SYS_LOG'"
check "services are stopped in reverse order" bash -c "grep -n 'stop net-' '$BRCM_SYS_LOG' | head -1 | grep -q net-b"
check "rollback cancels a pending timer" grep -q 'brcm43602-fix-restore.timer' "$BRCM_SYS_LOG"

# ---- failsafe decisions
export BRCM_FAILSAFE_SOURCE_ONLY=1 BRCM_FLAG=$T/state/trial
NETDIR=$T/net; mkdir -p "$NETDIR"
export BRCM_NET_DIR=$NETDIR BRCM_KNOWN_NETWORKS=$T/known.json
echo '{"networks":[{"ssid":"Home"},{"ssid":"Cafe Net"}]}' > "$T/known.json"
route=none; scan=""; kerr=""
cat > "$T/bin/ip" <<EOF
#!/bin/sh
case "\$*" in *route*) if [ "\$(cat $T/route)" = yes ]; then echo "default via 10.0.0.1 dev wlp2s0"; fi;; esac
exit 0
EOF
cat > "$T/bin/iw" <<EOF
#!/bin/sh
cat $T/scan
EOF
cat > "$T/bin/journalctl" <<EOF
#!/bin/sh
cat $T/kerr
EOF
chmod +x "$T"/bin/*
# shellcheck disable=SC1091
. "$HERE/dist/brcm-fw-failsafe.sh"
setcase() { echo "$1" > "$T/route"; printf '%b' "$2" > "$T/scan"; printf '%b' "$3" > "$T/kerr"; if [ "$4" = yes ]; then touch "$NETDIR/wlp2s0"; else rm -f "$NETDIR/wlp2s0"; fi; }
setcase yes ""  ""  yes;                                          check "route => pass"                                  test "$(decide)" = pass
setcase no  ""  ""  no;                                           check "no route, interface missing => restore"         test "$(decide)" = restore
setcase yes ""  ""  no;                                           check "route via a tether but Wi-Fi interface missing => restore" test "$(decide)" = restore
setcase yes ""  "brcmf_pcie_download_fw_nvram: FW failed to initialize\n" yes; check "route via a tether but firmware failed => restore" test "$(decide)" = restore
setcase no  ""  "brcmf_pcie_download_fw_nvram: failed\n" yes;     check "no route, firmware boot error => restore"       test "$(decide)" = restore
setcase no  "\tSSID: Neighbour\n\tSSID: Home\n" "" yes;           check "no route, known network in range => restore"    test "$(decide)" = restore
setcase no  "\tSSID: Neighbour\n\tSSID: Other\n" "" yes;          check "no route, nothing known in range => keep"       test "$(decide)" = keep
setcase no  ""  "brcmf_inetaddr_changed: fail to get arp ip table err:-52\n" yes; check "the benign arp-table line is not a firmware error" test "$(decide)" = keep
setcase no  "\tSSID: home\n" "" yes;                              check "SSID match is exact (case counts)"              test "$(decide)" = keep

# a lab run interrupted by a reboot: never "pass", always remove the candidate even if Wi-Fi came up fine
export BRCM_LAB_FLAG=$T/state/lab BRCM_FAILSAFE_DRY=1
unset BRCM_FAILSAFE_SOURCE_ONLY
touch "$BRCM_FLAG" "$BRCM_LAB_FLAG"; setcase yes "" "" yes
check "lab leftovers are removed even when Wi-Fi works" test "$(BRCM_FAILSAFE_TRIES=1 BRCM_FAILSAFE_STEP=0 bash "$HERE/dist/brcm-fw-failsafe.sh")" = restore-lab
rm -f "$BRCM_LAB_FLAG" "$BRCM_FLAG"; unset BRCM_FAILSAFE_DRY; export BRCM_LAB_FLAG=$T/state/no-such-lab

# whole script: route appears => flag removed, nothing reverted
unset BRCM_FAILSAFE_SOURCE_ONLY
export BRCM_FAILSAFE_TRIES=2 BRCM_FAILSAFE_STEP=0 BRCM_ROLLBACK=$T/fake-rollback
printf '#!/bin/sh\necho called "$@" > %s/rb-called\n' "$T" > "$T/fake-rollback"; chmod +x "$T/fake-rollback"
touch "$BRCM_FLAG"; setcase yes "" "" yes
bash "$HERE/dist/brcm-fw-failsafe.sh"; check "pass removes the trial flag" test ! -e "$BRCM_FLAG"
check "pass does not roll back" test ! -e "$T/rb-called"
touch "$BRCM_FLAG"; setcase no "" "" no
bash "$HERE/dist/brcm-fw-failsafe.sh"; check "restore runs rollback --boot" grep -q 'called --boot' "$T/rb-called"
rm -f "$T/rb-called"; touch "$BRCM_FLAG"; setcase no "" "" yes
bash "$HERE/dist/brcm-fw-failsafe.sh"; check "keep leaves flag and files alone" test -e "$BRCM_FLAG" -a ! -e "$T/rb-called"
rm -f "$BRCM_FLAG"; setcase no "" "" no
bash "$HERE/dist/brcm-fw-failsafe.sh"; check "no flag => does nothing" test ! -e "$T/rb-called"

echo; echo "$pass passed, $fail failed"
[ "$fail" = 0 ]
