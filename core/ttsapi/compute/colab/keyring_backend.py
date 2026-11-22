"""An account-specific credential store for the pinned CLI process."""

import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from keyring.backend import KeyringBackend
from keyring.errors import KeyringLocked, PasswordDeleteError


def system_backend():
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()

    import secretstorage
    from keyring.backends.SecretService import Keyring

    class UnlockedKeyring(Keyring):
        def get_preferred_collection(self):
            collection = secretstorage.get_default_collection(secretstorage.dbus_init())
            self.unlock(collection)
            return collection

        def unlock(self, item):
            if item.is_locked():
                raise KeyringLocked("Colab keyring is locked")

    return UnlockedKeyring()


# colab CLI 0.8.0 存 OAuth 令牌用的条目（colab_cli 的 KEYRING_SERVICE_NAME / KEYRING_USERNAME）
CLI_SERVICE, CLI_USERNAME = "google-colab-cli", "oauth_token"
FILE_KEY_SERVICE = "tts-core-colab-file-key"


def forget(account, keyring_dir):
    """移除账号时删掉 core 给它写的凭据（AccountKeyring 映射出的那一条）：macOS 是钥匙串里
    google-colab-cli / oauth_token/<账号>；Linux 是 keyring/<账号> 加密文件和它在系统密钥环里的文件密钥。
    default（本机 colab CLI 原有的登录）不归 core 管，不删。本来就没有的条目跳过，别的删除失败照常抛出。"""
    if account == "default":
        raise ValueError("default uses the existing system login")
    system = system_backend()
    if sys.platform == "darwin":
        entries = [(CLI_SERVICE, f"{CLI_USERNAME}/{account}")]
    else:
        path = Path(keyring_dir) / account
        path.unlink(missing_ok=True)
        path.with_suffix(".lock").unlink(missing_ok=True)
        entries = [(FILE_KEY_SERVICE, account)]
    for service, username in entries:
        try:
            system.delete_password(service, username)
        except PasswordDeleteError as e:
            if not _missing(e):
                raise  # 钥匙串拒绝删除等：报错，调用方保留可重试状态


def _missing(error):
    """删除失败是因为条目本来就没有：macOS 后端把钥匙串的 NotFound 包成 PasswordDeleteError（__cause__ 是它，
    拒绝访问等别的错误也包成同一种）；Secret Service 只在没找到时抛 PasswordDeleteError（没有 __cause__）。
    按类名判断：在 Linux 上 import macOS 的 api 模块会去加载 Security 框架。"""
    if sys.platform == "darwin":
        return type(error.__cause__).__name__ == "NotFound"
    return error.__cause__ is None


class AccountKeyring(KeyringBackend):
    priority = 1

    @property
    def account(self):
        return os.environ["TTS_COLAB_ACCOUNT"]

    def _file(self):
        return Path(os.environ["TTS_COLAB_KEYRING"]) / self.account

    def _cipher(self, create):
        from cryptography.fernet import Fernet

        system = system_backend()
        service = FILE_KEY_SERVICE
        key = system.get_password(service, self.account)
        if key is None:
            if not create:
                raise KeyringLocked("Colab account file key is unavailable")
            key = Fernet.generate_key().decode("ascii")
            system.set_password(service, self.account, key)
        return Fernet(key.encode("ascii"))

    def get_password(self, service, username):
        if self.account == "default":
            return system_backend().get_password(service, username)
        if sys.platform == "darwin":
            return system_backend().get_password(service, f"{username}/{self.account}")
        path = self._file()
        if path.is_file():
            return self._cipher(create=False).decrypt(path.read_bytes()).decode()
        return None

    def set_password(self, service, username, password):
        if self.account == "default":
            return  # refresh in process only; the existing login belongs to the standalone CLI
        if sys.platform == "darwin":
            system_backend().set_password(
                service, f"{username}/{self.account}", password
            )
            return
        import fcntl

        path = self._file()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            encrypted = self._cipher(create=not path.exists()).encrypt(
                password.encode()
            )
            with NamedTemporaryFile(dir=path.parent, delete=False) as out:
                out.write(encrypted)
            os.replace(out.name, path)

    def delete_password(self, service, username):
        raise PasswordDeleteError(
            "credential removal is managed outside the job runner"
        )
