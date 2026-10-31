"""Codex login mode: the host-side proxy that holds a `codex login` so the sandbox never sees it.

A fake ChatGPT backend and token endpoint stand in for chatgpt.com and
auth.openai.com; no test reaches either. The end-to-end test runs a fake `codex`
under real srt and has it call the proxy the way Codex would in API-key mode
against `openai_base_url`.
"""

import base64
import json
import os
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_codex_adapter import ALL_FLAGS, CANONICAL, events, only_run, repo, srt  # noqa: F401  (fixtures by name)
from typer.testing import CliRunner

from agent_team.adapters.codex import login_proxy
from agent_team.adapters.codex.invoke import CodexUnavailableError, auth_mode, has_auth
from agent_team.adapters.codex.login_proxy import LoginError, LoginProxy, has_login, login_path
from agent_team.cli import app

runner = CliRunner()

ACCOUNT = "acct-TEST"
REFRESH = "rt-LOGINCANARY-1"


def jwt(**claims: object) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def access(name: str, *, expires_in: float = 3600) -> str:
    return jwt(sub=name, exp=int(time.time() + expires_in))


def write_login(path: Path, *, access_token: str, refresh_token: str = REFRESH, account: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens = {"id_token": jwt(**{"https://api.openai.com/auth": {"chatgpt_account_id": ACCOUNT}}),
              "access_token": access_token, "refresh_token": refresh_token}  # fmt: skip
    if account:
        tokens["account_id"] = ACCOUNT
    path.write_text(json.dumps({"OPENAI_API_KEY": None, "tokens": tokens, "last_refresh": "2026-10-01T00:00:00Z"}))
    path.chmod(0o600)


class Backend:
    """A fake chatgpt.com: records each request, answers 401 to any token it was told is revoked."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.revoked: set[str] = set()
        self.refreshes: list[dict] = []
        self.next_access = access("refreshed")
        backend = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/oauth/token":
                    backend.refreshes.append(json.loads(body))
                    out = json.dumps({"access_token": backend.next_access, "refresh_token": "rt-ROTATED-2",
                                      "id_token": jwt(sub="new-id")}).encode()  # fmt: skip
                    self._answer(200, out, "application/json")
                    return
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                backend.requests.append({"path": self.path, "token": token, "body": body,
                                         "account": self.headers.get("chatgpt-account-id"),
                                         "headers": dict(self.headers)})  # fmt: skip
                if token in backend.revoked:
                    self._answer(401, b'{"error":"expired"}', "application/json")
                    return
                self._answer(200, b"event: response.completed\ndata: {}\n\n", "text/event-stream")

            def _answer(self, code: int, body: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def backend() -> Iterator[Backend]:
    fake = Backend()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


@pytest.fixture
def proxy(tmp_path: Path, backend: Backend) -> Iterator[LoginProxy]:
    write_login(tmp_path / ".codex" / "auth.json", access_token=access("current"))
    with login_proxy.running(tmp_path / ".codex" / "auth.json", upstream=backend.url,
                             refresh_url=f"{backend.url}/oauth/token") as running:  # fmt: skip
        yield running


def call(proxy: LoginProxy, path: str = "/responses", *, key: str | None = None, headers: dict | None = None):
    request = urllib.request.Request(
        proxy.base_url + path, data=b'{"model":"gpt"}', method="POST",
        headers={"Authorization": f"Bearer {key or proxy.run_key}", "Content-Type": "application/json", **(headers or {})},
    )  # fmt: skip
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --- finding the login ---------------------------------------------------------------


def test_the_login_is_found_where_codex_keeps_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert login_path({}, home) == home / ".codex" / "auth.json"
    assert login_path({"CODEX_HOME": str(tmp_path / "ch")}, home) == tmp_path / "ch" / "auth.json"
    assert login_path({"CODEX_HOME": "relative"}, home) == home / ".codex" / "auth.json"


def test_only_a_chatgpt_login_counts(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "auth.json"
    assert not has_login(path), "no file"
    path.parent.mkdir()
    path.write_text('{"OPENAI_API_KEY": "sk-proj-X", "tokens": null}')
    assert not has_login(path), "an API key saved by `codex login --with-api-key` is not a ChatGPT login"
    path.write_text("not json")
    assert not has_login(path)
    write_login(path, access_token=access("a"))
    assert has_login(path)


def test_the_api_key_wins_and_the_login_is_the_fallback(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with pytest.raises(CodexUnavailableError, match="set CODEX_API_KEY for this shell, or run `codex login`"):
        auth_mode({}, home)
    assert not has_auth("codex", {}, home)
    write_login(home / ".codex" / "auth.json", access_token=access("a"))
    assert auth_mode({}, home) == "login" and has_auth("codex", {}, home)
    assert auth_mode({"CODEX_API_KEY": "sk-proj-X"}, home) == "api_key"


def test_a_proxy_needs_a_login(tmp_path: Path) -> None:
    with pytest.raises(LoginError, match="run `codex login`, or set CODEX_API_KEY"):
        LoginProxy(tmp_path / "auth.json")


# --- the proxy -----------------------------------------------------------------------


def test_the_proxy_swaps_the_run_key_for_the_login(proxy: LoginProxy, backend: Backend) -> None:
    status, body = call(proxy, headers={"chatgpt-account-id": "acct-FORGED", "OpenAI-Organization": "org-X"})
    assert status == 200 and b"response.completed" in body
    [seen] = backend.requests
    assert seen["path"] == "/backend-api/codex/responses" and seen["body"] == b'{"model":"gpt"}'
    assert seen["token"] == proxy._tokens.access_token and seen["token"] != proxy.run_key
    assert seen["account"] == ACCOUNT, "the login's account, never one the sandbox sent"
    assert "OpenAI-Organization" not in seen["headers"]


def test_the_proxy_refuses_anything_but_its_run_key(proxy: LoginProxy, backend: Backend) -> None:
    assert call(proxy, key="guess")[0] == 401
    request = urllib.request.Request(proxy.base_url + "/responses", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as no_key:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10)
    assert no_key.value.code == 401
    assert backend.requests == [], "nothing reaches the backend without the key"


@pytest.mark.parametrize("path", ["/../../backend-api/me", "/%2e%2e/%2e%2e/backend-api/me"])
def test_the_proxy_forwards_only_the_codex_backend(proxy: LoginProxy, backend: Backend, path: str) -> None:
    assert call(proxy, path)[0] == 403
    request = urllib.request.Request(f"http://127.0.0.1:{proxy.port}/backend-api/me", method="GET",
                                     headers={"Authorization": f"Bearer {proxy.run_key}"})  # fmt: skip
    with pytest.raises(urllib.error.HTTPError) as outside:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10)
    assert outside.value.code == 403
    assert backend.requests == []


def test_a_websocket_upgrade_gets_426_so_codex_falls_back_to_http(proxy: LoginProxy) -> None:
    status, _ = call(proxy, headers={"Upgrade": "websocket", "Connection": "Upgrade"})
    assert status == 426


def test_a_401_refreshes_once_and_saves_the_rotated_tokens(proxy: LoginProxy, backend: Backend) -> None:
    path = proxy.path
    backend.revoked.add(proxy._tokens.access_token)
    status, _ = call(proxy)
    assert status == 200
    assert [r["token"] for r in backend.requests] == [*backend.revoked, backend.next_access]
    assert backend.refreshes == [{"client_id": login_proxy.CLIENT_ID, "grant_type": "refresh_token",
                                  "refresh_token": REFRESH}]  # fmt: skip
    saved = json.loads(path.read_text())
    assert saved["tokens"]["access_token"] == backend.next_access
    assert saved["tokens"]["refresh_token"] == "rt-ROTATED-2", "a rotated refresh token is kept, or the login breaks"
    assert saved["tokens"]["account_id"] == ACCOUNT and saved["OPENAI_API_KEY"] is None
    assert saved["last_refresh"] != "2026-10-01T00:00:00Z"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["auth.json"], "no temp file left behind"


def test_a_token_near_expiry_is_refreshed_before_it_is_sent(tmp_path: Path, backend: Backend) -> None:
    path = tmp_path / "auth.json"
    write_login(path, access_token=access("old", expires_in=60))
    with login_proxy.running(path, upstream=backend.url, refresh_url=f"{backend.url}/oauth/token") as running:
        assert call(running)[0] == 200
    assert [r["token"] for r in backend.requests] == [backend.next_access]


def test_a_login_codex_refreshed_meanwhile_is_used_rather_than_refreshed_again(
    proxy: LoginProxy, backend: Backend
) -> None:
    newer = access("from-codex")
    write_login(proxy.path, access_token=newer, refresh_token="rt-FROM-CODEX")
    backend.revoked.add(proxy._tokens.access_token)
    assert call(proxy)[0] == 200
    assert backend.refreshes == [] and backend.requests[-1]["token"] == newer
    assert json.loads(proxy.path.read_text())["tokens"]["refresh_token"] == "rt-FROM-CODEX"


def test_a_failed_refresh_says_to_log_in_again_without_the_token(tmp_path: Path, backend: Backend) -> None:
    path = tmp_path / "auth.json"
    write_login(path, access_token=access("old", expires_in=0))
    with login_proxy.running(path, upstream=backend.url, refresh_url=f"{backend.url}/nowhere") as running:
        status, body = call(running)
    assert status == 401 and b"run `codex login` again" in body
    assert REFRESH.encode() not in body
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == REFRESH, "the login on disk is untouched"


def test_the_account_id_falls_back_to_the_id_token(tmp_path: Path, backend: Backend) -> None:
    path = tmp_path / "auth.json"
    write_login(path, access_token=access("a"), account=False)
    with login_proxy.running(path, upstream=backend.url) as running:
        call(running)
    assert backend.requests[0]["account"] == ACCOUNT


# --- end to end under srt ------------------------------------------------------------

FAKE_LOGIN_CODEX = r'''#!{python}
"""Fake codex in login mode: calls openai_base_url as Codex would, then probes what srt lets through."""
import json, os, sys, urllib.request, urllib.error

argv = sys.argv[1:]
if argv == ["exec", "--help"]:
    print("\n".join({flags!r}))
    sys.exit(0)
config = dict(a.split("=", 1) for a in argv if "=" in a and a.split("=", 1)[0] == "openai_base_url")
base = json.loads(config["openai_base_url"])


def post(url, key):
    request = urllib.request.Request(url, data=b'{{"model":"m"}}', method="POST",
                                     headers={{"Authorization": "Bearer " + key, "Content-Type": "application/json"}})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError as exc:
        return str(exc)[:80]


def readable(path):
    try:
        open(path).read()
        return True
    except OSError:
        return False


seen = {{"argv": argv, "env": dict(os.environ), "stdin": sys.stdin.read(),
         "via_proxy": post(base + "/responses", os.environ["CODEX_API_KEY"]),
         "wrong_key": post(base + "/responses", "guess"),
         "backend_direct": post({backend!r} + "/backend-api/codex/responses", "guess"),
         "login_readable": readable(os.path.join(os.path.expanduser("~"), ".codex", "auth.json"))}}
with open(os.path.join(os.environ["TMPDIR"], "fake-codex.json"), "w") as out:
    json.dump(seen, out)
for event in {events!r}:
    print(json.dumps(event), flush=True)
'''


def test_a_logged_in_codex_reviewer_end_to_end(
    repo: Path,  # noqa: F811
    srt: str,  # noqa: F811
    backend: Backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    login = home / ".codex" / "auth.json"
    write_login(login, access_token=access("current"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(login_proxy, "UPSTREAM", backend.url)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(FAKE_LOGIN_CODEX.format(python=sys.executable, flags=ALL_FLAGS, backend=backend.url,
                                            events=events({"verdict": "approve", "findings": []})))  # fmt: skip
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    result = runner.invoke(app, ["run", "--agent", "independent-reviewer", "--runtime", "codex", "--repo", str(repo),
                                 "--canonical", str(CANONICAL), "Review the diff."])  # fmt: skip
    assert result.exit_code == 0, result.output
    run, transitions, _ = only_run(repo)
    assert "auth login" in (transitions[1].note or "")

    seen = json.loads((repo / ".agent-team" / "tmp" / run.run_id / "fake-codex.json").read_text())
    assert seen["via_proxy"] == 200, "Codex reaches the backend only through the login proxy"
    assert seen["wrong_key"] == 401
    assert seen["backend_direct"] != 200, "srt allows the proxy's port and nothing else on the host's loopback"
    assert not seen["login_readable"], "and the login itself stays unreadable"
    tokens = json.loads(login.read_text())["tokens"]
    assert not {tokens["access_token"], tokens["refresh_token"]} & set(seen["env"].values())
    assert seen["env"]["CODEX_HOME"].endswith("codex-home")
    base_url = next(a for a in seen["argv"] if a.startswith("openai_base_url="))
    assert base_url.endswith('/backend-api/codex"') and "127.0.0.1:" in base_url
    [request] = backend.requests
    assert request["token"] == tokens["access_token"] and request["account"] == ACCOUNT

    settings = json.loads((repo / ".agent-team" / "sandbox" / f"{run.run_id}.json").read_text())
    domains = settings["network"]["allowedDomains"]
    port = base_url.split("127.0.0.1:")[1].split("/")[0]
    assert f"127.0.0.1:{port}" in domains
    assert not {"api.openai.com", "chatgpt.com", "auth.openai.com"} & set(domains)
