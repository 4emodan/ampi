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
    if ($In) { $out = $In | & ssh $Server $Command } else { $out = & ssh -n $Server $Command }
    if ($LASTEXITCODE -ne 0) { throw "the server said no to: $Command $out" }
    $out
}

if ($Install) {
    Invoke-Adb install -r $Apk | Out-Host
}
if ($Pair) {
    # The app may have reached the phone some other way (a chat): it must be exactly the file built here.
    $there = "$(Invoke-Adb shell -n pm path $pkg)".Trim() -replace '^package:', ''
    $sum = ("$(Invoke-Adb shell -n sha256sum $there)".Trim() -split '\s+')[0]
    if ($sum -ne (Get-FileHash -Algorithm SHA256 $Apk).Hash) { throw "the app on the phone isn't $Apk" }
    # Both sides must have forgotten an earlier pairing, before any codes are compared.
    $state = ""
    for ($i = 0; $i -lt 5 -and $state -notmatch 'result='; $i++) {
        try { $state = (Invoke-Adb shell -n am broadcast -n "$pkg/.PairReceiver" -a "$pkg.CERT") -join " " }
        catch { Start-Sleep -Seconds 2 }  # the phone was busy: ask again
    }
    if ($state -match 'result=2') { throw "the phone is already paired: unpair it in the app first (Ampi, at the bottom)" }
    # Removed only once the codes were compared; none of them is this phone any more (it isn't paired).
    $old = @()
    foreach ($line in @(Invoke-Server "ampi-phone devices")) {
        $known = ("$line" -split '\s')[0]
        if (-not $known) { continue }
        Write-Host "The server still knows a phone: $line"
        $answer = Read-Host "Remove it when this one is paired? (yes/no)"
        if ($answer -eq "yes") { $old += $known }
        elseif ($known -eq $Name) { throw "stopped, nothing was changed: a phone called $Name is already there (use -Name for another)" }
    }
    $offer = (Invoke-Server "ampi-phone offer") -join ""
    $p = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($offer)).Replace("+", "-").Replace("/", "_").TrimEnd("=")
    # Only adb may start this screen (it needs android.permission.DUMP).
    $started = Invoke-Adb shell -n am start -W -n "$pkg/.PairActivity" --es offer $p
    if ("$started" -match "Error|Exception") { throw "the pairing screen didn't open: $started" }
    Write-Host "Look at your phone and confirm the pairing there. Waiting (2 minutes)..."
    $cert = $null
    for ($i = 0; $i -lt 60 -and -not $cert; $i++) {
        Start-Sleep -Seconds 2
        try { $r = (Invoke-Adb shell -n am broadcast -n "$pkg/.PairReceiver" -a "$pkg.CERT") -join " " }
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
    foreach ($known in $old) { Invoke-Server "ampi-phone remove $known" | Out-Host }
    Invoke-Server "ampi-phone add $Name --code '$code'" $cert | Out-Host
    Write-Host "Now press 'The codes match' on the phone, and switch USB debugging off when you're done."
}
if ($Status) {
    Invoke-Server "ampi-phone status" | Out-Host
}
if (-not ($Install -or $Pair -or $Status)) { Get-Help $PSCommandPath }