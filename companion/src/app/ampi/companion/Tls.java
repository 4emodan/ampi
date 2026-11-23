package app.ampi.companion;

import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.ByteBuffer;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLEngine;
import javax.net.ssl.SSLEngineResult;

/**
 * A blocking TLS 1.3 client over two streams: the sign-in channel inside the link. (An SSLSocket would need a socket of its
 * own.) One thread uses it at a time. No Android in here: tested on the desktop.
 */
final class Tls {
    private static final ByteBuffer NOTHING = ByteBuffer.allocate(0);

    private final SSLEngine engine;
    private final InputStream in;
    private final OutputStream out;
    private final ByteBuffer net;   // received, not yet decrypted (ready to read from)
    private ByteBuffer plain;       // decrypted, not yet taken (ready to read from)
    private boolean ended;

    /** Connects: returns once both sides have proved who they are (the server by its certificate, see the context). */
    Tls(SSLContext context, InputStream in, OutputStream out) throws IOException {
        this.in = in;
        this.out = out;
        engine = context.createSSLEngine();
        engine.setUseClientMode(true);
        engine.setEnabledProtocols(new String[] {"TLSv1.3"});
        net = ByteBuffer.allocate(engine.getSession().getPacketBufferSize()).limit(0);
        plain = ByteBuffer.allocate(engine.getSession().getApplicationBufferSize()).limit(0);
        boolean done = false;
        try {
            handshake();
            done = true;
        } finally {
            if (!done) close();  // tells the other side why
        }
    }

    private void handshake() throws IOException {
        engine.beginHandshake();
        while (true) {
            switch (engine.getHandshakeStatus()) {
                case NEED_WRAP -> wrap(NOTHING);
                case NEED_UNWRAP, NEED_UNWRAP_AGAIN -> unwrap();
                case NEED_TASK -> tasks();
                default -> {  // done, or (some providers) closed without saying so
                    if (!"TLSv1.3".equals(engine.getSession().getProtocol())) throw new IOException("no TLS 1.3");
                    peer();
                    return;
                }
            }
        }
    }

    /** The other side's certificate. */
    byte[] peer() throws IOException {
        try {
            return engine.getSession().getPeerCertificates()[0].getEncoded();
        } catch (java.security.cert.CertificateException e) {
            throw new IOException(e);
        }
    }

    /** What the decrypted stream gives; the end of it is an error unless it comes between two messages. */
    InputStream input() {
        return new InputStream() {
            @Override
            public int read() throws IOException {
                byte[] one = new byte[1];
                return read(one, 0, 1) < 0 ? -1 : one[0] & 0xff;
            }

            @Override
            public int read(byte[] b, int off, int len) throws IOException {
                if (len == 0) return 0;
                while (!plain.hasRemaining()) {
                    if (ended) return -1;
                    unwrap();
                }
                int n = Math.min(len, plain.remaining());
                plain.get(b, off, n);
                return n;
            }
        };
    }

    void write(byte[] data) throws IOException {
        ByteBuffer src = ByteBuffer.wrap(data);
        while (src.hasRemaining()) wrap(src);
    }

    private void wrap(ByteBuffer src) throws IOException {
        ByteBuffer packet = ByteBuffer.allocate(engine.getSession().getPacketBufferSize());
        while (true) {
            SSLEngineResult r = engine.wrap(src, packet);
            switch (r.getStatus()) {
                case BUFFER_OVERFLOW -> packet = ByteBuffer.allocate(packet.capacity() * 2);
                default -> {
                    if (packet.position() > 0) {  // also with CLOSED: the goodbye, or why it failed
                        out.write(packet.array(), 0, packet.position());
                        out.flush();
                    }
                    if (r.getStatus() == SSLEngineResult.Status.CLOSED && src.hasRemaining()) throw new IOException("the channel is closed");
                    if (r.getHandshakeStatus() == SSLEngineResult.HandshakeStatus.NEED_TASK) tasks();
                    return;
                }
            }
        }
    }

    /** Decrypts one record (waiting for it). */
    private void unwrap() throws IOException {
        while (true) {
            plain.compact();
            SSLEngineResult r;
            try {
                r = engine.unwrap(net, plain);
            } finally {
                plain.flip();
            }
            switch (r.getStatus()) {
                case BUFFER_UNDERFLOW -> {
                    net.compact();
                    int n;
                    try {
                        if (!net.hasRemaining()) throw new IOException("a record too long");
                        n = in.read(net.array(), net.position(), net.remaining());
                        if (n > 0) net.position(net.position() + n);
                    } finally {
                        net.flip();
                    }
                    if (n < 0) throw new EOFException("the channel ended");  // without TLS's own goodbye: cut off
                }
                case BUFFER_OVERFLOW -> {
                    ByteBuffer bigger = ByteBuffer.allocate(plain.capacity() * 2);
                    bigger.put(plain).flip();
                    plain = bigger;
                }
                case CLOSED -> {
                    ended = true;
                    return;
                }
                default -> {
                    if (r.getHandshakeStatus() == SSLEngineResult.HandshakeStatus.NEED_TASK) tasks();
                    return;
                }
            }
        }
    }

    private void tasks() {
        for (Runnable t = engine.getDelegatedTask(); t != null; t = engine.getDelegatedTask()) t.run();
    }

    /** Says goodbye (the other side then knows nothing was cut off). */
    void close() {
        engine.closeOutbound();
        try {
            for (int i = 0; i < 4 && !engine.isOutboundDone(); i++) wrap(NOTHING);
        } catch (IOException | RuntimeException e) {
            // Nobody there any more.
        }
    }
}
