# Huawei Core Server

## Deployment state

The HTTPS Core API is deployed on Huawei and was checked from this Mac on
2026-11-22. `https://100.117.129.107:8765/health` returned 200 with the pinned
certificate, and an unauthenticated `/v1/status` request returned 401. A
temporary, scope-limited device passed signed status, received 403 for memory
and chat without those scopes, and was revoked; its private key was ephemeral.
The actual Mac Keychain identity has not been paired, and the Mac remains in
Local mode.

The current source release is isolated in `server/core_api`; it does not import
the macOS backend or open `data/jarvis.db`. It serves HTTPS at
`https://100.117.129.107:8765`. The installer binds only to
Huawei's Tailscale address, and its Windows Firewall rule is limited to the
Tailscale interface, port 8765, and this Mac's Tailscale address
`100.123.222.86`. No router or public-internet rule is used.

## Responsibility split

| Component | Responsibility |
| --- | --- |
| Huawei Core | Device-authenticated API, separate central memory, bounded LLM queue, OpenAI calls, health and logs |
| Mac Agent | Existing UI, local memory until explicitly selected for transfer, microphone, STT/TTS, wake word, macOS permissions, Safari, apps, Shortcuts and approved tool execution |
| iPhone/iPad | Future independent paired API clients over Tailscale; no clients were built or paired in this stage |
| Future Kinect adapter | Optional Windows device adapter; not installed, enabled, or physically tested |

The Mac's local SQLite memory and settings stay in place. Server mode sends the
current prompt and applicable Mac-generated context to Huawei over TLS; Huawei
then sends the request to OpenAI when a key has been provisioned. Imported Core
memory is searched on Huawei and added as bounded reference context only for
devices with `memory.read`. Memory text is treated as data, not instructions.
The existing voice, audio, macOS and device command implementations remain on
the Mac.

## Security and data

- The new transport is HTTPS-only, using a self-signed P-256 certificate bound
  to the Huawei Tailscale IP. The Mac validates and pins the public certificate
  pasted from an authenticated SSH session; it never accepts an arbitrary CA.
- Pairing uses a five-minute, single-use invitation. Each paired device gets a
  P-256 identity; its private key is stored in the Mac's `ThisDeviceOnly`
  Keychain item. Huawei stores public identities, scopes, replay nonces, and
  request IDs. A Mac can manage devices; iPhone/iPad grants cannot.
- The old DPAPI bootstrap credential and `/v1/status` route remain available on
  HTTPS during transition. They are not used by the new Mac client. The live
  deployment confirmed that requests without a token are rejected.
- TLS private key and the optional OpenAI key are protected with machine-scope
  Windows DPAPI and stored beneath
  `C:\ProgramData\Chushpan\CoreServer\secrets` or `tls`. Logs are bounded and
  omit prompts, memory contents, and credentials.
- The Core API is not configured with an OpenAI key in this stage. No live or
  paid LLM call was made. Provision a key interactively over SSH only if you
  choose to enable Core OpenAI; the CLI hides input and stores it using DPAPI.
- OpenAI is the default Core LLM provider. An optional Ollama adapter accepts
  only Huawei loopback port 11434 and can be selected with
  `CHUSHPAN_CORE_LLM_PROVIDER=ollama`; no model is installed or probed. The
  `auto` Mac mode may separately fall back to the Mac's configured Ollama before
  Huawei emits a response. Huawei Ollama is not enabled; evaluate RAM and model
  requirements on the actual Huawei before selecting it.

## Core memory

The server database is separate from the device registry:

- `data/core.sqlite3`: device grants, invitations, replay nonces and request IDs.
- `data/memory.sqlite3`: versioned SQLite/FTS5 central memory (schema version 2).

Memory supports scoped list/search/create/update/history/delete APIs, category
and source fields, deduplication, private mode, and validated SQLite backups.
Core chat searches at most five relevant records and caps added memory context
at 6,000 characters. Requests and queues are bounded; one LLM stream runs at a
time, with up to eight active-and-waiting requests. Disconnecting a client cancels the local
stream and releases the queue; upstream provider cancellation cannot be
guaranteed once OpenAI has received the request.

No local memory has been copied. In the Mac settings under **Server Huawei**,
the explicit transfer flow is implemented: create a read-only preview, select
individual eligible records, confirm the transfer, and optionally roll back
the latest transfer. Sensitive, secret-like, inactive, and unsupported
categories are excluded. A transfer is atomic and idempotent; rollback removes
only entries from that import that remain unchanged. This UI has not been used
to send any user data.

On Huawei, after secure deployment, the operator can make and check a backup
over SSH:

```powershell
& 'C:\ProgramData\Chushpan\CoreServer\app\.venv\Scripts\python.exe' -m chushpan_core.admin backup-memory
& 'C:\ProgramData\Chushpan\CoreServer\app\.venv\Scripts\python.exe' -m chushpan_core.admin verify-memory-backup --name memory-YYYYMMDDTHHMMSSZ.sqlite3
```

Backups are created beneath `C:\ProgramData\Chushpan\CoreServer\backups`;
the CLI refuses to overwrite a backup and validates it after writing. The
tested restore helper only restores into a new, non-existent destination. It
does not replace the active memory database.

## Pair the real MacBook

The current endpoint is live. From the repository root, the deployment command
can be run again when updating the server:

```bash
.venv/bin/python scripts/deploy_huawei_core.py
```

The script stages only `server/core_api`, installs the locked environment,
protects the TLS key with DPAPI, updates the existing Core Task Scheduler job,
and restricts the API firewall rule to this Mac. It retains prior code releases
and restores code if startup checks fail. It does not copy `.env`, `private/`,
`data/`, memory, conversations, or keys. Before the successful deployment the
installer was corrected to stop only matching Core processes; TLS loading was
also checked on the real Windows host.

To pair the actual Mac, issue a one-time invite from an interactive SSH
PowerShell session on the Mac:

```powershell
ssh huawei-remote
& 'C:\ProgramData\Chushpan\CoreServer\app\.venv\Scripts\python.exe' -m chushpan_core.admin invite --host 100.117.129.107 --name MacBook --kind mac
```

Copy the public certificate over the same SSH session:

```powershell
Get-Content -Raw 'C:\ProgramData\Chushpan\CoreServer\tls\server-cert.pem'
```

In Chushpan, open **Settings → Server Huawei**, paste the HTTPS address, public
certificate, and one-time code, then select **Pair MacBook**. Pairing leaves
the app in Local mode. Check the server and device list first. Select Server
mode only after the OpenAI key is provisioned; `auto` keeps the existing local
Ollama as fallback. The OpenAI key is provisioned on Huawei over SSH, never
entered in Mac settings.

## Stage boundaries

This stage adds and deploys the independent Core API, signed Mac client, local
settings integration, and opt-in memory transfer. The following remain for a
later stage: pairing the real Mac Keychain identity; OpenAI key provisioning
and a user-approved paid smoke request; iPhone/iPad clients; Huawei scenario
scheduling and home-device adapters; and Kinect assessment. The Mac's existing
memory is not moved automatically.
