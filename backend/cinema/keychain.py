"""A single generic password in macOS Keychain, through public Security APIs.

No shell arguments, files, environment variables or plaintext fallback. Status
queries never request password data or display Keychain authentication dialogs.
"""

import re
import threading
import time

from backend.permission_policy import requests_allowed
from backend.tools.registry import ToolError


class TavilyCredentials:
    def __init__(self, *, security=None, service="ee.chushpan.cinema.search", account="tavily"):
        self._security = security
        self.service, self.account = service, account
        self._lock = threading.RLock()
        self._cached = None
        self._expires = 0

    def security(self):
        if self._security is None:
            try:
                import Security
            except ImportError:
                raise ToolError("macOS Keychain недоступен. Ключ Tavily не сохранён.") from None
            self._security = Security
        return self._security

    def query(self):
        s = self.security()
        return {
            s.kSecClass: s.kSecClassGenericPassword,
            s.kSecAttrService: self.service,
            s.kSecAttrAccount: self.account,
        }

    def status(self, *, refresh=False):
        with self._lock:
            if not refresh and self._cached and self._expires > time.monotonic():
                return dict(self._cached)
            try:
                s = self.security()
                code, _ = s.SecItemCopyMatching(
                    {
                        **self.query(),
                        s.kSecReturnAttributes: True,
                        s.kSecUseAuthenticationUI: s.kSecUseAuthenticationUIFail,
                    },
                    None,
                )
                result = {
                    "configured": code in {s.errSecSuccess, s.errSecInteractionNotAllowed},
                    "storage": "keychain",
                    "state": "saved"
                    if code == s.errSecSuccess
                    else "missing"
                    if code == s.errSecItemNotFound
                    else "locked"
                    if code == s.errSecInteractionNotAllowed
                    else "unavailable",
                }
            except Exception:
                result = {"configured": False, "storage": "keychain", "state": "unavailable"}
            self._cached, self._expires = result, time.monotonic() + 30
            return dict(result)

    @property
    def configured(self):
        return self.status()["configured"]

    @staticmethod
    def failure(code):
        if code in {-25308, -25293, -128}:
            return ToolError(
                "Доступ к ключу Tavily в Keychain не разрешён или Связка ключей заблокирована. "
                "Разблокируй её и повтори действие."
            )
        return ToolError(
            "macOS Keychain не выполнил операцию с ключом Tavily. Копия в файл не создавалась."
        )

    def save(self, credential):
        if (
            not isinstance(credential, str)
            or len(credential) > 512
            or (credential and not re.fullmatch(r"tvly-[A-Za-z0-9_-]{8,507}", credential))
        ):
            raise ToolError("Укажи ключ Tavily с префиксом tvly- без пробелов и переносов строк.")
        with self._lock:
            s = self.security()
            query = self.query()
            if not credential:
                code = s.SecItemDelete(query)
                if code not in {s.errSecSuccess, s.errSecItemNotFound}:
                    raise self.failure(code)
            else:
                value = {s.kSecValueData: credential.encode("utf-8")}
                code = s.SecItemUpdate(query, value)
                if code == s.errSecItemNotFound:
                    code, _ = s.SecItemAdd(
                        {**query, **value, s.kSecAttrLabel: "Чушпан · Tavily Search API"}, None
                    )
                if code != s.errSecSuccess:
                    raise self.failure(code)
            self._cached = None
            self._expires = 0

    def read(self):
        with self._lock:
            s = self.security()
            query = {**self.query(), s.kSecReturnData: True}
            if not requests_allowed.get():
                query[s.kSecUseAuthenticationUI] = s.kSecUseAuthenticationUIFail
            code, data = s.SecItemCopyMatching(query, None)
            if code == s.errSecItemNotFound:
                raise ToolError("Добавь ключ Tavily в медиатеке → Поиск и Safari.")
            if code != s.errSecSuccess:
                raise self.failure(code)
            try:
                value = bytes(data).decode("utf-8")
            except (TypeError, ValueError):
                raise ToolError(
                    "Не удалось прочитать ключ Tavily из Keychain. Сохрани его заново."
                ) from None
            return value
