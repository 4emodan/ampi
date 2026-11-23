package app.ampi.companion;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.security.GeneralSecurityException;

import javax.net.ssl.SSLSocket;

/** Connecting to the paired server's gateway. */
final class Link {
    private Link() {}

    /** TLS with the pinned server over {@code raw} (which the caller can close to end it). */
    static SSLSocket connect(Config cfg, Socket raw) throws IOException, GeneralSecurityException {
        raw.connect(new InetSocketAddress(cfg.host(), cfg.port()), 15_000);
        raw.setTcpNoDelay(true);
        SSLSocket s = (SSLSocket) Keys.context(cfg.alias(), cfg.server()).getSocketFactory()
                .createSocket(raw, cfg.host(), cfg.port(), true);
        s.setEnabledProtocols(new String[] {"TLSv1.3"});
        s.setSoTimeout(20_000);
        s.startHandshake();
        return s;
    }

    /** TLS 1.3: the server's refusal of this phone only shows at the first read, so nothing counts before "welcome". */
    static JSONObject welcome(SSLSocket s) throws IOException {
        String f = Frames.read(s.getInputStream());
        JSONObject w;
        try {
            w = f == null ? new JSONObject() : new JSONObject(f);
        } catch (JSONException e) {
            throw new IOException("bad message");
        }
        if (!"welcome".equals(w.optString("t"))) throw new IOException("the server doesn't accept this phone");
        if (w.optInt("proto") != 1) throw new IOException("the server speaks another version: update the app");
        return w;
    }
}
