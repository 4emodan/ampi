package app.ampi.companion;

import android.content.Context;
import android.hardware.biometrics.BiometricManager;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyPermanentlyInvalidatedException;
import android.security.keystore.KeyProperties;
import android.util.AtomicFile;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileNotFoundException;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.UnrecoverableKeyException;
import java.util.ArrayList;
import java.util.List;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * The saved logins: user name and password of each, encrypted by a key in the phone's secure hardware that needs a fingerprint
 * for every single use, saving included. The site, the label and the paired sign-in service are bound into each (a change of
 * them, or another server, can't open it). Only here: nothing of them is on the server.
 */
final class Logins {
    static final int MAX = 100;
    private static final String KEY = "logins";
    private static final int TAG_BITS = 128;

    private Logins() {}

    /** @param server which sign-in service it was saved for (hash of its certificate) */
    record Login(String id, String label, List<String> hosts, String server, byte[] iv, byte[] data) {
        byte[] bound() {
            return ("ampi-login-v1\n" + id + "\n" + server + "\n" + String.join(" ", hosts) + "\n" + label + "\n")
                    .getBytes(StandardCharsets.UTF_8);
        }
    }

    /** The key is there no more (a fingerprint was added, all were removed, or the screen lock was). */
    static final class Gone extends Exception {
        Gone() {
            super("The saved logins can't be opened any more, because the phone's fingerprints, face unlock or screen lock "
                    + "changed. If that wasn't you, first remove fingerprints you don't know and change the PIN: whoever added one "
                    + "could also open what you save next.");
        }
    }

    static boolean fingerprintReady(Context c) {
        return c.getSystemService(BiometricManager.class).canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG)
                == BiometricManager.BIOMETRIC_SUCCESS;
    }

    static String serverId(byte[] signin) {
        try {
            return hex(MessageDigest.getInstance("SHA-256").digest(signin));
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException(e);
        }
    }

    static String newId() {
        byte[] b = new byte[8];
        new SecureRandom().nextBytes(b);
        return hex(b);
    }

    private static String hex(byte[] bytes) {
        var out = new StringBuilder();
        for (byte b : bytes) out.append(String.format("%02x", b));
        return out.toString();
    }

    // --- the key -----------------------------------------------------------------------------------------

    private static SecretKey key() throws GeneralSecurityException, Gone {
        try {
            return (SecretKey) Keys.store().getKey(KEY, null);
        } catch (UnrecoverableKeyException e) {  // also what getKey makes of a key that was invalidated
            if (String.valueOf(e.getMessage()).toLowerCase(java.util.Locale.ROOT).contains("invalidated")) throw new Gone();
            throw e;
        }
    }

    /** For saving one more: makes the key the first time, never while there are logins it couldn't open. */
    static Cipher toSave(Context c) throws GeneralSecurityException, IOException, Gone {
        if (!all(c).isEmpty()) return encryptor();
        try {
            return encryptor();
        } catch (Gone e) {
            create();
            return encryptor();
        }
    }

    private static Cipher encryptor() throws GeneralSecurityException, Gone {
        SecretKey k = key();
        if (k == null) throw new Gone();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        try {
            cipher.init(Cipher.ENCRYPT_MODE, k);
        } catch (KeyPermanentlyInvalidatedException e) {
            throw new Gone();
        }
        return cipher;
    }

    /** A new one for every try: a fingerprint is good for one use. */
    static Cipher toOpen(Login l) throws GeneralSecurityException, Gone {
        SecretKey k = key();
        if (k == null) throw new Gone();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        try {
            cipher.init(Cipher.DECRYPT_MODE, k, new GCMParameterSpec(TAG_BITS, l.iv));
        } catch (KeyPermanentlyInvalidatedException e) {
            throw new Gone();
        }
        return cipher;
    }

    private static void create() throws GeneralSecurityException {
        KeyGenerator g = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        g.init(new KeyGenParameterSpec.Builder(KEY, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).setUserAuthenticationRequired(true)
                .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
                .setInvalidatedByBiometricEnrollment(true).build());
        g.generateKey();
    }

    /** After the fingerprint ({@code cipher} is the one it was given for). */
    static Login sealed(Cipher cipher, String label, List<String> hosts, String server, String user, String secret)
            throws GeneralSecurityException, JSONException {
        Login l = new Login(newId(), label, List.copyOf(hosts), server, cipher.getIV(), new byte[0]);
        cipher.updateAAD(l.bound());
        byte[] plain = new JSONObject().put("user", user).put("secret", secret).toString().getBytes(StandardCharsets.UTF_8);
        try {
            return new Login(l.id, l.label, l.hosts, l.server, l.iv, cipher.doFinal(plain));
        } finally {
            java.util.Arrays.fill(plain, (byte) 0);
        }
    }

    /** After the fingerprint: the plain text. The caller wipes it. */
    static byte[] opened(Cipher cipher, Login l) throws GeneralSecurityException {
        cipher.updateAAD(l.bound());
        return cipher.doFinal(l.data);
    }

    // --- the file ------------------------------------------------------------------------------------------

    private static AtomicFile file(Context c) {
        return new AtomicFile(new File(c.getFilesDir(), "logins.json"));
    }

    static synchronized List<Login> all(Context c) throws IOException {
        List<Login> out = new ArrayList<>();
        AtomicFile f = file(c);
        byte[] raw;
        try {
            raw = f.readFully();
        } catch (FileNotFoundException e) {
            if (f.getBaseFile().exists()) throw e;
            return out;
        }
        try {
            JSONArray a = new JSONArray(new String(raw, StandardCharsets.UTF_8));
            for (int i = 0; i < a.length(); i++) {
                JSONObject o = a.getJSONObject(i);
                JSONArray h = o.getJSONArray("hosts");
                List<String> hosts = new ArrayList<>();
                for (int j = 0; j < h.length(); j++) {
                    if (!Hosts.valid(h.getString(j))) throw new JSONException("host");
                    hosts.add(h.getString(j));
                }
                out.add(new Login(o.getString("id"), o.getString("label"), List.copyOf(hosts), o.getString("server"),
                        Base64.decode(o.getString("iv"), Base64.NO_WRAP), Base64.decode(o.getString("data"), Base64.NO_WRAP)));
            }
        } catch (JSONException | RuntimeException e) {
            throw new IOException("the saved logins can't be read", e);  // never overwritten, only deleted by the owner
        }
        return out;
    }

    static synchronized void add(Context c, Login l) throws IOException {
        List<Login> all = all(c);
        if (all.size() >= MAX) throw new IOException("at most " + MAX + " logins");
        all.add(l);
        save(c, all);
    }

    static synchronized void delete(Context c, String id) throws IOException {
        List<Login> all = all(c);
        all.removeIf(l -> l.id.equals(id));
        save(c, all);
    }

    static synchronized void deleteAll(Context c) {
        file(c).delete();
        Keys.delete(KEY);
    }

    private static void save(Context c, List<Login> all) throws IOException {
        try {
            JSONArray a = new JSONArray();
            for (Login l : all) {
                a.put(new JSONObject().put("id", l.id).put("label", l.label).put("hosts", new JSONArray(l.hosts))
                        .put("server", l.server).put("iv", Base64.encodeToString(l.iv, Base64.NO_WRAP))
                        .put("data", Base64.encodeToString(l.data, Base64.NO_WRAP)));
            }
            AlarmStore.save(file(c), a.toString().getBytes(StandardCharsets.UTF_8));
        } catch (JSONException e) {
            throw new IOException(e);
        }
    }
}
