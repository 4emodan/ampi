package app.ampi.companion;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.net.Socket;
import java.security.GeneralSecurityException;
import java.util.ArrayDeque;
import java.util.Arrays;

import javax.net.ssl.SSLSocket;

/**
 * A sign-in channel: a second connection to the gateway, and inside it TLS with the sign-in service, whose certificate was fixed
 * while pairing. Nothing of a request is looked at before that has been proved, so the gateway can't make one up. One request
 * per channel, and everything about a channel ends at a fixed moment: the gateway can't keep one for later either.
 */
final class VaultSession implements Runnable {
    private static final long LIFE = 110_000, SETUP = 20_000, REQUEST = 10_000;
    private static final int MAX_FRAME = 8192, MAX_WAIT = 75, PER_MINUTE = 6, PER_HOUR = 40;
    private static final Handler main = new Handler(Looper.getMainLooper());
    private static final ArrayDeque<Long> opened = new ArrayDeque<>();  // elapsedRealtime
    private static VaultSession current;
    private static Runnable next;  // a channel asked for while an older one was still ending
    private static long nextUntil;

    private final Context c;
    private final Config cfg;
    private final String ch;
    private final PowerManager.WakeLock lock;
    private final Socket raw = new Socket();
    private final Object writing = new Object();
    private final Runnable end = this::closeRaw;
    private volatile SSLSocket outer;
    private Tls tls;
    private Unlock.Ask ask;

    private VaultSession(Context c, Config cfg, String ch) {
        this.c = c.getApplicationContext();
        this.cfg = cfg;
        this.ch = ch;
        lock = c.getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ampi:signin");
        lock.setReferenceCounted(false);
    }

    /** The gateway asked for a channel. It only has one at a time, so an older one here is dead: its end comes first. The
     *  limits are this phone's own: a flood must not cost more than this. */
    static synchronized void open(Context c, Config cfg, String ch) {
        if (cfg.signin().length == 0 || !ch.matches("[0-9a-f]{32}")) return;
        long now = SystemClock.elapsedRealtime();
        if (current != null) {
            if (next == null) current.closeRaw();  // (once)
            next = () -> open(c, cfg, ch);
            nextUntil = now + 8_000;  // the gateway waits 12 s
            return;
        }
        while (!opened.isEmpty() && opened.peekFirst() <= now - 3600_000L) opened.removeFirst();
        if (opened.size() >= PER_HOUR || opened.stream().filter(t -> t > now - 60_000L).count() >= PER_MINUTE) return;
        opened.addLast(now);
        VaultSession s = new VaultSession(c, cfg, ch);
        try {
            current = s;
            s.lock.acquire(SETUP);  // before the caller lets the phone sleep again
            new Thread(s, "ampi-signin").start();
        } catch (Throwable e) {  // (no thread to be had)
            current = null;
            Log.e("Ampi", "sign-in channel", e);
            if (s.lock.isHeld()) s.lock.release();
        }
    }

    /** Unpaired: whatever is open ends. */
    static synchronized void closeCurrent() {
        next = null;
        if (current != null) current.closeRaw();
    }

    private void closeRaw() {
        try {
            new Thread(() -> {
                try {
                    raw.close();
                    SSLSocket s = outer;
                    if (s != null) s.close();  // its native parts
                } catch (IOException e) {
                    // Closed either way.
                }
            }, "ampi-close").start();
        } catch (Throwable e) {
            Log.e("Ampi", "close", e);
        }
    }

    private boolean stillPaired() {
        Config now = Config.load(c);
        return now != null && now.alias().equals(cfg.alias()) && Arrays.equals(now.signin(), cfg.signin());
    }

    @Override
    public void run() {
        main.postDelayed(end, SETUP);  // a timeout per read isn't one for all of it
        try {
            SSLSocket s = Link.connect(cfg, raw);
            outer = s;
            s.setSoTimeout((int) SETUP);
            Link.welcome(s);
            Frames.write(s.getOutputStream(), new JSONObject().put("t", "vault").put("ch", ch).toString());
            tls = new Tls(Keys.context(cfg.alias(), cfg.signin()), s.getInputStream(), s.getOutputStream());
            if (!Arrays.equals(tls.peer(), cfg.signin())) throw new IOException("another sign-in service");
            long proved = SystemClock.elapsedRealtime();
            s.setSoTimeout((int) REQUEST);
            String f = Frames.read(tls.input(), MAX_FRAME);
            if (f == null) return;
            if (SystemClock.elapsedRealtime() - proved > REQUEST) throw new IOException("a request held back on the way");
            lock.acquire(LIFE);
            main.removeCallbacks(end);
            main.postDelayed(end, LIFE);
            s.setSoTimeout((int) LIFE);
            handle(new JSONObject(f));
            // Nothing more is asked on this channel; its end means nobody is waiting any more.
            if (Frames.read(tls.input(), MAX_FRAME) != null) throw new IOException("a second request");
        } catch (IOException | GeneralSecurityException | JSONException e) {
            Log.i("Ampi", "sign-in channel: " + e);
        } catch (Throwable e) {  // a broken message must not end the app (an alarm may be ringing)
            Log.e("Ampi", "sign-in channel", e);
        } finally {
            main.removeCallbacks(end);
            try {
                if (ask != null) ask.end("interrupted");  // nobody is there to be answered: the screen goes away
                if (tls != null) {
                    synchronized (writing) {
                        tls.close();
                    }
                }
            } finally {
                closeRaw();
                synchronized (VaultSession.class) {
                    if (current == this) current = null;
                    Runnable n = next;
                    next = null;
                    if (n != null && SystemClock.elapsedRealtime() < nextUntil) n.run();
                }
                if (lock.isHeld()) lock.release();
            }
        }
    }

    private void handle(JSONObject m) {
        String host = m.optString("host");
        String purpose = m.optString("purpose");
        if (m.optInt("v") != 1 || !"get".equals(m.optString("op")) || !(purpose.equals("test") || purpose.equals("fill"))) {
            send(Unlock.result("unsupported"));
        } else if (!Hosts.valid(host)) {
            send(Unlock.result("bad_request"));
        } else {
            int wait = Math.max(5, Math.min(MAX_WAIT, m.optInt("wait", MAX_WAIT)));
            ask = Unlock.request(c, host, purpose.equals("fill"), wait * 1000L, cfg.signin(), this::stillPaired, this::send);
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
