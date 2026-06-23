package com.gboardrelay;

import android.app.Activity;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.view.View;
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

        showKeyboard();
        connectLoop();
    }

    // ── Immersive mode (hide the Android status/navigation bars) ────────────────

    /** Hide the system status + navigation bars so the relay window is all app,
     *  shortening the wasted strip at the top of the emulator. Sticky immersive so
     *  the bars stay hidden after the soft keyboard or a swipe. */
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
                c.setSystemBarsBehavior(
                        WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
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
                        // Server can send "CLEAR" to reset the field
                        if ("CLEAR".equals(line)) {
                            mainHandler.post(() -> inputField.resetBuffer());
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
