package app.ampi.companion;

import android.app.Activity;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.security.GeneralSecurityException;
import java.util.Arrays;

import javax.crypto.Cipher;

/** Shows the one open sign-in request; only the fingerprint goes on from here. */
public final class UnlockActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable tick = this::tick, over = this::over;
    private Unlock.Ask ask;
    private Logins.Login chosen;
    private CancellationSignal prompt;
    private TextView left;
    private String note = "";

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
        ((android.view.View) page.getParent()).setAccessibilityDataSensitive(android.view.View.ACCESSIBILITY_DATA_SENSITIVE_YES);
        Ui.title(page, "Use a saved login?");
        LinearLayout card = Ui.card(page);
        TextView host = Ui.add(card, ask.host, 26, Ui.accent(this));
        host.setTypeface(android.graphics.Typeface.MONOSPACE, android.graphics.Typeface.BOLD);
        Ui.body(card, "Only a test: the login is unlocked here, and nothing of it is sent or filled in.");
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
        if (!note.isEmpty()) Ui.body(card, note);
        Button go = Ui.button(page, "Use fingerprint", true, v -> unlock());
        go.setFilterTouchesWhenObscured(true);
        go.setEnabled(chosen != null);
        go.setAlpha(chosen != null ? 1 : 0.4f);
        Ui.button(page, "Refuse", false, v -> ask.refuse());
        left = Ui.small(page, "");
        left.setPadding(0, Ui.dp(this, 12), 0, 0);
        left.setText(Math.max(0, ask.left() / 1000) + " seconds left");
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
                .setDescription("Test: nothing is sent or filled in").setConfirmationRequired(true)
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                .setNegativeButton("Back", getMainExecutor(), (d, w) -> { })
                .build().authenticate(new BiometricPrompt.CryptoObject(cipher), prompt, getMainExecutor(),
                        new BiometricPrompt.AuthenticationCallback() {
                            @Override
                            public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) {
                                opened(a, l, r.getCryptoObject().getCipher());
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

    private void opened(Unlock.Ask a, Logins.Login l, Cipher cipher) {
        if (!a.live()) {
            over();
            return;
        }
        byte[] plain = null;
        try {
            plain = Logins.opened(cipher, l);
            a.unlocked();
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
