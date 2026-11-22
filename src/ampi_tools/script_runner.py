"""Runs one watcher script in the sandbox (watchers v2, part B).

Runs as user `ampiscript` in a socket-activated `ampi-script@` instance (deploy/ampi-script@.service): the
connection is stdin/stdout, the unit has no network, no address families, no home directories and only a
private /tmp. Standard library only; installed as /usr/local/lib/ampi-script/script_runner.py.

Protocol: one JSON line in, one JSON line out.
  in:  {"code": "<python source>", "input": {...}, "space": {path: base64}, "timeout": seconds (<= 60)}
  out: {"ok": bool, "output": {...} | null, "space_changes": {path: base64 | null}, "stderr": "...",
        "error": "..." | null, "duration_ms": n}

The script gets `input` as JSON on stdin, its space files under ./space/, and must print one JSON object.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,63}){0,3}$")
MAX_REQUEST = 40 * 1024 * 1024  # base64 space (20 MB) + input + code
MAX_CODE = 512 * 1024
MAX_FILE = 5 * 1024 * 1024
MAX_SPACE = 20 * 1024 * 1024
MAX_FILES = 500
MAX_STDOUT = 1024 * 1024
STDERR_TAIL = 4000
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 60
MEMORY = 200 * 1024 * 1024
POSIX = os.name == "posix"


class Refused(Exception):
    """The request or the script's result breaks a rule; the message says which."""


def check_path(p: object) -> str:
    if not isinstance(p, str) or not PATH_RE.fullmatch(p) or ".." in p.split("/"):
        raise Refused(f"bad space file name {p!r}")
    return p


def _decode_space(space: object) -> dict[str, bytes]:
    if space is None:
        return {}
    if not isinstance(space, dict) or len(space) > MAX_FILES:
        raise Refused(f"space must be an object of at most {MAX_FILES} files")
    out, total = {}, 0
    for path, data in space.items():
        check_path(path)
        if not isinstance(data, str):
            raise Refused(f"{path}: file contents must be base64 text")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise Refused(f"{path}: bad base64") from None
        if len(raw) > MAX_FILE:
            raise Refused(f"{path}: larger than {MAX_FILE // 1024 // 1024} MB")
        total += len(raw)
        out[path] = raw
    if total > MAX_SPACE:
        raise Refused(f"space larger than {MAX_SPACE // 1024 // 1024} MB")
    # A file and a folder with the same name can't both exist.
    names = set(out)
    for path in out:
        parts = path.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in names:
                raise Refused(f"{path}: a parent folder has the name of a file")
    return out


def parse_request(line: bytes) -> tuple[str, dict, dict[str, bytes], float]:
    try:
        req = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise Refused("the request is not JSON") from None
    if not isinstance(req, dict):
        raise Refused("the request must be a JSON object")
    code = req.get("code")
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > MAX_CODE:
        raise Refused(f"code must be Python source of at most {MAX_CODE // 1024} KB")
    data = req.get("input", {})
    if not isinstance(data, dict):
        raise Refused("input must be an object")
    timeout = req.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT:
        raise Refused(f"timeout must be 1-{MAX_TIMEOUT} seconds")
    return code, data, _decode_space(req.get("space")), float(timeout)


def write_space(root: str, files: dict[str, bytes]) -> None:
    for path, data in files.items():
        target = os.path.join(root, *path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)


def read_space(root: str) -> dict[str, bytes]:
    """Everything under space/ after the run; refuses links, special files, bad names and oversize."""
    out: dict[str, bytes] = {}
    total = 0
    if not os.path.lexists(root):
        return out
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise Refused("space/ was replaced by something that is not a folder")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode) or (os.name == "nt" and os.path.islink(full)):
                raise Refused(f"the script left a link in space/ ({rel}); links aren't allowed")
            if stat.S_ISDIR(st.st_mode):
                if not PATH_RE.fullmatch(rel) or ".." in rel.split("/"):
                    raise Refused(f"bad folder name in space/: {rel!r}")
                continue
            if not stat.S_ISREG(st.st_mode):
                raise Refused(f"the script left a special file in space/ ({rel})")
            check_path(rel)
            if st.st_size > MAX_FILE:
                raise Refused(f"{rel}: larger than {MAX_FILE // 1024 // 1024} MB")
            total += st.st_size
            if total > MAX_SPACE:
                raise Refused(f"space larger than {MAX_SPACE // 1024 // 1024} MB")
            if len(out) >= MAX_FILES:
                raise Refused(f"more than {MAX_FILES} files in space/")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(full, flags)
            with os.fdopen(fd, "rb") as f:
                out[rel] = f.read(MAX_FILE + 1)
            if len(out[rel]) > MAX_FILE:
                raise Refused(f"{rel}: larger than {MAX_FILE // 1024 // 1024} MB")
    return out


def space_changes(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, str | None]:
    changes: dict[str, str | None] = {}
    digest = {p: hashlib.sha256(d).digest() for p, d in before.items()}
    for path, data in after.items():
        if digest.get(path) != hashlib.sha256(data).digest():
            changes[path] = base64.b64encode(data).decode("ascii")
    for path in before:
        if path not in after:
            changes[path] = None
    return changes


def _limits() -> None:  # runs in the child before exec (POSIX only)
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (MAX_TIMEOUT + 5, MAX_TIMEOUT + 5))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY, MEMORY))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_SPACE, MAX_SPACE))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if os.environ.get("AMPI_SCRIPT_SANDBOX") == "1":  # only in the real unit: counts the whole ampiscript user
        resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))


def _kill(proc: subprocess.Popen) -> None:
    try:
        if POSIX:
            os.killpg(proc.pid, 9)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - STDERR_TAIL * 4))
            return f.read().decode("utf-8", "replace")[-STDERR_TAIL:]
    except OSError:
        return ""


def run(code: str, data: dict, space: dict[str, bytes], timeout: float) -> dict:
    started = time.monotonic()
    work = tempfile.mkdtemp(prefix="ampi-script-")
    try:
        cwd = os.path.join(work, "run")
        io_dir = os.path.join(work, "io")
        os.mkdir(cwd, 0o700)
        os.mkdir(io_dir, 0o700)
        space_root = os.path.join(cwd, "space")
        os.mkdir(space_root, 0o700)
        write_space(space_root, space)
        with open(os.path.join(cwd, "main.py"), "w", encoding="utf-8") as f:
            f.write(code)
        in_path, out_path, err_path = (os.path.join(io_dir, n) for n in ("stdin", "stdout", "stderr"))
        with open(in_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
               "PYTHONDONTWRITEBYTECODE": "1", "HOME": cwd, "TMPDIR": cwd}
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        with open(in_path, "rb") as fin, open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
            kwargs: dict = {"cwd": cwd, "env": env, "stdin": fin, "stdout": fout, "stderr": ferr}
            if POSIX:
                kwargs.update(start_new_session=True, preexec_fn=_limits)
            proc = subprocess.Popen([sys.executable, "-I", "-S", "main.py"], **kwargs)
            try:
                code_rc = proc.wait(timeout)
            except subprocess.TimeoutExpired:
                _kill(proc)
                return _reply(False, None, {}, _tail(err_path), f"the script took longer than {timeout:g} s",
                              started)
            if POSIX:  # anything the script left running in its group goes too
                try:
                    os.killpg(proc.pid, 9)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        stderr = _tail(err_path)
        if code_rc != 0:
            return _reply(False, None, {}, stderr, f"the script failed (exit code {code_rc})", started)
        if os.path.getsize(out_path) > MAX_STDOUT:
            return _reply(False, None, {}, stderr, f"the script printed more than {MAX_STDOUT // 1024} KB", started)
        with open(out_path, "rb") as f:
            raw = f.read()
        try:
            output = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _reply(False, None, {}, stderr, "the script must print exactly one JSON object", started)
        if not isinstance(output, dict):
            return _reply(False, None, {}, stderr, "the script must print a JSON object, not a list or value",
                          started)
        try:
            changes = space_changes(space, read_space(space_root))
        except Refused as e:
            return _reply(False, None, {}, stderr, str(e), started)
        return _reply(True, output, changes, stderr, None, started)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _reply(ok: bool, output, changes, stderr: str, error: str | None, started: float) -> dict:
    return {"ok": ok, "output": output, "space_changes": changes, "stderr": stderr, "error": error,
            "duration_ms": int((time.monotonic() - started) * 1000)}


def handle(line: bytes) -> dict:
    started = time.monotonic()
    try:
        code, data, space, timeout = parse_request(line)
        return run(code, data, space, timeout)
    except Refused as e:
        return _reply(False, None, {}, "", str(e), started)
    except Exception as e:  # never die without an answer
        return _reply(False, None, {}, "", f"sandbox error: {type(e).__name__}: {e}", started)


def main() -> None:
    try:
        line = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if len(line) > MAX_REQUEST:
            reply = _reply(False, None, {}, "", "request too large", time.monotonic())
        else:
            reply = handle(line)
    except Exception as e:
        reply = _reply(False, None, {}, "", f"sandbox error: {type(e).__name__}: {e}", time.monotonic())
    sys.stdout.write(json.dumps(reply, ensure_ascii=True) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
