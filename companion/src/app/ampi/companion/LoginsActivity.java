package app.ampi.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipboardManager;
import android.content.Intent;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.provider.Settings;
import android.text.InputType;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.textclassifier.TextClassifier;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

import org.json.JSONException;

import java.io.IOException;
import java.security.GeneralSecurityException;
import java.util.List;

import javax.crypto.Cipher;

/** The saved logins: add and delete. Nothing in them can be shown again. */
public final class LoginsActivity extends Activity {
    private static final int TEXT = InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS;

    private final OnBackInvokedCallback toList = this::list;
    private CancellationSignal prompt;
    private boolean adding;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setHideOverlayWindows(true);
    }

    @Override
    protected void onDestroy() {
        if (prompt != null) prompt.cancel();
        super.onDestroy();
    }

    private static byte[] signin(Activity a) {
        Config cfg = Config.load(a);
        return cfg == null ? new byte[0] : cfg.signin();
    }

    private LinearLayout start(String title) {
        LinearLayout page = Ui.page(this);
        View root = (View) page.getParent();
        root.setImportantForContentCapture(View.IMPORTANT_FOR_CONTENT_CAPTURE_NO_EXCLUDE_DESCENDANTS);
        root.setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES);
        root.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        Ui.title(page, title);
        return page;
    }

    // --- the list --------------------------------------------------------------------------------------------

    private void list() {
        if (adding) getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(toList);
        adding = false;
        LinearLayout page = start("Saved logins");
        Ui.small(page, "Only on this phone. Each use needs your fingerprint (any fingerprint saved on this phone, or its face "
                + "unlock if that is of the strong kind). Keep them in your password manager too: without this phone, or after a "
                + "fingerprint is added, they have to be entered again.");
        byte[] signin = signin(this);
        List<Logins.Login> all;
        try {
            all = Logins.all(this);
        } catch (IOException e) {
            LinearLayout card = Ui.card(page);
            Ui.body(card, "The saved logins can't be read.");
            Ui.button(card, "Delete all", false, v -> deleteAll());
            return;
        }
        String server = Logins.serverId(signin);
        boolean gone = false;
        if (!all.isEmpty()) {
            try {
                Logins.toOpen(all.get(0));
            } catch (Logins.Gone e) {
                gone = true;
                LinearLayout card = Ui.card(page);
                Ui.body(card, e.getMessage());
                Ui.button(card, "Delete all", true, v -> deleteAll());
            } catch (GeneralSecurityException | RuntimeException e) {
                Log.w("Ampi", "logins", e);
                LinearLayout card = Ui.card(page);
                Ui.body(card, "The key to the saved logins can't be reached right now. If that stays so, delete them all and add them again.");
                Ui.button(card, "Delete all", false, v -> deleteAll());
            }
        }
        for (Logins.Login l : all) {
            LinearLayout card = Ui.card(page);
            Ui.body(card, String.join("\n", l.hosts()));
            Ui.small(card, l.label());
            if (signin.length > 0 && !l.server().equals(server)) Ui.small(card, "Saved for another server: can't be used with this one.");
            Ui.button(card, "Delete", false, v -> show(new AlertDialog.Builder(this).setTitle("Delete this login?")
                    .setMessage(String.join(", ", l.hosts()) + " · " + l.label()).setNegativeButton("Keep", null)
                    .setPositiveButton("Delete", (d, w) -> {
                        String problem = null;
                        try {
                            Logins.delete(this, l.id());
                        } catch (IOException e) {
                            problem = "It couldn't be deleted: " + e.getMessage();
                        }
                        list();
                        if (problem != null) tell(problem);
                    })));
        }
        if (all.isEmpty()) Ui.small(Ui.card(page), "None yet.");
        if (gone) return;
        String no = whyNotAdd(signin);
        if (no == null) {
            Ui.button(page, "Add a login", true, v -> add());
        } else {
            LinearLayout card = Ui.card(page);
            Ui.body(card, no);
            if (signin.length > 0) {
                Ui.button(card, "Fingerprint settings", true, v -> {
                    try {
                        startActivity(new Intent(Settings.ACTION_BIOMETRIC_ENROLL).putExtra(
                                Settings.EXTRA_BIOMETRIC_AUTHENTICATORS_ALLOWED, BiometricManager.Authenticators.BIOMETRIC_STRONG));
                    } catch (ActivityNotFoundException e) {
                        startActivity(new Intent(Settings.ACTION_SECURITY_SETTINGS));
                    }
                });
            }
        }
        Ui.button(page, "Done", false, v -> finish());
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!adding) list();
    }

    private String whyNotAdd(byte[] signin) {
        if (signin.length == 0) return "To save logins, pair this phone with your server again (scripts\\companion.ps1 -Pair on your PC).";
        return switch (getSystemService(BiometricManager.class).canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG)) {
            case BiometricManager.BIOMETRIC_SUCCESS -> null;
            case BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED -> "Add a fingerprint to this phone first.";
            case BiometricManager.BIOMETRIC_ERROR_HW_UNAVAILABLE -> "The fingerprint sensor isn't ready now.";
            case BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE -> "This phone's fingerprint sensor isn't strong enough for saved logins.";
            default -> "The fingerprint sensor can't be used for saved logins now.";
        };
    }

    private void deleteAll() {
        show(new AlertDialog.Builder(this).setTitle("Delete all saved logins?").setNegativeButton("Keep", null)
                .setPositiveButton("Delete all", (d, w) -> {
                    Logins.deleteAll(this);
                    list();
                }));
    }

    private void tell(String s) {
        show(new AlertDialog.Builder(this).setMessage(s).setPositiveButton("OK", null));
    }

    /** A dialog is a window of its own: screenshots must not see it either. */
    private static void show(AlertDialog.Builder b) {
        AlertDialog d = b.create();
        d.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        d.show();
    }

    // --- adding one --------------------------------------------------------------------------------------------

    private void add() {
        if (!adding) getOnBackInvokedDispatcher().registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, toList);
        adding = true;
        LinearLayout page = start("Add a login");
        LinearLayout card = Ui.card(page);
        EditText site = field(card, "Site, e.g. example.com (more addresses: one per line)",
                TEXT | InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        EditText label = field(card, "Name for it, e.g. Personal", TEXT);
        EditText user = field(card, "User name or email", TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        EditText secret = field(card, "Password", TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        CheckBox show = new CheckBox(this);
        show.setText("Show password");
        show.setTextColor(Ui.quiet(this));
        show.setOnCheckedChangeListener((b, on) -> {
            int at = secret.getSelectionEnd();
            secret.setInputType(TEXT | (on ? InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD : InputType.TYPE_TEXT_VARIATION_PASSWORD));
            secret.setSelection(Math.max(0, at));
        });
        card.addView(show);
        TextView problem = Ui.body(card, "");
        Button save = Ui.button(page, "Save with fingerprint", true, v -> save(site, label, user, secret, problem));
        save.setFilterTouchesWhenObscured(true);
        Ui.small(page, "The clipboard is emptied after saving. Your keyboard may keep a clipboard history of its own: clear it there.");
        Ui.button(page, "Cancel", false, v -> list());
    }

    private EditText field(LinearLayout parent, String hint, int type) {
        Ui.small(parent, hint);
        EditText e = new EditText(this);
        e.setInputType(type);
        e.setTextColor(Ui.text(this));
        e.setSaveEnabled(false);
        e.setTextClassifier(TextClassifier.NO_OP);  // selected text goes to no "smart" service
        e.setImeOptions(EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING | EditorInfo.IME_FLAG_NO_FULLSCREEN);
        parent.addView(e, Ui.FULL, Ui.WRAP);
        return e;
    }

    private void save(EditText site, EditText label, EditText user, EditText secret, TextView problem) {
        List<String> hosts;
        String name = Texts.line(label.getText().toString(), 40), u = user.getText().toString().strip(), s = secret.getText().toString();
        byte[] signin = signin(this);
        Cipher cipher;
        try {
            hosts = Hosts.parse(site.getText().toString());
            if (name.isEmpty()) throw new IllegalArgumentException("Give it a name");
            if (u.isEmpty() || s.isEmpty()) throw new IllegalArgumentException("Enter the user name and the password");
            if (u.length() > 200 || s.length() > 200) throw new IllegalArgumentException("At most 200 characters each");
            if (signin.length == 0) throw new IllegalArgumentException("Pair this phone first");
            cipher = Logins.toSave(this);
        } catch (IllegalArgumentException | Logins.Gone | IOException e) {
            problem.setText(e.getMessage());
            return;
        } catch (GeneralSecurityException | RuntimeException e) {
            Log.e("Ampi", "save", e);
            problem.setText("The phone couldn't make the key: " + Texts.line(String.valueOf(e.getMessage()), 150));
            return;
        }
        problem.setText("");
        if (prompt != null) prompt.cancel();
        prompt = new CancellationSignal();
        new BiometricPrompt.Builder(this).setTitle("Save this login").setSubtitle(name)
                .setDescription("For " + String.join(", ", hosts))
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                .setNegativeButton("Cancel", getMainExecutor(), (d, w) -> { })
                .build().authenticate(new BiometricPrompt.CryptoObject(cipher), prompt, getMainExecutor(),
                        new BiometricPrompt.AuthenticationCallback() {
                            @Override
                            public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) {
                                try {
                                    Logins.add(LoginsActivity.this, Logins.sealed(r.getCryptoObject().getCipher(), name, hosts,
                                            Logins.serverId(signin), u, s));
                                } catch (GeneralSecurityException | JSONException | IOException | RuntimeException e) {
                                    Log.e("Ampi", "save", e);
                                    problem.setText("It wasn't saved. Try again. (" + Texts.line(String.valueOf(e.getMessage()), 150) + ")");
                                    return;
                                }
                                secret.setText("");
                                try {
                                    getSystemService(ClipboardManager.class).clearPrimaryClip();
                                } catch (RuntimeException e) {
                                    Log.w("Ampi", "clipboard", e);
                                }
                                if (!isDestroyed()) list();
                            }

                            @Override
                            public void onAuthenticationError(int code, CharSequence text) {
                                problem.setText("Not saved. " + Texts.line(String.valueOf(text), 200));
                            }
                        });
    }
}
