"""Encrypted MT5 credential vault and one-time setup link lifecycle."""

import ctypes
import hashlib
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse

from sqlalchemy.orm import Session, sessionmaker

from plough_backer.config import AccountCredentials
from plough_backer.enums import AuditEventType
from plough_backer.exceptions import ConfigurationError, SetupTokenInvalid
from plough_backer.persistence import repositories as repo
from plough_backer.persistence.models import AccountCredential, AccountSetupToken


class SecretProtector(Protocol):
    def protect(self, plaintext: str) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> str: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class WindowsDPAPIProtector:
    """Encrypt credentials for the Windows user running Plough Backer."""

    _NO_UI = 0x1

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
        return blob, buffer

    def protect(self, plaintext: str) -> bytes:
        if sys.platform != "win32":
            raise ConfigurationError("Windows DPAPI credential storage requires Windows")
        raw = plaintext.encode("utf-8")
        source, source_buffer = self._blob(raw)
        target = _DataBlob()
        try:
            ok = ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(source),
                ctypes.c_wchar_p("Plough Backer MT5 credential"),
                None,
                None,
                None,
                self._NO_UI,
                ctypes.byref(target),
            )
            if not ok:
                raise ctypes.WinError()
            return ctypes.string_at(target.pbData, target.cbData)
        finally:
            ctypes.memset(source_buffer, 0, len(raw))
            if target.pbData:
                ctypes.windll.kernel32.LocalFree(target.pbData)

    def unprotect(self, ciphertext: bytes) -> str:
        if sys.platform != "win32":
            raise ConfigurationError("Windows DPAPI credential storage requires Windows")
        source, source_buffer = self._blob(ciphertext)
        target = _DataBlob()
        try:
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(source), None, None, None, None, self._NO_UI, ctypes.byref(target)
            )
            if not ok:
                raise ctypes.WinError()
            plaintext = ctypes.string_at(target.pbData, target.cbData)
            try:
                return plaintext.decode("utf-8")
            finally:
                mutable = bytearray(plaintext)
                mutable[:] = b"\x00" * len(mutable)
        finally:
            ctypes.memset(source_buffer, 0, len(ciphertext))
            if target.pbData:
                ctypes.windll.kernel32.LocalFree(target.pbData)


class CredentialVault:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        protector: SecretProtector,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._protector = protector
        self._clock = clock

    def _store(
        self, session: Session, account_id: str, *, login: int, server: str, password: str
    ) -> None:
        encrypted = self._protector.protect(password)
        row = session.get(AccountCredential, account_id)
        now = self._clock()
        if row is None:
            session.add(
                AccountCredential(
                    account_id=account_id,
                    login=login,
                    server=server,
                    encrypted_password=encrypted,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.login = login
            row.server = server
            row.encrypted_password = encrypted
            row.updated_at = now
        repo.append_audit(
            session,
            AuditEventType.ACCOUNT_CREDENTIALS_UPDATED,
            account_id=account_id,
            server=server,
        )

    def store(self, account_id: str, *, login: int, server: str, password: str) -> None:
        with self._sessions.begin() as session:
            self._store(session, account_id, login=login, server=server, password=password)

    def load(self, account_id: str) -> AccountCredentials | None:
        with self._sessions.begin() as session:
            row = session.get(AccountCredential, account_id)
            if row is None:
                return None
            password = self._protector.unprotect(row.encrypted_password)
            return AccountCredentials(login=row.login, password=password, server=row.server)

    def has(self, account_id: str) -> bool:
        with self._sessions.begin() as session:
            return session.get(AccountCredential, account_id) is not None


@dataclass(frozen=True, slots=True)
class SetupSubmission:
    account_id: str
    login: int
    server: str


class SetupLinkService:
    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        vault: CredentialVault,
        base_url: str,
        allowed_account_ids: set[str],
        ttl: timedelta = timedelta(minutes=10),
        on_saved: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        parsed = urlparse(base_url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("public account setup URL must use HTTPS")
        self._sessions = sessions
        self._vault = vault
        self._base_url = base_url.rstrip("/")
        self._allowed = allowed_account_ids
        self._ttl = ttl
        self._on_saved = on_saved
        self._clock = clock
        self._token_factory = token_factory

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, account_id: str, requested_by: int) -> str:
        if account_id not in self._allowed:
            raise ValueError(f"unknown account {account_id!r}")
        token = self._token_factory()
        now = self._clock()
        with self._sessions.begin() as session:
            session.add(
                AccountSetupToken(
                    token_hash=self._hash(token),
                    account_id=account_id,
                    requested_by=f"telegram:{requested_by}",
                    expires_at=now + self._ttl,
                    used_at=None,
                    created_at=now,
                )
            )
            repo.append_audit(
                session,
                AuditEventType.ACCOUNT_SETUP_LINK_ISSUED,
                actor=f"telegram:{requested_by}",
                account_id=account_id,
                expires_at=now + self._ttl,
            )
        return f"{self._base_url}/setup/{token}"

    def is_valid(self, token: str) -> bool:
        with self._sessions.begin() as session:
            row = session.get(AccountSetupToken, self._hash(token))
            return bool(row and row.used_at is None and row.expires_at > self._clock())

    def submit(self, token: str, *, login: str, server: str, password: str) -> str:
        try:
            parsed_login = int(login)
        except ValueError:
            raise ValueError("login must be a positive integer") from None
        if parsed_login <= 0:
            raise ValueError("login must be a positive integer")
        server = server.strip()
        if not server or len(server) > 200:
            raise ValueError("server is required")
        if not password or len(password) > 1000:
            raise ValueError("password is required")

        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(AccountSetupToken, self._hash(token))
            if row is None or row.used_at is not None or row.expires_at <= now:
                raise SetupTokenInvalid("setup link is invalid, expired, or already used")
            self._vault._store(
                session,
                row.account_id,
                login=parsed_login,
                server=server,
                password=password,
            )
            row.used_at = now
            account_id = row.account_id
        if self._on_saved is not None:
            self._on_saved(account_id)
        return account_id
