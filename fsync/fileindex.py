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
import os
import subprocess
import shutil
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
            st = target_for_stat.stat()
            size = st.st_size
            mtime = st.st_mtime
            inode = st.st_ino
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
    # Collect indices and target paths to hash
    to_hash = [(i, Path(item["_target_path"])) for i, item in enumerate(results) if item.get("_target_path")]

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
