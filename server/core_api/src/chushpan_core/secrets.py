"""Read the machine-protected Core API token on Windows."""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import POINTER, Structure, byref, c_void_p
from ctypes.wintypes import BOOL, DWORD, LPWSTR
from pathlib import Path


class _DataBlob(Structure):
    _fields_ = [("cbData", DWORD), ("pbData", POINTER(ctypes.c_ubyte))]


def token_path() -> Path:
    """Return the fixed ProgramData location used by the Windows installer."""
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(program_data) / "Chushpan" / "CoreServer" / "secrets" / "api-token.dpapi"


def load_api_token() -> str:
    """Decrypt a machine-scope DPAPI token without placing it in the environment."""
    if os.name != "nt":
        raise RuntimeError("The production token is protected by Windows DPAPI.")

    protected = token_path().read_bytes()
    if not protected or len(protected) > 4096:
        raise RuntimeError("The protected API token file is empty or invalid.")

    input_buffer = ctypes.create_string_buffer(protected)
    input_blob = _DataBlob(len(protected), ctypes.cast(input_buffer, POINTER(ctypes.c_ubyte)))
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        POINTER(_DataBlob),
        POINTER(LPWSTR),
        POINTER(_DataBlob),
        c_void_p,
        c_void_p,
        DWORD,
        POINTER(_DataBlob),
    ]
    unprotect.restype = BOOL

    if not unprotect(byref(input_blob), None, None, None, None, 1, byref(output_blob)):
        raise ctypes.WinError(ctypes.get_last_error())

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [c_void_p]
    local_free.restype = c_void_p
    try:
        raw_token = bytearray(ctypes.string_at(output_blob.pbData, output_blob.cbData))
        ctypes.memset(output_blob.pbData, 0, output_blob.cbData)
    finally:
        local_free(output_blob.pbData)

    try:
        token = raw_token.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("The protected API token is not valid ASCII.") from exc
    finally:
        raw_token[:] = bytes(len(raw_token))
    if len(token) != 43:
        raise RuntimeError("The protected API token has an unexpected length.")
    return token


def _provider_key_path(provider: str) -> Path:
    if provider != "openai":
        raise ValueError("Unsupported Core LLM provider.")
    return token_path().parent / "openai-api-key.dpapi"


def save_openai_key(value: str) -> None:
    """Store an OpenAI key with machine-scope DPAPI; callers must not log it."""
    if not isinstance(value, str) or not 20 <= len(value) <= 512:
        raise ValueError("The OpenAI key length is outside the accepted range.")
    if any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("The OpenAI key contains unsupported characters.")
    from chushpan_core.identity import _dpapi

    path = _provider_key_path("openai")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = bytearray(value.encode("ascii"))
    try:
        protected = _dpapi(bytes(raw), protect=True)
    finally:
        raw[:] = bytes(len(raw))
    temporary = path.with_suffix(".dpapi.tmp")
    temporary.write_bytes(protected)
    temporary.replace(path)


def load_openai_key() -> str:
    """Decrypt the manually provisioned provider key for the duration of a request."""
    path = _provider_key_path("openai")
    if os.name != "nt":
        raise RuntimeError("Core provider credentials are available only on Windows.")
    protected = path.read_bytes()
    if not protected or len(protected) > 4096:
        raise RuntimeError("Core provider credentials are unavailable or invalid.")
    from chushpan_core.identity import _dpapi

    raw = bytearray(_dpapi(protected, protect=False))
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("The provider credential is invalid.") from exc
    finally:
        raw[:] = bytes(len(raw))
    if not re.fullmatch(r"[^\s\x00-\x1f\x7f]{20,512}", value):
        raise RuntimeError("The provider credential is invalid.")
    return value


def openai_key_configured() -> bool:
    return _provider_key_path("openai").is_file()
