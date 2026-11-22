"""SSH-only administrative operations for the Windows Core Server."""

from __future__ import annotations

import argparse
import getpass
import json
import re
from datetime import UTC, datetime

from chushpan_core.identity import DEFAULT_SCOPES, DeviceRegistry, ensure_tls_certificate
from chushpan_core.memory import MemoryStore
from chushpan_core.paths import core_root, database_path, memory_path
from chushpan_core.secrets import save_openai_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    certificate = commands.add_parser(
        "ensure-tls", help="Create or validate the pinned TLS identity"
    )
    certificate.add_argument("--host", required=True)
    invite = commands.add_parser("invite", help="Issue a one-time device pairing invitation")
    invite.add_argument("--host", required=True)
    invite.add_argument("--name", required=True)
    invite.add_argument("--kind", choices=sorted(DEFAULT_SCOPES), required=True)
    invite.add_argument("--scope", action="append", dest="scopes")
    invite.add_argument("--expires", type=int, default=300)
    commands.add_parser(
        "set-openai-key", help="Provision an OpenAI key over an interactive SSH terminal"
    )
    commands.add_parser("backup-memory", help="Create and verify a Core memory backup")
    verify_backup = commands.add_parser(
        "verify-memory-backup", help="Verify a named backup in the protected backup directory"
    )
    verify_backup.add_argument("--name", required=True)
    args = parser.parse_args()

    try:
        if args.command == "set-openai-key":
            key = getpass.getpass("OpenAI API key (input hidden): ")
            if not key:
                parser.error("No key entered.")
            save_openai_key(key)
            print("OpenAI key stored with Windows DPAPI; it was not displayed or logged.")
            return 0
        if args.command == "backup-memory":
            directory = core_root() / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            name = "memory-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3"
            store = MemoryStore(memory_path())
            try:
                path = store.backup_to(directory / name)
            finally:
                store.close()
            print(json.dumps({"backup": str(path), **MemoryStore.verify_backup(path)}))
            return 0
        if args.command == "verify-memory-backup":
            if not re.fullmatch(r"memory-[0-9]{8}T[0-9]{6}Z\.sqlite3", args.name):
                parser.error("Backup name has an unsupported format.")
            directory = (core_root() / "backups").resolve()
            path = directory / args.name
            if path.resolve().parent != directory or not path.is_file():
                parser.error("The named backup is not in the protected backup directory.")
            print(json.dumps({"backup": str(path), **MemoryStore.verify_backup(path)}))
            return 0
        tls = ensure_tls_certificate(args.host)
        if args.command == "ensure-tls":
            print(json.dumps({"host": args.host, "fingerprint": tls["fingerprint"]}))
            return 0
        registry = DeviceRegistry(database_path())
        try:
            result = registry.issue_invite(
                args.name,
                args.kind,
                scopes=args.scopes,
                ttl_seconds=args.expires,
            )
        finally:
            registry.close()
        print(
            json.dumps(
                {
                    **result,
                    "host": args.host,
                    "tls_fingerprint": tls["fingerprint"],
                    "expires_in_seconds": args.expires,
                },
                ensure_ascii=False,
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Core administration failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
