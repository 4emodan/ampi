package app.ampi.companion;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.util.Base64;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;

/** Pairing, started by scripts/companion.ps1 over the USB cable (nothing but adb can start it). */
public final class PairActivity extends Activity {
    /** The key waiting for the owner's yes on both sides; PairReceiver gives its public certificate to the script. */
    static volatile Pending pending;

    record Pending(Config config, byte[] certificate) {}

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setHideOverlayWindows(true);
        drop();  // a key left over from a screen Android closed
        ask(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        drop();
        ask(intent);
    }

    private void ask(Intent intent) {
        LinearLayout page = Ui.page(this);
        Ui.title(page, "Pair with your server");
        LinearLayout card = Ui.card(page);
        if (Config.load(this) != null) {
            Ui.body(card, "This phone is already paired. Unpair it first (Ampi, at the bottom).");
            Ui.button(card, "Close", true, v -> finish());
            return;
        }
        try {
            String raw = String.valueOf(intent.getStringExtra("offer"));
            if (raw.length() > 8000) throw new JSONException("too long");
            JSONObject o = new JSONObject(new String(Base64.decode(raw, Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP), StandardCharsets.UTF_8));
            if (o.getInt("v") != 2) throw new JSONException("another version");
            String host = Texts.line(o.getString("host"), 100);
            int port = o.getInt("port");
            byte[] server = certificate(o.getString("cert"));
            byte[] signin = o.getString("signin").isEmpty() ? new byte[0] : certificate(o.getString("signin"));
            if (port < 1 || port > 65535 || !host.matches("[A-Za-z0-9.:-]{3,100}")) throw new JSONException("bad address");
            Ui.body(card, host + ":" + port);
            Ui.small(card, "Only go on if you just started this on your own PC. Ampi's server will be able to show notifications "
                    + "and set alarms on this phone" + (signin.length == 0 ? "." : ", and to ask for saved logins (each time you "
                    + "decide with your fingerprint)."));
            Ui.button(card, "Pair", true, v -> begin(host, port, server, signin));
            Ui.button(card, "Cancel", false, v -> finish());
        } catch (JSONException | GeneralSecurityException | IllegalArgumentException e) {
            Ui.body(card, "That isn't a pairing offer.");
            Ui.button(card, "Close", true, v -> finish());
        }
    }

    private static byte[] certificate(String b64) throws GeneralSecurityException {
        return ((X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(
                new ByteArrayInputStream(Base64.decode(b64, Base64.DEFAULT)))).getEncoded();
    }

    private void begin(String host, int port, byte[] server, byte[] signin) {
        String alias = null;
        try {
            drop();
            alias = Keys.create();
            byte[] mine = Keys.certificate(alias).getEncoded();
            String code = Texts.pairCode(server, mine, signin);
            pending = new Pending(new Config(host, port, server, signin, alias, code), mine);
            LinearLayout page = Ui.page(this);
            Ui.title(page, "Compare the code");
            LinearLayout card = Ui.card(page);
            Ui.body(card, "Your PC shows a code. Is it exactly this one?");
            TextView t = Ui.add(card, code.replace(" ", "\n"), 30, Ui.accent(this));
            t.setTypeface(android.graphics.Typeface.MONOSPACE);
            t.setPadding(0, Ui.dp(this, 12), 0, Ui.dp(this, 12));
            Ui.small(card, "Answer “yes” on the PC first, then press the button here.");
            Ui.button(card, "The codes match", true, v -> commit());
            Ui.button(card, "Cancel", false, v -> {
                drop();
                finish();
            });
        } catch (GeneralSecurityException | RuntimeException e) {
            if (alias != null) Keys.delete(alias);
            LinearLayout page = Ui.page(this);
            Ui.title(page, "That didn't work");
            Ui.body(Ui.card(page), "The phone couldn't make a key: " + e.getMessage());
        }
    }

    private void commit() {
        Pending p = pending;
        if (p == null || Config.load(this) != null) return;
        p.config().save(this);
        pending = null;
        LinkService.start(this, false);
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }

    private static void drop() {
        Pending p = pending;
        pending = null;
        if (p != null) Keys.delete(p.config().alias());
    }

    @Override
    protected void onDestroy() {
        if (isFinishing()) drop();
        super.onDestroy();
    }
}
