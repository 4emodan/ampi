package app.ampi.companion;

import android.app.Application;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.media.AudioAttributes;
import android.provider.Settings;

/** Runs before the first unlock too (alarms), so it must not touch the normal (credential-encrypted) storage. */
public final class App extends Application {
    static final String AMPI = "ampi", CHANGES = "changes", ALARMS = "alarms", BACKUP = "alarms_backup", LINK = "link";

    @Override
    public void onCreate() {
        super.onCreate();
        channels(this);
    }

    static void channels(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        make(nm, AMPI, "Messages from Ampi", NotificationManager.IMPORTANCE_DEFAULT, null);
        make(nm, CHANGES, "Alarm changes by Ampi", NotificationManager.IMPORTANCE_DEFAULT, null);
        NotificationChannel alarms = make(nm, ALARMS, "Ringing alarms", NotificationManager.IMPORTANCE_HIGH, null);
        alarms.setSound(null, null);  // RingService plays the sound
        alarms.enableVibration(false);
        nm.createNotificationChannel(alarms);
        NotificationChannel backup = make(nm, BACKUP, "Alarms (if the alarm screen can't start)",
                NotificationManager.IMPORTANCE_HIGH, null);
        backup.setSound(Settings.System.DEFAULT_ALARM_ALERT_URI, new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        nm.createNotificationChannel(backup);
        make(nm, LINK, "Connection", NotificationManager.IMPORTANCE_LOW, "Shows that the app keeps its connection open");
    }

    private static NotificationChannel make(NotificationManager nm, String id, String name, int importance, String about) {
        NotificationChannel ch = new NotificationChannel(id, name, importance);
        if (about != null) ch.setDescription(about);
        boolean quiet = id.equals(LINK);
        ch.setShowBadge(!quiet);
        ch.setLockscreenVisibility(quiet ? Notification.VISIBILITY_SECRET : Notification.VISIBILITY_PRIVATE);
        if (!id.equals(ALARMS) && !id.equals(BACKUP)) nm.createNotificationChannel(ch);
        return ch;
    }
}
