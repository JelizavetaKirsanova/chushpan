"""Single API worker supervised by :mod:`chushpan_core`."""

from __future__ import annotations

import argparse

import uvicorn

from chushpan_core.app import app, configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Chushpan Core API worker")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    configure_logging()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
