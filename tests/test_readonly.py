"""The read-only rule, enforced twice:

1. A code scan of waterworks_desk/sources/: no write-style calls, no HTTP methods other than
   GET/HEAD, no files opened for writing, no imports of client libraries' write helpers.
2. Every network adapter runs against a trap server that records any write; one write fails.

    python -m pytest tests -q
"""
import ast
import urllib.request
from pathlib import Path

from helpers import ROOT, make_env  # noqa: F401
from mock_plant import HttpTrap, ModbusTrap

from waterworks_desk import sources

SOURCES = ROOT / "waterworks_desk" / "sources"
FORBIDDEN_CALLS = {
    "write", "writelines", "write_register", "write_registers", "write_coil", "write_coils",
    "write_multiple_registers", "write_single_register", "mask_write_register", "readwrite_registers",
    "write_value", "write_values", "write_attribute", "write_attributes", "set_value", "set_attribute",
    "call_method", "call", "publish", "applyEdits", "apply_edits", "addFeatures", "updateFeatures",
    "deleteFeatures", "post", "put", "patch", "delete", "send_command", "operate", "direct_operate",
    "select_and_operate", "unlink", "rename", "replace", "rmdir", "mkdir", "touch", "write_text", "write_bytes",
}
ALLOWED_METHODS = {"GET", "HEAD"}
WRITE_MODES = set("wax+")


def scan(path):
    """Return a list of (line, problem) for one source file."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            # str.replace(a, b) and datetime.replace(tzinfo=...) are harmless and common: flag only
            # the file-moving forms, Path.replace(target) (one argument) and os.replace(src, dst)
            if name == "replace" and isinstance(f, ast.Attribute):
                owner = f.value.id if isinstance(f.value, ast.Name) else None
                if not (owner in ("os", "shutil") or (len(node.args) == 1 and not node.keywords)):
                    name = None
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "shutil":
                problems.append((node.lineno, f"calls shutil.{f.attr}()"))
            if name in FORBIDDEN_CALLS:
                problems.append((node.lineno, f"calls {name}()"))
            if name == "open":
                mode = None
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if mode and WRITE_MODES & set(str(mode)):
                    problems.append((node.lineno, f"opens a file for writing (mode {mode!r})"))
            for kw in node.keywords:
                if kw.arg == "method" and isinstance(kw.value, ast.Constant) and str(kw.value.value).upper() not in ALLOWED_METHODS:
                    problems.append((node.lineno, f"uses HTTP method {kw.value.value}"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            problems.append((node.lineno, f"mentions HTTP method {node.value}"))
    return problems


def test_sources_have_no_write_calls():
    found = []
    for f in sorted(SOURCES.glob("*.py")):
        found += [f"{f.name}:{ln}: {p}" for ln, p in scan(f)]
    assert not found, "Write path found in sources/ (the console must never write to a source):\n" + "\n".join(found)


def test_scanner_catches_writes(tmp_path):
    """The scanner itself must catch the things it claims to."""
    bad = tmp_path / "bad.py"
    bad.write_text("import urllib.request\n"
                   "def go(client, f):\n"
                   "    client.write_register(1, 2)\n"
                   "    urllib.request.Request('http://x', data=b'1', method='POST')\n"
                   "    open('x.csv', 'w')\n"
                   "    f.write('x')\n"
                   "    client.publish('plant/pump1', b'1')\n"
                   "    from pathlib import Path\n"
                   "    Path('a').replace('b')\n"
                   "    'text'.replace('a', 'b')\n")
    got = {p for _, p in scan(bad)}
    assert "calls write_register()" in got
    assert any("POST" in p for p in got)
    assert any("opens a file for writing" in p for p in got)
    assert "calls write()" in got and "calls publish()" in got
    assert sum(p == "calls replace()" for _, p in scan(bad)) == 1   # the Path move, not the string edit


def test_http_trap_records_writes():
    trap = HttpTrap({"/status": {"ok": 1}})
    try:
        assert urllib.request.urlopen(trap.url + "/status").read() == b'{"ok": 1}'
        assert not trap.violations
        req = urllib.request.Request(trap.url + "/pump/1", data=b"{}", method="PUT")
        try:
            urllib.request.urlopen(req)
        except Exception:
            pass
        assert trap.violations == [("PUT", "/pump/1")]
    finally:
        trap.close()


def test_modbus_trap_records_writes():
    trap = ModbusTrap()
    try:
        r = trap.request(3, 0, 4)            # read 4 holding registers: fine
        assert r[7] == 3 and not trap.violations
        r = trap.request(6, 10, 1234)        # write single register: a violation
        assert r[7] == 6 | 0x80
        assert trap.violations == [("function", 6)]
    finally:
        trap.close()


def test_every_network_adapter_passes_the_trap():
    """Each adapter with network = True must provide trap_test(trap) and make no writes.
    MQTT, OPC UA and historian adapters (M4) are held to this from their first commit."""
    checked = []
    for name, cls in sources.ADAPTERS.items():
        if not getattr(cls, "network", False):
            continue
        assert hasattr(cls, "trap_test"), f"network adapter {name} has no trap_test"
        for Trap in (HttpTrap, ModbusTrap):
            trap = Trap()
            try:
                cls.trap_test(trap)
                assert not trap.violations, f"{name} tried to write: {trap.violations}"
                if getattr(trap, "url", None):
                    assert trap.reads, f"{name} didn't read anything from the trap"
            finally:
                trap.close()
        checked.append(name)
    assert {"open_meteo", "stream_edm", "ea_rain"} <= set(checked)
