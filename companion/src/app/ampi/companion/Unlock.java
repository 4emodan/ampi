package app.ampi.companion;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.service.notification.StatusBarNotification;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.security.GeneralSecurityException;
import java.util.ArrayList;
import java.util.List;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

/**
 * The one sign-in request the owner can be shown. Whatever happens to it first (fingerprint, Refuse, its time, the channel, an
 * alarm) decides; nothing after that does anything.
 */
final class Unlock {
    private static final int NOTE = 3;
    private static final int PER_10_MIN = 6, PER_DAY = 30;
    private static final long PAUSE = 60_000;

    private static final Handler main = new Handler(Looper.getMainLooper());
    private static Ask current;
    private static Runnable watcher;
    private static long refused = -PAUSE;  // elapsedRealtime

    private Unlock() {}

    /** One request. What it is about never changes. */
    static final class Ask {
        final String host, nonce;
        final List<Logins.Login> choices;
        final long deadline;  // elapsedRealtime
        private final Context app;
        private final BooleanSupplier paired;
        private final Consumer<JSONObject> send;
        private boolean over;  // guarded by Unlock.class

        private Ask(Context c, String host, List<Logins.Login> choices, long deadline, BooleanSupplier paired, Consumer<JSONObject> send) {
            this.app = c.getApplicationContext();
            this.host = host;
            this.nonce = Logins.newId();
            this.choices = choices;
            this.deadline = deadline;
            this.paired = paired;
            this.send = send;
        }

        long left() {
            return deadline - SystemClock.elapsedRealtime();
        }

        boolean live() {
            synchronized (Unlock.class) {
                return !over && left() > 0;
            }
        }

        /** True when this decided it. */
        private boolean end() {
            synchronized (Unlock.class) {
                if (over) return false;
                over = true;
                if (current == this) current = null;
            }
            hide();
            main.post(() -> {
                if (watcher != null) watcher.run();
            });
            return true;
        }

        private void hide() {
            try {
                NotificationManager nm = app.getSystemService(NotificationManager.class);
                nm.cancel(nonce, NOTE);
                nm.cancel(App.SIGNIN, Notes.SUMMARY);
            } catch (RuntimeException e) {
                Log.e("Ampi", "sign-in notice", e);
            }
        }

        /** The fingerprint was given and the login opened. */
        void unlocked() {
            String no = left() <= 0 ? "expired" : paired.getAsBoolean() ? null : "interrupted";
            if (end()) answer(no);
        }

        void refuse() {
            synchronized (Unlock.class) {
                if (!over) refused = SystemClock.elapsedRealtime();
            }
            end("denied");
        }

        void end(String why) {
            if (end()) answer(why);
        }

        private void answer(String error) {
            JSONObject o = result(error);
            new Thread(() -> send.accept(o), "ampi-answer").start();  // the network: not on the main thread
        }
    }

    static JSONObject result(String error) {
        try {
            return error == null ? new JSONObject().put("ok", true) : new JSONObject().put("ok", false).put("error", error);
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
    }

    /** The one the owner can still decide, or null. */
    static Ask current() {
        Ask a;
        synchronized (Unlock.class) {
            a = current;
        }
        return a != null && a.live() ? a : null;
    }

    /** At the start: a request that was open when the app was killed is gone, its notification too. */
    static void clean(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        for (StatusBarNotification n : nm.getActiveNotifications()) if (n.getId() == NOTE) nm.cancel(n.getTag(), NOTE);
        nm.cancel(App.SIGNIN, Notes.SUMMARY);
    }

    /** The screen that is there wants to know when the request is over (main thread). */
    static void watch(Runnable r) {
        watcher = r;
    }

    static void unwatch(Runnable r) {
        if (watcher == r) watcher = null;
    }

    /** Something else needs the owner now. */
    static void interrupt() {
        Ask a;
        synchronized (Unlock.class) {
            a = current;
        }
        if (a != null) a.end("interrupted");
    }

    /** Shows a request: answers why not, or "asking" and later the outcome. Not on the main thread. */
    static Ask request(Context c, String host, long waitMs, byte[] signin, BooleanSupplier paired, Consumer<JSONObject> send) {
        Ask ask = null;
        String no;
        try {
            List<Logins.Login> choices = new ArrayList<>();
            no = whyNot(c, host, Logins.serverId(signin), choices);
            if (no == null && !paired.getAsBoolean()) no = "interrupted";
            if (no == null) {
                synchronized (Unlock.class) {
                    if (current != null) no = "busy";
                    else current = ask = new Ask(c, host, List.copyOf(choices), SystemClock.elapsedRealtime() + waitMs, paired, send);
                }
            }
        } catch (Logins.Gone e) {
            no = "key_gone";
        } catch (IOException | GeneralSecurityException | RuntimeException e) {
            Log.e("Ampi", "sign-in request", e);
            no = "failed";
        }
        if (no != null) {
            send.accept(result(no));
            return null;
        }
        Ask a = ask;
        try {
            main.postDelayed(() -> a.end("expired"), Math.max(0, a.left()));
            if (!RingService.texts().isEmpty()) {  // it started ringing just now
                a.end("interrupted");
            } else if (!show(c, a)) {
                a.end("notifications_off");
            } else if (!a.live()) {  // it was over before its notification was there
                a.hide();
            } else {
                send.accept(new JSONObject().put("asking", true));
            }
        } catch (JSONException | RuntimeException e) {
            Log.e("Ampi", "sign-in request", e);
            a.end("failed");
        }
        return a;
    }

    /** First what doesn't depend on the site, and one limit for all sites: the answers must not tell which ones are saved
     *  without the owner being shown something. */
    private static String whyNot(Context c, String host, String server, List<Logins.Login> choices)
            throws IOException, GeneralSecurityException, Logins.Gone {
        synchronized (Unlock.class) {
            if (current != null) return "busy";
            if (SystemClock.elapsedRealtime() - refused < PAUSE) return "paused";
        }
        if (!RingService.texts().isEmpty()) return "ringing";
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (!nm.areNotificationsEnabled() || Notes.off(c, App.SIGNIN)) return "notifications_off";
        if (!Logins.fingerprintReady(c)) return "no_fingerprint";
        List<Logins.Login> all = Logins.all(c);
        if (!all.isEmpty()) Logins.toOpen(all.get(0));  // is the key still there?
        if (!allowed(c)) return "limit";
        for (Logins.Login l : all) if (l.hosts().contains(host) && l.server().equals(server)) choices.add(l);
        if (choices.isEmpty()) {
            // Asking for sites to find out which ones are saved doesn't go unnoticed.
            Notes.change(c, "signin-missing", "Ampi asked for a login that isn't saved",
                    "If you didn't expect that, ask Ampi what it was for.", null, null);
            return "no_login";
        }
        return null;
    }

    /** Counts it, unless there were enough lately. */
    private static synchronized boolean allowed(Context c) {
        SharedPreferences p = c.getSharedPreferences("signin", Context.MODE_PRIVATE);
        long now = System.currentTimeMillis();
        var kept = new StringBuilder();
        int day = 0, lately = 0;
        for (String s : p.getString("asked", "").split(",")) {
            long t;
            try {
                t = Long.parseLong(s);
            } catch (NumberFormatException e) {
                continue;
            }
            if (Math.abs(now - t) >= 24 * 3600_000L) continue;
            kept.append(t).append(',');
            day++;
            if (Math.abs(now - t) < 600_000L) lately++;
        }
        if (lately >= PER_10_MIN || day >= PER_DAY) return false;
        p.edit().putString("asked", kept.append(now).append(',').toString()).apply();
        return true;
    }

    /** Which site it is about is only on the screen it opens: others may see a notification. */
    private static boolean show(Context c, Ask a) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        PendingIntent open = PendingIntent.getActivity(c, 1, new Intent(c, UnlockActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(c, App.SIGNIN).setSmallIcon(R.drawable.ic_ampi)
                .setContentTitle("Ampi wants to use a saved login").setContentText("Tap to see which one")
                .setContentIntent(open).setAutoCancel(false).setOnlyAlertOnce(true).setLocalOnly(true)
                .setTimeoutAfter(Math.max(1000, a.left())).setVisibility(Notification.VISIBILITY_PRIVATE)
                .setAllowSystemGeneratedContextualActions(false);
        Notes.grouped(c, App.SIGNIN, b, R.drawable.ic_ampi);  // never bundled with a ringing alarm
        nm.notify(a.nonce, NOTE, b.build());
        return Notes.appeared(nm, a.nonce);
    }
}
