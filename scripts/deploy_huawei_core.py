"""Stage and install only the isolated Core API on the Tailscale-connected Huawei."""

from __future__ import annotations

import argparse
import gzip
import ipaddress
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

REPOSITORY = Path(__file__).resolve().parents[1]
PROJECT = REPOSITORY / "server" / "core_api"
REMOTE_STAGE = r"C:\Users\olgan\AppData\Local\Temp\chushpan-core-stage"
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _validate_tailnet_ipv4(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.version != 4 or address not in TAILSCALE_NETWORK:
        raise ValueError("Expected an IPv4 address in Tailscale's 100.64.0.0/10 range")
    return str(address)


def compressed_installer(
    installer: str,
    *,
    huawei_address: str,
    mac_address: str,
    stage_path: str = REMOTE_STAGE,
) -> bytes:
    """Bind the target addresses and compress the script before SSH transfer."""
    huawei_address = _validate_tailnet_ipv4(huawei_address)
    mac_address = _validate_tailnet_ipv4(mac_address)
    body, matches = re.subn(
        r"\Aparam\(.*?^\)\s*",
        "",
        installer,
        count=1,
        flags=re.DOTALL | re.MULTILINE,
    )
    if matches != 1:
        raise ValueError("Could not find the install_windows.ps1 parameter block")
    source_assignment = "$source = $PSScriptRoot"
    if source_assignment not in body:
        raise ValueError("Installer does not contain its expected source directory")
    body = body.replace(source_assignment, f'$source = "{stage_path}"', 1)
    body = (
        f'$TailnetAddress = "{huawei_address}"\n'
        f'$AllowedClientAddress = "{mac_address}"\n'
        '$InterfaceAlias = "Tailscale"\n'
        "$Port = 8765\n" + body
    )

    return gzip.compress(body.encode("utf-8"), mtime=0)


def loader_command(stage_path: str = REMOTE_STAGE) -> str:
    """Return a short, single-line PowerShell loader that leaves policy unchanged."""
    escaped_path = stage_path.replace("'", "''")
    compressed_path = f"{escaped_path}\\install_windows.ps1.gz"
    return (
        "$ErrorActionPreference='Stop';try{"
        f"$m=[IO.File]::OpenRead('{compressed_path}');"
        "try{$g=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);"
        "try{$r=[IO.StreamReader]::new($g,[Text.Encoding]::UTF8);"
        "$s=$r.ReadToEnd();$r.Dispose()}finally{$g.Dispose()};"
        "&([ScriptBlock]::Create($s))}finally{$m.Dispose()}"
        "}catch{[Console]::Error.WriteLine($_.Exception.Message);exit 1}"
    )


def deploy(
    *,
    ssh_host: str,
    huawei_address: str,
    mac_address: str,
    project_dir: Path = PROJECT,
) -> None:
    stage_path = REMOTE_STAGE.replace("\\", "/")
    subprocess.run(
        [
            "ssh",
            ssh_host,
            "cmd.exe",
            "/c",
            f"if not exist {stage_path} mkdir {stage_path}",
        ],
        check=True,
    )
    staged_files = sorted(project_dir.iterdir())
    if not staged_files:
        raise ValueError("The Core API source directory is empty")
    subprocess.run(
        ["scp", "-r", *(str(path) for path in staged_files), f"{ssh_host}:{stage_path}/"],
        check=True,
    )
    installer = (project_dir / "install_windows.ps1").read_text(encoding="utf-8")
    compressed = compressed_installer(
        installer,
        huawei_address=huawei_address,
        mac_address=mac_address,
        stage_path=REMOTE_STAGE,
    )
    with tempfile.TemporaryDirectory(prefix="chushpan-core-deploy-") as temporary_directory:
        compressed_file = Path(temporary_directory) / "install_windows.ps1.gz"
        compressed_file.write_bytes(compressed)
        subprocess.run(
            [
                "scp",
                str(compressed_file),
                f"{ssh_host}:{stage_path}/install_windows.ps1.gz",
            ],
            check=True,
        )
    subprocess.run(
        [
            "ssh",
            ssh_host,
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "-",
        ],
        input=loader_command().encode("utf-8") + b"\n",
        check=True,
    )
    verify_api(huawei_address)


def verify_api(huawei_address: str) -> None:
    """Check the public probe and confirm protected status rejects no-token calls."""
    address = _validate_tailnet_ipv4(huawei_address)
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://{address}:8765/health", timeout=5) as response:
        health = response.read().decode("utf-8")
        if response.status != 200 or '"service":"chushpan-core"' not in health.replace(" ", ""):
            raise RuntimeError("Huawei Core /health did not return the expected service status")

    request = Request(f"http://{address}:8765/v1/status")
    try:
        opener.open(request, timeout=5)
    except HTTPError as exc:
        if exc.code != 401:
            raise RuntimeError(f"Protected API returned unexpected HTTP {exc.code}") from exc
    else:
        raise RuntimeError("Protected API accepted a request without authorization")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", default="huawei-remote")
    parser.add_argument("--huawei-address", default="100.117.129.107")
    parser.add_argument("--mac-address", default="100.123.222.86")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        if args.dry_run:
            installer = (PROJECT / "install_windows.ps1").read_text(encoding="utf-8")
            compressed = compressed_installer(
                installer,
                huawei_address=args.huawei_address,
                mac_address=args.mac_address,
            )
            print(
                f"Ready: SSH host={args.ssh_host}; compressed installer={len(compressed)} bytes; "
                f"loader={len(loader_command())} chars"
            )
            return 0
        deploy(
            ssh_host=args.ssh_host,
            huawei_address=args.huawei_address,
            mac_address=args.mac_address,
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Huawei Core deployment failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
