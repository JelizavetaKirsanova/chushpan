import gzip

import pytest

from scripts.deploy_huawei_core import compressed_installer

INSTALLER = """param(
    [Parameter(Mandatory = $true)]
    [string]$TailnetAddress,
    [Parameter(Mandatory = $true)]
    [string]$AllowedClientAddress
)
$source = $PSScriptRoot
Write-Output "ready"
"""


def test_installer_is_compressed_and_binds_tailnet_addresses():
    compressed = compressed_installer(
        INSTALLER,
        huawei_address="100.117.129.107",
        mac_address="100.123.222.86",
    )
    body = gzip.decompress(compressed).decode("utf-8")
    assert len(compressed) < len(INSTALLER.encode("utf-8"))
    assert '$TailnetAddress = "100.117.129.107"' in body
    assert '$AllowedClientAddress = "100.123.222.86"' in body
    assert '$source = "C:\\Users\\olgan\\AppData\\Local\\Temp\\chushpan-core-stage"' in body


def test_compressed_installer_rejects_addresses_outside_tailnet():
    with pytest.raises(ValueError, match="Tailscale"):
        compressed_installer(
            INSTALLER,
            huawei_address="8.8.8.8",
            mac_address="100.123.222.86",
        )
