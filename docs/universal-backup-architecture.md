# fsync as the home network's backup, archive and file inventory

*Design, agreed in outline 2026-11-11. Nothing in this document is built yet unless a
section says so. Decisions marked **Decided** came from the owner; items under
"Open questions" still need an answer.*

## 1. Goals

Standing goals of fsync (unchanged):

1. Minimise the risk of losing files.
2. Make switching between workstations convenient, with transparent continuation of
   work in the same git or svn projects.

This design adds, in service of goal 1:

- **a.** Automatic discovery of every file on every computer, with one central
  inventory database.
- **b.** Automatic backup of unique files to ZFS on files4dx.
- **c.** A permanent record of every event: discovery, sync, backup, verification,
  re-allocation, restore.
- **d.** Sensible placement of external drives, and a planner that proposes moves.
- **e.** A manually started command to scan and archive USB flash drives and SD cards.

## 2. Decisions

| Topic | Decision |
|---|---|
| Language | **Decided:** all fsync code migrates to Go. One static binary per platform. |
| Scan scope | **Decided:** all computers, including Windows PCs (`C:\shared`, user folders, data drives). |
| Windows | **Decided:** the agent runs as a Windows service. Two laptops are on Windows 10 and cannot run Windows 11; they are replaced with Linux once their media is verified in the archive. |
| VM disks | **Decided:** scan inside VM disk images to find media files. |
| External drives | **Decided:** most are attached to files4dx, powered through API-controlled smart plugs. |
| Offsite | **Decided:** a second file server in a family member's garage, connected by VPN to the home network. |
| Deletion | **Decided:** always manual and confirmed. Nothing is deleted automatically, anywhere. |
| Copy target | Proposed: one working copy, one on `tank`, one cold or offsite. |
| Conflict policy for sync | Unchanged: newest wins, loser backed up. |

## 3. Where things stand today (measured 2026-11-11)

- files4dx: Ryzen 7 2700X, 32 GB, encrypted RAIDZ2 pool `tank` (4 x 18 TB, 31.4 TB
  free). Running: Garage (S3), Caddy, ntfy, Beszel, NFS export for Proxmox dumps.
  sanoid and syncoid are installed with no policy. Tailscale is not running.
- ~~The fsync hub (51 GB) is in `~/.fsync/hub` on the single boot NVMe, outside `tank`,
  with no snapshots and in no backup job.~~ **Fixed 2026-11-11 (phase 0 done):** the hub
  is on dataset `tank/hub`, reached through a symlink at `~/.fsync/hub`; sanoid keeps
  48 hourly, 30 daily, 8 weekly and 12 monthly snapshots; sync failures and
  daemon-down alerts go to ntfy (fsync 0.11.1). The old copy remains at
  `~/.fsync/hub.pre-tank-20261111` on files4dx until the owner deletes it. Details:
  `docs/issues-and-fixes.md`, entry 0.11.1.
- Workstations minis4dx, fury4dx, omen4dx run the Python fsync 0.11.0 (fury is behind).
- `fsync risk` on minis: 479 GB hard to replace and in no profile, including one
  450 GB file in `~/backups/june-2026`.
- External drives:

| Drive | Serial | Format | Used | Notes |
|---|---|---|---|---|
| Seagate Expansion 21.8 TB | NT17WFN7 | exFAT | empty | Factory files only |
| Seagate Expansion 12.7 TB | NT1724CJ | exFAT | 2.2 TB | Laptop backups, disk images, UrBackup VHDX (910 GB) |
| Seagate Backup+ Desk 4.5 TB | NA7EW9LP | NTFS | 2.6 TB | 2003–2021 backups, film scans (223 GB), Acronis images (301 GB); drive is ~11 years old |
| Seagate Backup+ Hub 7.3 TB | NA9RRH66 | btrfs | 3.5 TB | On minis: Proxmox dumps, a 932 GB SanDisk recovery, media |

  Overlap between the 12.7 TB and 4.5 TB drives is under 4 GB by name and size, so
  most of that content is single-copy.

### 3.1 Proxmox hosts backing up to `tank` (as built)

| Host | Guests | Target on files4dx | Schedule | Retention |
|---|---|---|---|---|
| pve9 (192.168.60.10) | 30121 GitLab VM | `tank/pve`, NFS export `/pve` | 01:00 | 7 daily, 4 weekly, 3 monthly |
| proxmox-zbook (192.168.1.30, PVE 7.4) | 100 PFSense, 101 UbuntuMailServer, 102 Portainer | `tank/pve-zbook`, NFS export `/pve-zbook` | 22:00 | 7 daily, 12 monthly |

proxmox-zbook, set up 2026-11-11:

- Its only backup target had been the USB drive `seagate7tb4zbook` (Seagate 7.3 TB,
  serial NA9RQHPH). The drive was away from the host from 2026-08-04 to 2026-11-11, so
  no backups ran in that time.
- files4dx: dataset `tank/pve-zbook` (recordsize 1M), `/etc/exports.d/zbook.exports`,
  firewalld rich rules for `nfs`, `mountd`, `rpc-bind` from 192.168.1.30 only.
- proxmox-zbook: storage `files4dx-tank` (NFS 4.2, `soft,timeo=600`), a second vzdump job
  at 22:00 with hook `/usr/local/bin/vzdump-ntfy.sh`, which posts aborted backups to the
  ntfy topic `homelab-alerts` (PVE 7 has no webhook notifications). Config backups:
  `/root/storage.cfg.bak-20261111`, `/root/jobs.cfg.bak-20261111`.
- The original 21:00 job to the USB drive is unchanged. Disable it when the drive leaves
  that host for good.
- The USB drive's whole history (57 archives back to 2025-07-31, plus a 23 GB partition
  image from 2024) is copied to
  `tank/pve-zbook/archive/usb-seagate7tb4zbook-20261111/`, outside the `dump` folder so
  Proxmox retention never prunes it.
- SSH to proxmox-zbook as root works from minis4dx and fury4dx, not from omen4dx or
  files4dx.

## 4. Architecture

```
 workstations / Windows PCs / VM hosts                 files4dx                         garage server
┌──────────────────────────────┐        ┌──────────────────────────────────┐        ┌────────────────┐
│ fsync agent (Go, service)    │ mTLS   │ fsync server (Go)                │  VPN   │ ZFS pool       │
│  scanner ── file records ────┼───────►│  inventory DB + event log        │◄───────┤ pulls snapshots│
│  uploader ─ missing content ─┼───────►│  archive store   (tank/archive)  │  pull  │ (syncoid)      │
│  sync (profiles, git, svn) ──┼───────►│  sync hub        (tank/hub)      │        └────────────────┘
│  `fsync archive <media>`     │        │  image scanner (VM disks)        │
└──────────────────────────────┘        │  planner                         │──► smart plug ──► cold USB pools
                                        │  sanoid snapshots, monthly scrub │
                                        └──────────────────────────────────┘
```

### 4.1 Agent (every computer)

One Go binary, installed as a systemd user or system service on Linux and as a Windows
service (LocalSystem) on Windows.

- **Scanner.** Walks configured roots. Records volume, path, size, modification time
  and SHA-256. A file is re-hashed only when its size or time changed. Linux roots:
  home folders and mounted external volumes. Windows roots: `C:\shared`, user profile
  folders, extra data drives; `Windows` and `Program Files` are skipped. Locked files
  on Windows are read through Volume Shadow Copy.
- **Classifier.** The rules of today's `fsync risk`: synced by a profile, replaceable
  (caches, installed software, re-clonable history), or hard to replace.
- **Uploader.** Asks the server which hashes it lacks, sends only those, resumable.
- **Sync.** Today's profile sync, git snapshots and svn snapshots, ported to Go.
- **Offline behaviour.** Scan results and events queue locally and are delivered when
  files4dx is reachable.

### 4.2 Server (files4dx)

- **Inventory database.** SQLite on the apps volume (`/var/lib/apps/fsync`), which the
  existing nightly job already copies to `tank/backup/apps` with a snapshot. A pure-Go
  SQLite driver keeps the binary static.
- **Archive store.** Dataset `tank/archive`. Each unique file is stored once under its
  hash. Every source also gets a browsable folder tree made of hard links to those
  objects, so restoring is a plain copy and duplicates cost no space.
- **Sync hub.** Moves from the boot NVMe to dataset `tank/hub`.
- **Snapshots.** sanoid policy on `tank/hub` and `tank/archive`.
- **Verification.** Every upload is re-hashed on arrival before it counts as archived.
  The monthly scrub covers bit rot afterwards.

### 4.3 Data model

| Table | Holds |
|---|---|
| `hosts` | Computers running an agent |
| `volumes` | A filesystem, identified by filesystem ID and drive serial, never by mount path; also a partition inside a disk image |
| `files` | One row per path on a volume: size, time, hash, class, first seen, last seen |
| `contents` | One row per unique hash: size, kind, archived or not, copy count |
| `copies` | Which volumes hold each content |
| `events` | Append-only log (section 4.4) |
| `plans` | Proposals from the planner and what was done with them |

Because volumes are identified by their own IDs, a USB drive moved from omen to
files4dx remains the same volume in the inventory.

### 4.4 Event log

Append-only; rows are never edited or deleted. Mirrored to a journal file on `tank`.
Event kinds: volume attached, volume detached, file discovered, file changed, file
vanished, uploaded, verified, synced, conflict resolved, extracted from image,
replicated to cold pool, replicated offsite, plan proposed, plan approved, plan
executed, restored, deleted by owner.

### 4.5 Scanning inside VM disks

Runs on files4dx only, read-only, in a container with libguestfs.

- A disk image is a file that contains volumes. The scanner opens it, walks each
  partition, and records what it finds with its origin: image, partition, path.
- Media files not already in the archive are extracted and stored.
- Running VMs are scanned from their backups (the Proxmox dumps already on `tank/pve`),
  not from live disks.
- Known limits: Acronis `.tib` is proprietary and may need Acronis to open (301 GB of
  these exist); BitLocker volumes need their keys; compressed raw images must be
  unpacked first.

### 4.6 Storage tiers

| Tier | What | Protects against |
|---|---|---|
| Online | `tank` (RAIDZ2) | Drive failure, bit rot |
| Cold | USB drives on files4dx, each its own encrypted single-disk ZFS pool, powered by a smart plug only during its replication window | Above, plus mistakes, ransomware, most pool-level failures |
| Offsite | ZFS pool on the garage server | Above, plus fire, theft, surge at home |

- **Cold replication.** files4dx switches the plug on, waits for the drive, imports the
  pool, replicates snapshots with syncoid, exports the pool, switches the plug off, and
  writes an event. A drive that fails to appear raises an ntfy alert.
- **Offsite replication is pull.** The garage server connects into the home network and
  pulls snapshots with a read-only key. files4dx holds no credentials for the garage
  server, so a compromised home network cannot delete the offsite copies.
- The offsite pool uses its own encryption key. Keys for `tank`, the cold pools and the
  offsite pool are stored together in at least two places away from those pools.

### 4.7 Planner

Compares the copy count of each content against the target and writes proposals:
content that exists only on one external drive, content missing from the cold or
offsite tier, drives whose content is fully verified in the archive and could be
reused. It proposes only. Copies it can make safely run automatically; anything that
frees or deletes space waits for a manual, confirmed command.

### 4.8 Removable media

```
fsync archive /run/media/developer/<card>
```

Run by hand on whichever computer the card is plugged into. It identifies the card,
scans it, uploads only what the archive lacks, records events, and ends with a
verdict: files new, files already archived, and whether the card is safe to wipe.
Nothing runs automatically when media is inserted.

## 5. Go migration

The existing Python code works and has 309 tests, so the port is staged to avoid ever
having less protection than today.

1. New Go module in this repository beside the Python package. The Go binary is
   installed under a different name until cutover.
2. New components are written in Go from the start: agent scanner, uploader, server,
   inventory, archive store, `archive` command.
3. Sync is ported one kind at a time: file profiles, then git snapshots, then svn
   snapshots, then hub and daemon. Each port runs in compare-only mode against the
   Python result on real profiles before it takes over.
4. The Python package is removed only after every box has run the Go sync for an
   agreed period with matching results.

The wire protocol stays HTTPS with client certificates, so Python and Go components
can coexist during the transition.

## 6. Build order

| Phase | Work | Result |
|---|---|---|
| 0 | Move the hub to `tank/hub`; sanoid policy; ntfy for fsync alerts | Existing synced files are on redundant storage with history |
| 1 | Go server with inventory and event log; Go agent scanner on Linux | Central inventory of the Linux boxes |
| 2 | Archive store, uploader, `fsync archive`; ingest the USB drives at files4dx | Old backups and removable media are in the archive |
| 3 | Windows service; scan the two Windows 10 laptops | Laptops can be verified and moved to Linux |
| 4 | Cold pools with smart-plug control; garage server with pull replication | Three tiers in place |
| 5 | VM disk scanner | Media inside VM images is found and archived |
| 6 | Planner | Proposals for under-protected content and reusable drives |
| 7 | Port sync to Go; retire Python | One codebase |
| 8 | Search page | Find any file across all computers, drives and snapshots |

Phase 0 needs no new code beyond the hub path and can be done first.

## 7. Open questions

1. Which smart-plug model and API (local or cloud)? A local API is strongly preferred
   so backups do not depend on an internet service.
2. Garage server: what hardware and which VPN (WireGuard on pfSense, Tailscale)?
3. How many Windows PCs in total, beyond the two Windows 10 laptops?
4. Should replaceable files be inventoried only, or should large VM images be archived
   whole as well as scanned?
5. Retention for sanoid snapshots on `tank/hub` and `tank/archive`.
6. Which of the 4.5 TB and 7.3 TB drives are retired after ingest, and which become
   cold pools.
