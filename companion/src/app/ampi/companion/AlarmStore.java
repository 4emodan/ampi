package app.ampi.companion;

import android.content.Context;
import android.util.AtomicFile;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * The alarms, in device-protected storage (readable after a reboot before the first unlock). Every change goes through
 * {@link #change}, which holds one lock and writes the file before anything is reported as done.
 */
final class AlarmStore {
    private static final int EVENTS = 40, TRASH = 60, REQUESTS = 100;
    private static final Object LOCK = new Object();

    final List<Alarm> alarms = new ArrayList<>();
    final List<JSONObject> events = new ArrayList<>();  // {ts, ev, label}
    final List<Alarm> trash = new ArrayList<>();  // cancelled lately, for Undo
    final List<String> requests = new ArrayList<>();  // ids of requests that changed something
    final List<Long> sets = new ArrayList<>();  // when Ampi set something (limit per day)
    boolean broken;  // an earlier file couldn't be understood: alarms Android still knows about ring anyway

    interface Change<T> {
        T run(AlarmStore s) throws JSONException, IOException, Refused;
    }

    /** Asked for something the phone won't do; the code goes back to the server, which has the words for it. */
    static final class Refused extends Exception {
        Refused(String code) {
            super(code);
        }
    }

    static <T> T change(Context c, Change<T> change) throws IOException, Refused {
        synchronized (LOCK) {
            AlarmStore s = read(c);
            try {
                T result = change.run(s);
                s.write(c);
                return result;
            } catch (JSONException e) {
                throw new IOException(e);
            }
        }
    }

    /** For showing; empty when the file can't be read right now. */
    static AlarmStore get(Context c) {
        synchronized (LOCK) {
            try {
                return read(c);
            } catch (IOException e) {
                Log.e("Ampi", "the alarm file can't be read now", e);
                return new AlarmStore();
            }
        }
    }

    Alarm find(String id) {
        for (Alarm a : alarms) if (a.id.equals(id)) return a;
        return null;
    }

    void event(String code, Alarm a) throws JSONException {
        events.add(new JSONObject().put("ts", System.currentTimeMillis()).put("ev", code).put("label", a.label));
        while (events.size() > EVENTS) events.remove(0);
    }

    void trash(Alarm a) {
        trash.add(a);
        while (trash.size() > TRASH) trash.remove(0);
    }

    void request(String id) {
        requests.add(id);
        while (requests.size() > REQUESTS) requests.remove(0);
    }

    private static AtomicFile file(Context c) {
        return new AtomicFile(new File(c.createDeviceProtectedStorageContext().getFilesDir(), "alarms.json"));
    }

    private static AlarmStore read(Context c) throws IOException {
        AtomicFile f = file(c);
        AlarmStore s = new AlarmStore();
        byte[] raw;
        try {
            raw = f.readFully();
        } catch (java.io.FileNotFoundException e) {
            if (f.getBaseFile().exists()) throw e;  // it is there but can't be opened now
            return s;
        }
        try {
            JSONObject o = new JSONObject(new String(raw, StandardCharsets.UTF_8));
            fill(o.optJSONArray("alarms"), s.alarms);
            fill(o.optJSONArray("trash"), s.trash);
            JSONArray e = o.optJSONArray("events"), r = o.optJSONArray("requests"), t = o.optJSONArray("sets");
            for (int i = 0; e != null && i < e.length(); i++) s.events.add(e.getJSONObject(i));
            for (int i = 0; r != null && i < r.length(); i++) s.requests.add(r.getString(i));
            for (int i = 0; t != null && i < t.length(); i++) s.sets.add(t.getLong(i));
            s.broken = o.optBoolean("broken");
            return s;
        } catch (JSONException | RuntimeException e) {
            // Never silently: keep the broken file, tell the owner, and let Android's alarms still ring.
            Log.e("Ampi", "the alarm file is broken", e);
            f.getBaseFile().renameTo(new File(f.getBaseFile().getParentFile(), "alarms.broken.json"));
            Notes.change(c, "lost", "Your Ampi alarms could not be read", "They have to be set again.", null, null);
            AlarmStore empty = new AlarmStore();
            empty.broken = true;
            try {
                empty.write(c);
            } catch (JSONException x) {
                throw new IOException(x);
            }
            return empty;
        }
    }

    private static void fill(JSONArray from, List<Alarm> to) throws JSONException {
        for (int i = 0; from != null && i < from.length(); i++) to.add(Alarm.from(from.getJSONObject(i)));
    }

    private void write(Context c) throws JSONException, IOException {
        JSONArray a = new JSONArray(), t = new JSONArray();
        for (Alarm x : alarms) a.put(x.json());
        for (Alarm x : trash) t.put(x.json());
        byte[] data = new JSONObject().put("alarms", a).put("trash", t).put("events", new JSONArray(events))
                .put("requests", new JSONArray(requests)).put("sets", new JSONArray(sets)).put("broken", broken)
                .toString().getBytes(StandardCharsets.UTF_8);
        save(file(c), data);
    }

    /** Written, and really there: AtomicFile only logs when the last step fails. */
    static void save(AtomicFile f, byte[] data) throws IOException {
        FileOutputStream out = f.startWrite();
        try {
            out.write(data);
            f.finishWrite(out);
        } catch (IOException e) {
            f.failWrite(out);
            throw e;
        }
        if (!Arrays.equals(f.readFully(), data)) throw new IOException("the file wasn't written");
    }
}
