<#
  Builds a self-contained, signed Setup.exe.

  1. Stages the distributable files into a temp folder.
  2. Zips them into setup\payload.zip.
  3. Compiles setup\Setup.cs with the in-box .NET Framework csc.exe, embedding the
     zip as the resource "GboardIME.payload.zip".
  4. Code-signs Setup.exe with a self-signed cert (created/reused in CurrentUser\My)
     and timestamps it. (Self-signed: SmartScreen will still warn unless the public
     cert setup\GboardIME-codesign.cer is trusted by the machine.)

  Run from anywhere:  powershell -ExecutionPolicy Bypass -File setup\build-setup.ps1
#>
$ErrorActionPreference = "Stop"
$REPO = Split-Path $PSScriptRoot           # repo root (setup\ is one level down)
$OUT  = Join-Path $REPO "Setup.exe"
$ZIP  = Join-Path $PSScriptRoot "payload.zip"
$CER  = Join-Path $PSScriptRoot "GboardIME-codesign.cer"

function Say($m){ Write-Host $m -ForegroundColor Cyan }

# ---- 1. stage files -----------------------------------------------------------
Say "Staging distributable files..."
$stage = Join-Path $env:TEMP ("gboardime-stage-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $stage | Out-Null

$files = @(
    "install.ps1","launch.ps1","stop.ps1","Install.cmd","README.md",
    "windows\gboard_host.py","windows\GboardRelay.apk","windows\debloat_removed_packages.txt"
)
foreach ($f in $files) {
    $src = Join-Path $REPO $f
    if (-not (Test-Path $src)) { throw "missing file: $f" }
    $dst = Join-Path $stage $f
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $src $dst -Force
}
# android sources (whole tree, minus build artifacts)
$androidSrc = Join-Path $REPO "android"
$androidDst = Join-Path $stage "android"
Copy-Item $androidSrc $androidDst -Recurse -Force
Get-ChildItem $androidDst -Recurse -Directory -Include "build",".gradle",".idea" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ---- 2. zip -------------------------------------------------------------------
Say "Creating payload.zip..."
if (Test-Path $ZIP) { Remove-Item $ZIP -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $ZIP -Force
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue

# ---- 3. compile ---------------------------------------------------------------
Say "Compiling Setup.exe..."
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
$compArg = "/resource:`"$ZIP`",GboardIME.payload.zip"
& $csc /nologo /target:exe /platform:anycpu `
    "/out:$OUT" `
    /reference:System.IO.Compression.dll `
    /reference:System.IO.Compression.FileSystem.dll `
    $compArg `
    (Join-Path $PSScriptRoot "Setup.cs")
if (-not (Test-Path $OUT)) { throw "compile failed" }
Say ("Built {0} ({1:N0} bytes)" -f $OUT, (Get-Item $OUT).Length)

# ---- 4. sign ------------------------------------------------------------------
Say "Signing (self-signed code cert)..."
$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -eq "CN=GboardIME" -and $_.HasPrivateKey } |
    Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=GboardIME" `
        -CertStoreLocation Cert:\CurrentUser\My -KeyUsage DigitalSignature `
        -FriendlyName "GboardIME code signing" -NotAfter (Get-Date).AddYears(5)
}
Export-Certificate -Cert $cert -FilePath $CER -Force | Out-Null
$ts = "http://timestamp.digicert.com"
try   { Set-AuthenticodeSignature -FilePath $OUT -Certificate $cert -TimestampServer $ts -HashAlgorithm SHA256 | Out-Null }
catch { Set-AuthenticodeSignature -FilePath $OUT -Certificate $cert -HashAlgorithm SHA256 | Out-Null }
$sig = Get-AuthenticodeSignature $OUT
Say ("Signature: {0} ({1})" -f $sig.Status, $sig.SignerCertificate.Subject)
Say "Done."
