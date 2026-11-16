# GboardIME - Python finder
# Dot-sourced by install.ps1, launch.ps1 and watchdog.ps1.
#
# Finding python.exe on PATH is not enough. Windows ships "App execution alias"
# stubs in %LOCALAPPDATA%\Microsoft\WindowsApps (python.exe, python3.exe) that
# only point at the Microsoft Store. They show up in Get-Command like a real
# Python, but running one prints "Python was not found" and exits 9009. So every
# candidate here is actually started and must print its own sys.executable.
#
# ASCII-only (Windows PowerShell 5.1 / CP1252 safe). Do not add non-ASCII chars.

$script:GbPythonPathFile = Join-Path $PSScriptRoot "python-path.txt"

# Runs <exe> [preArgs] -c "...", returns the interpreter's real sys.executable,
# or $null if it did not start, timed out, failed or is not Python 3.
function Test-GbPython([string]$exe, [string]$preArgs = "", [int]$timeoutMs = 10000) {
    if (-not $exe) { return $null }
    $code = "import sys; print(sys.executable if sys.version_info[0] == 3 else '')"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = ("$preArgs -c `"$code`"").Trim()
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $p = $null
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        $out = $p.StandardOutput.ReadToEndAsync()
        $err = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($timeoutMs)) {
            try { $p.Kill() } catch {}
            return $null
        }
        $p.WaitForExit()
        if ($p.ExitCode -ne 0) { return $null }
        $line = ($out.Result -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if (-not $line) { return $null }
        $line = $line.Trim()
        if (-not (Test-Path -LiteralPath $line -PathType Leaf)) { return $null }
        return $line
    } catch {
        return $null
    } finally {
        if ($p) { $p.Dispose() }
    }
}

# pythonw.exe that sits next to a working python.exe, or $null.
function Get-GbPythonwBeside([string]$python) {
    if (-not $python) { return $null }
    $w = Join-Path (Split-Path $python) "pythonw.exe"
    if (Test-Path -LiteralPath $w -PathType Leaf) { return $w }
    return $null
}

# Searches for a Python 3 that really runs. Returns @{ Python; Pythonw } or $null.
# Order: the py launcher, every python/python3 on PATH (not just the first, so a
# stub early on PATH does not hide a real install later on it), then the usual
# install folders.
function Find-GbPython {
    $tried = @{}
    $cands = New-Object System.Collections.ArrayList

    $py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) { [void]$cands.Add(@($py.Source, "-3")) }
    foreach ($n in @("python.exe", "python3.exe")) {
        foreach ($c in @(Get-Command $n -CommandType Application -All -ErrorAction SilentlyContinue)) {
            [void]$cands.Add(@($c.Source, ""))
        }
    }
    foreach ($pat in @("$env:LOCALAPPDATA\Programs\Python\*\python.exe",
                       "C:\Python*\python.exe",
                       "$env:ProgramFiles\Python*\python.exe")) {
        $found = Get-ChildItem $pat -ErrorAction SilentlyContinue | Sort-Object FullName -Descending
        foreach ($f in $found) { [void]$cands.Add(@($f.FullName, "")) }
    }

    foreach ($c in $cands) {
        $key = ($c[0] + "|" + $c[1]).ToLowerInvariant()
        if ($tried.ContainsKey($key)) { continue }
        $tried[$key] = $true
        $real = Test-GbPython $c[0] $c[1]
        if (-not $real) { continue }
        $w = Get-GbPythonwBeside $real
        if ($w) { return @{ Python = $real; Pythonw = $w } }
    }
    return $null
}

# Remembers the Python that install.ps1 checked, so launch and the watchdog use
# the same one.
function Save-GbPython([string]$python) {
    try {
        [System.IO.File]::WriteAllText($script:GbPythonPathFile, $python,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch {}
}

# The pythonw.exe to run the host with, or $null if there is no working Python.
# Uses the saved path when it still runs, otherwise searches again.
function Get-GbPythonw {
    if (Test-Path -LiteralPath $script:GbPythonPathFile) {
        try {
            $saved = [System.IO.File]::ReadAllText($script:GbPythonPathFile,
                [System.Text.Encoding]::UTF8).Trim()
            $real = Test-GbPython $saved
            $w = Get-GbPythonwBeside $real
            if ($w) { return $w }
        } catch {}
    }
    $found = Find-GbPython
    if ($found) {
        Save-GbPython $found.Python
        return $found.Pythonw
    }
    return $null
}
