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
import android.view.inputmethod.InputMethodManager;
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
    /** True when the CURRENT selection was created through forwarded key events
     *  (Select mode's Shift+arrows, or Select all's Ctrl+A) and therefore exists on
     *  the WINDOWS side too. Deletion semantics differ: Windows collapses a selection
     *  with ONE backspace / replaces it by typing, so relaying per-char deletes for a
     *  mirrored selection removes double the text. A selection made only locally
     *  (dragging the relay's handles, swipe-delete highlight) leaves this false. */
    private boolean winSelection = false;
    /** True while the buffer holds the shadow padding installed after a reposition
     *  CLEAR (see enterShadowBuffer). All relaying stays relative, so the fake
     *  content never leaks to Windows; it only exists so Gboard enables its editing
     *  panel (arrows / Select / Copy / Cut grey out on an empty field). */
    private boolean shadowMode = false;
    // Shadow padding is built from INVISIBLE FAKE WORDS, not a flat run of spaces.
    // Rationale: Gboard's swipe-on-backspace deletes by WORD. A flat whitespace run
    // has no word boundaries, so Gboard selects the entire run and the "how many
    // words did the user swipe" information is destroyed before we ever see it -
    // every swipe collapsed to a single Ctrl+Backspace on Windows. Using NBSP
    // (U+00A0) as the word BODY and a real space as the SEPARATOR gives the buffer
    // real token structure while still rendering completely blank, so we can count
    // the fake words consumed and forward exactly that many word-deletes.
    private static final char SHADOW_CH = (char) 0x00A0;  // NBSP: renders blank,
    // but is NOT whitespace, so it forms a real word token for Gboard.
    private static final int SHADOW_WORD_LEN = 4;
    private static final int SHADOW_WORDS = 12;   // fake words each side of the caret
    /** True while Gboard holds a selection over the shadow padding (its swipe-on-
     *  backspace gesture SELECTS the fake spaces first: setSelection(a,b), then
     *  collapses, then deletes). That selection exists only in the relay — Windows
     *  has no idea — so the collapse that follows must NOT be forwarded as a caret
     *  jump, and the delete must NOT be forwarded per-char (40 fake spaces would
     *  become 40 real backspaces — the exact bug where a swipe-backspace nuked text
     *  far from the caret). */
    private boolean shadowFakeSel = false;
    /** One-shot: the last collapsed setSelection consumed a fake-padding selection
     *  (i.e. we're mid swipe-on-backspace gesture). While set, the delete that
     *  follows is BACKWARD-intent even if Gboard phrases it as an afterLength
     *  (collapse-to-START variant). Cleared by the delete that consumes it. */
    private boolean shadowGestureDelete = false;
    /** Tell the IME its editor context changed, so Gboard DISCARDS the word it is
     *  currently composing and starts fresh against the new buffer.
     *
     *  Without this, resetting the buffer under an active composing word made Gboard
     *  re-send that word with no delete -> the doubled-first-letter bug. Deferring
     *  the reset until composing ended looked safer, but DEADLOCKS: Windows focus
     *  changes never reach Android, so Gboard can keep a word composing forever and
     *  the reposition was never applied at all - leaving a STALE buffer whose
     *  leftover words then hijacked the next tap ("commands", "app"). restartInput
     *  fixes both: the reset lands immediately AND Gboard drops the stale word. */
    private void notifyImeReset() {
        try {
            InputMethodManager imm = (InputMethodManager)
                    getContext().getSystemService(Context.INPUT_METHOD_SERVICE);
            if (imm != null) imm.restartInput(this);
        } catch (Exception ignored) {}
    }
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

    /** Reset both our model and the on-screen buffer to truly empty. */
    public void resetBuffer() {
        composing = "";
        shadowMode = false;
        winSelection = false;
        shadowFakeSel = false;
        icHandled = true;
        try { setText(""); } finally { icHandled = false; }
        prevText = "";
    }

    /** Build the invisible word-structured padding and centre the caret. Returns the
     *  caret offset. Caller must guard with icHandled. */
    private int installShadowPadding() {
        char[] w = new char[SHADOW_WORD_LEN];
        java.util.Arrays.fill(w, SHADOW_CH);
        String word = new String(w);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < SHADOW_WORDS; i++) { sb.append(word).append(' '); }
        int caret = sb.length();              // caret sits just after a separator
        for (int i = 0; i < SHADOW_WORDS; i++) {
            if (i > 0) sb.append(' ');
            sb.append(word);
        }
        setText(sb.toString());
        setSelection(caret, caret);
        return caret;
    }

    /** Number of whitespace-delimited tokens in [from,to) of the current buffer.
     *  Used to translate a fake-padding deletion into the equivalent number of
     *  real word-deletes on Windows. */
    private int countShadowWords(CharSequence e, int from, int to) {
        if (e == null) return 0;
        int len = e.length();
        from = Math.max(0, Math.min(from, len));
        to   = Math.max(0, Math.min(to,   len));
        if (from >= to) return 0;
        int words = 0;
        boolean inWord = false;
        for (int i = from; i < to; i++) {
            boolean sep = (e.charAt(i) == ' ');   // NBSP is deliberately NOT a separator
            if (!sep && !inWord) { words++; inWord = true; }
            else if (sep) { inWord = false; }
        }
        return words;
    }

    /** Emit the Windows-side equivalent of deleting a run of fake padding that
     *  spanned `words` fake words: one backspace for a single char, otherwise one
     *  Ctrl+Backspace per word, so MULTI-WORD swipe-delete keeps working.
     *
     *  `from`/`to` bound the deleted region so we can detect the dangerous case:
     *  if the region runs to the very edge of the buffer, Gboard may have selected
     *  "everything on this side" rather than the N words the user actually swiped
     *  (that is exactly what it did with the old flat-whitespace padding). We cannot
     *  tell those apart, so we CLAMP to one word - deleting too little is trivially
     *  recoverable, deleting too much is not. Normal swipes never hit the edge
     *  because maybeRecenterShadow keeps several words of headroom on both sides. */
    private void sendShadowDelete(CharSequence src, int from, int to, boolean forward) {
        int chars = to - from;
        if (chars <= 0) return;
        if (chars == 1) { if (forward) send("KEY:DELETE"); else sendDel(1); return; }
        int words = countShadowWords(src, from, to);
        int len = src != null ? src.length() : 0;
        boolean hitEdge = (from <= 0) || (to >= len);
        int n = hitEdge ? 1 : Math.max(1, Math.min(words, SHADOW_WORDS));
        String key = forward ? "KEY:CTRL+DELETE" : "KEY:CTRL+BACKSPACE";
        send(n > 1 ? key + "*" + n : key);
        Log.d(TAG, "shadow delete chars=" + chars + " words=" + words
                + " hitEdge=" + hitEdge + " -> " + n + (forward ? " fwd" : " back"));
    }

    /** Host CLEAR (caret repositioned in Windows, content unknown): instead of a
     *  truly empty buffer — which makes Gboard GREY OUT its whole editing panel —
     *  install invisible shadow padding (spaces) with the caret centered. Gboard
     *  then keeps arrows / Select / Select all / Copy / Cut enabled, and because
     *  every relayed operation is RELATIVE (key events, Ctrl combos, caret deltas),
     *  they act correctly on the real Windows text. The padding itself is never
     *  sent anywhere. */
    public void enterShadowBuffer() {
        // NEVER reset mid-word. A CLEAR that lands while Gboard is composing (stray
        // tap misread as a reposition, or one arriving during a glide) would wipe the
        // composing tracker WITHOUT telling Windows about the prefix we already sent.
        // Gboard then re-sends the whole word and the prefix is never deleted - the
        // "doubled first letter" bug (ffliped, aalso, hnew). Skip instead: the word
        // finishes cleanly and the next reposition CLEAR still lands.
        boolean wasComposing = !composing.isEmpty();
        composing = "";
        winSelection = false;
        shadowFakeSel = false;
        icHandled = true;
        try { installShadowPadding(); } finally { icHandled = false; }
        prevText = getText() != null ? getText().toString() : "";
        shadowMode = true;
        // Drop any half-composed word so it cannot be re-sent against the new buffer.
        if (wasComposing) notifyImeReset();
    }

    /** Shadow mode: deletes/arrows consume the padding; once it runs low, silently
     *  re-install centered padding so Gboard's editing panel stays enabled and the
     *  caret keeps headroom on both sides. Purely local — nothing is sent to
     *  Windows, and the watcher is muted via icHandled. Skipped mid-compose or
     *  mid-fake-selection so we never yank state out from under Gboard. */
    private void maybeRecenterShadow() {
        if (!shadowMode || !composing.isEmpty() || shadowFakeSel) return;
        Editable e = getText();
        int len = e != null ? e.length() : 0;
        int caret = Math.max(0, getSelectionStart());
        // Keep at least a few fake words of headroom on BOTH sides, so a long
        // multi-word swipe always has padding to consume and the panel stays live.
        int leftWords  = countShadowWords(e, 0, caret);
        int rightWords = countShadowWords(e, caret, len);
        if (leftWords < 4 || rightWords < 4) {
            icHandled = true;
            try { installShadowPadding(); } finally { icHandled = false; }
            prevText = getText() != null ? getText().toString() : "";
        }
    }

    /** Called on SYNC: from the Windows host — replaces buffer with the current
     *  Windows field text and positions the cursor to match.
     *  Guarded with icHandled so the TextWatcher doesn't relay the setText back. */
    public void syncFromHost(String text, int selStart, int selEnd) {
        // An EMPTY sync means the host either read an empty Windows field or could
        // not read it at all (common for Electron/Chromium targets). Either way an
        // empty relay buffer would grey out Gboard's whole editing panel, so fall
        // back to the invisible shadow padding instead - the panel stays usable and
        // every operation is still relayed relatively.
        // Same rule as enterShadowBuffer: a SYNC that lands mid-word would silently
        // drop the composing prefix already relayed to Windows and duplicate it.
        boolean wasComposingSync = !composing.isEmpty();
        if (text == null || text.isEmpty()) { enterShadowBuffer(); return; }
        composing = "";
        shadowMode = false;
        winSelection = false;
        shadowFakeSel = false;
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
        if (wasComposingSync) notifyImeReset();
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
        if (shadowMode) {
            // External edit over the FAKE padding (a Gboard delete path we didn't
            // intercept). The counts describe fake padding, not real Windows text.
            // Count the fake WORDS that vanished (oldT is the pre-edit buffer) and
            // forward that many word-deletes, so a multi-word swipe stays multi-word.
            if (delCount > 0) sendShadowDelete(oldT, p, oldLen - s, false);
            if (addCount > 0) sendText(newT.substring(p, newLen - s));
            if (delCount > 0 || addCount > 0) {
                Log.d(TAG, "external shadow edit del=" + delCount + " add=" + addCount);
                composing = "";
                shadowFakeSel = false;
                post(this::maybeRecenterShadow);
            }
            return;
        }
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
                        + " sel=" + selectionLen() + " winSel=" + winSelection);
                // Commit replaces any composing region or active selection.
                int sel = selectionLen();
                if (composing.length() > 0) {
                    sendDel(composing.length());
                    sendText(text);
                } else if (sel > 0 && winSelection) {
                    // The SAME selection exists on Windows (it was made via forwarded
                    // Shift+arrows / Ctrl+A). Typing replaces it there by itself, so
                    // sending per-char deletes would remove EXTRA text. For a pure
                    // delete (empty commit) one backspace collapses the selection.
                    if (text.length() == 0) sendDel(1);
                    sendText(text);
                } else if (sel > 0 && shadowMode) {
                    // Selection over the FAKE shadow padding (swipe-on-backspace
                    // gesture). Windows has no such selection, so per-char deletes
                    // would eat real text. Forward one word-delete per fake WORD the
                    // selection covers; a replacement just types the new text.
                    if (text.length() == 0) {
                        int a = Math.min(getSelectionStart(), getSelectionEnd());
                        int b = Math.max(getSelectionStart(), getSelectionEnd());
                        sendShadowDelete(getText(), a, b, false);
                    }
                    sendText(text);
                    shadowFakeSel = false;
                    shadowGestureDelete = false;
                } else {
                    sendDel(sel);
                    sendText(text);
                }
                composing = "";
                winSelection = false;
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
                // swipe-delete selection) replaces that selection first. If the
                // selection is MIRRORED on Windows (winSelection), typing the first
                // composing char replaces it there by itself — no deletes needed.
                if (composing.length() == 0) {
                    int sel = selectionLen();
                    if (sel > 0 && !winSelection && !shadowMode) { sendDel(sel); }
                    winSelection = false;
                    shadowFakeSel = false;
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
                        + ") sel=" + selectionLen() + " shadow=" + shadowMode);
                if (shadowMode) {
                    // The counts refer to FAKE padding spaces, not real Windows text.
                    // A single tap (1) maps 1:1 to one backspace/delete; anything
                    // larger is the swipe-on-backspace word gesture -> ONE
                    // delete-word-left (Ctrl+Backspace), never N real deletes.
                    // If this delete consumes the gesture's collapsed selection
                    // (shadowGestureDelete), even an afterLength phrasing means the
                    // user swiped BACKSPACE - keep the intent backward.
                    int caret = Math.max(0, getSelectionStart());
                    if (beforeLength > 0) {
                        sendShadowDelete(getText(), caret - beforeLength, caret, false);
                    }
                    if (afterLength > 0) {
                        // Mid-gesture, an afterLength phrasing still means the user
                        // swiped BACKSPACE -> keep the direction backward.
                        sendShadowDelete(getText(), caret, caret + afterLength, !shadowGestureDelete);
                    }
                    shadowGestureDelete = false;
                    shadowFakeSel = false;
                    // Track the composing region exactly as the normal path does -
                    // blanking it here loses the prefix already sent to Windows and
                    // duplicates it on the next composing update.
                    if (beforeLength >= composing.length()) composing = "";
                    else composing = composing.substring(0, composing.length() - beforeLength);
                    icHandled = true;
                    try {
                        boolean r = super.deleteSurroundingText(beforeLength, afterLength);
                        post(RelayEditText.this::maybeRecenterShadow);
                        return r;
                    } finally { icHandled = false; }
                }
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
                        + afterLength + ") shadow=" + shadowMode);
                if (shadowMode) {
                    // Same fake-padding translation as deleteSurroundingText.
                    int caret2 = Math.max(0, getSelectionStart());
                    if (beforeLength > 0) {
                        sendShadowDelete(getText(), caret2 - beforeLength, caret2, false);
                    }
                    if (afterLength > 0) {
                        sendShadowDelete(getText(), caret2, caret2 + afterLength, !shadowGestureDelete);
                    }
                    shadowGestureDelete = false;
                    shadowFakeSel = false;
                    composing = "";
                    icHandled = true;
                    try {
                        boolean r = super.deleteSurroundingTextInCodePoints(beforeLength, afterLength);
                        post(RelayEditText.this::maybeRecenterShadow);
                        return r;
                    } finally { icHandled = false; }
                }
                sendDel(beforeLength);
                for (int i = 0; i < afterLength; i++) send("KEY:DELETE");
                composing = "";
                icHandled = true;
                try { return super.deleteSurroundingTextInCodePoints(beforeLength, afterLength); }
                finally { icHandled = false; }
            }

            @Override
            public boolean setSelection(int start, int end) {
                // CRITICAL for keeping the two buffers aligned: super.setSelection ALWAYS
                // moves the relay field's caret. If we don't mirror that same move to the
                // Windows caret, the two drift apart and every later word lands in the
                // wrong place (the "findcan you" vs "can youfind" scramble). So whenever
                // Gboard moves the caret (NOT one of our own mutations, icHandled), we
                // forward the EXACT delta as arrow presses. The delta is RELATIVE, so it
                // stays correct even after the relay buffer is trimmed (unlike absolute
                // Ctrl+Home/End). We batch with '*N' so a big jump is a single command.
                // This covers arrows, the |< / >| jump buttons, tap-to-position, AND the
                // swipe-the-spacebar-to-move-cursor gesture. No composing guard: Gboard
                // routes typing-time caret advances through commit/compose (which never
                // call this override), so a setSelection here is always a real move.
                int curPos = getSelectionStart();   // relay caret == Windows caret (invariant)
                Log.d(TAG, "setSelection(" + start + "," + end + ") icHandled=" + icHandled
                        + " from=" + curPos + " composing=" + composing);
                if (!icHandled && start != end) {
                    // A non-collapsed setSelection is a RELAY-ONLY selection (swipe-
                    // delete highlight, drag handles) — it is NOT mirrored on Windows.
                    winSelection = false;
                    if (shadowMode) shadowFakeSel = true;
                }
                if (!icHandled && start == end) {
                    winSelection = false;           // collapsed on both sides
                    if (shadowMode && shadowFakeSel) {
                        // Collapse step of Gboard's swipe-on-backspace gesture over the
                        // FAKE padding selection. Windows never saw that selection, so
                        // forwarding this as a caret move (RIGHT*k / Ctrl+End) would
                        // shift the real caret before the delete lands — the forward-
                        // delete bug. Swallow it and mark the gesture so the delete
                        // that follows is treated as BACKWARD-intent regardless of
                        // which side Gboard collapsed to.
                        shadowFakeSel = false;
                        shadowGestureDelete = true;
                        return super.setSelection(start, end);
                    }
                    int delta = start - curPos;
                    if (delta != 0) {
                        int len = getText() != null ? getText().length() : 0;
                        if (shadowMode && start <= 0) {
                            // Shadow padding: relay offsets are fiction, so the |< / >|
                            // jump buttons map to ABSOLUTE Windows jumps instead.
                            send("KEY:CTRL+HOME");
                        } else if (shadowMode && start >= len) {
                            send("KEY:CTRL+END");
                        } else if (delta > 0) {
                            send("KEY:RIGHT*" + delta);
                        } else {
                            send("KEY:LEFT*" + (-delta));
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
                        // Shift+nav extends a selection on BOTH sides; a plain nav key
                        // collapses both. Track it so deletes over a mirrored selection
                        // aren't double-relayed.
                        winSelection = (meta & KeyEvent.META_SHIFT_ON) != 0;
                    } else if (event.getKeyCode() == KeyEvent.KEYCODE_DEL) {
                        if (selectionLen() > 0 && shadowMode) {
                            // DEL over a FAKE padding selection (alternate swipe-delete
                            // path): one word-delete per fake WORD covered, so a
                            // multi-word swipe deletes multiple words on Windows.
                            int a = Math.min(getSelectionStart(), getSelectionEnd());
                            int b = Math.max(getSelectionStart(), getSelectionEnd());
                            sendShadowDelete(getText(), a, b, false);
                            shadowFakeSel = false;
                            winSelection = false;
                            icHandled = true;
                            try {
                                boolean r = super.sendKeyEvent(event);
                                post(RelayEditText.this::maybeRecenterShadow);
                                return r;
                            } finally { icHandled = false; }
                        }
                        if (selectionLen() > 0 && winSelection) {
                            // Backspace over a MIRRORED selection: one backspace deletes
                            // the whole selection on Windows. Apply the same one-key
                            // semantics and swallow the local removal (icHandled) so the
                            // safety-net watcher doesn't relay it again per-char.
                            send("DEL:1");
                            winSelection = false;
                            icHandled = true;
                            try { return super.sendKeyEvent(event); }
                            finally { icHandled = false; }
                        }
                        if (getText() == null || getText().length() == 0) {
                            // Empty relay box (e.g. after a reposition CLEAR): Gboard has
                            // nothing to delete locally, so backspace would do nothing on
                            // Windows. Forward it — the Windows field may still have text.
                            send("DEL:1");
                        }
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
                Log.d(TAG, "performContextMenuAction(" + id + ") winSel=" + winSelection);
                if (id == android.R.id.selectAll) {
                    send("KEY:CTRL+A");
                    winSelection = true;   // now selected on BOTH sides
                } else if (id == android.R.id.copy) {
                    send("KEY:CTRL+C");
                } else if (id == android.R.id.cut) {
                    // Ctrl+X removes the mirrored Windows selection by itself. The base
                    // cut below removes the RELAY copy — swallow that local removal
                    // (icHandled) so the safety-net watcher doesn't relay it AGAIN as
                    // backspaces (that double-deleted: selection + N extra chars).
                    send("KEY:CTRL+X");
                    winSelection = false;
                    icHandled = true;
                    try { return super.performContextMenuAction(id); }
                    finally { icHandled = false; }
                } else if (id == android.R.id.paste) {
                    send("KEY:CTRL+V");
                    return true;   // skip base paste to avoid emulator-clipboard desync
                }
                return super.performContextMenuAction(id);
            }

            @Override
            public boolean commitCompletion(CompletionInfo text) {
                Log.d(TAG, "commitCompletion(" + (text != null ? text.getText() : null) + ")");
                int del = composing.length() > 0 ? composing.length() : selectionLen();
                if (shadowMode && composing.length() == 0) del = 0;  // fake selection
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
