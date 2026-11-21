"""Read the machine-protected Core API token on Windows."""

from __future__ import annotations

import ctypes
import os
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
