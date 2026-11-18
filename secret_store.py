"""令牌加密存储：优先 DPAPI，其次系统钥匙串，最后**不落盘**。

草案的红线：**别明文落盘，失败也别静默降级成明文。**

所以这里的每个函数都可以失败，失败一律返回 ``None``，由插件层退成"只存进程
内存"，并在 `get_status` 里明说"重启需要重新填令牌"。绝不会因为加密不可用就
悄悄写一个明文文件——那正是米家插件踩过的坑。

三条通道：

    dpapi    Windows 的 CryptProtectData。绑定当前用户 + 当前机器，密文离开
             这两者就解不开。零依赖（纯 ctypes），所以 Windows 上几乎总可用。
    keyring  其他平台的系统钥匙串（macOS Keychain / Secret Service）。可选依赖，
             没装就跳过。
    none     都不可用 → 调用方只能放内存。

DPAPI 用 ``CRYPTPROTECT_UI_FORBIDDEN``：插件是后台进程，绝不能弹出系统对话框
等用户点按，那会卡死整个调用链。
"""

from __future__ import annotations

import sys
from typing import Any

BACKEND_DPAPI = "dpapi"
BACKEND_KEYRING = "keyring"
BACKEND_NONE = "none"

# 钥匙串里的服务名。写成常量，避免各处手抄出现拼写漂移导致读不回来。
KEYRING_SERVICE = "neko-plugin-homeassistant"
KEYRING_USERNAME = "ha-token"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def backend() -> str:
    """检测当前可用的加密后端。结果按调用实时判断，便于测试打桩。"""
    if sys.platform == "win32" and _dpapi_available():
        return BACKEND_DPAPI
    if _keyring_available():
        return BACKEND_KEYRING
    return BACKEND_NONE


# ── DPAPI ───────────────────────────────────────────────────────────────
def _dpapi_available() -> bool:
    try:
        import ctypes

        ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _dpapi_call(protect: bool, data: bytes) -> bytes | None:
    """调用 CryptProtectData / CryptUnprotectData。"""
    try:
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        buffer = ctypes.create_string_buffer(data, len(data))
        blob_in = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = DataBlob()

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(blob_in), None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
            )

        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            # 必须显式释放，否则每次保存连接都会泄漏一块 LocalAlloc 内存。
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


# ── keyring ─────────────────────────────────────────────────────────────
def _keyring_module() -> Any:
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring
    except Exception:
        return None


def _keyring_available() -> bool:
    module = _keyring_module()
    if module is None:
        return False
    try:
        backend_obj = module.get_keyring()
    except Exception:
        return False
    # keyring 的 fail 后端只是把明文存到内存/文件，不算"加密可用"。
    name = type(backend_obj).__module__.lower()
    return "fail" not in name


# ── 对外接口 ────────────────────────────────────────────────────────────
def encrypt(raw: bytes) -> bytes | None:
    """加密任意字节。不可用时返回 None（**不是**返回原数据）。"""
    which = backend()
    if which == BACKEND_DPAPI:
        return _dpapi_call(protect=True, data=raw)
    if which == BACKEND_KEYRING:
        # 钥匙串按"条目"存，不适合存任意二进制；交给插件层直接存令牌串。
        return None
    return None


def decrypt(blob: bytes) -> bytes | None:
    """解密由 :func:`encrypt` 产出的密文。"""
    if not blob:
        return None
    which = backend()
    if which == BACKEND_DPAPI:
        return _dpapi_call(protect=False, data=blob)
    return None


def keyring_set(token: str) -> bool:
    module = _keyring_module()
    if module is None:
        return False
    try:
        module.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
        return True
    except Exception:
        return False


def keyring_get() -> str:
    module = _keyring_module()
    if module is None:
        return ""
    try:
        return str(module.get_password(KEYRING_SERVICE, KEYRING_USERNAME) or "")
    except Exception:
        return ""


def keyring_delete() -> None:
    module = _keyring_module()
    if module is None:
        return
    try:
        module.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception:
        pass
