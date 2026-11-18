"""令牌存储：能加密就加密，不能就返回 None —— **绝不返回原文**。

最关键的一条红线在这两个测试里：
    test_encrypt_never_returns_plaintext_on_failure
    test_decrypt_of_garbage_returns_none
"""

import sys

import pytest
import secret_store


class TestBackendDetection:
    def test_backend_is_known_value(self):
        assert secret_store.backend() in {
            secret_store.BACKEND_DPAPI,
            secret_store.BACKEND_KEYRING,
            secret_store.BACKEND_NONE,
        }

    def test_dpapi_only_on_windows(self):
        if sys.platform != "win32":
            assert secret_store.backend() != secret_store.BACKEND_DPAPI


class TestDpapi:
    @pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")
    def test_roundtrip(self):
        if secret_store.backend() != secret_store.BACKEND_DPAPI:
            pytest.skip("当前环境 DPAPI 不可用")
        raw = b'{"base_url":"http://h:8123","token":"secret-token"}'
        blob = secret_store.encrypt(raw)
        assert blob is not None
        assert blob != raw, "密文不能等于原文"
        assert secret_store.decrypt(blob) == raw

    @pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")
    def test_ciphertext_differs_each_time(self):
        if secret_store.backend() != secret_store.BACKEND_DPAPI:
            pytest.skip("当前环境 DPAPI 不可用")
        # DPAPI 带随机 salt，同一明文两次加密结果必须不同。
        assert secret_store.encrypt(b"same") != secret_store.encrypt(b"same")

    @pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")
    def test_decrypt_of_garbage_returns_none(self):
        if secret_store.backend() != secret_store.BACKEND_DPAPI:
            pytest.skip("当前环境 DPAPI 不可用")
        assert secret_store.decrypt(b"not-a-real-blob") is None


class TestFailurePaths:
    def test_encrypt_never_returns_plaintext_on_failure(self, monkeypatch):
        # 没有可用后端时，唯一正确的返回是 None。返回 raw 就等于明文落盘。
        monkeypatch.setattr(secret_store, "backend", lambda: secret_store.BACKEND_NONE)
        assert secret_store.encrypt(b"my-token") is None

    def test_decrypt_without_backend_returns_none(self, monkeypatch):
        monkeypatch.setattr(secret_store, "backend", lambda: secret_store.BACKEND_NONE)
        assert secret_store.decrypt(b"whatever") is None

    def test_keyring_backend_does_not_store_blob(self, monkeypatch):
        # 钥匙串按条目存字符串，不适合二进制；encrypt 应老实返回 None。
        monkeypatch.setattr(secret_store, "backend", lambda: secret_store.BACKEND_KEYRING)
        assert secret_store.encrypt(b"data") is None

    def test_decrypt_empty_returns_none(self):
        assert secret_store.decrypt(b"") is None

    def test_dpapi_call_swallows_exceptions(self, monkeypatch):
        # 加密后端抛异常也必须退成 None，不能把异常冒到保存连接的路径上。
        monkeypatch.setattr(secret_store, "backend", lambda: secret_store.BACKEND_DPAPI)
        monkeypatch.setattr(secret_store, "_dpapi_call", lambda **_kwargs: None)
        assert secret_store.encrypt(b"x") is None


class TestKeyring:
    def test_missing_module_degrades_quietly(self, monkeypatch):
        monkeypatch.setattr(secret_store, "_keyring_module", lambda: None)
        assert secret_store.keyring_set("t") is False
        assert secret_store.keyring_get() == ""
        secret_store.keyring_delete()  # 不抛异常即通过

    def test_set_get_delete_roundtrip(self, monkeypatch):
        store: dict[tuple[str, str], str] = {}

        class FakeKeyring:
            @staticmethod
            def set_password(service, username, value):
                store[(service, username)] = value

            @staticmethod
            def get_password(service, username):
                return store.get((service, username))

            @staticmethod
            def delete_password(service, username):
                store.pop((service, username), None)

        monkeypatch.setattr(secret_store, "_keyring_module", lambda: FakeKeyring)
        assert secret_store.keyring_set("tok") is True
        assert secret_store.keyring_get() == "tok"
        secret_store.keyring_delete()
        assert secret_store.keyring_get() == ""

    def test_keyring_exception_returns_false(self, monkeypatch):
        class Broken:
            @staticmethod
            def set_password(*_args):
                raise RuntimeError("钥匙串不可用")

            @staticmethod
            def get_password(*_args):
                raise RuntimeError("钥匙串不可用")

        monkeypatch.setattr(secret_store, "_keyring_module", lambda: Broken)
        assert secret_store.keyring_set("t") is False
        assert secret_store.keyring_get() == ""

    def test_fail_backend_is_not_counted_as_available(self, monkeypatch):
        # keyring 的 fail 后端只是把明文写文件，不能当成"加密可用"。
        class FailKeyring:
            @staticmethod
            def get_keyring():
                return type("Keyring", (), {"__module__": "keyring.backends.fail"})()

        monkeypatch.setattr(secret_store, "_keyring_module", lambda: FailKeyring)
        monkeypatch.setattr(secret_store, "_dpapi_available", lambda: False)
        assert secret_store.backend() == secret_store.BACKEND_NONE
