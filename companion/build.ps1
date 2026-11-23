<#
Builds the companion app with the Android SDK's own tools (no Gradle, no libraries, no downloads).

  companion\build.ps1             out\ampi-companion-debug.apk, signed with the Android debug key (emulator only)
  companion\build.ps1 -Release    out\ampi-companion.apk, signed with your key in %USERPROFILE%\.ampi-companion

The release key is made on first use; keytool and apksigner ask you for its password. Keep a copy of that folder offline:
without it the app can only be reinstalled (and paired again).
#>
param([switch]$Release)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:LOCALAPPDATA "Android\Sdk" }
$tools = Join-Path $sdk "build-tools\36.0.0"
$android = Join-Path $sdk "platforms\android-36\android.jar"
$jbr = "C:\Program Files\Android\Android Studio\jbr"
if (-not (Test-Path "$jbr\bin\javac.exe")) { $jbr = $env:JAVA_HOME }
$java = "$jbr\bin\java.exe"
$out = Join-Path $here "out"
if (Test-Path $out) { Remove-Item "$out\classes", "$out\gen", "$out\dex", "$out\*.apk", "$out\*.zip" -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force "$out\classes", "$out\gen", "$out\dex" | Out-Null

function Step([string]$What, [scriptblock]$Do) {
    & $Do
    if ($LASTEXITCODE -ne 0) { throw "$What failed" }
}
Step "resources" { & "$tools\aapt2.exe" compile --dir "$here\res" -o "$out\res.zip" }
Step "manifest" { & "$tools\aapt2.exe" link -o "$out\base.apk" -I $android --manifest "$here\AndroidManifest.xml" --java "$out\gen" "$out\res.zip" }
$sources = @(Get-ChildItem "$here\src", "$out\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
Step "javac" { & "$jbr\bin\javac.exe" -encoding UTF-8 --release 17 "-Xlint:all,-classfile,-serial" -classpath $android -d "$out\classes" $sources }
$classes = @(Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName })
Step "d8" { & $java -cp "$tools\lib\d8.jar" com.android.tools.r8.D8 --release --min-api 36 --lib $android --output "$out\dex" $classes }
Push-Location "$out\dex"
try { Step "packaging" { & "$jbr\bin\jar.exe" uMf "$out\base.apk" classes.dex } } finally { Pop-Location }
Step "zipalign" { & "$tools\zipalign.exe" -f -p 4 "$out\base.apk" "$out\aligned.apk" }

if ($Release) {
    $keys = Join-Path $env:USERPROFILE ".ampi-companion"
    $ks = Join-Path $keys "release.p12"
    if (-not (Test-Path $ks)) {
        New-Item -ItemType Directory -Force $keys | Out-Null
        Write-Host "Making your release key: choose a password (it is asked for at every release build)."
        Step "keytool" { & "$jbr\bin\keytool.exe" -genkeypair -alias ampi -keyalg RSA -keysize 4096 -validity 12000 -storetype PKCS12 -keystore $ks -dname "CN=Ampi companion" }
    }
    $apk = "$out\ampi-companion.apk"
    Step "signing" { & $java -jar "$tools\lib\apksigner.jar" sign --ks $ks --out $apk "$out\aligned.apk" }
} else {
    $apk = "$out\ampi-companion-debug.apk"
    Step "signing" { & $java -jar "$tools\lib\apksigner.jar" sign --ks "$env:USERPROFILE\.android\debug.keystore" --ks-pass pass:android --out $apk "$out\aligned.apk" }
}
Remove-Item "$out\base.apk", "$out\aligned.apk", "$out\res.zip", "$out\*.idsig" -ErrorAction SilentlyContinue
Write-Host "$apk ($((Get-Item $apk).Length) bytes)"