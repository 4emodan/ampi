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
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.Map;

/** Rings: sound on the alarm channel, vibration, and the alarm screen over the lock screen. Ends itself after 10 min. */
public final class RingService extends Service {
    private static final String RING = "ring", STOP = "stop", SNOOZE = "snooze", ID = "id";
    private static final int NOTE = 1;
    private static final long MAX_MS = 10 * 60_000L;
    /** id -> text of what is ringing in this process. Only touched on the main thread. */
    private static final Map<String, String> ringing = new LinkedHashMap<>();
    private static Runnable watcher;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable end = () -> stopAll("missed: nobody stopped it", false, true);
    private MediaPlayer player;
    private ToneGenerator tone;
    private PowerManager.WakeLock lock;

    static void ring(Context c, String id) {
        try {
            c.startForegroundService(new Intent(c, RingService.class).setAction(RING).putExtra(ID, id));
        } catch (RuntimeException e) {
            // Android didn't allow the service: a notification with the alarm sound is better than silence.
            Log.e("Ampi", "ring", e);
            Alarm a = AlarmStore.get(c).find(id);
            Notes.backupAlarm(c, id, a == null ? "Alarm" : a.text());
            Alarms.finished(c, id, "rang as a notification only", false);
        }
    }

    static boolean isRinging(String id) {
        synchronized (ringing) {
            return ringing.containsKey(id);
        }
    }

    static Collection<String> texts() {
        synchronized (ringing) {
            return java.util.List.copyOf(ringing.values());
        }
    }

    /** The alarm screen wants to know when it changes (main thread). */
    static void watch(Runnable r) {
        watcher = r;
    }

    static Intent stop(Context c, boolean snooze) {
        return new Intent(c, RingService.class).setAction(snooze ? SNOOZE : STOP);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (RING.equals(action)) {
            String id = String.valueOf(intent.getStringExtra(ID));
            Alarm a = AlarmStore.get(this).find(id);
            boolean first;
            synchronized (ringing) {
                first = ringing.isEmpty();
                ringing.put(id, a == null ? "Alarm" : a.text());
            }
            show();
            if (first) start();
            handler.removeCallbacks(end);
            handler.postDelayed(end, MAX_MS);
            changed();
            return START_STICKY;
        }
        show();  // a service started as a foreground one must show itself, even to stop
        if (STOP.equals(action)) stopAll("stopped", false, false);
        else if (SNOOZE.equals(action)) stopAll("snoozed", true, false);
        else if (texts().isEmpty()) {
            // Restarted by Android after a crash while ringing: the file says what is due.
            Alarms.rearmAll(this);
            if (texts().isEmpty()) handler.postDelayed(() -> {
                if (texts().isEmpty()) finish();
            }, 2000);
        }
        return START_STICKY;
    }

    private void show() {
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
        startForeground(NOTE, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SYSTEM_EXEMPTED);
    }

    private PendingIntent service(String action) {
        return PendingIntent.getForegroundService(this, 0, new Intent(this, RingService.class).setAction(action),
                PendingIntent.FLAG_IMMUTABLE);
    }

    private void start() {
        lock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ampi:ring");
        lock.acquire(MAX_MS + 30_000);
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
        Vibrator v = getSystemService(VibratorManager.class).getDefaultVibrator();
        v.vibrate(VibrationEffect.createWaveform(new long[] {0, 700, 700}, 0),
                VibrationAttributes.createForUsage(VibrationAttributes.USAGE_ALARM));
    }

    private void beep() {
        if (player != null) {
            player.release();
            player = null;
        }
        if (tone != null) return;
        try {
            tone = new ToneGenerator(AudioManager.STREAM_ALARM, 90);
        } catch (RuntimeException e) {
            Log.e("Ampi", "no tone either", e);
            return;
        }
        handler.post(new Runnable() {
            @Override
            public void run() {
                if (tone == null) return;
                tone.startTone(ToneGenerator.TONE_CDMA_ALERT_CALL_GUARD, 1200);
                handler.postDelayed(this, 1800);
            }
        });
    }

    private void stopAll(String how, boolean snooze, boolean missed) {
        Map<String, String> was;
        synchronized (ringing) {
            was = new LinkedHashMap<>(ringing);
            ringing.clear();
        }
        for (var e : was.entrySet()) {
            Alarms.finished(this, e.getKey(), how, snooze);
            if (missed) Notes.change(this, "missed-" + e.getKey(), "Missed alarm", e.getValue(), null, null);
        }
        finish();
    }

    private void finish() {
        handler.removeCallbacksAndMessages(null);
        if (player != null) player.release();
        if (tone != null) tone.release();
        player = null;
        tone = null;
        getSystemService(VibratorManager.class).getDefaultVibrator().cancel();
        if (lock != null && lock.isHeld()) lock.release();
        stopForeground(STOP_FOREGROUND_REMOVE);
        getSystemService(NotificationManager.class).cancel(NOTE);
        changed();
        stopSelf();
    }

    private static void changed() {
        if (watcher != null) watcher.run();
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        if (player != null) player.release();
        if (tone != null) tone.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
