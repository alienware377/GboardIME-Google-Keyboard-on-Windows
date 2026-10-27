# GboardIME Quit
# Cleanly shuts down everything: the Windows host, the relay app, the ADB reverse
# tunnel, and the Android emulator - leaving the machine ready for a fresh start.
#
# IMPORTANT: every external command is wrapped in a job + timeout so the Start-menu
# "Quit GboardIME" shortcut can NEVER hang. Older versions called 'adb emu kill'
# (a telnet console command) directly, which would block forever if the emulator's
# telnet port was unresponsive - and since the script runs with WindowStyle Hidden,
# the user couldn't see it was stuck. Now we kill the emulator process by PID as
# the reliable path, with adb commands as best-effort with strict timeouts.

$ErrorActionPreference = "Continue"
$SDK = "$env:LOCALAPPDATA\Android\Sdk"
$ADB = "$SDK\platform-tools\adb.exe"
$LOG = "$env:LOCALAPPDATA\GboardIME\stop.log"
New-Item -ItemType Directory -Force (Split-Path $LOG) -ErrorAction SilentlyContinue | Out-Null

function Log($m){
    $line = "$(Get-Date -Format 'HH:mm:ss') $m"
    Write-Host "[GboardIME] $m" -ForegroundColor Yellow
    Add-Content -Path $LOG -Value $line -ErrorAction SilentlyContinue
}

# Run a native exe with a hard timeout. Uses System.Diagnostics.Process so we get a
# real WaitForExit(ms) - PowerShell jobs are unreliable for this (nested-job issues
# in 5.1 and slow startup). On timeout we hard-kill the process.
# Returns the exit code, -1 if it timed out (and was killed), -2 if it could not start.
#
# Do NOT use ProcessStartInfo.ArgumentList: it does not exist on Windows PowerShell 5.1,
# which is what the Start-menu shortcut runs. An earlier version called it first, so it
# threw on every call, and the catch below returned "success" - every command in this
# script was silently skipped for months while the log said "ok" (including the guest
# filesystem sync that protects Gboard's learned words). All our arguments are plain
# tokens without quotes, so a quoted Arguments string is safe.
function Invoke-WithTimeout($exe, $exeArgs, $timeoutMs) {
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        $psi.Arguments = ($exeArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError  = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit($timeoutMs)) {
            try { $p.Kill() } catch {}
            return -1
        }
        return $p.ExitCode
    } catch {
        Log "could not run ${exe}: $($_.Exception.Message)"
        return -2   # still never blocks shutdown; just reported honestly
    }
}

function Describe($rc) {
    switch ($rc) { 0 { 'ok' } -1 { 'TIMEOUT' } -2 { 'FAILED to start' } default { "exit code $rc" } }
}

Log "stop.ps1 starting (pid $PID)"

# 1. Kill any STUCK older instances of stop.ps1 from previous clicks. We require
#    age > 5s so we don't accidentally kill our own parent process (which may have
#    'stop.ps1' in its cmdline if it spawned us via an inline invocation). Stuck
#    instances are by definition much older than 5s.
$cutoff = (Get-Date).AddSeconds(-5)
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -like '*stop.ps1*' -and
        $_.CreationDate -lt $cutoff
    } |
    ForEach-Object {
        Log "killing previous stuck stop.ps1 (pid $($_.ProcessId))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

# 1b. Signal the watchdog to stand down BEFORE killing the host, else it would
#     just relaunch it. Remove the marker (watchdog exits on its next tick) and
#     kill the watchdog process now so it can't race us.
$marker = "$env:LOCALAPPDATA\GboardIME\running.flag"
Remove-Item $marker -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '-File\s+"?[^"]*watchdog\.ps1' } |
    ForEach-Object {
        Log "stopping watchdog (pid $($_.ProcessId))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

# 2. Kill the Windows host (console or windowless) ----------------------------
$killed = 0
foreach ($name in 'python.exe','pythonw.exe') {
    Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*gboard_host*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $killed++
        }
}
Log "Windows host stopped ($killed proc)."

# 3. Best-effort: tell the relay app to stop and remove the reverse tunnel ----
#    Wrapped with timeouts so a stuck adb can't hang us.
if (Test-Path $ADB) {
    $rc = Invoke-WithTimeout $ADB @('-s','emulator-5554','shell','am','force-stop','com.gboardrelay') 4000
    Log "force-stop relay: $(Describe $rc)"
    $rc = Invoke-WithTimeout $ADB @('-s','emulator-5554','reverse','--remove-all') 3000
    Log "reverse --remove-all: $(Describe $rc)"

    # Flush the GUEST page cache before the VM is killed, or Gboard's learned words
    # are lost. Gboard writes its user-history dictionary rarely and we kill
    # qemu-system-x86_64 outright (step 4), so anything still sitting in the guest
    # kernel's page cache never reaches userdata-qemu.img - the file looks written
    # inside Android but the block never lands in the disk image. That reads to the
    # user as "my learned words keep resetting".
    # Timeout-wrapped like everything else here: Quit must never hang.
    $rc = Invoke-WithTimeout $ADB @('-s','emulator-5554','shell','sync') 8000
    Log "sync guest filesystem: $(Describe $rc)"
} else {
    Log "ADB not found at $ADB - skipping adb cleanup."
}

# 3b. Release the GPU clock floor. The host locks the GPU's minimum clock while the
#     keyboard is showing (optional NVIDIA boost tasks), and it was just killed above,
#     so it cannot release it itself. schtasks /run returns immediately; the task only
#     exists if the user opted in, otherwise this silently does nothing.
$rc = Invoke-WithTimeout "schtasks.exe" @('/run','/tn','\GboardIME\GPU Release') 4000
Log "GPU clock release: $(if ($rc -eq 1) { 'not installed (skipped)' } else { Describe $rc })"

# 4. Kill the emulator BY PID - reliable, never hangs. The old 'adb emu kill'
#    sent a telnet console command which would block forever if the emulator was
#    unresponsive. Stop-Process is immediate. We kill both the emulator launcher
#    and the qemu-system-x86_64 VM process it spawned.
$emKilled = 0
foreach ($pname in 'qemu-system-x86_64','emulator','crashpad_handler') {
    Get-Process -Name $pname -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $emKilled++
    }
}
Log "Emulator processes stopped ($emKilled killed)."

# 5. Clean up dangling adb daemons (multiple can pile up across sessions) -----
#    Leave the most recent one alive (it's the user's active server) - kill orphans.
$adbProcs = Get-Process -Name 'adb' -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
if ($adbProcs.Count -gt 1) {
    $orphans = $adbProcs | Select-Object -Skip 1
    foreach ($p in $orphans) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    Log "Killed $($orphans.Count) stale adb daemon(s)."
}

Log "GboardIME fully stopped. Use 'Start GboardIME' for a fresh launch."
