# GboardIME — Google Keyboard on Windows

Type and **glide-type (swipe)** on the real Google Keyboard (Gboard) and have the text
appear in any focused Windows application. Gboard runs inside an Android emulator; a tiny
relay app captures its actual input events and forwards them to a Windows host process that
replays them with `SendInput`. Everything stays on `localhost` — no external network is used.

```
┌──────────────────────────────┐         ┌───────────────────────────┐
│   Android emulator (AVD)      │         │        Windows host       │
│                               │         │                           │
│   Gboard  ──InputConnection── │         │   gboard_host.py          │
│      │                        │         │     listens :9877         │
│      ▼                        │         │        │                  │
│   GboardRelay app             │  ADB    │        ▼                  │
│     connects 127.0.0.1:9876 ──┼─reverse─┼──▶  SendInput → focused   │
│                               │ 9876→   │     Windows app           │
│                               │   9877  │                           │
└──────────────────────────────┘         └───────────────────────────┘
```

The relay forwards Gboard's **real** `InputConnection` operations (commit, compose, delete,
swipe-delete) rather than diffing text, so corrections, gesture typing, and backspace-swipe
behave exactly as they do on a phone.

## Repository layout

| Path | What it is |
|------|------------|
| `android/GboardRelay/` | The Android relay app (Java). Captures Gboard input and sends it over TCP. |
| `windows/gboard_host.py` | The Windows host. Receives commands and replays them via `SendInput`; system-tray icon; draggable custom title bar docked to the emulator window. |
| `windows/GboardRelay.apk` | Pre-built debug APK of the relay app. |
| `windows/debloat_removed_packages.txt` | The ~70 packages removed from the AVD to cut background load, with restore instructions. |
| `setup.ps1` | One-time bootstrap: SDK tools, system image, AVD, Python deps, builds the APK. |
| `launch.ps1` | Starts (or reuses) the emulator, sets the ADB reverse tunnel, launches the relay app, starts the host. |
| `stop.ps1` | Cleanly stops the host, relay, reverse tunnel, and emulator. |

`launch.ps1` and `stop.ps1` are wired to **Start GboardIME** / **Quit GboardIME** Start-menu
shortcuts.

## Requirements

- Windows 10/11
- [Android Studio](https://developer.android.com/studio) / Android SDK (`platform-tools`, `emulator`)
- A Java runtime (Android Studio bundles one at `jbr/`)
- Python 3 (host uses `pystray`, `pillow`, `uiautomation` — installed by `setup.ps1`)
- A CPU with virtualization enabled (for the x86_64 emulator) and a GPU (host-GPU mode is required for smooth swipe typing)

## Install

The easiest path: **double-click `Install.cmd`** (or run `powershell -ExecutionPolicy Bypass -File install.ps1`).

The installer is idempotent and does everything end-to-end:

- downloads the Android SDK cmdline-tools / platform-tools / emulator if missing
- installs the `google_apis` x86_64 API-34 system image (the rootable, non-Play-Store image)
- creates the `GboardIME_Root` AVD and patches it to 1080x1180 @ 420dpi with host-GPU
- installs the Python host dependencies and the prebuilt relay APK
- cold-boots the emulator, sets Gboard as the default keyboard, sets the ADB reverse tunnel
- provisions kiosk / Lock Task mode (Device Owner) so the keyboard can't be swiped away
- debloats the emulator (Play Services, Assistant, stock apps) to cut background load
- creates **Start GboardIME** / **Quit GboardIME** Start-menu shortcuts

Switches:

```powershell
.\install.ps1 -SkipDebloat   # keep all stock apps
.\install.ps1 -SkipKiosk     # don't lock the keyboard to the foreground
.\install.ps1 -SkipEmulator  # build/install host bits only; don't boot or provision
```

Requires a Java runtime only if the prebuilt APK is missing (Android Studio bundles one). After
install, use the Start-menu shortcuts to run it.

## Run

```powershell
.\launch.ps1        # or the "Start GboardIME" Start-menu shortcut
```

Click into any Windows text field, then type/swipe on Gboard in the emulator window. Text
lands in the focused Windows app.

```powershell
.\stop.ps1          # or the "Quit GboardIME" Start-menu shortcut
```

## Relay protocol (newline-delimited TCP)

| Command | Meaning |
|---------|---------|
| `TEXT:<chars>` | Forward these characters |
| `DEL:<n>` | Send `n` backspaces |
| `KEY:ENTER` | Send Enter |
| `KEY:TAB` | Send Tab |
| `PING` | Keepalive |
| `CLEAR` (host→app) | Reset the relay's editor buffer |

Commands are written on a single serialized thread so delete/text/commit sequences keep their
exact order.

## Notes & quirks

- **Smooth swipe typing requires host-GPU mode.** Software rendering (SwiftShader) drops touch
  samples mid-glide and produces wrong words. Launch the emulator with `-gpu host`.
- **Gboard's keyboard height is capped at ~⅔ of the screen.** Presets and prefs can shrink it but
  not exceed that ceiling — this is baked into Gboard and not configurable around.
- **Kiosk / Lock Task mode** keeps the keyboard locked to the foreground so it can't be
  accidentally swiped away. The app becomes Device Owner and pins itself; Home, Recents, and the
  gesture swipe-up are all disabled. Provision once (no Google account may be present on the AVD):
  ```
  adb shell dpm set-device-owner com.gboardrelay/.RelayAdminReceiver
  ```
  This persists across reboots. To exit on purpose, use **Quit GboardIME**.
- The AVD is **debloated** (Play Services, Assistant, and dozens of stock apps removed) to reduce
  background load. Gboard's typing/swipe/prediction is fully on-device and works without Play
  Services. See `windows/debloat_removed_packages.txt` to restore anything.

## License

Personal project. Gboard and Google Keyboard are trademarks of Google LLC; this project does not
redistribute Gboard — you install it yourself inside the emulator.
