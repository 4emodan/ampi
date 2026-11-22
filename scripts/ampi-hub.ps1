<#
.SYNOPSIS
  Opens the Ampi hub (your settings pages on the server) in your browser through an SSH tunnel.

.DESCRIPTION
  The hub listens only on a Unix socket on the server (/run/ampi-hub/hub.sock), with no network port. This script:
    1. has the server write a fresh access token and reads it over SSH (the previous one stops working),
    2. forwards 127.0.0.1:PORT on this computer to the hub's socket (ssh -L),
    3. checks that the port really belongs to that ssh process, then opens the browser with the token in the
       URL fragment (# ...), which the browser never sends anywhere.
  Leave this window open while you use the page; press Enter (or close the window) to close the tunnel.

.EXAMPLE
  .\ampi-hub.ps1
  .\ampi-hub.ps1 -Server pipa-home -Port 8765
#>
param(
    [string]$Server = "pipa-home",   # an SSH host alias from ~/.ssh/config (logs in as root)
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$socket = "/run/ampi-hub/hub.sock"

function Test-PortFree([int]$p) {
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $p)
        $l.Start(); $l.Stop()
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-PortFree $Port)) {
    Write-Host "Port $Port on this computer is already in use (an old tunnel still open?). Close it, or run with -Port 8766." -ForegroundColor Red
    exit 1
}

Write-Host "Getting a fresh access token from $Server ..."
$raw = & $ssh -o ConnectTimeout=15 $Server "/usr/local/sbin/ampi-hub-token"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Couldn't get a token. Is the hub running?  ssh $Server systemctl status ampi-hub" -ForegroundColor Red
    exit 1
}
$token = ("$raw").Trim()
if ($token -notmatch '^[A-Za-z0-9_-]{32,100}$') {
    Write-Host "The token looks wrong; not opening anything." -ForegroundColor Red
    exit 1
}

Write-Host "Opening the tunnel 127.0.0.1:$Port -> ${Server}:$socket ..."
$sshArgs = @("-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
             "-o", "ConnectTimeout=15", "-L", "127.0.0.1:${Port}:$socket", $Server)
$proc = Start-Process -FilePath $ssh -ArgumentList $sshArgs -NoNewWindow -PassThru

try {
    # Wait until our ssh listens on the port; refuse if anything else does (it would get the token).
    $ready = $false
    # Up to 5 minutes: ssh may be waiting for your key passphrase.
    for ($i = 0; $i -lt 600; $i++) {
        if ($proc.HasExited) { break }
        $listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count -gt 0) {
            if (@($listeners | Where-Object { $_.OwningProcess -ne $proc.Id }).Count -gt 0) {
                Write-Host "Something other than this tunnel listens on port $Port; not opening the browser." -ForegroundColor Red
                exit 1
            }
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        Write-Host "The tunnel didn't come up (see the ssh message above)." -ForegroundColor Red
        exit 1
    }

    $url = "http://127.0.0.1:$Port/address-book#t=$token"
    if ($NoBrowser) {
        Write-Host "Open this in your browser: $url"
    } else {
        Start-Process $url
        Write-Host "Opened the address book in your browser." -ForegroundColor Green
    }
    Write-Host "Keep this window open while you use the hub. Press Enter to close the tunnel."
    $console = $true
    try { $null = [Console]::KeyAvailable } catch { $console = $false }
    if ($console) {
        while (-not $proc.HasExited) {
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true)
                if ($key.Key -eq "Enter") { break }
            }
            Start-Sleep -Milliseconds 300
        }
    } else {
        $null = Read-Host "Press Enter to close the tunnel"  # a host without a console (e.g. PowerShell ISE)
    }
    if ($proc.HasExited) { Write-Host "The SSH connection closed." -ForegroundColor Yellow }
} finally {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "Tunnel closed."
}
