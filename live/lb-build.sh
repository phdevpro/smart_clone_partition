#!/usr/bin/env bash
# Runs inside the debian:trixie container started by build.sh.
set -euo pipefail
OWNER="$1"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq live-build xorriso >/dev/null

WORK=/src/.build/work
rm -rf "$WORK"
mkdir -p "$WORK" /src/.build/cache
cd "$WORK"
ln -s ../cache cache

lb config \
    --distribution trixie \
    --architectures amd64 \
    --archive-areas "main contrib non-free non-free-firmware" \
    --binary-images iso-hybrid \
    --uefi-secure-boot enable \
    --apt-recommends false \
    --debian-installer none \
    --memtest none \
    --firmware-chroot false \
    --firmware-binary false \
    --iso-application SmartClone \
    --iso-volume SMARTCLONE \
    --iso-publisher SmartClone \
    --bootappend-live "boot=live components quiet hostname=smartclone noautologin"

cp -r /src/live/config/. config/
install -D -m 755 /src/smartclone.py config/includes.chroot_after_packages/usr/local/bin/smartclone

lb build

mkdir -p /src/dist
# Secure Boot: the EFI loader must be Debian's Microsoft-signed shim.
xorriso -osirrox on -indev live-image-amd64.hybrid.iso -extract /EFI/boot/bootx64.efi /tmp/bootx64.efi 2>/dev/null
grep -q "shim" /tmp/bootx64.efi || { echo "ERROR: bootx64.efi is not shim, Secure Boot would fail" >&2; exit 1; }
cp live-image-amd64.hybrid.iso /src/dist/smartclone.iso
chown -R "$OWNER" /src/dist /src/.build
