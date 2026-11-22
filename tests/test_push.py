"""store.push against real git repositories: data/ lives inside a repo whose other files must stay untouched."""

import subprocess
from pathlib import Path

from fiberyet import store


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout


def make_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README").write_text("code\n")
    (seed / "data").mkdir()
    (seed / "data" / "keep.txt").write_text("0")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "main")
    return remote


def clone(tmp_path: Path, remote: Path, name: str) -> Path:
    path = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(remote), str(path)], check=True)
    return path


def remote_files(remote: Path) -> list[str]:
    return sorted(subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"],
        check=True, capture_output=True, text=True).stdout.split())


def test_commits_only_data_dir_and_leaves_other_changes_alone(tmp_path):
    remote = make_remote(tmp_path)
    repo = clone(tmp_path, remote, "work")
    (repo / "data" / "m.jsonl").write_text("row\n")
    (repo / "README").write_text("edited by a human, not part of the data commit\n")
    (repo / "src_new.py").write_text("print('untracked')\n")

    assert store.push(repo / "data", "data: slice") is True

    assert remote_files(remote) == ["README", "data/keep.txt", "data/m.jsonl"]
    log = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%an|%s", "-1", "main"],
                         check=True, capture_output=True, text=True).stdout.strip()
    assert log == "github-actions[bot]|data: slice"
    status = git(repo, "status", "--porcelain")
    assert " M README" in status and "?? src_new.py" in status
    assert (repo / "README").read_text().startswith("edited by a human")


def test_nothing_to_commit_returns_false_even_if_the_repo_is_dirty(tmp_path):
    remote = make_remote(tmp_path)
    repo = clone(tmp_path, remote, "work")
    (repo / "README").write_text("dirty outside data\n")
    assert store.push(repo / "data", "nothing") is False
    assert remote_files(remote) == ["README", "data/keep.txt"]


def test_rebases_over_a_concurrent_push_and_keeps_both_changes(tmp_path):
    remote = make_remote(tmp_path)
    a, b = clone(tmp_path, remote, "a"), clone(tmp_path, remote, "b")
    (a / "data" / "tel.jsonl").write_text("a\n")
    assert store.push(a / "data", "a") is True
    (b / "data" / "eins.jsonl").write_text("b\n")  # b is behind: its first push is rejected
    (b / "README").write_text("b has an uncommitted code edit\n")
    assert store.push(b / "data", "b") is True
    assert remote_files(remote) == ["README", "data/eins.jsonl", "data/keep.txt", "data/tel.jsonl"]
    assert (b / "README").read_text().startswith("b has an uncommitted")


def test_pushes_the_branch_it_is_on(tmp_path):
    remote = make_remote(tmp_path)
    repo = clone(tmp_path, remote, "work")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "data" / "x.txt").write_text("1")
    assert store.push(repo / "data", "on feature") is True
    heads = subprocess.run(["git", "--git-dir", str(remote), "branch", "--list"],
                           check=True, capture_output=True, text=True).stdout
    assert "feature" in heads
    assert "data/x.txt" not in remote_files(remote)
