#!/usr/bin/env bash
#
# mount_cache_disk.sh — mount a dedicated disk at the S3 cache / warehouse dir
# and make it persist across reboots.
#
# The reporting pipeline writes its heavy, high-churn data under
#   /root/s3reporting/.cache          (S3_CACHE_DIR)  — S3 cache pickles
#   /root/s3reporting/.cache/prepared (DATA_DIR)      — all *.db warehouses
# Putting that on its own disk spares the SD card and isolates a disk failure
# from the code + secrets, which stay on the SD card.
#
# Usage:
#   sudo ./mount_cache_disk.sh /dev/sda            # disk already has a filesystem
#   sudo FORMAT=1 ./mount_cache_disk.sh /dev/sda   # brand-new disk: partition + format (ERASES IT)
#
# Optional overrides:
#   MOUNTPOINT=/root/s3reporting/.cache   # where to mount (default shown)
#   LABEL=s3cache                         # filesystem label used when FORMAT=1
#   OWNER=root:root                       # ownership applied to the mountpoint
#
set -euo pipefail

DEVICE="${1:-}"
MOUNTPOINT="${MOUNTPOINT:-/root/s3reporting/.cache}"
LABEL="${LABEL:-s3cache}"
OWNER="${OWNER:-root:root}"
FORMAT="${FORMAT:-0}"

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)."
[ -n "$DEVICE" ]     || die "pass the disk device, e.g. sudo $0 /dev/sda   (check with: lsblk)"
[ -b "$DEVICE" ]     || die "$DEVICE is not a block device. Check 'lsblk' and pass the whole disk (e.g. /dev/sda)."

# --- Safety guards ---------------------------------------------------------
# Refuse the SD card / boot disk.
case "$DEVICE" in
  /dev/mmcblk*) die "$DEVICE looks like the SD card / boot disk. Refusing." ;;
esac
# Refuse a disk that currently holds the root filesystem.
ROOT_SRC="$(findmnt -no SOURCE / || true)"
if [ -n "$ROOT_SRC" ] && [[ "$ROOT_SRC" == "$DEVICE"* ]]; then
  die "$DEVICE hosts the root filesystem (/). Refusing."
fi
# Refuse a disk (or any of its partitions) that is currently mounted.
if lsblk -no MOUNTPOINT "$DEVICE" 2>/dev/null | grep -q . ; then
  die "$DEVICE (or a partition of it) is already mounted. Unmount it first:  lsblk $DEVICE"
fi

# Partition suffix differs for nvme/mmc (p1) vs sd* (1).
case "$DEVICE" in
  *[0-9]) PART="${DEVICE}p1" ;;   # e.g. /dev/nvme0n1 -> /dev/nvme0n1p1
  *)      PART="${DEVICE}1"  ;;   # e.g. /dev/sda      -> /dev/sda1
esac

# --- Partition + format (only with FORMAT=1) -------------------------------
if [ "$FORMAT" = "1" ]; then
  echo ">> FORMAT=1 — this will ERASE all data on $DEVICE"
  lsblk "$DEVICE"
  read -r -p "Type the device name again to confirm ($DEVICE): " CONFIRM
  [ "$CONFIRM" = "$DEVICE" ] || die "confirmation did not match. Aborting."

  echo ">> Partitioning $DEVICE (GPT, single ext4 partition)"
  parted "$DEVICE" --script mklabel gpt mkpart primary ext4 0% 100%
  udevadm settle || true; sleep 2
  echo ">> Creating ext4 filesystem on $PART (label: $LABEL)"
  mkfs.ext4 -F -L "$LABEL" "$PART"
else
  # No format: find the partition that actually has a filesystem.
  if ! blkid "$PART" >/dev/null 2>&1; then
    if blkid "$DEVICE" >/dev/null 2>&1; then
      PART="$DEVICE"            # filesystem written to the whole disk, no partition table
    else
      die "no filesystem found on $DEVICE or $PART. For a new disk, re-run with: sudo FORMAT=1 $0 $DEVICE"
    fi
  fi
fi

UUID="$(blkid -s UUID -o value "$PART")"
[ -n "$UUID" ] || die "could not read UUID of $PART."
echo ">> Filesystem UUID: $UUID"

# --- Preserve anything already living at the mountpoint --------------------
# If the target dir already has data (and isn't already a mount), copy it onto
# the new disk so nothing gets hidden underneath the mount.
mkdir -p "$MOUNTPOINT"
if ! mountpoint -q "$MOUNTPOINT" && [ -n "$(ls -A "$MOUNTPOINT" 2>/dev/null || true)" ]; then
  echo ">> $MOUNTPOINT already has data — copying it onto the new disk first"
  TMP_MNT="$(mktemp -d)"
  mount "$PART" "$TMP_MNT"
  rsync -aHAX --info=progress2 "$MOUNTPOINT"/ "$TMP_MNT"/
  umount "$TMP_MNT"
  rmdir "$TMP_MNT"
fi

# --- Persist in /etc/fstab (by UUID, idempotent) ---------------------------
FSTAB_LINE="UUID=$UUID  $MOUNTPOINT  ext4  defaults,nofail,x-systemd.device-timeout=15  0  2"
# Drop any prior entry for this mountpoint or this UUID, then add the fresh one.
if grep -qE "([[:space:]]$MOUNTPOINT[[:space:]]|UUID=$UUID[[:space:]])" /etc/fstab; then
  echo ">> Removing existing /etc/fstab entry for $MOUNTPOINT / this UUID"
  cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
  grep -vE "([[:space:]]$MOUNTPOINT[[:space:]]|UUID=$UUID[[:space:]])" /etc/fstab > /etc/fstab.tmp
  mv /etc/fstab.tmp /etc/fstab
fi
echo "$FSTAB_LINE" >> /etc/fstab
echo ">> Added to /etc/fstab: $FSTAB_LINE"

# --- Mount and verify ------------------------------------------------------
systemctl daemon-reload 2>/dev/null || true
mount "$MOUNTPOINT"
chown "$OWNER" "$MOUNTPOINT"

echo
echo ">> Done. Verification:"
findmnt "$MOUNTPOINT"
df -h "$MOUNTPOINT"
echo
echo ">> 'nofail' means a future disk failure won't block the Pi from booting."
echo ">> If the warehouses were lost with the old disk, restore them with:"
echo ">>   cd /root/s3reporting && python3 restore_from_sharepoint.py"
