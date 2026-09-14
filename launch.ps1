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
# Resolve pythonw.exe portably: PATH first, then common install dirs, then a fallback.
$PYTHONW = $null
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) { $PYTHONW = $cmd.Source }
if (-not $PYTHONW) {
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\*\pythonw.exe","C:\Python*\pythonw.exe")) {
        $f = Get-ChildItem $p -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { $PYTHONW = $f.FullName; break }
    }
}
if (-not $PYTHONW) { $PYTHONW = "C:\Python314\pythonw.exe" }   # last-resort default
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
    # -gpu angle_indirect (ANGLE/D3D11) NOT 'host': measured on an RTX 3090 Ti, the
    # native GL translator stalled on 'issue draw commands' for 351 of 366 frames
    # (95.9% jank, 48ms median), starving Gboard's glide sampler so swipes produced
    # wrong words. ANGLE: 17ms median, 0 missed vsync, that stall gone entirely.
    # -writable-system is REQUIRED so the system Gboard 12.4 overlay stays in place;
    # without it the keyboard reverts to the stock preload state.
    #
    # Launch via WScript.Shell.Run with window style 0 (hidden) so the emulator's
    # noisy console/log window never appears. The Android display (a separate Qt
    # window) still shows normally; only the text-log console is suppressed.
    # -no-audio silences the emulator completely. It is only ever a keyboard here, and
    # the guest was making notification sounds on boot.
    # -prop qemu.hw.mainkeys=1 tells the framework this device has hardware nav keys, so
    # SystemUI never draws the software navigation bar. That reclaims 63px at 420dpi (the
    # app area was 1080x1257 of a 1080x1320 screen). It is dead space in kiosk mode: lock
    # task already blocks home/recents, so the bar only cost height.
    $emuArgs = "-avd `"$AVD_NAME`" -no-snapshot-load -no-snapshot-save " +
               "-writable-system -no-boot-anim -no-metrics -no-audio -gpu angle_indirect -memory 2048 " +
               "-prop qemu.hw.mainkeys=1"
    # QT_QPA_PLATFORM=windows:nowmpointer makes Qt handle pen/touch through the legacy
    # WM_MOUSE path instead of WM_POINTER. This is the glide-typing fix for tablet pens.
    # Qt 6.5's pointer path replays the pen's coalesced history in a tight loop and stamps
    # every replayed point with the CURRENT time, so a stroke arrives as same-instant
    # clumps separated by 100-350ms holes (measured: 66% of gaps under 0.1ms, 78% of
    # stroke time stalled). Gboard derives velocity from point timing, so it read garbage
    # and picked wrong words. Mouse was never affected because Qt already sends
    # QT_PT_MOUSE down the legacy path - which is exactly the asymmetry we measured.
    # WScript.Shell.Run inherits this process's environment, so setting it here is enough.
    # NOTE: do NOT add 'nomousefromtouch' - it makes Qt drop synthesized pen events.
    # NOTE: this option was REMOVED in Qt 6.8; if the SDK ships a newer emulator this
    # silently stops working (the warning is suppressed by QT_LOGGING_RULES), so re-check
    # pen behaviour after any SDK update.
    $env:QT_QPA_PLATFORM = "windows:nowmpointer"
    $wshRun = New-Object -ComObject WScript.Shell
    $wshRun.Run("`"$EMULATOR`" $emuArgs", 0, $false) | Out-Null
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

# 3b. Force THREE-BUTTON navigation so the swipe-up-from-bottom "home" gesture does
# not exist. Hiding the navigation bar (-prop qemu.hw.mainkeys=1) does NOT disable
# gesture navigation - it only stops the bar being drawn, which moved the keyboard
# down into the home-gesture strip at the very bottom edge. Swiping up there left
# the kiosk and showed the (empty) launcher.
# Lock task does not save us: the quickstep gesture reaches the launcher before the
# activity manager blocks it, and an app cannot opt out - Android caps gesture
# exclusion regions and refuses them outright for the home gesture.
# Three-button mode has no such gesture, and because mainkeys=1 still suppresses the
# bar itself, we get neither a navigation bar nor navigation gestures.
# Disable 'gestural' EXPLICITLY: enabling threebutton alone leaves both overlays
# marked enabled, and which one wins after a reboot is not guaranteed.
& $ADB -s $serial shell "cmd overlay enable com.android.internal.systemui.navbar.threebutton" 2>$null | Out-Null
& $ADB -s $serial shell "cmd overlay disable com.android.internal.systemui.navbar.gestural" 2>$null | Out-Null
$navMode = ((& $ADB -s $serial shell settings get secure navigation_mode) -join "").Trim()
if ($navMode -eq "0") { Log "Navigation gestures disabled (three-button mode, bar still hidden)." }
else { Log "WARNING: navigation_mode is '$navMode', expected 0 - the swipe-up home gesture may still be active." }

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

# 7. Watchdog: mark "should be running" and start the PowerShell watchdog that
#    relaunches the host if it gets killed (e.g. collateral python kills). The
#    marker gates it so an intentional Quit (stop.ps1 removes the marker) is not
#    fought. stop.ps1 also kills the watchdog.
$state = "$env:LOCALAPPDATA\GboardIME"
New-Item -ItemType Directory -Force $state | Out-Null
Set-Content -Path "$state\running.flag" -Value "1" -Encoding ASCII
$already = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -like '*watchdog.ps1*' }
if (-not $already) {
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        "-NoProfile","-ExecutionPolicy","Bypass","-File","`"$ROOT\watchdog.ps1`"")
    Log "Watchdog started (auto-restarts the host if it dies)."
}
Log "GboardIME is running. Ctrl+Alt+K toggles the keyboard; right-click the tray icon for options."
