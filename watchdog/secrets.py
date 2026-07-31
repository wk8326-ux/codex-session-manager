from __future__ import annotations

import ctypes
import os
from typing import Protocol


class SecretStoreError(RuntimeError):
    """Raised when a channel secret cannot be protected or recovered."""


class SecretStore(Protocol):
    def protect(self, value: str) -> bytes:
        raise NotImplementedError

    def unprotect(self, value: bytes) -> str:
        raise NotImplementedError


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class DpapiSecretStore:
    """Protect secrets with Windows DPAPI in the current-user scope."""

    CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def __init__(self, *, entropy: bytes | None = None) -> None:
        self._entropy = entropy or b""

    @staticmethod
    def _blob(data: bytes) -> tuple[_DataBlob, object]:
        buffer = ctypes.create_string_buffer(data)
        blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    def protect(self, value: str) -> bytes:
        if not isinstance(value, str) or not value:
            raise SecretStoreError("secret must not be empty")
        if os.name != "nt":
            raise SecretStoreError("DPAPI is available only on Windows")
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = self._blob(value.encode("utf-8"))
        entropy_blob, entropy_buffer = self._blob(self._entropy) if self._entropy else (None, None)
        output_blob = _DataBlob()
        try:
            success = crypt32.CryptProtectData(
                ctypes.byref(input_blob), None, ctypes.byref(entropy_blob) if entropy_blob else None,
                None, None, self.CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output_blob)
            )
            if not success:
                raise SecretStoreError("Windows DPAPI could not protect the secret")
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        except SecretStoreError:
            raise
        except (AttributeError, OSError, ctypes.ArgumentError) as error:
            raise SecretStoreError("Windows DPAPI is unavailable") from error
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)
            del input_buffer, entropy_buffer

    def unprotect(self, value: bytes) -> str:
        if not isinstance(value, bytes) or not value:
            raise SecretStoreError("ciphertext must not be empty")
        if os.name != "nt":
            raise SecretStoreError("DPAPI is available only on Windows")
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = self._blob(value)
        entropy_blob, entropy_buffer = self._blob(self._entropy) if self._entropy else (None, None)
        output_blob = _DataBlob()
        try:
            success = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob), None, ctypes.byref(entropy_blob) if entropy_blob else None,
                None, None, self.CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output_blob)
            )
            if not success:
                raise SecretStoreError("Windows DPAPI could not recover the secret")
            try:
                return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
            except UnicodeDecodeError as error:
                raise SecretStoreError("protected secret is not valid UTF-8") from error
        except SecretStoreError:
            raise
        except (AttributeError, OSError, ctypes.ArgumentError) as error:
            raise SecretStoreError("Windows DPAPI is unavailable") from error
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)
            del input_buffer, entropy_buffer
