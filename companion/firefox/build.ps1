<#
Packs the Firefox extension: out\ampi-session.zip, to upload at https://addons.mozilla.org/developers/ ("On your own": signed, not
listed). Install the signed file in Firefox for Android: Settings, About Firefox, tap the logo five times, then Settings, Install
extension from file.

Each owner needs an id of their own (Mozilla signs an id for one account only): made once, kept in %USERPROFILE%\.ampi-companion.
#>
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$keep = Join-Path $env:USERPROFILE ".ampi-companion"
$idFile = Join-Path $keep "firefox-id.txt"
if (-not (Test-Path $idFile)) {
    New-Item -ItemType Directory -Force $keep | Out-Null
    $bytes = New-Object byte[] 8
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($idFile, "ampi-session-" + (($bytes | ForEach-Object { $_.ToString("x2") }) -join "") + "@ampi.invalid")
}
$id = [IO.File]::ReadAllText($idFile).Trim()
$out = Join-Path $here "out"
$dir = Join-Path $out "files"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force $dir | Out-Null
foreach ($f in "manifest.json", "popup.html", "popup.css", "popup.js", "logic.js", "icon.svg") {
    $text = [IO.File]::ReadAllText((Join-Path $here $f)).Replace("@ID@", $id)
    [IO.File]::WriteAllText((Join-Path $dir $f), $text)
}
Compress-Archive -Path (Join-Path $dir "*") -DestinationPath (Join-Path $out "ampi-session.zip")
Write-Host (Join-Path $out "ampi-session.zip")
