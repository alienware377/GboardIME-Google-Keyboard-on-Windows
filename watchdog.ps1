# GboardIME Watchdog
# --------------------------------------------------------------------------
# Keeps the Windows host (gboard_host.py) alive. The host has twice been killed
# collaterally by unrelated `taskkill python` / `pkill python` from OTHER tools
# on this machine, silently stranding the keyboard. This watchdog is PowerShell
# (NOT python), so those python-targeted kills don't touch it, and it relaunches
# the host within a few seconds.
#
# It is gated by a MARKER file so it never fights an intentional Quit:
#   launch.ps1 creates the marker + starts this watchdog.
#   stop.ps1   deletes the marker (this watchdog then exits) + kills the host.
# While the marker exists the host is meant to be running; if it dies, restart it.
#
# ASCII-only (Windows PowerShell 5.1 / CP1252 safe). Do not add non-ASCII chars.

$ErrorActionPreference = "Continue"
$ROOT   = $PSScriptRoot
$SDK    = "$env:LOCALAPPDATA\Android\Sdk"
$ADB    = "$SDK\platform-tools\adb.exe"
$HOSTPY = "$ROOT\windows\gboard_host.py"
$STATE  = "$env:LOCALAPPDATA\GboardIME"
$MARKER = "$STATE\running.flag"
$WLOG   = "$STATE\watchdog.log"
$SERIAL = "emulator-5554"
$DEVICE_PORT = 9876
$HOST_PORT   = 9877
$POLL_SEC    = 5          # how often to check while healthy
$SETTLE_SEC  = 9          # grace after a (re)start before checking again

New-Item -ItemType Directory -Force $STATE -ErrorAction SilentlyContinue | Out-Null

function WLog($m){
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m"
    try { Add-Content -Path $WLOG -Value $line -ErrorAction SilentlyContinue } catch {}
}

function Resolve-Pythonw {
    $c = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Python\*\pythonw.exe","C:\Python*\pythonw.exe")) {
        $f = Get-ChildItem $p -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { return $f.FullName }
    }
    return "C:\Python314\pythonw.exe"
}

function Host-Alive {
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like '*gboard_host*' }
    return [bool]$procs
}

function Emu-Running {
    return [bool](Get-Process -Name 'qemu-system-x86_64' -ErrorAction SilentlyContinue)
}

# Single-instance guard: the watchdog with the LOWEST pid wins. Any watchdog that
# sees another watchdog.ps1 with a smaller pid defers and exits. This is race-free
# (pids are unique) - a naive "is another running?" check lets two simultaneously
# started watchdogs each see the other and BOTH exit, leaving none. We re-check
# every tick too, so even two that slip through startup converge to one.
function Lower-Watchdog {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*watchdog.ps1*' -and $_.ProcessId -lt $PID } |
        Select-Object -First 1
}
$lw = Lower-Watchdog
if ($lw) {
    WLog "watchdog with lower pid ($($lw.ProcessId)) running; exiting (pid $PID)."
    exit 0
}

WLog "watchdog started (pid $PID)."

while ($true) {
    Start-Sleep -Seconds $POLL_SEC

    # Converge to a single instance if two slipped past the startup check.
    $lw = Lower-Watchdog
    if ($lw) {
        WLog "watchdog with lower pid ($($lw.ProcessId)) present; exiting (pid $PID)."
        break
    }

    # Clean shutdown signal: the marker is gone (stop.ps1 ran) -> exit quietly.
    if (-not (Test-Path $MARKER)) {
        WLog "marker gone; watchdog exiting."
        break
    }

    if (Host-Alive) { continue }   # happy path: cheap, no adb

    # Host is down. Only meaningful to relaunch it if the emulator is up (there is
    # something to relay to, and the reverse tunnel targets it). If the emulator is
    # gone too, leave a full relaunch to the user / launch.ps1 - don't cold-boot.
    if (-not (Emu-Running)) {
        WLog "host down but emulator not running; waiting (use Start GboardIME to relaunch)."
        continue
    }

    WLog "host not running - restarting it."
    # Re-assert the reverse tunnel (harmless if already set) and the relay app,
    # then start the host exactly as launch.ps1 does.
    if (Test-Path $ADB) {
        & $ADB -s $SERIAL reverse "tcp:$DEVICE_PORT" "tcp:$HOST_PORT" 2>$null | Out-Null
        & $ADB -s $SERIAL shell am start -n "com.gboardrelay/.MainActivity" 2>$null | Out-Null
    }
    $pyw = Resolve-Pythonw
    if (Test-Path $pyw) {
        Start-Process $pyw -ArgumentList "`"$HOSTPY`""
    } else {
        Start-Process python -ArgumentList "`"$HOSTPY`"" -WindowStyle Minimized
    }
    WLog "host restarted."
    Start-Sleep -Seconds $SETTLE_SEC   # let it bind the socket before re-checking
}
