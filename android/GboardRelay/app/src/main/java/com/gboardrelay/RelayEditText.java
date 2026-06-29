package com.gboardrelay;

import android.content.Context;
import android.text.Editable;
import android.text.TextWatcher;
import android.util.AttributeSet;
import android.util.Log;
import android.view.KeyEvent;
import android.view.inputmethod.CompletionInfo;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputConnectionWrapper;
import android.widget.EditText;

/**
 * EditText that taps Gboard's InputConnection so the host receives EXACTLY what
 * the keyboard does — commit, composing-region replacement, delete, swipe-delete
 * — instead of guessing from a whole-field text diff.
 *
 * Why this beats the old TextWatcher diff:
 *   • Corrections (tapping a suggestion) replace the composing word via
 *     setComposingText / commitText. We mirror that precisely, so the text and
 *     spaces no longer get jumbled.
 *   • Swipe-typed words arrive as a single commitText with Gboard's own spacing,
 *     so the auto-space lands exactly where Gboard puts it (no after/before swap).
 *   • The real text stays in the field, so Gboard has full context (suggestions,
 *     glide typing) AND the swipe-delete selection highlights on screen.
 *
 * Safety net for swipe / gesture delete:
 *   Gboard's word-swipe-delete deletes through paths that aren't a plain
 *   deleteSurroundingText (it varies by version — sendKeyEvent on a selection,
 *   setSelection + delete, etc.). Enumerating them is fragile. So in ADDITION to
 *   the precise InputConnection overrides we keep a TextWatcher that fires only
 *   for changes NOT produced by our own overrides (guarded by {@link #icHandled}).
 *   That watcher forwards any external DELETION as DEL — restoring swipe-delete no
 *   matter which mechanism Gboard uses. Insertions are left to the IC overrides
 *   (and Activity ENTER handling) so they're never double-sent.
 *
 * Every Gboard edit happens at the cursor (end of buffer): the live word,
 * corrections that replace the just-typed word, swipe words, and swipe-delete of
 * trailing text. So mirroring them as end-anchored DEL/TEXT on Windows is exact.
 */
public class RelayEditText extends EditText {

    private static final String TAG = "RELAYIC";

    /** Sink for wire commands (TEXT:/DEL:/KEY:...). Set by the Activity. */
    public interface Sender { void send(String cmd); }

    private Sender sender;
    /** Text currently marked as composing (always at the cursor/end). */
    private String composing = "";
    /** Whole-field snapshot kept in sync so the watcher can diff external edits. */
    private String prevText = "";
    /** True while one of OUR InputConnection overrides is mutating the field, so
     *  the TextWatcher knows that change is already accounted for and skips it. */
    private boolean icHandled = false;
    /** Trim the buffer once it gets long, but only at a word boundary. */
    private static final int TRIM_AT = 800, TRIM_KEEP = 400;

    public RelayEditText(Context c) { super(c); init(); }
    public RelayEditText(Context c, AttributeSet a) { super(c, a); init(); }
    public RelayEditText(Context c, AttributeSet a, int d) { super(c, a, d); init(); }

    private void init() {
        prevText = getText() != null ? getText().toString() : "";
        addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int st, int c, int a) {}
            @Override public void onTextChanged(CharSequence s, int st, int b, int c) {}
            @Override public void afterTextChanged(Editable e) {
                String cur = e.toString();
                if (!icHandled) reconcileExternal(prevText, cur);
                prevText = cur;
            }
        });
    }

    public void setSender(Sender s) { this.sender = s; }

    /** Reset both our model and the on-screen buffer (server CLEAR command). */
    public void resetBuffer() {
        composing = "";
        icHandled = true;
        try { setText(""); } finally { icHandled = false; }
        prevText = "";
    }

    /** Called on SYNC: from the Windows host — replaces buffer with the current
     *  Windows field text and positions the cursor to match.
     *  Guarded with icHandled so the TextWatcher doesn't relay the setText back. */
    public void syncFromHost(String text, int selStart, int selEnd) {
        composing = "";
        icHandled = true;
        try {
            setText(text);
            int len = text.length();
            int s = Math.max(0, Math.min(selStart, len));
            int e = Math.max(0, Math.min(selEnd,   len));
            setSelection(s, e);
        } finally {
            icHandled = false;
        }
        prevText = text;
    }

    /** Called on CURSOR: from the Windows host — moves the Android cursor to match
     *  a mouse-click reposition in Windows. Guarded with icHandled so our
     *  setSelection override doesn't re-emit a KEY:CTRL+HOME/END. */
    public void setCursorFromHost(int selStart, int selEnd) {
        icHandled = true;
        try {
            int len = getText() != null ? getText().length() : 0;
            int s = Math.max(0, Math.min(selStart, len));
            int e = Math.max(0, Math.min(selEnd,   len));
            setSelection(s, e);
        } finally {
            icHandled = false;
        }
    }

    private void send(String cmd) {
        Sender s = sender;
        if (s != null) s.send(cmd);
    }

    /** Emit typed text, converting embedded newlines to KEY:ENTER. */
    private void sendText(CharSequence cs) {
        if (cs == null || cs.length() == 0) return;
        String[] parts = cs.toString().split("\n", -1);
        for (int i = 0; i < parts.length; i++) {
            if (!parts[i].isEmpty()) send("TEXT:" + parts[i]);
            if (i < parts.length - 1) send("KEY:ENTER");
        }
    }

    private void sendDel(int n) { if (n > 0) send("DEL:" + n); }

    /** Map a cursor-movement keycode to a host KEY name, or null if it's not a
     *  navigation key we forward. Text-changing keys (DEL, ENTER, TAB) return null
     *  on purpose - those are handled elsewhere. */
    private static String navKeyName(int code) {
        switch (code) {
            case KeyEvent.KEYCODE_DPAD_LEFT:  return "LEFT";
            case KeyEvent.KEYCODE_DPAD_RIGHT: return "RIGHT";
            case KeyEvent.KEYCODE_DPAD_UP:    return "UP";
            case KeyEvent.KEYCODE_DPAD_DOWN:  return "DOWN";
            case KeyEvent.KEYCODE_MOVE_HOME:  return "HOME";
            case KeyEvent.KEYCODE_MOVE_END:   return "END";
            case KeyEvent.KEYCODE_PAGE_UP:    return "PAGEUP";
            case KeyEvent.KEYCODE_PAGE_DOWN:  return "PAGEDOWN";
            default: return null;
        }
    }

    /** Chars currently selected — the range Gboard is about to replace/delete. */
    private int selectionLen() {
        int a = getSelectionStart(), b = getSelectionEnd();
        if (a < 0 || b < 0) return 0;
        return Math.abs(b - a);
    }

    /** Forward an edit that arrived OUTSIDE our InputConnection overrides — i.e.
     *  Gboard's swipe/gesture delete. End-anchored diff: anything removed from the
     *  cursor end becomes DEL; a simultaneous insertion (rare) becomes TEXT. Pure
     *  insertions are ignored here — those always travel through the IC overrides
     *  (or Activity ENTER handling), so forwarding them again would double them. */
    private void reconcileExternal(String oldT, String newT) {
        int oldLen = oldT.length(), newLen = newT.length();
        int p = 0, max = Math.min(oldLen, newLen);
        while (p < max && oldT.charAt(p) == newT.charAt(p)) p++;
        int s = 0;
        while (s < (max - p)
                && oldT.charAt(oldLen - 1 - s) == newT.charAt(newLen - 1 - s)) s++;
        int delCount = oldLen - p - s;   // chars removed
        int addCount = newLen - p - s;   // chars inserted
        if (delCount > 0) {
            Log.d(TAG, "external delete del=" + delCount + " add=" + addCount
                    + " (swipe/gesture delete)");
            sendDel(delCount);
            if (addCount > 0) sendText(newT.substring(p, newLen - s));
            composing = "";   // external edit invalidates the composing baseline
        }
    }

    /** Replace the tracked composing word with newText using an end-anchored
     *  minimal diff (composing is always at the cursor end, so backspacing from
     *  the Windows cursor is correct). */
    private void replaceComposing(String newText) {
        String oldText = composing;
        int oldLen = oldText.length(), newLen = newText.length();
        int p = 0, maxP = Math.min(oldLen, newLen);
        while (p < maxP && oldText.charAt(p) == newText.charAt(p)) p++;
        int s = 0;
        while (s < (maxP - p)
                && oldText.charAt(oldLen - 1 - s) == newText.charAt(newLen - 1 - s)) s++;
        sendDel(oldLen - p - s);
        sendText(newText.substring(p, newLen - s));
        composing = newText;
    }

    /** When the buffer gets long, drop the leading text (already sent to Windows)
     *  so the field doesn't grow without bound. Only safe with no composing word. */
    private void maybeTrim() {
        if (composing.isEmpty()) {
            Editable e = getText();
            if (e != null && e.length() > TRIM_AT) {
                String tail = e.subSequence(e.length() - TRIM_KEEP, e.length()).toString();
                setText(tail);
                setSelection(tail.length());
            }
        }
    }

    @Override
    public InputConnection onCreateInputConnection(EditorInfo outAttrs) {
        InputConnection base = super.onCreateInputConnection(outAttrs);
        if (base == null) return null;
        return new InputConnectionWrapper(base, true) {

            @Override
            public boolean commitText(CharSequence text, int newCursorPosition) {
                Log.d(TAG, "commitText(" + text + ") composing=" + composing
                        + " sel=" + selectionLen());
                // Commit replaces any composing region or active selection.
                int del = composing.length() > 0 ? composing.length() : selectionLen();
                sendDel(del);
                sendText(text);
                composing = "";
                icHandled = true;
                try {
                    boolean r = super.commitText(text, newCursorPosition);
                    maybeTrim();
                    return r;
                } finally { icHandled = false; }
            }

            @Override
            public boolean setComposingText(CharSequence text, int newCursorPosition) {
                Log.d(TAG, "setComposingText(" + text + ") composing=" + composing
                        + " sel=" + selectionLen());
                // Starting to compose over a selection (e.g. retyping after a
                // swipe-delete selection) replaces that selection first.
                if (composing.length() == 0) {
                    int sel = selectionLen();
                    if (sel > 0) { sendDel(sel); }
                }
                replaceComposing(text.toString());
                icHandled = true;
                try { return super.setComposingText(text, newCursorPosition); }
                finally { icHandled = false; }
            }

            @Override
            public boolean setComposingRegion(int start, int end) {
                Log.d(TAG, "setComposingRegion(" + start + "," + end + ")");
                // Gboard re-marks existing text (often the last word) as composing
                // so it can offer corrections. Track what's in that region so a
                // following setComposingText diffs against the right baseline.
                Editable e = getText();
                if (e != null) {
                    int a = Math.max(0, Math.min(start, end));
                    int b = Math.min(e.length(), Math.max(start, end));
                    composing = (a < b) ? e.subSequence(a, b).toString() : "";
                }
                return super.setComposingRegion(start, end);
            }

            @Override
            public boolean finishComposingText() {
                Log.d(TAG, "finishComposingText composing=" + composing);
                composing = "";
                return super.finishComposingText();
            }

            @Override
            public boolean deleteSurroundingText(int beforeLength, int afterLength) {
                Log.d(TAG, "deleteSurroundingText(" + beforeLength + "," + afterLength
                        + ") sel=" + selectionLen());
                // Backspace and swipe-delete of trailing text.
                sendDel(beforeLength);
                for (int i = 0; i < afterLength; i++) send("KEY:DELETE");
                if (beforeLength >= composing.length()) composing = "";
                else composing = composing.substring(0, composing.length() - beforeLength);
                icHandled = true;
                try { return super.deleteSurroundingText(beforeLength, afterLength); }
                finally { icHandled = false; }
            }

            @Override
            public boolean deleteSurroundingTextInCodePoints(int beforeLength, int afterLength) {
                Log.d(TAG, "deleteSurroundingTextInCodePoints(" + beforeLength + ","
                        + afterLength + ")");
                sendDel(beforeLength);
                composing = "";
                icHandled = true;
                try { return super.deleteSurroundingTextInCodePoints(beforeLength, afterLength); }
                finally { icHandled = false; }
            }

            @Override
            public boolean setSelection(int start, int end) {
                // The relay field's cursor is always mirrored to Windows (we relay every
                // edit + nav key), so getSelectionStart() BEFORE this call is exactly
                // where the Windows caret is. A collapsed setSelection that did NOT come
                // from our own mutation (icHandled) and is NOT inside an active composing
                // word is a pure cursor MOVE: Gboard's editing-panel arrows, the |< / >|
                // jump buttons, OR the swipe-the-spacebar-to-move-cursor gesture. Forward
                // the delta to Windows as the matching arrow presses so both carets track.
                int curPos = getSelectionStart();   // current cursor == Windows caret
                Log.d(TAG, "setSelection(" + start + "," + end + ") icHandled=" + icHandled
                        + " from=" + curPos + " composing=" + composing);
                if (!icHandled && start == end && composing.isEmpty()) {
                    int len = getText() != null ? getText().length() : 0;
                    int delta = start - curPos;
                    if (delta != 0) {
                        if (start <= 0 && curPos > 1) {
                            send("KEY:CTRL+HOME");          // jump to very start (1 key)
                        } else if (start >= len && (len - curPos) > 1) {
                            send("KEY:CTRL+END");            // jump to very end (1 key)
                        } else if (delta > 0) {
                            for (int i = 0; i < delta; i++)  send("KEY:RIGHT");
                        } else {
                            for (int i = 0; i < -delta; i++) send("KEY:LEFT");
                        }
                    }
                }
                return super.setSelection(start, end);
            }

            @Override
            public boolean sendKeyEvent(KeyEvent event) {
                Log.d(TAG, "sendKeyEvent action=" + event.getAction()
                        + " code=" + event.getKeyCode() + " meta=" + event.getMetaState()
                        + " sel=" + selectionLen() + " composing=" + composing);
                // Forward cursor-movement / selection keys (the Gboard text-editing
                // panel: arrows, Home/End, the "Select" toggle = Shift held). We do NOT
                // forward text-CHANGING keys here: DEL is left to the TextWatcher safety
                // net (single source of truth), ENTER/TAB go via Activity.dispatchKeyEvent.
                // The base connection still applies the key so the relay field's cursor
                // and selection mirror the Windows side (keeps Copy/Cut accurate).
                if (event.getAction() == KeyEvent.ACTION_DOWN) {
                    String name = navKeyName(event.getKeyCode());
                    if (name != null) {
                        int meta = event.getMetaState();
                        String mods = "";
                        if ((meta & KeyEvent.META_CTRL_ON) != 0)  mods += "CTRL+";
                        if ((meta & KeyEvent.META_SHIFT_ON) != 0) mods += "SHIFT+";
                        if ((meta & KeyEvent.META_ALT_ON) != 0)   mods += "ALT+";
                        send("KEY:" + mods + name);
                    }
                }
                return super.sendKeyEvent(event);
            }

            @Override
            public boolean performContextMenuAction(int id) {
                // The panel's Select all / Copy / Cut / Paste buttons. Mirror them to
                // Windows as the standard Ctrl shortcuts. Select all / Copy / Cut also
                // run on the base field so its selection stays in sync; Paste is NOT run
                // on the base (the emulator clipboard differs from Windows') - we only
                // forward Ctrl+V so Windows pastes its own clipboard.
                Log.d(TAG, "performContextMenuAction(" + id + ")");
                if (id == android.R.id.selectAll) { send("KEY:CTRL+A"); }
                else if (id == android.R.id.copy) { send("KEY:CTRL+C"); }
                else if (id == android.R.id.cut)  { send("KEY:CTRL+X"); }
                else if (id == android.R.id.paste) {
                    send("KEY:CTRL+V");
                    return true;   // skip base paste to avoid emulator-clipboard desync
                }
                return super.performContextMenuAction(id);
            }

            @Override
            public boolean commitCompletion(CompletionInfo text) {
                Log.d(TAG, "commitCompletion(" + (text != null ? text.getText() : null) + ")");
                int del = composing.length() > 0 ? composing.length() : selectionLen();
                sendDel(del);
                if (text != null) sendText(text.getText());
                composing = "";
                icHandled = true;
                try { return super.commitCompletion(text); }
                finally { icHandled = false; }
            }
        };
    }
}
