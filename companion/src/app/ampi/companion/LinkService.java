package app.ampi.companion;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.NoRouteToHostException;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.security.GeneralSecurityException;

import javax.net.ssl.SSLSocket;

/** Keeps the one connection to the paired server: a foreground service with one thread. */
public final class LinkService extends Service {
    private static final int NOTE = 2;
    private static final String CHECK = "check";
    private static final long WATCHDOG = 15 * 60_000L, SILENT = 4 * 60_000L, BACKOFF_MIN = 5_000, BACKOFF_MAX = 15 * 60_000L;

    /** For the status screen. */
    static volatile long connectedSince, lastContact;  // wall clock, 0 = not connected / never
    static volatile String problem = "";

    private static volatile LinkService running;
    private static PowerManager.WakeLock awake;

    private final Object wake = new Object();
    private boolean nudged;
    private volatile boolean stopping;
    private volatile Socket socket;
    private volatile long lastFrame;  // elapsedRealtime
    private volatile long welcomes;
    private Thread thread;
    private PowerManager.WakeLock lock;
    private volatile Network used;
    private final ConnectivityManager.NetworkCallback networks = new ConnectivityManager.NetworkCallback() {
        @Override
        public void onAvailable(Network n) {
            // The default network changed: a connection over another one is dead or about to be.
            if (!n.equals(used)) reconnect();
        }

        @Override
        public void onLost(Network n) {
            if (n.equals(used)) close();
        }
    };

    static void start(Context c, boolean check) {
        if (Config.load(c) == null) return;
        try {
            c.startForegroundService(new Intent(c, LinkService.class).setAction(check ? CHECK : null));
        } catch (RuntimeException e) {
            // Not allowed from the background without the battery exemption; the status screen says what to do.
            Log.w("Ampi", "can't start the connection now", e);
            problem = "Android didn't let the connection start in the background";
        }
        watchdog(c);
    }

    /** Until the service has started its thread or reconnects (the phone may otherwise sleep first). */
    static synchronized void stayAwake(Context c) {
        if (awake == null) {
            awake = c.getApplicationContext().getSystemService(PowerManager.class)
                    .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ampi:check");
            awake.setReferenceCounted(false);
        }
        awake.acquire(45_000);
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, LinkService.class));
        c.getSystemService(AlarmManager.class).cancel(watchdogIntent(c));
    }

    private static PendingIntent watchdogIntent(Context c) {
        return PendingIntent.getBroadcast(c, 0, new Intent(c, LinkReceiver.class), PendingIntent.FLAG_IMMUTABLE);
    }

    private static void watchdog(Context c) {
        c.getSystemService(AlarmManager.class).setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP,
                SystemClock.elapsedRealtime() + WATCHDOG, watchdogIntent(c));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        running = this;
        lock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ampi:link");
        lock.setReferenceCounted(false);
        getSystemService(ConnectivityManager.class).registerDefaultNetworkCallback(networks);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!show() || Config.load(this) == null) {
            stopSelf(startId);
            return START_NOT_STICKY;
        }
        // Reconnecting must not wait for the next time the phone wakes up.
        if (thread == null || !thread.isAlive()) {
            stayAwake(this);
            thread = new Thread(this::run, "ampi-link");
            thread.start();
        } else if (socket != null && SystemClock.elapsedRealtime() - lastFrame > SILENT) {
            stayAwake(this);
            reconnect();
        } else if (socket == null) {
            stayAwake(this);
            nudge();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopping = true;
        if (running == this) {
            running = null;
            connectedSince = 0;
        }
        getSystemService(ConnectivityManager.class).unregisterNetworkCallback(networks);
        close();
        nudge();
        if (lock.isHeld()) lock.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void close() {
        Socket s = socket;
        if (s == null) return;
        // Off the main thread: closing a TLS socket can write.
        new Thread(() -> {
            try {
                s.close();
            } catch (IOException e) {
                // Closed either way.
            }
        }, "ampi-close").start();
    }

    private void reconnect() {
        close();
        nudge();
    }

    private void nudge() {
        synchronized (wake) {
            nudged = true;
            wake.notifyAll();
        }
    }

    // --- the thread ------------------------------------------------------------------------------------

    private void run() {
        long backoff = BACKOFF_MIN;
        while (!stopping) {
            Config cfg = Config.load(this);
            if (cfg == null) break;
            long before = welcomes;
            try {
                session(cfg);
                problem = "the server closed the connection";
            } catch (GeneralSecurityException e) {
                problem = text(e);
            } catch (ConnectException | SocketTimeoutException | NoRouteToHostException e) {
                problem = "can't reach the server";
            } catch (IOException | JSONException | RuntimeException | StackOverflowError e) {
                problem = text(e);
                Log.i("Ampi", "connection: " + e);
            } finally {
                socket = null;
                if (running == this) connectedSince = 0;
            }
            if (stopping) break;
            if (welcomes != before) backoff = BACKOFF_MIN;  // only a real connection resets it
            show();
            synchronized (wake) {
                try {
                    if (!nudged) wake.wait(backoff);
                } catch (InterruptedException e) {
                    break;
                }
                if (nudged) backoff = Math.min(backoff, 2 * BACKOFF_MIN);
                nudged = false;
            }
            backoff = Math.min(backoff * 2, BACKOFF_MAX);
        }
    }

    private static String text(Throwable e) {
        return e.getMessage() == null ? e.getClass().getSimpleName() : Texts.line(e.getMessage(), 200);
    }

    private void session(Config cfg) throws IOException, GeneralSecurityException, JSONException {
        lastFrame = SystemClock.elapsedRealtime();
        used = getSystemService(ConnectivityManager.class).getActiveNetwork();
        Socket raw = new Socket();
        socket = raw;
        try {
            raw.connect(new InetSocketAddress(cfg.host(), cfg.port()), 15_000);
            raw.setTcpNoDelay(true);
            SSLSocket s = (SSLSocket) Keys.context(cfg.alias(), cfg.server()).getSocketFactory()
                    .createSocket(raw, cfg.host(), cfg.port(), true);
            socket = s;
            s.setEnabledProtocols(new String[] {"TLSv1.3"});
            s.setSoTimeout(20_000);
            s.startHandshake();
            talk(s);
        } finally {
            Socket s = socket;
            try {
                if (s != null) s.close();
                raw.close();
            } catch (IOException e) {
                // Closed either way.
            }
        }
    }

    private void talk(SSLSocket s) throws IOException, JSONException {
        InputStream in = s.getInputStream();
        OutputStream out = s.getOutputStream();
        // TLS 1.3: the server's refusal of this phone only shows at the first read, so nothing counts before "welcome".
        JSONObject w = read(in);
        if (w == null || !"welcome".equals(w.optString("t"))) throw new IOException("the server doesn't accept this phone");
        if (w.optInt("proto") != 1) throw new IOException("the server speaks another version: update the app");
        Clock clock = new Clock(w.optDouble("now", 0));
        Frames.write(out, hello().toString());
        s.setSoTimeout(400_000);
        welcomes++;
        problem = "";
        connectedSince = lastContact = System.currentTimeMillis();
        show();
        while (!stopping) {
            JSONObject m = read(in);
            if (m == null) return;
            lock.acquire(30_000);
            try {
                lastContact = System.currentTimeMillis();
                switch (m.optString("t")) {
                    case "ping" -> {
                        clock = clock.ahead(m.optDouble("now", 0));
                        Frames.write(out, "{\"t\":\"pong\"}");
                    }
                    case "req" -> {
                        clock = clock.ahead(m.optDouble("now", 0));
                        Frames.write(out, Requests.handle(this, m, clock.now()).toString());
                    }
                    default -> { }  // later versions' messages (logins) are never handled here
                }
            } finally {
                lock.release();
            }
        }
    }

    private JSONObject read(InputStream in) throws IOException {
        String f = Frames.read(in);
        lastFrame = SystemClock.elapsedRealtime();
        if (f == null) return null;
        try {
            return new JSONObject(f);
        } catch (JSONException e) {
            throw new IOException("bad message");
        }
    }

    /** The server's clock, carried on by this phone's monotonic one: the phone's own wall clock may be wrong. */
    private record Clock(double at, long since) {
        Clock(double at) {
            this(at, SystemClock.elapsedRealtime());
        }

        /** A message held up on the way says an old time: whichever is further ahead is nearer the truth. */
        Clock ahead(double at) {
            return at > now() && at < now() + 3600 ? new Clock(at) : this;
        }

        double now() {
            return at + (SystemClock.elapsedRealtime() - since) / 1000.0;
        }
    }

    private JSONObject hello() throws JSONException {
        JSONArray warn = Alarms.warnings(this);
        if (!Checks.batteryFree(this)) warn.put("battery");
        if (!Checks.keepsPermissions(this)) warn.put("hibernation");
        return new JSONObject().put("t", "hello").put("proto", 1).put("app", Checks.version(this))
                .put("sdk", Build.VERSION.SDK_INT + "." + Build.getMinorSdkVersion(Build.VERSION.SDK_INT_FULL))
                .put("model", Build.MANUFACTURER + " " + Build.MODEL).put("zone", java.time.ZoneId.systemDefault().getId())
                .put("warn", warn);
    }

    private boolean show() {
        if (running != this) return false;
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, App.LINK).setSmallIcon(R.drawable.ic_ampi)
                .setContentTitle(connectedSince != 0 ? "Connected" : "Not connected").setContentIntent(open).setOngoing(true)
                .setShowWhen(false).setLocalOnly(true).setAllowSystemGeneratedContextualActions(false).build();
        try {
            if (Thread.currentThread() == getMainLooper().getThread()) {
                startForeground(NOTE, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                getSystemService(NotificationManager.class).notify(NOTE, n);
            }
            return true;
        } catch (RuntimeException e) {  // not allowed from the background without the battery exemption
            Log.w("Ampi", "can't show the connection", e);
            problem = "Android didn't let the connection start in the background";
            return false;
        }
    }
}
