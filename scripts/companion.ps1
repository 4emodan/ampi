<#
Installs the Ampi companion app over USB and pairs it with your server.

  companion\build.ps1 -Release          build the app (once per version)
  scripts\companion.ps1 -Install        install or update it (USB debugging on, phone unlocked)
  scripts\companion.ps1 -Pair           pair it (over the cable and your SSH; nothing on the network)
  scripts\companion.ps1 -Status         what the server sees

Switch USB debugging off again afterwards.
#>
param(
    [switch]$Install, [switch]$Pair, [switch]$Status,
    [string]$Server = "pipa-home",
    [string]$Name = "phone",
    [string]$Apk = (Join-Path $PSScriptRoot "..\companion\out\ampi-companion.apk"),
    [string]$Serial
)
$ErrorActionPreference = "Stop"
$OutputEncoding = New-Object System.Text.ASCIIEncoding
$pkg = "app.ampi.companion"
$adb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
if (-not (Test-Path $adb)) { $adb = "adb" }

function Invoke-Adb {
    $a = @(); if ($Serial) { $a += "-s", $Serial }
    $out = & $adb @a @args
    if ($LASTEXITCODE -ne 0) { throw "adb $($args[0]) failed: $out" }
    $out
}
function Invoke-Server([string]$Command, [string]$In) {
    if ($In) { $out = $In | & ssh $Server $Command } else { $out = & ssh $Server $Command }
    if ($LASTEXITCODE -ne 0) { throw "the server said no to: $Command $out" }
    $out
}

if ($Install) {
    Invoke-Adb install -r $Apk | Out-Host
}
if ($Pair) {
    $offer = (Invoke-Server "ampi-phone offer") -join ""
    $p = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($offer)).Replace("+", "-").Replace("/", "_").TrimEnd("=")
    # Only adb may start this screen (it needs android.permission.DUMP).
    $started = Invoke-Adb shell am start -W -n "$pkg/.PairActivity" --es offer $p
    if ("$started" -match "Error|Exception") { throw "the pairing screen didn't open: $started" }
    Write-Host "Look at your phone and confirm the pairing there. Waiting (2 minutes)..."
    $cert = $null
    for ($i = 0; $i -lt 60 -and -not $cert; $i++) {
        Start-Sleep -Seconds 2
        try { $r = (Invoke-Adb shell am broadcast -n "$pkg/.PairReceiver" -a "$pkg.CERT") -join " " }
        catch { continue }  # the phone was busy: ask again
        if ($r -match 'result=1, data="([A-Za-z0-9+/=]+)"') { $cert = $Matches[1] }
        elseif ($r -match 'result=2') { throw "the phone is already paired: unpair it in the app first" }
    }
    if (-not $cert) { throw "nothing was confirmed on the phone" }
    $code = ((Invoke-Server "ampi-phone code" $cert) -join "").Trim()
    Write-Host ""
    Write-Host "    $code" -ForegroundColor Cyan
    Write-Host ""
    $yes = Read-Host "Does the phone show exactly this code? (yes/no)"
    if ($yes -ne "yes") { throw "stopped: nothing was paired. Cancel on the phone too." }
    Invoke-Server "ampi-phone add $Name --code '$code'" $cert | Out-Host
    Write-Host "Now press 'The codes match' on the phone, and switch USB debugging off when you're done."
}
if ($Status) {
    Invoke-Server "ampi-phone status" | Out-Host
}
if (-not ($Install -or $Pair -or $Status)) { Get-Help $PSCommandPath }