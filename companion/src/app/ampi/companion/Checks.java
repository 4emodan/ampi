package app.ampi.companion;

import android.Manifest;
import android.app.Activity;
import android.app.NotificationManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.PowerManager;
import android.provider.Settings;

import java.util.ArrayList;
import java.util.List;

/** What is missing on this phone, and the way to the setting that fixes it. */
final class Checks {
    private Checks() {}

    record Problem(String text, String button, Runnable fix) {}

    static List<Problem> problems(Activity a) {
        List<Problem> out = new ArrayList<>();
        NotificationManager nm = a.getSystemService(NotificationManager.class);
        Uri me = Uri.fromParts("package", a.getPackageName(), null);
        Intent notifications = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, a.getPackageName());
        if (!nm.areNotificationsEnabled()) {
            out.add(new Problem("Notifications are off: alarms and messages can't show.", "Allow", () -> {
                if (a.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                        && !asked(a)) {
                    a.requestPermissions(new String[] {Manifest.permission.POST_NOTIFICATIONS}, 1);
                } else {
                    open(a, notifications);
                }
            }));
        } else if (Notes.off(a, App.ALARMS) || Notes.off(a, App.AMPI) || Notes.off(a, App.CHANGES)) {
            out.add(new Problem("Some of Ampi's notifications are switched off.", "Open settings", () -> open(a, notifications)));
        }
        if (!batteryFree(a)) {
            out.add(new Problem("Android may stop the connection to save battery.", "Allow in background",
                    () -> open(a, new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, me))));
        }
        if (!nm.canUseFullScreenIntent()) {
            out.add(new Problem("The alarm screen may not open over the lock screen.", "Allow",
                    () -> open(a, new Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, me))));
        }
        if (!keepsPermissions(a)) {
            out.add(new Problem("Android may take the app's permissions away if you don't open it for months. Switch off "
                    + "“Manage app if unused”.", "Open settings",
                    () -> open(a, new Intent(Intent.ACTION_AUTO_REVOKE_PERMISSIONS, me))));
        }
        return out;
    }

    static boolean batteryFree(Context c) {
        return c.getSystemService(PowerManager.class).isIgnoringBatteryOptimizations(c.getPackageName());
    }

    static boolean keepsPermissions(Context c) {
        return c.getPackageManager().isAutoRevokeWhitelisted();
    }

    static String version(Context c) {
        try {
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "?";
        }
    }

    /** Android shows the permission question twice at most; after that only the settings help. */
    private static boolean asked(Activity a) {
        var p = a.getSharedPreferences("ui", Context.MODE_PRIVATE);
        int n = p.getInt("asked", 0);
        p.edit().putInt("asked", n + 1).apply();
        return n >= 2;
    }

    private static void open(Activity a, Intent i) {
        try {
            a.startActivity(i);
        } catch (ActivityNotFoundException e) {
            a.startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", a.getPackageName(), null)));
        }
    }
}
