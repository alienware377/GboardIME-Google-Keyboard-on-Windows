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
# in 5.1 and slow startup). On timeout we hard-kill the process. Returns $true if
# the exe completed in time, $false if it was killed.
function Invoke-WithTimeout($exe, $exeArgs, $timeoutMs) {
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        foreach ($a in $exeArgs) { [void]$psi.ArgumentList.Add($a) } 2>$null
        if (-not $psi.ArgumentList -or $psi.ArgumentList.Count -eq 0) {
            # ArgumentList not supported on PS 5.1 - fall back to Arguments string
            $psi.Arguments = ($exeArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '
        }
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError  = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        if (-not $p.WaitForExit($timeoutMs)) {
            try { $p.Kill() } catch {}
            return $false
        }
        return $true
    } catch {
        return $true   # don't block shutdown if we couldn't even launch adb
    }
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
    $ok = Invoke-WithTimeout $ADB @('-s','emulator-5554','shell','am','force-stop','com.gboardrelay') 4000
    Log "force-stop relay: $(if($ok){'ok'}else{'TIMEOUT'})"
    $ok = Invoke-WithTimeout $ADB @('-s','emulator-5554','reverse','--remove-all') 3000
    Log "reverse --remove-all: $(if($ok){'ok'}else{'TIMEOUT'})"
} else {
    Log "ADB not found at $ADB - skipping adb cleanup."
}

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
