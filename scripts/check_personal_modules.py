"""Isolated browser acceptance server. Temporary DB and mocked native transport only."""

import argparse
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import uvicorn
from PIL import Image, ImageDraw

from backend.config import Settings
from backend.hud.bus import HudBus
from backend.hud.server import create_app
from backend.mac import MacDesktop
from backend.memory.store import MemoryStore
from backend.personal_modules import PersonalModules
from backend.sync.identity import KeychainIdentity
from backend.tools import build_registry


class IsolatedSettings(Settings):
    model_config = {**Settings.model_config, "yaml_file": None, "yaml_file_encoding": None}

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **kwargs):
        return (init_settings,)


class MockDesktop(MacDesktop):
    def __init__(self, settings):
        super().__init__(settings)
        self.bridge.request = AsyncMock(side_effect=self.native)

    async def native(self, operation, **kwargs):
        if operation == "desktop_apps":
            return {
                "apps": [
                    {
                        "name": "Test Editor",
                        "bundle": "example.test.editor",
                        "pid": 1,
                        "active": True,
                    }
                ]
            }
        if operation == "permission_status":
            return {"accessibility": "granted"}
        if operation == "desktop_capture_status":
            return {"granted": True}
        if operation == "desktop_ocr":
            return {"lines": [{"text": "TEST FIXTURE: local text recognition"}]}
        if operation == "desktop_activity_status":
            return {"idle_s": 0}
        raise ValueError("Native operation unavailable in isolated acceptance server")

    async def screenshot(self, mode="screen"):
        path = self.s.data_path / "test-screen.png"
        image = Image.new("RGB", (800, 450), "#23192e")
        ImageDraw.Draw(image).text(
            (30, 30), "TEST FIXTURE ONLY - NO REAL SCREEN CAPTURE", fill="white"
        )
        image.save(path)
        return {"ok": True, "path": str(path), "message": "Test fixture only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument(
        "--mobile", action="store_true", help="Loopback TLS, ephemeral test key, no Bonjour"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="chushpan-modules-ui-") as directory:
        folder = Path(directory)
        settings = IsolatedSettings(
            language="ru-RU",
            assistant_name="Чушпан",
            tools={"enabled": ["system", "memory", "clock"]},
            led={"enabled": False},
            location={"timezone": "Europe/Tallinn"},
        )
        # data_path is environment-backed in production; no private environment is loaded here.
        import os

        os.environ["JARVIS_DATA_DIR"] = str(folder)
        store = MemoryStore(folder / "test.sqlite")
        bus = HudBus()
        tools = build_registry(settings, store=store, desktop=MockDesktop(settings))
        modules = None

        @asynccontextmanager
        async def lifespan(app):
            nonlocal modules
            modules = PersonalModules(settings, store, tools, bus)
            if args.mobile:
                # Generate the CA through an injected in-memory Security API. No real
                # Keychain read/write and no local-network advertisement in this test.
                import sys
                from types import SimpleNamespace
                from unittest.mock import patch

                from backend.brain.llm import ChatChunk

                class Model:
                    cloud_context = False

                    async def chat_stream(self, messages, tools=None):
                        yield ChatChunk(
                            content="TEST REPLY: mobile transport confirmed.", done=True
                        )

                    async def aclose(self):
                        pass

                names = [
                    "kSecClass",
                    "kSecClassGenericPassword",
                    "kSecAttrService",
                    "kSecAttrAccount",
                    "kSecReturnData",
                    "kSecValueData",
                    "kSecAttrAccessible",
                    "kSecAttrAccessibleWhenUnlockedThisDeviceOnly",
                ]
                security = SimpleNamespace(
                    **{name: name for name in names},
                    errSecSuccess=0,
                    errSecItemNotFound=-25300,
                    SecItemCopyMatching=lambda *args: (-25300, None),
                    SecItemAdd=lambda *args: (0, None),
                )
                with patch.dict(sys.modules, {"Security": security}):
                    material = KeychainIdentity().read()
                modules.sync.identity = SimpleNamespace(read=lambda: material)
                modules.sync.model_factory = lambda settings: Model()
                await modules.sync.start("127.0.0.1", 8877, advertise=False)
                nonce = "test-invitation-" + __import__("secrets").token_hex(16)
                modules.sync.invite = {
                    "nonce": nonce,
                    "url": modules.sync.url + "/#pair=" + nonce,
                    "expires": __import__("time").time() + 120,
                    "qr": "",
                }
                print(json.dumps({"mobile_url": modules.sync.invite["url"]}), flush=True)
            bus.publish(
                {
                    "type": "hello",
                    "name": "Чушпан · проверка",
                    "language": "ru-RU",
                    "model": "Изолированный тест",
                    "owner": None,
                    "place": None,
                    "tools": tools.names,
                    "wake_word": False,
                }
            )
            bus.publish({"type": "state", "state": "sleeping"})
            try:
                yield
            finally:
                await modules.close()
                store.close()

        app = create_app(bus, {f"http://127.0.0.1:{args.port}"})
        app.router.lifespan_context = lifespan
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
