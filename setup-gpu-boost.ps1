<#
  GboardIME - optional "smart GPU boost" for NVIDIA cards.

  Why: glide typing needs the emulator's frames on time. Under the emulator's light
  load an NVIDIA card can stay parked in its idle state (P8, 210 MHz on an RTX 3090 Ti),
  frames come out late, touch points reach Android in clumps and Gboard misreads the
  glide. Holding the card at full speed all the time works but costs ~80 W extra for
  as long as the emulator runs, even while the keyboard is hidden.

  What this does: creates two scheduled tasks that the (non-admin) GboardIME host
  starts on its own - one raises the GPU's minimum clock while the keyboard is on
  screen, the other resets it when the keyboard is hidden or GboardIME quits:
    \GboardIME\GPU Boost   -> nvidia-smi --lock-gpu-clocks=1500,<max>
    \GboardIME\GPU Release -> nvidia-smi --reset-gpu-clocks
  Measured: 1500 MHz types as smoothly as full speed (3.0% vs 3.2% clumped touch
  points) at about 38 W, and it drops back to ~33 W idle within 2 s of hiding.
  Clock locking needs admin rights, so the tasks run with highest privileges, but they
  only ever run those two fixed commands, and your account may only START them.

  Run it (it asks Windows for admin permission itself):
      powershell -ExecutionPolicy Bypass -File setup-gpu-boost.ps1
  Remove it again:
      powershell -ExecutionPolicy Bypass -File setup-gpu-boost.ps1 -Remove

  NOTE: keep this file ASCII-only so Windows PowerShell 5.1 parses it correctly.
#>
param([switch]$Remove, [switch]$Elevated)

$ErrorActionPreference = "Stop"
$SMI  = "C:\Windows\System32\nvidia-smi.exe"
$LOG  = "$env:LOCALAPPDATA\GboardIME\gpu-boost-setup.log"
New-Item -ItemType Directory -Force (Split-Path $LOG) -ErrorAction SilentlyContinue | Out-Null
function Log($m) { "$(Get-Date -Format 'HH:mm:ss') $m" | Add-Content -Path $LOG -Encoding ascii; Write-Host "  $m" }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    if ($Elevated) { Log "FAILED: still not elevated after the admin prompt"; exit 2 }
    if (-not $Remove -and -not (Test-Path $SMI)) { Write-Host "  No NVIDIA driver (nvidia-smi) found - GPU boost is not needed here."; exit 0 }
    # Relaunch elevated. The script must live on a local drive: elevated processes
    # cannot see per-user mapped or cloud drives.
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Elevated")
    if ($Remove) { $argList += "-Remove" }
    try {
        $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden -ArgumentList $argList
    } catch {
        Write-Host "  Admin permission was declined - GPU boost not set up. You can run this script again any time."
        exit 1
    }
    if (Test-Path $LOG) { Get-Content $LOG -Tail 3 | ForEach-Object { Write-Host "  $_" } }
    exit $p.ExitCode
}

try {
    if ($Remove) {
        foreach ($n in "GPU Boost", "GPU Release") {
            Unregister-ScheduledTask -TaskPath "\GboardIME\" -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        }
        if (Test-Path $SMI) { & $SMI --reset-gpu-clocks | Out-Null }
        Log "GPU boost removed"
        exit 0
    }
    if (-not (Test-Path $SMI)) { Log "no nvidia-smi - nothing to do"; exit 0 }

    # Top of the range: the highest graphics clock this card supports.
    $max = (& $SMI --query-supported-clocks=graphics --format=csv,noheader,nounits |
            ForEach-Object { [int]($_.Trim()) } | Measure-Object -Maximum).Maximum
    if (-not $max -or $max -lt 1500) { Log "unexpected max clock '$max' - aborting"; exit 3 }

    $me  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr  = New-ScheduledTaskPrincipal -UserId $me.Name -LogonType S4U -RunLevel Highest
    $st  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $boost   = New-ScheduledTaskAction -Execute $SMI -Argument "--lock-gpu-clocks=1500,$max"
    $release = New-ScheduledTaskAction -Execute $SMI -Argument "--reset-gpu-clocks"
    Register-ScheduledTask -TaskPath "\GboardIME\" -TaskName "GPU Boost"   -Action $boost   -Principal $pr -Settings $st -Force | Out-Null
    Register-ScheduledTask -TaskPath "\GboardIME\" -TaskName "GPU Release" -Action $release -Principal $pr -Settings $st -Force | Out-Null

    # Let this same user START the tasks without an admin prompt (read + execute only).
    $sid = $me.User.Value
    $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
    $f = $svc.GetFolder("\GboardIME")
    foreach ($n in "GPU Boost", "GPU Release") {
        $f.GetTask($n).SetSecurityDescriptor("D:(A;;FA;;;BA)(A;;FA;;;SY)(A;;GRGX;;;$sid)", 0)
    }
    Log "GPU boost set up for $($me.Name) (1500-$max MHz while the keyboard is showing)"
    exit 0
} catch {
    Log "FAILED: $($_.Exception.Message)"
    exit 4
}
