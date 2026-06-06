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
import sys
from pathlib import Path
from typing import Any

from .fileindex import list_files_with_metadata, compare_file_lists


def cmd_index(args: argparse.Namespace) -> int:
    fields = None
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]

    data = list_files_with_metadata(
        Path(args.dir),
        recursive=args.recursive,
        follow_symlinks=args.follow_symlinks,
        hash_algo=args.hash,
        fields=fields,
        workers=args.workers,
        show_progress=args.progress,
        logger=getattr(args, "logger", None),
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
    # Optionally store in DB
    if getattr(args, "store_db", False):
        db_url = args.db_url or os.environ.get("DB_URL")
        if not db_url:
            print("DB URL not provided (use --db-url or set DB_URL)", file=sys.stderr)
            return 2
        from .db import store_index

        store_index(db_url, data)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    fields = None
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]

    a = list_files_with_metadata(
        Path(args.dirA), recursive=args.recursive, hash_algo=args.hash, fields=fields, workers=args.workers, show_progress=args.progress, logger=getattr(args, "logger", None)
    )
    b = list_files_with_metadata(
        Path(args.dirB), recursive=args.recursive, hash_algo=args.hash, fields=fields, workers=args.workers, show_progress=args.progress, logger=getattr(args, "logger", None)
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

        store_index(db_url, a)
        store_index(db_url, b)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fsync")
    sub = p.add_subparsers(dest="cmd")

    p_index = sub.add_parser("index", help="Index a directory and print JSON metadata")
    p_index.add_argument("dir", help="Directory to index")
    p_index.add_argument("--recursive", action="store_true", default=True, help="Recurse into subdirectories")
    p_index.add_argument("--no-recursive", dest="recursive", action="store_false", help="Do not recurse")
    p_index.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks to files")
    p_index.add_argument("--hash", default="sha256", help="Hash algorithm (default: sha256)")
    p_index.add_argument("--fields", help="Comma-separated list of metadata fields to include (default: all)")
    p_index.add_argument("--output", help="Write output JSON to file")
    p_index.add_argument("--format", choices=("json","jsonl"), default="json", help="Output format for index (json|jsonl)")
    p_index.add_argument("--workers", type=int, default=1, help="Number of worker threads for hashing (default: 1)")
    p_index.add_argument("--progress", action="store_true", help="Show a progress bar (requires tqdm)")
    p_index.add_argument("--verbose", action="count", default=0, help="Increase verbosity (repeat for more)")
    p_index.add_argument("--b3sum-path", help="Path to external b3sum binary (optional)")
    p_index.add_argument("--store-db", action="store_true", help="Store index results into a Postgres DB")
    p_index.add_argument("--db-url", help="Postgres connection URL (overrides DB_URL env var)")

    p_cmp = sub.add_parser("compare", help="Compare two directories and print JSON report")
    p_cmp.add_argument("dirA", help="Left directory")
    p_cmp.add_argument("dirB", help="Right directory")
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

    p_bench = sub.add_parser("benchmark", help="Run a simple hashing benchmark")
    p_bench.add_argument("dir", help="Directory to create files for benchmark (will write files)")
    p_bench.add_argument("--count", type=int, default=50, help="Number of files to create")
    p_bench.add_argument("--size", type=int, default=1024, help="Size of each file in bytes")
    p_bench.add_argument("--hash", default="sha256", help="Hash algorithm to benchmark (sha256 or b3sum)")
    p_bench.add_argument("--workers", type=int, default=1, help="Worker threads for hashing")
    p_bench.add_argument("--b3sum-path", help="Path to b3sum binary (optional)")
    p_bench.add_argument("--progress", action="store_true", help="Show a progress bar (requires tqdm)")
    p_bench.add_argument("--verbose", action="count", default=0, help="Increase verbosity (repeat for more)")

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
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
