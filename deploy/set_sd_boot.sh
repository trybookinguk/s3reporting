#!/usr/bin/env bash
#
# set_sd_boot.sh — set the Raspberry Pi bootloader to boot from the SD card only
# (BOOT_ORDER=0xf1), so it never tries the USB data disk at boot.
#
# The USB drive (/dev/sda1) is a data disk with no OS; a USB-first boot order
# (e.g. 0xf14) makes the Pi scan it first and fall through to SD, which looks
# like "trying to boot from USB" and slows boot. 0xf1 = try SD, retry SD, never
# USB. Pass a different order as $1 (e.g. 0xf41 to keep USB as a fallback).
#
# Pi 4 / 5 / 400 / CM4 only (uses rpi-eeprom). Reboot required to take effect.
#
# Usage:  sudo ./deploy/set_sd_boot.sh          # sets 0xf1 (SD only)
#         sudo ./deploy/set_sd_boot.sh 0xf41    # SD first, USB fallback
set -euo pipefail

WANT="${1:-0xf1}"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
command -v rpi-eeprom-config >/dev/null || {
  echo "ERROR: rpi-eeprom-config not found — this Pi model doesn't use the EEPROM bootloader" >&2
  echo "       (Pi 3 and earlier set boot order differently). Check: cat /proc/device-tree/model" >&2
  exit 1
}

CUR="$(rpi-eeprom-config | grep -oP 'BOOT_ORDER=\K\S+' || echo '(unset)')"
echo ">> Current BOOT_ORDER: $CUR"
if [ "$CUR" = "$WANT" ]; then
  echo ">> Already $WANT — nothing to do."
  exit 0
fi

tmp="$(mktemp)"
rpi-eeprom-config --out "$tmp"
if grep -q '^BOOT_ORDER=' "$tmp"; then
  sed -i "s/^BOOT_ORDER=.*/BOOT_ORDER=$WANT/" "$tmp"
else
  echo "BOOT_ORDER=$WANT" >> "$tmp"
fi
rpi-eeprom-config --apply "$tmp"
rm -f "$tmp"

echo ">> BOOT_ORDER staged: $WANT"
rpi-eeprom-config | grep BOOT_ORDER
echo ">> Reboot to apply:  sudo reboot"
echo ">> After reboot verify:  findmnt /   (expect /dev/mmcblk0p2)"
