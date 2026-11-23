package app.ampi.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Every 15 minutes: is the connection still alive? */
public final class LinkReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        LinkService.start(c, true);
    }
}
