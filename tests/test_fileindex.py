import tempfile
import os
from pathlib import Path
from fsync.fileindex import list_files_with_metadata, compare_file_lists


def write_file(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(data)


def test_exact_match_and_differences(tmp_path):
    # Setup directory A
    a_dir = tmp_path / "A"
    a_dir.mkdir()
    write_file(a_dir / "file1.txt", b"hello world")
    write_file(a_dir / "same_name_diff_content.txt", b"version A")
    write_file(a_dir / "unique_a.txt", b"only in A")

    # Setup directory B
    b_dir = tmp_path / "B"
    b_dir.mkdir()
    write_file(b_dir / "file1.txt", b"hello world")  # exact match
    write_file(b_dir / "same_name_diff_content.txt", b"version B")  # same name, diff hash
    write_file(b_dir / "different_name_same_content.txt", b"only in A but content matches unique_a")

    # Make one file in B with same content as A's unique_a
    write_file(b_dir / "different_name_same_content.txt", (a_dir / "unique_a.txt").read_bytes())

    list_a = list_files_with_metadata(a_dir)
    list_b = list_files_with_metadata(b_dir)

    res = compare_file_lists(list_a, list_b)

    # exact match should include file1.txt
    exact_names = [a["name"] for a, _ in res["exact_matches"]]
    assert "file1.txt" in exact_names

    # name match diff hash should include same_name_diff_content.txt
    name_diff = [a["name"] for a, _ in res["name_matches_diff_hash"]]
    assert "same_name_diff_content.txt" in name_diff

    # hash match diff name should match unique_a.txt with different_name_same_content.txt
    hash_matches = [(a["name"], b["name"]) for a, b in res["hash_matches_diff_name"]]
    assert ("unique_a.txt", "different_name_same_content.txt") in hash_matches

    # only_in_a and only_in_b should be empty after above matches
    assert len(res["only_in_a"]) == 0
    assert len(res["only_in_b"]) == 0
