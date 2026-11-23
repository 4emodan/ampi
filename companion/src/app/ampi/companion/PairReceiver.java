package app.ampi.companion;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Base64;

/** Gives the waiting key's public certificate to scripts/companion.ps1 (only adb may ask). */
public final class PairReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        if (!isOrderedBroadcast()) return;
        PairActivity.Pending p = PairActivity.pending;
        if (p != null) {
            setResultCode(1);
            setResultData(Base64.encodeToString(p.certificate(), Base64.NO_WRAP));
        } else {
            setResultCode(Config.load(c) != null ? 2 : 3);
        }
    }
}
