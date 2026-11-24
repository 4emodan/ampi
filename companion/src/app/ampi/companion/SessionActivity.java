package app.ampi.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipDescription;
import android.content.ClipboardManager;
import android.content.Intent;
import android.os.Bundle;
import android.os.PersistableBundle;
import android.util.Log;
import android.view.View;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.security.GeneralSecurityException;

/** Sessions from Firefox: takes one that is shared or pasted, and sets the extension up. Any app may share a text to it: it is
 *  opened only with the extension's key, and only when Ampi asks for that site and the owner gives a fingerprint. */
public final class SessionActivity extends Activity {
    private String note = "", key = "";

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setHideOverlayWindows(true);
        if (state == null) received(getIntent());
        draw();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        received(intent);
        draw();
    }

    private void received(Intent i) {
        if (i == null || !Intent.ACTION_SEND.equals(i.getAction())) return;
        note = "";
        try {
            take(i.getStringExtra(Intent.EXTRA_TEXT));
        } catch (RuntimeException e) {  // any app may send anything
            Log.w("Ampi", "shared text", e);
        }
    }

    private void take(String text) {
        try {
            SessionBox b = Handover.take(this, text);
            hideKey();
            note = "Received. Ask Ampi within 10 minutes to use your session on " + b.site() + ". (It is checked when it is opened.)";
        } catch (IllegalArgumentException e) {
            note = e.getMessage();
        } catch (RuntimeException e) {
            Log.e("Ampi", "session", e);
            note = "That session can't be read.";
        }
    }

    private LinearLayout page(String title) {
        LinearLayout page = Ui.page(this);
        View root = (View) page.getParent();
        root.setImportantForContentCapture(View.IMPORTANT_FOR_CONTENT_CAPTURE_NO_EXCLUDE_DESCENDANTS);
        root.setAccessibilityDataSensitive(View.ACCESSIBILITY_DATA_SENSITIVE_YES);
        Ui.title(page, title);
        return page;
    }

    private void draw() {
        if (!key.isEmpty()) {
            drawKey();
            return;
        }
        LinearLayout page = page("Sessions from Firefox");
        Ui.small(page, "For sites you sign in to with Google, a passkey or something else Ampi can't do: sign in there yourself in Firefox, "
                + "give the session with the Ampi button, and ask Ampi. It gets that one site's session, for one task, after your fingerprint.");
        if (!note.isEmpty()) Ui.body(Ui.card(page), note);
        SessionBox b = Handover.current();
        if (b != null) {
            LinearLayout card = Ui.card(page);
            Ui.body(card, b.host());
            Ui.small(card, b.count() + " cookies, given " + Alarm.when(b.at()) + ". Nothing was sent yet.");
            Ui.button(card, "Forget it", false, v -> {
                Handover.clear();
                note = "";
                draw();
            });
        }
        boolean gone = Handover.ready() && !Handover.usable();
        if (gone) Ui.body(Ui.card(page), "The phone's fingerprints changed, so this key is no good any more: make a new one and paste it into the extension.");
        if (Handover.ready()) {
            if (!gone) Ui.button(page, "Paste a session", true, v -> {
                CharSequence text = pasted();
                take(text == null ? "" : text.toString());
                draw();
            });
            Ui.button(page, "New key for the extension", false, v -> sure("Make a new key?", "The key the extension has now stops working.", "New key", this::key));
            Ui.button(page, "Switch off", false, v -> sure("Switch this off?", "The extension's key is deleted here.", "Switch off", () -> {
                Handover.remove();
                note = "";
                draw();
            }));
        } else {
            LinearLayout card = Ui.card(page);
            Ui.body(card, Logins.fingerprintReady(this) ? "Not set up yet: install the extension in Firefox, then make a key here and paste it there."
                    : "Add a fingerprint to this phone first.");
            if (Logins.fingerprintReady(this)) Ui.button(card, "Make a key", true, v -> key());
        }
        Ui.button(page, "Done", false, v -> finish());
    }

    private CharSequence pasted() {
        ClipData clip = getSystemService(ClipboardManager.class).getPrimaryClip();
        return clip == null || clip.getItemCount() == 0 ? null : clip.getItemAt(0).getText();
    }

    private void sure(String title, String text, String yes, Runnable then) {
        AlertDialog d = new AlertDialog.Builder(this).setTitle(title).setMessage(text).setNegativeButton("Keep", null)
                .setPositiveButton(yes, (x, w) -> then.run()).create();
        d.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        d.show();
    }

    private void key() {
        try {
            key = Handover.create();
        } catch (GeneralSecurityException | RuntimeException e) {
            Log.e("Ampi", "session key", e);
            note = "The phone couldn't make the key: " + Texts.line(String.valueOf(e.getMessage()), 150);
        }
        draw();
    }

    private void hideKey() {
        if (key.isEmpty()) return;
        key = "";
        try {
            getSystemService(ClipboardManager.class).clearPrimaryClip();
        } catch (RuntimeException e) {
            Log.w("Ampi", "clipboard", e);
        }
    }

    private void drawKey() {
        LinearLayout page = page("The extension's key");
        LinearLayout card = Ui.card(page);
        Ui.body(card, "Copy it, then in Firefox open the Ampi button and paste it there, and come back here. It is shown only now.");
        TextView t = Ui.small(card, key);
        t.setTypeface(android.graphics.Typeface.MONOSPACE);
        ClipboardManager board = getSystemService(ClipboardManager.class);
        Ui.button(page, "Copy", true, v -> {
            ClipData clip = ClipData.newPlainText("", key);
            PersistableBundle secret = new PersistableBundle();
            secret.putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true);
            clip.getDescription().setExtras(secret);
            board.setPrimaryClip(clip);
        });
        Ui.button(page, "Done (empties the clipboard)", false, v -> {
            hideKey();
            note = "";
            draw();
        });
    }
}
