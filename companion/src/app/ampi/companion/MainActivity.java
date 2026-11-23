package app.ampi.companion;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.KeyguardManager;
import android.content.Intent;
import android.hardware.biometrics.BiometricManager;
import android.hardware.biometrics.BiometricPrompt;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.Looper;
import android.widget.LinearLayout;

import java.util.List;

/** Status and what to fix, alarms, messages, unpairing. */
public final class MainActivity extends Activity {
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable tick = this::refresh;
    private String shown = "";

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Alarms.rearmAll(this);
        LinkService.start(this, false);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!RingService.texts().isEmpty()) startActivity(new Intent(this, RingActivity.class));  // also when its notification is off
        shown = "";
        refresh();
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(tick);
        super.onPause();
    }

    private void refresh() {
        handler.removeCallbacks(tick);
        handler.postDelayed(tick, 2000);
        Config cfg = Config.load(this);
        AlarmStore store = AlarmStore.get(this);
        List<Notes.Message> messages = Notes.messages(this);
        List<Checks.Problem> problems = Checks.problems(this);
        var key = new StringBuilder().append(LinkService.connectedSince).append(LinkService.problem).append(cfg == null)
                .append(Unlock.current() != null).append(messages.size()).append(messages.isEmpty() ? "" : messages.get(messages.size() - 1).id());
        for (Checks.Problem p : problems) key.append(p.text());
        for (Alarm a : store.alarms) key.append(a.id).append(a.at).append(a.ringing);
        if (key.toString().equals(shown)) return;  // keeps the scroll position while nothing changed
        shown = key.toString();
        draw(cfg, store, messages, problems);
    }

    private void draw(Config cfg, AlarmStore store, List<Notes.Message> messages, List<Checks.Problem> problems) {
        LinearLayout page = Ui.page(this);
        Ui.title(page, "Ampi");
        LinearLayout status = Ui.card(page);
        if (cfg == null) {
            Ui.body(status, "Not paired");
            Ui.small(status, "Connect the phone to your PC with a USB cable and run scripts\\companion.ps1 -Pair there.");
        } else if (LinkService.connectedSince != 0) {
            Ui.body(status, "Connected");
            Ui.small(status, "since " + Alarm.when(LinkService.connectedSince));
        } else {
            Ui.body(status, "Not connected");
            String last = LinkService.lastContact == 0 ? "no contact since the app started" : "last contact " + Alarm.when(LinkService.lastContact);
            Ui.small(status, last + (LinkService.problem.isEmpty() ? "" : "\n" + LinkService.problem));
            Ui.small(status, "Alarms already set ring anyway.");
        }
        if (Unlock.current() != null) {
            LinearLayout card = Ui.card(page);
            Ui.body(card, "Ampi wants to use a saved login");
            Ui.button(card, "Show", true, v -> startActivity(new Intent(this, UnlockActivity.class)));
        }
        for (Checks.Problem p : problems) {
            LinearLayout card = Ui.card(page);
            Ui.body(card, p.text());
            Ui.button(card, p.button(), true, v -> p.fix().run());
        }

        Ui.heading(page, "Alarms");
        for (Alarm a : Alarms.sorted(store.alarms)) {
            LinearLayout card = Ui.card(page);
            Ui.body(card, a.text());
            Ui.small(card, a.kind.equals("timer") ? "Timer" : "Alarm");
            Ui.button(card, "Delete", false, v -> {
                Alarms.delete(this, a.id, "deleted");
                refresh();
            });
        }
        if (store.alarms.isEmpty()) Ui.small(Ui.card(page), "None. Ask Ampi, e.g. “wake me at 7”.");
        Ui.button(page, "Test: ring in 10 seconds", false, v -> {
            Alarms.test(this);
            refresh();
        });

        Ui.heading(page, "Saved logins");
        LinearLayout logins = Ui.card(page);
        Ui.small(logins, "Only on this phone, opened by your fingerprint.");
        Ui.button(logins, "Open", false, v -> startActivity(new Intent(this, LoginsActivity.class)));

        Ui.heading(page, "Messages");
        if (messages.isEmpty()) Ui.small(Ui.card(page), "None yet.");
        for (int i = messages.size() - 1; i >= 0; i--) {
            Notes.Message m = messages.get(i);
            LinearLayout card = Ui.card(page);
            Ui.body(card, m.title());
            if (!m.text().isEmpty()) Ui.small(card, m.text()).setTextIsSelectable(true);
            Ui.small(card, Alarm.when(m.ts()));
        }
        if (!messages.isEmpty()) {
            Ui.button(page, "Clear messages", false, v -> {
                Notes.clear(this);
                refresh();
            });
        }

        Ui.heading(page, "About");
        LinearLayout about = Ui.card(page);
        Ui.small(about, "Version " + Checks.version(this));
        if (cfg != null) {
            Ui.small(about, "Server " + cfg.host() + ":" + cfg.port() + "\nPairing code " + cfg.code());
            Ui.button(about, "Unpair", false, v -> confirmUnpair());
        }
    }

    private void confirmUnpair() {
        new AlertDialog.Builder(this).setTitle("Unpair this phone?")
                .setMessage("The connection ends and its key is deleted. Alarms and saved logins stay (the logins only work with the "
                        + "same server). On the server, also run: ampi-phone remove.")
                .setNegativeButton("Keep", null).setPositiveButton("Unpair", (d, w) -> withScreenLock(this::unpair)).show();
    }

    private void withScreenLock(Runnable then) {
        if (!getSystemService(KeyguardManager.class).isDeviceSecure()) {
            then.run();
            return;
        }
        new BiometricPrompt.Builder(this).setTitle("Unpair from the server")
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG | BiometricManager.Authenticators.DEVICE_CREDENTIAL)
                .build().authenticate(new CancellationSignal(), getMainExecutor(), new BiometricPrompt.AuthenticationCallback() {
                    @Override
                    public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) {
                        then.run();
                    }
                });
    }

    private void unpair() {
        LinkService.stop(this);
        Config.clear(this);
        Unlock.interrupt();
        VaultSession.closeCurrent();
        Notes.clear(this);
        refresh();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        refresh();
    }
}
