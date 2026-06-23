# GboardIME Quit
# Cleanly shuts down everything: the Windows host, the relay app, the ADB reverse
# tunnel, and the Android emulator — leaving the machine ready for a fresh start.

$ErrorActionPreference = "Continue"
$SDK = "$env:LOCALAPPDATA\Android\Sdk"
$ADB = "$SDK\platform-tools\adb.exe"

function Log($m){ Write-Host "[GboardIME] $m" -ForegroundColor Yellow }

# 1. Kill the Windows host (console or windowless) ----------------------------
Get-CimInstance Win32_Process -Filter "Name='python.exe'"  | Where-Object { $_.CommandLine -like '*gboard_host*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*gboard_host*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Log "Windows host stopped."

# 2. Find the emulator, remove the reverse tunnel, shut it down ---------------
$serial = $null
foreach($l in (& $ADB devices)){
    if($l -match '^(emulator-\d+)\s+device'){ $serial = $matches[1]; break }
}
if($serial){
    & $ADB -s $serial shell am force-stop com.gboardrelay 2>$null
    & $ADB -s $serial reverse --remove-all 2>$null
    & $ADB -s $serial emu kill 2>$null
    Log "Emulator $serial shut down."
} else {
    Log "No emulator was running."
}

Log "GboardIME fully stopped. Use 'Start GboardIME' for a fresh launch."
