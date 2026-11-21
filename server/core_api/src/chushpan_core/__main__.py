"""Command line entry point used by the Windows Scheduled Task."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

from chushpan_core.app import configure_logging

logger = logging.getLogger("chushpan_core.supervisor")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Chushpan Core API")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    configure_logging("supervisor.log")
    package_root = Path(__file__).resolve().parents[2]
    delay = 5

    while True:
        started = time.monotonic()
        command = [
            sys.executable,
            "-m",
            "chushpan_core.server",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ]
        logger.info("Starting API worker on %s:%d", args.host, args.port)
        try:
            process = subprocess.Popen(
                command,
                cwd=package_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            _, stderr = process.communicate()
            runtime = time.monotonic() - started
            if runtime >= 300:
                delay = 5
            detail = (stderr or b"").decode("utf-8", errors="replace")[-2000:].strip()
            logger.error(
                "API worker exited with code %s after %.1fs; restart in %ds%s",
                process.returncode,
                runtime,
                delay,
                f"; stderr={detail}" if detail else "",
            )
        except OSError:
            logger.exception("Could not start the API worker; retry in %ds", delay)

        time.sleep(delay)
        delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
