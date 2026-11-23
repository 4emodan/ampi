package app.ampi.companion;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.media.ToneGenerator;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.VibrationAttributes;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.provider.Settings;
import android.util.Log;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Rings: sound on the alarm channel, vibration, and the alarm screen over the lock screen. Ends itself after 10 min. */
public final class RingService extends Service {
    private static final String RING = "ring", STOP = "stop", SNOOZE = "snooze", SILENCE = "silence", ID = "id", TEXT = "text";
    private static final int NOTE = 1;
    private static final long MAX_MS = 10 * 60_000L;
    /** id -> text of what is ringing in this process. */
    private static final Map<String, String> ringing = new LinkedHashMap<>();
    private static Runnable watcher;
    private static PowerManager.WakeLock awake, whileRinging;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable end = () -> stopAll("missed_unanswered", false, true);
    private final Runnable beeping = this::beepAgain;
    private MediaPlayer player;
    private ToneGenerator tone;
    private int lastStart;

    /** Keeps the processor on until the sound is (the phone may otherwise sleep between the alarm and the service). */
    static synchronized void stayAwake(Context c) {
        if (awake == null) {
            awake = c.getApplicationContext().getSystemService(PowerManager.class)
                    .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ampi:ring");
            awake.setReferenceCounted(false);
        }
        awake.acquire(60_000);
    }

    /** A lock of its own, so that the short one above never cuts it off. */
    private static synchronized void ringAwake(Context c, boolean on) {
        if (whileRinging == null) {
            whileRinging = c.getApplicationContext().getSystemService(PowerManager.class)
                    .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ampi:ringing");
            whileRinging.setReferenceCounted(false);
        }
        if (on) whileRinging.acquire(MAX_MS + 30_000);
        else if (whileRinging.isHeld()) whileRinging.release();
    }

    static void ring(Context c, String id, String text) {
        stayAwake(c);
        try {
            c.startForegroundService(new Intent(c, RingService.class).setAction(RING).putExtra(ID, id).putExtra(TEXT, text));
        } catch (RuntimeException e) {
            Log.e("Ampi", "ring", e);
            fallback(c, id, text);
        }
    }

    /** Android didn't allow the service: a notification with the alarm sound is better than silence. */
    private static void fallback(Context c, String id, String text) {
        synchronized (ringing) {
            ringing.remove(id);
        }
        Notes.backupAlarm(c, id, text == null ? "Alarm" : text);
        Alarms.finished(c, id, "rang_notification_only", false);
    }

    static boolean isRinging(String id) {
        synchronized (ringing) {
            return ringing.containsKey(id);
        }
    }

    static List<String> texts() {
        synchronized (ringing) {
            return List.copyOf(ringing.values());
        }
    }

    /** The alarm screen wants to know when it changes (main thread). */
    static void watch(Runnable r) {
        watcher = r;
    }

    static Intent stop(Context c, boolean snooze) {
        return new Intent(c, RingService.class).setAction(snooze ? SNOOZE : STOP);
    }

    /** The owner deleted a ringing alarm: it is gone from the file already. */
    static void silence(Context c, String id) {
        if (!isRinging(id)) return;
        try {
            c.startForegroundService(new Intent(c, RingService.class).setAction(SILENCE).putExtra(ID, id));
        } catch (RuntimeException e) {
            Log.e("Ampi", "silence", e);  // its Stop button still works
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        lastStart = startId;
        String action = intent == null ? null : intent.getAction();
        String id = intent == null ? null : intent.getStringExtra(ID);
        if (RING.equals(action) && id != null) {
            String text = intent.getStringExtra(TEXT);
            synchronized (ringing) {
                ringing.put(id, text == null ? "Alarm" : text);
            }
            if (!show()) {
                fallback(this, id, text);
                if (texts().isEmpty()) finish();
                return START_NOT_STICKY;
            }
            ringAwake(this, true);
            if (player == null && tone == null) start();
            handler.removeCallbacks(end);
            handler.postDelayed(end, MAX_MS);
            changed();
            return START_STICKY;
        }
        show();  // a service started as a foreground one must show itself, even to stop
        if (STOP.equals(action)) {
            stopAll("stopped", false, false);
        } else if (SNOOZE.equals(action)) {
            stopAll("snoozed", true, false);
        } else if (SILENCE.equals(action)) {
            synchronized (ringing) {
                ringing.remove(id);
            }
            if (texts().isEmpty()) finish();
            else changed();
        } else {
            // Restarted by Android after a crash while ringing: the file says what is due (and starts this again).
            Alarms.rearmAll(this);
            if (texts().isEmpty()) finish();
        }
        return START_STICKY;
    }

    private boolean show() {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, RingActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE);
        String text = String.join("\n", texts());
        Notification n = new Notification.Builder(this, App.ALARMS).setSmallIcon(R.drawable.ic_alarm)
                .setContentTitle("Alarm").setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text))
                .setCategory(Notification.CATEGORY_ALARM).setVisibility(Notification.VISIBILITY_PUBLIC).setOngoing(true)
                .setFullScreenIntent(open, true).setContentIntent(open)
                .setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
                .setAllowSystemGeneratedContextualActions(false)
                .addAction(new Notification.Action.Builder(null, "Snooze 10 min", service(SNOOZE)).build())
                .addAction(new Notification.Action.Builder(null, "Stop", service(STOP)).build()).build();
        try {
            startForeground(NOTE, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SYSTEM_EXEMPTED);
            return true;
        } catch (RuntimeException e) {  // not allowed from the background right now
            Log.e("Ampi", "startForeground", e);
            return false;
        }
    }

    private PendingIntent service(String action) {
        return PendingIntent.getForegroundService(this, 0, new Intent(this, RingService.class).setAction(action),
                PendingIntent.FLAG_IMMUTABLE);
    }

    private void start() {
        AudioAttributes attrs = new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build();
        try {
            player = new MediaPlayer();
            player.setAudioAttributes(attrs);
            player.setDataSource(this, Settings.System.DEFAULT_ALARM_ALERT_URI);
            player.setLooping(true);
            player.setOnErrorListener((p, what, extra) -> {
                beep();
                return true;
            });
            player.prepare();
            player.start();
        } catch (IOException | RuntimeException e) {
            Log.w("Ampi", "no alarm sound, using a tone", e);  // e.g. its file is locked before the first unlock
            beep();
        }
        vibrator().vibrate(VibrationEffect.createWaveform(new long[] {0, 700, 700}, 0),
                VibrationAttributes.createForUsage(VibrationAttributes.USAGE_ALARM));
    }

    private Vibrator vibrator() {
        return getSystemService(VibratorManager.class).getDefaultVibrator();
    }

    private void beep() {
        if (player != null) player.release();
        player = null;
        if (tone != null) return;
        try {
            tone = new ToneGenerator(AudioManager.STREAM_ALARM, 90);
        } catch (RuntimeException e) {
            Log.e("Ampi", "no tone either", e);
            return;
        }
        handler.post(beeping);
    }

    private void beepAgain() {
        if (tone == null) return;
        tone.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, 1200);
        handler.postDelayed(beeping, 1800);
    }

    private void stopAll(String code, boolean snooze, boolean missed) {
        Map<String, String> was;
        synchronized (ringing) {
            was = new LinkedHashMap<>(ringing);
            ringing.clear();
        }
        for (var e : was.entrySet()) {
            Alarms.finished(this, e.getKey(), code, snooze);
            if (missed) Notes.change(this, "missed-" + e.getKey(), "Missed alarm", e.getValue(), null, null);
        }
        finish();
    }

    private void finish() {
        quiet();
        stopForeground(STOP_FOREGROUND_REMOVE);
        getSystemService(NotificationManager.class).cancel(NOTE);
        changed();
        // Only up to the last start this has seen: an alarm that is just starting it again must not be lost.
        stopSelf(lastStart);
    }

    private void quiet() {
        handler.removeCallbacksAndMessages(null);
        if (player != null) player.release();
        if (tone != null) tone.release();
        player = null;
        tone = null;
        vibrator().cancel();
        ringAwake(this, false);
    }

    private static void changed() {
        if (watcher != null) watcher.run();
    }

    @Override
    public void onDestroy() {
        quiet();
        boolean cut;
        synchronized (ringing) {
            cut = !ringing.isEmpty();
            ringing.clear();
        }
        if (cut) Alarms.rearmAll(this);  // ended while ringing, by someone else: ring again, or report it
        changed();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
