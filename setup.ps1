# GboardIME Setup Script
# Downloads Android cmdline-tools, installs system image, creates AVD, builds APK
# Run once before first use.

$ErrorActionPreference = "Stop"
$SDK      = "$env:LOCALAPPDATA\Android\Sdk"
$ADB      = "$SDK\platform-tools\adb.exe"
$JAVA     = "C:\Program Files\Android\Android Studio\jbr\bin\java.exe"
$AVD_NAME = "GboardIME_Pixel6"
$SYSIMAGE = "system-images;android-34;google_apis_playstore;x86_64"

function Step($msg) { Write-Host "`n== $msg ==" -ForegroundColor Cyan }

# ── 1. Install cmdline-tools ─────────────────────────────────────────────────
Step "Checking cmdline-tools"
$sdkManager = "$SDK\cmdline-tools\latest\bin\sdkmanager.bat"
if (-not (Test-Path $sdkManager)) {
    Write-Host "Downloading Android cmdline-tools (~135 MB)..."
    $zip = "$env:TEMP\cmdline-tools.zip"
    Invoke-WebRequest `
        "https://dl.google.com/android/repository/commandlinetools-win-14742923_latest.zip" `
        -OutFile $zip -UseBasicParsing
    Write-Host "Extracting..."
    Expand-Archive $zip "$env:TEMP\cmdline-tools-extract" -Force
    $dest = "$SDK\cmdline-tools\latest"
    New-Item -ItemType Directory -Force $dest | Out-Null
    # The zip contains a single "cmdline-tools" folder; move its contents
    $src = "$env:TEMP\cmdline-tools-extract\cmdline-tools"
    if (Test-Path $src) {
        Copy-Item "$src\*" $dest -Recurse -Force
    } else {
        Copy-Item "$env:TEMP\cmdline-tools-extract\*" $dest -Recurse -Force
    }
    Remove-Item $zip -Force
    Remove-Item "$env:TEMP\cmdline-tools-extract" -Recurse -Force
    Write-Host "cmdline-tools installed."
} else {
    Write-Host "cmdline-tools already present."
}

# ── 2. Accept licenses ───────────────────────────────────────────────────────
Step "Accepting SDK licenses"
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:ANDROID_HOME = $SDK
"y`ny`ny`ny`ny`ny`n" | & $sdkManager --licenses 2>&1 | Out-Null
Write-Host "Licenses accepted."

# ── 3. Install system image ──────────────────────────────────────────────────
Step "Checking system image (google_apis_playstore x86_64 API 34)"
$imgPath = "$SDK\system-images\android-34\google_apis_playstore\x86_64"
if (-not (Test-Path $imgPath)) {
    Write-Host "Downloading system image (~1.5 GB). This may take a few minutes..."
    & $sdkManager $SYSIMAGE --sdk_root=$SDK 2>&1
    Write-Host "System image installed."
} else {
    Write-Host "System image already present."
}

# ── 4. Create AVD ────────────────────────────────────────────────────────────
Step "Creating AVD: $AVD_NAME"
$avdManager = "$SDK\cmdline-tools\latest\bin\avdmanager.bat"
$existing = & $avdManager list avd 2>&1
if ($existing -notmatch $AVD_NAME) {
    "no" | & $avdManager create avd `
        --name $AVD_NAME `
        --package $SYSIMAGE `
        --device "pixel_6" `
        --force 2>&1
    Write-Host "AVD created."
} else {
    Write-Host "AVD already exists."
}

# ── 4b. Install Python host dependencies ─────────────────────────────────────
Step "Installing Python host dependencies"
$pyExe = "C:\Python314\python.exe"
if (-not (Test-Path $pyExe)) { $pyExe = "python" }   # fall back to PATH
# pystray + pillow: tray icon; uiautomation: focus-driven auto show/hide.
& $pyExe -m pip install --user pystray pillow uiautomation 2>&1 | Out-Null
Write-Host "Python deps installed (pystray, pillow, uiautomation)."

# ── 5. Build Android APK ──────────────────────────────────────────────────────
Step "Building GboardRelay APK"
$project = "$PSScriptRoot\android\GboardRelay"
Push-Location $project
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
$env:ANDROID_HOME = $SDK
& .\gradlew.bat assembleDebug 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed! Check output above." -ForegroundColor Red
    Pop-Location; exit 1
}
Pop-Location
$apk = "$project\app\build\outputs\apk\debug\app-debug.apk"
Write-Host "APK built: $apk" -ForegroundColor Green

# Copy APK to windows folder for easy access
Copy-Item $apk "$PSScriptRoot\windows\GboardRelay.apk" -Force

Step "Setup complete!"
Write-Host @"

Next steps:
  1. Run launch.ps1  — starts the emulator + host
  2. In the emulator: Settings > System > Language & input > On-screen keyboard
     → Install Gboard from Play Store, set it as default keyboard
  3. Press Ctrl+Alt+K to show/hide the keyboard window
  4. Click any Windows app text field, then type in Gboard

"@ -ForegroundColor Green
