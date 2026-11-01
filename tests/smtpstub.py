"""A tiny SMTP server on 127.0.0.1 for exercising the REAL email code path.

modes: ok | no_starttls | bad_greeting | silent (accepts, never greets) | auth_reject | data_reject
"""
from __future__ import annotations

import base64
import socket
import ssl
import threading


class SMTPStub:
    def __init__(self, mode="ok", tls: tuple[str, str] | None = None, require_tls_for_auth=True):
        self.mode, self.messages, self.auth, self.events = mode, [], [], []
        self.ctx = None
        if tls:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(*tls)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self._stop = False
        self.t = threading.Thread(target=self._accept, daemon=True)
        self.t.start()

    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass

    def _accept(self):
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            if self.mode == "silent":
                threading.Event().wait(5)
                return
            f = c.makefile("rwb", buffering=0)
            send = lambda t: f.write((t + "\r\n").encode())  # noqa: E731
            if self.mode == "bad_greeting":
                send("554 no service here")
                return
            send("220 stub ESMTP")
            tls = False
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.decode(errors="replace").strip()
                up = cmd.upper()
                self.events.append(("tls" if tls else "plain", up.split()[0] if up else ""))
                if up.startswith("EHLO"):
                    send("250-stub")
                    if self.ctx and self.mode != "no_starttls" and not tls:
                        send("250-STARTTLS")
                    send("250 AUTH PLAIN LOGIN" if tls or not self.ctx else "250 8BITMIME")
                elif up == "STARTTLS":
                    send("220 go ahead")
                    c = self.ctx.wrap_socket(c, server_side=True)
                    f = c.makefile("rwb", buffering=0)
                    send = lambda t, f=f: f.write((t + "\r\n").encode())  # noqa: E731
                    tls = True
                elif up.startswith("AUTH"):
                    parts = cmd.split()
                    if parts[1].upper() == "PLAIN":
                        raw = base64.b64decode(parts[2]) if len(parts) > 2 else b""
                        self.auth.append(raw.split(b"\0")[1:])
                    else:
                        send("334 VXNlcm5hbWU6")
                        u = base64.b64decode(f.readline().strip())
                        send("334 UGFzc3dvcmQ6")
                        p = base64.b64decode(f.readline().strip())
                        self.auth.append([u, p])
                    send("535 5.7.8 bad credentials" if self.mode == "auth_reject" else "235 ok")
                elif up.startswith(("MAIL", "RCPT")):
                    send("250 ok")
                elif up == "DATA":
                    send("354 end with .")
                    body = []
                    while True:
                        ln = f.readline()
                        if ln in (b".\r\n", b""):
                            break
                        body.append(ln.decode(errors="replace"))
                    self.messages.append(("tls" if tls else "plain", "".join(body)))
                    send("554 rejected" if self.mode == "data_reject" else "250 queued")
                elif up == "QUIT":
                    send("221 bye")
                    return
                else:
                    send("250 ok")
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
