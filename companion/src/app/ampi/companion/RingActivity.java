package app.ampi.companion;

import android.app.Activity;
import android.os.Bundle;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextClock;
import android.widget.TextView;

/** The alarm screen, over the lock screen. Leaving it doesn't stop the alarm; Stop and Snooze do. */
public final class RingActivity extends Activity {
    private TextView what;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        setShowWhenLocked(true);
        setTurnScreenOn(true);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        LinearLayout col = Ui.page(this);
        col.setGravity(Gravity.CENTER);
        TextClock time = new TextClock(this);
        time.setFormat12Hour("HH:mm");
        time.setFormat24Hour("HH:mm");
        time.setTextSize(72);
        time.setTextColor(Ui.text(this));
        time.setGravity(Gravity.CENTER);
        col.addView(time, Ui.FULL, Ui.WRAP);
        what = Ui.add(col, "", 20, Ui.quiet(this));
        what.setGravity(Gravity.CENTER);
        what.setPadding(0, 0, 0, Ui.dp(this, 48));
        Ui.button(col, "Stop", true, v -> startForegroundService(RingService.stop(this, false))).setMinHeight(Ui.dp(this, 72));
        Ui.button(col, "Snooze 10 min", false, v -> startForegroundService(RingService.stop(this, true))).setMinHeight(Ui.dp(this, 72));
    }

    @Override
    protected void onResume() {
        super.onResume();
        RingService.watch(this::update);
        update();
    }

    @Override
    protected void onPause() {
        RingService.watch(null);
        super.onPause();
    }

    private void update() {
        var texts = RingService.texts();
        if (texts.isEmpty()) finish();
        else what.setText(String.join("\n", texts));
    }
}
