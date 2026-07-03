"""File indexing utilities.

Provides:
- list_files_with_metadata(path, recursive=True, follow_symlinks=False, hash_algo='sha256')
  returns a list of dicts: { 'path': relpath, 'size': int, 'mtime': float, 'hash': hexstr }

- compare_file_lists(list_a, list_b)
  compares two lists (as returned above) and returns dict with matches and duplicates

The functions are written to be easy to test and use standard libraries only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import shutil
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, List, Dict, Any, Tuple


def _find_b3sum(b3sum_path: str | None = None) -> str | None:
    # If explicit path provided and executable, use it
    if b3sum_path:
        p = shutil.which(b3sum_path) if not os.path.isabs(b3sum_path) else b3sum_path
        if p and os.access(p, os.X_OK):
            return p
    # Try common names
    for name in ("b3sum", "blake3sum", "blake3", "b3sum.exe", "blake3sum.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _compute_hash(path: Path, algo: str = "sha256", chunk_size: int = 8192, b3sum_path: str | None = None) -> str:
    # Special-case external b3sum utility for blake3 when requested
    if algo in ("b3sum", "blake3"):
        exe = _find_b3sum(b3sum_path)
        if exe:
            try:
                proc = subprocess.run([exe, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                out = proc.stdout.strip()
                if out:
                    token = out.split()[0]
                    if all(c in "0123456789abcdefABCDEF" for c in token):
                        return token.lower()
            except FileNotFoundError:
                pass
        # fall back to sha256 if external tool unavailable
        algo = "sha256"

    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def matches_exclude(rel_path: str, patterns: Iterable[str]) -> bool:
    """True when ``rel_path`` (posix, relative to the indexed root) is excluded.

    Pattern semantics (fnmatch-based, kept deliberately simple):
    - a pattern containing ``/`` matches against the whole relative path
      (``backups/*`` excludes everything under ``backups/``);
    - a pattern without ``/`` matches against any single path component
      (``*.tmp`` excludes such files anywhere; ``.git`` excludes whole trees).
    """
    parts = rel_path.split("/")
    for pat in patterns:
        if "/" in pat:
            if fnmatch(rel_path, pat):
                return True
        elif any(fnmatch(part, pat) for part in parts):
            return True
    return False


def default_cache_path(root: str | Path) -> Path:
    """Per-root hash-cache location under ``~/.cache/fsync/hashcache/``."""
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]
    return Path.home() / ".cache" / "fsync" / "hashcache" / f"{key}.json"


def _load_hash_cache(cache_path: str | Path, algo: str) -> Dict[str, list]:
    """Load ``{rel_path: [size, mtime, hash]}``; empty on miss/corruption/algo change."""
    try:
        data = json.loads(Path(cache_path).read_text())
        if data.get("algo") != algo:
            return {}
        files = data.get("files")
        return files if isinstance(files, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_hash_cache(cache_path: str | Path, algo: str, merged: Dict[str, list]) -> None:
    """Atomically persist the cache; failures are non-fatal (cache is advisory)."""
    try:
        p = Path(cache_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"algo": algo, "files": merged}))
        os.replace(tmp, p)
    except OSError:
        pass


def load_index_file(path: str | Path) -> List[Dict[str, Any]]:
    """Load a saved ``fsync index`` output (JSON array or JSONL) into records."""
    text = Path(path).read_text()
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected a JSON array of file records")
        return data
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def list_files_with_metadata(
    root: str | Path,
    recursive: bool = True,
    follow_symlinks: bool = False,
    hash_algo: str = "sha256",
    fields: Iterable[str] | None = None,
    workers: int = 1,
    show_progress: bool = False,
    logger: Any | None = None,
    b3sum_path: str | None = None,
    exclude: Iterable[str] | None = None,
    cache_path: str | Path | None = None,
) -> List[Dict[str, Any]]:
    """List files under `root` and return metadata including checksum.

    Each item in the returned list is a dict with keys:
    - name: filename only (not path)
    - path: relative path from `root` as posix string
    - size: file size in bytes
    - mtime: modification time (float, seconds since epoch)
    - hash: hex digest of the file contents using `hash_algo`
    - inode: os inode number
    - atime: last access time
    - ctime: creation/change time
    - uid: owner user id
    - gid: owner group id
    - mode: file mode (permissions)
    - is_symlink: whether the entry was a symbolic link (True/False)

    Args:
        root: directory to index
        recursive: walk subdirectories when True
        follow_symlinks: whether to follow symbolic links
        hash_algo: hashing algorithm supported by hashlib (default: sha256)

    Returns:
        List of metadata dictionaries.
    """
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"Root path not found: {root}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {root}")

    results: List[Dict[str, Any]] = []
    if recursive:
        walker = root_path.rglob("*")
    else:
        walker = root_path.iterdir()

    # Optionally wrap the walker with tqdm to show progress
    use_tqdm = show_progress
    tqdm = None
    if use_tqdm:
        try:
            from tqdm import tqdm as _tqdm

            tqdm = _tqdm
        except Exception:
            tqdm = None
            if logger:
                logger.warning("tqdm not available; --progress ignored")

    iterable = list(walker)
    if tqdm:
        iterable = tqdm(iterable, desc="scanning files")

    exclude_pats = list(exclude) if exclude else None

    for p in iterable:
        try:
            # Only include regular files. If follow_symlinks is True, also include symlinks that
            # resolve to regular files.
            if p.is_file():
                is_symlink = False
                target_for_stat = p
            elif follow_symlinks and p.is_symlink():
                # resolve target; if it is a file include it
                try:
                    resolved = p.resolve(strict=True)
                except OSError:
                    # broken symlink or cannot resolve -> skip
                    continue
                if not resolved.is_file():
                    continue
                is_symlink = True
                target_for_stat = resolved
            else:
                continue

            rel = p.relative_to(root_path).as_posix()
            if exclude_pats and matches_exclude(rel, exclude_pats):
                continue
            st = target_for_stat.stat()
            size = st.st_size
            mtime = st.st_mtime
            inode = st.st_ino
            dev = st.st_dev
            nlink = st.st_nlink
            atime = st.st_atime
            ctime = st.st_ctime
            uid = st.st_uid
            gid = st.st_gid
            mode = st.st_mode
            # Defer hash computation to a later phase so we can parallelize it.
            item = {
                "name": p.name,
                "path": rel,
                "size": size,
                "mtime": mtime,
                "hash": None,
                "inode": inode,
                "dev": dev,
                "nlink": nlink,
                "atime": atime,
                "ctime": ctime,
                "uid": uid,
                "gid": gid,
                "mode": mode,
                "is_symlink": is_symlink,
                "_target_path": str(target_for_stat),
            }
            results.append(item)
            if logger and logger.level <= 10:
                logger.debug(f"Indexed {rel} (size={size})")
        except PermissionError:
            # skip files we can't read
            continue
    # Compute hashes (possibly in parallel) over the collected results.
    # To avoid re-reading the same physical file many times (e.g. UrBackup-style
    # hardlink farms), hash only one representative per (st_dev, st_ino) group and
    # copy the digest to its hardlink siblings afterwards. Only files with more
    # than one link are grouped, so filesystems that report a constant/zero inode
    # for distinct files (no real hardlink info) still hash each file individually.
    to_hash: List[Tuple[int, Path]] = []
    seen_group: Dict[Tuple[Any, Any], int] = {}
    cache = _load_hash_cache(cache_path, hash_algo) if cache_path else None
    for i, item in enumerate(results):
        if not item.get("_target_path"):
            continue
        ino = item.get("inode")
        nlink = item.get("nlink") or 1
        group_key = (item.get("dev"), ino) if (nlink > 1 and ino) else None
        if group_key is not None and group_key in seen_group:
            # Sibling hardlink: reuse the representative's hash later.
            item["_hash_from"] = seen_group[group_key]
            continue
        if group_key is not None:
            seen_group[group_key] = i
        if cache is not None:
            hit = cache.get(item["path"])
            # unchanged (size, mtime) => trust the cached digest, skip the read
            if hit and hit[0] == item["size"] and hit[1] == item["mtime"] and hit[2]:
                item["hash"] = hit[2]
                continue
        to_hash.append((i, Path(item["_target_path"])))

    if to_hash:
        if workers is None or workers <= 1:
            # sequential
            for idx, pth in to_hash:
                try:
                    results[idx]["hash"] = _compute_hash(pth, algo=hash_algo, b3sum_path=b3sum_path)
                except PermissionError:
                    results[idx]["hash"] = None
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=workers) as exe:
                future_to_idx = {exe.submit(_compute_hash, pth, hash_algo, b3sum_path): idx for idx, pth in to_hash}
                if show_progress and tqdm:
                    futures = list(future_to_idx.keys())
                    for fut in tqdm(as_completed(futures), total=len(futures), desc="hashing"):
                        idx = future_to_idx[fut]
                        try:
                            results[idx]["hash"] = fut.result()
                        except Exception:
                            results[idx]["hash"] = None
                else:
                    for fut in as_completed(future_to_idx):
                        idx = future_to_idx[fut]
                        try:
                            results[idx]["hash"] = fut.result()
                        except Exception:
                            results[idx]["hash"] = None

    # Copy each representative's hash onto its hardlink siblings.
    for item in results:
        rep = item.pop("_hash_from", None)
        if rep is not None:
            item["hash"] = results[rep].get("hash")

    if cache_path:
        # Merge (not replace) so records outside this run's exclude set survive.
        merged = cache if cache is not None else {}
        for item in results:
            if item.get("hash"):
                merged[item["path"]] = [item["size"], item["mtime"], item["hash"]]
        _save_hash_cache(cache_path, hash_algo, merged)

    # Remove internal _target_path and apply fields filtering
    final: List[Dict[str, Any]] = []
    for item in results:
        item.pop("_target_path", None)
        if fields is not None:
            filtered = {k: item[k] for k in fields if k in item}
            final.append(filtered)
        else:
            final.append(item)

    return final


def iter_index_records(
    root: str | Path,
    recursive: bool = True,
    follow_symlinks: bool = False,
    hash_algo: str = "sha256",
    workers: int = 1,
    b3sum_path: str | None = None,
    chunk_size: int = 5000,
    logger: Any | None = None,
    exclude: Iterable[str] | None = None,
):
    """Yield file metadata records one at a time, hashing in bounded-memory chunks.

    Unlike :func:`list_files_with_metadata` (which materialises the whole tree),
    this walks lazily and hashes files in chunks of ``chunk_size`` (in parallel
    within a chunk), yielding finalised records as it goes. Hardlinks are
    deduplicated globally by ``(st_dev, st_ino)`` through a small persistent
    cache, so each physical file is read at most once even across chunks.

    Memory stays bounded to roughly one chunk plus the inode cache, which is
    what lets it index multi-million-file trees (e.g. UrBackup stores) without
    being OOM-killed.
    """
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"Root path not found: {root}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {root}")

    from concurrent.futures import ThreadPoolExecutor

    inode_hash: Dict[Tuple[Any, Any], str] = {}

    def finalize(pending: List[Tuple[Dict[str, Any], Path, Any]]) -> List[Dict[str, Any]]:
        # Decide which entries actually need hashing: skip ones whose (dev,ino)
        # is already known (cached from a prior chunk) or shared within this chunk.
        seen_local: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
        need: List[Tuple[Dict[str, Any], Path, Any]] = []
        siblings: List[Tuple[Dict[str, Any], Any]] = []
        for rec, tgt, gk in pending:
            if gk is None:
                need.append((rec, tgt, gk))
            elif gk in inode_hash:
                rec["hash"] = inode_hash[gk]
            elif gk in seen_local:
                siblings.append((rec, gk))
            else:
                seen_local[gk] = rec
                need.append((rec, tgt, gk))

        def do(item: Tuple[Dict[str, Any], Path, Any]) -> None:
            rec, tgt, gk = item
            try:
                h = _compute_hash(tgt, algo=hash_algo, b3sum_path=b3sum_path)
            except Exception:
                h = None
            rec["hash"] = h
            if gk is not None and h is not None:
                inode_hash[gk] = h

        if workers and workers > 1 and len(need) > 1:
            with ThreadPoolExecutor(max_workers=workers) as exe:
                list(exe.map(do, need))
        else:
            for item in need:
                do(item)

        # Fill same-chunk hardlink siblings from their now-hashed representative.
        for rec, gk in siblings:
            rec["hash"] = inode_hash.get(gk)

        return [rec for (rec, _, _) in pending]

    walker = root_path.rglob("*") if recursive else root_path.iterdir()
    exclude_pats = list(exclude) if exclude else None
    pending: List[Tuple[Dict[str, Any], Path, Any]] = []
    for p in walker:
        try:
            if p.is_file():
                is_symlink = False
                target_for_stat = p
            elif follow_symlinks and p.is_symlink():
                try:
                    resolved = p.resolve(strict=True)
                except OSError:
                    continue
                if not resolved.is_file():
                    continue
                is_symlink = True
                target_for_stat = resolved
            else:
                continue

            rel = p.relative_to(root_path).as_posix()
            if exclude_pats and matches_exclude(rel, exclude_pats):
                continue
            st = target_for_stat.stat()
            rec = {
                "name": p.name,
                "path": rel,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "hash": None,
                "inode": st.st_ino,
                "dev": st.st_dev,
                "nlink": st.st_nlink,
                "atime": st.st_atime,
                "ctime": st.st_ctime,
                "uid": st.st_uid,
                "gid": st.st_gid,
                "mode": st.st_mode,
                "is_symlink": is_symlink,
            }
            gk = (st.st_dev, st.st_ino) if (st.st_nlink > 1 and st.st_ino) else None
            pending.append((rec, target_for_stat, gk))
            if len(pending) >= chunk_size:
                for r in finalize(pending):
                    yield r
                pending = []
        except PermissionError:
            continue
    if pending:
        for r in finalize(pending):
            yield r


def compare_file_lists(
    list_a: Iterable[Dict[str, Any]],
    list_b: Iterable[Dict[str, Any]],
    match_on: str = "path",
) -> Dict[str, Any]:
    """Compare two file metadata lists.

    Returns a dict with keys:
    - exact_matches: list of tuples (a_item, b_item) where name and hash match
    - name_matches_diff_hash: list of tuples (a_item, b_item) where name matches but hash differs
    - hash_matches_diff_name: list of tuples (a_item, b_item) where hash matches but name differs
    - only_in_a: list of a_items not matched
    - only_in_b: list of b_items not matched

    Matching is performed one-to-one and removes matched items from consideration.
    """
    a_list = list(list_a)
    b_list = list(list_b)

    # Which key to use for name-based matching: 'path' or 'name'
    if match_on not in ("path", "name"):
        raise ValueError("match_on must be 'path' or 'name'")

    # Index by (match_key->list of items) and (hash->list of items)
    from collections import defaultdict
    name_index_b = defaultdict(list)
    hash_index_b = defaultdict(list)
    for idx, item in enumerate(b_list):
        key = item.get(match_on) if match_on in item else item.get("name")
        name_index_b[key].append(idx)
        hash_index_b[item["hash"]].append(idx)

    exact_matches: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    name_matches_diff_hash: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    hash_matches_diff_name: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    matched_b = set()  # set of indices in b_list

    # First pass: find exact matches (name+hash)
    for a in a_list:
        key = a.get(match_on) if match_on in a else a.get("name")
        candidates = name_index_b.get(key, [])
        found_idx = None
        for b_idx in candidates:
            if b_idx in matched_b:
                continue
            b = b_list[b_idx]
            if b["hash"] == a["hash"]:
                found_idx = b_idx
                break
        if found_idx is not None:
            exact_matches.append((a, b_list[found_idx]))
            matched_b.add(found_idx)

    # Second pass: name matches but different hash
    for a in a_list:
        # skip already matched
        if any(a is pair[0] for pair in exact_matches):
            continue
        key = a.get(match_on) if match_on in a else a.get("name")
        candidates = name_index_b.get(key, [])
        found_idx = None
        for b_idx in candidates:
            if b_idx in matched_b:
                continue
            b = b_list[b_idx]
            # different hash
            if b["hash"] != a["hash"]:
                found_idx = b_idx
                break
        if found_idx is not None:
            name_matches_diff_hash.append((a, b_list[found_idx]))
            matched_b.add(found_idx)

    # Third pass: same hash but different name
    for a in a_list:
        if any(a is pair[0] for pair in exact_matches) or any(a is pair[0] for pair in name_matches_diff_hash):
            continue
        candidates = hash_index_b.get(a["hash"], [])
        found_idx = None
        for b_idx in candidates:
            if b_idx in matched_b:
                continue
            b = b_list[b_idx]
            if b["name"] != a["name"]:
                found_idx = b_idx
                break
        if found_idx is not None:
            hash_matches_diff_name.append((a, b_list[found_idx]))
            matched_b.add(found_idx)

    # Collect unmatched
    only_in_a = []
    for a in a_list:
        if any(a is pair[0] for pair in exact_matches) or any(a is pair[0] for pair in name_matches_diff_hash) or any(a is pair[0] for pair in hash_matches_diff_name):
            continue
        only_in_a.append(a)

    only_in_b = [b for idx, b in enumerate(b_list) if idx not in matched_b]

    return {
        "exact_matches": exact_matches,
        "name_matches_diff_hash": name_matches_diff_hash,
        "hash_matches_diff_name": hash_matches_diff_name,
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
    }


# ---------------------------------------------------------------------------
# Sync planning: turn a compare report into rsync inputs.
#
# fsync itself never copies a byte; it computes the diff and projects it onto
# rsync. The plan is hash-based (from ``compare_file_lists``), so it transfers
# only genuinely-different files and treats content-identical-but-renamed files
# as renames rather than re-copies.
# ---------------------------------------------------------------------------

DEFAULT_RSYNC_FLAGS: Tuple[str, ...] = (
    "-aHAXS",
    "--numeric-ids",
    # The plan is already hash-authoritative (fsync decided these files differ),
    # so force rsync to transfer exactly the listed files instead of re-deciding
    # by size+mtime — otherwise a same-size/same-mtime-but-different-content file
    # would be silently skipped, which is the whole hazard we are guarding against.
    "--ignore-times",
    "--info=progress2",
    "--partial",
)
# AES-NI cipher, no double-compression; fast on a Thunderbolt/USB4 link.
DEFAULT_RSYNC_SSH = "ssh -T -c aes128-gcm@openssh.com -o Compression=no -x"

CONFLICT_POLICIES = ("review", "newer", "a-wins", "b-wins")
MIRROR_MODES = (None, "a-to-b", "b-to-a")


def _pair(entry: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Normalise a paired compare entry to ``(a, b)``.

    Tolerates both the in-memory tuple form returned by
    :func:`compare_file_lists` and the 2-element list form produced when a
    report is round-tripped through JSON (``compare --output``).
    """
    return entry[0], entry[1]


def build_sync_plan(
    report: Dict[str, Any],
    conflict: str = "review",
    mirror: str | None = None,
    rename_min_size: int = 0,
) -> Dict[str, Any]:
    """Project a :func:`compare_file_lists` report onto a directional sync plan.

    ``report`` is the dict returned by :func:`compare_file_lists`, or the same
    structure loaded back from a ``compare --output`` JSON file (paired buckets
    may be tuples or 2-element lists).

    Returns a plan dict:

    - ``a_to_b``  – file items to copy from A to B
    - ``b_to_a``  – file items to copy from B to A
    - ``a_delete``– items to delete on A (only for ``mirror='b-to-a'``)
    - ``b_delete``– items to delete on B (only for ``mirror='a-to-b'``)
    - ``conflicts``– list of ``(a_item, b_item)`` left for a human to decide
      (only with ``conflict='review'`` or an undecidable ``newer`` tie)
    - ``renames`` – list of ``(a_item, b_item)`` that are content-identical
      under a different name (a rename on the receiver, not a copy)
    - ``noop``    – count of byte-identical files (exact matches)

    ``conflict`` routes same-path/different-content files
    (``name_matches_diff_hash``): ``review`` (default; leave for a human),
    ``newer`` (route by mtime), ``a-wins`` or ``b-wins``.

    ``mirror`` makes the plan one-way and is the only mode that proposes
    deletions: ``a-to-b`` makes B match A (A wins all conflicts, B's extras are
    queued for deletion); ``b-to-a`` is the reverse. ``conflict`` is ignored
    when ``mirror`` is set.

    Rename pairs are only trustworthy when the shared hash is unique to that
    pair: content that occurs at more than two paths (empty lock files,
    boilerplate blobs) pairs *unrelated* files, and acting on such a "rename"
    moves live files around on the receiver. Those pairs — plus, when
    ``rename_min_size`` > 0, pairs below that size — are demoted to plain
    copies (or copy+delete under mirror) and reported in
    ``renames_suppressed``.
    """
    if conflict not in CONFLICT_POLICIES:
        raise ValueError(f"conflict must be one of {CONFLICT_POLICIES}")
    if mirror not in MIRROR_MODES:
        raise ValueError(f"mirror must be one of {MIRROR_MODES}")

    only_in_a = list(report.get("only_in_a", []))
    only_in_b = list(report.get("only_in_b", []))
    changed = [_pair(e) for e in report.get("name_matches_diff_hash", [])]
    renames_all = [_pair(e) for e in report.get("hash_matches_diff_name", [])]
    noop = len(report.get("exact_matches", []))

    renames: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    suppressed: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    if renames_all:
        from collections import Counter

        occurrences: Counter = Counter()
        for e in report.get("exact_matches", []):
            a, b = _pair(e)
            occurrences[a.get("hash")] += 2
        for a, b in changed:
            occurrences[a.get("hash")] += 1
            occurrences[b.get("hash")] += 1
        for it in only_in_a + only_in_b:
            occurrences[it.get("hash")] += 1
        for a, b in renames_all:
            occurrences[a.get("hash")] += 2
        for a, b in renames_all:
            h = a.get("hash")
            size = a.get("size")
            too_small = size is not None and size < rename_min_size
            if h is None or occurrences[h] > 2 or too_small:
                suppressed.append((a, b))
            else:
                renames.append((a, b))

    a_to_b: List[Dict[str, Any]] = []
    b_to_a: List[Dict[str, Any]] = []
    a_delete: List[Dict[str, Any]] = []
    b_delete: List[Dict[str, Any]] = []
    conflicts: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

    # Demoted rename pairs travel as ordinary content: under mirror the winning
    # side's path is copied and the loser's old name deleted; under union both
    # sides simply receive the other's path (content already identical).
    if mirror == "a-to-b":
        a_to_b.extend(only_in_a)
        a_to_b.extend(a for a, _ in changed)  # A wins every conflict
        a_to_b.extend(a for a, _ in suppressed)
        b_delete.extend(only_in_b)
        b_delete.extend(b for _, b in suppressed)
    elif mirror == "b-to-a":
        b_to_a.extend(only_in_b)
        b_to_a.extend(b for _, b in changed)  # B wins every conflict
        b_to_a.extend(b for _, b in suppressed)
        a_delete.extend(only_in_a)
        a_delete.extend(a for a, _ in suppressed)
    else:
        # Bidirectional union: each side's unique files flow to the other.
        a_to_b.extend(only_in_a)
        b_to_a.extend(only_in_b)
        a_to_b.extend(a for a, _ in suppressed)
        b_to_a.extend(b for _, b in suppressed)
        for a, b in changed:
            if conflict == "a-wins":
                a_to_b.append(a)
            elif conflict == "b-wins":
                b_to_a.append(b)
            elif conflict == "newer":
                am, bm = a.get("mtime"), b.get("mtime")
                if am is None or bm is None or am == bm:
                    conflicts.append((a, b))  # cannot decide -> hand to human
                elif am > bm:
                    a_to_b.append(a)
                else:
                    b_to_a.append(b)
            else:  # review
                conflicts.append((a, b))

    return {
        "a_to_b": a_to_b,
        "b_to_a": b_to_a,
        "a_delete": a_delete,
        "b_delete": b_delete,
        "conflicts": conflicts,
        "renames": renames,
        "renames_suppressed": suppressed,
        "noop": noop,
    }


def _with_slash(p: str) -> str:
    return p if p.endswith("/") else p + "/"


def _is_remote(endpoint: str) -> bool:
    """True if ``endpoint`` is an rsync remote ``[user@]host:path`` spec.

    A leading ``/``, ``./`` or ``../`` is always local; otherwise a colon that
    appears before the first slash marks a remote host spec.
    """
    if endpoint.startswith(("/", "./", "../")):
        return False
    head = endpoint.split("/", 1)[0]
    return ":" in head


def split_remote(endpoint: str) -> Tuple[str | None, str]:
    """Split an rsync endpoint into ``(host_or_None, path)``.

    ``developer@host:/home/developer`` -> ``("developer@host", "/home/developer")``;
    a local path returns ``(None, path)``.
    """
    if not _is_remote(endpoint):
        return None, endpoint
    host, _, path = endpoint.partition(":")
    return host, path


def render_rsync_command(
    files_from: str,
    src: str,
    dest: str,
    dry_run: bool = True,
    ssh: str | None = DEFAULT_RSYNC_SSH,
    flags: Iterable[str] | None = None,
) -> str:
    """Render one guarded rsync command line driven by a ``--files-from`` list.

    The listed paths are relative to ``src``'s root; ``-e ssh`` is added only
    when either endpoint is remote. ``--files-from`` makes rsync transfer only
    the named files (creating their parent dirs), so no whole-tree ``--delete``
    walk happens here.
    """
    parts: List[str] = ["rsync", *(list(flags) if flags is not None else list(DEFAULT_RSYNC_FLAGS))]
    if dry_run:
        parts.append("-n")
    if ssh and (_is_remote(src) or _is_remote(dest)):
        parts.append(f"-e {shlex.quote(ssh)}")
    parts.append(f"--files-from={shlex.quote(files_from)}")
    parts.append(shlex.quote(_with_slash(src)))
    parts.append(shlex.quote(_with_slash(dest)))
    return " ".join(parts)


def render_delete_block(list_file: str, target: str) -> str:
    """Render a guarded shell block that deletes the paths in ``list_file`` on ``target``.

    Works for a local path or a remote ``host:path`` endpoint. The caller is
    expected to keep this commented out by default — deletions are the
    destructive part of a mirror.
    """
    host, base = split_remote(target)
    base = base.rstrip("/")
    lf = shlex.quote(list_file)
    if host is None:
        # Bind base to a quoted shell var so a path containing $, backticks,
        # quotes, etc. cannot be re-interpreted (or break out of) the command.
        return (
            f"base={shlex.quote(base)}; "
            f'while IFS= read -r f; do rm -vf -- "$base/$f"; done < {lf}'
        )
    remote = f'cd {shlex.quote(base)} && while IFS= read -r f; do rm -vf -- "$f"; done'
    return f"ssh {shlex.quote(host)} {shlex.quote(remote)} < {lf}"
