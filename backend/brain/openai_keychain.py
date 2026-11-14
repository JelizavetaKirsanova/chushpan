"""OpenAI API credential stored only in macOS Keychain."""

from __future__ import annotations

import re

from backend.tools.registry import ToolError


class OpenAIKeychain:
    service = "ee.chushpan.ai.openai"
    account = "api-key"

    def __init__(self, *, security=None):
        self._security = security

    def security(self):
        if self._security is None:
            try:
                import Security
            except ImportError:
                raise ToolError("macOS Keychain недоступен. Ключ OpenAI не сохранён.") from None
            self._security = Security
        return self._security

    def query(self):
        security = self.security()
        return {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: self.service,
            security.kSecAttrAccount: self.account,
        }

    def status(self):
        try:
            security = self.security()
            code, _ = security.SecItemCopyMatching(
                {
                    **self.query(),
                    security.kSecReturnAttributes: True,
                    security.kSecUseAuthenticationUI: security.kSecUseAuthenticationUIFail,
                },
                None,
            )
        except Exception:
            return {"configured": False, "state": "unavailable", "storage": "keychain"}
        return {
            "configured": code in {security.errSecSuccess, security.errSecInteractionNotAllowed},
            "state": "saved"
            if code == security.errSecSuccess
            else "missing"
            if code == security.errSecItemNotFound
            else "locked"
            if code == security.errSecInteractionNotAllowed
            else "unavailable",
            "storage": "keychain",
        }

    def save(self, credential: str):
        if not isinstance(credential, str) or len(credential) > 512:
            raise ToolError("Укажи корректный ключ OpenAI без пробелов и переносов строк.")
        if credential and not re.fullmatch(r"sk-[A-Za-z0-9_-]{20,500}", credential):
            raise ToolError("Ключ должен начинаться с sk- и не содержать пробелов.")
        security = self.security()
        query = self.query()
        if not credential:
            code = security.SecItemDelete(query)
            if code not in {security.errSecSuccess, security.errSecItemNotFound}:
                raise ToolError("Не удалось удалить ключ OpenAI из macOS Keychain.")
            return
        value = {security.kSecValueData: credential.encode("utf-8")}
        code = security.SecItemUpdate(query, value)
        if code == security.errSecItemNotFound:
            code, _ = security.SecItemAdd(
                {**query, **value, security.kSecAttrLabel: "Чушпан · OpenAI API"}, None
            )
        if code != security.errSecSuccess:
            raise ToolError("macOS Keychain не сохранил ключ OpenAI.")

    def read(self) -> str:
        security = self.security()
        try:
            code, data = security.SecItemCopyMatching(
                {**self.query(), security.kSecReturnData: True}, None
            )
        except Exception:
            raise ToolError("Не удалось прочитать ключ OpenAI из macOS Keychain.") from None
        if code == security.errSecItemNotFound:
            raise ToolError("Сначала добавь API-ключ OpenAI в настройках Чушпана.")
        if code != security.errSecSuccess:
            raise ToolError("Доступ к ключу OpenAI в macOS Keychain не разрешён.")
        try:
            return bytes(data).decode("utf-8")
        except (TypeError, ValueError):
            raise ToolError("Не удалось прочитать ключ OpenAI. Сохрани его заново.") from None
