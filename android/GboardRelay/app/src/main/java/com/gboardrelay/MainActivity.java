package com.gboardrelay;

import android.app.Activity;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.view.View;
import android.view.MotionEvent;
import android.widget.TextView;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.Socket;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

/**
 * GboardRelay — bridges Gboard IME input to a Windows host via TCP.
 *
 * Protocol (newline-delimited):
 *   TEXT:<chars>   — forward these characters
 *   DEL:<n>        — send n backspaces
 *   KEY:ENTER      — send Enter
 *   KEY:TAB        — send Tab
 *   PING           — keepalive
 */
public class MainActivity extends Activity {

    private static final int HOST_PORT = 9876;
    private static final String HOST_ADDR = "127.0.0.1";

    private RelayEditText inputField;
    private TextView statusText;
    private TextView titleText;
    private Handler mainHandler;
    private ExecutorService ioExecutor;
    /** Single-threaded so wire commands are written in the EXACT order they're
     *  produced. A multi-threaded pool races DEL/TEXT writes out of order, which
     *  jumbles corrections, swipe spacing, and commit-then-delete sequences. */
    private ExecutorService sendExecutor;

    private volatile PrintWriter socketOut;
    private volatile Socket activeSocket;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        hideSystemBars();   // after setContentView: needs the decor view / insets controller

        inputField = findViewById(R.id.input_field);
        statusText = findViewById(R.id.status_text);
        titleText  = findViewById(R.id.title_text);
        mainHandler = new Handler(Looper.getMainLooper());
        ioExecutor = Executors.newCachedThreadPool();
        sendExecutor = Executors.newSingleThreadExecutor();

        // Forward Gboard's actual InputConnection operations (commit, composing,
        // delete, swipe-delete) straight to the Windows host — no fragile diff.
        inputField.setSender(this::sendCmd);

        enterKioskMode();
        showKeyboard();
        connectLoop();
    }

    // ── Lock Task (kiosk) mode ──────────────────────────────────────────────────

    /** If this app has been made Device Owner
     *  (`adb shell dpm set-device-owner com.gboardrelay/.RelayAdminReceiver`),
     *  whitelist itself and pin into Lock Task mode. That fully disables Home,
     *  Recents, and the gesture-nav swipe-up, so the keyboard can't be accidentally
     *  swiped away. If we're not Device Owner, this is a harmless no-op. */
    private void enterKioskMode() {
        try {
            DevicePolicyManager dpm =
                    (DevicePolicyManager) getSystemService(Context.DEVICE_POLICY_SERVICE);
            ComponentName admin = new ComponentName(this, RelayAdminReceiver.class);
            if (dpm != null && dpm.isDeviceOwnerApp(getPackageName())) {
                dpm.setLockTaskPackages(admin, new String[]{ getPackageName() });
            }
            // startLockTask() works when the package is lock-task-whitelisted (above);
            // otherwise it falls back to screen-pinning (which shows a confirm dialog),
            // so only call it when we're whitelisted as Device Owner.
            if (dpm != null && dpm.isLockTaskPermitted(getPackageName())) {
                startLockTask();
            }
        } catch (Exception ignored) {
            // Not provisioned as Device Owner — keyboard still works normally.
        }
    }

    // ── Immersive mode (hide the Android status/navigation bars) ────────────────

    /** Hide the system status + navigation bars so the relay window is all app,
     *  shortening the wasted strip at the top of the emulator. The bars stay hidden
     *  permanently — swiping from the bottom does NOT reveal the navbar or open
     *  recents/home, preventing accidental app exits. */
    private void hideSystemBars() {
        // Draw edge-to-edge: the activity content extends behind (and replaces) the
        // status + navigation bars, so there's no fixed black inset band at the top.
        if (android.os.Build.VERSION.SDK_INT >= 30) {
            getWindow().setDecorFitsSystemWindows(false);
            // Render into the camera-cutout area too, so there's no reserved black
            // strip at the top of the Pixel display profile.
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.layoutInDisplayCutoutMode =
                    WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS;
            getWindow().setAttributes(lp);
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                // BEHAVIOR_SHOW_BARS_BY_TOUCH only shows on touch in the bar area — prevents gesture
                // navigation from triggering recents/home on swipe-up from bottom
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_BARS_BY_TOUCH);
            }
        } else {
            View decor = getWindow().getDecorView();
            decor.setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                  | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                  | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                  | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                  | View.SYSTEM_UI_FLAG_FULLSCREEN
                  | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
        }
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        if (event.getAction() == KeyEvent.ACTION_DOWN) {
            switch (event.getKeyCode()) {
                case KeyEvent.KEYCODE_BACK:
                    // Consume back button — prevent accidental exit
                    return true;
                case KeyEvent.KEYCODE_ENTER:
                    sendCmd("KEY:ENTER");
                    return true;
                case KeyEvent.KEYCODE_TAB:
                    sendCmd("KEY:TAB");
                    return true;
            }
        }
        return super.dispatchKeyEvent(event);
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent event) {
        // Block all touches in the bottom gesture-nav area (~80px) to prevent swipe-up
        // from opening recents/app switcher. This intercepts BEFORE the system gesture
        // handler sees the touch, at the earliest dispatch point.
        float bottomThreshold = getWindow().getDecorView().getHeight() - 80;
        if (event.getY() > bottomThreshold) {
            // Block the gesture — return true to consume and prevent system handling
            return true;
        }
        return super.dispatchTouchEvent(event);
    }

    // ── Networking ───────────────────────────────────────────────────────────

    private void connectLoop() {
        ioExecutor.submit(() -> {
            while (!Thread.currentThread().isInterrupted()) {
                setStatus("Connecting to Windows host...", false);
                try {
                    Socket s = new Socket(HOST_ADDR, HOST_PORT);
                    s.setTcpNoDelay(true);
                    activeSocket = s;
                    socketOut = new PrintWriter(s.getOutputStream(), true);
                    setStatus("Connected ✓  —  type to send to Windows", true);

                    // Read ACK / server messages (optional, keeps socket alive)
                    BufferedReader reader = new BufferedReader(
                            new InputStreamReader(s.getInputStream()));
                    String line;
                    while ((line = reader.readLine()) != null) {
                        if ("CLEAR".equals(line)) {
                            mainHandler.post(() -> inputField.resetBuffer());
                        } else if (line.startsWith("SYNC:")) {
                            final String payload = line.substring(5);
                            mainHandler.post(() -> handleSync(payload));
                        } else if (line.startsWith("CURSOR:")) {
                            final String payload = line.substring(7);
                            mainHandler.post(() -> handleCursor(payload));
                        }
                    }
                } catch (Exception e) {
                    // fall through to retry
                }
                socketOut = null;
                activeSocket = null;
                setStatus("Disconnected — retrying...", false);
                try { Thread.sleep(2000); } catch (InterruptedException ie) { break; }
            }
        });
    }

    private void sendCmd(final String cmd) {
        // Serialize on a single thread so commands keep their submission order.
        sendExecutor.submit(() -> {
            PrintWriter out = socketOut;
            if (out != null) {
                try { out.println(cmd); }
                catch (Exception ignored) {}
            }
        });
    }

    // ── Host→app command handlers ────────────────────────────────────────────

    /** SYNC:<base64text>:<sel_start>:<sel_end>
     *  Replace the relay buffer with the current Windows field text and cursor. */
    private void handleSync(String payload) {
        try {
            // payload format: <base64>:<start>:<end>  (base64 has no colons)
            int c1 = payload.indexOf(':');
            int c2 = payload.indexOf(':', c1 + 1);
            if (c1 < 0 || c2 < 0) return;
            byte[] decoded = android.util.Base64.decode(
                    payload.substring(0, c1), android.util.Base64.DEFAULT);
            String text     = new String(decoded, "UTF-8");
            int    selStart = Integer.parseInt(payload.substring(c1 + 1, c2));
            int    selEnd   = Integer.parseInt(payload.substring(c2 + 1));
            inputField.syncFromHost(text, selStart, selEnd);
        } catch (Exception ignored) {}
    }

    /** CURSOR:<sel_start>:<sel_end>
     *  Move the relay cursor to match a mouse-click reposition in Windows. */
    private void handleCursor(String payload) {
        try {
            int colon = payload.indexOf(':');
            if (colon < 0) return;
            int selStart = Integer.parseInt(payload.substring(0, colon));
            int selEnd   = Integer.parseInt(payload.substring(colon + 1));
            inputField.setCursorFromHost(selStart, selEnd);
        } catch (Exception ignored) {}
    }

    // ── UI helpers ───────────────────────────────────────────────────────────

    private void setStatus(String msg, boolean ok) {
        mainHandler.post(() -> {
            statusText.setText(msg);
            statusText.setTextColor(ok ? 0xFF4CAF50 : 0xFFFF8A65);
        });
    }

    private void showKeyboard() {
        inputField.requestFocus();
        inputField.postDelayed(() -> {
            InputMethodManager imm = (InputMethodManager)
                    getSystemService(INPUT_METHOD_SERVICE);
            if (imm != null) {
                imm.showSoftInput(inputField, InputMethodManager.SHOW_FORCED);
            }
        }, 300);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ioExecutor.shutdownNow();
        sendExecutor.shutdownNow();
        try { if (activeSocket != null) activeSocket.close(); }
        catch (Exception ignored) {}
    }
}
