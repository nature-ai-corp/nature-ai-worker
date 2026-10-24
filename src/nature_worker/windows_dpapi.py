"""Windows-only DPAPI data protection through native CryptProtectData APIs.

A per-user DPAPI token is not an isolation boundary against code executing
under the same Windows identity. Enterprise S-C requires an independent OS
identity / approved enterprise policy and NTFS DACLs before activation.
"""
from __future__ import annotations

import sys


class WindowsProtectionError(RuntimeError):
    pass


def protect_for_current_user(plaintext: bytes) -> bytes:
    if sys.platform != "win32":
        raise WindowsProtectionError("Windows DPAPI is unavailable on this platform")
    if not isinstance(plaintext, bytes):
        raise WindowsProtectionError("DPAPI plaintext must be bytes")
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        raw = ctypes.create_string_buffer(plaintext)
        source = DATA_BLOB(len(plaintext), ctypes.cast(raw, ctypes.POINTER(ctypes.c_byte)))
        result = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
            ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None,
                                         0x01, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))
    except OSError as exc:
        raise WindowsProtectionError("Windows DPAPI protection failed") from exc


def unprotect_for_current_user(ciphertext: bytes) -> bytes:
    if sys.platform != "win32":
        raise WindowsProtectionError("Windows DPAPI is unavailable on this platform")
    if not isinstance(ciphertext, bytes):
        raise WindowsProtectionError("DPAPI ciphertext must be bytes")
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        raw = ctypes.create_string_buffer(ciphertext)
        source = DATA_BLOB(len(ciphertext), ctypes.cast(raw, ctypes.POINTER(ctypes.c_byte)))
        result = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None,
                                           None, 0x01, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))
    except OSError as exc:
        raise WindowsProtectionError("Windows DPAPI decryption failed") from exc
