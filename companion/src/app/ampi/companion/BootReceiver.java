package app.ampi.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.UserManager;

/** Sets the alarms again after a reboot (even before the first unlock), an update, or a time or zone change. */
public final class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        Alarms.rearmAll(c);
        boolean unlocked = c.getSystemService(UserManager.class).isUserUnlocked();
        if (unlocked && !Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(intent.getAction())) LinkService.start(c, false);
    }
}
