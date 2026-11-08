#!/usr/bin/env python3
"""SmartClone - a simplified Clonezilla.

Main menu:
  Save     pick an operating system -> pick a destination (external disk,
           SMB/Windows share, NFS export or mounted folder) -> the whole disk
           that OS lives on is saved as an image folder.
  Restore  pick where the images are -> pick an image -> pick the target disk
           -> the disk is rebuilt (partition table, boot area, partitions).
  Network, keyboard layout, shell, reboot, power off.

Run it as root from the SmartClone live USB (see live/) or any live Linux.
Only the Python standard library is needed. Uses partclone (used blocks only)
when installed, ntfsclone for NTFS as a second choice, raw copy otherwise.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field

VERSION = "0.4.0"
WORK_DIR = "/run/smartclone"
LIVE_MEDIUM = "/run/live/medium"
CHUNK = 4 * 1024 * 1024
BOOT_AREA_MAX = 64 * 1024 * 1024
IMAGE_SEARCH_DEPTH = 3
MIN_DISK = 64 * 1024 * 1024  # hides floppies, card-reader stubs and the like
# EFI system, Linux /boot (XBOOTLDR) and Microsoft reserved partitions:
# never a place to keep images, never an OS root.
SYSTEM_PARTTYPES = {"c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0xef",
                    "bc13c2ff-59e6-4262-a352-b275fd6f7172",
                    "e3c9e316-0b5c-4db8-817d-f92df00215ae"}
RECOVERY_PARTTYPES = {"de94bba4-06d1-4d40-a16a-bfd50179d6ac", "0x27"}
ESP_PARTTYPES = {"c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0xef"}
MSR_PARTTYPE = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
# Folder name under EFI/ on the EFI system partition -> OS it boots.
EFI_VENDORS = {
    "microsoft": "Microsoft Windows", "fedora": "Fedora Linux", "ubuntu": "Ubuntu",
    "debian": "Debian GNU/Linux", "opensuse": "openSUSE", "arch": "Arch Linux",
    "redhat": "Red Hat Enterprise Linux", "centos": "CentOS", "rocky": "Rocky Linux",
    "almalinux": "AlmaLinux", "pop": "Pop!_OS", "manjaro": "Manjaro Linux",
    "neon": "KDE neon", "elementary": "elementary OS", "kali": "Kali Linux",
    "zorin": "Zorin OS", "mx": "MX Linux", "endeavouros": "EndeavourOS", "nixos": "NixOS",
}

# Filesystems we try to mount read-only to look for an OS and measure usage.
PROBE_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "ntfs", "vfat", "exfat", "f2fs"}
# Filesystems that can hold images on an external disk.
STORE_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "ntfs", "vfat", "exfat", "f2fs"}
# Read-only mount options that also avoid replaying journals on the source.
PROBE_OPTS = {
    "ext3": "ro,noload",
    "ext4": "ro,noload",
    "xfs": "ro,norecovery",
    "btrfs": "ro,subvolid=5",
}
PARTCLONE_TOOLS = {
    "ext2": ["partclone.ext2", "partclone.extfs"],
    "ext3": ["partclone.ext3", "partclone.extfs"],
    "ext4": ["partclone.ext4", "partclone.extfs"],
    "xfs": ["partclone.xfs"],
    "btrfs": ["partclone.btrfs"],
    "vfat": ["partclone.fat", "partclone.vfat", "partclone.fat32"],
    "exfat": ["partclone.exfat"],
    "ntfs": ["partclone.ntfs"],
    "f2fs": ["partclone.f2fs"],
    "hfsplus": ["partclone.hfsp"],
}
# Btrfs layouts used by common distros (Fedora: root, Ubuntu/openSUSE: @).
LINUX_ROOT_SUBDIRS = ["", "root", "@", "@root", "@rootfs"]
KEYMAPS = [("us", "English (US)"), ("uk", "English (UK)"), ("it", "Italiano"),
           ("de", "Deutsch"), ("fr", "Français"), ("es", "Español"),
           ("pt", "Português"), ("ch", "Schweiz"), ("be", "Belgique"),
           ("nl", "Nederlands"), ("se", "Svenska"), ("no", "Norsk"),
           ("dk", "Dansk"), ("fi", "Suomi"), ("pl", "Polski")]


class Abort(Exception):
    """User-facing error: shown in a message box, then back to the menu."""


LOG = []  # why things were not detected: shown by --list --debug, saved as probe.log


def note(msg):
    LOG.append(msg)


# --------------------------------------------------------------------------- utils

def human(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def run(cmd, check=True, **kw):
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    kw.setdefault("text", True)
    res = subprocess.run(cmd, **kw)
    if check and res.returncode != 0:
        err = (res.stderr or "").strip() if isinstance(res.stderr, str) else ""
        raise Abort(f"Command failed: {' '.join(cmd)}\n{err}")
    return res


def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "image"


def find_ci(base, *parts):
    """Case-insensitive path lookup (NTFS mounts may differ in case)."""
    path = base
    for part in parts:
        try:
            names = os.listdir(path)
        except OSError:
            return None
        match = next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        path = os.path.join(path, match)
    return path


def part_dev(disk_path, number):
    """/dev/sda + 2 -> /dev/sda2, /dev/nvme0n1 + 2 -> /dev/nvme0n1p2."""
    return f"{disk_path}{'p' if disk_path[-1].isdigit() else ''}{number}"


def disk_of_path(path):
    """Whole-disk device a mounted path lives on, None for network/virtual."""
    if not os.path.exists(path):
        return None
    src = run(["findmnt", "-n", "-o", "SOURCE", "--target", path], check=False).stdout.strip()
    src = re.sub(r"\[.*\]$", "", src)  # btrfs "/dev/sda2[/root]"
    if not src.startswith("/dev/"):
        return None
    parent = run(["lsblk", "-n", "-d", "-o", "PKNAME", src], check=False).stdout.strip()
    return f"/dev/{parent}" if parent else src


def ip_summary():
    res = run(["ip", "-4", "-o", "addr", "show", "scope", "global"], check=False)
    addrs = [f"{f[1]} {f[3].split('/')[0]}" for f in
             (line.split() for line in res.stdout.splitlines()) if len(f) > 3]
    return ", ".join(addrs) or "not connected"


# --------------------------------------------------------------------------- UI

class UI:
    """whiptail dialogs when available, plain prompts otherwise."""

    def __init__(self, plain=False):
        self.whiptail = (not plain and sys.stdin.isatty()
                         and shutil.which("whiptail") is not None)
        self.title = f"SmartClone {VERSION}"

    def size(self):
        cols, lines = shutil.get_terminal_size((80, 24))
        return max(lines - 4, 15), min(max(cols - 6, 60), 110)

    def _wt(self, args):
        h, w = self.size()
        sub = {"{H}": str(h), "{W}": str(w), "{L}": str(max(h - 9, 4))}
        args = [sub.get(a, a) for a in args]
        res = subprocess.run(["whiptail", "--title", self.title] + args,
                             stderr=subprocess.PIPE, text=True)
        return res.returncode, res.stderr.strip()

    def menu(self, text, items, cancel="Back"):
        """items: list of (tag, description). Returns tag, or None on cancel."""
        if self.whiptail:
            flat = [x for tag, desc in items for x in (tag, desc)]
            rc, out = self._wt(["--cancel-button", cancel, "--notags",
                                "--menu", text, "{H}", "{W}", "{L}"] + flat)
            return out if rc == 0 else None
        print(f"\n{text}")
        for i, (tag, desc) in enumerate(items, 1):
            print(f"  {i}) {desc}")
        while True:
            ans = input(f"Choice (empty = {cancel.lower()}): ").strip()
            if not ans:
                return None
            if ans.isdigit() and 1 <= int(ans) <= len(items):
                return items[int(ans) - 1][0]

    def ask(self, text, default="", password=False):
        if self.whiptail:
            kind = "--passwordbox" if password else "--inputbox"
            rc, out = self._wt([kind, text, "12", "{W}", default])
            return out if rc == 0 else None
        if password:
            import getpass
            return getpass.getpass(f"{text}: ")
        ans = input(f"{text} [{default}]: ").strip()
        return ans or default

    def yesno(self, text, yes="Yes", no="No"):
        if self.whiptail:
            rc, _ = self._wt(["--yes-button", yes, "--no-button", no,
                              "--defaultno", "--yesno", text, "{H}", "{W}"])
            return rc == 0
        return input(f"\n{text}\n[y/N]: ").strip().lower() in ("y", "yes")

    def msg(self, text):
        if self.whiptail:
            self._wt(["--msgbox", text, "{H}", "{W}"])
        else:
            print(f"\n{text}\n")

    def info(self, text):
        """Non-blocking 'please wait' box."""
        if self.whiptail:
            h, w = self.size()
            subprocess.run(["whiptail", "--title", self.title, "--infobox", text,
                            "8", str(w)], stderr=subprocess.DEVNULL)
        else:
            print(text)


class Progress:
    """Overall progress of a save/restore: whiptail gauge or a text line."""

    def __init__(self, ui, heading, total):
        self.ui, self.heading, self.total = ui, heading, max(total, 1)
        self.done, self.item = 0, ""
        self.start = self.last = time.monotonic()
        self.gauge = None
        if ui.whiptail:
            _, w = ui.size()
            # whiptail sizes the text area from the first text: reserve the
            # lines that _draw fills in later (blank, item, stats).
            first = heading + "\n\nStarting...\n "
            self.gauge = subprocess.Popen(
                ["whiptail", "--title", ui.title, "--gauge", first, "14", str(w), "0"],
                stdin=subprocess.PIPE, text=True)

    def step(self, item):
        self.item = item
        self._draw(force=True)

    def add(self, n):
        self.done += n
        self._draw()

    def _draw(self, force=False):
        now = time.monotonic()
        if not force and now - self.last < 0.5:
            return
        self.last = now
        elapsed = max(now - self.start, 1e-6)
        rate = self.done / elapsed
        pct = min(int(self.done * 100 / self.total), 99)
        eta = (self.total - self.done) / rate if rate and self.done < self.total else 0
        stats = (f"{human(self.done)} of ~{human(self.total)}   {human(rate)}/s   "
                 f"elapsed {duration(elapsed)}   left ~{duration(eta)}")
        if self.gauge:
            try:
                self.gauge.stdin.write(f"XXX\n{pct}\n{self.heading}\n\n{self.item}\n{stats}\nXXX\n")
                self.gauge.stdin.flush()
            except BrokenPipeError:
                self.gauge = None
        else:
            sys.stderr.write(f"\r  {pct:3d}%  {self.item:<28} {stats}".ljust(110))
            sys.stderr.flush()

    def close(self):
        if self.gauge:
            try:
                self.gauge.stdin.close()
            except BrokenPipeError:
                pass
            self.gauge.wait()
        else:
            sys.stderr.write("\n")


# --------------------------------------------------------------------------- disks

@dataclass
class Partition:
    name: str
    path: str
    size: int
    fstype: str = None
    label: str = None
    uuid: str = None
    parttype: str = None
    partuuid: str = None
    mountpoints: list = field(default_factory=list)
    used: int = None          # bytes in use, when we could measure it
    os_name: str = None
    inner_path: str = None    # unlocked LUKS: /dev/mapper device holding the filesystem
    inner_fstype: str = None
    boot_for: list = field(default_factory=list)    # OSes named by boot files on it
    fstab_refs: set = field(default_factory=set)    # UUID=/LABEL=... its /etc/fstab mounts

    @property
    def ptype(self):
        return (self.parttype or "").lower()

    @property
    def mounted(self):
        return [m for m in self.mountpoints if m and m != "[SWAP]"]


@dataclass
class Disk:
    name: str
    path: str
    size: int
    model: str = None
    tran: str = None
    removable: bool = False
    pttype: str = None
    partitions: list = field(default_factory=list)
    boot_names: list = field(default_factory=list)  # OSes named in its boot files

    def describe(self, short=False):
        if short:
            return f"{self.name} {human(self.size)}"
        bits = [self.name, human(self.size)]
        if self.tran:
            bits.append(self.tran.upper())
        if self.model:
            bits.append(self.model.strip())
        return "  ".join(bits)

    def contents(self):
        found = [p.os_name for p in self.partitions if p.os_name]
        if found:
            return ", ".join(found)
        labels = [p.label or p.fstype for p in self.partitions if p.label or p.fstype]
        return ", ".join(labels) or "empty"


LSBLK_COLS = "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,PTTYPE,PARTTYPE,PARTUUID,MODEL,TRAN,RM"


def list_disks():
    """All real disks except the SmartClone live USB itself."""
    res = run(["lsblk", "-J", "-b", "-o", LSBLK_COLS + ",MOUNTPOINTS"], check=False)
    if res.returncode != 0:  # util-linux < 2.37
        res = run(["lsblk", "-J", "-b", "-o", LSBLK_COLS + ",MOUNTPOINT"])
    live = disk_of_path(LIVE_MEDIUM)
    disks = []
    for dev in json.loads(res.stdout)["blockdevices"]:
        if dev.get("type") != "disk" or dev["path"] == live:
            continue
        if dev["name"].startswith(("zram", "ram", "fd", "sr")) or int(dev.get("size") or 0) < MIN_DISK:
            continue
        disk = Disk(name=dev["name"], path=dev["path"], size=int(dev["size"]),
                    model=dev.get("model"), tran=dev.get("tran"),
                    removable=dev.get("rm") in (True, "1", 1), pttype=dev.get("pttype"))
        for ch in dev.get("children") or []:
            if ch.get("type") != "part":
                continue
            # Mountpoints of anything stacked on the partition (unlocked LUKS,
            # LVM) count too: the partition is in use either way.
            mps, stack = [], [ch]
            while stack:
                node = stack.pop()
                mps += node.get("mountpoints") or [node.get("mountpoint")]
                stack += node.get("children") or []
            disk.partitions.append(Partition(
                name=ch["name"], path=ch["path"], size=int(ch["size"]),
                fstype=ch.get("fstype"), label=ch.get("label"), uuid=ch.get("uuid"),
                parttype=ch.get("parttype"), partuuid=ch.get("partuuid"),
                mountpoints=[m for m in mps if m]))
        disks.append(disk)
    return disks


class Mounts:
    """Tracks mounts and unlocked LUKS devices we make so they are always undone."""

    def __init__(self):
        self.stack = []
        self.mappers = []

    def unlock(self, dev, name, passphrase, readonly=True):
        """cryptsetup open; returns the /dev/mapper path or None (wrong passphrase)."""
        cmd = ["cryptsetup", "open", "--type", "luks", "--key-file", "-"]
        if readonly:
            cmd.append("--readonly")
        res = run(cmd + [dev, name], check=False, input=passphrase)
        if res.returncode != 0:
            note(f"cryptsetup open {dev}: {res.stderr.strip()}")
            return None
        self.mappers.append(name)
        run(["udevadm", "settle"], check=False)
        return f"/dev/mapper/{name}"

    def lock(self, name):
        run(["cryptsetup", "close", name], check=False)
        if name in self.mappers:
            self.mappers.remove(name)

    def mount(self, dev, target, fstype=None, options=None):
        os.makedirs(target, exist_ok=True)
        cmd = ["mount"]
        if fstype:
            cmd += ["-t", fstype]
        if options:
            cmd += ["-o", options]
        res = run(cmd + [dev, target], check=False)
        if res.returncode == 0:
            self.stack.append(target)
            return True
        note(f"{' '.join(cmd + [dev])}: {res.stderr.strip() or 'failed'}")
        return False

    def umount(self, target):
        run(["umount", target], check=False)
        if target in self.stack:
            self.stack.remove(target)
        try:
            os.rmdir(target)
        except OSError:
            pass

    def umount_all(self):
        for target in reversed(list(self.stack)):
            self.umount(target)
        for name in reversed(list(self.mappers)):
            self.lock(name)


def mount_ro(mounts, path, fs, target):
    if fs == "ntfs":
        # Windows Fast Startup leaves NTFS hibernated/dirty: ntfs3 then needs
        # "force"; ntfs-3g mounts it read-only anyway.
        attempts = [("ntfs3", "ro"), ("ntfs-3g", "ro"), ("ntfs3", "ro,force")]
    else:
        attempts = [(fs, PROBE_OPTS.get(fs, "ro")), (fs, "ro")]
    return any(mounts.mount(path, target, t, o) for t, o in attempts)


def read_os_release(path):
    vals = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    vals[k] = v.strip().strip('"').strip("'")
    except OSError:
        return None
    return vals.get("PRETTY_NAME") or " ".join(
        x for x in (vals.get("NAME"), vals.get("VERSION")) if x) or None


def windows_name(root):
    name = "Microsoft Windows"
    hive = find_ci(root, "Windows", "System32", "config", "SOFTWARE")
    if hive and shutil.which("hivexget"):
        key = r"\Microsoft\Windows NT\CurrentVersion"
        product = run(["hivexget", hive, key, "ProductName"], check=False).stdout.strip()
        build = run(["hivexget", hive, key, "CurrentBuild"], check=False).stdout.strip()
        if product:
            # Windows 11 still reports "Windows 10" in ProductName.
            if build.isdigit() and int(build) >= 22000:
                product = product.replace("Windows 10", "Windows 11")
            name = product
    return name


def detect_os(root):
    for sub in LINUX_ROOT_SUBDIRS:
        base = os.path.join(root, sub)
        for rel in ("etc/os-release", "usr/lib/os-release"):
            name = read_os_release(os.path.join(base, rel))
            if name:
                return name
    if find_ci(root, "Windows", "System32", "ntoskrnl.exe"):
        return windows_name(root)
    return None


def usage(root):
    try:
        st = os.statvfs(root)
        return (st.f_blocks - st.f_bfree) * st.f_frsize
    except OSError:
        return None


def boot_names(root):
    """OS names from an EFI system partition or a separate /boot partition.

    Works when the OS partition itself cannot be read (encrypted, BitLocker,
    hibernated Windows).
    """
    names = []
    efi = find_ci(root, "EFI")
    for vendor in sorted(os.listdir(efi)) if efi else []:
        name = EFI_VENDORS.get(vendor.lower())
        if vendor.lower() == "microsoft" and not find_ci(efi, vendor, "Boot", "bootmgfw.efi"):
            name = None
        if name:
            names.append(name)
    # Boot Loader Spec entries (Fedora, RHEL): "title Fedora Linux (6.17.1-300.fc44) 44 (...)"
    entries = find_ci(root, "loader", "entries")
    for entry in sorted(os.listdir(entries)) if entries else []:
        try:
            with open(os.path.join(entries, entry), errors="replace") as f:
                title = next((l.split(None, 1)[1].strip() for l in f
                              if l.startswith("title ") and len(l.split(None, 1)) > 1), "")
        except OSError:
            continue
        if title and "rescue" not in title.lower():
            names.append(re.sub(r"\s*\(\d[^)]*\)", "", title))  # drop kernel version
    return names


def same_os(a, b):
    """'Fedora Linux 44 (KDE)' matches EFI vendor 'Fedora Linux', Windows matches Windows."""
    key = lambda s: s.lower().replace("microsoft ", "").split()[0]
    return key(a) == key(b)


def probe(disks, mounts):
    """Fill in os_name/used for every partition we can look inside."""
    for disk in disks:
        for part in disk.partitions:
            if part.fstype == "crypto_LUKS":
                note(f"{part.name}: encrypted (LUKS), contents not readable without passphrase")
                continue
            if part.fstype not in PROBE_FS:
                continue
            if part.mounted:
                root = part.mounted[0]
                read_partition(part, root)
                if part.os_name and "/" in part.mounted:
                    part.os_name += "  (running now)"
                continue
            target = os.path.join(WORK_DIR, "probe", part.name)
            if not mount_ro(mounts, part.path, part.fstype, target):
                note(f"{part.name}: could not mount {part.fstype} read-only")
                continue
            try:
                read_partition(part, target)
            finally:
                mounts.umount(target)
            note(f"{part.name}: {part.fstype}, {part.os_name or 'no OS'}, used {human(part.used)}")
        disk.boot_names = [n for p in disk.partitions for n in p.boot_for]
        assign_boot_names(disk)


def read_partition(part, root):
    part.os_name = detect_os(root)
    part.used = usage(root)
    part.boot_for = boot_names(root)
    part.fstab_refs = fstab_refs(root) if part.os_name else set()


def fstab_refs(root):
    """Devices a Linux root mounts at boot: {"UUID=...", "LABEL=...", "/dev/sda3"}."""
    for sub in LINUX_ROOT_SUBDIRS:
        try:
            with open(os.path.join(root, sub, "etc", "fstab"), errors="replace") as f:
                return {l.split()[0] for l in f if l.strip() and not l.lstrip().startswith("#")}
        except OSError:
            continue
    return set()


def system_partitions(disk, os_part):
    """Partitions one OS needs to boot on its own."""
    windows = same_os(os_part.os_name, "Windows")
    keep = []
    for p in disk.partitions:
        refs = {f"UUID={p.uuid}", f"PARTUUID={p.partuuid}", f"LABEL={p.label}", p.path}
        if (p is os_part or p.ptype in ESP_PARTTYPES
                or (windows and (p.ptype == MSR_PARTTYPE or p.ptype in RECOVERY_PARTTYPES))
                or (not windows and (p.fstype == "swap"
                                     or any(same_os(n, os_part.os_name) for n in p.boot_for)
                                     or refs & os_part.fstab_refs))):
            keep.append(p)
    return keep


def assign_boot_names(disk):
    """Name partitions we could not read after the OSes their disk boots."""
    # Longest first: "Fedora Linux 44 (KDE Plasma Desktop Edition)" from /boot
    # beats the plain "Fedora Linux" from the EFI folder name.
    for name in sorted(set(disk.boot_names), key=len, reverse=True):
        if any(p.os_name and same_os(p.os_name, name) for p in disk.partitions):
            continue
        windows = same_os(name, "Windows")
        wanted = (("ntfs", "BitLocker") if windows else
                  ("crypto_LUKS", "LVM2_member", "ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs"))
        free = [p for p in disk.partitions if not p.os_name and p.fstype in wanted
                and (p.parttype or "").lower() not in SYSTEM_PARTTYPES
                and (p.parttype or "").lower() not in RECOVERY_PARTTYPES]
        if not free:
            note(f"{disk.name}: boot files name {name}, but no partition left to match it")
            continue
        part = max(free, key=lambda p: p.size)
        why = {"crypto_LUKS": "encrypted", "BitLocker": "BitLocker"}.get(part.fstype, "not readable")
        part.os_name = f"{name}  ({why})"
        note(f"{part.name}: named {name} from boot files")


def scan(ui, mounts):
    ui.info("Scanning disks...")
    LOG.clear()
    disks = list_disks()
    probe(disks, mounts)
    try:
        os.makedirs(WORK_DIR, exist_ok=True)
        with open(os.path.join(WORK_DIR, "probe.log"), "w") as f:
            f.write("\n".join(LOG) + "\n")
    except OSError:
        pass
    return disks


def os_choices(disks):
    """(label, disk, os name, partitions or None for the whole disk).

    A disk with several systems gets one entry per system (only its
    partitions) plus one for the whole disk; any disk can be saved whole.
    """
    with_os, without_os = [], []
    for disk in disks:
        found = [p for p in disk.partitions if p.os_name]
        if len(found) > 1:
            for p in found:
                with_os.append((f"{p.os_name} - only this system   ({disk.describe(True)})",
                                disk, p.os_name, system_partitions(disk, p)))
            names = " + ".join(short_name(p.os_name) for p in found)
            with_os.append((f"Whole disk: {names}   ({disk.describe(True)})",
                            disk, names, None))
        elif found:
            with_os.append((f"{found[0].os_name}   ({disk.describe()})", disk, found[0].os_name, None))
        else:
            without_os.append((f"No OS found - whole disk   ({disk.describe()})", disk, None, None))
    return with_os + without_os


def short_name(os_name):
    return re.sub(r"\s*\((encrypted|not readable|BitLocker|running now)\)", "", os_name).strip()


def check_not_busy(disk, parts=None):
    fresh = next((d for d in list_disks() if d.path == disk.path), disk)
    wanted = {p.path for p in (parts or disk.partitions)} or {p.path for p in fresh.partitions}
    busy = [f"{p.path} on {', '.join(p.mounted)}" for p in fresh.partitions
            if p.mounted and p.path in wanted]
    if busy:
        raise Abort(f"{disk.path} is in use:\n  " + "\n  ".join(busy) +
                    "\n\nUnmount it, or boot from the SmartClone USB to work on the running system.")


# --------------------------------------------------------------------------- image location

def choose_location(ui, disks, mounts, purpose, exclude=None):
    """Mount the place images are saved to / restored from.

    Returns (folder, name to show the user), or None if cancelled.
    """
    verb = "saved" if purpose == "save" else "loaded from"
    while True:
        kind = ui.menu(f"Where should the image be {verb}?", [
            ("usb", "External USB disk / other local disk"),
            ("smb", "Network share - Windows / NAS (SMB)"),
            ("nfs", "Network share - Linux / NAS (NFS)"),
            ("dir", "A folder that is already mounted"),
        ])
        if kind is None:
            return None
        target = os.path.join(WORK_DIR, "images")
        try:
            path = {"usb": loc_disk, "smb": loc_smb, "nfs": loc_nfs,
                    "dir": loc_dir}[kind](ui, disks, mounts, target, purpose, exclude)
        except Abort as e:
            ui.msg(str(e))
            continue
        if path:
            return path


def loc_disk(ui, disks, mounts, target, purpose, exclude):
    while True:
        items = []
        for d in disks:
            if exclude and d.path == exclude.path:
                continue
            for p in d.partitions:
                if (p.fstype not in STORE_FS or p.os_name
                        or (p.parttype or "").lower() in SYSTEM_PARTTYPES | RECOVERY_PARTTYPES
                        or any(m == "/" or m.startswith("/boot") for m in p.mounted)):
                    continue
                free = ""
                if p.mounted:
                    free = f"  free {human(shutil.disk_usage(p.mounted[0]).free)}"
                lbl = f"'{p.label}'  " if p.label else ""
                items.append((p.path, f"{p.name}  {human(p.size)}  {p.fstype}  "
                                      f"{lbl}{d.describe()}{free}"))
        items.append(("rescan", ">> Rescan (plug the disk in first)"))
        text = ("Pick the disk partition that holds the images."
                if purpose != "save" else "Pick the disk partition to save the image to.")
        if exclude:
            text += f"\nThe source disk {exclude.name} is not listed."
        choice = ui.menu(text, items)
        if choice is None:
            return None
        if choice == "rescan":
            ui.info("Looking for disks...")
            time.sleep(3)
            disks[:] = list_disks()
            continue
        part = next(p for d in disks for p in d.partitions if p.path == choice)
        name = f"{part.name} '{part.label}'" if part.label else part.name
        if part.mounted:
            return part.mounted[0], name
        attempts = [(None, None)]
        if part.fstype == "ntfs":
            attempts += [("ntfs3", None), ("ntfs-3g", None)]
        if not any(mounts.mount(part.path, target, t, o) for t, o in attempts):
            raise Abort(f"Could not mount {part.path} ({part.fstype}).")
        return target, name


def loc_smb(ui, disks, mounts, target, purpose, exclude):
    share = ui.ask("Share address, for example:\n  //192.168.1.10/backups\n  //nas/backups", "//")
    if not share or share == "//":
        return None
    share = share.replace("\\", "/")
    if not share.startswith("//"):
        share = "//" + share.lstrip("/")
    user = ui.ask("Username (leave empty for guest access)\nDomain users: DOMAIN\\user", "")
    if user is None:
        return None
    opts = ["uid=0", "gid=0", "file_mode=0600", "dir_mode=0700"]
    cred = None
    if user:
        domain = ""
        if "\\" in user:
            domain, user = user.split("\\", 1)
        password = ui.ask(f"Password for {user}", password=True)
        if password is None:
            return None
        os.makedirs(WORK_DIR, exist_ok=True)
        cred = os.path.join(WORK_DIR, "smb.cred")
        fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"username={user}\npassword={password}\n")
            if domain:
                f.write(f"domain={domain}\n")
        opts.append(f"credentials={cred}")
    else:
        opts.append("guest")
    ui.info(f"Connecting to {share}...")
    try:
        ok = mounts.mount(share, target, "cifs", ",".join(opts))
    finally:
        if cred:
            os.remove(cred)
    if not ok:
        raise Abort(f"Could not connect to {share}.\n\nCheck the network (main menu > Network),"
                    " the server and share name, and the username/password.")
    return pick_subfolder(ui, target, share, purpose)


def loc_nfs(ui, disks, mounts, target, purpose, exclude):
    export = ui.ask("NFS export, for example:\n  192.168.1.10:/srv/backups", "")
    if not export:
        return None
    if ":" not in export:
        raise Abort("The NFS export must look like  server:/path")
    ui.info(f"Connecting to {export}...")
    if not mounts.mount(export, target, "nfs"):
        raise Abort(f"Could not connect to {export}.\n\nCheck the network (main menu > Network)"
                    " and that this machine is allowed by the NFS server.")
    return pick_subfolder(ui, target, export, purpose)


def loc_dir(ui, disks, mounts, target, purpose, exclude):
    path = ui.ask("Folder path", "/mnt")
    if not path:
        return None
    if not os.path.isdir(path):
        raise Abort(f"{path} is not a folder.")
    return path, path


def pick_subfolder(ui, root, name, purpose):
    if purpose != "save":
        return root, name  # restore searches subfolders by itself
    sub = ui.ask("Folder inside the share to save into (empty = top level)", "")
    if sub is None:
        return None
    sub = sub.strip("/")
    path = os.path.join(root, sub)
    os.makedirs(path, exist_ok=True)
    return path, f"{name}/{sub}" if sub else name


# --------------------------------------------------------------------------- save

def compressor():
    if shutil.which("zstd"):
        return ["zstd", "-T0", "-3", "-q", "-c"], "zst"
    if shutil.which("pigz"):
        return ["pigz", "-c"], "gz"
    return ["gzip", "-c"], "gz"


def decompressor(ext):
    if ext == "zst":
        return ["zstd", "-dc"]
    return ["pigz", "-dc"] if shutil.which("pigz") else ["gzip", "-dc"]


def partclone_tool(fstype):
    for name in PARTCLONE_TOOLS.get(fstype, []):
        if shutil.which(name):
            return name
    return None


def plan_partition(part):
    """Returns (method, producer command or None, expected bytes)."""
    if part.fstype == "swap":
        return "swap", None, 0
    if part.inner_path:  # unlocked LUKS: save the filesystem inside + the LUKS header
        tool = partclone_tool(part.inner_fstype)
        if tool:
            return ("luks-partclone", [tool, "-c", "-s", part.inner_path, "-o", "-"],
                    part.used or part.size)
        return "luks-raw", None, part.size
    tool = partclone_tool(part.fstype)
    if tool:
        return "partclone", [tool, "-c", "-s", part.path, "-o", "-"], part.used or part.size
    if part.fstype == "ntfs" and shutil.which("ntfsclone"):
        return ("ntfsclone", ["ntfsclone", "--save-image", "--output", "-", part.path],
                part.used or part.size)
    return "raw", None, part.size


def pump(reader, writer, progress):
    """Copy reader -> writer. Returns (bytes, sha256 of the data)."""
    sha, done = hashlib.sha256(), 0
    while True:
        chunk = reader.read(CHUNK)
        if not chunk:
            break
        writer.write(chunk)
        sha.update(chunk)
        done += len(chunk)
        progress.add(len(chunk))
    return done, sha.hexdigest()


def partition_layout(disk_path):
    """Start sectors / numbers / sector size from sfdisk."""
    res = run(["sfdisk", "-J", disk_path], check=False)
    if res.returncode != 0:
        return {}, 512
    table = json.loads(res.stdout)["partitiontable"]
    sector = int(table.get("sectorsize", 512))
    layout = {}
    for p in table.get("partitions", []):
        num = re.search(r"(\d+)$", p["node"])
        layout[p["node"]] = {"start": int(p["start"]), "sectors": int(p["size"]),
                             "number": int(num.group(1)) if num else None}
    return layout, sector


def save_image(ui, disk, os_name, img_dir, shown, log, only=None):
    """Save the whole disk, or with `only` just those partitions (one system)."""
    comp_cmd, ext = compressor()
    manifest = {
        "tool": f"smartclone {VERSION}",
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "os": os_name,
        "scope": "system" if only else "disk",
        "systems": [short_name(p.os_name) for p in (only or disk.partitions) if p.os_name],
        "disk": {"name": disk.name, "path": disk.path, "size": disk.size,
                 "model": disk.model, "pttype": disk.pttype},
        "compression": ext,
        "partitions": [],
    }

    # Partition table + everything before the first partition (MBR, GRUB core).
    with open(os.path.join(img_dir, "pt.sfdisk"), "w") as f:
        res = run(["sfdisk", "-d", disk.path], check=False, stdout=f)
    has_table = res.returncode == 0
    if disk.pttype == "gpt" and shutil.which("sgdisk"):
        run(["sgdisk", f"--backup={os.path.join(img_dir, 'pt.sgdisk')}", disk.path],
            check=False, stdout=log, stderr=log)
    layout, sector = partition_layout(disk.path)
    # A GPT system-only image is laid out afresh on restore; MBR keeps offsets
    # and boot code, so it needs the boot area.
    if layout and not (only and disk.pttype == "gpt"):
        boot_len = min(min(v["start"] for v in layout.values()) * sector, BOOT_AREA_MAX)
        with open(disk.path, "rb") as src, open(os.path.join(img_dir, "boot-area.img"), "wb") as dst:
            dst.write(src.read(boot_len))
        manifest["boot_area_bytes"] = boot_len
    manifest["sector_size"] = sector
    manifest["partition_table"] = "pt.sfdisk" if has_table else None

    parts = only or disk.partitions
    if not parts:  # no partition table: save the whole device raw
        parts = [Partition(name=disk.name, path=disk.path, size=disk.size)]
    plans = [(p, *plan_partition(p)) for p in parts]

    progress = Progress(ui, f"Saving {os_name or disk.name}\nto {shown}",
                        sum(exp for _, _, _, exp in plans))
    try:
        for i, (part, method, cmd, _) in enumerate(plans, 1):
            entry = {"name": part.name, "path": part.path, "size": part.size,
                     "fstype": part.fstype, "label": part.label, "uuid": part.uuid,
                     "partuuid": part.partuuid, "parttype": part.parttype,
                     "method": method, **layout.get(part.path, {})}
            manifest["partitions"].append(entry)
            if method == "swap":
                continue  # recreated with mkswap on restore
            fs = part.fstype or "raw"
            if part.inner_path:
                fs = f"encrypted {part.inner_fstype or 'data'}"
                entry["inner_fstype"] = part.inner_fstype
                entry["luks_header"] = f"{part.name}.luks-header"
                run(["cryptsetup", "luksHeaderBackup", part.path, "--header-backup-file",
                     os.path.join(img_dir, entry["luks_header"])])
            progress.step(f"Partition {i}/{len(plans)}: {part.name} ({fs})")
            entry["file"] = f"{part.name}.{method}.{ext}"
            out_path = os.path.join(img_dir, entry["file"])
            with open(out_path, "wb") as out:
                comp = subprocess.Popen(comp_cmd, stdin=subprocess.PIPE, stdout=out)
                proc = None
                try:
                    if cmd:
                        log.write(f"\n=== {' '.join(cmd)}\n")
                        log.flush()
                        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log)
                        entry["bytes"], entry["sha256"] = pump(proc.stdout, comp.stdin, progress)
                    else:
                        with open(part.inner_path or part.path, "rb", buffering=0) as src:
                            entry["bytes"], entry["sha256"] = pump(src, comp.stdin, progress)
                finally:
                    comp.stdin.close()
                    comp_rc = comp.wait()
                    rc = 0
                    if proc:
                        proc.stdout.close()
                        rc = proc.wait()
            if rc != 0:
                hint = ""
                if part.fstype == "ntfs":
                    hint = ("\n\nIf Windows was not fully shut down (Fast Startup or hibernation):"
                            " start Windows, turn off Fast Startup (Control Panel > Power"
                            " Options > Choose what the power buttons do), shut down and try again.")
                raise Abort(f"{cmd[0]} failed on {part.path}.\nDetails in {shown}/smartclone.log{hint}")
            if comp_rc != 0:
                raise Abort(f"Writing {out_path} failed (destination full or disconnected?).")
            entry["stored_bytes"] = os.path.getsize(out_path)
    finally:
        progress.close()

    with open(os.path.join(img_dir, "image.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(img_dir, "RESTORE.txt"), "w") as f:
        f.write(restore_text(manifest))
    return manifest


def restore_text(m):
    d = " ".join(decompressor(m["compression"]))
    return "\n".join([
        f"Image of {m['os'] or 'a disk'} ({m['disk']['path']}, {human(m['disk']['size'])}),"
        f" created {m['created']} by {m['tool']}.",
        "",
        "Easiest: boot the SmartClone USB and choose 'Restore an image to a disk'.",
        "",
        "By hand, as root, onto a disk at least as big as the original",
        "(ALL DATA ON THE TARGET DISK IS DESTROYED):",
        "",
        "  TARGET=/dev/sdX          # or /dev/nvme0n1",
        "  P=''                     # use P=p for nvme/mmc (nvme0n1p1)",
        "",
        *(["dd if=boot-area.img of=$TARGET bs=1M conv=fsync",
           "sed -e '/^device:/d' -e '/^last-lba:/d' -e 's#^/dev/[^ ]*[^0-9]\\([0-9]\\+\\) :#${TARGET}${P}\\1 :#'"
           " pt.sfdisk | sfdisk $TARGET",
           "partprobe $TARGET", ""] if m.get("partition_table") and m.get("scope") != "system"
          else ["# Only some partitions of the original disk are in this image: create them",
                "# first (sizes and types in pt.sfdisk, numbers in image.json), or simply",
                "# restore with SmartClone, which lays them out by itself.", ""]),
        *[restore_cmd(p, d) for p in m["partitions"]],
        "sync",
        "",
        "Each partition's sha256 in image.json is of the uncompressed stream:",
        f"  {d} <file> | sha256sum",
        "",
    ])


def restore_cmd(p, d):
    num = p.get("number")
    dev = f"${{TARGET}}${{P}}{num}" if num else "$TARGET"
    f = p.get("file")
    if p["method"].startswith("luks-"):
        if p["method"] == "luks-partclone":
            tool = partclone_tool(p["inner_fstype"]) or f"partclone.{p['inner_fstype']}"
            write = f"{d} {f} | {tool} -r -s - -o /dev/mapper/restored"
        else:
            write = f"{d} {f} | dd of=/dev/mapper/restored bs=4M conv=fsync"
        return "\n".join([f"dd if={p['luks_header']} of={dev} bs=1M conv=fsync",
                          f"cryptsetup open {dev} restored     # same passphrase as before",
                          write, "cryptsetup close restored"])
    if p["method"] == "partclone":
        tool = partclone_tool(p["fstype"]) or f"partclone.{p['fstype']}"
        return f"{d} {f} | {tool} -r -s - -o {dev}"
    if p["method"] == "ntfsclone":
        return f"{d} {f} | ntfsclone --restore-image --overwrite {dev} -"
    if p["method"] == "raw":
        return f"{d} {f} | dd of={dev} bs=4M conv=fsync"
    opts = (f" -U {p['uuid']}" if p.get("uuid") else "") + \
           (f" -L {p['label']}" if p.get("label") else "")
    return f"mkswap{opts} {dev}"


def unlock_encrypted(ui, mounts, parts):
    """Ask for each LUKS partition's passphrase so only the used space is saved."""
    for part in parts:
        if part.fstype != "crypto_LUKS":
            continue
        mapper = None
        while mapper is None:
            what = f"\n{part.os_name}" if part.os_name else ""
            pw = ui.ask(f"{part.name} ({human(part.size)}) is encrypted.{what}\n\n"
                        "Enter its passphrase to save only the used space.\n"
                        "Cancel = copy the whole partition as it is (slow, the image is as "
                        "big as the partition).", password=True)
            if not pw:
                break
            ui.info("Unlocking...")
            mapper = mounts.unlock(part.path, f"smartclone-{part.name}", pw)
            if mapper is None:
                ui.msg("Wrong passphrase, try again.")
        if mapper is None:
            continue
        fs = run(["blkid", "-o", "value", "-s", "TYPE", mapper], check=False).stdout.strip()
        part.inner_path, part.inner_fstype = mapper, fs or None
        target = os.path.join(WORK_DIR, "probe", f"{part.name}-inner")
        if fs and mount_ro(mounts, mapper, fs, target):
            try:
                name = detect_os(target)
                part.used = usage(target)
            finally:
                mounts.umount(target)
            if name:
                part.os_name = f"{name}  (encrypted)"


def do_save(ui, mounts):
    disks = scan(ui, mounts)
    if not disks:
        raise Abort("No disks found.")
    choices = os_choices(disks)
    idx = ui.menu("Which system do you want to save?\n"
                  "'Only this system' saves just its partitions; the others save the whole disk.",
                  [(str(i), c[0]) for i, c in enumerate(choices)])
    if idx is None:
        return
    _, source, os_name, only = choices[int(idx)]
    parts = only or source.partitions
    check_not_busy(source, parts)
    unlock_encrypted(ui, mounts, parts)
    if os_name and "(encrypted)" in os_name:  # name may be more precise now
        os_name = next((p.os_name for p in parts if p.inner_path and p.os_name), os_name)

    loc = choose_location(ui, disks, mounts, "save", exclude=source)
    if loc is None:
        return
    dest, where = loc
    if disk_of_path(dest) == source.path:
        raise Abort(f"{dest} is on the disk being saved ({source.path}). Pick another place.")

    base = short_name(os_name or source.name)
    base = slug(base)
    if len(base) > 40:
        base = base[:40].rsplit("-", 1)[0]
    default = f"{base}-{datetime.datetime.now():%Y-%m-%d-%H%M}"
    while True:
        name = ui.ask("Name of the image (a folder with this name is created)", default)
        if not name:
            return
        img_dir = os.path.join(dest, slug(name))
        shown = f"{where}/{slug(name)}"
        if not os.path.exists(img_dir):
            break
        ui.msg(f"{shown} already exists, pick another name.")

    estimate = sum(plan_partition(p)[2] for p in parts) or source.size
    free = shutil.disk_usage(dest).free
    left_out = [f"{p.name} ({short_name(p.os_name) if p.os_name else p.label or p.fstype or 'empty'})"
                for p in source.partitions if p not in parts]
    summary = (f"Save:        {os_name or '(no OS found)'}"
               + ("  - only this system" if only else "") + "\n" +
               f"Disk:        {source.describe()}\n"
               f"Partitions:  {', '.join(p.name for p in parts) or 'none'}\n"
               + (f"Left out:    {', '.join(left_out)}\n" if only else "") +
               f"Save to:     {shown}\n"
               f"Data:        ~{human(estimate)} (compressed while saving)\n"
               f"Free space:  {human(free)}\n")
    if free < estimate:
        summary += "\nWARNING: free space is less than the data to save.\n" \
                   "Compression usually makes up for it, but saving may fail.\n"
    raw = sorted({p.fstype for p in parts
                  if p.fstype and p.fstype != "swap" and plan_partition(p)[0] == "raw"})
    if raw:
        summary += f"\nNote: {', '.join(raw)} partitions are copied in full (slower).\n"
    if not ui.yesno(summary + "\nStart saving?", yes="Start", no="Cancel"):
        return

    os.makedirs(img_dir)
    marker = os.path.join(img_dir, "INCOMPLETE")
    open(marker, "w").close()
    start = time.monotonic()
    with open(os.path.join(img_dir, "smartclone.log"), "w") as log:
        manifest = save_image(ui, source, os_name, img_dir, shown, log, only)
    os.remove(marker)
    ui.info("Flushing data to the destination...")
    run(["sync"], check=False)
    stored = sum(p.get("stored_bytes", 0) for p in manifest["partitions"])
    ui.msg(f"Image saved.\n\n{shown}\n\nSize: {human(stored)}   "
           f"Time: {duration(time.monotonic() - start)}")


# --------------------------------------------------------------------------- restore

def find_images(root):
    images = []
    base_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count("/") - base_depth >= IMAGE_SEARCH_DEPTH:
            dirnames[:] = []
        if "image.json" in filenames and "INCOMPLETE" not in filenames:
            try:
                with open(os.path.join(dirpath, "image.json")) as f:
                    images.append((dirpath, json.load(f)))
            except (OSError, ValueError):
                pass
            dirnames[:] = []
    images.sort(key=lambda im: im[1].get("created", ""), reverse=True)
    return images


def packed(m):
    """System-only GPT image: its partitions go one after another from the
    start of the disk (keeping their sizes, types and IDs)."""
    return m.get("scope") == "system" and m["disk"].get("pttype") == "gpt"


def needed_bytes(m):
    sector = m.get("sector_size", 512)
    parts = [p for p in m["partitions"] if "start" in p]
    if not parts:
        return m["disk"]["size"]
    if packed(m):
        grain = 1024 * 1024 // sector
        used = sum(-(-p["sectors"] // grain) * grain for p in parts)
        return (grain + used + 34) * sector
    # + room for the backup GPT at the end of the disk
    return (max(p["start"] + p["sectors"] for p in parts) + 34) * sector


def sfdisk_script(text, target, m=None):
    """Original dump retargeted: new device names, no fixed disk end.

    For a system-only image keep just its partitions (packed for GPT).
    Returns (script, {old partition number: new number}).
    """
    keep = None
    if m and m.get("scope") == "system":
        keep = [p["number"] for p in m["partitions"] if p.get("number")]
    lines, numbers = [], {}
    for line in text.splitlines():
        if line.startswith(("device:", "last-lba:")):
            continue
        found = re.match(r"^\s*(\S*?)(\d+)\s*:(.*)$", line)
        if found and "start=" in line:
            old, rest = int(found.group(2)), found.group(3)
            if keep is not None and old not in keep:
                continue
            new = old
            if keep is not None and packed(m):
                new = len(numbers) + 1
                rest = re.sub(r"start=\s*\d+,\s*", "", rest)
            numbers[old] = new
            line = f"{part_dev(target, new)} :{rest}"
        lines.append(line)
    return "\n".join(lines) + "\n", numbers


def wait_for(paths, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if all(os.path.exists(p) for p in paths):
            return True
        time.sleep(0.5)
    return False


def restore_image(ui, img_dir, m, target, log, mounts=None, passphrase=None):
    decomp = decompressor(m["compression"])
    parts = m["partitions"]

    ui.info("Writing partition table...")
    run(["wipefs", "-a", target.path], check=False, stdout=log, stderr=log)
    boot = os.path.join(img_dir, "boot-area.img")
    if os.path.exists(boot):
        with open(boot, "rb") as src, open(target.path, "r+b") as dst:
            dst.write(src.read())
            dst.flush()
            os.fsync(dst.fileno())
    numbers = {}
    if m.get("partition_table"):
        with open(os.path.join(img_dir, m["partition_table"])) as f:
            script, numbers = sfdisk_script(f.read(), target.path, m)
        res = run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", target.path],
                  check=False, input=script)
        log.write(res.stdout + res.stderr)
        if res.returncode != 0:
            raise Abort(f"Writing the partition table failed:\n{res.stderr.strip()}")
        run(["partprobe", target.path], check=False)
        run(["udevadm", "settle"], check=False)
    devs = {p["name"]: (part_dev(target.path, numbers.get(p["number"], p["number"]))
                        if p.get("number") else target.path) for p in parts}
    if not wait_for(devs.values()):
        raise Abort("The new partitions did not show up. Reboot and try again.")

    progress = Progress(ui, f"Restoring {m['os'] or 'image'}\nto {target.describe()}",
                        sum(p.get("bytes", 0) for p in parts))
    try:
        for i, p in enumerate(parts, 1):
            dev = devs[p["name"]]
            if p["method"] == "swap":
                cmd = ["mkswap"] + (["-U", p["uuid"]] if p.get("uuid") else []) + \
                      (["-L", p["label"]] if p.get("label") else []) + [dev]
                run(cmd, check=False, stdout=log, stderr=log)
                continue
            method, fstype, mapper = p["method"], p.get("fstype"), None
            if method.startswith("luks-"):
                # Same LUKS header as the original, so the same passphrase unlocks it;
                # then the filesystem goes inside.
                with open(os.path.join(img_dir, p["luks_header"]), "rb") as src, \
                        open(dev, "r+b") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
                mapper = f"smartclone-restore-{i}"
                dev = mounts.unlock(dev, mapper, passphrase, readonly=False)
                if dev is None:
                    raise Abort(f"Could not unlock the restored encrypted partition {devs[p['name']]}.")
                method, fstype = method[len("luks-"):], p.get("inner_fstype")
            progress.step(f"Partition {i}/{len(parts)}: {os.path.basename(devs[p['name']])} "
                          f"({'encrypted ' if mapper else ''}{fstype or 'raw'})")
            if method == "partclone":
                tool = partclone_tool(fstype)
                if not tool:
                    raise Abort(f"partclone for {fstype} is not installed.")
                cmd = [tool, "-r", "-s", "-", "-o", dev]
            elif method == "ntfsclone":
                cmd = ["ntfsclone", "--restore-image", "--overwrite", dev, "-"]
            else:
                cmd = None
            log.write(f"\n=== {p['file']} -> {' '.join(cmd) if cmd else dev}\n")
            log.flush()
            with open(os.path.join(img_dir, p["file"]), "rb") as f:
                dec = subprocess.Popen(decomp, stdin=f, stdout=subprocess.PIPE, stderr=log)
            consumer, rc = None, 0
            try:
                if cmd:
                    consumer = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=log)
                    _, sha = pump(dec.stdout, consumer.stdin, progress)
                else:
                    with open(dev, "r+b", buffering=0) as out:
                        _, sha = pump(dec.stdout, out, progress)
                        os.fsync(out.fileno())
            finally:
                dec.stdout.close()
                dec_rc = dec.wait()
                if consumer:
                    consumer.stdin.close()
                    rc = consumer.wait()
                if mapper:
                    mounts.lock(mapper)
            if dec_rc != 0 or rc != 0:
                raise Abort(f"Restoring {p['file']} to {dev} failed.\nDetails in {log.name}")
            if p.get("sha256") and sha != p["sha256"]:
                raise Abort(f"{p['file']} is damaged (checksum mismatch). The restore is incomplete.")
    finally:
        progress.close()
    fix_boot(ui, m, target, devs, mounts or Mounts(), log)


def fix_boot(ui, m, target, devs, mounts, log):
    """Make the restored disk boot on its own.

    System-only image: remove the other systems' folders from the EFI
    partition and point the fallback loader (EFI/BOOT/BOOTX64.EFI, what
    firmware starts on a disk it has no entry for) at this system.
    Always: add a firmware boot entry when running on UEFI.
    """
    esp = next((p for p in m["partitions"] if (p.get("parttype") or "").lower() in ESP_PARTTYPES), None)
    if not esp:
        return
    ui.info("Setting up the boot loader...")
    dev = devs[esp["name"]]
    mnt = os.path.join(WORK_DIR, "esp")
    if not mounts.mount(dev, mnt, "vfat"):
        note(f"fix_boot: could not mount {dev}")
        return
    try:
        efi = find_ci(mnt, "EFI")
        if not efi:
            return
        systems = m.get("systems") or []
        if m.get("scope") == "system":
            removed = []
            for vendor in os.listdir(efi):
                name = EFI_VENDORS.get(vendor.lower())
                if name and not any(same_os(name, sys_name) for sys_name in systems):
                    removed += [open(os.path.join(dirpath, f), "rb").read()
                                for dirpath, _, files in os.walk(os.path.join(efi, vendor))
                                for f in files if f.lower().endswith(".efi")]
                    shutil.rmtree(os.path.join(efi, vendor))
                    log.write(f"fix_boot: removed EFI/{vendor}\n")
            set_fallback_loader(efi, systems, removed, log)
        add_boot_entries(efi, target, dev, esp.get("partuuid"), log)
    finally:
        mounts.umount(mnt)


def own_loader(efi, systems):
    """(vendor folder, loader file name, label) of the first system's loader on the ESP."""
    for vendor in sorted(os.listdir(efi)):
        name = EFI_VENDORS.get(vendor.lower())
        if not name or not any(same_os(name, s) for s in systems):
            continue
        for loader in ("bootmgfw.efi", "shimx64.efi", "grubx64.efi"):
            path = find_ci(efi, vendor, "Boot", loader) if loader == "bootmgfw.efi" \
                else find_ci(efi, vendor, loader)
            if path:
                label = "Windows Boot Manager" if vendor.lower() == "microsoft" else name
                return path, label
    return None, None


def set_fallback_loader(efi, systems, removed, log):
    loader, _ = own_loader(efi, systems)
    if not loader:
        return
    boot = find_ci(efi, "BOOT") or os.path.join(efi, "BOOT")
    os.makedirs(boot, exist_ok=True)
    fallback = find_ci(boot, "BOOTX64.EFI") or os.path.join(boot, "BOOTX64.EFI")
    current = open(fallback, "rb").read() if os.path.exists(fallback) else None
    if current is not None and current not in removed:
        return  # already this system's (or a generic) loader
    shutil.copyfile(loader, fallback)
    # shim started from EFI/BOOT looks for grubx64.efi next to itself
    grub = os.path.join(os.path.dirname(loader), "grubx64.efi")
    if os.path.basename(loader).lower() == "shimx64.efi" and os.path.exists(grub):
        shutil.copyfile(grub, os.path.join(boot, "grubx64.efi"))
    log.write(f"fix_boot: EFI/BOOT/BOOTX64.EFI <- {os.path.relpath(loader, efi)}\n")


def add_boot_entries(efi, target, esp_dev, esp_partuuid, log):
    if not (os.path.isdir("/sys/firmware/efi") and shutil.which("efibootmgr")):
        return
    # Drop entries pointing at this EFI partition (earlier restores), then add fresh ones.
    if esp_partuuid:
        listing = run(["efibootmgr", "-v"], check=False).stdout
        for num in re.findall(rf"^Boot([0-9A-Fa-f]{{4}})\*?.*{re.escape(esp_partuuid)}",
                              listing, re.M | re.I):
            run(["efibootmgr", "-b", num, "-B"], check=False)
            log.write(f"fix_boot: removed old boot entry {num}\n")
    number = re.search(r"(\d+)$", esp_dev)
    for vendor in sorted(os.listdir(efi)):
        name = EFI_VENDORS.get(vendor.lower())
        if not name or not number:
            continue
        loader, label = own_loader(efi, [name])
        if loader:
            path = "\\" + os.path.relpath(loader, os.path.dirname(efi)).replace("/", "\\")
            res = run(["efibootmgr", "-c", "-d", target.path, "-p", number.group(1),
                       "-L", label, "-l", path], check=False)
            log.write(f"fix_boot: efibootmgr {label} {path}: rc={res.returncode} {res.stderr}\n")


def ask_image_passphrase(ui, img_dir, m):
    """Passphrase of the encrypted partition in the image, checked against its
    saved LUKS header. None if nothing is encrypted, False if cancelled."""
    headers = [p["luks_header"] for p in m["partitions"] if p.get("luks_header")]
    if not headers:
        return None
    while True:
        pw = ui.ask("This image contains an encrypted partition.\n"
                    "Enter the passphrase it had when it was saved:", password=True)
        if not pw:
            return False
        res = run(["cryptsetup", "open", "--test-passphrase", "--key-file", "-",
                   os.path.join(img_dir, headers[0])], check=False, input=pw)
        if res.returncode == 0:
            return pw
        ui.msg("Wrong passphrase, try again.")


def do_restore(ui, mounts):
    disks = scan(ui, mounts)
    loc = choose_location(ui, disks, mounts, "restore")
    if loc is None:
        return
    loc, where = loc
    ui.info("Looking for images...")
    images = find_images(loc)
    if not images:
        raise Abort(f"No SmartClone images found on {where} "
                    f"(searched {IMAGE_SEARCH_DEPTH} folders deep).")
    items = []
    for i, (path, m) in enumerate(images):
        stored = sum(p.get("stored_bytes", 0) for p in m["partitions"])
        scope = " - only this system" if m.get("scope") == "system" else ""
        items.append((str(i), f"{short_name(m.get('os') or 'Disk image')}{scope}   "
                              f"{m.get('created', '')[:16].replace('T', ' ')}   {human(stored)}"))
    idx = ui.menu("Which image do you want to restore?", items)
    if idx is None:
        return
    img_dir, m = images[int(idx)]
    missing = [p["file"] for p in m["partitions"] if p.get("file")
               and not os.path.exists(os.path.join(img_dir, p["file"]))]
    if missing:
        raise Abort("The image is incomplete, missing:\n  " + "\n  ".join(missing))

    need = needed_bytes(m)
    image_disk = disk_of_path(img_dir)
    targets = [d for d in disks if d.path != image_disk]
    if not targets:
        raise Abort("No disk to restore to (the disk holding the image cannot be used).")
    items = [(d.path, f"{d.describe(True)}   now: {short_name(d.contents())[:60]}"
                      + ("" if d.size >= need else "   TOO SMALL")) for d in targets]
    choice = ui.menu(f"Restore to which disk?\nThe image needs at least {human(need)}.\n"
                     "EVERYTHING on the chosen disk will be erased.", items)
    if choice is None:
        return
    target = next(d for d in targets if d.path == choice)
    if target.size < need:
        raise Abort(f"{target.name} is {human(target.size)}, the image needs {human(need)}.")
    check_not_busy(target)
    passphrase = ask_image_passphrase(ui, img_dir, m)
    if passphrase is False:
        return

    if not ui.yesno(f"Restore:  {m.get('os') or 'disk image'}  ({m.get('created', '')[:16]})\n"
                    f"From:     {where}/{os.path.relpath(img_dir, loc)}\n"
                    f"To:       {target.describe()}\n\n"
                    f"!!! ALL DATA ON {target.name} WILL BE ERASED !!!\n"
                    f"It currently contains: {target.contents()}\n\nContinue?",
                    yes="Erase and restore", no="Cancel"):
        return
    typed = ui.ask(f"To confirm, type the disk name:  {target.name}", "")
    if typed is None or typed.strip() != target.name:
        ui.msg("Name did not match. Nothing was changed.")
        return

    start = time.monotonic()
    os.makedirs(WORK_DIR, exist_ok=True)
    log_path = os.path.join(WORK_DIR, "restore.log")
    with open(log_path, "w") as log:
        restore_image(ui, img_dir, m, target, log, mounts, passphrase)
    ui.info("Flushing data to the disk...")
    run(["sync"], check=False)
    ui.msg(f"Restore finished in {duration(time.monotonic() - start)}.\n\n"
           f"{target.describe()} now holds {m.get('os') or 'the image'}.\n"
           "Remove the SmartClone USB and reboot.")


# --------------------------------------------------------------------------- main menu

def do_keyboard(ui, mounts):
    layout = ui.menu("Keyboard layout", KEYMAPS)
    if layout and run(["loadkeys", layout], check=False).returncode != 0:
        raise Abort(f"Could not load keyboard layout '{layout}'.")


def do_network(ui, mounts):
    if shutil.which("nmtui"):
        subprocess.run(["nmtui"])
    else:
        ui.msg("No network tool (nmtui) available.\nWired networks with DHCP connect by themselves.")


def do_power(command):
    def action(ui, mounts):
        if ui.yesno(f"{command.capitalize()} now?"):
            mounts.umount_all()
            run(["systemctl", command], check=False)
            sys.exit(0)
    return action


def run_action(ui, action):
    mounts = Mounts()
    try:
        action(ui, mounts)
    except Abort as e:
        ui.msg(str(e))
    except KeyboardInterrupt:
        ui.msg("Interrupted. A partly saved image is marked with an INCOMPLETE file;"
               " a partly restored disk will not boot.")
    finally:
        mounts.umount_all()


def main_menu(ui):
    actions = {
        "save": do_save, "restore": do_restore, "network": do_network,
        "keyboard": do_keyboard, "reboot": do_power("reboot"),
        "poweroff": do_power("poweroff"),
    }
    while True:
        choice = ui.menu(f"Network: {ip_summary()}\n\nWhat do you want to do?", [
            ("save", "Save a disk to an image   (backup)"),
            ("restore", "Restore an image to a disk"),
            ("network", "Network settings   (Wi-Fi, IP address)"),
            ("keyboard", "Keyboard layout"),
            ("shell", "Exit to command line   (type 'smartclone' to come back)"),
            ("reboot", "Reboot"),
            ("poweroff", "Power off"),
        ], cancel="Refresh")
        if choice == "shell" or (choice is None and not ui.whiptail):
            return
        if choice in actions:
            run_action(ui, actions[choice])


def main():
    ap = argparse.ArgumentParser(description="Simplified Clonezilla: save and restore disk images.")
    ap.add_argument("--save", action="store_true", help="go straight to saving an image")
    ap.add_argument("--restore", action="store_true", help="go straight to restoring an image")
    ap.add_argument("--list", action="store_true", help="only list detected operating systems")
    ap.add_argument("--plain", action="store_true", help="plain text prompts instead of dialogs")
    ap.add_argument("--debug", action="store_true", help="with --list: show why things were not detected")
    args = ap.parse_args()

    if os.geteuid() != 0:
        raise Abort("Run as root (sudo), ideally from the SmartClone USB.")
    for tool in ("lsblk", "sfdisk", "mount", "umount", "findmnt"):
        if not shutil.which(tool):
            raise Abort(f"Missing required tool: {tool}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))

    ui = UI(plain=args.plain or args.list)
    if args.list:
        mounts = Mounts()
        try:
            for choice in os_choices(scan(ui, mounts)):
                print(" ", choice[0])
                if choice[3]:
                    print("      partitions:", ", ".join(p.name for p in choice[3]))
            if args.debug:
                print("\nDetails:")
                for line in LOG:
                    print(" ", line)
        finally:
            mounts.umount_all()
    elif args.save:
        run_action(ui, do_save)
    elif args.restore:
        run_action(ui, do_restore)
    else:
        main_menu(ui)


if __name__ == "__main__":
    try:
        main()
    except Abort as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(1)
