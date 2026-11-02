"""Write traps: stand-ins for plant-side servers that answer reads and record any attempt to
write. Network adapters are run against these in tests/test_readonly.py; any recorded write
fails the build.

  HttpTrap    a historian or REST API: GET is answered; POST/PUT/PATCH/DELETE are violations
  ModbusTrap  a Modbus TCP RTU: function codes 3 and 4 (read registers) are answered; anything
              else (5, 6, 15, 16, 22, 23 are the writes) is a violation
"""
import json
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HttpTrap:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.violations, self.reads = [], []
        trap = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                trap.reads.append(self.path)
                body = json.dumps(trap.routes.get(self.path.split("?")[0], {})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _bad(self):
                trap.violations.append((self.command, self.path))
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_PUT = do_PATCH = do_DELETE = _bad

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class ModbusTrap:
    READS = {3, 4}

    def __init__(self):
        self.violations, self.reads = [], []
        trap = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    head = self._recv(7)
                    if not head:
                        return
                    tid, pid, length, unit = struct.unpack(">HHHB", head)
                    pdu = self._recv(length - 1)
                    if not pdu:
                        return
                    fc = pdu[0]
                    if fc in trap.READS:
                        addr, count = struct.unpack(">HH", pdu[1:5])
                        trap.reads.append((fc, addr, count))
                        data = bytes([fc, count * 2]) + b"\x00" * (count * 2)
                    else:
                        trap.violations.append(("function", fc))
                        data = bytes([fc | 0x80, 1])   # illegal function
                    self.request.sendall(struct.pack(">HHHB", tid, pid, len(data) + 1, unit) + data)

            def _recv(self, n):
                buf = b""
                while len(buf) < n:
                    chunk = self.request.recv(n - len(buf))
                    if not chunk:
                        return b""
                    buf += chunk
                return buf

        class S(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = S(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def request(self, fc, addr=0, value_or_count=1):
        """Send one Modbus request (used to prove the trap itself works)."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            pdu = struct.pack(">BHH", fc, addr, value_or_count)
            s.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
            return s.recv(260)

    def close(self):
        self.server.shutdown()
