<#
  GboardIME - Installer
  ---------------------
  One-shot setup for a fresh Windows machine. Idempotent: safe to re-run.

  What it does:
    1. Locates (or downloads) the Android SDK cmdline-tools, platform-tools, emulator.
    2. Installs the google_apis x86_64 API-34 system image (NOT playstore - the
       non-playstore image can be rooted, which kiosk mode + provisioning needs).
    3. Creates the "GboardIME_Root" AVD and patches it to 1080x1180 @ 420dpi, host GPU.
    4. Installs the Python host dependencies.
    5. Installs the relay APK (prebuilt; builds from source only if the APK is missing).
    6. Cold-boots the emulator (-writable-system -gpu host), sets Gboard as the default
       keyboard, sets the ADB reverse tunnel.
    7. Provisions kiosk / Lock Task mode (Device Owner) so the keyboard can't be swiped away.
    8. (Optional) debloats the emulator using windows/debloat_removed_packages.txt.
    9. Creates "Start GboardIME" / "Quit GboardIME" Start-menu shortcuts.

  Usage:
    Double-click Install.cmd, or:
      powershell -ExecutionPolicy Bypass -File install.ps1
    Switches:
      -SkipDebloat     leave all stock apps installed
      -SkipKiosk       don't lock the keyboard to the foreground
      -SkipEmulator    only build/install host bits, don't boot or provision

  NOTE: this file is intentionally ASCII-only so Windows PowerShell 5.1 parses it
  correctly regardless of file encoding. Do not add box-drawing or smart-quote chars.
#>
param(
    [switch]$SkipDebloat,
    [switch]$SkipKiosk,
    [switch]$SkipEmulator
)

$ErrorActionPreference = "Stop"
$ROOT      = $PSScriptRoot
$SDK       = "$env:LOCALAPPDATA\Android\Sdk"
$AVD_NAME  = "GboardIME_Root"
$SYSIMAGE  = "system-images;android-34;google_apis;x86_64"
$IMG_PATH  = "$SDK\system-images\android-34\google_apis\x86_64"
$DEVICE    = "pixel_4"
$APK       = "$ROOT\windows\GboardRelay.apk"
$HOSTPY    = "$ROOT\windows\gboard_host.py"
$GBOARD_IME = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
$ADMIN_COMP = "com.gboardrelay/.RelayAdminReceiver"
$DEVICE_PORT = 9876
$HOST_PORT   = 9877

function Info($m){ Write-Host "  $m" -ForegroundColor Gray }
function Step($m){ Write-Host "`n== $m ==" -ForegroundColor Cyan }
function Ok($m){ Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "  [!] $m" -ForegroundColor Yellow }
function Die($m){ Write-Host "`n[FATAL] $m" -ForegroundColor Red; exit 1 }

Write-Host "GboardIME installer" -ForegroundColor Magenta
Write-Host "===================" -ForegroundColor Magenta

# Resolve tools
$ADB      = "$SDK\platform-tools\adb.exe"
$EMULATOR = "$SDK\emulator\emulator.exe"

# Java (needed for sdkmanager/avdmanager and the optional Gradle build)
$JAVA_HOME = $env:JAVA_HOME
if (-not $JAVA_HOME -or -not (Test-Path "$JAVA_HOME\bin\java.exe")) {
    foreach ($cand in @(
        "C:\Program Files\Android\Android Studio\jbr",
        "$env:LOCALAPPDATA\Programs\Android Studio\jbr",
        "C:\Program Files\Eclipse Adoptium",
        "C:\Program Files\Java"
    )) {
        $found = Get-ChildItem -Path $cand -Filter "java.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { $JAVA_HOME = Split-Path (Split-Path $found.FullName); break }
    }
}
if ($JAVA_HOME -and (Test-Path "$JAVA_HOME\bin\java.exe")) {
    $env:JAVA_HOME = $JAVA_HOME
    Info "Java: $JAVA_HOME"
} else {
    Warn "No Java found. Install Android Studio (it bundles one) if SDK steps fail."
}
$env:ANDROID_HOME = $SDK

# Python
$PY = $null
foreach ($c in @("py","python","python3")) {
    $g = Get-Command $c -ErrorAction SilentlyContinue
    if ($g) { $PY = $g.Source; break }
}
if (-not $PY) {
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\*\python.exe","C:\Python*\python.exe")) {
        $f = Get-ChildItem $p -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { $PY = $f.FullName; break }
    }
}
if ($PY) { Info "Python: $PY" } else { Warn "No Python found - install Python 3 from python.org, then re-run." }

# 1. cmdline-tools
Step "Android SDK command-line tools"
$sdkManager = "$SDK\cmdline-tools\latest\bin\sdkmanager.bat"
if (-not (Test-Path $sdkManager)) {
    Info "Downloading cmdline-tools (~135 MB)..."
    $zip = "$env:TEMP\cmdline-tools.zip"
    Invoke-WebRequest "https://dl.google.com/android/repository/commandlinetools-win-14742923_latest.zip" -OutFile $zip -UseBasicParsing
    $tmp = "$env:TEMP\cmdline-tools-extract"
    Expand-Archive $zip $tmp -Force
    $dest = "$SDK\cmdline-tools\latest"
    New-Item -ItemType Directory -Force $dest | Out-Null
    $src = "$tmp\cmdline-tools"
    if (Test-Path $src) { Copy-Item "$src\*" $dest -Recurse -Force }
    else { Copy-Item "$tmp\*" $dest -Recurse -Force }
    Remove-Item $zip,$tmp -Recurse -Force -ErrorAction SilentlyContinue
    Ok "cmdline-tools installed"
} else { Ok "cmdline-tools present" }

# 2. platform-tools + emulator + licenses + system image
Step "SDK packages"
"y`ny`ny`ny`ny`ny`n" | & $sdkManager --licenses 2>&1 | Out-Null
if (-not (Test-Path $ADB))      { Info "Installing platform-tools..."; & $sdkManager "platform-tools" --sdk_root=$SDK 2>&1 | Out-Null }
if (-not (Test-Path $EMULATOR)) { Info "Installing emulator...";       & $sdkManager "emulator"       --sdk_root=$SDK 2>&1 | Out-Null }
if (-not (Test-Path $IMG_PATH)) {
    Info "Downloading system image (~1.5 GB, google_apis API 34)... this can take several minutes"
    & $sdkManager $SYSIMAGE --sdk_root=$SDK 2>&1 | Out-Null
    Ok "System image installed"
} else { Ok "System image present" }
if (-not (Test-Path $ADB)) { Die "platform-tools/adb still missing - SDK install failed." }

# 3. Create + patch the AVD
Step "AVD: $AVD_NAME"
$avdManager = "$SDK\cmdline-tools\latest\bin\avdmanager.bat"
$existing = & $avdManager list avd 2>&1
if ($existing -notmatch [regex]::Escape($AVD_NAME)) {
    "no" | & $avdManager create avd --name $AVD_NAME --package $SYSIMAGE --device $DEVICE --force 2>&1 | Out-Null
    Ok "AVD created"
} else { Ok "AVD already exists" }

# Patch config.ini for resolution / GPU / input
$cfg = "$env:USERPROFILE\.android\avd\$AVD_NAME.avd\config.ini"
if (Test-Path $cfg) {
    $desired = [ordered]@{
        "hw.lcd.width"            = "1080"
        "hw.lcd.height"           = "1180"
        "hw.lcd.density"          = "420"
        "hw.gpu.enabled"          = "yes"
        "hw.gpu.mode"             = "host"
        "hw.keyboard"             = "no"
        "hw.ramSize"              = "2048"
        "disk.dataPartition.size" = "6G"
        "PlayStore.enabled"       = "no"
    }
    $lines = Get-Content $cfg
    foreach ($k in $desired.Keys) {
        $v = $desired[$k]
        if ($lines -match "^$([regex]::Escape($k))=") {
            $lines = $lines -replace "^$([regex]::Escape($k))=.*", "$k=$v"
        } else {
            $lines += "$k=$v"
        }
    }
    $lines | Set-Content $cfg -Encoding ASCII
    Ok "AVD config patched (1080x1180 @ 420dpi, host GPU)"
} else { Warn "config.ini not found at $cfg" }

# 4. Python host dependencies
Step "Python host dependencies"
if ($PY) {
    & $PY -m pip install --user --quiet pystray pillow uiautomation 2>&1 | Out-Null
    Ok "pystray, pillow, uiautomation installed"
} else { Warn "Skipped (no Python)" }

# 5. Relay APK (prefer prebuilt; build only if missing)
Step "Relay APK"
if (-not (Test-Path $APK)) {
    Info "Prebuilt APK missing - building from source (needs Java)..."
    if (-not $env:JAVA_HOME) { Die "Cannot build APK without Java. Install Android Studio." }
    Push-Location "$ROOT\android\GboardRelay"
    & .\gradlew.bat assembleDebug 2>&1 | Out-Null
    Pop-Location
    $built = "$ROOT\android\GboardRelay\app\build\outputs\apk\debug\app-debug.apk"
    if (Test-Path $built) { Copy-Item $built $APK -Force; Ok "APK built" }
    else { Die "APK build failed." }
} else { Ok "Prebuilt APK present" }

# 6-8. Emulator: boot, configure, provision
if ($SkipEmulator) {
    Warn "Skipping emulator boot/provisioning (-SkipEmulator)"
} else {
    Step "Booting emulator (cold, host GPU, writable-system)"
    function Get-Serial { foreach($l in (& $ADB devices)){ if($l -match '^(emulator-\d+)\s+device'){ return $matches[1] } } ; return $null }
    $serial = Get-Serial
    if (-not $serial) {
        # Hidden launch (window style 0) so the emulator's noisy log console never
        # shows; the Android display (separate Qt window) still appears.
        $emuArgs = "-avd `"$AVD_NAME`" -no-snapshot-load -no-snapshot-save " +
                   "-writable-system -no-boot-anim -no-metrics -gpu host -memory 2048"
        $wshRun = New-Object -ComObject WScript.Shell
        $wshRun.Run("`"$EMULATOR`" $emuArgs", 0, $false) | Out-Null
        Info "Waiting for boot (up to 4 min)..."
        $t=0
        do {
            Start-Sleep 5; $t+=5; $serial = Get-Serial
            $boot = ""
            if ($serial) { $boot = ((& $ADB -s $serial shell getprop sys.boot_completed 2>$null) -join "").Trim() }
        } while ($boot -ne "1" -and $t -lt 240)
        if ($boot -ne "1") { Die "Emulator did not finish booting." }
        Ok "Emulator booted ($serial)"
    } else { Ok "Emulator already running ($serial)" }

    Step "Configure Gboard + relay"
    & $ADB -s $serial reverse "tcp:$DEVICE_PORT" "tcp:$HOST_PORT" | Out-Null
    Ok "Reverse tunnel device:$DEVICE_PORT -> host:$HOST_PORT"

    & $ADB -s $serial install -r $APK 2>&1 | Out-Null
    Ok "Relay app installed"

    $imes = (& $ADB -s $serial shell ime list -a -s 2>$null) -join "`n"
    if ($imes -match "com\.google\.android\.inputmethod\.latin") {
        & $ADB -s $serial shell ime enable $GBOARD_IME 2>&1 | Out-Null
        & $ADB -s $serial shell ime set $GBOARD_IME 2>&1 | Out-Null
        Ok "Gboard set as default keyboard"
    } else {
        Warn "Gboard not found on this image. Install it in the emulator, then set it as default."
    }

    if (-not $SkipDebloat) {
        Step "Debloat (reduce background load)"
        $listFile = "$ROOT\windows\debloat_removed_packages.txt"
        if (Test-Path $listFile) {
            $pkgs = Get-Content $listFile | Where-Object { $_ -and ($_ -notmatch '^\s*#') } | ForEach-Object { $_.Trim() } | Where-Object { $_ }
            $n = 0
            foreach ($p in $pkgs) {
                $r = (& $ADB -s $serial shell pm uninstall --user 0 $p 2>&1) -join ""
                if ($r -match "Success") { $n++ }
            }
            Ok "$n packages removed (reversible with: adb shell pm install-existing <pkg>)"
        } else { Warn "debloat list not found; skipping" }
    } else { Warn "Skipping debloat (-SkipDebloat)" }

    if (-not $SkipKiosk) {
        Step "Kiosk / Lock Task mode"
        $r = (& $ADB -s $serial shell dpm set-device-owner $ADMIN_COMP 2>&1) -join "`n"
        if ($r -match "Success") {
            Ok "Device Owner set - keyboard will lock to foreground (no swipe-away)"
        } elseif ($r -match "already") {
            Ok "Device Owner already set"
        } else {
            Warn "Could not set Device Owner (an account may exist on the AVD). Kiosk disabled. Detail: $r"
        }
        & $ADB -s $serial shell am force-stop com.gboardrelay 2>&1 | Out-Null
        & $ADB -s $serial shell am start -n com.gboardrelay/.MainActivity 2>&1 | Out-Null
    } else { Warn "Skipping kiosk (-SkipKiosk)" }
}

# 9. Start-menu shortcuts
Step "Start-menu shortcuts"
$startDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\GboardIME"
New-Item -ItemType Directory -Force $startDir | Out-Null
$wsh = New-Object -ComObject WScript.Shell
function Make-Shortcut($name,$script){
    $lnk = $wsh.CreateShortcut((Join-Path $startDir $name))
    $lnk.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $lnk.Arguments  = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ROOT\$script`""
    $lnk.WorkingDirectory = $ROOT
    $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $lnk.Save()
}
Make-Shortcut "Start GboardIME.lnk" "launch.ps1"
Make-Shortcut "Quit GboardIME.lnk"  "stop.ps1"
Ok "Shortcuts created in Start menu (folder: GboardIME)"

# Done
Write-Host "`nInstall complete!" -ForegroundColor Green
Write-Host @"

Next:
  - Use 'Start GboardIME' to launch, 'Quit GboardIME' to stop.
  - Click a Windows text field, then type/swipe on Gboard in the emulator.
  - To undo kiosk later: factory-reset the AVD, or have the app clear Device Owner.
"@ -ForegroundColor Green
