"""Local HTTPS companion with mutual pairing, signed requests and expiring commands."""

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import secrets
import socket
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from backend.brain.privacy import redact
from backend.center import Center, rows, text
from backend.context import MAC_CAPABILITIES, DeviceContext, RequestContext, request_scope, scenario_selection
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
    local = any(address in ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")) if address.version == 4 else False
    if not local:
        raise ValueError("Сервер доступен только по локальному IPv4-адресу. Публичный интерфейс запрещён.")
    return str(address)


class DeviceSync(Center):
    channel = "device_sync"

    def __init__(self, settings, memory, registry, bus, *, identity=None):
        self.s, self.memory, self.tools = settings, memory, registry
        self.store = SyncStore(memory)
        self.identity = identity or KeychainIdentity()
        self.server, self.server_task, self.bonjour, self.listener = None, None, None, None
        self.url, self.fingerprint, self.ca = "", "", None
        self.invite, self.pending = None, {}
        self.commands, self.rates = {}, defaultdict(deque)
        self.bind(bus)
        # A crashed command is uncertain and must never be replayed after restart.
        with memory.memory_transaction():
            memory.db.execute("UPDATE sync_commands SET status='interrupted' WHERE status='running'")

    def snapshot(self):
        now = time.time()
        self.pending = {k: v for k,v in self.pending.items() if v["expires"] > now}
        invite = self.invite if self.invite and self.invite["expires"] > now else None
        return {"running": bool(self.server and self.server.started and self.server_task and not self.server_task.done()),
                "url": self.url, "fingerprint": self.fingerprint, "invite": invite,
                "pending": [{k:v for k,v in p.items() if k != "key"} for p in self.pending.values()],
                "peers": self.store.peers(), "categories": CATEGORIES, "capabilities": sorted(CAPABILITIES),
                "history": rows(self.memory.db, "SELECT peer_id,command_id,operation,created_at,expires_at,status FROM sync_commands ORDER BY created_at DESC LIMIT 100"),
                "conflicts": rows(self.memory.db, "SELECT * FROM sync_conflicts WHERE status='pending' ORDER BY id DESC LIMIT 100")}

    def rate(self, address):
        now = time.monotonic()
        # Bound both the number of clients and their request histories.
        if len(self.rates) > 128:
            self.rates.clear()
        queue = self.rates[address]
        while queue and queue[0] < now-60:
            queue.popleft()
        if len(queue) >= 120:
            raise HTTPException(429, "Слишком много запросов. Подожди минуту.")
        queue.append(now)

    def application(self):
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.middleware("http")
        async def secure_headers(request, call_next):
            if request.headers.get("host") != self.url.removeprefix("https://"):
                return Response(status_code=403)
            response = await call_next(request)
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                                     "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"})
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

        @app.post("/api/{operation}")
        async def api(operation: str, request: Request):
            try:
                self.rate(request.client.host if request.client else "unknown")
                if request.headers.get("origin") != self.url or request.headers.get("content-type", "").split(";")[0] != "application/json":
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
                timestamp, nonce = request.headers.get("x-time", ""), request.headers.get("x-nonce", "")
                if len(timestamp) > 16 or abs(time.time()-int(timestamp)) > 60:
                    raise ValueError("Время запроса устарело. Проверь часы устройства.")
                if operation == "pair":
                    key, jwk, identity = public_key(data.get("key"))
                    if device != identity:
                        raise ValueError("Ключ не соответствует устройству.")
                elif operation == "pair_status" and device in self.pending:
                    jwk = self.pending[device]["key"]
                else:
                    jwk = self.store.peer(device, connected=operation != "reconnect")["public_key"]
                verify(jwk, request.headers.get("x-signature", ""), f"/api/{operation}\n{timestamp}\n{nonce}\n{body}")
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
                        self.memory.db.execute("UPDATE sync_peers SET disconnected=0 WHERE id=?", (device,))
                peer = self.store.peer(device)
                with self.memory.memory_transaction():
                    self.memory.db.execute("UPDATE sync_peers SET last_seen=? WHERE id=?", (time.time(),device))
                if operation in {"status", "reconnect"}:
                    return self.remote_status(peer)
                if operation == "command":
                    return self.submit(peer, data)
                if operation == "result":
                    row = self.memory.db.execute("SELECT status,result FROM sync_commands WHERE peer_id=? AND command_id=?", (device,data.get("id"))).fetchone()
                    if not row:
                        raise ValueError("Команда не найдена.")
                    return {"status": row["status"], "result": json.loads(row["result"]) if row["result"] else None}
                if operation == "sync":
                    return self.sync_data(peer)
                if operation == "memory_update":
                    return self.propose_memory(peer, data)
                raise HTTPException(404)
            except HTTPException:
                raise
            except (ValueError, TypeError, KeyError, UnicodeError):
                return JSONResponse({"error": "Запрос отклонён: проверь доверие устройства, подпись, срок действия и выбранные разрешения."}, status_code=403)
            except Exception:
                return JSONResponse({"error": "Локальный сервис временно недоступен."}, status_code=503)

        return app

    def pair(self, value, identity, jwk):
        invite = self.invite
        if not invite or invite["expires"] < time.time() or not secrets.compare_digest(str(value.get("invite", "")), invite["nonce"]):
            raise ValueError("Приглашение недействительно.")
        if value.get("kind") not in {"iphone", "ipad"} or len(self.pending) >= 8:
            raise ValueError("Некорректное устройство или слишком много запросов.")
        name = text(value.get("name"), 80)
        self.invite = None  # One invitation can bind only one public key.
        code = str(int(hashlib.sha256((invite["nonce"]+identity+self.fingerprint).encode()).hexdigest()[:12],16) % 1000000).zfill(6)
        self.pending[identity] = {"id": identity, "name": name, "kind": value["kind"], "key": jwk,
                                  "code": code, "expires": time.time()+120}
        self.publish(data=self.snapshot(), message="Сравни код на обоих устройствах и подтверди сопряжение.")
        return {"status": "pending", "id": identity, "code": code}

    def remote_status(self, peer):
        scenarios = []
        if "scenario" in peer["capabilities"] and self.tools.automations:
            scenarios = [{"id": s.id, "name": s.name, "revision": s.revision} for s in self.tools.automations.store.list() if s.enabled]
        devices = []
        if "devices" in peer["capabilities"] and self.tools.devices:
            devices = [{"id": d.id, "name": d.name, "kind": d.kind} for d in self.tools.devices.store.list() if d.enabled]
        return {"status": "online", "name": peer["name"], "capabilities": peer["capabilities"],
                "categories": peer["categories"], "scenarios": scenarios, "devices": devices,
                "history": rows(self.memory.db, "SELECT command_id,operation,created_at,status FROM sync_commands WHERE peer_id=? ORDER BY created_at DESC LIMIT 30", (peer["id"],))}

    def submit(self, peer, value):
        operation = value.get("operation")
        required = {"chat": "chat", "scenario": "scenario", "device": "devices", "open_app": "open_app",
                    "confirm": None, "cancel": None}.get(operation, "unsupported")
        if required == "unsupported" or (required and required not in peer["capabilities"]):
            raise ValueError("Эта команда не разрешена на Mac.")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 12000:
            raise ValueError("Некорректные аргументы команды.")
        digest = operation + ":" + hashlib.sha256(json.dumps(arguments,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        previous = self.store.reserve(peer["id"], value.get("id"), digest, value.get("expires"))
        if previous:
            return {"status": previous["status"], "id": value["id"]}
        if len(self.commands) >= 8:
            self.store.finish(peer["id"], value["id"], "rejected", {"error": "Очередь занята; повторного выполнения не будет."})
            return {"status": "rejected", "id": value["id"]}
        task = asyncio.create_task(self.execute(peer["id"], value))
        key = (peer["id"], value["id"])
        self.commands[key] = task
        task.add_done_callback(lambda _: self.commands.pop(key, None))
        return {"status": "running", "id": value["id"]}

    async def execute(self, identity, value):
        result, status = {}, "failed"
        try:
            async with asyncio.timeout(90):
                peer = self.store.peer(identity)
                if time.time() >= value["expires"]:
                    raise ValueError("Команда устарела.")
                operation, args = value["operation"], value.get("arguments", {})
                caps = set()
                if "scenario" in peer["capabilities"]:
                    caps.update(MAC_CAPABILITIES)
                if "devices" in peer["capabilities"]:
                    caps.update({"mac.system", "mac.media"})
                if "open_app" in peer["capabilities"]:
                    caps.add("mac.apps")
                device = DeviceContext(identity, peer["kind"], "sync-"+identity, frozenset(caps), True, "local-mac")
                context = RequestContext(device, "")
                if operation == "chat":
                    from backend.brain.conversation import Conversation
                    from backend.brain.providers import create_llm
                    from backend.memory.profile import ProfileMemory
                    from backend.tools.registry import ToolRegistry

                    tools = ToolRegistry()  # Chat cannot turn model output into Mac actions.
                    if "memory" in peer["categories"]:
                        tools.memory = ProfileMemory(self.memory)
                    model = create_llm(self.s)
                    try:
                        conversation = Conversation(self.s, model, self.memory, tools=tools, device=device)
                        answer = "".join([part async for part in conversation.reply(text(args.get("text"),4000))])
                        result = {"message": redact(answer)[:16000]}
                    finally:
                        await model.aclose()
                elif operation == "open_app":
                    name = text(args.get("name"),120)
                    context = RequestContext(device, "открой "+name)
                    with request_scope(context):
                        result = json.loads(await self.tools.call("open_app", {"name": name}))
                elif operation == "scenario":
                    scenario = self.tools.automations.store.get(args.get("id"))
                    if args.get("revision") != scenario.revision:
                        raise ValueError("Сценарий изменился.")
                    context = RequestContext(device, "Запусти сценарий "+scenario.name)
                    token = scenario_selection.set((context.request_id, scenario.id, scenario.revision))
                    try:
                        with request_scope(context):
                            result = json.loads(await self.tools.call("run_scenario", {"id": scenario.id, "revision": scenario.revision}))
                    finally:
                        scenario_selection.reset(token)
                elif operation == "device":
                    if args.get("action") not in {"power", "color", "brightness", "volume", "playback", "inspect"}:
                        raise ValueError("Операция устройства недоступна.")
                    context = RequestContext(device, "Управляй выбранным устройством")
                    with request_scope(context):
                        result = json.loads(await self.tools.call("device_command", args))
                elif operation == "confirm":
                    pending = self.tools.confirmations.pending.get(context.binding)
                    if not pending:
                        raise ValueError("Нет ожидающего подтверждения.")
                    context = RequestContext(device, "Подтверждаю "+text(args.get("code"),4))
                    result = {"message": await self.tools.confirmations.respond(context, self.tools.call, self.tools.direct_reply)}
                elif operation == "cancel":
                    target = self.commands.get((identity,args.get("id")))
                    if target and target is not asyncio.current_task():
                        target.cancel()
                    result = {"message": "Запрошена остановка своей команды. Уже отправленные действия не отменяются."}
                status = "failed" if result.get("error") else "confirmation_required" if result.get("status") == "confirmation_required" else "completed"
        except (asyncio.CancelledError, TimeoutError):
            status, result = "interrupted", {"error": "Выполнение прервано. Автоматического повтора нет."}
        except Exception:
            status, result = "failed", {"error": "Команда не подтверждена. Проверь разрешения и журнал на Mac."}
        self.store.finish(identity,value["id"],status,result)
        self.publish(data=self.snapshot())

    def sync_data(self, peer):
        result = {}
        categories = peer["categories"]
        if "memory" in categories:
            from backend.memory.profile import _SENSITIVE
            result["memory"] = [dict(r) for r in self.memory.db.execute("SELECT id,content,category,revision,updated_at FROM facts WHERE project_id IS NULL AND status='active' AND confirmed=1 AND sensitive=0 AND (expires_at IS NULL OR expires_at>datetime('now')) LIMIT 1000") if not _SENSITIVE.search(r["content"])]
        if "preferences" in categories:
            result["preferences"] = [dict(r) for r in self.memory.db.execute("SELECT id,content,revision FROM facts WHERE category='preference' AND sensitive=0 AND project_id IS NULL AND status='active' LIMIT 300")]
        if "scenarios" in categories and self.tools.automations:
            result["scenarios"] = [{"id": s.id, "name": s.name, "revision": s.revision, "enabled": s.enabled} for s in self.tools.automations.store.list()]
        if "devices" in categories and self.tools.devices:
            result["devices"] = [{"id": d.id, "name": d.name, "kind": d.kind} for d in self.tools.devices.store.list()]
        if "history" in categories:
            result["history"] = rows(self.memory.db, "SELECT role,content,created_at FROM messages WHERE device_id=? AND role IN ('user','assistant') ORDER BY id DESC LIMIT 100", (peer["id"],))
        if "media_progress" in categories and self.memory.db.execute("SELECT 1 FROM sqlite_master WHERE name='cinema_episodes'").fetchone():
            result["media_progress"] = rows(self.memory.db, "SELECT e.id,t.title,e.season,e.episode,e.position,e.duration,e.watched,e.last_viewed FROM cinema_episodes e JOIN cinema_titles t ON t.id=e.title_id WHERE e.last_viewed IS NOT NULL ORDER BY e.last_viewed DESC LIMIT 200")
        with self.memory.memory_transaction():
            self.memory.db.execute("UPDATE sync_peers SET last_sync=? WHERE id=?", (time.time(),peer["id"]))
        revision = hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {"schema": 1, "revision": revision, "data": result, "server_time": time.time()}

    def propose_memory(self, peer, value):
        if "memory" not in peer["categories"]:
            raise ValueError("Синхронизация памяти отключена.")
        from backend.memory.profile import _SENSITIVE
        content = text(value.get("content"))
        if _SENSITIVE.search(content):
            raise ValueError("Чувствительные сведения вводи на Mac с явным согласием.")
        row = self.memory.db.execute("SELECT * FROM facts WHERE id=? AND project_id IS NULL AND sensitive=0", (value.get("id"),)).fetchone()
        if not row:
            raise ValueError("Воспоминание недоступно.")
        with self.memory.memory_transaction():
            self.memory.db.execute("INSERT INTO sync_conflicts(peer_id,memory_id,revision,content,at) VALUES(?,?,?,?,?)", (peer["id"],row["id"],value.get("revision"),content,time.time()))
            self.memory.db.execute("DELETE FROM sync_conflicts WHERE id NOT IN (SELECT id FROM sync_conflicts ORDER BY id DESC LIMIT 100)")
        return {"status": "conflict" if row["revision"] != value.get("revision") else "awaiting_mac_confirmation"}

    async def start(self, host, port):
        if self.server_task and not self.server_task.done():
            raise ValueError("Сначала останови текущий сервер.")
        host = lan_address(host)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Порт должен быть от 1024 до 65535.")
        identity = await asyncio.to_thread(self.identity.read)
        certs = await asyncio.to_thread(certificates, identity, host)
        self.url, self.fingerprint, self.ca = f"https://{host}:{port}", certs["fingerprint"], certs["ca"]
        with tempfile.TemporaryDirectory(prefix="chushpan-tls-") as directory:
            cert_path, key_path = Path(directory)/"certificate.pem", Path(directory)/"key.pem"
            cert_path.write_bytes(certs["certificate"])
            key_path.touch(mode=0o600)
            key_path.write_bytes(certs["key"])
            config = uvicorn.Config(self.application(), host=host, port=port, ssl_certfile=str(cert_path), ssl_keyfile=str(key_path), access_log=False, log_level="warning", limit_concurrency=16, timeout_keep_alive=10)
            config.load()  # SSLContext owns the key before the private temporary file is removed.
        listener = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            listener.bind((host,port))
            listener.listen(16)
            listener.setblocking(False)
            self.listener = listener
            self.server = LocalServer(config)
            self.server_task = asyncio.create_task(self.server.serve(sockets=[listener]))
            for _ in range(50):
                if self.server_task.done():
                    await self.server_task
                    raise ValueError("TLS-сервер не запустился.")
                if self.server.started:
                    break
                await asyncio.sleep(.02)
            if not self.server.started:
                raise ValueError("TLS-сервер не подтвердил запуск.")
            self.bonjour = await asyncio.create_subprocess_exec("/usr/bin/dns-sd", "-R", "Chushpan", "_chushpan._tcp", "local.", str(port), "tls=1", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        self.invite, self.pending = None, {}
        for task in list(self.commands.values()):
            task.cancel()
        await asyncio.gather(*list(self.commands.values()),return_exceptions=True)
        if self.bonjour and self.bonjour.returncode is None:
            self.bonjour.terminate()
            await self.bonjour.wait()
        if self.server:
            self.server.should_exit = True
        if self.server_task:
            try:
                await asyncio.wait_for(self.server_task,3)
            except (TimeoutError, asyncio.CancelledError):
                pass
        if self.listener:
            self.listener.close()
        self.server, self.server_task, self.listener, self.bonjour = None,None,None,None

    async def handle(self, value):
        op = value.get("op", "snapshot")
        message = None
        if op == "start":
            await self.start(value.get("host"),value.get("port",8766))
            message = "Локальный TLS-сервер запущен. Доверенные устройства пока не получают разрешений автоматически."
        elif op == "stop":
            await self.stop()
        elif op == "invite":
            if not self.server or not self.server.started:
                raise ValueError("Сначала запусти локальный TLS-сервер.")
            nonce = secrets.token_urlsafe(24)
            url = self.url+"/#pair="+nonce
            qr = await self.tools.desktop.bridge.request("desktop_qr", text=url)
            self.invite = {"nonce": nonce, "url": url, "expires": time.time()+120, "qr": qr["image"]}
        elif op == "approve":
            pending = self.pending.get(value.get("id"))
            if not pending or pending["expires"] < time.time() or value.get("code") != pending["code"]:
                raise ValueError("Сопряжение устарело или код не совпадает.")
            if value.get("confirmed") is not True:
                raise ValueError("Сравни код на устройствах и подтверди сопряжение.")
            self.store.trust(pending)
            self.pending.pop(pending["id"])
            message = "Устройство доверенное. Теперь выбери разрешения и категории данных."
        elif op == "reject":
            self.pending.pop(value.get("id"), None)
        elif op in {"permissions", "disconnect", "revoke", "rename"}:
            peer = self.store.peer(value.get("id"),connected=False)
            with self.memory.memory_transaction():
                if op == "permissions":
                    categories, capabilities = value.get("categories",[]),value.get("capabilities",[])
                    if not isinstance(categories,list) or not isinstance(capabilities,list) or set(categories)-CATEGORIES.keys() or set(capabilities)-CAPABILITIES:
                        raise ValueError("Неизвестная категория или разрешение.")
                    self.memory.db.execute("UPDATE sync_peers SET categories=?,capabilities=? WHERE id=?", (json.dumps(categories),json.dumps(capabilities),peer["id"]))
                elif op == "rename":
                    self.memory.db.execute("UPDATE sync_peers SET name=? WHERE id=?", (text(value.get("name"),80),peer["id"]))
                else:
                    for (identity,_),task in list(self.commands.items()):
                        if identity == peer["id"]:
                            task.cancel()
                    column, at = ("revoked_at",time.time()) if op == "revoke" else ("disconnected",1)
                    self.memory.db.execute(f"UPDATE sync_peers SET {column}=? WHERE id=?", (at,peer["id"]))
                    self.tools.confirmations.pending.pop((peer["id"],"sync-"+peer["id"]),None)
            message = "Разрешения устройства обновлены."
        elif op == "conflict":
            row = self.memory.db.execute("SELECT * FROM sync_conflicts WHERE id=? AND status='pending'", (value.get("id"),)).fetchone()
            if not row:
                raise ValueError("Предложение уже обработано.")
            with self.memory.memory_transaction():
                if value.get("accept") is True:
                    current = self.memory.db.execute("SELECT * FROM facts WHERE id=?", (row["memory_id"],)).fetchone()
                    if not current or value.get("revision") != current["revision"]:
                        raise ValueError("Память изменилась. Проверь новую версию перед применением.")
                    self.memory.db.execute("UPDATE facts SET content=?,revision=revision+1,updated_at=? WHERE id=?", (row["content"],__import__('datetime').datetime.now(__import__('datetime').UTC).isoformat(),row["memory_id"]))
                    self.memory.invalidate_memory_context()
                self.memory.db.execute("UPDATE sync_conflicts SET status=? WHERE id=?", ("accepted" if value.get("accept") is True else "rejected",row["id"]))
        elif op == "certificate":
            if not self.ca:
                raise ValueError("Сначала запусти сервер, чтобы создать сертификат в Keychain.")
            return {"data": self.snapshot(), "download": {"name": "Chushpan-Local-CA.cer", "mime": "application/pkix-cert", "base64": base64.b64encode(self.ca).decode()}}
        elif op != "snapshot":
            raise ValueError("Неизвестное действие синхронизации.")
        return {"data": self.snapshot(), "message": message}

    async def close(self):
        await super().close()
        await self.stop()
