package app.ampi.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** The moment of an alarm, and the Undo buttons (only this app and Android's alarm clock can reach it). */
public final class AlarmReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        if (intent.getData() == null || intent.getAction() == null) return;
        String id = intent.getData().getSchemeSpecificPart();
        switch (intent.getAction()) {
            case Alarms.FIRE -> Alarms.fire(c, id);
            case Alarms.UNDO_SET -> Alarms.delete(c, id, "undone by the owner");
            case Alarms.UNDO_CANCEL -> Alarms.undoCancel(c, id);
            default -> { }
        }
    }
}
