package app.ampi.companion;

import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;

/** 4-byte big-endian length + UTF-8 text, at most {@link #MAX} bytes. */
final class Frames {
    static final int MAX = 64 * 1024;

    private Frames() {}

    /** Null when the other side closed the connection between two frames. */
    static String read(InputStream in) throws IOException {
        int first = in.read();
        if (first < 0) return null;
        var d = new DataInputStream(in);
        byte[] buf;
        try {
            int n = (first << 24) | (d.readUnsignedByte() << 16) | (d.readUnsignedByte() << 8) | d.readUnsignedByte();
            if (n <= 0 || n > MAX) throw new IOException("bad frame length " + n);
            buf = new byte[n];
            d.readFully(buf);
        } catch (EOFException e) {
            throw new IOException("cut frame");
        }
        try {
            return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(buf)).toString();
        } catch (CharacterCodingException e) {
            throw new IOException("not UTF-8");
        }
    }

    static void write(OutputStream out, String text) throws IOException {
        byte[] body = text.getBytes(StandardCharsets.UTF_8);
        if (body.length == 0 || body.length > MAX) throw new IOException("frame too big");
        out.write(ByteBuffer.allocate(4 + body.length).putInt(body.length).put(body).array());
        out.flush();
    }
}
