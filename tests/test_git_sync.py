"""Real temporary Git repositories and SQLite files; no GitHub or device writes."""

import json
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.codex_notify import dispatch
from scripts.git_sync import Blocked, Sync, private_path, write_json
from scripts.install_git_sync import replace_notify


def git(root, *args):
    return (
        subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Sync Test")
    git(root, "config", "user.email", "sync@example.invalid")
    (root / ".gitignore").write_text("data/\nprivate/\n.env\nconfig.local.yaml\n")
    (root / "app.py").write_text("print('initial')\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "origin", "main")
    sync = Sync(root)
    # A scanner double only; the installation separately uses real Gitleaks.
    scanner = tmp_path / "scanner"
    scanner.write_text(
        f"#!{sys.executable}\n"
        "import sys,json,pathlib,subprocess\n"
        "args=sys.argv; target=pathlib.Path(args[-1]); found=False\n"
        "if args[1]=='dir':\n"
        " found=any(b'REAL_SECRET_SENTINEL' in p.read_bytes() for p in target.rglob('*') "
        "if p.is_file())\n"
        "else:\n"
        " data=subprocess.check_output(['git','log','--all','-p'],cwd=target)\n"
        " found=b'REAL_SECRET_SENTINEL' in data\n"
        "report=args[args.index('--report-path')+1]\n"
        "pathlib.Path(report).write_text(json.dumps([{'RuleID':'test','File':'app.py',"
        "'StartLine':1,'Secret':'MUST_NOT_BE_LOGGED'}] if found else []))\n"
        "sys.exit(1 if found else 0)\n"
    )
    scanner.chmod(0o700)
    sync.cfg = {
        "origin": str(remote),
        "branch": "main",
        "gitleaks": str(scanner),
        "backups": str(tmp_path / "backups"),
    }
    write_json(sync.state / "settings.json", sync.cfg)
    return sync, remote


def test_real_commit_push_and_readback(repo):
    sync, remote = repo
    old = git(sync.root, "rev-parse", "HEAD")
    (sync.root / "app.py").write_text("print('updated')\n")
    result = sync.sync("fix: handle updated behavior")
    assert result["status"] == "synced"
    assert result["commit"] != old
    assert git(remote, "rev-parse", "main") == result["commit"]
    assert git(sync.root, "log", "-1", "--format=%s") == "fix: handle updated behavior"
    assert not git(sync.root, "status", "--porcelain")


def test_offline_commit_survives_then_pushes(repo, tmp_path):
    sync, remote = repo
    unavailable = str(tmp_path / "not-connected.git")
    git(sync.root, "remote", "set-url", "origin", unavailable)
    sync.cfg["origin"] = unavailable
    (sync.root / "app.py").write_text("print('offline change')\n")
    assert sync.sync("fix: retain offline work")["status"] == "pending_push"
    local = git(sync.root, "rev-parse", "HEAD")
    assert git(remote, "rev-parse", "main") != local
    git(sync.root, "remote", "set-url", "origin", str(remote))
    sync.cfg["origin"] = str(remote)
    assert sync.sync()["status"] == "synced"
    assert git(remote, "rev-parse", "main") == local


def test_remote_changes_are_never_overwritten(repo, tmp_path):
    sync, remote = repo
    other = tmp_path / "other"
    git(tmp_path, "clone", "--branch", "main", str(remote), str(other))
    git(other, "config", "user.name", "Other")
    git(other, "config", "user.email", "other@example.invalid")
    (other / "other.py").write_text("pass\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "another developer")
    git(other, "push", "origin", "main")
    expected = git(remote, "rev-parse", "main")
    (sync.root / "app.py").write_text("print('local')\n")
    with pytest.raises(Blocked, match="Remote has new commits"):
        sync.sync("fix: local work")
    assert git(remote, "rev-parse", "main") == expected
    assert git(sync.root, "log", "-1", "--format=%s") == "fix: local work"


def test_secret_in_deleted_history_blocks_publication(repo):
    sync, remote = repo
    original = git(remote, "rev-parse", "main")
    (sync.root / "app.py").write_text("REAL_SECRET_SENTINEL\n")
    git(sync.root, "commit", "-am", "bad old commit")
    (sync.root / "app.py").write_text("print('removed secret')\n")
    git(sync.root, "commit", "-am", "remove secret")
    with pytest.raises(Blocked, match="Secret scan"):
        sync.sync()
    assert git(remote, "rev-parse", "main") == original
    assert "MUST_NOT_BE_LOGGED" not in (sync.state / "secret-findings.json").read_text()


def test_secret_in_index_blocks_commit_and_restores_index(repo):
    sync, remote = repo
    before = (sync.git_dir / "index").read_bytes()
    (sync.root / "app.py").write_text("REAL_SECRET_SENTINEL\n")
    with pytest.raises(Blocked, match="Secret scan"):
        sync.sync()
    assert (sync.git_dir / "index").read_bytes() == before
    assert git(sync.root, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.production",
        "data/jarvis.db",
        "private/notes.txt",
        "config.local.yaml",
        "a/credentials.json",
        "token.json",
        "a/key.pem",
        ".venv/a.py",
        "auth.json",
        "docs/diagnostics/history.json",
        ".codex/config.toml",
    ],
)
def test_private_paths_are_rejected(name):
    assert private_path(name)


@pytest.mark.parametrize("name", [".env.example", "backend/security.py", "config.yaml", "uv.lock"])
def test_public_source_paths_are_allowed(name):
    assert not private_path(name)


def test_ignored_private_data_is_backed_up_without_being_committed(repo):
    sync, _ = repo
    (sync.root / ".env").write_text("EXAMPLE_PRIVATE_VALUE=do-not-publish\n")
    (sync.root / "config.local.yaml").write_text("language: ru-RU\n")
    sync.sync()
    snapshot = Path(json.loads((sync.state / "last-backup.json").read_text())["path"])
    assert (snapshot / ".env").read_text().endswith("do-not-publish\n")
    assert (snapshot / ".env").stat().st_mode & 0o777 == 0o600
    assert snapshot.stat().st_mode & 0o777 == 0o700
    assert ".env" not in git(sync.root, "ls-files").splitlines()


def test_sqlite_wal_backup_restores_committed_conversation(repo):
    sync, _ = repo
    (sync.root / "data").mkdir()
    db = sqlite3.connect(sync.root / "data/jarvis.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE conversation (text TEXT)")
    db.execute("INSERT INTO conversation VALUES ('private remembered fact')")
    db.commit()
    path = Path(sync.backup()) / "data/jarvis.db"
    with sqlite3.connect(path) as restored:
        assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT text FROM conversation").fetchone()[0].startswith("private")
    db.close()
    assert not path.with_name("jarvis.db-wal").exists()


def test_backups_skip_symlinks_and_unchanged_snapshots(repo, tmp_path):
    sync, _ = repo
    (sync.root / "private").mkdir()
    secret = tmp_path / "external-private"
    secret.write_text("not explicitly included")
    (sync.root / "private/alias").symlink_to(secret)
    first = sync.backup()
    assert sync.backup() == first
    assert not (Path(first) / "private/alias").exists()


def test_backups_skip_renamed_app_bundles_and_never_prune_existing_snapshots(repo):
    sync, _ = repo
    artifact = sync.root / "private/release/Чушпан-0.3.4-17.app/Contents/MacOS/Chushpan"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"app artifact")
    note = sync.root / "private/release/notes.txt"
    note.write_text("keep private diagnostics")
    base = Path(sync.cfg["backups"])
    base.mkdir()
    protected = []
    for i in range(50):
        p = base / f"snapshot-20260101T{i:06d}Z"
        p.mkdir()
        (p / "keep.txt").write_text("backup")
        protected.append(p)
    snapshot = Path(sync.backup())
    assert (snapshot / "private/release/notes.txt").read_text() == "keep private diagnostics"
    assert not (snapshot / "private/release/Чушпан-0.3.4-17.app").exists()
    assert all((p / "keep.txt").read_text() == "backup" for p in protected)
    assert json.loads((sync.state / "backup-retention-candidates.json").read_text())["candidates"]


def test_backup_retention_is_opt_in_keeps_three_verified_and_pins(repo):
    sync, _ = repo
    base = Path(sync.cfg["backups"])
    policy = base.parent / "storage-policy.json"
    policy.write_text(json.dumps({"auto_cleanup": True, "pinned": [], "retention_interval_s": 0}))
    first = None
    for index in range(5):
        (sync.root / "config.yaml").write_text(f"revision: {index}\n")
        current = Path(sync.backup())
        if first is None:
            first = current
            policy.write_text(
                json.dumps(
                    {
                        "auto_cleanup": True,
                        "pinned": [first.name],
                        "retention_interval_s": 0,
                    }
                )
            )
    snapshots = list(base.glob("snapshot-*"))
    assert len(snapshots) == 4
    assert first is not None and first.exists()
    assert len([path for path in snapshots if Sync._backup_valid(path)]) == 4


def test_backup_retention_never_deletes_a_corrupt_or_pinned_copy(repo):
    sync, _ = repo
    base = Path(sync.cfg["backups"])
    policy = base.parent / "storage-policy.json"
    policy.write_text(json.dumps({"auto_cleanup": False, "pinned": []}))
    copies = []
    for index in range(4):
        (sync.root / "config.yaml").write_text(f"revision: {index}\n")
        copies.append(Path(sync.backup()))
    damaged = copies[0]
    (damaged / "config.yaml").write_text("corrupted\n")
    policy.write_text(json.dumps({"auto_cleanup": True, "pinned": [], "retention_interval_s": 0}))
    (sync.root / "config.yaml").write_text("revision: 5\n")
    sync.backup()
    assert damaged.exists()


def test_backups_skip_nested_backup_and_app_artifacts_but_keep_sqlite(repo):
    sync, _ = repo
    artifact = sync.root / "private/qa.noindex/Чушпан.app/Contents/MacOS/Chushpan"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"rebuildable app")
    nested = sync.root / "private/qa.noindex/Backups.noindex/snapshot-old/old.db"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"nested backup copy")
    preserved = sync.root / "private/qa.noindex/user-data/jarvis.db"
    preserved.parent.mkdir(parents=True)
    with sqlite3.connect(preserved) as user_db:
        user_db.execute("CREATE TABLE preserved (value TEXT)")
        user_db.execute("INSERT INTO preserved VALUES ('user data')")
    db = sync.root / "data/jarvis.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as source:
        source.execute("CREATE TABLE memory (value TEXT)")
        source.execute("INSERT INTO memory VALUES ('retain')")
    snapshot = Path(sync.backup())
    assert not (snapshot / "private/qa.noindex/Чушпан.app").exists()
    assert not (snapshot / "private/qa.noindex/Backups.noindex").exists()
    with sqlite3.connect(snapshot / "private/qa.noindex/user-data/jarvis.db") as user_db:
        assert user_db.execute("SELECT value FROM preserved").fetchone()[0] == "user data"
    with sqlite3.connect(snapshot / "data/jarvis.db") as restored:
        assert restored.execute("SELECT value FROM memory").fetchone()[0] == "retain"


def test_checkpoint_keeps_index_and_worktree_unchanged_and_is_local(repo):
    sync, remote = repo
    (sync.root / "new.py").write_text("pass\n")
    (sync.root / "app.py").write_text("print('checkpoint')\n")
    index = (sync.git_dir / "index").read_bytes()
    head = git(sync.root, "rev-parse", "HEAD")
    ref = sync.checkpoint()
    assert (sync.git_dir / "index").read_bytes() == index
    assert git(sync.root, "rev-parse", "HEAD") == head
    assert "new.py" in git(sync.root, "ls-tree", "-r", "--name-only", ref)
    sync.sync("feat: checkpointed change")
    assert not git(remote, "for-each-ref", "refs/checkpoints")


def test_active_turn_and_quiet_period_prevent_incomplete_commits(repo):
    sync, _ = repo
    (sync.root / "app.py").write_text("print('in progress')\n")
    (sync.state / "active-turn").write_text("active")
    assert sync.sync(periodic=True)["reason"] == "Development turn active"
    (sync.state / "active-turn").unlink()
    assert sync.sync(periodic=True)["reason"] == "Waiting for files to settle"
    state = json.loads((sync.state / "quiet.json").read_text())
    state["since"] -= 200
    write_json(sync.state / "quiet.json", state)
    assert sync.sync(periodic=True)["status"] == "synced"


def test_periodic_restarts_quiet_period_when_untracked_file_is_edited(repo):
    sync, _ = repo
    file = sync.root / "new.py"
    file.write_text("pass\n")
    sync.sync(periodic=True)
    state = json.loads((sync.state / "quiet.json").read_text())
    state["since"] -= 200
    write_json(sync.state / "quiet.json", state)
    file.write_text("print('still editing')\n")
    assert sync.sync(periodic=True)["status"] == "deferred"


def test_wrong_remote_conflict_and_missing_scanner_fail_closed(repo):
    sync, _ = repo
    sync.cfg["origin"] = "https://example.invalid/wrong.git"
    with pytest.raises(Blocked, match="origin differs"):
        sync.sync()
    sync.cfg["origin"] = git(sync.root, "remote", "get-url", "origin")
    (sync.git_dir / "MERGE_HEAD").touch()
    with pytest.raises(Blocked, match="operation in progress"):
        sync.sync()
    (sync.git_dir / "MERGE_HEAD").unlink()
    sync.cfg["gitleaks"] = "/not-installed"
    with pytest.raises(Blocked, match="Gitleaks"):
        sync.sync()


@pytest.mark.parametrize(
    "original",
    [
        'model = "keep-me"\n[features]\na = true\n',
        'notify = ["existing", "arg"]\nmodel = "keep-me"\n',
        'notify = [\n "existing",\n "arg",\n]\nmodel = "keep-me"\n',
    ],
)
def test_notifier_install_preserves_all_other_settings(original):
    command = ["python", "/a path with spaces/notify.py", "notify"]
    result = tomllib.loads(replace_notify(original, command))
    assert result["notify"] == command
    assert {k: v for k, v in result.items() if k != "notify"} == {
        k: v for k, v in tomllib.loads(original).items() if k != "notify"
    }


def test_notify_keeps_existing_notifier_and_ignores_other_projects(repo, monkeypatch):
    sync, _ = repo
    sync.cfg["original_notify"] = ["old-notifier", "turn-ended"]
    write_json(sync.state / "settings.json", sync.cfg)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a[0]))
    payload = {"type": "agent-turn-complete", "cwd": "/another/project"}
    assert dispatch(payload, {**sync.cfg, "root": str(sync.root), "python": sys.executable}) == 0
    assert len(calls) == 1 and calls[0][:2] == ["old-notifier", "turn-ended"]


def test_notify_dispatches_scoped_completion(repo, monkeypatch):
    sync, _ = repo
    calls = []
    (sync.root / "scripts").mkdir()
    (sync.root / "scripts/git_sync.py").touch()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    assert (
        dispatch(
            {"type": "agent-turn-complete", "cwd": str(sync.root)},
            {**sync.cfg, "root": str(sync.root), "python": sys.executable},
        )
        == 0
    )
    assert calls[0][0][0][-1] == "complete"
    assert calls[0][1]["start_new_session"] is True


def test_local_config_has_priority_without_changing_public_defaults(tmp_path, monkeypatch):
    from backend.config import Settings

    public = tmp_path / "config.yaml"
    private = tmp_path / "config.local.yaml"
    public.write_text("language: en-US\nled:\n  enabled: false\n")
    private.write_text("language: ru-RU\nled:\n  enabled: true\n")
    monkeypatch.setitem(Settings.model_config, "yaml_file", (public, private))
    assert Settings().language == "ru-RU"
    assert Settings().led.enabled is True
    private.unlink()
    assert Settings().language == "en-US"


def test_committed_symlink_is_not_published(repo, tmp_path):
    sync, _ = repo
    path = tmp_path / "sensitive"
    path.write_text("sensitive")
    (sync.root / "alias").symlink_to(path)
    with pytest.raises(Blocked, match="Symlinks"):
        sync.sync()


def install_test_hooks(sync):
    source = Path(__file__).resolve().parents[1]
    (sync.root / "scripts").mkdir()
    shutil.copyfile(source / "scripts/git_sync.py", sync.root / "scripts/git_sync.py")
    shutil.copytree(source / ".githooks", sync.root / ".githooks")
    for hook in (sync.root / ".githooks").iterdir():
        hook.chmod(0o755)
    python = sync.root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    with (sync.root / ".gitignore").open("a") as out:
        out.write(".venv/\n")
    git(sync.root, "config", "core.hooksPath", str(sync.root / ".githooks"))


def test_real_git_hooks_publish_manual_commit_and_block_a_secret(repo):
    sync, remote = repo
    install_test_hooks(sync)
    (sync.root / "app.py").write_text("print('manual commit')\n")
    git(sync.root, "add", ".")
    git(sync.root, "commit", "-m", "fix: manually committed change")
    assert git(remote, "rev-parse", "main") == git(sync.root, "rev-parse", "HEAD")
    (sync.root / "app.py").write_text("REAL_SECRET_SENTINEL\n")
    git(sync.root, "add", ".")
    with sync.lock():
        result = subprocess.run(
            ["git", "commit", "-m", "must be rejected"], cwd=sync.root, capture_output=True
        )
    assert result.returncode != 0
    assert git(sync.root, "log", "-1", "--format=%s") == "fix: manually committed change"


def test_real_pre_push_hook_rejects_checkpoint_publication(repo):
    sync, remote = repo
    install_test_hooks(sync)
    git(sync.root, "add", ".")
    git(sync.root, "commit", "-m", "chore: install test hooks")
    ref = sync.checkpoint()
    with sync.lock():
        result = subprocess.run(
            ["git", "push", "origin", ref + ":refs/checkpoints/test"],
            cwd=sync.root,
            capture_output=True,
        )
    assert result.returncode != 0
    assert not git(remote, "for-each-ref", "refs/checkpoints")


def test_notifier_still_delegates_if_checkout_was_removed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))
    config = {
        "root": str(tmp_path / "removed"),
        "python": "/missing/python",
        "original_notify": ["original-notifier"],
    }
    assert dispatch({"type": "agent-turn-complete", "cwd": "/"}, config) == 0
    assert len(calls) == 1 and calls[0][0] == "original-notifier"


def test_fetch_timeout_keeps_commit_pending_for_retry(repo, monkeypatch):
    sync, _ = repo
    original = subprocess.run

    def run(args, **kwargs):
        if args[:2] == ["git", "fetch"]:
            raise subprocess.TimeoutExpired(args, 60)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    (sync.root / "app.py").write_text("print('timeout-safe')\n")
    result = sync.sync("fix: retain work through timeout")
    assert result["status"] == "pending_push"
    assert "timed out" in result["reason"]
    assert git(sync.root, "log", "-1", "--format=%s") == "fix: retain work through timeout"


def test_scoped_sync_leaves_concurrent_work_uncommitted(repo):
    sync, remote = repo
    (sync.root / "app.py").write_text("print('scoped fix')\n")
    (sync.root / "other.py").write_text("print('unfinished parallel work')\n")
    result = sync.sync("fix: selected change", paths=["app.py"])
    assert result["status"] == "synced"
    assert git(sync.root, "show", "--format=", "--name-only", "HEAD") == "app.py"
    assert "other.py" in git(sync.root, "status", "--porcelain")
    assert git(remote, "rev-parse", "main") == result["commit"]


def test_scoped_sync_refuses_unrelated_staged_work(repo):
    sync, _ = repo
    (sync.root / "other.py").write_text("print('other')\n")
    git(sync.root, "add", "other.py")
    with pytest.raises(Blocked, match="outside the scoped"):
        sync.sync("fix: scoped", paths=["app.py"])
    assert git(sync.root, "diff", "--cached", "--name-only") == "other.py"


def test_complete_refuses_foreign_changes_and_preserves_active_turn(repo, monkeypatch):
    from scripts import git_sync

    sync, remote = repo
    before = git(sync.root, "rev-parse", "HEAD")
    (sync.root / "app.py").write_text("print('QA fix')\n")
    (sync.root / "parallel.py").write_text("print('unfinished')\n")
    (sync.state / "active-turn").write_text("checkpoint")
    monkeypatch.setattr(git_sync, "Sync", lambda: sync)
    result = git_sync.main(
        ["complete", "-m", "fix: QA", "--path", "app.py", "--require-clean-unrelated"]
    )
    assert result == 1
    assert git(sync.root, "rev-parse", "HEAD") == before
    assert git(remote, "rev-parse", "main") == before
    assert not git(sync.root, "diff", "--cached", "--name-only")
    assert (sync.state / "active-turn").exists()
    assert json.loads((sync.state / "status.json").read_text())["status"] == "blocked"


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", "-A", ".env"])
def test_scoped_sync_cannot_escape_or_publish_private_paths(repo, path):
    sync, _ = repo
    (sync.root / "app.py").write_text("print('changed')\n")
    with pytest.raises(Blocked):
        sync.sync("fix: scoped", paths=[path])
