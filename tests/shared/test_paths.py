"""Тесты `shared.paths` — защита от выхода за рабочую папку (I-2, I-15)."""

from __future__ import annotations

import pytest

from shared.paths import (
    is_safe_archive_member,
    safe_basename,
    validate_file_name,
    validate_ipv4_address,
)


class TestValidateFileName:
    """Имя файла обновления (перенос v2: пустое значение и разделители запрещены)."""

    def test_plain_name_ok(self) -> None:
        assert validate_file_name(" update ") == "update"

    @pytest.mark.parametrize("bad", ["", "   ", ".", "..", "sub/update", "..\\update", "c:/tmp/x"])
    def test_dangerous_values_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_file_name(bad)


class TestSafeBasename:
    """FR-1: имя скачанного файла берётся как basename."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("update.cf", "update.cf"),
            ("update.cf.cfe", "update.cf.cfe"),
            ("files/update.cf", "update.cf"),
            (r"C:\Temp\update.cf", "update.cf"),
            (r"..\..\windows\system32\evil.exe", "evil.exe"),
            ('"update.cf"', "update.cf"),
        ],
    )
    def test_basename_extracted(self, raw: str, expected: str) -> None:
        assert safe_basename(raw) == expected

    @pytest.mark.parametrize("bad", ["", "   ", ".", "..", "C:\\"])
    def test_unsafe_names_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            safe_basename(bad)


class TestArchiveMembers:
    """I-2: защита от Zip Slip."""

    @pytest.mark.parametrize(
        "member",
        ["update.cf", "folder/update.cf", "folder/sub/update.cf", "./update.cf", "folder\\update.cf"],
    )
    def test_safe_members(self, member: str) -> None:
        assert is_safe_archive_member(member) is True

    @pytest.mark.parametrize(
        "member",
        ["", "..", "../update.cf", "folder/../../update.cf", "/etc/passwd", "C:\\Windows\\evil.exe", "."],
    )
    def test_dangerous_members(self, member: str) -> None:
        assert is_safe_archive_member(member) is False


class TestIpv4Validation:
    """I-15: устройство опрашивается только по явному IPv4-адресу."""

    def test_valid_addresses(self) -> None:
        assert validate_ipv4_address(" 10.10.5.21 ") == "10.10.5.21"
        assert validate_ipv4_address("192.168.0.1") == "192.168.0.1"

    @pytest.mark.parametrize("bad", ["", "10.10.5", "10.10.5.256", "камера", "10.10.5.0/24", "10.10.5.1.1"])
    def test_invalid_addresses(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_ipv4_address(bad)
