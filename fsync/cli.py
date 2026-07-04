"""Command-line interface for fsync.fileindex.

Usage (module):
  python -m fsync.cli index <dir> [--recursive] [--follow-symlinks] [--hash sha256]
  python -m fsync.cli compare <dirA> <dirB> [--recursive] [--hash sha256]

The `index` command writes JSON metadata to stdout. The `compare` command writes a JSON
report to stdout with keys: exact_matches, name_matches_diff_hash, hash_matches_diff_name,
only_in_a, only_in_b.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
from pathlib import Path
from typing import Any

from .fileindex import (
    list_files_with_metadata,
    compare_file_lists,
    iter_index_records,
    build_sync_plan,
    render_rsync_command,
    render_delete_block,
    split_remote,
    load_index_file,
    matches_exclude,
    default_cache_path,
    DEFAULT_RSYNC_SSH,
)


def _load_side(
    spec: str,
    *,
    recursive: bool,
    hash_algo: str,
    fields=None,
    workers: int = 1,
    show_progress: bool = False,
    exclude=None,
    b3sum_path=None,
    logger=None,
) -> list[dict]:
    """Resolve one compare/sync-plan side: scan a directory, or load a saved
    index file (JSON/JSONL) when ``spec`` points at a regular file.

    Index-file input is what lets each box hash locally and exchange only
    metadata — excludes are re-applied to loaded records so both input kinds
    honor the same filter.
    """
    p = Path(spec)
    if p.is_file():
        records = load_index_file(p)
        if exclude:
            records = [r for r in records if not matches_exclude(r.get("path", ""), exclude)]
        return records
    return list_files_with_metadata(
        p,
        recursive=recursive,
        hash_algo=hash_algo,
        fields=fields,
        workers=workers,
        show_progress=show_progress,
        exclude=exclude,
        b3sum_path=b3sum_path,
        logger=logger,
    )


def cmd_index(args: argparse.Namespace) -> int:
    fields = None
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]

    # Storing to a sink (DB or catalog API) uses the streaming path so memory
    # stays bounded even for multi-million-file trees (the in-memory list
    # approach gets OOM-killed).
    if getattr(args, "store_db", False) or getattr(args, "catalog_url", None):
        return _cmd_index_streaming(args, fields)

    data = list_files_with_metadata(
        Path(args.dir),
        recursive=args.recursive,
        follow_symlinks=args.follow_symlinks,
        hash_algo=args.hash,
        fields=fields,
        workers=args.workers,
        show_progress=args.progress,
        logger=getattr(args, "logger", None),
        exclude=getattr(args, "exclude", None),
        cache_path=default_cache_path(args.dir) if getattr(args, "cache", False) else None,
    )
    if args.format == "jsonl":
        # stream JSON lines
        lines = "\n".join(json.dumps(rec) for rec in data)
        out = lines
    else:
        out = json.dumps(data, indent=2)
    if args.output:
        Path(args.output).write_text(out)
    else:
        sys.stdout.write(out + "\n")
    return 0


def _cmd_index_streaming(args: argparse.Namespace, fields) -> int:
    """Index a directory straight into Postgres in bounded-memory batches.

    Optionally emits scan.* progress events to a dream4events server when
    --events-url is given (telemetry never blocks the scan; emit failures are
    swallowed).
    """
    db_url = args.db_url or os.environ.get("DB_URL")
    catalog_url = getattr(args, "catalog_url", None) or os.environ.get("FSYNC_CATALOG_URL")
    if not db_url and not catalog_url:
        print("No catalog sink: provide --catalog-url (preferred) or --db-url / DB_URL", file=sys.stderr)
        return 2

    source = args.source or str(Path(args.dir).resolve())
    logger = getattr(args, "logger", None)

    # Choose the catalog sink: HTTP catalog API (scanner holds no DB creds) or direct DB.
    catalog_client = None
    if catalog_url:
        from .catalog_client import CatalogClient

        catalog_client = CatalogClient(catalog_url, source)

        def store_batch(recs):
            catalog_client.post_batch(recs)
    else:
        from .db import store_index

        def store_batch(recs):
            store_index(db_url, recs, source=source)

    # Optional event telemetry.
    from .eventbus import FsyncEvents

    events_url = getattr(args, "events_url", None) or os.environ.get("FSYNC_EVENTS_URL")
    scanner_id = getattr(args, "scanner_id", None) or os.environ.get("FSYNC_SCANNER_ID") or socket.gethostname()
    run_id = getattr(args, "run_id", None)
    if events_url and not run_id:
        from dream4devops.events import ulid_new

        run_id = ulid_new()

    batch_size = 5000
    out_f = open(args.output, "w") if args.output else None
    batch: list[dict] = []
    total = 0
    bytes_done = 0
    with FsyncEvents(events_url, scanner_id, logger=logger) as events:
        events.scan_started(run_id, source, dir=str(Path(args.dir)), hash=args.hash)
        try:
            for rec in iter_index_records(
                Path(args.dir),
                recursive=args.recursive,
                follow_symlinks=args.follow_symlinks,
                hash_algo=args.hash,
                workers=args.workers,
                b3sum_path=getattr(args, "b3sum_path", None),
                chunk_size=batch_size,
                logger=logger,
                exclude=getattr(args, "exclude", None),
            ):
                if out_f is not None:
                    out_f.write(json.dumps(rec) + "\n")
                bytes_done += int(rec.get("size") or 0)
                batch.append({k: rec[k] for k in fields if k in rec} if fields else rec)
                if len(batch) >= batch_size:
                    store_batch(batch)
                    total += len(batch)
                    batch = []
                    print(f"stored {total} records...", file=sys.stderr)
                    events.scan_progress(run_id, total, bytes_done=bytes_done)
            if batch:
                store_batch(batch)
                total += len(batch)
            events.scan_completed(run_id, source=source, files=total, bytes=bytes_done)
        except Exception as e:
            events.scan_failed(run_id, e)
            raise
        finally:
            if out_f is not None:
                out_f.close()
            if catalog_client is not None:
                catalog_client.close()
    print(f"done: stored {total} records to source '{source}'", file=sys.stderr)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    fields = None
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]

    a = _load_side(
        args.dirA, recursive=args.recursive, hash_algo=args.hash, fields=fields,
        workers=args.workers, show_progress=args.progress,
        exclude=getattr(args, "exclude", None), logger=getattr(args, "logger", None),
    )
    b = _load_side(
        args.dirB, recursive=args.recursive, hash_algo=args.hash, fields=fields,
        workers=args.workers, show_progress=args.progress,
        exclude=getattr(args, "exclude", None), logger=getattr(args, "logger", None),
    )
    report = compare_file_lists(a, b, match_on=args.match_on)
    if args.format == "pretty":
        # concise human readable summary with optional samples
        parts = []
        parts.append(f"exact_matches: {len(report['exact_matches'])}")
        parts.append(f"name_matches_diff_hash: {len(report['name_matches_diff_hash'])}")
        parts.append(f"hash_matches_diff_name: {len(report['hash_matches_diff_name'])}")
        parts.append(f"only_in_a: {len(report['only_in_a'])}")
        parts.append(f"only_in_b: {len(report['only_in_b'])}")
        if args.show and args.show > 0:
            def sample_pairs(lst, label):
                s = []
                for i, pair in enumerate(lst[: args.show]):
                    a_item = pair[0]
                    b_item = pair[1]
                    s.append(f"  {label} {i+1}: {a_item.get('path', a_item.get('name'))} <-> {b_item.get('path', b_item.get('name'))}")
                return s

            parts.append("")
            parts.append("Samples:")
            parts.extend(sample_pairs(report['exact_matches'], 'exact'))
            parts.extend(sample_pairs(report['name_matches_diff_hash'], 'name_diff'))
            parts.extend(sample_pairs(report['hash_matches_diff_name'], 'hash_diff'))
        out = "\n".join(parts)
    else:
        out = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(out)
    else:
        sys.stdout.write(out + "\n")
    # Optionally store in DB
    if getattr(args, "store_db", False):
        db_url = args.db_url or os.environ.get("DB_URL")
        if not db_url:
            print("DB URL not provided (use --db-url or set DB_URL)", file=sys.stderr)
            return 2
        from .db import store_index

        source_a = args.source_a or str(Path(args.dirA).resolve())
        source_b = args.source_b or str(Path(args.dirB).resolve())
        store_index(db_url, a, source=source_a)
        store_index(db_url, b, source=source_b)
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from pathlib import Path
    import time

    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    # create files
    for i in range(args.count):
        (d / f"f{i}.bin").write_bytes(b"x" * args.size)

    start = time.time()
    # use list_files_with_metadata to compute hashes
    from .fileindex import list_files_with_metadata

    list_files_with_metadata(d, recursive=True, hash_algo=args.hash, workers=args.workers, show_progress=args.progress, b3sum_path=getattr(args, "b3sum_path", None))
    elapsed = time.time() - start
    print(f"Hashed {args.count} files of {args.size} bytes in {elapsed:.2f}s ({args.count/elapsed:.2f} files/s)")
    return 0


def cmd_ctl(args: argparse.Namespace) -> int:
    """Emit a command event addressed to a scanner (instance = target id)."""
    events_url = args.events_url or os.environ.get("FSYNC_EVENTS_URL")
    if not events_url:
        print("ctl requires --events-url (or FSYNC_EVENTS_URL)", file=sys.stderr)
        return 2
    from .eventbus import FsyncEvents, EV_COMMAND
    from dream4devops.events import ulid_new

    command_id = ulid_new()
    body = {"command": args.command, "command_id": command_id}
    if args.command == "start_scan":
        if not args.dir:
            print("start_scan requires --dir", file=sys.stderr)
            return 2
        body["args"] = {
            "dir": args.dir,
            "source": args.source or args.dir,
            "workers": args.workers,
            "hash": args.hash,
        }
    with FsyncEvents(events_url, args.scanner_id, source="fsync-ctl") as ev:
        eid = ev.emit(EV_COMMAND, body, correlation_id=command_id)
    print(f"sent {args.command} to {args.scanner_id} (command_id={command_id}, event={eid})")
    return 0


def _plan_paths(items: list[dict]) -> tuple[list[str], list]:
    """Extract clean relative paths for an rsync --files-from list.

    Returns ``(usable_paths, skipped_items)``. An entry is skipped when it has no
    ``path`` or the path contains an embedded newline (which would corrupt the
    newline-delimited list file). Callers must surface ``skipped`` — this tool
    must never drop user data silently.
    """
    out: list[str] = []
    skipped: list = []
    for it in items:
        p = it.get("path")
        if not p or "\n" in p or "\r" in p:
            skipped.append(it)
            continue
        out.append(p)
    return out, skipped


def _render_renames_script(pairs: list, target: str, old_idx: int, new_idx: int,
                           required: bool = False) -> str:
    """Render a script that renames content-identical files on ``target``.

    Each pair is ``(a_item, b_item)`` whose bytes are identical under a different
    name. ``old_idx``/``new_idx`` (0=A, 1=B) pick which side's path is the
    *existing* name on ``target`` and which is the *desired* name — so for
    ``--mirror a-to-b`` we rename on B (B's name -> A's name) and for
    ``b-to-a`` we rename on A (A's name -> B's name). Renaming avoids re-copying
    the bytes. ``required`` only changes the wording (mirror needs it to
    converge; union treats it as optional reconciliation).
    """
    host, base = split_remote(target)
    base = base.rstrip("/")
    intro = ("# Rename content-identical files so the two sides' names converge."
             if required else
             "# OPTIONAL: rename content-identical files to reconcile differing names.")
    lines = [
        "#!/usr/bin/env bash",
        intro,
        "# These bytes already exist under a different name; this avoids a re-copy.",
        "set -euo pipefail",
        "",
    ]
    dropped = 0
    for pair in pairs:
        old = pair[old_idx].get("path")
        new = pair[new_idx].get("path")
        if not old or not new:
            dropped += 1  # cannot place this rename -> surface it, never silent
            continue
        if old == new:
            continue  # already correctly named: a genuine no-op, not a loss
        old_full = f"{base}/{old}"
        new_full = f"{base}/{new}"
        new_dir = os.path.dirname(new_full)
        # mkdir -p so the rename works even when the other side's directory
        # layout doesn't yet exist on the receiver.
        op = f"mkdir -p -- {shlex.quote(new_dir)} && mv -vn -- {shlex.quote(old_full)} {shlex.quote(new_full)}"
        if host is None:
            lines.append(op)
        else:
            lines.append(f"ssh {shlex.quote(host)} {shlex.quote(op)}")
    return "\n".join(lines) + "\n", dropped


def cmd_sync_plan(args: argparse.Namespace) -> int:
    """Turn a compare report into rsync inputs: --files-from lists + a run.sh.

    Either compares two directories on the fly, or consumes a saved
    ``compare --output`` report via ``--from-report``. fsync plans and verifies;
    rsync moves the bytes.
    """
    logger = getattr(args, "logger", None)

    # 1. Obtain the compare report.
    if args.from_report:
        try:
            report = json.loads(Path(args.from_report).read_text())
        except (OSError, ValueError) as e:
            # OSError: missing / dir / unreadable; ValueError: bad JSON or non-UTF8.
            print(f"--from-report: cannot read {args.from_report}: {e}", file=sys.stderr)
            return 2
        if not args.src or not args.dest:
            print("--from-report requires --src and --dest (rsync endpoints)", file=sys.stderr)
            return 2
        src = args.src
        dest = args.dest
    else:
        if not args.dirA or not args.dirB:
            print("provide two directories (dirA dirB) or --from-report REPORT", file=sys.stderr)
            return 2
        a = _load_side(
            args.dirA, recursive=args.recursive, hash_algo=args.hash,
            workers=args.workers, show_progress=args.progress,
            exclude=getattr(args, "exclude", None),
            b3sum_path=getattr(args, "b3sum_path", None), logger=logger,
        )
        b = _load_side(
            args.dirB, recursive=args.recursive, hash_algo=args.hash,
            workers=args.workers, show_progress=args.progress,
            exclude=getattr(args, "exclude", None),
            b3sum_path=getattr(args, "b3sum_path", None), logger=logger,
        )
        report = compare_file_lists(a, b, match_on=args.match_on)
        # An index-file side has no directory to resolve into an endpoint.
        for spec, flag in ((args.dirA, "--src"), (args.dirB, "--dest")):
            if Path(spec).is_file() and not getattr(args, flag.lstrip("-"), None):
                print(f"{spec} is an index file; {flag} is required to name its rsync endpoint", file=sys.stderr)
                return 2
        src = args.src or str(Path(args.dirA).resolve())
        dest = args.dest or str(Path(args.dirB).resolve())

    # A newline in an endpoint would break out of run.sh comment/command lines.
    for label, ep in (("--src/dirA", src), ("--dest/dirB", dest)):
        if "\n" in ep or "\r" in ep:
            print(f"{label} contains a newline; refusing (would corrupt run.sh)", file=sys.stderr)
            return 2

    # 2. Build the directional plan.
    plan = build_sync_plan(report, conflict=args.conflict, mirror=args.mirror,
                           rename_min_size=getattr(args, "rename_min_size", 0))

    # 3. Emit the artifacts.
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ssh = None if args.no_ssh else (args.ssh or DEFAULT_RSYNC_SSH)
    dry_run = not args.execute
    flags = args.rsync_flags.split() if getattr(args, "rsync_flags", None) else None
    if flags is not None:
        # The default flags include --ignore-times specifically so rsync transfers
        # exactly the hash-chosen files. Warn if an override drops that guard.
        has_force = any(
            fl in ("--ignore-times", "--checksum", "-c")
            or (fl.startswith("-") and not fl.startswith("--") and ("I" in fl or "c" in fl))
            for fl in flags
        )
        if not has_force:
            print("WARNING: --rsync-flags lacks --ignore-times/-I (or --checksum); rsync "
                  "may skip same-size+mtime files that fsync flagged as different.",
                  file=sys.stderr)
    written: list[str] = []
    skipped_total = 0

    def write(name: str, text: str) -> str:
        (out_dir / name).write_text(text)
        written.append(name)
        return name

    def paths_for(items: list, label: str) -> list[str]:
        nonlocal skipped_total
        good, skipped = _plan_paths(items)
        if skipped:
            skipped_total += len(skipped)
            sample = [it.get("path") or it.get("name") or "<no path>" for it in skipped[:3]]
            print(f"WARNING: {label}: skipped {len(skipped)} entr(y/ies) with missing or "
                  f"newline-containing path (NOT synced): {sample}", file=sys.stderr)
        return good

    run_lines = [
        "#!/usr/bin/env bash",
        "# Generated by `fsync sync-plan`. REVIEW before running.",
        f"# mode: {args.mirror or 'bidirectional union'}   conflict: {args.conflict}",
        f"# SRC (A): {src}",
        f"# DEST(B): {dest}",
        "# rsync commands "
        + ("include -n (DRY RUN); re-run sync-plan with --execute to apply."
           if dry_run else "are LIVE (no -n). They will modify the destination."),
        "# Copies never delete; any deletions (mirror) are commented out below.",
        "set -euo pipefail",
        'cd "$(dirname "$0")"',
        "",
    ]

    a_paths = paths_for(plan["a_to_b"], "A->B")
    if a_paths:
        lf = write("plan.a_to_b.lst", "\n".join(a_paths) + "\n")
        run_lines += [
            f"# A -> B : {len(a_paths)} file(s) present/newer on A",
            render_rsync_command(lf, src, dest, dry_run=dry_run, ssh=ssh, flags=flags),
            "",
        ]

    b_paths = paths_for(plan["b_to_a"], "B->A")
    if b_paths:
        lf = write("plan.b_to_a.lst", "\n".join(b_paths) + "\n")
        run_lines += [
            f"# B -> A : {len(b_paths)} file(s) present/newer on B",
            render_rsync_command(lf, dest, src, dry_run=dry_run, ssh=ssh, flags=flags),
            "",
        ]

    del_paths = paths_for(plan["b_delete"], "delete-on-B")
    if del_paths:
        lf = write("plan.b_delete.lst", "\n".join(del_paths) + "\n")
        run_lines += [
            f"# --- DELETE on B ({len(del_paths)} file(s)) : mirror {args.mirror}. ---",
            "# DESTRUCTIVE and commented out. Review plan.b_delete.lst, then uncomment:",
            "# " + render_delete_block(lf, dest),
            "",
        ]
    del_paths_a = paths_for(plan["a_delete"], "delete-on-A")
    if del_paths_a:
        lf = write("plan.a_delete.lst", "\n".join(del_paths_a) + "\n")
        run_lines += [
            f"# --- DELETE on A ({len(del_paths_a)} file(s)) : mirror {args.mirror}. ---",
            "# DESTRUCTIVE and commented out. Review plan.a_delete.lst, then uncomment:",
            "# " + render_delete_block(lf, src),
            "",
        ]

    if plan["conflicts"]:
        clines = [
            "# Unresolved conflicts: same path, different content on both sides.",
            "# Decide each, then edit the .lst files or re-run with",
            "# --conflict newer|a-wins|b-wins (or --mirror to force a direction).",
            "",
        ]
        for a, b in plan["conflicts"]:
            clines.append(a.get("path", a.get("name", "?")))
            clines.append(f"    A: mtime={a.get('mtime')}  hash={a.get('hash')}")
            clines.append(f"    B: mtime={b.get('mtime')}  hash={b.get('hash')}")
        write("plan.conflicts.txt", "\n".join(clines) + "\n")
        run_lines += [
            f"# {len(plan['conflicts'])} unresolved conflict(s) -> see plan.conflicts.txt (NOT synced).",
            "",
        ]

    if plan["renames"]:
        # Rename on the side that must change so its names match the other side.
        # mirror b-to-a => change A (src), A's name -> B's name; otherwise change
        # B (dest), B's name -> A's name. In mirror mode the rename is needed for
        # convergence (renamed files are in neither copy nor delete list).
        if args.mirror == "b-to-a":
            rn_target, old_idx, new_idx = src, 0, 1
        else:
            rn_target, old_idx, new_idx = dest, 1, 0
        required = args.mirror is not None
        rn_text, rn_dropped = _render_renames_script(plan["renames"], rn_target, old_idx, new_idx, required=required)
        rn = write("plan.renames.sh", rn_text)
        os.chmod(out_dir / rn, 0o755)
        if rn_dropped:
            skipped_total += rn_dropped
            print(f"WARNING: renames: skipped {rn_dropped} rename(s) with missing path "
                  f"(NOT reconciled){'; mirror will not fully converge' if required else ''}.",
                  file=sys.stderr)
        n = len(plan["renames"]) - rn_dropped
        if required and not dry_run:
            run_lines += [f"# {n} content-identical rename(s) (required for mirror convergence):",
                          f"bash {rn}", ""]
        elif required:
            run_lines += [f"# {n} content-identical rename(s) (required for mirror; runs live):",
                          f"# DRY RUN — review, then with --execute this becomes: bash {rn}", ""]
        else:
            run_lines += [f"# {n} content-identical rename(s) -> optional reconciliation: bash {rn}", ""]

    run_name = write("run.sh", "\n".join(run_lines) + "\n")
    os.chmod(out_dir / run_name, 0o755)

    # 4. Summary.
    print(
        "sync-plan ({mode}, conflict={c}): A->B {ab}, B->A {ba}, "
        "conflicts {cf}, renames {rn}, identical {noop}{dels}{sup}{sk}".format(
            mode=args.mirror or "union",
            c=args.conflict,
            ab=len(a_paths), ba=len(b_paths),
            cf=len(plan["conflicts"]), rn=len(plan["renames"]), noop=plan["noop"],
            dels=(f", deletes {len(del_paths) + len(del_paths_a)}"
                  if (del_paths or del_paths_a) else ""),
            sup=(f", renames demoted to copies {len(plan['renames_suppressed'])}"
                 if plan.get("renames_suppressed") else ""),
            sk=(f", SKIPPED {skipped_total}" if skipped_total else ""),
        ),
        file=sys.stderr,
    )
    print(f"wrote {len(written)} file(s) to {out_dir} (run: bash {out_dir / 'run.sh'}"
          + ("; DRY RUN)" if dry_run else "; LIVE)"), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fsync")
    sub = p.add_subparsers(dest="cmd")

    p_index = sub.add_parser("index", help="Index a directory and print JSON metadata")
    p_index.add_argument("dir", help="Directory to index")
    p_index.add_argument("--recursive", action="store_true", default=True, help="Recurse into subdirectories")
    p_index.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not recurse")
    p_index.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks to files")
    p_index.add_argument("--hash", default="sha256", help="Hash algorithm (default: sha256)")
    p_index.add_argument("--exclude", action="append", default=None, metavar="PATTERN",
                         help="Exclude pattern (repeatable): with '/' matches the relative path, without matches any path component")
    p_index.add_argument("--cache", action="store_true",
                         help="Reuse cached hashes for files with unchanged size+mtime (per-root cache under ~/.cache/fsync)")
    p_index.add_argument("--fields", help="Comma-separated list of metadata fields to include (default: all)")
    p_index.add_argument("--output", help="Write output JSON to file")
    p_index.add_argument("--format", choices=("json","jsonl"), default="json", help="Output format for index (json|jsonl)")
    p_index.add_argument("--workers", type=int, default=1, help="Number of worker threads for hashing (default: 1)")
    p_index.add_argument("--progress", action="store_true", help="Show a progress bar (requires tqdm)")
    p_index.add_argument("--verbose", action="count", default=0, help="Increase verbosity (repeat for more)")
    p_index.add_argument("--b3sum-path", help="Path to external b3sum binary (optional)")
    p_index.add_argument("--store-db", action="store_true", help="Store index results into a Postgres DB")
    p_index.add_argument("--db-url", help="Postgres connection URL (overrides DB_URL env var)")
    p_index.add_argument("--catalog-url", help="fsync catalog API base URL; POST batches over HTTP instead of direct DB")
    p_index.add_argument("--source", help="Source label for the central catalog (default: absolute path of dir)")
    p_index.add_argument("--events-url", help="dream4events base URL; if set, emit scan.* progress events")
    p_index.add_argument("--scanner-id", help="Scanner id used as event instance (default: hostname)")
    p_index.add_argument("--run-id", help="Correlation id for this scan's events (default: generated ULID)")

    p_cmp = sub.add_parser("compare", help="Compare two directories (or saved index files) and print JSON report")
    p_cmp.add_argument("dirA", help="Left directory, or a saved `index --output` JSON/JSONL file")
    p_cmp.add_argument("dirB", help="Right directory, or a saved `index --output` JSON/JSONL file")
    p_cmp.add_argument("--exclude", action="append", default=None, metavar="PATTERN",
                       help="Exclude pattern (repeatable); also filters records loaded from index files")
    p_cmp.add_argument("--recursive", action="store_true", default=True, help="Recurse into subdirectories")
    p_cmp.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not recurse")
    p_cmp.add_argument("--hash", default="sha256", help="Hash algorithm (default: sha256)")
    p_cmp.add_argument("--fields", help="Comma-separated list of metadata fields to include (default: all)")
    p_cmp.add_argument("--output", help="Write output JSON to file")
    p_cmp.add_argument("--format", choices=("json","pretty"), default="json", help="Output format for compare (default: json)")
    p_cmp.add_argument("--match-on", choices=("path","name"), default="path", help="Match on 'path' (relative path) or 'name' (filename) when comparing (default: path)")
    p_cmp.add_argument("--workers", type=int, default=1, help="Number of worker threads for hashing (default: 1)")
    p_cmp.add_argument("--show", type=int, default=0, help="Show up to N sample matching entries in pretty format")
    p_cmp.add_argument("--progress", action="store_true", help="Show a progress bar (requires tqdm)")
    p_cmp.add_argument("--verbose", action="count", default=0, help="Increase verbosity (repeat for more)")
    p_cmp.add_argument("--b3sum-path", help="Path to external b3sum binary (optional)")
    p_cmp.add_argument("--store-db", action="store_true", help="Store index results into a Postgres DB")
    p_cmp.add_argument("--db-url", help="Postgres connection URL (overrides DB_URL env var)")
    p_cmp.add_argument("--source-a", help="Source label for dirA in the central catalog (default: absolute path)")
    p_cmp.add_argument("--source-b", help="Source label for dirB in the central catalog (default: absolute path)")

    p_bench = sub.add_parser("benchmark", help="Run a simple hashing benchmark")
    p_bench.add_argument("dir", help="Directory to create files for benchmark (will write files)")
    p_bench.add_argument("--count", type=int, default=50, help="Number of files to create")
    p_bench.add_argument("--size", type=int, default=1024, help="Size of each file in bytes")
    p_bench.add_argument("--hash", default="sha256", help="Hash algorithm to benchmark (sha256 or b3sum)")
    p_bench.add_argument("--workers", type=int, default=1, help="Worker threads for hashing")
    p_bench.add_argument("--b3sum-path", help="Path to b3sum binary (optional)")
    p_bench.add_argument("--progress", action="store_true", help="Show a progress bar (requires tqdm)")
    p_bench.add_argument("--verbose", action="count", default=0, help="Increase verbosity (repeat for more)")

    # agent: long-running control-plane endpoint
    p_agent = sub.add_parser("agent", help="Run the long-running scanner agent (register/heartbeat/poll commands)")
    p_agent.add_argument("--scanner-id", help="Stable scanner id (default: hostname)")
    p_agent.add_argument("--events-url", help="dream4events base URL (or FSYNC_EVENTS_URL)")
    p_agent.add_argument("--catalog-url", help="fsync catalog API base URL (or FSYNC_CATALOG_URL)")
    p_agent.add_argument("--db-url", help="Direct Postgres DSN fallback if no catalog-url (or DB_URL)")
    p_agent.add_argument("--sources", help="Comma-separated roots this scanner manages (advertised in registration)")
    p_agent.add_argument("--cursor", help="Path to persist the command-consumer cursor")
    p_agent.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between command polls")
    p_agent.add_argument("--heartbeat-interval", type=float, default=30.0, help="Seconds between heartbeats")
    p_agent.add_argument("--verbose", action="count", default=0, help="Increase verbosity")

    # ctl: emit a command to a scanner (operator tool)
    p_ctl = sub.add_parser("ctl", help="Send a command to a scanner via dream4events")
    p_ctl.add_argument("command", choices=("ping", "start_scan", "stop"), help="Command to send")
    p_ctl.add_argument("--scanner-id", required=True, help="Target scanner id")
    p_ctl.add_argument("--events-url", help="dream4events base URL (or FSYNC_EVENTS_URL)")
    p_ctl.add_argument("--dir", help="(start_scan) directory to index")
    p_ctl.add_argument("--source", help="(start_scan) source label")
    p_ctl.add_argument("--workers", type=int, default=4, help="(start_scan) hashing workers")
    p_ctl.add_argument("--hash", default="sha256", help="(start_scan) hash algorithm")
    p_ctl.add_argument("--verbose", action="count", default=0, help="Increase verbosity")

    # sync-plan: project a compare report onto rsync inputs (--files-from + run.sh)
    p_plan = sub.add_parser("sync-plan", help="Generate rsync --files-from lists + run.sh from a compare diff")
    p_plan.add_argument("dirA", nargs="?", help="Left directory or saved index file (omit if using --from-report)")
    p_plan.add_argument("dirB", nargs="?", help="Right directory or saved index file (omit if using --from-report)")
    p_plan.add_argument("--exclude", action="append", default=None, metavar="PATTERN",
                        help="Exclude pattern (repeatable); applies to scans and loaded index files")
    p_plan.add_argument("--rename-min-size", type=int, default=0, metavar="BYTES",
                        help="Demote rename pairs smaller than BYTES to plain copies (duplicate-content pairs are always demoted)")
    p_plan.add_argument("--from-report", help="Consume a saved `compare --output` JSON report instead of scanning")
    p_plan.add_argument("--out-dir", default="fsync-plan", help="Directory for the generated plan files (default: fsync-plan)")
    p_plan.add_argument("--conflict", choices=("review", "newer", "a-wins", "b-wins"), default="review",
                        help="How to route same-path/different-content files (default: review)")
    p_plan.add_argument("--mirror", choices=("a-to-b", "b-to-a"), default=None,
                        help="One-way mirror (the only mode that proposes deletions); ignores --conflict")
    p_plan.add_argument("--src", help="rsync SOURCE endpoint for A (default: resolved dirA); e.g. /home/developer")
    p_plan.add_argument("--dest", help="rsync DEST endpoint for B (default: resolved dirB); e.g. developer@10.55.0.2:/home/developer")
    p_plan.add_argument("--ssh", help=f"ssh command for rsync -e on remote endpoints (default: {DEFAULT_RSYNC_SSH!r})")
    p_plan.add_argument("--rsync-flags", help="Override the default rsync flags ('-aHAXS --numeric-ids --ignore-times --info=progress2 --partial')")
    p_plan.add_argument("--no-ssh", action="store_true", help="Do not add -e ssh (both endpoints are local)")
    p_plan.add_argument("--execute", action="store_true", help="Emit live rsync commands (default: dry-run, with -n)")
    p_plan.add_argument("--recursive", action="store_true", default=True, help="Recurse into subdirectories")
    p_plan.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not recurse")
    p_plan.add_argument("--hash", default="sha256", help="Hash algorithm for the scan (default: sha256)")
    p_plan.add_argument("--match-on", choices=("path", "name"), default="path", help="Match files on path or name (default: path)")
    p_plan.add_argument("--workers", type=int, default=1, help="Hashing worker threads (default: 1)")
    p_plan.add_argument("--b3sum-path", help="Path to external b3sum binary (optional)")
    p_plan.add_argument("--progress", action="store_true", help="Show a progress bar (requires tqdm)")
    p_plan.add_argument("--verbose", action="count", default=0, help="Increase verbosity")

    # sync: profile-driven home synchronisation (docs/home-sync-automation.md)
    p_sync = sub.add_parser("sync", help="Profile-driven two-box sync: plan, transfer with backup-dir safety, report")
    sync_sub = p_sync.add_subparsers(dest="sync_cmd")
    p_sync_run = sync_sub.add_parser("run", help="Run sync profiles headlessly (safe no-op when the peer is away)")
    p_sync_run.add_argument("--profile", action="append", default=None,
                            help="Profile name to run (repeatable); use --all for every profile")
    p_sync_run.add_argument("--all", action="store_true", help="Run every profile in the config")
    p_sync_run.add_argument("--config", help="Profiles YAML (default: ~/.config/fsync/sync-profiles.yaml)")
    p_sync_run.add_argument("--dry-run", action="store_true", help="Plan and report but pass -n to rsync (no transfers, no backups)")
    p_sync_run.add_argument("--plan-only", action="store_true",
                            help="Print a JSON preview of what would transfer (no lock, no rsync) — used by `fsync tui`")
    p_sync_run.add_argument("--plan-progress", metavar="FILE", default=None,
                            help="With --plan-only: maintain a live per-path planning snapshot at FILE (for UIs)")
    p_sync_run.add_argument("--workers", type=int, default=None, help="Override hashing workers on both sides")
    p_sync_run.add_argument("--notify", action="store_true",
                            help="Desktop notification when files moved or the run failed (used by the timer)")
    p_sync_run.add_argument("--verbose", action="count", default=0, help="Increase verbosity")
    p_sync_init = sync_sub.add_parser("init", help="Write a starter sync-profiles.yaml")
    p_sync_init.add_argument("--config", help="Target path (default: ~/.config/fsync/sync-profiles.yaml)")
    p_sync_init.add_argument("--force", action="store_true", help="Overwrite an existing config")
    p_sync_init.add_argument("--verbose", action="count", default=0, help="Increase verbosity")

    p_sync_timer = sync_sub.add_parser("timer", help="Manage the systemd user timer for hands-off runs")
    p_sync_timer.add_argument("action", choices=("install", "remove", "status"),
                              help="install: write+enable units; remove: disable+delete; status: timers + last run")
    p_sync_timer.add_argument("--interval", default="1h",
                              help="Run cadence as a systemd time span (default: 1h)")
    p_sync_timer.add_argument("--verbose", action="count", default=0, help="Increase verbosity")

    # tui: thin client of fsyncd — plan -> one confirmation -> backend run
    p_tui = sub.add_parser("tui", help="Terminal UI for sync (thin client of fsyncd)")
    p_tui.add_argument("--profile", action="append", default=None,
                       help="Limit to profile NAME (repeatable); default: all profiles")
    p_tui.add_argument("--port", type=int, default=None, help="fsyncd port (default: 7444)")
    p_tui.add_argument("--verbose", action="count", default=0, help="Increase verbosity")

    # daemon: the operational backend (REST over TLS/mTLS + scheduler)
    p_dmn = sub.add_parser("daemon", help="fsyncd backend: REST API over TLS/mTLS, absorbs run scheduling")
    p_dmn.add_argument("action", choices=("run", "install", "remove", "status", "trust", "cert"),
                       help="run: serve in foreground; install: systemd user service (retires the timer); "
                            "trust: pin a peer's cert over ssh; cert: show this box's cert")
    p_dmn.add_argument("host", nargs="?", help="(trust) ssh target of the peer, e.g. minis4dx.lan")
    p_dmn.add_argument("--name", help="(trust) name to pin the peer under (default: host short name)")
    p_dmn.add_argument("--show", action="store_true", help="(cert) print the PEM to stdout")
    p_dmn.add_argument("--config", help="Profiles YAML (default: ~/.config/fsync/sync-profiles.yaml)")
    p_dmn.add_argument("--verbose", action="count", default=0, help="Increase verbosity")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Configure logging
    import logging

    log_level = max(10, 30 - (10 * getattr(args, "verbose", 0)))
    logging.basicConfig(level=log_level)
    logger = logging.getLogger("fsync")

    args.logger = logger

    if args.cmd == "index":
        return cmd_index(args)
    if args.cmd == "compare":
        return cmd_compare(args)
    if args.cmd == "benchmark":
        return cmd_benchmark(args)
    if args.cmd == "agent":
        from .agent import run_agent

        return run_agent(args)
    if args.cmd == "ctl":
        return cmd_ctl(args)
    if args.cmd == "sync-plan":
        return cmd_sync_plan(args)
    if args.cmd == "sync":
        from .homesync import cmd_sync

        return cmd_sync(args)
    if args.cmd == "tui":
        try:
            from .tui import run_tui
        except ImportError:
            print("fsync tui requires the 'textual' package: pip install textual", file=sys.stderr)
            return 2
        return run_tui(args)
    if args.cmd == "daemon":
        from .daemon import cmd_daemon

        return cmd_daemon(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
