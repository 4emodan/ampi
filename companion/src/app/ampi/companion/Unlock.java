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

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
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
    private static final int PER_10_MIN = 6, PER_DAY = 30, CODES = 3;
    private static final long CODE_WINDOW = 10 * 60_000;
    private static final long[] PAUSE = {60_000, 5 * 60_000, 30 * 60_000};  // after one refusal, two in a row, more

    private static final Handler main = new Handler(Looper.getMainLooper());
    private static Ask current;
    private static Runnable watcher;
    private static long refused;  // elapsedRealtime of the last refusal
    private static int refusals;  // in a row
    private static Last last;

    private Unlock() {}

    /** TEST: nothing of the login is sent. FILL: it goes to the sign-in service. CODE: the one-time code a site asks for after
     *  that. FILL_CODE: both at once. SESSION: the session the owner gave from Firefox. */
    enum Kind { TEST, FILL, CODE, FILL_CODE, SESSION }

    /** The login that was sent last: codes are only for it, for a few minutes (guarded by Unlock.class). */
    private static final class Last {
        final Logins.Login login;
        final long at = SystemClock.elapsedRealtime();
        int codes;
        long window = -1;  // of the last code worked out here: the same one twice is no good

        Last(Logins.Login login) {
            this.login = login;
        }
    }

    /** One request. What it is about never changes. */
    static final class Ask {
        final String host, nonce;
        final Kind kind;
        final List<Logins.Login> choices;
        final SessionBox box;  // of a SESSION
        final long deadline;  // elapsedRealtime
        private final Context app;
        private final BooleanSupplier paired;
        private final Consumer<JSONObject> send;
        private boolean over;  // guarded by Unlock.class

        private Ask(Context c, String host, Kind kind, List<Logins.Login> choices, SessionBox box, long deadline, BooleanSupplier paired,
                    Consumer<JSONObject> send) {
            this.box = box;
            this.app = c.getApplicationContext();
            this.host = host;
            this.kind = kind;
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

        /** The fingerprint was given and the login opened. Nothing of it is in an answer unless this decided the request,
         *  in time, for a login that is still saved and the server it is paired with. `typed`: the code, where the owner
         *  typed one instead of having it worked out here. */
        void unlocked(Logins.Login l, byte[] plain, String typed) {
            String no = left() <= 0 ? "expired" : paired.getAsBoolean() && stillSaved(l) ? null : "interrupted";
            if (!end()) return;
            if (no != null || kind == Kind.TEST) {
                if (no == null) calm();
                answer(result(no));
                return;
            }
            try {
                JSONObject p = new JSONObject(new String(plain, StandardCharsets.UTF_8));
                JSONObject o = result(null).put("hosts", new JSONArray(l.hosts()));
                if (kind != Kind.CODE) o.put("user", p.getString("user")).put("secret", p.getString("secret"));
                Totp totp = kind == Kind.FILL || typed != null ? null : Totp.parse(p.getString("totp"));
                if (typed != null && kind != Kind.FILL) o.put("code", typed);
                calm();
                Last now;
                synchronized (Unlock.class) {
                    if (kind != Kind.CODE) last = new Last(l);
                    now = last;
                    if (kind != Kind.FILL && now != null) now.codes++;
                }
                if (totp == null) answer(o);
                else new Thread(() -> workOut(o, totp, now), "ampi-code").start();
            } catch (JSONException | IllegalArgumentException e) {
                answer(result("failed"));
            }
        }

        /** The fingerprint was given and the session opened: sent once, then it is gone here. */
        void given(byte[] plain) {
            if (kind != Kind.SESSION) return;
            String no = left() <= 0 ? "expired" : paired.getAsBoolean() ? null : "interrupted";
            if (!end()) return;
            try {
                JSONObject o = Handover.contents(plain, box);
                if (no == null && !Handover.spend(app, box)) no = "interrupted";
                if (no == null) calm();
                answer(no != null ? result(no) : result(null).put("host", o.get("host")).put("path", o.get("path")).put("cookies", o.get("cookies")));
            } catch (JSONException e) {
                answer(result("failed"));
            }
        }

        /** The code of now; a moment later where this one is about to end or was used already. */
        private void workOut(JSONObject o, Totp totp, Last of) {
            try {
                long now = System.currentTimeMillis() / 1000, w = totp.window(now);
                boolean used = of != null && of.window == w;
                if (used || (w + 1) * totp.period - now < 4) {
                    long wait = Math.max(0, (w + 1) * totp.period * 1000 - System.currentTimeMillis()) + 300;
                    if (wait > Math.max(0, left()) + 10_000) {  // nobody waits that long any more
                        send.accept(result("expired"));
                        return;
                    }
                    Thread.sleep(wait);
                    w = totp.window(System.currentTimeMillis() / 1000);
                }
                if (of != null) of.window = w;
                send.accept(o.put("code", totp.code(w)));
            } catch (GeneralSecurityException | JSONException | InterruptedException e) {
                Log.e("Ampi", "code", e);
                send.accept(result("failed"));
            } finally {
                totp.wipe();
            }
        }

        /** The owner typed the code (no login is opened for that). */
        void typed(Logins.Login l, String code) {
            String no = left() <= 0 ? "expired" : paired.getAsBoolean() && stillSaved(l) ? null : "interrupted";
            if (kind != Kind.CODE || !end()) return;
            try {
                if (no == null) {
                    calm();
                    synchronized (Unlock.class) {
                        if (last != null) last.codes++;
                    }
                }
                answer(no != null ? result(no) : result(null).put("code", code).put("hosts", new JSONArray(l.hosts())));
            } catch (JSONException e) {
                answer(result("failed"));
            }
        }

        private boolean stillSaved(Logins.Login l) {
            try {
                for (Logins.Login x : Logins.all(app)) if (x.id().equals(l.id())) return true;
            } catch (IOException e) {
                Log.e("Ampi", "logins", e);
            }
            return false;
        }

        void refuse() {
            synchronized (Unlock.class) {
                if (!over) {
                    refused = SystemClock.elapsedRealtime();
                    refusals++;
                    last = null;  // and no more codes for it
                }
            }
            end("denied");
        }

        void end(String why) {
            if (end()) answer(result(why));
        }

        private void answer(JSONObject o) {
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

    private static synchronized void calm() {
        refusals = 0;
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
    static Ask request(Context c, String host, Kind kind, long waitMs, byte[] signin, BooleanSupplier paired,
                       Consumer<JSONObject> send) {
        Ask ask = null;
        String no;
        try {
            List<Logins.Login> choices = new ArrayList<>();
            SessionBox box = Handover.current();
            if (box != null && !box.site().equals(host)) box = null;
            no = whyNot(c, host, kind, box, Logins.serverId(signin), choices);
            if (no == null && !paired.getAsBoolean()) no = "interrupted";
            if (no == null) {
                synchronized (Unlock.class) {
                    if (current != null) no = "busy";
                    else current = ask = new Ask(c, host, kind, List.copyOf(choices), kind == Kind.SESSION ? box : null, SystemClock.elapsedRealtime() + waitMs, paired, send);
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
    private static String whyNot(Context c, String host, Kind kind, SessionBox box, String server, List<Logins.Login> choices)
            throws IOException, GeneralSecurityException, Logins.Gone {
        synchronized (Unlock.class) {
            if (current != null) return "busy";
            if (refusals > 0 && SystemClock.elapsedRealtime() - refused < PAUSE[Math.min(refusals, PAUSE.length) - 1]) return "paused";
        }
        if (!RingService.texts().isEmpty()) return "ringing";
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (!nm.areNotificationsEnabled() || Notes.off(c, App.SIGNIN)) return "notifications_off";
        if (!Logins.fingerprintReady(c)) return "no_fingerprint";
        if (kind == Kind.SESSION) {
            if (!allowed(c)) return "limit";
            if (box == null) {
                // Asking for sites to find out which one was given doesn't go unnoticed.
                Notes.change(c, "signin-missing", "Ampi asked for a session you didn't give",
                        "If you didn't expect that, ask Ampi what it was for.", null, null);
                return "no_session";
            }
            try {
                Handover.toOpen(box);  // is the key still there?
            } catch (Logins.Gone e) {
                return "session_key_gone";
            }
            return null;
        }
        List<Logins.Login> all = Logins.all(c);
        if (!all.isEmpty()) Logins.toOpen(all.get(0));  // is the key still there?
        if (!allowed(c)) return "limit";
        if (kind == Kind.CODE) {
            // Only for the login that was just used, whatever site asks.
            synchronized (Unlock.class) {
                if (last == null || SystemClock.elapsedRealtime() - last.at > CODE_WINDOW || last.codes >= CODES
                        || !last.login.hosts().contains(host) || !last.login.server().equals(server)) return "no_signin";
                choices.add(last.login);
            }
            return null;
        }
        if (!Hosts.refused(host)) for (Logins.Login l : all) if (l.hosts().contains(host) && l.server().equals(server)) choices.add(l);
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
                .setContentTitle(a.kind == Kind.CODE ? "A sign-in asks for a code" : a.kind == Kind.SESSION ? "Ampi wants to use your session"
                        : "Ampi wants to use a saved login")
                .setContentText("Tap to see which one")
                .setContentIntent(open).setAutoCancel(false).setOnlyAlertOnce(true).setLocalOnly(true)
                .setTimeoutAfter(Math.max(1000, a.left())).setVisibility(Notification.VISIBILITY_PRIVATE)
                .setAllowSystemGeneratedContextualActions(false);
        Notes.grouped(c, App.SIGNIN, b, R.drawable.ic_ampi);  // never bundled with a ringing alarm
        nm.notify(a.nonce, NOTE, b.build());
        return Notes.appeared(nm, a.nonce);
    }
}
