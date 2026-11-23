package app.ampi.companion;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;

import java.math.BigInteger;
import java.net.Socket;
import java.security.GeneralSecurityException;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.Principal;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.security.spec.ECGenParameterSpec;
import java.time.Instant;
import java.util.Arrays;
import java.util.Date;

import javax.net.ssl.KeyManager;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLEngine;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509ExtendedKeyManager;
import javax.net.ssl.X509ExtendedTrustManager;
import javax.security.auth.x500.X500Principal;

/** The connection's key (in the phone's secure hardware, can't be exported) and the pinned server certificate. */
final class Keys {
    private Keys() {}

    /** Makes a key and its self-signed certificate. Not fingerprint-bound: the link works while the phone is locked. */
    static String create() throws GeneralSecurityException {
        var random = new SecureRandom();
        byte[] name = new byte[8];
        random.nextBytes(name);
        String alias = "link-" + new BigInteger(1, name).toString(16);
        KeyPairGenerator g = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore");
        g.initialize(new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
                .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                // TLS signs a hash it made itself (NONEwithECDSA).
                .setDigests(KeyProperties.DIGEST_NONE, KeyProperties.DIGEST_SHA256)
                // A subject of its own: two phones with the default one get in each other's way on the server.
                .setCertificateSubject(new X500Principal("CN=ampi-" + alias))
                .setCertificateSerialNumber(new BigInteger(64, random).add(BigInteger.ONE))
                .setCertificateNotBefore(Date.from(Instant.parse("2020-01-01T00:00:00Z")))
                .setCertificateNotAfter(Date.from(Instant.parse("2049-01-01T00:00:00Z")))
                .build());
        g.generateKeyPair();
        return alias;
    }

    static void delete(String alias) {
        try {
            store().deleteEntry(alias);
        } catch (GeneralSecurityException e) {
            // Nothing left to delete.
        }
    }

    static X509Certificate certificate(String alias) throws GeneralSecurityException {
        if (store().getCertificate(alias) instanceof X509Certificate c) return c;
        throw new GeneralSecurityException("the app's key is gone: pair again");
    }

    static KeyStore store() throws GeneralSecurityException {
        try {
            KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
            ks.load(null);
            return ks;
        } catch (java.io.IOException e) {
            throw new GeneralSecurityException(e);
        }
    }

    static SSLContext context(String alias, byte[] server) throws GeneralSecurityException {
        if (!(store().getKey(alias, null) instanceof PrivateKey key)) throw new GeneralSecurityException("the app's key is gone: pair again");
        X509Certificate cert = certificate(alias);
        SSLContext ctx = SSLContext.getInstance("TLSv1.3");
        ctx.init(new KeyManager[] {new OneKey(alias, key, cert)}, new TrustManager[] {new Pin(server)}, null);
        return ctx;
    }

    private static final class OneKey extends X509ExtendedKeyManager {
        private final String alias;
        private final PrivateKey key;
        private final X509Certificate cert;

        OneKey(String alias, PrivateKey key, X509Certificate cert) {
            this.alias = alias;
            this.key = key;
            this.cert = cert;
        }

        @Override public String chooseClientAlias(String[] types, Principal[] issuers, Socket s) { return alias; }
        @Override public String chooseEngineClientAlias(String[] types, Principal[] issuers, SSLEngine e) { return alias; }
        @Override public String[] getClientAliases(String type, Principal[] issuers) { return new String[] {alias}; }
        @Override public X509Certificate[] getCertificateChain(String a) { return new X509Certificate[] {cert}; }
        @Override public PrivateKey getPrivateKey(String a) { return key; }
        @Override public String[] getServerAliases(String type, Principal[] issuers) { return null; }
        @Override public String chooseServerAlias(String type, Principal[] issuers, Socket s) { return null; }
    }

    /** Exactly one server: its certificate must be the one fixed while pairing. No authorities, no host names. */
    static final class Pin extends X509ExtendedTrustManager {
        private final byte[] server;

        Pin(byte[] server) {
            this.server = server.clone();
        }

        private void check(X509Certificate[] chain) throws CertificateException {
            if (server.length == 0 || chain == null || chain.length == 0 || !Arrays.equals(chain[0].getEncoded(), server)) {
                throw new CertificateException("this is not the paired server");
            }
        }

        @Override public void checkServerTrusted(X509Certificate[] c, String t) throws CertificateException { check(c); }
        @Override public void checkServerTrusted(X509Certificate[] c, String t, Socket s) throws CertificateException { check(c); }
        @Override public void checkServerTrusted(X509Certificate[] c, String t, SSLEngine e) throws CertificateException { check(c); }
        @Override public void checkClientTrusted(X509Certificate[] c, String t) throws CertificateException { throw new CertificateException(); }
        @Override public void checkClientTrusted(X509Certificate[] c, String t, Socket s) throws CertificateException { throw new CertificateException(); }
        @Override public void checkClientTrusted(X509Certificate[] c, String t, SSLEngine e) throws CertificateException { throw new CertificateException(); }
        @Override public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
    }
}
