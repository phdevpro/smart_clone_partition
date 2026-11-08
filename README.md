# SmartClone

A simplified Clonezilla on a bootable USB stick. Plug it in, boot from it, and a menu opens:

```
 Save a disk to an image   (backup)
 Restore an image to a disk
 Network settings   (Wi-Fi, IP address)
 Keyboard layout
 Exit to command line
 Reboot
 Power off
```

**Save** — pick the operating system (Windows and Linux are recognised by name), pick where
to save (external USB disk, Windows/NAS share via SMB, NFS share, or a mounted folder),
confirm. The whole disk that OS lives on is saved — partition table, boot area and every
partition — so a restore boots again.

**Restore** — pick where the images are, pick an image (listed by OS and date), pick the
target disk, type its name to confirm. The disk is rebuilt exactly as it was.

Everything is arrow keys + Enter, with a progress bar while it works.

**Dual boot and encryption** — on a disk with several systems each one is listed by name,
also when it can't be read directly: names come from the EFI system partition
(`EFI/Microsoft`, `EFI/fedora`, …) and from `/boot` entries. An encrypted (LUKS) Linux
asks for its passphrase: with it only the used space is saved (the LUKS header is kept, so
the restored disk opens with the same passphrase); without it the partition is copied as is.
Hibernated Windows (Fast Startup) is saved read-only; for a consistent copy shut Windows
down fully first (disable Fast Startup).

If a system is not listed, open the command line and run `smartclone --list --debug` (or see
`/run/smartclone/probe.log`): it says why each partition was not recognised.

## 1. Build the USB image

Needs Docker and internet access (about 2–5 minutes, ~1 GB download the first time):

```bash
./live/build.sh
```

Result: `dist/smartclone.iso` (~460 MB). Boots on UEFI with Secure Boot on or off and on
legacy BIOS. Secure Boot uses Debian's Microsoft-signed shim, GRUB and kernel; the build
fails if the EFI loader is not the signed shim.

## 2. Write it to a USB stick

**This erases the USB stick.** Find its device name first (plug it in, then look for the
stick by size/model):

```bash
lsblk -d -o NAME,SIZE,MODEL,TRAN
```

Then write it, replacing `sdX` with the stick (never your system disk):

```bash
sudo dd if=dist/smartclone.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

On Windows or macOS use [balenaEtcher](https://etcher.balena.io/) or Rufus (DD mode) instead.

## 3. Use it

Boot the PC from the stick (boot menu key is usually F12, F11, F8 or Esc). After 5 seconds
the SmartClone menu opens by itself.

- **Secure Boot** can stay on. A few PCs only trust Microsoft's own Windows key (no
  "third-party UEFI CA"); on those, enable "Allow Microsoft 3rd-party UEFI CA" in the
  firmware or turn Secure Boot off.
- Wired network with DHCP connects by itself; use *Network settings* for Wi-Fi or a fixed IP.
- Set *Keyboard layout* before typing share passwords if you don't use a US keyboard.
- The images folder can be on another USB disk (ext4, NTFS, exFAT, FAT32…) or a network share.
  The USB stick with SmartClone itself is read-only and never offered as a source or target.

## Image layout

Each image is a folder:

```
Ubuntu-26.04-LTS-2026-11-08-1134/
  image.json        manifest: OS, disk, partitions, method, sha256 per partition
  RESTORE.txt       commands to restore by hand, without SmartClone
  pt.sfdisk         partition table
  pt.sgdisk         GPT backup (GPT disks)
  boot-area.img     bytes before the first partition (MBR + bootloader)
  sda1.partclone.zst / sda3.ntfsclone.zst / sda2.raw.zst ...
  smartclone.log
```

Partitions are saved with partclone (only used space), compressed with zstd. Swap is not
copied, it is recreated on restore. Restore checks every partition's checksum. A folder with
an `INCOMPLETE` file is an interrupted save and is not offered for restore.

## Running without the USB

`smartclone.py` also runs on any Linux as root (Python 3.8+, standard library only):

```bash
sudo python3 smartclone.py            # menu
```

```bash
sudo python3 smartclone.py --save     # straight to save; also --restore, --list, --plain
```

It refuses to touch a disk with mounted partitions, so to save the running system boot
from the USB.

## Project layout

```
smartclone.py                 the tool (installed as /usr/local/bin/smartclone on the USB)
live/build.sh                 builds dist/smartclone.iso with Debian live-build in Docker
live/lb-build.sh              build steps run inside the container
live/config/                  live-build overlay: package list, autologin + autostart, boot menu
```
