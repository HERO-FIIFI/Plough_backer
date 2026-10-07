"""Minimal credential setup form; designed for localhost behind an HTTPS reverse proxy."""

import html
import ssl
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from plough_backer.account_credentials import SetupLinkService
from plough_backer.exceptions import SetupTokenInvalid

_MAX_BODY = 16 * 1024


def _page(title: str, content: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui;max-width:32rem;margin:3rem auto;padding:1rem}"
        "label{display:block;margin-top:1rem}input{box-sizing:border-box;width:100%;"
        "padding:.7rem}button{margin-top:1.5rem;padding:.8rem 1.2rem}</style>"
        f"</head><body><h1>{html.escape(title)}</h1>{content}</body></html>"
    ).encode()


_FORM = """
<p>This one-time form expires shortly. The password is encrypted on this Windows VPS.</p>
<form method="post" autocomplete="off">
<label>MT5 login<input name="login" inputmode="numeric" required maxlength="20"></label>
<label>Broker server<input name="server" required maxlength="200"></label>
<label>MT5 password<input name="password" type="password" required maxlength="1000"></label>
<button type="submit">Save credentials</button>
</form>
"""


class CredentialSetupServer:
    def __init__(
        self,
        service: SetupLinkService,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        tls_cert: Path | None = None,
        tls_key: Path | None = None,
    ) -> None:
        if (tls_cert is None) != (tls_key is None):
            raise ValueError("TLS certificate and key must be configured together")
        self._service = service
        self._host = host
        self._requested_port = port
        self._tls_cert = tls_cert
        self._tls_key = tls_key
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._server is None:
            return self._requested_port
        return int(self._server.server_address[1])

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        service = self._service

        class Handler(BaseHTTPRequestHandler):
            server_version = "PloughBacker"
            sys_version = ""

            def log_message(self, _format: str, *args: object) -> None:
                return  # access logs could expose the one-time token path

            def _token(self) -> str | None:
                parts = urlsplit(self.path).path.split("/")
                if len(parts) != 3 or parts[1] != "setup" or not parts[2]:
                    return None
                token = parts[2]
                return token if len(token) <= 200 else None

            def _send(self, status: HTTPStatus, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
                )
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                token = self._token()
                if token is None:
                    self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<p>Invalid path.</p>"))
                    return
                if not service.is_valid(token):
                    self._send(
                        HTTPStatus.GONE,
                        _page("Link unavailable", "<p>This setup link expired or was used.</p>"),
                    )
                    return
                self._send(HTTPStatus.OK, _page("Connect MT5 account", _FORM))

            def do_POST(self) -> None:
                token = self._token()
                if token is None:
                    self._send(HTTPStatus.NOT_FOUND, _page("Not found", "<p>Invalid path.</p>"))
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > _MAX_BODY:
                    self._send(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        _page("Invalid request", "<p>The submitted form is invalid.</p>"),
                    )
                    return
                try:
                    fields = parse_qs(
                        self.rfile.read(length).decode("utf-8"),
                        keep_blank_values=True,
                        max_num_fields=3,
                    )
                    service.submit(
                        token,
                        login=fields.get("login", [""])[0],
                        server=fields.get("server", [""])[0],
                        password=fields.get("password", [""])[0],
                    )
                except SetupTokenInvalid:
                    self._send(
                        HTTPStatus.GONE,
                        _page("Link unavailable", "<p>This setup link expired or was used.</p>"),
                    )
                    return
                except (UnicodeDecodeError, ValueError) as exc:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _page("Check the form", f"<p>{html.escape(str(exc))}</p>{_FORM}"),
                    )
                    return
                self._send(
                    HTTPStatus.OK,
                    _page(
                        "Credentials saved",
                        "<p>The encrypted credentials were saved. You may close this page.</p>",
                    ),
                )

        return Handler

    def start(self) -> None:
        if self._server is not None:
            return
        server = ThreadingHTTPServer((self._host, self._requested_port), self._handler())
        if self._tls_cert is not None and self._tls_key is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self._tls_cert, self._tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="credential-setup",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
        self._thread = None

    def __enter__(self) -> "CredentialSetupServer":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
