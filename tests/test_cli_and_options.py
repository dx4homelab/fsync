import tempfile
from pathlib import Path
from subprocess import run, PIPE
import json
import sys


def run_cli(args, cwd=None):
    cmd = [sys.executable, '-m', 'fsync.cli'] + args
    res = run(cmd, stdout=PIPE, stderr=PIPE, text=True, cwd=cwd)
    return res


def test_index_fields(tmp_path):
    d = tmp_path / 'src'
    d.mkdir()
    (d / 'x.txt').write_text('abc')

    res = run_cli(['index', str(d), '--fields', 'name,path,hash'])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert isinstance(data, list)
    assert 'name' in data[0]
    assert 'hash' in data[0]
    assert 'uid' not in data[0]


def test_compare_match_on_name(tmp_path):
    a = tmp_path / 'A'
    b = tmp_path / 'B'
    a.mkdir(); b.mkdir()
    # create same filename but different paths
    (a / 'sub').mkdir(); (b / 'other').mkdir()
    (a / 'sub' / 'dup.txt').write_text('hello')
    (b / 'other' / 'dup.txt').write_text('hello')

    # run compare matching on name
    res = run_cli(['compare', str(a), str(b), '--match-on', 'name'])
    assert res.returncode == 0
    report = json.loads(res.stdout)
    assert len(report['exact_matches']) == 1


def test_index_jsonl_and_workers(tmp_path):
    d = tmp_path / 'data'
    d.mkdir()
    for i in range(5):
        (d / f'f{i}.txt').write_text('x' * (i+1))

    res = run_cli(['index', str(d), '--format', 'jsonl', '--workers', '2'])
    assert res.returncode == 0
    # jsonl has one JSON object per line
    lines = [l for l in res.stdout.splitlines() if l.strip()]
    assert len(lines) == 5


def test_compare_pretty(tmp_path):
    a = tmp_path / 'A2'
    b = tmp_path / 'B2'
    a.mkdir(); b.mkdir()
    (a / 'one.txt').write_text('a')
    (b / 'two.txt').write_text('b')

    res = run_cli(['compare', str(a), str(b), '--format', 'pretty'])
    assert res.returncode == 0
    assert 'exact_matches' in res.stdout


def test_compare_pretty_show(tmp_path):
    a = tmp_path / 'A3'
    b = tmp_path / 'B3'
    a.mkdir(); b.mkdir()
    (a / 'same').mkdir(); (b / 'oth').mkdir()
    (a / 'same' / 's.txt').write_text('x')
    (b / 'oth' / 's.txt').write_text('x')

    res = run_cli(['compare', str(a), str(b), '--format', 'pretty', '--show', '2'])
    assert res.returncode == 0
    assert 'Samples:' in res.stdout


def test_b3sum_if_available(tmp_path):
    # If b3sum is not installed, skip this test
    import shutil
    if shutil.which("b3sum") is None:
        import pytest

        pytest.skip("b3sum not available")

    d = tmp_path / 'b3'
    d.mkdir()
    (d / 'f.txt').write_text('hello')
    res = run_cli(['index', str(d), '--hash', 'b3sum'])
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data[0].get('hash')


def test_benchmark_small(tmp_path):
    d = tmp_path / 'bench'
    res = run_cli(['benchmark', str(d), '--count', '5', '--size', '128', '--workers', '2'])
    assert res.returncode == 0