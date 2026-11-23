package app.ampi.companion;

import android.content.Context;
import android.os.PowerManager;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.net.Socket;
import java.security.GeneralSecurityException;
import java.util.Arrays;

import javax.net.ssl.SSLSocket;

/**
 * A sign-in channel: a second connection to the gateway, and inside it TLS with the sign-in service, whose certificate was fixed
 * while pairing. Nothing of a request is looked at before that has been proved, so the gateway can't make one up.
 */
final class VaultSession implements Runnable {
    private static final int LIFE = 120_000, MAX_FRAME = 8192, MAX_WAIT = 75;
    private static VaultSession current;

    private final Context c;
    private final Config cfg;
    private final String ch;
    private final PowerManager.WakeLock lock;
    private final Socket raw = new Socket();
    private final Object writing = new Object();
    private Tls tls;
    private Unlock.Ask ask;

    private VaultSession(Context c, Config cfg, String ch) {
        this.c = c.getApplicationContext();
        this.cfg = cfg;
        this.ch = ch;
        lock = c.getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ampi:signin");
        lock.setReferenceCounted(false);
    }

    /** The gateway asked for a channel. It only has one at a time, so an older one here is dead. */
    static synchronized void open(Context c, Config cfg, String ch) {
        if (cfg.signin().length == 0 || !ch.matches("[0-9a-f]{32}")) return;
        if (current != null) current.closeRaw();
        current = new VaultSession(c, cfg, ch);
        current.lock.acquire(LIFE);  // before the caller lets the phone sleep again
        new Thread(current, "ampi-signin").start();
    }

    private void closeRaw() {
        new Thread(() -> {
            try {
                raw.close();
            } catch (IOException e) {
                // Closed either way.
            }
        }, "ampi-close").start();
    }

    @Override
    public void run() {
        try {
            SSLSocket outer = Link.connect(cfg, raw);
            outer.setSoTimeout(15_000);
            Link.welcome(outer);
            Frames.write(outer.getOutputStream(), new JSONObject().put("t", "vault").put("ch", ch).toString());
            tls = new Tls(Keys.context(cfg.alias(), cfg.signin()), outer.getInputStream(), outer.getOutputStream());
            if (!Arrays.equals(tls.peer(), cfg.signin())) throw new IOException("another sign-in service");
            outer.setSoTimeout(LIFE);
            for (String f = Frames.read(tls.input(), MAX_FRAME); f != null; f = Frames.read(tls.input(), MAX_FRAME)) {
                handle(new JSONObject(f));
            }
        } catch (IOException | GeneralSecurityException | JSONException e) {
            Log.i("Ampi", "sign-in channel: " + e);
        } catch (Throwable e) {  // a broken message must not end the app (an alarm may be ringing)
            Log.e("Ampi", "sign-in channel", e);
        } finally {
            if (ask != null) ask.end("interrupted");  // nobody is there to be answered: the screen goes away
            if (tls != null) {
                synchronized (writing) {
                    tls.close();
                }
            }
            closeRaw();
            synchronized (VaultSession.class) {
                if (current == this) current = null;
            }
            if (lock.isHeld()) lock.release();
        }
    }

    private void handle(JSONObject m) {
        String host = m.optString("host");
        if (m.optInt("v") != 1 || !"get".equals(m.optString("op")) || !"test".equals(m.optString("purpose"))) {
            send(Unlock.result("unsupported"));
        } else if (!Hosts.valid(host)) {
            send(Unlock.result("bad_request"));
        } else {
            int wait = Math.max(5, Math.min(MAX_WAIT, m.optInt("wait", MAX_WAIT)));
            Unlock.Ask a = Unlock.request(c, host, wait * 1000L, cfg.signin(), this::send);
            if (a != null) ask = a;
        }
    }

    private void send(JSONObject o) {
        try {
            synchronized (writing) {
                tls.write(Frames.bytes(o.toString()));
            }
        } catch (IOException | RuntimeException e) {
            Log.i("Ampi", "sign-in answer: " + e);
            closeRaw();
        }
    }
}
