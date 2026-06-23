# GboardIME Launcher
# Starts (or reuses) the Android emulator, installs the relay APK, sets the ADB
# reverse tunnel, launches the relay app, and starts the Windows host (tray).
# Safe to run repeatedly - it kills any old host first.

$ErrorActionPreference = "Continue"
$ROOT        = $PSScriptRoot
$SDK         = "$env:LOCALAPPDATA\Android\Sdk"
$ADB         = "$SDK\platform-tools\adb.exe"
$EMULATOR    = "$SDK\emulator\emulator.exe"
$AVD_NAME    = "GboardIME_Root"
$APK         = "$ROOT\windows\GboardRelay.apk"
$HOSTPY      = "$ROOT\windows\gboard_host.py"
$PYTHONW     = "C:\Python314\pythonw.exe"
$DEVICE_PORT = 9876   # what the relay APK connects to inside the emulator
$HOST_PORT   = 9877   # what the Windows host listens on (9876 is taken by Blender)

function Log($m){ Write-Host "[GboardIME] $m" -ForegroundColor Green }

function Get-EmuSerial {
    foreach($l in (& $ADB devices)){
        if($l -match '^(emulator-\d+)\s+device'){ return $matches[1] }
    }
    return $null
}

# 1. Start the emulator if it isn't already running ----------------------------
$serial = Get-EmuSerial
if(-not $serial){
    Log "Starting emulator $AVD_NAME ..."
    # -no-snapshot-load forces a cold boot so the AVD's current hw.lcd resolution
    # (1080x1180 @ 420dpi) and host-GPU mode are always applied, instead of loading
    # a stale snapshot. -no-snapshot-save keeps it from persisting RAM state.
    # -writable-system is REQUIRED so the system Gboard 12.4 overlay stays in place;
    # without it the keyboard reverts to the stock preload state.
    Start-Process $EMULATOR -ArgumentList @(
        "-avd", $AVD_NAME, "-no-snapshot-load", "-no-snapshot-save",
        "-writable-system", "-no-boot-anim", "-gpu", "host", "-memory", "2048"
    ) -WindowStyle Normal
    Log "Waiting for emulator to boot (up to 3 min)..."
    $elapsed = 0
    do {
        Start-Sleep 5; $elapsed += 5
        $serial = Get-EmuSerial
        $boot = ""
        if($serial){ $boot = ((& $ADB -s $serial shell getprop sys.boot_completed) -join "").Trim() }
        Log "  boot=$boot  (${elapsed}s)"
    } while($boot -ne "1" -and $elapsed -lt 180)
    if($boot -ne "1"){ Log "Emulator did not boot in time - continuing anyway." }
    else { Log "Emulator booted." }
    Start-Sleep 3
} else {
    Log "Emulator already running ($serial)."
}
if(-not $serial){ $serial = Get-EmuSerial }
if(-not $serial){ Log "No emulator serial found - aborting."; exit 1 }

# 2. Install / update the relay APK -------------------------------------------
if(Test-Path $APK){
    Log "Installing relay APK..."
    & $ADB -s $serial install -r $APK | Out-Null
} else {
    Log "APK not found at $APK (run setup.ps1 first)."
}

# 3. ADB reverse: device:9876 -> host:9877 ------------------------------------
& $ADB -s $serial reverse "tcp:$DEVICE_PORT" "tcp:$HOST_PORT" | Out-Null
Log "ADB reverse set: device:$DEVICE_PORT -> host:$HOST_PORT"

# 4. Kill any existing Windows host so we start clean -------------------------
Get-CimInstance Win32_Process -Filter "Name='python.exe'"  | Where-Object { $_.CommandLine -like '*gboard_host*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*gboard_host*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

# 5. Launch the relay app on the emulator -------------------------------------
& $ADB -s $serial shell am force-stop com.gboardrelay
& $ADB -s $serial shell am start -n "com.gboardrelay/.MainActivity" | Out-Null

# 6. Start the Windows host (system tray, no console window) -------------------
if(Test-Path $PYTHONW){
    Start-Process $PYTHONW -ArgumentList "`"$HOSTPY`""
} else {
    Start-Process python -ArgumentList "`"$HOSTPY`"" -WindowStyle Minimized
}
Log "GboardIME is running. Ctrl+Alt+K toggles the keyboard; right-click the tray icon for options."
