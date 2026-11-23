package app.ampi.companion;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;

/** The paired server, in the normal storage (readable after the first unlock). */
record Config(String host, int port, byte[] server, byte[] signin, String alias, String code) {
    // signin: the sign-in service's certificate; empty when paired without one (or by an app before there was one).

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("pairing", Context.MODE_PRIVATE);
    }

    /** Null when not paired. */
    static Config load(Context c) {
        SharedPreferences p = prefs(c);
        String alias = p.getString("alias", null), host = p.getString("host", null), server = p.getString("server", null);
        if (alias == null || host == null || server == null) return null;
        return new Config(host, p.getInt("port", 0), Base64.decode(server, Base64.NO_WRAP),
                Base64.decode(p.getString("signin", ""), Base64.NO_WRAP), alias, p.getString("code", ""));
    }

    void save(Context c) {
        prefs(c).edit().putString("host", host).putInt("port", port).putString("alias", alias).putString("code", code)
                .putString("server", Base64.encodeToString(server, Base64.NO_WRAP))
                .putString("signin", Base64.encodeToString(signin, Base64.NO_WRAP)).commit();
    }

    static void clear(Context c) {
        Config old = load(c);
        prefs(c).edit().clear().commit();
        if (old != null) Keys.delete(old.alias);
    }
}
