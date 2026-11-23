package app.ampi.companion;

import java.io.ByteArrayInputStream;
import java.io.FileInputStream;
import java.net.Socket;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.Arrays;

import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/** The phone's side of the sign-in channel against Python's ssl (run by tests/test_companion.py): PORT P12 SERVER.CRT. */
final class TlsTest {
    public static void main(String[] a) throws Exception {
        KeyStore ks = KeyStore.getInstance("PKCS12");
        try (var f = new FileInputStream(a[1])) {
            ks.load(f, "test".toCharArray());
        }
        KeyManagerFactory keys = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        keys.init(ks, "test".toCharArray());
        byte[] pinned;
        try (var f = new FileInputStream(a[2])) {
            pinned = CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(f.readAllBytes())).getEncoded();
        }
        SSLContext ctx = SSLContext.getInstance("TLSv1.3");
        ctx.init(keys.getKeyManagers(), new TrustManager[] {new X509TrustManager() {
            @Override public void checkClientTrusted(X509Certificate[] c, String t) { throw new IllegalStateException(); }
            @Override public void checkServerTrusted(X509Certificate[] c, String t) throws java.security.cert.CertificateException {
                if (!Arrays.equals(c[0].getEncoded(), pinned)) throw new java.security.cert.CertificateException("another server");
            }
            @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
        }}, null);
        try (Socket s = new Socket("127.0.0.1", Integer.parseInt(a[0]))) {
            s.setSoTimeout(20_000);
            Tls t = new Tls(ctx, s.getInputStream(), s.getOutputStream());
            // Echoes frames until the other side says goodbye.
            for (String f = Frames.read(t.input()); f != null; f = Frames.read(t.input())) t.write(Frames.bytes(f));
            t.close();
            System.out.println("goodbye");
        } catch (java.io.IOException e) {
            System.out.println("error " + e.getClass().getSimpleName());
        }
    }
}
