"""Credential secrets are encrypted and setup links are short-lived and single-use."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from alembic import command
from sqlalchemy import Engine, select

from plough_backer.account_credentials import CredentialVault, SetupLinkService
from plough_backer.credential_server import CredentialSetupServer
from plough_backer.exceptions import SetupTokenInvalid
from plough_backer.persistence.database import alembic_config, make_engine, session_factory
from plough_backer.persistence.models import AccountCredential, AccountSetupToken


class FakeProtector:
    def protect(self, plaintext: str) -> bytes:
        return b"encrypted:" + plaintext[::-1].encode()

    def unprotect(self, ciphertext: bytes) -> str:
        return ciphertext.removeprefix(b"encrypted:").decode()[::-1]


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    url = f"sqlite:///{(tmp_path / 'credentials.db').as_posix()}"
    cfg = alembic_config(url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    value = make_engine(url)
    yield value
    value.dispose()


def test_vault_never_persists_plaintext_and_can_replace_credentials(engine: Engine) -> None:
    sessions = session_factory(engine)
    vault = CredentialVault(sessions, FakeProtector(), clock=lambda: NOW)

    vault.store("method-1", login=123, server="Broker-Demo", password="first-secret")

    with sessions.begin() as session:
        row = session.get(AccountCredential, "method-1")
        assert row is not None
        assert b"first-secret" not in row.encrypted_password
    loaded = vault.load("method-1")
    assert loaded is not None
    assert loaded.login == 123
    assert loaded.password.get_secret_value() == "first-secret"

    vault.store("method-1", login=456, server="Broker-Live", password="second-secret")
    replaced = vault.load("method-1")
    assert replaced is not None
    assert replaced.login == 456
    assert replaced.password.get_secret_value() == "second-secret"


def test_setup_link_stores_only_hash_and_is_single_use(engine: Engine) -> None:
    sessions = session_factory(engine)
    vault = CredentialVault(sessions, FakeProtector(), clock=lambda: NOW)
    saved: list[str] = []
    setup = SetupLinkService(
        sessions=sessions,
        vault=vault,
        base_url="https://setup.example.com",
        allowed_account_ids={"method-1"},
        on_saved=saved.append,
        clock=lambda: NOW,
        token_factory=lambda: "raw-one-time-token",
    )

    link = setup.issue("method-1", requested_by=42)

    assert link == "https://setup.example.com/setup/raw-one-time-token"
    with sessions.begin() as session:
        (row,) = session.scalars(select(AccountSetupToken)).all()
        assert row.token_hash != "raw-one-time-token"
        assert row.requested_by == "telegram:42"
    assert setup.is_valid("raw-one-time-token")

    account_id = setup.submit(
        "raw-one-time-token", login="123", server="Broker-Demo", password="secret"
    )

    assert account_id == "method-1"
    assert saved == ["method-1"]
    assert not setup.is_valid("raw-one-time-token")
    with pytest.raises(SetupTokenInvalid):
        setup.submit(
            "raw-one-time-token", login="123", server="Broker-Demo", password="secret"
        )


def test_setup_link_rejects_unknown_account_invalid_login_and_expiry(engine: Engine) -> None:
    sessions = session_factory(engine)
    current = [NOW]
    setup = SetupLinkService(
        sessions=sessions,
        vault=CredentialVault(sessions, FakeProtector(), clock=lambda: current[0]),
        base_url="https://setup.example.com/",
        allowed_account_ids={"method-1"},
        ttl=timedelta(minutes=10),
        clock=lambda: current[0],
        token_factory=lambda: "expiring-token",
    )

    with pytest.raises(ValueError, match="unknown account"):
        setup.issue("method-2", requested_by=42)
    setup.issue("method-1", requested_by=42)
    with pytest.raises(ValueError, match="login"):
        setup.submit("expiring-token", login="abc", server="Broker", password="secret")
    current[0] += timedelta(minutes=11)
    with pytest.raises(SetupTokenInvalid):
        setup.submit("expiring-token", login="123", server="Broker", password="secret")


@pytest.mark.parametrize("base_url", ["http://public.example.com", "ftp://example.com"])
def test_public_setup_url_requires_https(engine: Engine, base_url: str) -> None:
    sessions = session_factory(engine)
    with pytest.raises(ValueError, match="HTTPS"):
        SetupLinkService(
            sessions=sessions,
            vault=CredentialVault(sessions, FakeProtector()),
            base_url=base_url,
            allowed_account_ids={"method-1"},
        )


def test_setup_http_form_has_security_headers_and_consumes_token(engine: Engine) -> None:
    sessions = session_factory(engine)
    vault = CredentialVault(sessions, FakeProtector(), clock=lambda: NOW)
    setup = SetupLinkService(
        sessions=sessions,
        vault=vault,
        base_url="http://127.0.0.1",
        allowed_account_ids={"method-1"},
        clock=lambda: NOW,
        token_factory=lambda: "http-token",
    )
    setup.issue("method-1", requested_by=42)
    server = CredentialSetupServer(setup, host="127.0.0.1", port=0)
    server.start()
    url = f"http://127.0.0.1:{server.port}/setup/http-token"
    try:
        with urlopen(url, timeout=2) as response:
            body = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert 'type="password"' in body

        payload = urlencode(
            {"login": "123", "server": "Broker-Demo", "password": "form-secret"}
        ).encode()
        request = Request(url, data=payload, method="POST")
        with urlopen(request, timeout=2) as response:  # noqa: S310 - fixed loopback URL
            assert "Credentials saved" in response.read().decode()
        loaded = vault.load("method-1")
        assert loaded is not None
        assert loaded.password.get_secret_value() == "form-secret"

        with pytest.raises(HTTPError) as reused:
            urlopen(request, timeout=2)  # noqa: S310 - fixed loopback URL
        assert reused.value.code == 410
        reused.value.close()
    finally:
        server.stop()
