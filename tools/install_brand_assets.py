"""Install FK Chromium binary brand assets into a Chromium source tree."""

import argparse
import errno
import json
import os
from pathlib import Path
import stat


def _relative_manifest_path(value, field_name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"Manifest asset {field_name} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    has_drive_prefix = len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
    segments = normalized.split("/")
    if (
        normalized.startswith("/")
        or has_drive_prefix
        or "" in segments
        or "." in segments
        or ".." in segments
    ):
        raise ValueError(
            f"Manifest asset {field_name} must be a normalized relative path without '..'"
        )
    return Path(*segments)


def _load_assets(manifest_path):
    with Path(manifest_path).open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, dict):
        raise ValueError("Branding manifest must contain a JSON object")
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Branding manifest assets must be a list")
    return assets


def _preflight(source_root, branding_root, manifest_path):
    # Keep this path lexical. Resolving it would turn a source-root symlink into
    # permission to write to the symlink target; the copy backends validate and
    # anchor each path component without following links or reparse points.
    source_root = Path(os.path.abspath(os.fspath(source_root)))
    branding_root = Path(branding_root).resolve()
    planned = []
    destinations = set()
    for asset in _load_assets(manifest_path):
        if not isinstance(asset, dict):
            raise ValueError("Branding manifest assets must contain objects")
        source_relative = _relative_manifest_path(asset.get("source"), "source")
        destination_relative = _relative_manifest_path(asset.get("destination"), "destination")
        source = (branding_root / source_relative).resolve()
        if not source.is_relative_to(branding_root):
            raise ValueError("Manifest asset source resolves outside the branding root")
        if not source.is_file():
            raise FileNotFoundError(f"Missing brand asset source: {source_relative.as_posix()}")
        destination = source_root / destination_relative
        resolved_parent = destination.parent.resolve()
        if not resolved_parent.is_relative_to(source_root):
            raise ValueError("Manifest asset destination resolves outside the source root")
        if destination_relative in destinations:
            raise ValueError(
                f"Manifest contains duplicate asset destination: {destination_relative.as_posix()}"
            )
        destinations.add(destination_relative)
        planned.append((destination_relative, source, destination))
    return source_root, sorted(planned, key=lambda item: item[0].as_posix())


def _posix_directory_flags():
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise RuntimeError("This POSIX platform lacks no-follow directory primitives")
    if any(
        function not in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.stat, os.unlink, os.rename)
    ):
        raise RuntimeError("This POSIX platform lacks anchored path primitives")
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_posix_destination_parent(source_root, destination_relative):
    """Return an fd-anchored, no-follow chain to the destination parent."""
    directory_flags = _posix_directory_flags()
    if not source_root.is_absolute() or source_root == Path(source_root.anchor):
        raise ValueError("Source root must be a non-root absolute path")

    handles = []
    entries = []
    current_path = Path(source_root.anchor)
    current_fd = os.open(source_root.anchor, directory_flags)
    handles.append(current_fd)
    source_parts = source_root.parts[1:]
    components = [*source_parts, *destination_relative.parent.parts]
    source_root_index = len(source_parts) - 1
    try:
        for index, component in enumerate(components):
            if index >= source_root_index:
                try:
                    os.mkdir(component, mode=0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
            try:
                child_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError(
                        "Manifest asset destination resolves outside the source root"
                    ) from error
                raise
            current_path /= component
            handles.append(child_fd)
            entries.append((current_fd, component, child_fd, current_path))
            current_fd = child_fd
        parent = {"fd": current_fd, "handles": handles, "entries": entries}
        _verify_posix_parent(parent)
        return parent
    except BaseException:
        for handle in reversed(handles):
            os.close(handle)
        raise


def _verify_posix_parent(parent):
    """Fail if any lexical directory entry no longer names its held handle."""
    for parent_fd, name, child_fd, path in parent["entries"]:
        entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(child_fd)
        if (
            not stat.S_ISDIR(entry.st_mode)
            or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ValueError(f"Validated destination directory was replaced: {path}")


def _open_posix_temporary(parent, temporary_name):
    _verify_posix_parent(parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    return os.open(temporary_name, flags, 0o600, dir_fd=parent["fd"])


def _replace_posix_temporary(parent, temporary_name, destination_name, temporary_fd):
    _verify_posix_parent(parent)
    entry = os.stat(temporary_name, dir_fd=parent["fd"], follow_symlinks=False)
    opened = os.fstat(temporary_fd)
    if (
        not stat.S_ISREG(entry.st_mode)
        or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise RuntimeError("Brand asset temporary file was replaced")
    os.rename(
        temporary_name,
        destination_name,
        src_dir_fd=parent["fd"],
        dst_dir_fd=parent["fd"],
    )


def _copy_posix(source, source_root, destination_relative):
    parent = _open_posix_destination_parent(source_root, destination_relative)
    temporary_name = destination_relative.name + ".tmp"
    temporary_fd = None
    temporary_exists = False
    try:
        temporary_fd = _open_posix_temporary(parent, temporary_name)
        temporary_exists = True
        with source.open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(temporary_fd, view)
                    view = view[written:]
        os.fsync(temporary_fd)
        _replace_posix_temporary(
            parent, temporary_name, destination_relative.name, temporary_fd
        )
        temporary_exists = False
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent["fd"])
            except FileNotFoundError:
                pass
        for handle in reversed(parent["handles"]):
            os.close(handle)


def _windows_api():
    """Load the Win32 calls required for handle-anchored installation."""
    import ctypes
    from ctypes import wintypes

    if not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("Win32 no-follow primitives are unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel32, ByHandleFileInformation


def _windows_information(api, handle):
    ctypes, _, kernel32, information_type = api
    information = information_type()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return information


def _open_windows_directory(api, path):
    ctypes, _, kernel32, _ = api
    generic_read = 0x80000000
    share_read = 0x00000001
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        generic_read,
        share_read,  # Exclude writer/reparse mutation, rename, and deletion.
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _windows_information(api, handle)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    directory = 0x00000010
    reparse_point = 0x00000400
    if not information.dwFileAttributes & directory or information.dwFileAttributes & reparse_point:
        kernel32.CloseHandle(handle)
        raise ValueError("Destination path contains a reparse point")
    return handle


def _open_windows_destination_parent(source_root, destination_relative):
    api = _windows_api()
    _, _, kernel32, _ = api
    if not source_root.drive or source_root.anchor.startswith("\\\\"):
        raise RuntimeError("Only local-drive Windows source roots are supported safely")
    handles = []
    paths = []
    current = Path(source_root.anchor)
    try:
        root_handle = _open_windows_directory(api, current)
        handles.append(root_handle)
        paths.append(current)
        source_parts = source_root.parts[1:]
        components = [*source_parts, *destination_relative.parent.parts]
        source_root_index = len(source_parts) - 1
        for index, component in enumerate(components):
            current /= component
            if index >= source_root_index:
                try:
                    os.mkdir(current)
                except FileExistsError:
                    pass
            handle = _open_windows_directory(api, current)
            handles.append(handle)
            paths.append(current)
        parent = {"api": api, "fd": handles[-1], "handles": handles, "paths": paths}
        _verify_windows_parent(parent)
        return parent
    except BaseException:
        for handle in reversed(handles):
            kernel32.CloseHandle(handle)
        raise


def _verify_windows_parent(parent):
    directory = 0x00000010
    reparse_point = 0x00000400
    for path, handle in zip(parent["paths"], parent["handles"]):
        information = _windows_information(parent["api"], handle)
        if (
            not information.dwFileAttributes & directory
            or information.dwFileAttributes & reparse_point
        ):
            raise ValueError(f"Validated destination directory became unsafe: {path}")


def _open_windows_temporary(parent, path):
    ctypes, _, kernel32, _ = parent["api"]
    _verify_windows_parent(parent)
    generic_write = 0x40000000
    delete_access = 0x00010000
    create_new = 1
    normal_attribute = 0x00000080
    open_reparse_point = 0x00200000
    handle = kernel32.CreateFileW(
        str(path),
        generic_write | delete_access,
        0,
        None,
        create_new,
        normal_attribute | open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _replace_windows_temporary(parent, temporary_handle, destination_name):
    ctypes, wintypes, kernel32, _ = parent["api"]
    _verify_windows_parent(parent)
    encoded_name = destination_name.encode("utf-16-le")

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    buffer_size = FileRenameInfo.FileName.offset + len(encoded_name)
    buffer = ctypes.create_string_buffer(buffer_size)
    information = ctypes.cast(buffer, ctypes.POINTER(FileRenameInfo)).contents
    information.ReplaceIfExists = True
    information.RootDirectory = parent["fd"]
    information.FileNameLength = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + FileRenameInfo.FileName.offset,
        encoded_name,
        len(encoded_name),
    )
    file_rename_info = 3
    if not kernel32.SetFileInformationByHandle(
        temporary_handle, file_rename_info, buffer, buffer_size
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _dispose_windows_temporary(parent, temporary_handle):
    ctypes, wintypes, kernel32, _ = parent["api"]

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    information = FileDispositionInfo(True)
    file_disposition_info = 4
    if not kernel32.SetFileInformationByHandle(
        temporary_handle,
        file_disposition_info,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _copy_windows(source, source_root, destination_relative):
    parent = _open_windows_destination_parent(source_root, destination_relative)
    ctypes, wintypes, kernel32, _ = parent["api"]
    temporary_path = source_root / destination_relative.parent / (
        destination_relative.name + ".tmp"
    )
    temporary_handle = None
    renamed = False
    try:
        temporary_handle = _open_windows_temporary(parent, temporary_path)
        with source.open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                buffer = ctypes.create_string_buffer(chunk)
                written = wintypes.DWORD()
                if not kernel32.WriteFile(
                    temporary_handle,
                    buffer,
                    len(chunk),
                    ctypes.byref(written),
                    None,
                ) or written.value != len(chunk):
                    raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.FlushFileBuffers(temporary_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        _replace_windows_temporary(parent, temporary_handle, destination_relative.name)
        renamed = True
    finally:
        cleanup_error = None
        if temporary_handle is not None:
            if not renamed:
                try:
                    _dispose_windows_temporary(parent, temporary_handle)
                except OSError as error:
                    cleanup_error = error
            kernel32.CloseHandle(temporary_handle)
        for handle in reversed(parent["handles"]):
            kernel32.CloseHandle(handle)
        if cleanup_error is not None:
            raise RuntimeError(
                "Could not securely remove brand asset temporary file"
            ) from cleanup_error


def _copy_atomically(source, destination, source_root):
    try:
        destination_relative = destination.relative_to(source_root)
    except ValueError as error:
        raise ValueError("Manifest asset destination resolves outside the source root") from error
    if os.name == "posix":
        _copy_posix(source, source_root, destination_relative)
    elif os.name == "nt":
        _copy_windows(source, source_root, destination_relative)
    else:
        raise RuntimeError("No race-safe brand asset installer exists for this platform")


def install_assets(source_root: Path, branding_root: Path, manifest_path: Path) -> list[Path]:
    """Copy manifest assets into *source_root* atomically and return destinations."""
    source_root, planned = _preflight(source_root, branding_root, manifest_path)
    copied = []
    for relative, source, destination in planned:
        _copy_atomically(source, destination, source_root)
        print(relative.as_posix())
        copied.append(destination)
    return copied


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--branding-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    install_assets(args.source_root, args.branding_root, args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
