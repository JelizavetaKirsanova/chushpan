# Huawei Core Server

## Audit and boundary

The current Chushpan app is a macOS desktop application. Its companion server in
`backend/sync/service.py` uses macOS-only `KeychainIdentity` from
`backend/sync/identity.py`; the HUD and local WebSocket are intentionally bound
to loopback. The project dependency set also includes macOS audio, MLX, BLE,
Safari, and UI packages, so it is not a safe Windows server install target.

`server/core_api` is an independent Python project and process. The first release
does not import Chushpan's desktop backend or open its database. It exposes:

- `GET /health`: low-information liveness probe.
- `GET /v1/status`: same basic service status, protected by a bearer token.

On the current tailnet the listener is the Huawei Tailscale address
`100.117.129.107:8765`; Windows Firewall accepts that port only on the Tailscale
interface and only from this Mac's observed Tailscale address
`100.123.222.86`. No router port forwarding or public listener is configured.
The Tailscale WireGuard path encrypts transport; the API additionally requires
application authentication for protected routes. Add future iPhone/iPad
Tailscale addresses through the reviewed deployment script before using them.

The API token is generated on Huawei, protected using machine-scope Windows
DPAPI, and stored under `C:\ProgramData\Chushpan\CoreServer\secrets` with a
restricted ACL. The service writes bounded, content-free logs to
`C:\ProgramData\Chushpan\CoreServer\logs\core.log`. Windows Task Scheduler
runs the process at startup under the installing account's limited S4U logon
(without storing its Windows password). A supervisor restarts the API worker
with backoff after unexpected exits; Task Scheduler also has a boot trigger and
retry settings. This account can read the installed per-user Python runtime;
move to a dedicated service identity before adding memory or device actions.

## Responsibility split

| Component | Responsibility |
| --- | --- |
| Huawei Core | Authenticated API, future shared memory service, LLM calls, scenario scheduler, device adapters, diagnostics and monitoring |
| Mac Agent | Existing macOS UI, microphone, speakers, STT/TTS, macOS permissions, Safari, applications and Shortcuts |
| iPhone/iPad clients | Independent API clients over Tailscale, with per-device credentials and revocation planned before broader API access |
| Future Kinect adapter | Windows-specific optional integration behind a narrow device capability interface; not installed or enabled in this release |

## Safe evolution plan

1. Define versioned API schemas and client pairing/revocation. Replace the
   bootstrap bearer token with per-device grants before exposing memory or
   actions. Keep `health` separate from authenticated status.
2. Add a Huawei-owned memory store with explicit backup, retention, provenance,
   deletion, and migration review. The existing `data/jarvis.db` stays on Mac;
   no data is copied by this deployment.
3. Add LLM provider adapters with keys stored in Windows-protected credentials,
   bounded request sizes/timeouts, and redacted logs. No model key is deployed in
   this first release.
4. Keep macOS actions behind an authenticated Mac Agent capability channel.
   The Huawei process must not execute arbitrary shell commands on Mac.
5. Add device integrations one adapter at a time, with explicit capabilities,
   authorization, idempotency, timeouts, and physical-device testing.
6. Add Kinect only after Windows SDK/device availability and consent are
   verified; it remains an optional plugin and does not gate Core startup.

Updates can be staged with `scripts/deploy_huawei_core.py`. It copies only
`server/core_api`, uses the pinned `uv.lock`, restricts the Firewall rule to the
configured Tailscale client, preserves the existing DPAPI token, and keeps a
code-only prior release under `C:\ProgramData\Chushpan\CoreServer\releases`.
If its listener check fails, it restores that release. The helper then verifies
`/health` and that `/v1/status` rejects requests without authorization. The
script never packages `.env`, `private/`, `data/`, databases, exports, or
credential files.
