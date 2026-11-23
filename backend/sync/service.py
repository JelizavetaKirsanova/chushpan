"""Local HTTPS companion with mutual pairing, signed requests and expiring commands."""

import asyncio
import base64
import contextlib
import errno
import hashlib
import ipaddress
import json
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from backend.brain.privacy import redact
from backend.center import Center, rows, text
from backend.context import (
    MAC_CAPABILITIES,
    DeviceContext,
    RequestContext,
    request_scope,
    scenario_selection,
)
from backend.memory.store import _now
from backend.sync.identity import KeychainIdentity, certificates, public_key, verify
from backend.sync.store import CAPABILITIES, CATEGORIES, SyncStore

CLIENT = Path(__file__).parent / "client"


class LocalServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield  # The desktop process owns its signal handlers.


def lan_address(value):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise ValueError("Укажи локальный IPv4-адрес этого Mac.") from None
    networks = (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "100.64.0.0/10",  # Tailscale's CGNAT range; never a public listener.
    )
    local = address.version == 4 and any(address in ipaddress.ip_network(net) for net in networks)
    if not local:
        raise ValueError("Нужен локальный IPv4 или адрес Tailscale. Публичный интерфейс запрещён.")
    return str(address)


def discover_lan_address():
    """Return the preferred private IPv4 without sending a network request."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("192.0.2.1", 9))  # UDP connect only asks the OS for its route.
            address = route.getsockname()[0]
            parsed = ipaddress.ip_address(address)
            if address != "0.0.0.0" and not parsed.is_loopback:
                if parsed not in ipaddress.ip_network("100.64.0.0/10"):
                    return lan_address(address)
    except (OSError, ValueError):
        pass

    ipconfig = "/usr/sbin/ipconfig"
    if not Path(ipconfig).is_file():
        return None
    names = [name for _, name in socket.if_nameindex() if name.startswith(("en", "bridge", "eth"))][
        :12
    ]
    names.sort(key=lambda name: (name not in {"en0", "en1", "en2"}, name))
    for name in names:
        if name.startswith(("lo", "utun", "awdl", "llw")):
            continue
        try:
            result = subprocess.run(
                [ipconfig, "getifaddr", name], capture_output=True, text=True, timeout=1
            )
            address = result.stdout.strip()
            if result.returncode == 0:
                parsed = ipaddress.ip_address(address)
                if (
                    parsed.version == 4
                    and not parsed.is_loopback
                    and parsed not in ipaddress.ip_network("100.64.0.0/10")
                ):
                    return lan_address(address)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return None


def discover_tailscale_address():
    executable = shutil.which("tailscale")
    if not executable and Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale").is_file():
        executable = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "ip", "-4"], capture_output=True, text=True, timeout=2)
        for value in result.stdout.splitlines():
            try:
                address = ipaddress.ip_address(value.strip())
                if address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"):
                    return lan_address(str(address))
            except ValueError:
                continue
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def pairing_code(challenge, identity, fingerprint):
    raw = f"{challenge}{identity}{fingerprint}".encode()
    return str(int(hashlib.sha256(raw).hexdigest()[:12], 16) % 1_000_000).zfill(6)


def connection_type(address):
    try:
        parsed = ipaddress.ip_address(address)
    except (ValueError, TypeError):
        return "local"
    return (
        "tailscale"
        if parsed.version == 4 and parsed in ipaddress.ip_network("100.64.0.0/10")
        else "local"
    )


class DeviceSync(Center):
    channel = "device_sync"

    def __init__(self, settings, memory, registry, bus, *, identity=None, model_factory=None):
        self.s, self.memory, self.tools = settings, memory, registry
        self.store = SyncStore(memory)
        self.identity = identity or KeychainIdentity()
        self.model_factory = model_factory
        self.server, self.server_task, self.bonjour, self.listener = None, None, None, None
        self.listeners, self.network_watch_task = [], None
        self.url, self.fingerprint, self.ca = "", "", None
        self.bound_hosts, self.allowed_hosts, self.allowed_origins, self.service_hostname = (
            [],
            set(),
            set(),
            "",
        )
        self.invite, self.pending = None, {}
        self.failure = None
        self.started_at = None
        self.endpoint = memory.preference("sync", "endpoint") or {"host": "", "port": 8766}
        self.tailnet_host = ""
        self.commands, self.rates = {}, defaultdict(deque)
        self.pair_attempts = deque()
        self.bind(bus)
        # A crashed command is uncertain and must never be replayed after restart.
        with memory.memory_transaction():
            memory.db.execute(
                "UPDATE sync_commands SET status='interrupted' WHERE status='running'"
            )

    def snapshot(self):
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        invite = self.invite if self.invite and self.invite["expires"] > now else None
        running = bool(
            self.server and self.server.started and self.server_task and not self.server_task.done()
        )
        failure = self.failure or (
            "TLS-сервер неожиданно остановился. Проверь адрес и запусти его снова."
            if self.server_task
            and self.server_task.done()
            and self.server
            and not self.server.should_exit
            else None
        )
        return {
            "running": running,
            "error": failure,
            "endpoint": self.endpoint,
            "tailnet_host": self.tailnet_host,
            "url": self.url if running else "",
            "fingerprint": self.fingerprint,
            "invite": invite if running else None,
            "pending": [{k: v for k, v in p.items() if k != "key"} for p in self.pending.values()],
            "peers": [
                {
                    **p,
                    "online": bool(
                        running
                        and self.started_at is not None
                        and (p["last_seen"] or 0) >= self.started_at
                        and p["online"]
                    ),
                }
                for p in self.store.peers()
            ],
            "categories": CATEGORIES,
            "capabilities": sorted(CAPABILITIES),
            "history": rows(
                self.memory.db,
                "SELECT peer_id,command_id,operation,created_at,expires_at,status "
                "FROM sync_commands ORDER BY created_at DESC LIMIT 100",
            ),
            "conflicts": rows(
                self.memory.db,
                "SELECT * FROM sync_conflicts WHERE status='pending' ORDER BY id DESC LIMIT 100",
            ),
        }

    def rate(self, address):
        now = time.monotonic()
        # Bound both the number of clients and their request histories.
        if len(self.rates) > 128:
            self.rates.clear()
        queue = self.rates[address]
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= 120:
            raise HTTPException(429, "Слишком много запросов. Подожди минуту.")
        queue.append(now)

    def application(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.middleware("http")
        async def secure_headers(request, call_next):
            allowed = self.allowed_hosts or {self.url.removeprefix("https://")}
            if request.headers.get("host", "").lower() not in allowed:
                return Response(status_code=403)
            response = await call_next(request)
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
                    "style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                    "form-action 'self'; frame-ancestors 'none'",
                }
            )
            return response

        @app.get("/")
        async def home():
            return FileResponse(CLIENT / "index.html", media_type="text/html")

        @app.get("/client.js")
        async def script():
            return FileResponse(CLIENT / "client.js", media_type="text/javascript")

        @app.get("/client.css")
        async def style():
            return FileResponse(CLIENT / "client.css", media_type="text/css")

        @app.get("/api/certificate")
        async def certificate():
            # The CA is public. Mobile clients fetch it during pairing and compare its
            # SHA-256 fingerprint with the value shown in the authenticated Mac UI.
            # Commands and personal data still require the signed device protocol below.
            if not self.ca:
                return Response(status_code=503)
            return Response(
                self.ca,
                media_type="application/pkix-cert",
                headers={"Cache-Control": "no-store"},
            )

        @app.post("/api/{operation}")
        async def api(operation: str, request: Request):
            try:
                client_address = request.client.host if request.client else "unknown"
                self.rate(client_address)
                if (
                    request.headers.get("origin") not in self.allowed_origins | {self.url}
                    or request.headers.get("content-type", "").split(";")[0] != "application/json"
                ):
                    raise HTTPException(403)
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 131072:
                        raise HTTPException(413)
                body = raw.decode("utf-8")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("Нужен объект запроса.")
                device = request.headers.get("x-device", "")
                timestamp, nonce = (
                    request.headers.get("x-time", ""),
                    request.headers.get("x-nonce", ""),
                )
                if len(timestamp) > 16 or abs(time.time() - int(timestamp)) > 60:
                    raise ValueError("Время запроса устарело. Проверь часы устройства.")
                if operation == "pair":
                    key, jwk, identity = public_key(data.get("key"))
                    if device != identity:
                        raise ValueError("Ключ не соответствует устройству.")
                elif operation == "pair_status" and device in self.pending:
                    jwk = self.pending[device]["key"]
                else:
                    jwk = self.store.peer(device, connected=operation != "reconnect")["public_key"]
                verify(
                    jwk,
                    request.headers.get("x-signature", ""),
                    f"/api/{operation}\n{timestamp}\n{nonce}\n{body}",
                )
                self.store.nonce(device, nonce)
                if operation == "pair":
                    return self.pair(data, device, jwk)
                if operation == "pair_status":
                    if device in self.pending:
                        pending = self.pending[device]
                        if pending["expires"] < time.time():
                            self.pending.pop(device)
                            raise ValueError("Сопряжение истекло.")
                        return {"status": "pending", "code": pending["code"]}
                    self.store.peer(device)
                    return {"status": "trusted", "id": device}
                if operation == "reconnect":
                    with self.memory.memory_transaction():
                        self.memory.db.execute(
                            "UPDATE sync_peers SET disconnected=0 WHERE id=?", (device,)
                        )
                peer = self.store.peer(device)
                with self.memory.memory_transaction():
                    self.memory.db.execute(
                        "UPDATE sync_peers SET last_seen=?,connection_type=? WHERE id=?",
                        (time.time(), connection_type(client_address), device),
                    )
                if operation in {"status", "reconnect"}:
                    await self.refresh_mobile_device_state(peer)
                    return self.remote_status(peer)
                if operation == "command":
                    return self.submit(peer, data)
                if operation == "result":
                    row = self.memory.db.execute(
                        "SELECT status,result FROM sync_commands WHERE peer_id=? AND command_id=?",
                        (device, data.get("id")),
                    ).fetchone()
                    if not row:
                        raise ValueError("Команда не найдена.")
                    return {
                        "status": row["status"],
                        "result": json.loads(row["result"]) if row["result"] else None,
                    }
                if operation == "sync":
                    return self.sync_data(peer)
                if operation == "memory_update":
                    return self.propose_memory(peer, data)
                raise HTTPException(404)
            except HTTPException:
                raise
            except (ValueError, TypeError, KeyError, UnicodeError):
                return JSONResponse(
                    {
                        "error": "Запрос отклонён: проверь доверие устройства, подпись, срок "
                        "действия и выбранные разрешения."
                    },
                    status_code=403,
                )
            except Exception:
                return JSONResponse(
                    {"error": "Локальный сервис временно недоступен."}, status_code=503
                )

        return app

    def pair(self, value, identity, jwk):
        now = time.time()
        self.pending = {key: item for key, item in self.pending.items() if item["expires"] > now}
        while self.pair_attempts and self.pair_attempts[0] < now - 60:
            self.pair_attempts.popleft()
        if len(self.pair_attempts) >= 20:
            raise ValueError("Слишком много запросов на сопряжение. Подожди минуту.")
        if value.get("kind") not in {"iphone", "ipad"} or len(self.pending) >= 8:
            raise ValueError("Некорректное устройство или слишком много запросов.")
        self.pair_attempts.append(now)
        name = text(value.get("name"), 80)
        challenge = value.get("challenge")
        if (
            isinstance(challenge, str)
            and 24 <= len(challenge) <= 100
            and all(
                character.isascii() and (character.isalnum() or character in "-_=")
                for character in challenge
            )
        ):
            code = pairing_code(challenge, identity, self.fingerprint)
        else:
            invite = self.invite
            if (
                not invite
                or invite["expires"] < time.time()
                or not secrets.compare_digest(str(value.get("invite", "")), invite["nonce"])
            ):
                raise ValueError("Приглашение недействительно или отсутствует проверочный вызов.")
            self.invite = None  # A legacy QR invitation can bind only one public key.
            code = pairing_code(invite["nonce"], identity, self.fingerprint)
        self.pending[identity] = {
            "id": identity,
            "name": name,
            "kind": value["kind"],
            "key": jwk,
            "code": code,
            "expires": time.time() + 120,
        }
        self.publish(
            data=self.snapshot(), message="Сравни код на обоих устройствах и подтверди сопряжение."
        )
        return {"status": "pending", "id": identity, "code": code}

    async def refresh_mobile_device_state(self, peer):
        """Refresh safe readbacks through the Mac before returning mobile controls."""
        if "devices" not in peer["capabilities"] or not self.tools.devices:
            return
        enabled = getattr(self.tools, "enabled", None)
        if not callable(enabled):
            return
        device = DeviceContext(
            peer["id"],
            peer["kind"],
            "sync-" + peer["id"],
            frozenset({"mac.system", "mac.media"}),
            True,
            "local-mac",
        )
        with request_scope(RequestContext(device, "Покажи состояние Mac")):
            for name, arguments in (
                ("get_system_status", {"topic": "volume"}),
                ("get_brightness", {}),
            ):
                if enabled(name):
                    try:
                        await self.tools.call(name, arguments)
                    except Exception:
                        # A failed read must not hide saved devices or erase last known state.
                        pass
            device_service = self.tools.devices
            if enabled("device_command"):
                for item in device_service.store.list():
                    if item.kind != "external_display":
                        continue
                    try:
                        with request_scope(
                            RequestContext(device, "Управляй выбранным устройством")
                        ):
                            await self.tools.call(
                                "device_command", {"target": item.id, "action": "inspect"}
                            )
                    except Exception:
                        # Inspection is read-only; retain metadata and the last saved state.
                        pass

    def remote_status(self, peer):
        scenarios = []
        if "scenario" in peer["capabilities"] and self.tools.automations:
            scenarios = [
                {
                    "id": s.id,
                    "name": s.name,
                    "revision": s.revision,
                    "variants": [
                        {
                            "id": variant.id,
                            "name": variant.name,
                            "place_id": variant.place_id,
                            "enabled": variant.enabled,
                        }
                        for variant in s.variants
                        if variant.enabled
                    ],
                    "variables": [
                        {"name": variable.name, "kind": variable.kind}
                        for variable in getattr(s, "variables", [])
                    ],
                }
                for s in self.tools.automations.store.list()
                if s.enabled
            ]
        devices = []
        if "devices" in peer["capabilities"] and self.tools.devices:
            fields = (
                "id",
                "revision",
                "name",
                "kind",
                "protocol",
                "endpoint",
                "place_id",
                "room",
                "state",
                "sent",
                "last_contact",
                "state_at",
                "result_status",
                "favorites",
                "aliases",
                "favorite",
                "auto_connect",
                "ble_name",
                "features",
                "ble",
                "preview_color",
                "connection",
                "connection_error",
                "control_reason",
                "usable",
                "reason",
                "busy",
            )
            snapshot = self.tools.devices.snapshot()
            places = {
                place.get("id"): place.get("name")
                for place in snapshot.get("places", [])
                if isinstance(place, dict)
            }
            seen = set()
            for item in snapshot["items"]:
                identifier = item.get("id")
                if not isinstance(identifier, str) or identifier in seen:
                    continue
                seen.add(identifier)
                remote = {key: item[key] for key in fields if key in item}
                if item.get("place_id") in places:
                    remote["place_name"] = places[item["place_id"]]
                connection = item.get("connection", "unknown")
                state = item.get("state") if isinstance(item.get("state"), dict) else {}
                has_mac_readback = item.get("kind") == "mac" and any(
                    type(state.get(key)) is int for key in ("brightness", "volume_percent")
                )
                if item.get("usable") is False or (connection == "error" and not has_mac_readback):
                    availability = "remote-unavailable"
                elif connection == "disconnected":
                    availability = "offline"
                elif connection == "observed" or has_mac_readback:
                    availability = "online"
                else:
                    availability = "unknown"
                remote["availability"] = availability
                devices.append(remote)
        return {
            "status": "online",
            "name": peer["name"],
            "capabilities": peer["capabilities"],
            "categories": peer["categories"],
            "mobile_chat": {
                "provider": "openai",
                "model": self.s.openai.model,
                "fallback": False,
                "desktop_provider": self.s.llm.provider,
            },
            "scenarios": scenarios,
            "devices": devices,
            "history": rows(
                self.memory.db,
                "SELECT command_id,operation,created_at,status FROM sync_commands "
                "WHERE peer_id=? ORDER BY created_at DESC LIMIT 30",
                (peer["id"],),
            ),
        }

    def submit(self, peer, value):
        operation = value.get("operation")
        required = {
            "chat": "chat",
            "scenario": "scenario",
            "device": "devices",
            "open_app": "open_app",
            "media_add": "media_library",
            "media_update": "media_library",
            "media_delete": "media_library",
            "calendar_day": None,
            "confirm": None,
            "cancel": None,
        }.get(operation, "unsupported")
        if required == "unsupported" or (required and required not in peer["capabilities"]):
            raise ValueError("Эта команда не разрешена на Mac.")
        if operation == "calendar_day" and "calendar" not in peer["categories"]:
            raise ValueError("Календарь Mac не разрешён этому устройству.")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 12000:
            raise ValueError("Некорректные аргументы команды.")
        digest = (
            operation
            + ":"
            + hashlib.sha256(
                json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        )
        previous = self.store.reserve(peer["id"], value.get("id"), digest, value.get("expires"))
        if previous:
            return {"status": previous["status"], "id": value["id"]}
        if len(self.commands) >= 8:
            self.store.finish(
                peer["id"],
                value["id"],
                "rejected",
                {"error": "Очередь занята; повторного выполнения не будет."},
            )
            return {"status": "rejected", "id": value["id"]}
        task = asyncio.create_task(self.execute(peer["id"], value))
        key = (peer["id"], value["id"])
        self.commands[key] = task
        task.add_done_callback(lambda _: self.commands.pop(key, None))
        return {"status": "running", "id": value["id"]}

    async def execute(self, identity, value):
        result, status = {}, "failed"
        try:
            async with asyncio.timeout(min(90, value["expires"] - time.time())):
                peer = self.store.peer(identity)
                if time.time() >= value["expires"]:
                    raise ValueError("Команда устарела.")
                operation, args = value["operation"], value.get("arguments", {})
                required = {
                    "chat": "chat",
                    "scenario": "scenario",
                    "device": "devices",
                    "open_app": "open_app",
                    "media_add": "media_library",
                    "media_update": "media_library",
                    "media_delete": "media_library",
                }.get(operation)
                if required and required not in peer["capabilities"]:
                    raise ValueError("Разрешение отозвано до начала выполнения.")
                if operation == "calendar_day" and "calendar" not in peer["categories"]:
                    raise ValueError("Календарь Mac не разрешён этому устройству.")
                caps = set()
                if "scenario" in peer["capabilities"]:
                    caps.update(MAC_CAPABILITIES)
                if "devices" in peer["capabilities"]:
                    caps.update({"mac.system", "mac.media"})
                if "open_app" in peer["capabilities"]:
                    caps.add("mac.apps")
                device = DeviceContext(
                    identity, peer["kind"], "sync-" + identity, frozenset(caps), True, "local-mac"
                )
                context = RequestContext(device, "")
                if operation == "chat":
                    from backend.brain.conversation import Conversation
                    from backend.brain.providers import create_llm_async
                    from backend.memory.profile import ProfileMemory
                    from backend.tools.registry import ToolRegistry

                    conversation_id = text(args.get("conversation_id"), 80)
                    if not re.fullmatch(r"[a-fA-F0-9-]{16,80}", conversation_id):
                        raise ValueError("Некорректный идентификатор диалога.")
                    is_private = args.get("private") is True
                    chat_device = DeviceContext(
                        identity,
                        peer["kind"],
                        f"sync-{identity}-{conversation_id}",
                        frozenset(caps),
                        True,
                        f"sync-{identity}-{conversation_id}",
                    )
                    tools = ToolRegistry()  # Chat cannot turn model output into Mac actions.
                    if not is_private and "memory" in peer["categories"]:
                        tools.memory = ProfileMemory(self.memory)
                    conversation_store = self.memory
                    if is_private:
                        from backend.memory.store import MemoryStore

                        conversation_store = MemoryStore(":memory:")
                    mobile_settings = self.s.model_copy(deep=True)
                    mobile_settings.llm.provider = "openai"
                    mobile_settings.openai.fallback = False
                    model = None
                    try:
                        model = await create_llm_async(mobile_settings)
                        conversation = Conversation(
                            mobile_settings,
                            model,
                            conversation_store,
                            tools=tools,
                            device=chat_device,
                        )
                        if is_private:
                            history = args.get("history", [])
                            if not isinstance(history, list) or len(history) > 24:
                                raise ValueError("Некорректная приватная история диалога.")
                            total = 0
                            for message in history:
                                if not isinstance(message, dict):
                                    raise ValueError("Некорректная приватная история диалога.")
                                role, content = message.get("role"), message.get("content")
                                if role not in {"user", "assistant"} or not isinstance(
                                    content, str
                                ):
                                    raise ValueError("Некорректная приватная история диалога.")
                                total += len(content)
                                if total > 6000:
                                    raise ValueError(
                                        "Приватная история превышает допустимый размер."
                                    )
                                conversation_store.add(conversation.id, role, content)
                        answer = "".join(
                            [
                                part
                                async for part in conversation.reply(text(args.get("text"), 4000))
                            ]
                        )
                        actual_provider = getattr(model, "last_provider", None)
                        actual_model = getattr(model, "last_model", None)
                        if actual_provider != "openai":
                            raise ValueError("Мобильный чат не получил ответ от GPT.")
                        actual_model = actual_model or mobile_settings.openai.model
                        result = {
                            "message": redact(answer)[:16000],
                            "provider": actual_provider,
                            "model": actual_model,
                        }
                        import logging

                        logging.getLogger(__name__).info(
                            "IOS_CHAT completed provider=%s model=%s device=%s",
                            actual_provider,
                            actual_model,
                            identity[:12],
                        )
                    finally:
                        if model is not None:
                            await model.aclose()
                        if is_private:
                            conversation_store.close()
                elif operation == "open_app":
                    name = text(args.get("name"), 120)
                    context = RequestContext(device, "открой " + name)
                    with request_scope(context):
                        result = json.loads(await self.tools.call("open_app", {"name": name}))
                elif operation == "scenario":
                    scenario = self.tools.automations.store.get(args.get("id"))
                    if args.get("revision") != scenario.revision:
                        raise ValueError("Сценарий изменился.")
                    context = RequestContext(device, "Запусти сценарий " + scenario.name)
                    token = scenario_selection.set(
                        (context.request_id, scenario.id, scenario.revision)
                    )
                    try:
                        run_args = {
                            "id": scenario.id,
                            "revision": scenario.revision,
                        }
                        if args.get("variant"):
                            run_args["variant"] = args["variant"]
                        values = args.get("values", {})
                        if not isinstance(values, dict) or len(json.dumps(values)) > 4000:
                            raise ValueError("Некорректные параметры сценария.")
                        if values:
                            run_args["values"] = json.dumps(values, ensure_ascii=False)
                        with request_scope(context):
                            result = json.loads(await self.tools.call("run_scenario", run_args))
                    finally:
                        scenario_selection.reset(token)
                elif operation == "device":
                    if args.get("action") not in {
                        "led",
                        "power",
                        "color",
                        "brightness",
                        "volume",
                        "playback",
                        "inspect",
                    }:
                        raise ValueError("Операция устройства недоступна.")
                    context = RequestContext(device, "Управляй выбранным устройством")
                    with request_scope(context):
                        result = json.loads(await self.tools.call("device_command", args))
                elif operation in {"media_add", "media_update", "media_delete"}:
                    cinema = getattr(self.tools, "cinema", None)
                    if cinema is None or not cinema.available:
                        raise ValueError("Медиатека Mac недоступна.")
                    if operation == "media_add":
                        from backend.cinema.models import Entry

                        entry = Entry(
                            title=text(args.get("title"), 200),
                            kind=args.get("kind", "movie"),
                            year=args.get("year", 0),
                        )
                        await cinema.db("add", entry.model_dump())
                    elif operation == "media_update":
                        title_id = text(args.get("id"), 80)
                        changes = args.get("values")
                        if not isinstance(changes, dict) or len(changes) > 8:
                            raise ValueError("Некорректные изменения медиатеки.")
                        if "position" in changes or "duration" in changes:
                            progress_values = {
                                "position": changes.pop("position", 0),
                                "duration": changes.pop("duration", 0),
                                "progress_status": changes.pop("progress_status", "watching"),
                                "season": changes.pop("season", args.get("season", 0)),
                                "episode": changes.pop("episode", args.get("episode", 0)),
                            }
                            await cinema.db(
                                "manual_progress",
                                title_id,
                                progress_values["season"],
                                progress_values["episode"],
                                progress_values["position"],
                                progress_values["duration"],
                                progress_values["progress_status"],
                            )
                            if changes:
                                await cinema.db("update_title", title_id, changes)
                        else:
                            await cinema.db("update_title", title_id, changes)
                    else:
                        title_id = text(args.get("id"), 80)
                        await cinema.db("delete", title_id)
                    result = {"message": "Медиатека Mac обновлена.", "provider": "macos"}
                elif operation == "calendar_day":
                    dashboard = getattr(self.bus, "dashboard", None)
                    if dashboard is None:
                        raise ValueError("Календарь Mac недоступен.")
                    day = text(args.get("date"), 10)
                    try:
                        from datetime import date as calendar_date

                        calendar_date.fromisoformat(day)
                    except ValueError:
                        raise ValueError("Укажи дату календаря в формате ГГГГ-ММ-ДД.") from None
                    await dashboard.read_calendar(day, request_access=False)
                    result = {"message": "Календарь Mac обновлён."}
                elif operation == "confirm":
                    pending = self.tools.confirmations.pending.get(context.binding)
                    if not pending:
                        raise ValueError("Нет ожидающего подтверждения.")
                    context = RequestContext(device, "Подтверждаю " + text(args.get("code"), 4))
                    result = {
                        "message": await self.tools.confirmations.respond(
                            context, self.tools.call, self.tools.direct_reply
                        )
                    }
                elif operation == "cancel":
                    target = self.commands.get((identity, args.get("id")))
                    if target and target is not asyncio.current_task():
                        target.cancel()
                    result = {
                        "message": "Запрошена остановка своей команды. "
                        "Уже отправленные действия не "
                        "отменяются."
                    }
                status = (
                    "failed"
                    if result.get("error")
                    else "confirmation_required"
                    if result.get("status") == "confirmation_required"
                    else "completed"
                )
        except (asyncio.CancelledError, TimeoutError):
            status, result = (
                "interrupted",
                {"error": "Выполнение прервано. Автоматического повтора нет."},
            )
        except Exception as exc:
            from backend.brain.llm import LLMError

            if isinstance(exc, LLMError):
                status, result = "failed", {"error": redact(str(exc))[:500], "provider": "openai"}
            elif isinstance(exc, ValueError) and str(exc).startswith(
                ("Медиатека", "Календарь", "Укажи дату")
            ):
                status, result = "failed", {"error": str(exc)[:500]}
            else:
                status, result = (
                    "failed",
                    {"error": "Команда не подтверждена. Проверь разрешения и журнал на Mac."},
                )
        # Mobile receipts never include adapter internals, endpoints or credentials.
        result = {
            key: redact(value) if isinstance(value, str) else value
            for key, value in result.items()
            if key
            in {
                "message",
                "error",
                "status",
                "result_status",
                "delivery_state",
                "device_state_confirmed",
                "physical_confirmed",
                "provider",
                "model",
            }
            and (value is None or isinstance(value, (str, bool, int, float)))
        }
        self.store.finish(identity, value["id"], status, result)
        self.publish(data=self.snapshot())

    def sync_data(self, peer):
        result = {}
        categories = peer["categories"]
        result["mobile_chat"] = {
            "provider": "openai",
            "model": self.s.openai.model,
            "fallback": False,
            "desktop_provider": self.s.llm.provider,
        }
        if "memory" in categories:
            from backend.memory.profile import _SENSITIVE

            result["memory"] = [
                dict(r)
                for r in self.memory.db.execute(
                    "SELECT id,content,category,revision,updated_at FROM facts WHERE "
                    "project_id IS NULL AND status='active' AND confirmed=1 AND sensitive=0 "
                    "AND (expires_at IS NULL OR expires_at>?) LIMIT 1000",
                    (_now(),),
                )
                if not _SENSITIVE.search(r["content"])
            ]
        if "preferences" in categories:
            from backend.memory.profile import _SENSITIVE

            result["preferences"] = [
                dict(r)
                for r in self.memory.db.execute(
                    "SELECT id,content,revision FROM facts WHERE category='preference' "
                    "AND confirmed=1 AND sensitive=0 AND project_id IS NULL "
                    "AND status='active' AND (expires_at IS NULL OR expires_at>?) LIMIT 300",
                    (_now(),),
                )
                if not _SENSITIVE.search(r["content"])
            ]
        if "scenarios" in categories and self.tools.automations:
            result["scenarios"] = [
                {"id": s.id, "name": s.name, "revision": s.revision, "enabled": s.enabled}
                for s in self.tools.automations.store.list()
            ]
        if "devices" in categories and self.tools.devices:
            result["devices"] = [
                {"id": d.id, "name": d.name, "kind": d.kind}
                for d in self.tools.devices.store.list()
            ]
        if "history" in categories:
            result["history"] = rows(
                self.memory.db,
                "SELECT id,role,content,created_at FROM messages WHERE device_id=? "
                "AND session_id LIKE ? AND role IN ('user','assistant') "
                "ORDER BY id DESC LIMIT 100",
                (peer["id"], "sync-" + peer["id"] + "-%"),
            )
        if "media_library" in categories:
            media = (
                rows(
                    self.memory.db,
                    "SELECT t.id,t.title,t.kind,t.year,t.poster_url,t.description,"
                    "u.status,u.favorite,u.user_rating,u.watchlist,u.manual_watched,"
                    "u.added_at,u.updated_at AS library_updated_at,"
                    "e.id AS episode_id,e.season,e.episode,e.position,e.duration,e.watched,"
                    "e.last_viewed "
                    "FROM cinema_titles t JOIN cinema_library u ON u.title_id=t.id "
                    "LEFT JOIN cinema_episodes e ON e.id=COALESCE(t.last_episode_id,"
                    "(SELECT id FROM cinema_episodes WHERE title_id=t.id AND watched=0 "
                    "ORDER BY season,episode LIMIT 1),"
                    "(SELECT id FROM cinema_episodes WHERE title_id=t.id "
                    "ORDER BY season,episode LIMIT 1)) "
                    "ORDER BY u.favorite DESC,u.updated_at DESC LIMIT 2000",
                )
                if self.memory.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cinema_titles'"
                ).fetchone()
                else []
            )
            for item in media:
                for field in ("favorite", "watchlist", "manual_watched", "watched"):
                    if item.get(field) is not None:
                        item[field] = bool(item[field])
            result["media_library"] = media
        if (
            "media_progress" in categories
            and self.memory.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cinema_episodes'"
            ).fetchone()
        ):
            progress = rows(
                self.memory.db,
                "SELECT e.id,t.title,e.season,e.episode,e.position,e.duration,e.watched,"
                "e.last_viewed "
                "FROM cinema_episodes e JOIN cinema_titles t ON t.id=e.title_id "
                "WHERE e.last_viewed IS NOT NULL ORDER BY e.last_viewed DESC LIMIT 200",
            )
            for item in progress:
                item["watched"] = bool(item["watched"])
            result["media_progress"] = progress
        if "calendar" in categories:
            dashboard = getattr(self.bus, "dashboard", None)
            today = dashboard.today() if dashboard else ""
            days = list(dashboard.calendar.values())[-32:] if dashboard else []
            day = dashboard.calendar.get(today, {}) if dashboard else {}
            result["calendar"] = {
                "today": today,
                "status": day.get("status", "unknown"),
                "message": day.get("message", "Календарь Mac пока не загружен."),
                "timezone": day.get("timezone", getattr(dashboard, "timezone", "")),
                "updated_at": day.get("updated_at"),
                "days": [
                    {
                        "date": saved.get("date", ""),
                        "status": saved.get("status", "unknown"),
                        "message": saved.get("message", ""),
                        "timezone": saved.get("timezone", ""),
                        "updated_at": saved.get("updated_at"),
                        "events": [
                            {
                                key: event.get(key)
                                for key in (
                                    "id",
                                    "title",
                                    "color",
                                    "start",
                                    "end",
                                    "all_day",
                                    "calendar",
                                )
                            }
                            for event in saved.get("events", [])[:200]
                        ],
                    }
                    for saved in days
                ],
                "events": [
                    {
                        key: event.get(key)
                        for key in (
                            "id",
                            "title",
                            "color",
                            "start",
                            "end",
                            "all_day",
                            "calendar",
                        )
                    }
                    for event in day.get("events", [])[:200]
                ],
            }
        if "statistics" in categories:
            dashboard = getattr(self.bus, "dashboard", None)
            result["statistics"] = dashboard.statistics(7) if dashboard else None
        with self.memory.memory_transaction():
            self.memory.db.execute(
                "UPDATE sync_peers SET last_sync=? WHERE id=?", (time.time(), peer["id"])
            )
        revision = hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return {"schema": 1, "revision": revision, "data": result, "server_time": time.time()}

    def propose_memory(self, peer, value):
        if "memory" not in peer["categories"]:
            raise ValueError("Синхронизация памяти отключена.")
        from backend.memory.profile import _SENSITIVE

        content = text(value.get("content"))
        if _SENSITIVE.search(content):
            raise ValueError("Чувствительные сведения вводи на Mac с явным согласием.")
        row = self.memory.db.execute(
            "SELECT * FROM facts WHERE id=? AND project_id IS NULL AND sensitive=0",
            (value.get("id"),),
        ).fetchone()
        if not row:
            raise ValueError("Воспоминание недоступно.")
        with self.memory.memory_transaction():
            self.memory.db.execute(
                "INSERT INTO sync_conflicts(peer_id,memory_id,revision,content,at)"
                " VALUES(?,?,?,?,?)",
                (peer["id"], row["id"], value.get("revision"), content, time.time()),
            )
            self.memory.db.execute(
                "DELETE FROM sync_conflicts WHERE id NOT IN (SELECT id FROM "
                "sync_conflicts ORDER BY id DESC LIMIT 100)"
            )
        return {
            "status": "conflict"
            if row["revision"] != value.get("revision")
            else "awaiting_mac_confirmation"
        }

    async def start(self, host=None, port=0, *, advertise=True, monitor=True):
        self.memory._require_memory()
        if self.server_task and not self.server_task.done():
            raise ValueError("Сначала останови текущий сервер.")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Порт должен быть от 0 до 65535.")

        local_host = None
        if host is None:
            local_host, tailnet_host = await asyncio.gather(
                asyncio.to_thread(discover_lan_address),
                asyncio.to_thread(discover_tailscale_address),
            )
            hosts = [value for value in dict.fromkeys((local_host, tailnet_host)) if value]
        else:
            hosts = [lan_address(host)]
            if hosts[0] not in ipaddress.ip_network("100.64.0.0/10"):
                local_host = hosts[0]
        if not hosts:
            raise ValueError(
                "Не найдена локальная сеть. Подключи Mac к Wi-Fi или настрой Tailscale."
            )
        use_bonjour = bool(advertise and local_host)

        identity = await asyncio.to_thread(self.identity.read)
        # The stable Bonjour hostname follows the Mac across DHCP address changes. Its
        # certificate is also pinned before a paired client sends any signed requests.
        ca_fingerprint = hashlib.sha256(
            identity["ca"].encode() if identity["ca"].startswith("-----BEGIN") else identity["ca"]
        ).hexdigest()
        service_hostname = f"chushpan-{ca_fingerprint[:12]}.local" if use_bonjour else ""
        certs = await asyncio.to_thread(
            certificates, identity, hosts, service_hostname=service_hostname or None
        )
        with tempfile.TemporaryDirectory(prefix="chushpan-tls-") as directory:
            cert_path, key_path = Path(directory) / "certificate.pem", Path(directory) / "key.pem"
            cert_path.write_bytes(certs["certificate"])
            key_path.touch(mode=0o600)
            key_path.write_bytes(certs["key"])
            config = uvicorn.Config(
                self.application(),
                host=hosts[0],
                port=port,
                ssl_certfile=str(cert_path),
                ssl_keyfile=str(key_path),
                access_log=False,
                log_level="warning",
                limit_concurrency=16,
                timeout_keep_alive=10,
            )
            config.load()  # SSLContext owns the key before the private temporary file is removed.

        listeners = []
        actual_port = port
        try:
            for index, address in enumerate(hosts):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind((address, actual_port))
                    if index == 0 and actual_port == 0:
                        actual_port = listener.getsockname()[1]
                    listener.listen(16)
                    listener.setblocking(False)
                    listeners.append(listener)
                except OSError:
                    listener.close()
                    if index == 0:
                        raise

            actual_hosts = [sock.getsockname()[0] for sock in listeners]
            service_authority = f"{service_hostname}:{actual_port}" if service_hostname else ""
            url_authority = service_authority or f"{actual_hosts[0]}:{actual_port}"
            self.url = f"https://{url_authority}"
            self.fingerprint, self.ca = certs["fingerprint"], certs["ca"]
            self.service_hostname = service_hostname
            self.bound_hosts = actual_hosts
            self.allowed_hosts = {f"{address}:{actual_port}" for address in actual_hosts}
            if service_authority:
                self.allowed_hosts.add(service_authority)
            self.allowed_origins = {f"https://{authority}" for authority in self.allowed_hosts}
            self.listeners = listeners
            self.listener = listeners[0]
            self.server = LocalServer(config)
            self.started_at = time.time()
            self.server_task = asyncio.create_task(self.server.serve(sockets=listeners))
            for _ in range(50):
                if self.server_task.done():
                    await self.server_task
                    raise ValueError("TLS-сервер не запустился.")
                if self.server.started:
                    break
                await asyncio.sleep(0.02)
            if not self.server.started:
                raise ValueError("TLS-сервер не подтвердил запуск.")

            tailnet_host = next(
                (
                    value
                    for value in actual_hosts
                    if ipaddress.ip_address(value) in ipaddress.ip_network("100.64.0.0/10")
                ),
                "",
            )
            if use_bonjour:
                arguments = [
                    "/usr/bin/dns-sd",
                    "-P",
                    "Chushpan",
                    "_chushpan._tcp",
                    "local.",
                    str(actual_port),
                    service_hostname + ".",
                    local_host,
                    "tls=1",
                    "version=2",
                ]
                if tailnet_host:
                    arguments.append("tailscale=" + tailnet_host)
                try:
                    self.bonjour = await asyncio.create_subprocess_exec(
                        *arguments,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except OSError:
                    self.failure = (
                        "HTTPS запущен, но Bonjour недоступен. Проверь службу DNS-SD Mac."
                    )
            self.failure = self.failure if use_bonjour and self.bonjour is None else None
            self.endpoint = {
                "host": local_host or actual_hosts[0],
                "port": actual_port,
            }
            self.tailnet_host = tailnet_host
            self.memory.set_preference("sync", "endpoint", self.endpoint)
            if (
                monitor
                and self.network_watch_task is None
                and not all(ipaddress.ip_address(value).is_loopback for value in actual_hosts)
            ):
                self.network_watch_task = asyncio.create_task(self.watch_network(actual_port))
        except BaseException as exc:
            for listener in listeners:
                listener.close()
            await self._stop_server(clear_pairings=False)
            self.failure = (
                "Запуск сервера отменён."
                if isinstance(exc, asyncio.CancelledError)
                else "Этот порт уже занят. Выбери автоматический порт или освободи порт."
                if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE
                else "IP-адрес сменился или интерфейс отключён. Подключи Mac к сети снова."
                if isinstance(exc, OSError) and exc.errno == errno.EADDRNOTAVAIL
                else "TLS-сервер не запущен. Проверь доступность сети и Keychain."
            )
            raise

    async def start_automatically(self):
        if self.server_task and not self.server_task.done():
            return
        try:
            await self.start(port=0)
            self.publish(data=self.snapshot(), message="Защищённая точка подключения готова.")
        except (OSError, ValueError) as exc:
            self.failure = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Не удалось запустить локальную точку подключения."
            )
            if self.network_watch_task is None:
                self.network_watch_task = asyncio.create_task(self.watch_network(0))
            self.publish(data=self.snapshot())

    async def watch_network(self, port):
        try:
            while True:
                await asyncio.sleep(8)
                local_host, tailnet_host = await asyncio.gather(
                    asyncio.to_thread(discover_lan_address),
                    asyncio.to_thread(discover_tailscale_address),
                )
                current = set(self.bound_hosts)
                wanted = {value for value in (local_host, tailnet_host) if value}
                if current == wanted:
                    continue
                await self._stop_server(clear_pairings=False)
                if wanted:
                    try:
                        await self.start(port=port, monitor=False)
                    except (OSError, ValueError):
                        self.failure = "Сеть изменилась. Повторяю поиск локального Mac."
                else:
                    self.failure = "Mac сейчас не подключён к локальной сети или Tailscale."
                self.publish(data=self.snapshot())
        except asyncio.CancelledError:
            return

    async def _stop_server(self, *, clear_pairings=True):
        if clear_pairings:
            self.invite, self.pending = None, {}
        for task in list(self.commands.values()):
            task.cancel()
        await asyncio.gather(*list(self.commands.values()), return_exceptions=True)
        if self.bonjour and self.bonjour.returncode is None:
            self.bonjour.terminate()
            await self.bonjour.wait()
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            try:
                await asyncio.wait_for(self.server_task, 3)
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                self.failure = "TLS-сервер завершился с ошибкой."
        for listener in self.listeners or ([self.listener] if self.listener else []):
            listener.close()
        self.server, self.server_task, self.listener, self.bonjour = None, None, None, None
        self.listeners = []
        self.url = ""
        self.bound_hosts = []
        self.allowed_hosts, self.allowed_origins = set(), set()
        self.service_hostname = ""
        self.started_at = None

    async def stop(self):
        watcher, self.network_watch_task = self.network_watch_task, None
        if watcher and watcher is not asyncio.current_task():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await self._stop_server()

    async def handle(self, value):
        op = value.get("op", "snapshot")
        message = None
        if op == "start":
            try:
                await self.start(
                    value.get("host"),
                    value.get("port", 0),
                    advertise=value.get("mode") != "tailscale",
                )
            except OSError:
                raise ValueError(
                    self.failure or "Не удалось открыть локальный TLS-сервер."
                ) from None
            message = (
                "Защищённая точка подключения запущена. Разрешения устройств не включены "
                "автоматически."
            )
        elif op == "stop":
            await self.stop()
            self.failure = None
            message = "Локальный TLS-сервер остановлен. Сопряжения и разрешения сохранены."
        elif op == "invite":
            if not self.snapshot()["running"]:
                raise ValueError("Сначала запусти локальный TLS-сервер.")
            nonce = secrets.token_urlsafe(24)
            url = self.url + "/#pair=" + nonce
            qr = await self.tools.desktop.bridge.request("desktop_qr", text=url)
            self.invite = {
                "nonce": nonce,
                "url": url,
                "expires": time.time() + 120,
                "qr": qr["image"],
            }
        elif op == "approve":
            pending = self.pending.get(value.get("id"))
            if (
                not pending
                or pending["expires"] < time.time()
                or value.get("code") != pending["code"]
            ):
                raise ValueError("Сопряжение устарело или код не совпадает.")
            if value.get("confirmed") is not True:
                raise ValueError("Сравни код на устройствах и подтверди сопряжение.")
            self.store.trust(pending)
            self.pending.pop(pending["id"])
            message = "Устройство доверенное. Теперь выбери разрешения и категории данных."
        elif op == "reject":
            self.pending.pop(value.get("id"), None)
        elif op in {"permissions", "disconnect", "revoke", "rename"}:
            peer = self.store.peer(value.get("id"), connected=False)
            with self.memory.memory_transaction():
                if op == "permissions":
                    categories, capabilities = (
                        value.get("categories", []),
                        value.get("capabilities", []),
                    )
                    if (
                        not isinstance(categories, list)
                        or not isinstance(capabilities, list)
                        or set(categories) - CATEGORIES.keys()
                        or set(capabilities) - CAPABILITIES
                    ):
                        raise ValueError("Неизвестная категория или разрешение.")
                    self.memory.db.execute(
                        "UPDATE sync_peers SET categories=?,capabilities=? WHERE id=?",
                        (json.dumps(categories), json.dumps(capabilities), peer["id"]),
                    )
                    for (identity, _), task in list(self.commands.items()):
                        if identity == peer["id"]:
                            task.cancel()
                    self.tools.confirmations.pending.pop((peer["id"], "sync-" + peer["id"]), None)
                elif op == "rename":
                    self.memory.db.execute(
                        "UPDATE sync_peers SET name=? WHERE id=?",
                        (text(value.get("name"), 80), peer["id"]),
                    )
                else:
                    for (identity, _), task in list(self.commands.items()):
                        if identity == peer["id"]:
                            task.cancel()
                    column, at = (
                        ("revoked_at", time.time()) if op == "revoke" else ("disconnected", 1)
                    )
                    self.memory.db.execute(
                        f"UPDATE sync_peers SET {column}=? WHERE id=?", (at, peer["id"])
                    )
                    self.tools.confirmations.pending.pop((peer["id"], "sync-" + peer["id"]), None)
            message = "Разрешения устройства обновлены."
        elif op == "conflict":
            row = self.memory.db.execute(
                "SELECT * FROM sync_conflicts WHERE id=? AND status='pending'", (value.get("id"),)
            ).fetchone()
            if not row:
                raise ValueError("Предложение уже обработано.")
            with self.memory.memory_transaction():
                if value.get("accept") is True:
                    current = self.memory.db.execute(
                        "SELECT * FROM facts WHERE id=?", (row["memory_id"],)
                    ).fetchone()
                    if not current or value.get("revision") != current["revision"]:
                        raise ValueError(
                            "Память изменилась. Проверь новую версию перед применением."
                        )
                    self.memory.db.execute(
                        "UPDATE facts SET content=?,revision=revision+1,updated_at=? WHERE id=?",
                        (
                            row["content"],
                            _now(),
                            row["memory_id"],
                        ),
                    )
                    self.memory.invalidate_memory_context()
                self.memory.db.execute(
                    "UPDATE sync_conflicts SET status=? WHERE id=?",
                    ("accepted" if value.get("accept") is True else "rejected", row["id"]),
                )
        elif op == "certificate":
            if not self.ca:
                raise ValueError("Сначала запусти сервер, чтобы создать сертификат в Keychain.")
            return {
                "data": self.snapshot(),
                "download": {
                    "name": "Chushpan-Local-CA.cer",
                    "mime": "application/pkix-cert",
                    "base64": base64.b64encode(self.ca).decode(),
                },
            }
        elif op != "snapshot":
            raise ValueError("Неизвестное действие синхронизации.")
        return {"data": self.snapshot(), "message": message}

    async def close(self):
        await super().close()
        await self.stop()
