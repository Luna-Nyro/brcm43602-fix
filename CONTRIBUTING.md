# Contributing

Thanks for helping. This is a small research project about one Wi-Fi card in one laptop model, so the most
useful contributions are measurements and careful fixes.

## Reporting results

If you ran the tools on your own MacBookPro14,2 (or another Mac with a BCM43602), please open an issue with:

* machine model, kernel version, distribution, `brcmfmac` firmware version (from `dmesg`);
* what you ran and the relevant `results.json` / kernel lines.

**Before posting, remove anything that identifies you or your hardware:** the card's `macaddr`, `boardnum`,
BSSIDs and SSIDs from scans, and raw SROM dumps (`cc-0800.bin` etc. contain your MAC address). Don't attach
anything extracted from Apple's packages (`work/kext/`, `work/cache/`). That's Apple's copyrighted code.

## Code changes

* Python tools use the standard library only and must keep working under `python3 -I`.
* Anything that touches the system (firmware files, the driver, systemd) must go through the existing safety
  net: a timer or failsafe armed *before* the change, and `rollback.sh` able to undo it.
* Shell scripts honour `BRCM_NO_SYSTEM=1` so the tests can run them against fake directories. Keep new system
  calls behind the `sys` helper.
* Add or update tests in `tests/` and run all of them before sending a pull request:

  ```bash
  python3 -I tests/test_fetch.py
  python3 -I tests/test_srom.py
  python3 -I tests/test_lab.py
  bash tests/test_scripts.sh
  ```

* Don't hard-code personal paths, SSIDs, MAC addresses or service names. Use the `BRCM_*` settings (see the
  README's Configuration section).
