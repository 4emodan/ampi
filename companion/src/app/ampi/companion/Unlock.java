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
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.security.GeneralSecurityException;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Consumer;

/**
 * The one sign-in request the owner can be shown. Whatever happens to it first (fingerprint, Refuse, its time, the channel, an
 * alarm) decides; nothing after that does anything.
 */
final class Unlock {
    private static final int NOTE = 3;
    private static final int PER_10_MIN = 6, PER_DAY = 30, UNKNOWN_PER_HOUR = 10;
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
        private final Consumer<JSONObject> send;
        private boolean over;  // guarded by Unlock.class

        private Ask(Context c, String host, List<Logins.Login> choices, long deadline, Consumer<JSONObject> send) {
            this.app = c.getApplicationContext();
            this.host = host;
            this.nonce = Logins.newId();
            this.choices = choices;
            this.deadline = deadline;
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
            app.getSystemService(NotificationManager.class).cancel(nonce, NOTE);
            main.post(() -> {
                if (watcher != null) watcher.run();
            });
            return true;
        }

        /** The fingerprint was given and the login opened. */
        void unlocked() {
            boolean late = left() <= 0;
            if (end()) answer(late ? "expired" : null);
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

    static synchronized Ask current() {
        return current;
    }

    /** The screen wants to know when the request is over (main thread). */
    static void watch(Runnable r) {
        watcher = r;
    }

    /** Something else needs the owner now. */
    static void interrupt() {
        Ask a = current();
        if (a != null) a.end("interrupted");
    }

    /** Shows a request: answers why not, or "asking" and later the outcome. Not on the main thread. */
    static Ask request(Context c, String host, long waitMs, byte[] signin, Consumer<JSONObject> send) {
        Ask ask = null;
        String no;
        try {
            List<Logins.Login> choices = new ArrayList<>();
            for (Logins.Login l : Logins.all(c)) if (l.hosts().contains(host)) choices.add(l);
            no = whyNot(c, choices, Logins.serverId(signin));
            if (no == null) {
                synchronized (Unlock.class) {
                    if (current != null) no = "busy";
                    else current = ask = new Ask(c, host, List.copyOf(choices), SystemClock.elapsedRealtime() + waitMs, send);
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
        if (!RingService.texts().isEmpty()) {  // it started ringing just now
            a.end("interrupted");
        } else if (!show(c, a)) {
            a.end("notifications_off");
        } else {
            main.postDelayed(() -> a.end("expired"), Math.max(0, a.left()));
            try {
                send.accept(new JSONObject().put("asking", true));
            } catch (JSONException e) {
                throw new IllegalStateException(e);
            }
        }
        return a;
    }

    private static String whyNot(Context c, List<Logins.Login> choices, String server) throws GeneralSecurityException, Logins.Gone {
        synchronized (Unlock.class) {
            if (current != null) return "busy";
            if (SystemClock.elapsedRealtime() - refused < PAUSE) return "paused";
        }
        if (choices.isEmpty()) return allowed(c, "unknown", 3600_000L, UNKNOWN_PER_HOUR) ? "no_login" : "limit";
        choices.removeIf(l -> !l.server().equals(server));
        if (choices.isEmpty()) return "other_server";
        if (!RingService.texts().isEmpty()) return "ringing";
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (!nm.areNotificationsEnabled() || Notes.off(c, App.SIGNIN)) return "notifications_off";
        if (!Logins.fingerprintReady(c)) return "no_fingerprint";
        Logins.toOpen(choices.get(0));  // is the key still there?
        if (!allowed(c, "asked", 600_000L, PER_10_MIN) || !allowed(c, "asked_day", 24 * 3600_000L, PER_DAY)) return "limit";
        return null;
    }

    /** Counts it, unless there were enough in that time. */
    private static synchronized boolean allowed(Context c, String what, long time, int most) {
        SharedPreferences p = c.getSharedPreferences("signin", Context.MODE_PRIVATE);
        long now = System.currentTimeMillis();
        var kept = new StringBuilder();
        int n = 0;
        for (String s : p.getString(what, "").split(",")) {
            if (s.isEmpty()) continue;
            long t = Long.parseLong(s);
            if (t > now - time && t <= now) {
                kept.append(s).append(',');
                n++;
            }
        }
        if (n >= most) return false;
        p.edit().putString(what, kept.append(now).append(',').toString()).apply();
        return true;
    }

    /** Which site it is about is only on the screen it opens: others may see a notification. */
    private static boolean show(Context c, Ask a) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        PendingIntent open = PendingIntent.getActivity(c, 1, new Intent(c, UnlockActivity.class), PendingIntent.FLAG_IMMUTABLE);
        nm.notify(a.nonce, NOTE, new Notification.Builder(c, App.SIGNIN).setSmallIcon(R.drawable.ic_ampi)
                .setContentTitle("Ampi wants to use a saved login").setContentText("Tap to see which one")
                .setContentIntent(open).setAutoCancel(false).setOnlyAlertOnce(true).setLocalOnly(true)
                .setTimeoutAfter(Math.max(1000, a.left())).setVisibility(Notification.VISIBILITY_PRIVATE)
                .setGroup(App.SIGNIN).setAllowSystemGeneratedContextualActions(false).build());
        return Notes.appeared(nm, a.nonce);
    }
}
