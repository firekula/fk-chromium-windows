import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from tools import install_brand_assets
from tools.install_brand_assets import install_assets, main


def _write_branding_fixture(tmp_path, assets=None):
    branding_root = tmp_path / "branding-root"
    generated = branding_root / "branding" / "generated"
    generated.mkdir(parents=True)
    source_bytes = {
        "branding/generated/fk_chromium.ico": b"\x00\x00\x01\x00FK-ICO\xff",
        "branding/generated/product_logo_16.png": b"\x89PNG\r\n\x1a\nFK-16\x00",
    }
    for relative, content in source_bytes.items():
        source = branding_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
    if assets is None:
        # Deliberately reverse lexical destination order.
        assets = [
            {
                "source": "branding/generated/product_logo_16.png",
                "destination": "chrome/app/theme/chromium/product_logo_16.png",
            },
            {
                "source": "branding/generated/fk_chromium.ico",
                "destination": "chrome/app/theme/chromium/chromium.ico",
            },
        ]
    manifest_path = branding_root / "branding" / "manifest.json"
    manifest_path.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    return branding_root, manifest_path, source_bytes


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_install_assets_copies_exact_bytes_and_hashes_in_lexical_order(tmp_path):
    """A lossy copy or manifest-order return must fail this binary contract."""
    branding_root, manifest_path, source_bytes = _write_branding_fixture(tmp_path)
    source_root = tmp_path / "src"

    copied = install_assets(source_root, branding_root, manifest_path)

    expected_relatives = sorted(
        [
            Path("chrome/app/theme/chromium/product_logo_16.png"),
            Path("chrome/app/theme/chromium/chromium.ico"),
        ]
    )
    assert copied == [source_root / relative for relative in expected_relatives]
    source_for_destination = {
        "chromium.ico": "branding/generated/fk_chromium.ico",
        "product_logo_16.png": "branding/generated/product_logo_16.png",
    }
    for destination in copied:
        relative = destination.relative_to(source_root)
        source = branding_root / source_for_destination[destination.name]
        assert destination.is_relative_to(source_root)
        assert destination.read_bytes() == source.read_bytes()
        assert _sha256(destination) == _sha256(source)
        assert not destination.with_name(destination.name + ".tmp").exists()
        assert relative.parts[:4] == ("chrome", "app", "theme", "chromium")


def test_install_assets_atomically_replaces_an_existing_destination(tmp_path):
    """Writing around rather than replacing the destination must fail this outcome test."""
    branding_root, manifest_path, source_bytes = _write_branding_fixture(tmp_path)
    source_root = tmp_path / "src"
    destination = source_root / "chrome/app/theme/chromium/chromium.ico"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old icon")

    copied = install_assets(source_root, branding_root, manifest_path)

    assert destination in copied
    assert destination.read_bytes() == source_bytes["branding/generated/fk_chromium.ico"]
    assert not destination.with_name(destination.name + ".tmp").exists()


def test_install_assets_rejects_a_missing_source_before_any_write(tmp_path):
    """Silently skipping one absent source must fail without leaving a partial install."""
    assets = [
        {
            "source": "branding/generated/product_logo_16.png",
            "destination": "chrome/app/theme/chromium/product_logo_16.png",
        },
        {
            "source": "branding/generated/missing.png",
            "destination": "chrome/app/theme/chromium/missing.png",
        },
    ]
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path, assets)
    source_root = tmp_path / "src"

    with pytest.raises(FileNotFoundError, match="branding/generated/missing.png"):
        install_assets(source_root, branding_root, manifest_path)

    assert not source_root.exists()


@pytest.mark.parametrize(
    "destination",
    [
        "../outside.png",
        "assets/../outside.png",
        "/outside.png",
        r"C:\outside.png",
        r"\\server\share\outside.png",
        r"assets\..\outside.png",
    ],
)
def test_install_assets_rejects_destination_escape_without_writing(tmp_path, destination):
    """Accepting any cross-platform escape spelling must fail this write boundary."""
    assets = [
        {
            "source": "branding/generated/product_logo_16.png",
            "destination": destination,
        }
    ]
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path, assets)
    source_root = tmp_path / "src"

    with pytest.raises(ValueError, match="destination.*relative"):
        install_assets(source_root, branding_root, manifest_path)

    assert not source_root.exists()
    assert not (tmp_path / "outside.png").exists()


@pytest.mark.parametrize(
    "source",
    [
        "../outside.png",
        "/outside.png",
        r"C:\outside.png",
        r"\\server\share\outside.png",
        r"branding\..\outside.png",
    ],
)
def test_install_assets_rejects_source_escape(tmp_path, source):
    """Reading an asset outside the branding root must fail before destination writes."""
    assets = [
        {
            "source": source,
            "destination": "chrome/app/theme/chromium/product_logo_16.png",
        }
    ]
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path, assets)
    source_root = tmp_path / "src"

    with pytest.raises(ValueError, match="source.*relative"):
        install_assets(source_root, branding_root, manifest_path)

    assert not source_root.exists()


def test_install_assets_rejects_a_symlinked_destination_parent(tmp_path):
    """Following a destination-directory symlink outside source_root must fail this boundary."""
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path)
    source_root = tmp_path / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    (source_root / "chrome/app/theme").mkdir(parents=True)
    (source_root / "chrome/app/theme/chromium").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="destination.*source root"):
        install_assets(source_root, branding_root, manifest_path)

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX race simulation")
def test_install_assets_rejects_root_swap_before_directory_creation(
    tmp_path, monkeypatch
):
    """Following a root swapped after preflight must not create anything outside it."""
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path)
    source_root = tmp_path / "src"
    source_root.mkdir()
    detached = tmp_path / "detached-src"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_copy = install_brand_assets._copy_atomically

    def copy_after_swap(source, destination, validated_root):
        source_root.rename(detached)
        source_root.symlink_to(outside, target_is_directory=True)
        return original_copy(source, destination, validated_root)

    monkeypatch.setattr(install_brand_assets, "_copy_atomically", copy_after_swap)

    with pytest.raises((OSError, RuntimeError, ValueError)):
        install_assets(source_root, branding_root, manifest_path)

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX race simulation")
def test_install_assets_rejects_ancestor_swap_before_temporary_creation(
    tmp_path, monkeypatch
):
    """A validated parent swapped just before the temp open must fail closed."""
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path)
    source_root = tmp_path / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = install_brand_assets._open_posix_temporary

    def open_after_swap(parent, temporary_name):
        theme = source_root / "chrome/app/theme"
        detached = tmp_path / "detached-theme"
        theme.rename(detached)
        theme.symlink_to(outside, target_is_directory=True)
        return original_open(parent, temporary_name)

    monkeypatch.setattr(
        install_brand_assets, "_open_posix_temporary", open_after_swap
    )

    with pytest.raises((OSError, RuntimeError, ValueError)):
        install_assets(source_root, branding_root, manifest_path)

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX race simulation")
def test_install_assets_rejects_ancestor_swap_before_final_replacement(
    tmp_path, monkeypatch
):
    """A validated parent swapped just before the atomic rename must fail closed."""
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path)
    source_root = tmp_path / "src"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_replace = install_brand_assets._replace_posix_temporary

    def replace_after_swap(parent, temporary_name, destination_name, temporary_fd):
        theme = source_root / "chrome/app/theme"
        detached = tmp_path / "detached-theme"
        theme.rename(detached)
        theme.symlink_to(outside, target_is_directory=True)
        return original_replace(
            parent, temporary_name, destination_name, temporary_fd
        )

    monkeypatch.setattr(
        install_brand_assets, "_replace_posix_temporary", replace_after_swap
    )

    with pytest.raises((OSError, RuntimeError, ValueError)):
        install_assets(source_root, branding_root, manifest_path)

    assert list(outside.iterdir()) == []
    assert not (outside / "chromium.ico.tmp").exists()


def test_windows_directory_open_rejects_reparse_points(monkeypatch):
    """A Windows junction/reparse directory must be closed and rejected."""

    class Kernel32:
        def __init__(self):
            self.closed = []

        def CreateFileW(self, *_args):
            return 41

        def CloseHandle(self, handle):
            self.closed.append(handle)

    kernel32 = Kernel32()
    fake_ctypes = SimpleNamespace(
        c_void_p=lambda value: SimpleNamespace(value=value),
        get_last_error=lambda: 0,
        WinError=OSError,
    )
    api = (fake_ctypes, None, kernel32, None)
    monkeypatch.setattr(
        install_brand_assets,
        "_windows_information",
        lambda _api, _handle: SimpleNamespace(dwFileAttributes=0x00000010 | 0x00000400),
    )

    with pytest.raises(ValueError, match="reparse point"):
        install_brand_assets._open_windows_directory(api, r"C:\\src")

    assert kernel32.closed == [41]


def test_windows_directory_handles_deny_write_and_delete_sharing(monkeypatch):
    """Allowing another writer/deleter to open a held ancestor breaks containment."""

    class Kernel32:
        def __init__(self):
            self.create_calls = []

        def CreateFileW(self, *args):
            self.create_calls.append(args)
            return 51

        def CloseHandle(self, _handle):
            return True

    kernel32 = Kernel32()
    api = (ctypes, wintypes, kernel32, None)
    monkeypatch.setattr(
        install_brand_assets,
        "_windows_information",
        lambda _api, _handle: SimpleNamespace(dwFileAttributes=0x00000010),
    )

    handle = install_brand_assets._open_windows_directory(api, r"C:\\src")

    share_mode = kernel32.create_calls[0][2]
    assert share_mode == 0x00000001  # FILE_SHARE_READ only.
    assert not share_mode & 0x00000002  # No FILE_SHARE_WRITE.
    assert not share_mode & 0x00000004  # No FILE_SHARE_DELETE.
    kernel32.CloseHandle(handle)


def test_windows_directory_handle_closes_when_information_query_fails(monkeypatch):
    """A failed post-open safety query must not leak its directory handle."""

    class Kernel32:
        def __init__(self):
            self.closed = []

        def CreateFileW(self, *_args):
            return 61

        def CloseHandle(self, handle):
            self.closed.append(handle)

    kernel32 = Kernel32()
    api = (ctypes, wintypes, kernel32, None)

    def fail_information(_api, _handle):
        raise OSError("information query failed")

    monkeypatch.setattr(
        install_brand_assets, "_windows_information", fail_information
    )

    with pytest.raises(OSError, match="information query failed"):
        install_brand_assets._open_windows_directory(api, r"C:\\src")

    assert kernel32.closed == [61]


def test_windows_held_directories_block_writers_at_directory_temp_and_final_phases(
    tmp_path, monkeypatch
):
    """Ancestor handles must remain write-exclusive through both commit phases."""

    class Kernel32:
        def __init__(self):
            self.next_handle = 70
            self.directory_shares = {}
            self.closed = []

        def CreateFileW(self, _path, _access, share, *_args):
            handle = self.next_handle
            self.next_handle += 1
            self.directory_shares[handle] = share
            return handle

        def WriteFile(self, _handle, _buffer, length, written, _overlapped):
            written._obj.value = length
            return True

        def FlushFileBuffers(self, _handle):
            return True

        def CloseHandle(self, handle):
            self.closed.append(handle)

        def directory_mutation_is_blocked(self, handle):
            return not self.directory_shares[handle] & (0x00000002 | 0x00000004)

    kernel32 = Kernel32()
    api = (ctypes, wintypes, kernel32, None)
    phases = []
    monkeypatch.setattr(install_brand_assets, "_windows_api", lambda: api)
    monkeypatch.setattr(
        install_brand_assets,
        "_windows_information",
        lambda _api, _handle: SimpleNamespace(dwFileAttributes=0x00000010),
    )

    def create_directory(_path, **_kwargs):
        assert kernel32.directory_shares
        assert all(
            kernel32.directory_mutation_is_blocked(handle)
            for handle in kernel32.directory_shares
        )
        phases.append("directory")

    monkeypatch.setattr(install_brand_assets.os, "mkdir", create_directory)

    def open_temporary(held_parent, _path):
        assert all(
            kernel32.directory_mutation_is_blocked(handle)
            for handle in held_parent["handles"]
        )
        phases.append("temporary")
        return 99

    def replace_temporary(held_parent, _temporary_handle, _destination_name):
        assert all(
            kernel32.directory_mutation_is_blocked(handle)
            for handle in held_parent["handles"]
        )
        assert not any(handle in kernel32.closed for handle in held_parent["handles"])
        phases.append("final")

    monkeypatch.setattr(
        install_brand_assets, "_open_windows_temporary", open_temporary
    )
    monkeypatch.setattr(
        install_brand_assets, "_replace_windows_temporary", replace_temporary
    )
    source = tmp_path / "asset.bin"
    source.write_bytes(b"brand bytes")

    install_brand_assets._copy_windows(
        source, PureWindowsPath("C:/source-root"), Path("chrome/asset.bin")
    )

    assert phases == ["directory", "directory", "temporary", "final"]
    assert all(
        handle in kernel32.closed for handle in kernel32.directory_shares
    )


def test_install_assets_accepts_a_safe_shallow_destination(tmp_path):
    """Basing containment on a fixed destination depth must fail this safe copy."""
    assets = [
        {
            "source": "branding/generated/product_logo_16.png",
            "destination": "logo.png",
        }
    ]
    branding_root, manifest_path, source_bytes = _write_branding_fixture(tmp_path, assets)
    source_root = tmp_path / "src"

    assert install_assets(source_root, branding_root, manifest_path) == [source_root / "logo.png"]
    assert (source_root / "logo.png").read_bytes() == source_bytes[
        "branding/generated/product_logo_16.png"
    ]


def test_cli_logs_only_relative_destinations(tmp_path, capsys):
    """Leaking absolute workspace paths into build logs must fail this CLI contract."""
    branding_root, manifest_path, _ = _write_branding_fixture(tmp_path)
    source_root = tmp_path / "src"

    assert main(
        [
            "--source-root",
            str(source_root),
            "--branding-root",
            str(branding_root),
            "--manifest",
            str(manifest_path),
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "chrome/app/theme/chromium/chromium.ico" in output
    assert "chrome/app/theme/chromium/product_logo_16.png" in output
    assert str(tmp_path) not in output
