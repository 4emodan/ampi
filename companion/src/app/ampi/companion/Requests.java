package app.ampi.companion;

import android.content.Context;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;

/** What Ampi may ask of this phone. Anything else is refused here, whatever the server says. Answers are codes: no free
 *  text goes back, except the labels Ampi gave and what the owner reads here. */
final class Requests {
    private static final double MAX_TIMER_S = 24 * 3600 + 30;

    private Requests() {}

    /** @param now the server's clock, in seconds */
    static JSONObject handle(Context c, JSONObject msg, double now) throws JSONException {
        String id = msg.optString("id");
        JSONObject res = new JSONObject().put("t", "res").put("id", id);
        try {
            if (!id.matches("[0-9a-f]{16}")) throw new AlarmStore.Refused("bad_request");
            double exp = msg.optDouble("exp", 0);
            if (!(exp > now)) throw new AlarmStore.Refused("too_late");
            String kind = msg.optString("kind");
            // Nothing Ampi writes may appear while the owner looks at a sign-in request.
            if (!kind.equals("status") && !kind.equals("alarm.list") && Unlock.current() != null) {
                throw new AlarmStore.Refused("signin_open");
            }
            JSONObject body = msg.optJSONObject("body");
            JSONObject data = run(c, id, kind, body == null ? new JSONObject() : body, now);
            return res.put("ok", true).put("data", data);
        } catch (AlarmStore.Refused e) {
            return res.put("ok", false).put("error", e.getMessage());
        } catch (IOException | RuntimeException e) {
            Log.e("Ampi", "request", e);
            return res.put("ok", false).put("error", "failed");
        }
    }

    private static JSONObject run(Context c, String id, String kind, JSONObject body, double now)
            throws AlarmStore.Refused, IOException, JSONException {
        switch (kind) {
            case "status":
                return new JSONObject().put("zone", java.time.ZoneId.systemDefault().getId()).put("warn", Alarms.warnings(c));
            case "notify": {
                String title = Texts.line(body.optString("title"), 80), text = Texts.lines(body.optString("text"), 1000);
                if (title.isEmpty()) throw new AlarmStore.Refused("bad_request");
                Notes.message(c, id, title, text);
                return new JSONObject();
            }
            case "alarm.set":
                return Alarms.set(c, id, Alarms.parse(body));
            case "timer.set": {
                double left = body.optDouble("end", 0) - now;
                if (!(left > 0)) throw new AlarmStore.Refused("past");
                if (left > MAX_TIMER_S) throw new AlarmStore.Refused("too_long");
                Alarm a = new Alarm();
                a.kind = "timer";
                a.label = Texts.line(body.optString("label"), 60);
                a.in(c, System.currentTimeMillis(), Math.round(left * 1000));
                return Alarms.set(c, id, a);
            }
            case "alarm.list":
                return Alarms.list(c);
            case "alarm.cancel":
                Alarms.cancel(c, id, body.optString("id"));
                return new JSONObject();
            default:
                throw new AlarmStore.Refused("unknown_kind");
        }
    }
}
