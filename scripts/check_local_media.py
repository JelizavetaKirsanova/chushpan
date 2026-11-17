"""Opt-in isolated Chromium/WebKit regression for persistent local media.

Uses synthetic temporary files and loopback HTTP; never opens the installed app,
user profiles, microphone, devices, or external websites.
"""

from __future__ import annotations

import functools
import http.server
import json
import subprocess
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main():
    source = subprocess.check_output(
        [
            "node",
            "--input-type=module",
            "-e",
            "import fs from 'node:fs';import {stripTypeScriptTypes} from 'node:module';"
            "process.stdout.write(stripTypeScriptTypes(fs.readFileSync(process.argv[1],'utf8'),"
            "{mode:'transform'}).replace(/^export /gm,'')"
            "+';window.LocalMediaStore=LocalMediaStore;');",
            str(ROOT / "frontend/src/local_media.ts"),
        ],
        text=True,
    )
    reports = []
    with tempfile.TemporaryDirectory(prefix="chushpan-media-regression-") as folder:
        root = Path(folder)
        (root / "index.html").write_text('<input type="file">')
        payload = root / "synthetic.txt"
        payload.write_text("Synthetic media payload — проверка UTF-8")
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(QuietHandler, directory=folder)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                for name in ("chromium", "webkit"):
                    engine = getattr(playwright, name)
                    profile = root / name
                    for phase in ("legacy", "update", "reopen"):
                        context = engine.launch_persistent_context(str(profile), headless=True)
                        try:
                            page = context.new_page()
                            page.goto(f"http://127.0.0.1:{server.server_port}/")
                            page.add_script_tag(content=source)
                            page.add_script_tag(path=ROOT / "frontend/tests/local_media.browser.js")
                            if phase == "legacy":
                                page.locator("input").set_input_files(payload)
                                page.evaluate("mediaChecks.legacy()")
                            elif phase == "update":
                                page.evaluate("mediaChecks.update()")
                            else:
                                result = page.evaluate("mediaChecks.reopen()")
                                reports.append({"engine": name, **result})
                        finally:
                            context.close()
        finally:
            server.shutdown()
            server.server_close()
    print(json.dumps(reports))


if __name__ == "__main__":
    main()
