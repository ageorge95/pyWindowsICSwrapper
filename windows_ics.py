import ctypes
import json
import os
import subprocess
import sys
import tempfile

CREATE_NO_WINDOW = 0x08000000
POWERSHELL = "powershell.exe"
DEFAULT_TIMEOUT = 300
DEFAULT_ICS_SCOPE = "192.168.137.1"


class IcsError(RuntimeError):
    pass


_SNAPSHOT_SCRIPT = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$adapterList = foreach ($adapter in (Get-NetAdapter | Sort-Object ifIndex)) {
    $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254.*' } |
        Select-Object -ExpandProperty IPAddress)
    [pscustomobject]@{
        Name        = $adapter.Name
        Description = $adapter.InterfaceDescription
        Status      = [string]$adapter.Status
        IfIndex     = $adapter.ifIndex
        MacAddress  = $adapter.MacAddress
        IPv4        = $addresses
    }
}

$defaultRoute = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Where-Object { $_.NextHop -ne '0.0.0.0' } |
    Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1

$sharingList = @()
try {
    $sharing = New-Object -ComObject HNetCfg.HNetShare
    $sharingList = @(foreach ($connection in $sharing.EnumEveryConnection) {
        $properties = $sharing.NetConnectionProps($connection)
        $configuration = $sharing.INetSharingConfigurationForINetConnection($connection)
        [pscustomobject]@{
            Name           = $properties.Name
            SharingEnabled = [bool]$configuration.SharingEnabled
            ConnectionType = if ($configuration.SharingEnabled) { [int]$configuration.SharingConnectionType } else { -1 }
        }
    })
} catch {
    Write-Output ("Could not query existing sharing: {0}" -f $_.Exception.Message)
}

[pscustomobject]@{
    Adapters          = @($adapterList)
    DefaultRouteAlias = if ($defaultRoute) { [string]$defaultRoute.InterfaceAlias } else { $null }
    Sharing           = $sharingList
} | ConvertTo-Json -Depth 6 -Compress
"""


_SHARE_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicAdapter,
    [Parameter(Mandatory = $true)][string]$PrivateAdapter,
    [Parameter(Mandatory = $true)][string]$ShareIP,
    [int]$PrefixLength = 24
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RegistryKey = "HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters"

function ConvertTo-UInt32([string]$IP) {
    $bytes = [System.Net.IPAddress]::Parse($IP).GetAddressBytes()
    [Array]::Reverse($bytes)
    [BitConverter]::ToUInt32($bytes, 0)
}

function Get-NetworkAddress([string]$IP, [int]$Prefix) {
    $value = ConvertTo-UInt32 $IP
    if ($Prefix -le 0) { return [uint32]0 }
    if ($Prefix -ge 32) { return $value }
    $mask = [uint32]([uint32]::MaxValue -shl (32 - $Prefix))
    return [uint32]($value -band $mask)
}

function Get-SharingConnections {
    $sharing = New-Object -ComObject HNetCfg.HNetShare
    $result = @()
    foreach ($connection in $sharing.EnumEveryConnection) {
        $result += [pscustomobject]@{
            Name   = $sharing.NetConnectionProps($connection).Name
            Config = $sharing.INetSharingConfigurationForINetConnection($connection)
        }
    }
    return $result
}

function Disable-AllSharing {
    foreach ($item in Get-SharingConnections) {
        if ($item.Config.SharingEnabled) {
            Write-Output ("Disabling ICS on {0}" -f $item.Name)
            $item.Config.DisableSharing()
        }
    }
}

if (-not (Get-NetAdapter -Name $PublicAdapter -ErrorAction SilentlyContinue)) {
    throw "Adapter not found: $PublicAdapter"
}
if (-not (Get-NetAdapter -Name $PrivateAdapter -ErrorAction SilentlyContinue)) {
    throw "Adapter not found: $PrivateAdapter"
}
if ($PublicAdapter -eq $PrivateAdapter) {
    throw "The public and private adapter must be different."
}

$parsedIP = $null
if (-not [System.Net.IPAddress]::TryParse($ShareIP, [ref]$parsedIP) -or $parsedIP.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw "Invalid IPv4 address: $ShareIP"
}
if ($PrefixLength -lt 8 -or $PrefixLength -gt 30) {
    throw "PrefixLength must be between 8 and 30."
}
if ($PrefixLength -ne 24) {
    Write-Output "Note: ICS treats the shared network as /24 internally; 24 is the safe choice."
}

$shareNetwork = Get-NetworkAddress $ShareIP $PrefixLength
foreach ($address in (Get-NetIPAddress -InterfaceAlias $PublicAdapter -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
    if ($address.IPAddress -like "169.254.*") { continue }
    if ((Get-NetworkAddress $address.IPAddress $PrefixLength) -eq $shareNetwork) {
        Write-Output ("WARNING: {0} is in the same subnet as the internet adapter address {1}." -f $ShareIP, $address.IPAddress)
    }
}

Write-Output "Disabling any existing sharing..."
Disable-AllSharing

Write-Output ("Setting ICS scope to {0}..." -f $ShareIP)
Set-ItemProperty -Path $RegistryKey -Name ScopeAddress -Value $ShareIP
Set-ItemProperty -Path $RegistryKey -Name ScopeAddressBackup -Value $ShareIP
Set-ItemProperty -Path $RegistryKey -Name StandaloneDhcpAddress -Value $ShareIP

Write-Output ("Configuring {0} as {1}/{2}..." -f $PrivateAdapter, $ShareIP, $PrefixLength)
Set-NetIPInterface -InterfaceAlias $PrivateAdapter -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue
Remove-NetIPAddress -InterfaceAlias $PrivateAdapter -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceAlias $PrivateAdapter -DestinationPrefix "0.0.0.0/0" -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceAlias $PrivateAdapter -IPAddress $ShareIP -PrefixLength $PrefixLength | Out-Null
Set-DnsClientServerAddress -InterfaceAlias $PrivateAdapter -ResetServerAddresses

Write-Output "Enabling IP forwarding..."
Set-NetIPInterface -InterfaceAlias $PublicAdapter -AddressFamily IPv4 -Forwarding Enabled
Set-NetIPInterface -InterfaceAlias $PrivateAdapter -AddressFamily IPv4 -Forwarding Enabled

Write-Output "Enabling Internet Connection Sharing..."
$connections = Get-SharingConnections
$publicConnection = ($connections | Where-Object Name -eq $PublicAdapter | Select-Object -First 1).Config
$privateConnection = ($connections | Where-Object Name -eq $PrivateAdapter | Select-Object -First 1).Config
if (-not $publicConnection -or -not $privateConnection) {
    throw "Could not find the selected connections in the ICS list."
}
$publicConnection.EnableSharing(0)
$privateConnection.EnableSharing(1)
Start-Sleep -Seconds 5

Write-Output ""
Write-Output "=== Result ==="
foreach ($item in (Get-SharingConnections)) {
    if ($item.Config.SharingEnabled) {
        $role = if ($item.Config.SharingConnectionType -eq 0) { "public / internet source" } else { "private / shared LAN" }
        Write-Output ("  {0}: {1}" -f $item.Name, $role)
    }
}
Write-Output ("Clients must use gateway and DNS {0}." -f $ShareIP)
"""


_DISABLE_SCRIPT = r"""
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RegistryKey = "HKLM:\SYSTEM\CurrentControlSet\Services\SharedAccess\Parameters"

$sharing = New-Object -ComObject HNetCfg.HNetShare
foreach ($connection in $sharing.EnumEveryConnection) {
    $properties = $sharing.NetConnectionProps($connection)
    $configuration = $sharing.INetSharingConfigurationForINetConnection($connection)
    if ($configuration.SharingEnabled) {
        Write-Output ("Disabling ICS on {0}" -f $properties.Name)
        $configuration.DisableSharing()
    }
}

Set-ItemProperty -Path $RegistryKey -Name ScopeAddress -Value "192.168.137.1"
Set-ItemProperty -Path $RegistryKey -Name ScopeAddressBackup -Value "192.168.137.1"
Set-ItemProperty -Path $RegistryKey -Name StandaloneDhcpAddress -Value "192.168.137.1"
Write-Output "Sharing disabled and ICS scope reset to default (192.168.137.1)."
"""


_CLIENT_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AdapterName,
    [Parameter(Mandatory = $true)][string]$ServerIP,
    [Parameter(Mandatory = $true)][string]$ClientIP,
    [int]$PrefixLength = 24,
    [string]$Dns = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function ConvertTo-UInt32([string]$IP) {
    $bytes = [System.Net.IPAddress]::Parse($IP).GetAddressBytes()
    [Array]::Reverse($bytes)
    [BitConverter]::ToUInt32($bytes, 0)
}

function Get-NetworkAddress([string]$IP, [int]$Prefix) {
    $value = ConvertTo-UInt32 $IP
    if ($Prefix -le 0) { return [uint32]0 }
    if ($Prefix -ge 32) { return $value }
    $mask = [uint32]([uint32]::MaxValue -shl (32 - $Prefix))
    return [uint32]($value -band $mask)
}

if (-not (Get-NetAdapter -Name $AdapterName -ErrorAction SilentlyContinue)) {
    throw "Adapter not found: $AdapterName"
}

$parsedServer = $null
if (-not [System.Net.IPAddress]::TryParse($ServerIP, [ref]$parsedServer) -or $parsedServer.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw "Invalid server IPv4 address: $ServerIP"
}
$parsedClient = $null
if (-not [System.Net.IPAddress]::TryParse($ClientIP, [ref]$parsedClient) -or $parsedClient.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw "Invalid client IPv4 address: $ClientIP"
}
if ($PrefixLength -lt 8 -or $PrefixLength -gt 30) {
    throw "PrefixLength must be between 8 and 30."
}

if ((Get-NetworkAddress $ServerIP $PrefixLength) -ne (Get-NetworkAddress $ClientIP $PrefixLength)) {
    Write-Output ("WARNING: {0} and {1} are not in the same subnet with prefix /{2}." -f $ServerIP, $ClientIP, $PrefixLength)
}

if ([string]::IsNullOrWhiteSpace($Dns)) { $Dns = $ServerIP }
$dnsServers = @($Dns.Split(",;".ToCharArray()) | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($dnsServers.Count -eq 0) {
    throw "No DNS servers given."
}
foreach ($server in $dnsServers) {
    $parsedDns = $null
    if (-not [System.Net.IPAddress]::TryParse($server, [ref]$parsedDns) -or $parsedDns.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "Invalid DNS IPv4 address: $server"
    }
}

Write-Output ""
Write-Output ("Configuring {0}: {1}/{2}, gateway {3}, DNS {4}..." -f $AdapterName, $ClientIP, $PrefixLength, $ServerIP, ($dnsServers -join ", "))
Set-NetIPInterface -InterfaceAlias $AdapterName -AddressFamily IPv4 -Dhcp Disabled -ErrorAction SilentlyContinue
Remove-NetIPAddress -InterfaceAlias $AdapterName -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceAlias $AdapterName -DestinationPrefix "0.0.0.0/0" -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceAlias $AdapterName -IPAddress $ClientIP -PrefixLength $PrefixLength -DefaultGateway $ServerIP | Out-Null
Set-DnsClientServerAddress -InterfaceAlias $AdapterName -ServerAddresses $dnsServers
Start-Sleep -Seconds 2

Write-Output ""
Write-Output "=== Result ==="
Get-NetIPConfiguration -InterfaceAlias $AdapterName | Format-List InterfaceAlias, IPv4Address, IPv4DefaultGateway, DNSServer | Out-String | Write-Output
Write-Output ("{0} is configured. Gateway and DNS: {1}." -f $AdapterName, $ServerIP)
"""


_DHCP_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AdapterName
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Get-NetAdapter -Name $AdapterName -ErrorAction SilentlyContinue)) {
    throw "Adapter not found: $AdapterName"
}

Write-Output ("Returning {0} to DHCP..." -f $AdapterName)
Set-NetIPInterface -InterfaceAlias $AdapterName -AddressFamily IPv4 -Dhcp Enabled
Remove-NetIPAddress -InterfaceAlias $AdapterName -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
Set-DnsClientServerAddress -InterfaceAlias $AdapterName -ResetServerAddresses
Write-Output ("{0} now uses DHCP." -f $AdapterName)
"""


_DIAGNOSTICS_SCRIPT = r"""
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ServerIP
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$serverOk = Test-Connection -ComputerName $ServerIP -Count 2 -Quiet -ErrorAction SilentlyContinue
Write-Output ("Ping server {0}: {1}" -f $ServerIP, $(if ($serverOk) { "OK" } else { "FAILED" }))

$internetOk = Test-Connection -ComputerName "1.1.1.1" -Count 2 -Quiet -ErrorAction SilentlyContinue
Write-Output ("Ping internet 1.1.1.1: {0}" -f $(if ($internetOk) { "OK" } else { "FAILED (check the sharing machine)" }))

$dnsOk = $false
try {
    $dnsOk = [bool](Resolve-DnsName "www.msftconnecttest.com" -ErrorAction Stop)
} catch {
    $dnsOk = $false
}
Write-Output ("DNS lookup: {0}" -f $(if ($dnsOk) { "OK" } else { "FAILED" }))

[pscustomobject]@{
    Server   = [bool]$serverOk
    Internet = [bool]$internetOk
    Dns      = [bool]$dnsOk
} | ConvertTo-Json -Compress
"""


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(extra_args=None):
    arguments = list(sys.argv[1:])
    if not getattr(sys, "frozen", False):
        arguments.insert(0, os.path.abspath(sys.argv[0]))
    arguments.extend(extra_args or [])
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        subprocess.list2cmdline(arguments),
        os.getcwd(),
        1,
    )
    return int(result) > 32


def run_powershell_script(script, parameters=None, timeout=DEFAULT_TIMEOUT, on_output=None):
    parameters = parameters or {}
    script_path = _write_temp_script(script)
    try:
        command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script_path]
        for name, value in parameters.items():
            if isinstance(value, bool):
                if value:
                    command.append("-" + name)
            elif isinstance(value, (list, tuple)):
                command.append("-" + name)
                command.extend(str(item) for item in value)
            else:
                command.extend(["-" + name, str(value)])
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        lines = []
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            lines.append(line)
            if on_output is not None:
                on_output(line)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise IcsError(f"The operation timed out after {timeout} seconds.")
        if process.returncode != 0:
            raise IcsError(_extract_error(lines, process.returncode))
        return "\n".join(lines)
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


def get_network_snapshot(timeout=60):
    output = run_powershell_script(_SNAPSHOT_SCRIPT, timeout=timeout)
    data = _parse_json_output(output)
    adapters = data.get("Adapters") or []
    if isinstance(adapters, dict):
        adapters = [adapters]
    sharing = data.get("Sharing") or []
    if isinstance(sharing, dict):
        sharing = [sharing]
    return {
        "Adapters": adapters,
        "DefaultRouteAlias": data.get("DefaultRouteAlias"),
        "Sharing": sharing,
    }


def enable_sharing(public_adapter, private_adapter, share_ip, prefix_length=24, on_output=None):
    return run_powershell_script(
        _SHARE_SCRIPT,
        {
            "PublicAdapter": public_adapter,
            "PrivateAdapter": private_adapter,
            "ShareIP": share_ip,
            "PrefixLength": int(prefix_length),
        },
        on_output=on_output,
    )


def disable_all_sharing(on_output=None):
    return run_powershell_script(_DISABLE_SCRIPT, on_output=on_output)


def configure_client(adapter_name, server_ip, client_ip, prefix_length=24, dns="", on_output=None):
    return run_powershell_script(
        _CLIENT_SCRIPT,
        {
            "AdapterName": adapter_name,
            "ServerIP": server_ip,
            "ClientIP": client_ip,
            "PrefixLength": int(prefix_length),
            "Dns": dns or "",
        },
        on_output=on_output,
    )


def set_client_dhcp(adapter_name, on_output=None):
    return run_powershell_script(_DHCP_SCRIPT, {"AdapterName": adapter_name}, on_output=on_output)


def test_connectivity(server_ip, timeout=60):
    output = run_powershell_script(_DIAGNOSTICS_SCRIPT, {"ServerIP": server_ip}, timeout=timeout)
    result = _parse_json_output(output)
    lines = [line for line in output.splitlines() if line.strip() and not line.lstrip().startswith("{")]
    return result, lines


def _write_temp_script(script):
    handle, path = tempfile.mkstemp(prefix="pyics_", suffix=".ps1")
    with os.fdopen(handle, "w", encoding="utf-8-sig") as stream:
        stream.write(script)
    return path


def _extract_error(lines, returncode):
    meaningful = [line for line in lines if line.strip()]
    if not meaningful:
        return f"PowerShell exited with code {returncode}."
    marker = ".ps1 : "
    for line in meaningful:
        if marker in line:
            message = line.split(marker, 1)[1].strip()
            if message:
                return message
    for line in meaningful:
        stripped = line.strip()
        if not stripped.startswith(("+", "At line:")):
            return stripped
    return "\n".join(line.strip() for line in meaningful[-4:])


def _parse_json_output(output):
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise IcsError("Could not parse the PowerShell output.")
