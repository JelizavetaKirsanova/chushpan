# Chushpan Core API

An isolated HTTPS service for Windows. It uses machine-scope DPAPI for TLS and
provider secrets, Tailscale-only binding/firewall policy, P-256 device identities,
scoped signed requests, and a separate SQLite/FTS5 memory database. It does not
import the macOS backend or read the Mac memory database.

The API includes device pairing and revocation, authenticated status, memory
CRUD/search/history, bounded/idempotent selected-memory import with rollback,
and streamed OpenAI chat. Core chat uses only explicit Core memory records with
the caller's `memory.read` scope. The client remains responsible for local
microphone, speech, macOS tools, and their existing permission checks.

The Windows installer creates a Python 3.13 environment, a startup Task
Scheduler task, a worker supervisor, protected directories and a firewall rule
limited to the configured Huawei and Mac Tailscale addresses. It retains the
HTTPS bootstrap status route for the migration window. See
[deployment and architecture notes](../../docs/HUAWEI_CORE_SERVER.md).

The HTTPS release was deployed and checked on the real Huawei on 2026-11-22.
The Mac-to-Huawei health probe succeeded with the pinned certificate, and the
bootstrap status endpoint rejected a request without authorization. A temporary
device protocol test also verified signed status, scope denial, and revocation.
The production Mac Keychain identity remains unpaired; no Mac memory was
transferred and the OpenAI provider credential is not configured.
