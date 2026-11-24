package app.ampi.companion;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyPermanentlyInvalidatedException;
import android.security.keystore.KeyProperties;
import android.security.keystore.KeyProtection;

import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.security.SecureRandom;
import java.security.UnrecoverableKeyException;
import java.util.Arrays;
import java.util.Base64;

import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * Sessions the owner gives from Firefox (docs/COMPANION.md): the extension encrypts them with a key it got from here once; here that
 * key only opens, in the phone's secure hardware, with a fingerprint every time. One session at a time, in memory, for 10 minutes.
 */
final class Handover {
    private static final String KEY = "handover";
    private static SessionBox current;  // guarded by Handover.class

    private Handover() {}

    static boolean ready() {
        try {
            return Keys.store().containsAlias(KEY);
        } catch (GeneralSecurityException e) {
            return false;
        }
    }

    /** A new key (the old one is gone): as text for the extension, shown once. */
    static String create() throws GeneralSecurityException {
        byte[] raw = new byte[32];
        new SecureRandom().nextBytes(raw);
        try {
            clear();
            Keys.delete(KEY);
            Keys.store().setEntry(KEY, new KeyStore.SecretKeyEntry(new SecretKeySpec(raw, "AES")),
                    new KeyProtection.Builder(KeyProperties.PURPOSE_DECRYPT).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setUserAuthenticationRequired(true)
                            .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
                            .setInvalidatedByBiometricEnrollment(true).build());
            return "ampi-key1-" + Base64.getUrlEncoder().withoutPadding().encodeToString(raw);
        } finally {
            Arrays.fill(raw, (byte) 0);
        }
    }

    static void remove() {
        clear();
        Keys.delete(KEY);
    }

    static synchronized void clear() {
        current = null;
    }

    /** What was shared or pasted. @throws IllegalArgumentException with the reason, for the owner */
    static SessionBox take(Context c, String text) {
        SessionBox b = SessionBox.parse(text, System.currentTimeMillis());
        if (!ready()) throw new IllegalArgumentException("Set this up first.");
        if (("," + used(c).getString("ids", "") + ",").contains("," + b.id() + ",")) throw new IllegalArgumentException("That session was used already: give it in Firefox again.");
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

    /** It is sent now: not a second time. */
    static void spent(Context c, SessionBox b) {
        synchronized (Handover.class) {
            if (current == b) current = null;
        }
        String ids = b.id() + "," + used(c).getString("ids", "");
        used(c).edit().putString("ids", ids.substring(0, Math.min(ids.length(), 50 * 33))).apply();
    }

    /** A new one for every try: a fingerprint is good for one use. */
    static Cipher toOpen(SessionBox b) throws GeneralSecurityException, Logins.Gone {
        SecretKey k;
        try {
            k = (SecretKey) Keys.store().getKey(KEY, null);
        } catch (UnrecoverableKeyException e) {
            if (String.valueOf(e.getMessage()).contains("Failed to obtain information")) throw e;
            throw new Logins.Gone();
        }
        if (k == null) throw new Logins.Gone();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        try {
            cipher.init(Cipher.DECRYPT_MODE, k, new GCMParameterSpec(128, b.iv()));
        } catch (KeyPermanentlyInvalidatedException e) {
            throw new Logins.Gone();
        }
        return cipher;
    }

    /** After the fingerprint: the plain text. The caller wipes it. */
    static byte[] opened(Cipher cipher, SessionBox b) throws GeneralSecurityException {
        cipher.updateAAD(b.bound());
        return cipher.doFinal(b.data());
    }
}
