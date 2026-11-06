#!/usr/bin/env python3
"""Conservative, offline-capable Git sync. Uses only the Python standard library."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ORIGIN = "https://github.com/JelizavetaKirsanova/chushpan.git"
ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NAMES = {"config.local.yaml", "auth.json", "credentials.json"}
PRIVATE_DIRS = {"data", "private", "backups", ".venv", "venv", "node_modules"}


class Blocked(RuntimeError):
    """A human must resolve this condition; never bypass it automatically."""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        os.chmod(temporary, 0o600)
        json.dump(value, out, ensure_ascii=False, indent=2)
    temporary.replace(path)


def private_path(name: str) -> bool:
    p = Path(name)
    return (
        bool(set(p.parts) & PRIVATE_DIRS)
        or p.name in PRIVATE_NAMES
        or (p.name.startswith(".env") and p.name != ".env.example")
        or p.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3"}
        or p.name.endswith((".db-wal", ".db-shm"))
        or p.name.lower().startswith(("credentials", "oauth", "token"))
        and p.suffix == ".json"
        or name.startswith("docs/diagnostics/")
        or name == ".codex/config.toml"
    )


class Sync:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.git_dir = Path(self.git("rev-parse", "--absolute-git-dir").strip().decode())
        self.state = self.git_dir / "chushpan-sync"
        self.state.mkdir(exist_ok=True, mode=0o700)
        path = self.state / "settings.json"
        self.cfg = json.loads(path.read_text()) if path.exists() else {}

    def git(self, *args: str, env: dict | None = None, check: bool = True) -> bytes:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
            capture_output=True,
            timeout=120,
        )
        if check and result.returncode:
            # Never persist raw stderr: remote helpers can put credentials in it.
            raise Blocked(f"git {args[0]} failed (exit {result.returncode})")
        return result.stdout

    @contextlib.contextmanager
    def lock(self):
        with (self.state / "lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def status(self, status: str, **details: object) -> dict:
        value = {"at": dt.datetime.now(dt.UTC).isoformat(), "status": status, **details}
        write_json(self.state / "status.json", value)
        # Bounded, redacted operational history; never contains prompts or secret values.
        log = self.state / "events.jsonl"
        if log.exists() and log.stat().st_size > 512_000:
            log.replace(self.state / "events.previous.jsonl")
        with log.open("a", encoding="utf-8") as out:
            os.chmod(log, 0o600)
            out.write(json.dumps(value, ensure_ascii=False) + "\n")
        return value

    def ready(self) -> str:
        branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD").decode().strip()
        if branch != self.cfg.get("branch", "main"):
            raise Blocked("Automatic sync only runs on the configured branch")
        for marker in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
        ):
            if (self.git_dir / marker).exists():
                raise Blocked("Git operation in progress")
        if (self.git_dir / "index.lock").exists() or self.git("ls-files", "-u"):
            raise Blocked("Git index is locked or contains conflicts")
        expected = self.cfg.get("origin", ORIGIN)
        for flag in ([], ["--push"]):
            urls = self.git("remote", "get-url", *flag, "--all", "origin").decode().splitlines()
            if urls != [expected]:
                raise Blocked("origin differs from the configured destination")
        return branch

    def files(self) -> list[str]:
        return sorted(
            set(
                os.fsdecode(p)
                for p in self.git("ls-files", "-co", "--exclude-standard", "-z").split(b"\0")
                if p
            )
        )

    def guard_unrelated(self, paths: list[str] | None) -> None:
        """Honor an explicit request to publish only after concurrent work settles."""
        if not paths:
            raise Blocked("Clean-unrelated check requires explicit --path selections")
        changed = self.git("diff", "HEAD", "--name-only", "-z") + self.git(
            "ls-files", "--others", "--exclude-standard", "-z"
        )
        unrelated = sorted(
            {
                os.fsdecode(name)
                for name in changed.split(b"\0")
                if name
                and not any(
                    os.fsdecode(name) == p or os.fsdecode(name).startswith(p.rstrip("/") + "/")
                    for p in paths
                )
            }
        )
        if unrelated:
            raise Blocked(
                "Uncommitted changes outside the selected task: " + ", ".join(unrelated[:8])
            )

    def guard_paths(self, paths: list[str]) -> None:
        denied = [p for p in paths if private_path(p)]
        if denied:
            raise Blocked("Private files in Git: " + ", ".join(denied[:8]))
        for name in paths:
            p = self.root / name
            if p.is_symlink():
                raise Blocked("Symlinks require manual review before publication: " + name)

    def scan(self, mode: str, target: Path, *, history: bool = False) -> None:
        scanner = Path(self.cfg.get("gitleaks", ""))
        if not scanner.is_file():
            raise Blocked("Gitleaks is unavailable; publication disabled")
        config = self.state / "scanner.toml"
        config.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        config.chmod(0o600)
        with tempfile.TemporaryDirectory(prefix="scan-", dir=self.state) as directory:
            report = Path(directory) / "report.json"
            args = [
                str(scanner),
                mode,
                "--no-banner",
                "--redact",
                "--ignore-gitleaks-allow",
                "--config",
                str(config),
                "--gitleaks-ignore-path",
                str(Path(directory) / "none"),
                "--report-format=json",
                "--report-path",
                str(report),
            ]
            if history:
                args.append("--log-opts=--all")
            result = subprocess.run(
                [*args, str(target)],
                cwd=directory,
                capture_output=True,
                timeout=180,
                env={k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS")},
            )
            findings = json.loads(report.read_text()) if report.exists() else []
            if result.returncode or findings:
                # Only rule/path/line metadata. No secret, match, entropy or raw stderr.
                safe = [
                    {k: item.get(k) for k in ("RuleID", "File", "StartLine", "Commit")}
                    for item in findings
                ]
                write_json(self.state / "secret-findings.json", safe)
                if not findings:
                    raise Blocked(
                        f"Secret scanner failed (exit {result.returncode}); push disabled"
                    )
                raise Blocked(f"Secret scan blocked publication ({len(safe)} findings)")

    def audit_history(self) -> None:
        refs = self.git("for-each-ref", "--format=%(objectname)") + self.git("rev-parse", "HEAD")
        scanner = Path(self.cfg.get("gitleaks", ""))
        if not scanner.is_file():
            raise Blocked("Gitleaks is unavailable; publication disabled")
        fingerprint = hashlib.sha256(refs + scanner.read_bytes()).hexdigest()
        cached = self.state / "history-ok"
        if cached.exists() and cached.read_text() == fingerprint:
            return
        self.scan("git", self.root, history=True)
        cached.write_text(fingerprint)

    def audit_index(self) -> None:
        paths = [os.fsdecode(p) for p in self.git("ls-files", "-z").split(b"\0") if p]
        self.guard_paths(paths)
        # Scan the exact staged tree, not a possibly different working copy.
        with tempfile.TemporaryDirectory(prefix="index-", dir=self.state) as directory:
            for entry in self.git("ls-files", "--stage", "-z").split(b"\0"):
                if not entry:
                    continue
                metadata, raw = entry.split(b"\t", 1)
                mode, oid, stage = metadata.split()
                name = os.fsdecode(raw)
                if stage != b"0" or mode not in (b"100644", b"100755"):
                    raise Blocked("Unmerged files, symlinks or submodules need manual review")
                dest = Path(directory) / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(self.git("cat-file", "blob", oid.decode()))
            self.scan("dir", Path(directory))

    def backup(self) -> str:
        base = Path(self.cfg["backups"]).expanduser().resolve()
        if base.is_relative_to(self.root):
            raise Blocked("Private backups must live outside the repository")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        base.chmod(0o700)
        sources = {
            name: self.root / name
            for name in (
                ".env",
                "config.yaml",
                "config.local.yaml",
                "personality.md",
                "private",
                "data",
                "docs/diagnostics",
            )
        }
        for label, source in self.cfg.get("extra_backups", {}).items():
            sources["external/" + label] = Path(source).expanduser()
        skip_dirs = {"native", "Чушпан.app", "__pycache__", "node_modules"}
        with tempfile.TemporaryDirectory(prefix=".pending-", dir=base) as directory:
            staging = Path(directory)
            manifest = {}
            for label, source in sources.items():
                if not source.exists() or source.is_symlink():
                    continue
                paths = source.rglob("*") if source.is_dir() else [source]
                for path in paths:
                    relative = path.relative_to(source) if source.is_dir() else Path()
                    if set(relative.parts) & skip_dirs or path.is_symlink() or not path.is_file():
                        continue
                    if path.name.endswith(("-wal", "-shm", ".lock")):
                        continue
                    dest = staging / label / relative
                    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if path.suffix in {".db", ".sqlite", ".sqlite3"}:
                        with (
                            contextlib.closing(
                                sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                            ) as src,
                            contextlib.closing(sqlite3.connect(dest)) as out,
                        ):
                            src.backup(out)
                            out.execute("PRAGMA journal_mode=DELETE")
                            if out.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                                raise Blocked("SQLite backup integrity check failed")
                    else:
                        shutil.copyfile(path, dest)
                    dest.chmod(0o600)
                    manifest[str(dest.relative_to(staging))] = hashlib.sha256(
                        dest.read_bytes()
                    ).hexdigest()
            digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
            previous = self.state / "last-backup.json"
            if previous.exists():
                last = json.loads(previous.read_text())
                if last["digest"] == digest and Path(last["path"]).is_dir():
                    return last["path"]
            write_json(staging / "manifest.json", {"sha256": manifest, "digest": digest})
            for folder in staging.rglob("*"):
                if folder.is_dir():
                    folder.chmod(0o700)
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
            final = base / ("snapshot-" + stamp)
            staging.rename(final)
            write_json(previous, {"digest": digest, "path": str(final)})
        # Keep 48 recent snapshots plus one per day for 30 days. Initial backup stays.
        snapshots = sorted(base.glob("snapshot-*"), reverse=True)
        days = set()
        for index, path in enumerate(snapshots):
            day = path.name[9:17]
            keep = index < 48 or (day not in days and len(days) < 30)
            days.add(day)
            if not keep:
                shutil.rmtree(path)
        return str(final)

    def checkpoint(self) -> str:
        self.ready()
        self.guard_paths(self.files())
        with tempfile.TemporaryDirectory(prefix="checkpoint-", dir=self.state) as directory:
            env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
            self.git("read-tree", self.git("write-tree").decode().strip(), env=env)
            self.git("add", "-A", "--", ".", env=env)
            # The regular index and working tree remain untouched.
            original = os.environ.get("GIT_INDEX_FILE")
            try:
                os.environ.update(env)
                self.audit_index()
            finally:
                if original is None:
                    os.environ.pop("GIT_INDEX_FILE", None)
                else:
                    os.environ["GIT_INDEX_FILE"] = original
            tree = self.git("write-tree", env=env).decode().strip()
            stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
            commit = (
                self.git(
                    "commit-tree", tree, "-p", "HEAD", "-m", "checkpoint: before development turn"
                )
                .decode()
                .strip()
            )
            ref = "refs/checkpoints/" + stamp
            self.git("update-ref", ref, commit)
            return ref

    def push(self) -> dict:
        branch = self.ready()
        self.audit_history()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        # Fetch only the configured branch. Never merge, rebase, reset or force-push.
        try:
            result = subprocess.run(
                ["git", "fetch", "--no-tags", "origin", branch],
                cwd=self.root,
                env=env,
                capture_output=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return self.status("pending_push", reason="Fetch timed out; commits kept")
        if result.returncode:
            return self.status(
                "pending_push", reason="Network or GitHub authentication unavailable"
            )
        remote = self.git("rev-parse", "FETCH_HEAD").strip().decode()
        local = self.git("rev-parse", "HEAD").strip().decode()
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", remote, local],
            cwd=self.root,
            capture_output=True,
        )
        if ancestry.returncode:
            raise Blocked("Remote has new commits; integrate them manually before publishing")
        if remote != local:
            try:
                result = subprocess.run(
                    ["git", "push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}"],
                    cwd=self.root,
                    env=env,
                    capture_output=True,
                    timeout=90,
                )
            except subprocess.TimeoutExpired:
                return self.status("pending_push", reason="Push timed out; commits kept")
            if result.returncode:
                return self.status(
                    "pending_push", reason="Push rejected or unavailable; commits kept"
                )
        # A real read-back is required before claiming synchronization.
        try:
            verified = self.git("ls-remote", "origin", f"refs/heads/{branch}").split()
        except (Blocked, subprocess.TimeoutExpired):
            return self.status("pending_push", reason="Remote HEAD verification unavailable")
        if not verified or verified[0].decode() != local:
            return self.status("pending_push", reason="Remote HEAD could not be verified")
        self.git("update-ref", f"refs/remotes/origin/{branch}", local)
        self.git("config", f"branch.{branch}.remote", "origin")
        self.git("config", f"branch.{branch}.merge", f"refs/heads/{branch}")
        return self.status("synced", commit=local)

    def sync(
        self, message: str | None = None, *, periodic: bool = False, paths: list[str] | None = None
    ) -> dict:
        self.ready()
        backup = self.backup()
        dirty = self.git("status", "--porcelain=v1", "-z")
        if dirty and periodic:
            if (self.state / "active-turn").exists():
                return self.status("deferred", reason="Development turn active", backup=backup)
            metadata = [
                (name, (self.root / name).stat().st_mtime_ns, (self.root / name).stat().st_size)
                for name in self.files()
                if (self.root / name).is_file()
            ]
            signature = hashlib.sha256(
                dirty + self.git("diff", "HEAD") + json.dumps(metadata).encode()
            ).hexdigest()
            pending = self.state / "quiet.json"
            before = json.loads(pending.read_text()) if pending.exists() else {}
            if before.get("signature") != signature:
                write_json(pending, {"signature": signature, "since": time.time()})
                return self.status("deferred", reason="Waiting for files to settle", backup=backup)
            if time.time() - before["since"] < 120:
                return self.status("deferred", reason="Waiting for files to settle", backup=backup)
        self.audit_history()
        if dirty:
            self.guard_paths(self.files())
            if paths:
                if any(
                    not p or Path(p).is_absolute() or ".." in Path(p).parts or p.startswith("-")
                    for p in paths
                ):
                    raise Blocked("Scoped sync requires relative project paths")
                self.guard_paths(paths)

                def selected(name):
                    return any(name == p or name.startswith(p.rstrip("/") + "/") for p in paths)

                staged = self.git("diff", "--cached", "--name-only", "-z").split(b"\0")
                if any(name and not selected(os.fsdecode(name)) for name in staged):
                    raise Blocked("Index includes paths outside the scoped change")
            index = self.git_dir / "index"
            original_index = index.read_bytes() if index.exists() else None
            try:
                self.git("add", "-A", "--", *(paths or ["."]))
                self.audit_index()
            except BaseException:
                if original_index is None:
                    index.unlink(missing_ok=True)
                else:
                    index.write_bytes(original_index)
                raise
            changes = self.git("diff", "--cached", "--name-only").decode().splitlines()
            if changes:
                areas = sorted({"/".join(Path(p).parts[:2]) for p in changes})
                title = message or ("chore: сохранить изменения " + ", ".join(areas[:4]))
                if "\n" in title or len(title) > 200:
                    raise Blocked("Commit title must be one line, at most 200 characters")
                body = "Changed files:\n" + "\n".join(changes)
                self.git("commit", "-m", title, "-m", body)
        return self.push()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "sync",
            "periodic",
            "push",
            "backup",
            "checkpoint",
            "begin",
            "complete",
            "check-index",
            "check-push",
            "status",
        ],
    )
    parser.add_argument("-m", "--message")
    parser.add_argument(
        "--require-clean-unrelated",
        action="store_true",
        help="Refuse commit and push while paths outside this task have uncommitted changes",
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        help="Commit only these project paths; retain all safety checks",
    )
    args = parser.parse_args(argv)
    sync = Sync()
    if args.command == "status":
        path = sync.state / "status.json"
        print(path.read_text() if path.exists() else '{"status":"not_run"}')
        return 0
    try:
        if args.command == "check-index":
            sync.audit_history()
            sync.audit_index()
            return 0
        elif args.command == "check-push":
            sync.ready()
            sync.audit_history()
            sync.guard_paths(
                [
                    os.fsdecode(p)
                    for p in sync.git("ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0")
                    if p
                ]
            )
            for line in sys.stdin:
                local_ref, local_oid, remote_ref, remote_oid = line.split()
                if remote_ref != "refs/heads/" + sync.cfg.get("branch", "main"):
                    raise Blocked("Only the configured branch may be published automatically")
                if set(local_oid) == {"0"}:
                    raise Blocked("Deleting remote refs is disabled")
                if local_ref not in {"HEAD", "refs/heads/" + sync.cfg.get("branch", "main")}:
                    raise Blocked("Only the current branch HEAD may be published")
                if local_oid != sync.git("rev-parse", "HEAD").decode().strip():
                    raise Blocked("Push source differs from the audited HEAD")
                if set(remote_oid) != {"0"}:
                    result = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", remote_oid, local_oid],
                        cwd=sync.root,
                        capture_output=True,
                    )
                    if result.returncode:
                        raise Blocked("Non-fast-forward push is disabled")
            return 0
        with sync.lock() as acquired:
            if not acquired:
                return 0
            if args.command == "begin":
                sync.backup()
                ref = sync.checkpoint()
                (sync.state / "active-turn").write_text(ref)
                result = sync.status("working", checkpoint=ref)
            elif args.command == "checkpoint":
                result = sync.status("checkpoint", ref=sync.checkpoint())
            elif args.command == "backup":
                result = sync.status("backup", path=sync.backup())
            elif args.command == "push":
                sync.backup()
                result = sync.push()
            else:
                if args.require_clean_unrelated:
                    sync.guard_unrelated(args.paths)
                if args.command == "complete":
                    (sync.state / "active-turn").unlink(missing_ok=True)
                result = sync.sync(
                    args.message, periodic=args.command == "periodic", paths=args.paths
                )
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["status"] in {"blocked", "pending_push"} else 0
    except (Blocked, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        # Exception messages from subprocesses are intentionally not included.
        reason = str(exc) if isinstance(exc, Blocked) else type(exc).__name__
        print(json.dumps(sync.status("blocked", reason=reason), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
