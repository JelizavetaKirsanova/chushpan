# Chushpan Core API

An isolated Windows-compatible FastAPI process. It does not import the macOS app,
local Keychain identity, microphone/audio stack, local memory database, or device
adapters. Its first release exposes an unauthenticated, low-information `/health`
probe and a bearer-protected `/v1/status` route.

The Windows deployment script provisions a Python 3.13 virtual environment,
machine-scope DPAPI token, restricted file ACLs, a Tailscale-interface-only
Windows Firewall rule, and a Task Scheduler startup job. It does not copy user
data or the existing Chushpan memory database.

The scheduled process supervises an API worker and retries unexpected worker
exits with bounded backoff. Both processes write bounded logs under
`C:\ProgramData\Chushpan\CoreServer\logs`.

See [the deployment and architecture notes](../../docs/HUAWEI_CORE_SERVER.md).
