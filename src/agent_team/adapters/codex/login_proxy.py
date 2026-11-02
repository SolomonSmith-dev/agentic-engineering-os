"""Codex login mode: a host-side proxy that holds your ChatGPT login so the sandbox never sees it.

`codex login` keeps a ChatGPT OAuth login in `$CODEX_HOME/auth.json` (`~/.codex`
by default). Mounting that file in the run's srt sandbox would hand it to every
command the reviewer runs (issue #12), so it stays denied. Instead the
orchestrator, outside srt, starts this proxy for the length of the run:

- Codex runs in API-key mode against `openai_base_url = http://127.0.0.1:<port>/backend-api/codex`,
  with a random per-run key as `CODEX_API_KEY`. That key opens this proxy and nothing else.
- The proxy checks the key, swaps in the login's access token and `chatgpt-account-id`, and
  forwards the request to `https://chatgpt.com/backend-api/codex`. srt allows only
  `127.0.0.1:<port>`, so neither the login nor chatgpt.com is reachable from the sandbox.
- The proxy never refreshes the login or writes it anywhere: refreshing and saving it stays
  Codex's own job on your machine. A run is refused when the access token is within
  `EXPIRY_MARGIN_S` of expiry, and a 401 from the backend makes the proxy re-read `auth.json`
  once (Codex outside the run may have refreshed it) before passing the 401 on.

The login is read only here, held in memory, and never logged, recorded, written or passed on.

Routes, headers and `auth.json`'s shape were read from the openai/codex
source (commit 67f0994, 2026-10-31: `login/src/auth/manager.rs`, `login/src/auth/storage.rs`,
`model-provider-info/src/lib.rs`, `model-provider/src/bearer_auth_provider.rs`, `core/src/client.rs`).
No real Codex or ChatGPT backend has run this code.
"""

import base64
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

LOGIN_FILE = "auth.json"

UPSTREAM = "https://chatgpt.com"

# Only the Codex backend is forwarded; any other path on chatgpt.com is refused.
PATH_PREFIX = "/backend-api/codex/"
BASE_PATH = PATH_PREFIX.rstrip("/")

# A run is refused when the access token expires sooner than this; Codex itself refreshes at 5 minutes.
EXPIRY_MARGIN_S = 10 * 60

UPSTREAM_TIMEOUT_S = 600
CHUNK = 64 * 1024

# Hop-by-hop headers, and the ones the proxy sets itself, are never forwarded in either direction.
HOP_HEADERS = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
     "trailer", "transfer-encoding", "upgrade", "host", "content-length"}
)  # fmt: skip
CREDENTIAL_HEADERS = frozenset({"authorization", "chatgpt-account-id", "openai-organization", "openai-project"})

AUTH_CLAIMS = "https://api.openai.com/auth"


class LoginError(RuntimeError):
    """No usable `codex login` was found. The message says what to do."""


def login_path(env: dict[str, str], home: Path) -> Path:
    """Where Codex keeps its login: `$CODEX_HOME/auth.json` when CODEX_HOME is an absolute path, else ~/.codex."""
    codex_home = env.get("CODEX_HOME", "").strip()
    if codex_home and Path(codex_home).expanduser().is_absolute():
        return Path(codex_home).expanduser() / LOGIN_FILE
    return home / ".codex" / LOGIN_FILE


def _jwt_claims(token: str) -> dict[str, Any]:
    """A JWT's payload, unverified: only its expiry and account id are read, and the backend checks it."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    account_id: str | None

    def expires_at(self) -> float | None:
        exp = _jwt_claims(self.access_token).get("exp")
        return float(exp) if isinstance(exp, int | float) else None


def _tokens(data: Any) -> Tokens | None:
    """The ChatGPT tokens in a parsed auth.json, or None when it holds no ChatGPT login."""
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        return None
    tokens = data["tokens"]
    access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
    if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
        return None
    account = tokens.get("account_id")
    if not isinstance(account, str) or not account:
        id_token = tokens.get("id_token")
        auth = _jwt_claims(id_token).get(AUTH_CLAIMS) if isinstance(id_token, str) else None
        account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return Tokens(access, refresh, account if isinstance(account, str) and account else None)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def has_login(path: Path) -> bool:
    """True when `path` holds a ChatGPT login. Only its presence is reported, never a value."""
    return _tokens(_read(path)) is not None


class LoginProxy:
    """The proxy for one run. `start` binds 127.0.0.1 on a free port; `close` stops it."""

    def __init__(self, path: Path, *, upstream: str | None = None) -> None:
        tokens = _tokens(_read(path))
        if tokens is None:
            raise LoginError(f"no ChatGPT login in {path}; run `codex login`, or set CODEX_API_KEY")
        expires = tokens.expires_at()
        if expires is not None and expires - time.time() < EXPIRY_MARGIN_S:
            # agent-team never refreshes or saves the login (no secret is persisted); Codex does both.
            raise LoginError(f"the Codex login in {path} has expired or expires within minutes; run `codex login` again")
        self.path = path
        self.upstream = (upstream or UPSTREAM).rstrip("/")
        self.run_key = secrets.token_urlsafe(32)
        self._tokens = tokens
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self._server is not None, "the proxy is not started"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        """Codex's `openai_base_url` for the run."""
        return f"http://127.0.0.1:{self.port}{BASE_PATH}"

    @property
    def allowed_domain(self) -> str:
        """The one srt network entry the run needs: this port on the host's loopback."""
        return f"127.0.0.1:{self.port}"

    def start(self) -> "LoginProxy":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="codex-login-proxy", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # --- tokens -----------------------------------------------------------------------

    def access(self, *, stale: str | None = None) -> Tokens:
        """Tokens to send now; after a 401 for `stale`, whatever auth.json holds now, if Codex has refreshed it."""
        with self._lock:
            if stale is not None and stale == self._tokens.access_token:
                on_disk = _tokens(_read(self.path))
                if on_disk is not None:
                    self._tokens = on_disk
            return self._tokens

    def authorized(self, header: str | None) -> bool:
        return header is not None and hmac.compare_digest(header.encode(), f"Bearer {self.run_key}".encode())


@contextmanager
def running(path: Path, **kwargs: str) -> Iterator[LoginProxy]:
    proxy = LoginProxy(path, **kwargs).start()
    try:
        yield proxy
    finally:
        proxy.close()


def _allowed_path(raw: str) -> bool:
    path = urlsplit(raw).path
    return path.startswith(PATH_PREFIX) and ".." not in path.split("/") and "%" not in path


def _handler(proxy: LoginProxy) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass  # never log: request lines and headers are the run's, not the operator's

        def do_GET(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def _refuse(self, code: int, message: str) -> None:
            body = json.dumps({"error": {"message": message}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _body(self) -> bytes | None:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                parts = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        return b"".join(parts)
                    parts.append(self.rfile.read(size))
                    self.rfile.readline()
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else None

        def _forward(self) -> None:
            if self.headers.get("Upgrade", "").lower() == "websocket":
                # Codex answers 426 by switching to HTTP for the rest of the session (client.rs).
                self._refuse(426, "this proxy speaks HTTP only")
                return
            if not proxy.authorized(self.headers.get("Authorization")):
                self._refuse(401, "not this run's key")
                return
            if not _allowed_path(self.path):
                self._refuse(403, "only the Codex backend is forwarded")
                return
            body = self._body()
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS | CREDENTIAL_HEADERS}
            try:
                tokens = proxy.access()
                response = self._send(tokens, headers, body)
                if response.status == 401:
                    response.close()
                    fresh = proxy.access(stale=tokens.access_token)
                    if fresh.access_token == tokens.access_token:
                        self._refuse(401, "the ChatGPT backend refused the Codex login; run `codex login` again")
                        return
                    response = self._send(fresh, headers, body)
            except (urllib.error.URLError, OSError):
                self._refuse(502, "the Codex backend could not be reached")
                return
            with response:
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in HOP_HEADERS | {"set-cookie"}:
                        self.send_header(key, value)
                length = response.headers.get("Content-Length")
                if length is not None:
                    self.send_header("Content-Length", length)
                self.send_header("Connection", "close")  # a streamed body without a length ends at close
                self.end_headers()
                read = getattr(response, "read1", None) or response.read  # read1: an SSE event as it arrives
                while chunk := read(CHUNK):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            self.close_connection = True

        def _send(self, tokens: Tokens, headers: dict[str, str], body: bytes | None) -> Any:
            # unverified: that the ChatGPT backend accepts the request Codex sends in API-key mode once these
            # two headers are set, as it does from Codex's own ChatGPT mode (BearerAuthProvider).
            out = dict(headers)
            out["Authorization"] = f"Bearer {tokens.access_token}"
            if tokens.account_id:
                out["chatgpt-account-id"] = tokens.account_id
            request = urllib.request.Request(proxy.upstream + self.path, data=body, method=self.command, headers=out)
            try:
                return urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S)
            except urllib.error.HTTPError as exc:
                return exc.fp or exc  # an error answer is still the backend's answer; pass it through

    return Handler
