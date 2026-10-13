"""Тесты безопасности (§8.1, §8.3, I-7): пароли, cookie, токены, Fernet, маски."""

from __future__ import annotations

import pytest

from server.core.security import (
    MASK,
    SecretBox,
    SessionSigner,
    generate_agent_token,
    hash_password,
    load_or_create_secret,
    mask_mapping,
    mask_secret,
    verify_password,
)


class TestAdminPasswords:
    """Хеширование паролей администраторов (§4.2)."""

    def test_roundtrip(self) -> None:
        stored = hash_password("magaz-strong-password")
        assert verify_password("magaz-strong-password", stored) is True

    def test_wrong_password_rejected(self) -> None:
        stored = hash_password("magaz-strong-password")
        assert verify_password("magaz-strong-passwerd", stored) is False

    def test_salt_is_random(self) -> None:
        first, second = hash_password("same-password"), hash_password("same-password")
        assert first != second
        assert verify_password("same-password", first)
        assert verify_password("same-password", second)

    def test_hash_format_matches_pbkdf2(self) -> None:
        algorithm, iterations, salt_b64, digest_b64 = hash_password("x" * 12).split("$")
        assert algorithm == "pbkdf2_sha256"
        assert int(iterations) >= 100_000
        assert salt_b64 and digest_b64

    @pytest.mark.parametrize("broken", ["", "no-dollar-sign", "md5$1$a$b", "pbkdf2_sha256$x$y$z"])
    def test_broken_hash_is_false_not_exception(self, broken: str) -> None:
        assert verify_password("anything", broken) is False

    def test_empty_password_rejected(self) -> None:
        with pytest.raises(ValueError):
            hash_password("")


class TestAgentTokens:
    """Токены агентов (§4.1, FR-11)."""

    def test_tokens_are_unique_and_long_enough(self) -> None:
        tokens = {generate_agent_token() for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(token) >= 32 for token in tokens)


class TestMasking:
    """Маскирование секретов (I-7: пароль не попадает в логи и аудит)."""

    def test_mask_secret(self) -> None:
        assert mask_secret("secret") == MASK
        assert mask_secret("") == ""
        assert mask_secret(None) == ""

    def test_mask_mapping_hides_password_and_keeps_rest(self) -> None:
        params = {"file_name": "update", "password": "secret", "user_name": "Администратор"}
        masked = mask_mapping(params)
        assert masked["password"] == MASK
        assert masked["file_name"] == "update"
        assert params["password"] == "secret", "исходный словарь не мутируется"

    def test_mask_mapping_skips_empty_password(self) -> None:
        assert mask_mapping({"password": ""})["password"] == ""


class TestSecretBox:
    """Fernet-шифрование пароля 1С (П-1, §8.3)."""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        from cryptography.fernet import Fernet

        box = SecretBox(key=Fernet.generate_key())
        token = box.encrypt("Пароль1С:сложный#2026")
        assert token is not None
        assert "Пароль" not in token
        assert box.decrypt(token) == "Пароль1С:сложный#2026"

    def test_key_file_created_once_and_reused(self, tmp_path) -> None:  # noqa: ANN001
        key_path = tmp_path / "secrets.key"
        first = load_or_create_secret(key_path)
        second = load_or_create_secret(key_path)

        assert key_path.exists()
        assert first == second, "ключ переиспользуется, а не пересоздаётся"

        box = SecretBox(key=first)
        token = box.encrypt("Пароль1С")
        assert token and token != "Пароль1С"
        assert SecretBox(key=second).decrypt(token) == "Пароль1С"

    def test_env_value_has_priority_over_file(self, tmp_path) -> None:  # noqa: ANN001
        from cryptography.fernet import Fernet

        generated = Fernet.generate_key().decode()
        key = load_or_create_secret(tmp_path / "secrets.key", generated)
        assert key == generated.encode()
        assert not (tmp_path / "secrets.key").exists(), "файл не создаётся, если задан MAGAZ_SECRET_KEY"

    def test_empty_password_is_none(self) -> None:
        from cryptography.fernet import Fernet

        box = SecretBox(key=Fernet.generate_key())
        assert box.encrypt(None) is None
        assert box.encrypt("") is None
        assert box.decrypt(None) is None

    def test_foreign_key_returns_none_instead_of_crash(self) -> None:
        from cryptography.fernet import Fernet

        token = SecretBox(key=Fernet.generate_key()).encrypt("Пароль1С")
        other = SecretBox(key=Fernet.generate_key())
        assert other.decrypt(token) is None

    def test_reencrypt_changes_token_keeps_value(self) -> None:
        from cryptography.fernet import Fernet

        box = SecretBox(key=Fernet.generate_key())
        original = box.encrypt("Пароль1С")
        rotated = box.reencrypt(original)
        assert rotated != original
        assert box.decrypt(rotated) == "Пароль1С"


class TestSessionSigner:
    """Подписанная cookie администратора (§4.2)."""

    def test_roundtrip(self) -> None:
        signer = SessionSigner("secret-key-for-tests")
        token = signer.dump({"admin_id": 1, "username": "admin"})
        assert signer.load(token, max_age=60) == {"admin_id": 1, "username": "admin"}

    def test_tampered_token_rejected(self) -> None:
        signer = SessionSigner("secret-key-for-tests")
        token = signer.dump({"admin_id": 1})
        assert signer.load(token[:-2] + "xy", max_age=60) is None

    def test_other_secret_rejected(self) -> None:
        token = SessionSigner("secret-a").dump({"admin_id": 1})
        assert SessionSigner("secret-b").load(token, max_age=60) is None

    def test_expired_token_rejected(self) -> None:
        signer = SessionSigner("secret-key-for-tests")
        token = signer.dump({"admin_id": 1})
        assert signer.load(token, max_age=-1) is None

    def test_missing_token_is_none(self) -> None:
        assert SessionSigner("secret-key-for-tests").load(None, max_age=60) is None
