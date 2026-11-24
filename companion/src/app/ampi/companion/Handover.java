package app.ampi.companion;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyPermanentlyInvalidatedException;
import android.security.keystore.KeyProperties;
import android.security.keystore.KeyProtection;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.UnrecoverableKeyException;
import java.util.Arrays;
import java.util.Base64;

import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;

/**
 * Sessions the owner gives from Firefox (docs/COMPANION.md). The extension has a public key and a secret: with them it can seal one,
 * and nobody without them can; opening needs the private key, in the phone's secure hardware, with a fingerprint every time. One
 * session at a time, in memory, for 10 minutes.
 */
final class Handover {
    private static final String KEY = "handover", SECRET = "handover-secret";
    private static SessionBox current;  // guarded by Handover.class

    private Handover() {}

    static boolean ready() {
        try {
            return Keys.store().containsAlias(KEY) && Keys.store().containsAlias(SECRET);
        } catch (GeneralSecurityException e) {
            return false;
        }
    }

    /** The key can still be used (a newly enrolled fingerprint ends it). */
    static boolean usable() {
        try {
            opener();
            return true;
        } catch (Logins.Gone e) {
            return false;
        } catch (GeneralSecurityException | RuntimeException e) {
            return true;  // perhaps only now
        }
    }

    /** New keys (the old ones are gone): what the extension needs, as text, shown once. */
    static String create() throws GeneralSecurityException {
        byte[] secret = new byte[32];
        new SecureRandom().nextBytes(secret);
        try {
            remove();
            KeyPairGenerator g = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_RSA, "AndroidKeyStore");
            g.initialize(new KeyGenParameterSpec.Builder(KEY, KeyProperties.PURPOSE_DECRYPT).setKeySize(2048)
                    .setDigests(KeyProperties.DIGEST_SHA1).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_RSA_OAEP)
                    .setUserAuthenticationRequired(true).setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
                    .setInvalidatedByBiometricEnrollment(true).build());
            KeyPair pair = g.generateKeyPair();
            Keys.store().setEntry(SECRET, new KeyStore.SecretKeyEntry(new SecretKeySpec(secret, KeyProperties.KEY_ALGORITHM_HMAC_SHA256)),
                    new KeyProtection.Builder(KeyProperties.PURPOSE_SIGN).setDigests(KeyProperties.DIGEST_SHA256).build());
            Base64.Encoder e = Base64.getUrlEncoder().withoutPadding();
            return "ampi-key2." + e.encodeToString(secret) + "." + e.encodeToString(pair.getPublic().getEncoded());
        } finally {
            Arrays.fill(secret, (byte) 0);
        }
    }

    static void remove() {
        clear();
        Keys.delete(KEY);
        Keys.delete(SECRET);
    }

    static synchronized void clear() {
        current = null;
    }

    /** What was shared or pasted. @throws IllegalArgumentException with the reason, for the owner */
    static SessionBox take(Context c, String text) {
        SessionBox b = SessionBox.parse(text, System.currentTimeMillis());
        if (!ready()) throw new IllegalArgumentException("Set this up first.");
        if (wasUsed(c, b)) throw new IllegalArgumentException("That session was used already: give it in Firefox again.");
        synchronized (Handover.class) {
            current = b;
        }
        return b;
    }

    /** The one that is here and still good, or null. */
    static synchronized SessionBox current() {
        if (current != null && !current.fresh(System.currentTimeMillis())) current = null;
        return current;
    }

    private static SharedPreferences used(Context c) {
        return c.getSharedPreferences("handover", Context.MODE_PRIVATE);
    }

    private static boolean wasUsed(Context c, SessionBox b) {
        return ("," + used(c).getString("ids", "")).contains("," + b.id() + ",");
    }

    /** It is sent now, and not a second time; false when it was already, or isn't the one that is here any more. */
    static synchronized boolean spend(Context c, SessionBox b) {
        if (current() != b || wasUsed(c, b)) return false;
        current = null;
        String ids = b.id() + "," + used(c).getString("ids", "");
        return used(c).edit().putString("ids", ids.substring(0, Math.min(ids.length(), 50 * 33))).commit();
    }

    private static Cipher opener() throws GeneralSecurityException, Logins.Gone {
        PrivateKey k;
        try {
            k = (PrivateKey) Keys.store().getKey(KEY, null);
        } catch (UnrecoverableKeyException e) {
            if (String.valueOf(e.getMessage()).contains("Failed to obtain information")) throw e;
            throw new Logins.Gone();
        }
        if (k == null) throw new Logins.Gone();
        Cipher cipher = Cipher.getInstance("RSA/ECB/OAEPWithSHA-1AndMGF1Padding");
        try {
            cipher.init(Cipher.DECRYPT_MODE, k);
        } catch (KeyPermanentlyInvalidatedException e) {
            throw new Logins.Gone();
        }
        return cipher;
    }

    /** A new one for every try: a fingerprint is good for one use. */
    static Cipher toOpen(SessionBox b) throws GeneralSecurityException, Logins.Gone {
        return opener();
    }

    /** After the fingerprint: the plain text. The caller wipes it. */
    static byte[] opened(Cipher cipher, SessionBox b) throws GeneralSecurityException {
        byte[] random = cipher.doFinal(b.wrapped()), key = null;
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init((SecretKey) Keys.store().getKey(SECRET, null));
            key = mac.doFinal(b.forKey(random));
            return b.open(key);
        } finally {
            Arrays.fill(random, (byte) 0);
            if (key != null) Arrays.fill(key, (byte) 0);
        }
    }

    /** What is in it, when it is what was said it is: the number of cookies, and how far they go. */
    static JSONObject contents(byte[] plain, SessionBox b) throws JSONException {
        JSONObject o = new JSONObject(new String(plain, StandardCharsets.UTF_8));
        JSONArray cookies = o.getJSONArray("c");
        if (cookies.length() != b.count()) throw new JSONException("count");
        for (int i = 0; i < cookies.length(); i++) {
            JSONObject c = cookies.getJSONObject(i);
            String d = c.getString("d");
            if (c.getBoolean("o") ? !d.equals(b.host()) : b.scope().isEmpty() || !(d.equals(b.scope()) || d.endsWith("." + b.scope()))) throw new JSONException("scope");
        }
        return new JSONObject().put("host", b.host()).put("path", o.getString("p")).put("cookies", cookies);
    }
}
