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
$POLL_SEC    = 5          # how often to check the host process while healthy
$SETTLE_SEC  = 9          # grace after a (re)start before checking again
$LINK_EVERY  = 3          # check the relay link every Nth tick (~15s)
$LINK_FAILS  = 2          # consecutive link failures before repairing (~30s down)
$TUNNEL_EVERY = 12        # verify the reverse-tunnel ENTRY every Nth tick (~60s)
$ADB_TIMEOUT = 4000       # ms; never let a wedged adb freeze the watchdog

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

# Run adb with a hard timeout. A wedged adb (seen when the emulator is mid-boot or
# an adb server restart is in flight) must never hang the watchdog, so we use a real
# Process + WaitForExit(ms) and kill it if it overruns.
function Run-Adb([string]$arguments, [int]$timeoutMs) {
    if (-not (Test-Path $ADB)) { return $false }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $ADB
        $psi.Arguments = $arguments
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit($timeoutMs)) { try { $p.Kill() } catch {}; return $false }
        return $true
    } catch { return $false }
}

# Does the ADB reverse mapping still exist? An existing socket can keep working
# after the mapping is removed, so a live connection does NOT prove the tunnel is
# there - it only breaks later, when the relay next tries to reconnect (silent,
# delayed failure). Verifying the entry itself lets us restore it before that bites.
function Tunnel-Present {
    if (-not (Test-Path $ADB)) { return $true }   # can't tell; don't thrash
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $ADB
        $psi.Arguments = "-s $SERIAL reverse --list"
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit($ADB_TIMEOUT)) { try { $p.Kill() } catch {}; return $true }
        $out = $p.StandardOutput.ReadToEnd()
        return ($out -match "tcp:$DEVICE_PORT")
    } catch { return $true }
}

# Is the relay app actually CONNECTED to the host? The host process being alive is
# not enough: the ADB reverse tunnel can silently disappear (adb server restart,
# emulator hiccup, another tool calling 'adb reverse --remove-all'), after which the
# relay retries forever and the keyboard is dead with every process looking healthy.
function Relay-Connected {
    try {
        $c = Get-NetTCPConnection -LocalPort $HOST_PORT -State Established -ErrorAction SilentlyContinue |
             Where-Object { $_.LocalAddress -eq '127.0.0.1' }
        return [bool]$c
    } catch {
        # Fall back to netstat parsing if the cmdlet is unavailable.
        $out = netstat -ano 2>$null | Select-String ":$HOST_PORT\s" | Select-String 'ESTABLISHED'
        return [bool]$out
    }
}

# Single-instance guard: a named MUTEX, not command-line matching. Matching on
# "a powershell whose command line contains watchdog.ps1" is wrong - it also matches
# any shell that merely MENTIONS the script (an installer, a diagnostic one-liner,
# an editor task), so the real watchdog would defer to a phantom and exit, leaving
# nothing running. A mutex is race-free and matches only actual instances; Windows
# releases it automatically when the holder exits, even if it is killed.
$script:Mutex = New-Object System.Threading.Mutex($false, "Local\GboardIME_Watchdog")
$haveLock = $false
try { $haveLock = $script:Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $haveLock = $true }
if (-not $haveLock) {
    WLog "another watchdog already holds the lock; exiting (pid $PID)."
    exit 0
}

WLog "watchdog started (pid $PID)."
$tick = 0
$linkFails = 0

while ($true) {
    Start-Sleep -Seconds $POLL_SEC
    $tick++


    # Clean shutdown signal: the marker is gone (stop.ps1 ran) -> exit quietly.
    if (-not (Test-Path $MARKER)) {
        WLog "marker gone; watchdog exiting."
        break
    }

    if (Host-Alive) {
        # Host is up, but "up" is not the same as "working": verify the relay is
        # actually connected through the ADB reverse tunnel. Checked every Nth tick
        # (cheap, no adb) and only repaired after several consecutive failures, so a
        # momentary reconnect during normal operation is never fought.
        if ($tick % $LINK_EVERY -eq 0) {
            if (Relay-Connected) {
                if ($linkFails -gt 0) { WLog "relay link recovered."; $linkFails = 0 }
            } elseif (Emu-Running) {
                $linkFails++
                if ($linkFails -ge $LINK_FAILS) {
                    WLog "relay link down ($linkFails checks) - re-applying reverse tunnel."
                    $ok = Run-Adb "-s $SERIAL reverse tcp:$DEVICE_PORT tcp:$HOST_PORT" $ADB_TIMEOUT
                    if (-not $ok) { WLog "  adb reverse timed out/failed." }
                    Start-Sleep -Seconds 2
                    if (-not (Relay-Connected)) {
                        # Tunnel restored but the relay never re-dialled - nudge the app.
                        WLog "  still down; restarting the relay app."
                        Run-Adb "-s $SERIAL shell am start -n com.gboardrelay/.MainActivity" $ADB_TIMEOUT | Out-Null
                        Start-Sleep -Seconds 3
                    }
                    if (Relay-Connected) { WLog "  relay link repaired." }
                    else { WLog "  repair did not take; will retry." }
                    $linkFails = 0
                }
            } else {
                $linkFails = 0   # emulator is gone; nothing to link to
            }
        }
        # Even while the link looks healthy, make sure the reverse mapping itself is
        # still registered - re-applying is idempotent and costs one adb call a minute.
        if (($tick % $TUNNEL_EVERY -eq 0) -and (Emu-Running)) {
            if (-not (Tunnel-Present)) {
                WLog "reverse tunnel entry missing - restoring it."
                Run-Adb "-s $SERIAL reverse tcp:$DEVICE_PORT tcp:$HOST_PORT" $ADB_TIMEOUT | Out-Null
            }
        }
        continue
    }

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
    $linkFails = 0
    Start-Sleep -Seconds $SETTLE_SEC   # let it bind the socket before re-checking
}
