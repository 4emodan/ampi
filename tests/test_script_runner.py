import base64
import json
import os
import subprocess
import sys

import pytest

from ampi_tools import script_runner as sr


def b64(s: str | bytes) -> str:
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()


def call(code, input=None, space=None, timeout=20):
    req = {"code": code, "input": input or {}, "space": space or {}, "timeout": timeout}
    return sr.handle(json.dumps(req).encode())


def test_normal_run_gets_input_and_prints_output():
    r = call("import json,sys\nd=json.load(sys.stdin)\nprint(json.dumps({'twice': d['n']*2}))", {"n": 21})
    assert r["ok"] and r["output"] == {"twice": 42} and r["error"] is None
    assert r["space_changes"] == {}


def test_stop_output_passes_through():
    r = call("import json\nprint(json.dumps({'stop': True}))")
    assert r["ok"] and r["output"] == {"stop": True}


def test_space_add_change_delete_and_unchanged():
    code = (
        "import json, os\n"
        "open('space/new.txt','w').write('n')\n"
        "open('space/keep.txt','w').write('changed')\n"
        "os.remove('space/gone.txt')\n"
        "os.makedirs('space/sub', exist_ok=True)\n"
        "open('space/sub/x.json','w').write('{}')\n"
        "print('{}')\n"
    )
    space = {"keep.txt": b64("old"), "gone.txt": b64("bye"), "same.txt": b64("same")}
    r = call(code, space=space)
    assert r["ok"], r
    ch = r["space_changes"]
    assert base64.b64decode(ch["new.txt"]) == b"n"
    assert base64.b64decode(ch["keep.txt"]) == b"changed"
    assert ch["gone.txt"] is None
    assert base64.b64decode(ch["sub/x.json"]) == b"{}"
    assert "same.txt" not in ch


def test_script_reads_space_files():
    r = call("import json\nprint(json.dumps({'v': open('space/a/b.txt').read()}))", space={"a/b.txt": b64("hi")})
    assert r["output"] == {"v": "hi"}


def test_bad_json_output():
    r = call("print('not json')")
    assert not r["ok"] and "JSON object" in r["error"]
    r = call("print('[1, 2]')")
    assert not r["ok"] and "not a list" in r["error"]


def test_nonzero_exit_and_stderr_capture():
    r = call("import sys\nsys.stderr.write('boom happened\\n')\nraise SystemExit(3)")
    assert not r["ok"] and "exit code 3" in r["error"]
    assert "boom happened" in r["stderr"]


def test_stderr_kept_on_success():
    r = call("import sys\nsys.stderr.write('debug line')\nprint('{}')")
    assert r["ok"] and "debug line" in r["stderr"]


def test_timeout():
    r = call("import time\ntime.sleep(30)", timeout=1)
    assert not r["ok"] and "longer than 1 s" in r["error"]


def test_oversize_output():
    r = call(f"print('\"' + 'x' * {sr.MAX_STDOUT + 10} + '\"')")
    assert not r["ok"] and "more than" in r["error"]


@pytest.mark.parametrize("name", ["../x", "/etc/passwd", "a/../../b", "", ".hidden", "a\\b", "a/b/c/d/e"])
def test_bad_space_names_in_request(name):
    r = call("print('{}')", space={name: b64("x")})
    assert not r["ok"] and "bad space file name" in r["error"]


def test_bad_base64_and_file_folder_clash():
    r = call("print('{}')", space={"a.txt": "!!!"})
    assert not r["ok"] and "base64" in r["error"]
    r = call("print('{}')", space={"a": b64("x"), "a/b": b64("y")})
    assert not r["ok"] and "parent folder" in r["error"]


def test_request_validation():
    assert "not JSON" in sr.handle(b"{nope")["error"]
    assert "object" in sr.handle(b"[1]")["error"]
    assert "code" in sr.handle(json.dumps({"code": ""}).encode())["error"]
    assert "timeout" in sr.handle(json.dumps({"code": "x", "timeout": 999}).encode())["error"]
    assert "input" in sr.handle(json.dumps({"code": "x", "input": [1]}).encode())["error"]


def test_bad_file_name_created_by_script():
    r = call("open('space/.secret','w').write('x')\nprint('{}')")
    assert not r["ok"] and "bad space file name" in r["error"]


@pytest.mark.skipif(os.name != "posix", reason="symlinks need POSIX")
def test_symlink_created_by_script_refused():
    r = call("import os\nos.symlink('/etc/passwd', 'space/link')\nprint('{}')")
    assert not r["ok"] and "link" in r["error"]


@pytest.mark.skipif(os.name != "posix", reason="special files need POSIX")
def test_fifo_created_by_script_refused():
    r = call("import os\nos.mkfifo('space/f')\nprint('{}')")
    assert not r["ok"] and "special file" in r["error"]


@pytest.mark.skipif(os.name != "posix", reason="process groups need POSIX")
def test_background_children_are_killed():
    r = call("import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             "print('{}')")
    assert r["ok"]


def test_isolated_mode_ignores_cwd_modules():
    # A json.py next to main.py must not shadow the standard library (python -I keeps the script dir off sys.path).
    code = ("open('json.py', 'w').write('raise SystemExit(9)')\n"
            "import importlib; importlib.invalidate_caches()\n"
            "import json\nprint(json.dumps({'ok': True}))")
    r = call(code)
    assert r["ok"] and r["output"] == {"ok": True}


def test_main_speaks_one_json_line():
    req = json.dumps({"code": "print('{\"a\": 1}')", "input": {}}) + "\n"
    p = subprocess.run([sys.executable, "-I", sr.__file__], input=req.encode(), capture_output=True, timeout=60)
    lines = p.stdout.decode().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["output"] == {"a": 1}
