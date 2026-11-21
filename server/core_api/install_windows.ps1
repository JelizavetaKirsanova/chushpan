param(
    [Parameter(Mandatory = $true)]
    [string]$TailnetAddress,

    [Parameter(Mandatory = $true)]
    [string]$AllowedClientAddress,

    [string]$InterfaceAlias = 'Tailscale',
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer from an elevated PowerShell session.'
}

foreach ($address in @($TailnetAddress, $AllowedClientAddress)) {
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($address, [ref]$parsed) -or
        $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'Both addresses must be IPv4 addresses in the Tailscale 100.64.0.0/10 range.'
    }
    $octets = $parsed.GetAddressBytes()
    if ($octets[0] -ne 100 -or $octets[1] -lt 64 -or $octets[1] -gt 127) {
        throw 'Both addresses must be IPv4 addresses in the Tailscale 100.64.0.0/10 range.'
    }
}

$adapter = Get-NetAdapter -Name $InterfaceAlias -ErrorAction Stop
$localAddresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 |
    Select-Object -ExpandProperty IPAddress)
if ($TailnetAddress -notin $localAddresses) {
    throw "The requested listener address is not configured on $InterfaceAlias."
}

$source = $PSScriptRoot
$root = Join-Path $env:ProgramData 'Chushpan\CoreServer'
$app = Join-Path $root 'app'
$logs = Join-Path $root 'logs'
$secrets = Join-Path $root 'secrets'
$taskName = 'ChushpanCoreServer'
$firewallName = 'Chushpan Core API - Tailscale Mac'
$python = (Get-Command python.exe -ErrorAction Stop).Source
$uv = (Get-Command uv.exe -ErrorAction Stop).Source

foreach ($directory in @($app, $logs, $secrets)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

$backupPath = $null
if ((Test-Path (Join-Path $app 'pyproject.toml')) -and
    (Test-Path (Join-Path $app 'uv.lock')) -and
    (Test-Path (Join-Path $app 'src'))) {
    $releases = Join-Path $root 'releases'
    New-Item -ItemType Directory -Path $releases -Force | Out-Null
    $backupPath = Join-Path $releases (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
    New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
    Copy-Item (Join-Path $app 'pyproject.toml') -Destination $backupPath -Force
    Copy-Item (Join-Path $app 'uv.lock') -Destination $backupPath -Force
    Copy-Item (Join-Path $app 'src') -Destination $backupPath -Recurse -Force
}

$previousTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($previousTask -and $previousTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        $previousTask = Get-ScheduledTask -TaskName $taskName
        if ($previousTask.State -ne 'Running') { break }
    }
    if ($previousTask.State -eq 'Running') { throw 'The existing Core Server task did not stop cleanly.' }
}

$tokenPath = Join-Path $secrets 'api-token.dpapi'
if (-not (Test-Path $tokenPath)) {
    Add-Type -AssemblyName System.Security
    $rawToken = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($rawToken)
    }
    finally {
        $random.Dispose()
    }
    $token = [Convert]::ToBase64String($rawToken).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $tokenBytes = [Text.Encoding]::ASCII.GetBytes($token)
    $protected = [Security.Cryptography.ProtectedData]::Protect(
        $tokenBytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    [IO.File]::WriteAllBytes($tokenPath, $protected)
    [Array]::Clear($rawToken, 0, $rawToken.Length)
    [Array]::Clear($tokenBytes, 0, $tokenBytes.Length)
    Remove-Variable token, tokenBytes, protected
}

$serviceSid = '*S-1-5-19'
$systemSid = '*S-1-5-18'
$administratorsSid = '*S-1-5-32-544'
$userSid = "*$($identity.User.Value)"
try {
Copy-Item (Join-Path $source 'pyproject.toml') -Destination $app -Force
Copy-Item (Join-Path $source 'uv.lock') -Destination $app -Force
$activePackage = Join-Path $app 'src\chushpan_core'
if (Test-Path $activePackage) { Remove-Item -LiteralPath $activePackage -Recurse -Force }
Copy-Item (Join-Path $source 'src') -Destination $app -Recurse -Force

& icacls.exe $root /remove:g $serviceSid /T /C /Q > $null
if ($LASTEXITCODE -ne 0) { throw 'Could not remove the unused service-account access rule.' }
$directories = @((Get-Item -LiteralPath $root)) + @(
    Get-ChildItem -LiteralPath $root -Directory -Recurse -Force
)
foreach ($directory in $directories) {
    & icacls.exe $directory.FullName /inheritance:r /grant:r `
        "${systemSid}:(OI)(CI)(F)" `
        "${administratorsSid}:(OI)(CI)(F)" `
        "${userSid}:(OI)(CI)(RX)" /Q > $null
    if ($LASTEXITCODE -ne 0) { throw "Could not apply ACL to $($directory.FullName)." }
}
foreach ($file in Get-ChildItem -LiteralPath $root -File -Recurse -Force) {
    $userRights = if ($file.DirectoryName -eq $logs) { 'M' } else { 'R' }
    & icacls.exe $file.FullName /inheritance:r /grant:r `
        "${systemSid}:F" "${administratorsSid}:F" "${userSid}:$userRights" /Q > $null
    if ($LASTEXITCODE -ne 0) { throw "Could not apply ACL to $($file.FullName)." }
}
& icacls.exe $logs /grant:r "${userSid}:(OI)(CI)(M)" /Q > $null
if ($LASTEXITCODE -ne 0) { throw 'Could not grant the service write access to its log directory.' }

& $uv sync --project $app --locked --no-dev --python $python
if ($LASTEXITCODE -ne 0) { throw 'uv could not synchronize the locked Core API environment.' }

foreach ($file in Get-ChildItem -LiteralPath $app -File -Recurse -Force) {
    & icacls.exe $file.FullName /grant:r "${systemSid}:F" "${administratorsSid}:F" "${userSid}:RX" /Q > $null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure application file $($file.FullName)." }
}
foreach ($directory in Get-ChildItem -LiteralPath $app -Directory -Recurse -Force) {
    & icacls.exe $directory.FullName /grant:r `
        "${systemSid}:(OI)(CI)(F)" `
        "${administratorsSid}:(OI)(CI)(F)" `
        "${userSid}:(OI)(CI)(RX)" /Q > $null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure application directory $($directory.FullName)." }
}

$existingRule = Get-NetFirewallRule -DisplayName $firewallName -ErrorAction SilentlyContinue
if ($existingRule) {
    Set-NetFirewallRule -DisplayName $firewallName -Enabled True -Action Allow -Profile Any
    $addressFilter = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $existingRule
    Set-NetFirewallAddressFilter -InputObject $addressFilter `
        -LocalAddress $TailnetAddress -RemoteAddress $AllowedClientAddress
    $portFilter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $existingRule
    Set-NetFirewallPortFilter -InputObject $portFilter -Protocol TCP -LocalPort $Port
    $interfaceFilter = Get-NetFirewallInterfaceFilter -AssociatedNetFirewallRule $existingRule
    Set-NetFirewallInterfaceFilter -InputObject $interfaceFilter -InterfaceAlias $InterfaceAlias
}
else {
    New-NetFirewallRule -DisplayName $firewallName `
        -Direction Inbound -Action Allow -Enabled True -Profile Any `
        -Protocol TCP -LocalPort $Port `
        -LocalAddress $TailnetAddress -RemoteAddress $AllowedClientAddress `
        -InterfaceAlias $InterfaceAlias -EdgeTraversalPolicy Block | Out-Null
}

$pythonInVenv = Join-Path $app '.venv\Scripts\python.exe'
$arguments = "-m chushpan_core --host $TailnetAddress --port $Port"
$action = New-ScheduledTaskAction -Execute $pythonInVenv -Argument $arguments -WorkingDirectory $app
$trigger = New-ScheduledTaskTrigger -AtStartup -RandomDelay (New-TimeSpan -Seconds 30)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $identity.Name `
    -LogonType S4U `
    -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $taskPrincipal
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $taskPrincipal `
    -Description 'Chushpan Core API, restricted to the Huawei Tailscale interface and approved Mac client.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
$listenerReady = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 2
    $listeners = @(Get-NetTCPConnection -State Listen -LocalAddress $TailnetAddress `
        -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) { $listenerReady = $true; break }
}
if (-not $listenerReady) { throw 'The Core API did not open its Tailscale listener.' }
$registeredTask = Get-ScheduledTask -TaskName $taskName
Write-Output "Installed $taskName; state=$($registeredTask.State); listener=$($TailnetAddress):$Port; allowed-client=$AllowedClientAddress; backup=$backupPath."
}
catch {
    $failure = $_.Exception.Message
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($backupPath) {
        $activePackage = Join-Path $app 'src\chushpan_core'
        if (Test-Path $activePackage) { Remove-Item -LiteralPath $activePackage -Recurse -Force }
        Copy-Item (Join-Path $backupPath 'pyproject.toml') -Destination $app -Force
        Copy-Item (Join-Path $backupPath 'uv.lock') -Destination $app -Force
        Copy-Item (Join-Path $backupPath 'src') -Destination $app -Recurse -Force
        & $uv sync --project $app --locked --no-dev --python $python
        if ($LASTEXITCODE -ne 0) { throw "Update failed and the previous venv could not be restored from ${backupPath}: $failure" }
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $taskName
        }
        throw "Update failed; the previous application code was restored from $backupPath. Cause: $failure"
    }
    throw "Initial Core API installation failed. No previous application release was present. Cause: $failure"
}
