package app.ampi.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Every 15 minutes: is the connection still alive, and is every alarm really set? */
public final class LinkReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        LinkService.stayAwake(c);
        Alarms.rearmAll(c);
        LinkService.start(c, true);
    }
}
