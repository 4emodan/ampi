package app.ampi.companion;

import android.app.Activity;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.textclassifier.TextClassifier;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.security.GeneralSecurityException;
import java.util.Arrays;

import javax.crypto.Cipher;

/** Shows the one open sign-in request; only the fingerprint goes on from here, or a one-time code the owner types. */
public final class UnlockActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable tick = this::tick, over = this::over;
    private Unlock.Ask ask;
    private Logins.Login chosen;
    private CancellationSignal prompt;
    private TextView left;
    private String note = "", code = "";

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setHideOverlayWindows(true);
        begin();
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        if (ask == null || !ask.live()) begin();
    }

    private void begin() {
        ask = Unlock.current();
        chosen = null;
        note = "";
        code = "";
        if (ask == null || !ask.live()) {
            finish();
            return;
        }
        if (ask.choices.size() == 1) chosen = ask.choices.get(0);
        draw();
    }

    @Override
    protected void onResume() {
        super.onResume();
        Unlock.watch(over);
        over();
        handler.post(tick);
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(tick);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        Unlock.unwatch(over);
        stopPrompt();
        super.onDestroy();
    }

    /** The request was decided some other way: nothing is left to do here. */
    private void over() {
        if (ask != null && ask.live()) return;
        stopPrompt();
        finish();
    }

    private void tick() {
        handler.postDelayed(tick, 1000);
        if (ask != null && left != null) left.setText(Math.max(0, ask.left() / 1000) + " seconds left");
        if (ask == null || !ask.live()) over();
    }

    private void draw() {
        LinearLayout page = Ui.page(this);
        View root = (View) page.getParent();
        root.setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES);
        root.setImportantForContentCapture(View.IMPORTANT_FOR_CONTENT_CAPTURE_NO_EXCLUDE_DESCENDANTS);
        root.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        boolean onlyCode = ask.kind == Unlock.Kind.CODE, asksCode = onlyCode || ask.kind == Unlock.Kind.FILL_CODE;
        boolean saved = chosen != null && chosen.totp();
        Ui.title(page, onlyCode ? "A code to sign in?" : ask.kind == Unlock.Kind.TEST ? "Use a saved login?" : "Sign in?");
        LinearLayout card = Ui.card(page);
        TextView host = Ui.add(card, ask.host, 26, Ui.accent(this));
        host.setTypeface(android.graphics.Typeface.MONOSPACE, android.graphics.Typeface.BOLD);
        Ui.body(card, onlyCode ? "You just had your login filled in there. The site asks for a one-time code, and it goes into that page. "
                + "Ampi doesn't see it."
                : ask.kind == Unlock.Kind.TEST ? "Only a test: the login is unlocked here, and nothing of it is sent or filled in."
                : "Ampi's browser on your server has this site's sign-in page open. Your user name and password are "
                + "filled in there. Ampi doesn't see them, and can then use your account there until the browser is closed.");
        if (ask.choices.size() > 1) {
            Ui.small(card, "Which one?");
            for (Logins.Login l : ask.choices) {
                Ui.button(card, (l == chosen ? "✓ " : "") + l.label(), l == chosen, v -> {
                    chosen = l;
                    draw();
                });
            }
        } else {
            Ui.small(card, chosen.label());
        }
        if (asksCode) {
            LinearLayout box = Ui.card(page);
            Ui.body(box, "If the code came in a message, read it: if it is about a payment, a new phone or email, or any change, refuse.");
            Ui.small(box, saved ? "Your authenticator key is saved with this login: use the fingerprint, or type a code instead."
                    : "The code the site asks for");
            EditText e = new EditText(this);
            e.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
            e.setTextColor(Ui.text(this));
            e.setTextSize(24);
            e.setSaveEnabled(false);
            e.setTextClassifier(TextClassifier.NO_OP);
            e.setImeOptions(EditorInfo.IME_ACTION_DONE | EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING | EditorInfo.IME_FLAG_NO_FULLSCREEN);
            e.setText(code);
            e.setSelection(code.length());
            e.addTextChangedListener(new TextWatcher() {
                @Override
                public void beforeTextChanged(CharSequence s, int start, int count, int after) { }

                @Override
                public void onTextChanged(CharSequence s, int start, int before, int count) { }

                @Override
                public void afterTextChanged(Editable s) {
                    code = s.toString();
                }
            });
            box.addView(e, Ui.FULL, Ui.WRAP);
        }
        if (!note.isEmpty()) Ui.body(card, note);
        if (onlyCode) {
            Button send = Ui.button(page, "Send this code", !saved, v -> sendTyped());
            send.setFilterTouchesWhenObscured(true);
        }
        if (!onlyCode || saved) {
            Button go = Ui.button(page, "Use fingerprint", true, v -> unlock());
            go.setFilterTouchesWhenObscured(true);
            go.setEnabled(chosen != null);
            go.setAlpha(chosen != null ? 1 : 0.4f);
        }
        Ui.button(page, "Refuse", false, v -> ask.refuse());
        left = Ui.small(page, "");
        left.setPadding(0, Ui.dp(this, 12), 0, 0);
        left.setText(Math.max(0, ask.left() / 1000) + " seconds left");
    }

    /** What was typed, as a code; null (and a note) when it isn't one. */
    private String typed() {
        String c = Totp.typed(code);
        if (c == null) say("That doesn't look like a code: 4 to 16 letters and digits.");
        return c;
    }

    private void sendTyped() {
        Unlock.Ask a = ask;
        if (a == null || chosen == null || !a.live()) {
            over();
            return;
        }
        String c = typed();
        if (c != null) {
            code = "";
            a.typed(chosen, c);
        }
    }

    private void stopPrompt() {
        CancellationSignal p = prompt;
        prompt = null;
        if (p != null) p.cancel();
    }

    private void unlock() {
        Unlock.Ask a = ask;
        Logins.Login l = chosen;
        if (a == null || l == null || !a.live()) {
            over();
            return;
        }
        String typed = null;
        if (a.kind == Unlock.Kind.FILL_CODE && (!l.totp() || !code.isBlank()) && (typed = typed()) == null) return;
        String with = typed;
        Cipher cipher;
        try {
            cipher = Logins.toOpen(l);
        } catch (Logins.Gone e) {
            a.end("key_gone");
            return;
        } catch (GeneralSecurityException | RuntimeException e) {
            Log.e("Ampi", "unlock", e);
            say("The phone couldn't start that. Try again.");
            return;
        }
        stopPrompt();
        prompt = new CancellationSignal();
        new BiometricPrompt.Builder(this).setTitle(a.host).setSubtitle(l.label())
                .setDescription(a.kind == Unlock.Kind.TEST ? "Test: nothing is sent or filled in"
                        : a.kind == Unlock.Kind.CODE ? "A code to sign in there" : "Sign in there").setConfirmationRequired(true)
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                .setNegativeButton("Back", getMainExecutor(), (d, w) -> { })
                .build().authenticate(new BiometricPrompt.CryptoObject(cipher), prompt, getMainExecutor(),
                        new BiometricPrompt.AuthenticationCallback() {
                            @Override
                            public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) {
                                opened(a, l, r.getCryptoObject().getCipher(), with);
                            }

                            @Override
                            public void onAuthenticationError(int code, CharSequence text) {
                                // Not a refusal: the prompt's own time is short, and anything may have closed it.
                                boolean closed = code == BiometricPrompt.BIOMETRIC_ERROR_USER_CANCELED
                                        || code == BiometricPrompt.BIOMETRIC_ERROR_CANCELED
                                        || code == BiometricPrompt.BIOMETRIC_ERROR_TIMEOUT;
                                say(closed ? "" : Texts.line(String.valueOf(text), 200));
                            }
                        });
    }

    private void opened(Unlock.Ask a, Logins.Login l, Cipher cipher, String typed) {
        if (!a.live()) {
            over();
            return;
        }
        byte[] plain = null;
        try {
            plain = Logins.opened(cipher, l);
            a.unlocked(l, plain, typed);
        } catch (GeneralSecurityException | RuntimeException e) {
            Log.e("Ampi", "unlock", e);
            say("That login couldn't be opened. Try again; if it stays, delete it and add it again.");
        } finally {
            if (plain != null) Arrays.fill(plain, (byte) 0);
        }
    }

    private void say(String s) {
        if (isDestroyed() || ask == null || !ask.live()) return;
        note = s;
        draw();
    }
}
