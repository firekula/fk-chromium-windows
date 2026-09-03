from pathlib import Path
import hashlib
import json
import subprocess
import sys

import pytest

from tools.release_metadata import (
    UpstreamVersion,
    artifact_names,
    package_artifacts,
    parse_upstream_tag,
    release_tag,
    write_sha256s,
)


def test_artifact_names_are_exact():
    names = artifact_names("151.0.7922.173")

    assert names.installer == "FK-Chromium-151.0.7922.173-Windows-x64-Installer.exe"
    assert names.portable == "FK-Chromium-151.0.7922.173-Windows-x64-Portable.zip"
    assert release_tag("151.0.7922.173", 1) == "151.0.7922.173-fk.1"


@pytest.mark.parametrize("revision", (0, -1, 1_000_001))
def test_release_tag_rejects_nonpositive_or_oversized_revisions(revision):
    """Public revision parsing must share the selector's finite allocation bound."""
    with pytest.raises(ValueError):
        release_tag("151.0.7922.173", revision)


@pytest.mark.parametrize(
    "tag",
    (
        "151.0.7922",
        "151.0.7922.173",
        "151.0.7922.173-1",
        "151.0.7922.173-beta",
        "151.0.7922.173-0.1",
        "151.0.7922.173-1.0",
        "151.0.7922.173-1.1.1",
    ),
)
def test_parse_upstream_tag_rejects_non_release_tags(tag):
    with pytest.raises(ValueError):
        parse_upstream_tag(tag)


def test_parse_upstream_tag_returns_version_and_revision():
    parsed = parse_upstream_tag("151.0.7922.173-2.3")

    assert parsed == UpstreamVersion(
        tag="151.0.7922.173-2.3",
        version="151.0.7922.173",
        version_components=(151, 0, 7922, 173),
        packaging_revision=2,
        package_revision=3,
    )


@pytest.mark.parametrize(
    "tag",
    (
        "0151.0.7922.173-1.1",
        "151.00.7922.173-1.1",
        "151.0.7922.173-01.1",
        "151.0.7922.173-1.01",
        "١٥١.٠.٧٩٢٢.١٧٣-1.1",
        "１５１.０.７９２２.１７３-1.1",
    ),
)
def test_parse_upstream_tag_rejects_noncanonical_numeric_spellings(tag):
    """Numeric aliases must not create distinct identities for one Windows release."""
    with pytest.raises(ValueError):
        parse_upstream_tag(tag)


@pytest.mark.parametrize(
    "version",
    (
        "0151.0.7922.173",
        "151.00.7922.173",
        "١٥١.٠.٧٩٢٢.١٧٣",
        "１５１.０.７９２２.１７３",
    ),
)
def test_public_version_consumers_reject_noncanonical_numeric_spellings(version):
    """Public tags and artifact names must use the one canonical four-part spelling."""
    with pytest.raises(ValueError):
        release_tag(version, 1)
    with pytest.raises(ValueError):
        artifact_names(version)


def test_write_sha256s_uses_lexical_names_and_standard_format(tmp_path):
    portable = tmp_path / "FK-Chromium-Portable.zip"
    installer = tmp_path / "FK-Chromium-Installer.exe"
    portable.write_bytes(b"portable")
    installer.write_bytes(b"installer")
    checksums = tmp_path / "SHA256SUMS.txt"

    write_sha256s((portable, installer), checksums)

    assert checksums.read_text(encoding="utf-8").splitlines() == [
        f"{hashlib.sha256(installer.read_bytes()).hexdigest()}  {installer.name}",
        f"{hashlib.sha256(portable.read_bytes()).hexdigest()}  {portable.name}",
    ]


def _upstream_package_paths(build_directory, upstream_tag="151.0.7922.173-1.1"):
    return (
        build_directory / f"ungoogled-chromium_{upstream_tag}_installer_x64.exe",
        build_directory / f"ungoogled-chromium_{upstream_tag}_windows_x64.zip",
    )


def _write_upstream_packages(build_directory, upstream_tag="151.0.7922.173-1.1"):
    installer, portable = _upstream_package_paths(build_directory, upstream_tag)
    installer.write_bytes(b"installer")
    portable.write_bytes(b"portable")
    return installer, portable


def test_package_command_renames_exact_x64_upstream_packages_and_writes_checksums(tmp_path):
    build_directory = tmp_path / "build"
    build_directory.mkdir()
    installer, portable = _write_upstream_packages(build_directory)
    (build_directory / "fk-build-metadata.json").write_text(
        json.dumps({"upstream_tag": "151.0.7922.173-1.1"}), encoding="utf-8"
    )

    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "tools" / "release_metadata.py"),
            "package",
            "--build-dir",
            str(build_directory),
        ],
        check=True,
    )

    names = artifact_names("151.0.7922.173")
    assert not installer.exists()
    assert not portable.exists()
    assert (build_directory / names.installer).read_bytes() == b"installer"
    assert (build_directory / names.portable).read_bytes() == b"portable"
    assert (build_directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines() == [
        f"{hashlib.sha256(b'installer').hexdigest()}  {names.installer}",
        f"{hashlib.sha256(b'portable').hexdigest()}  {names.portable}",
    ]


def test_package_artifacts_rejects_a_different_upstream_revision_before_renaming(tmp_path):
    build_directory = tmp_path / "build"
    build_directory.mkdir()
    installer, portable = _write_upstream_packages(build_directory, "151.0.7922.173-2.1")

    with pytest.raises(ValueError):
        package_artifacts(build_directory, "151.0.7922.173-1.1")

    assert installer.exists()
    assert portable.exists()
    assert not (build_directory / artifact_names("151.0.7922.173").installer).exists()


def test_package_artifacts_rejects_shortened_tag_without_renaming_exact_packages(tmp_path):
    build_directory = tmp_path / "build"
    build_directory.mkdir()
    installer, portable = _write_upstream_packages(build_directory)

    with pytest.raises(ValueError):
        package_artifacts(build_directory, "151.0.7922.173-1")

    assert installer.is_file()
    assert portable.is_file()


@pytest.mark.parametrize(
    "filename",
    (
        "ungoogled-chromium_151.0.7922.173-1.1_installer_x86.exe",
        "ungoogled-chromium_151.0.7922.173-1.1_windows_arm64.zip",
        "ungoogled-chromium_151.0.7922.174-1.1_installer_x64.exe",
        "ungoogled-chromium_151.0.7922.174-1.1_windows_x64.zip",
    ),
)
def test_package_artifacts_rejects_wrong_architecture_or_version_without_renaming(tmp_path, filename):
    build_directory = tmp_path / "build"
    build_directory.mkdir()
    installer, portable = _write_upstream_packages(build_directory)
    invalid = build_directory / filename
    if invalid.suffix == ".exe":
        installer.unlink()
    else:
        portable.unlink()
    invalid.write_bytes(b"invalid")

    with pytest.raises(ValueError):
        package_artifacts(build_directory, "151.0.7922.173-1.1")

    assert invalid.exists()
    assert not (build_directory / artifact_names("151.0.7922.173").installer).exists()
    assert not (build_directory / "SHA256SUMS.txt").exists()


@pytest.mark.parametrize("existing_name", ("installer", "portable", "checksums"))
def test_package_artifacts_rejects_existing_targets_before_any_rename(tmp_path, existing_name):
    build_directory = tmp_path / "build"
    build_directory.mkdir()
    installer, portable = _write_upstream_packages(build_directory)
    names = artifact_names("151.0.7922.173")
    existing_paths = {
        "installer": build_directory / names.installer,
        "portable": build_directory / names.portable,
        "checksums": build_directory / "SHA256SUMS.txt",
    }
    existing_paths[existing_name].write_bytes(b"old")

    with pytest.raises(ValueError):
        package_artifacts(build_directory, "151.0.7922.173-1.1")

    assert installer.exists()
    assert portable.exists()
    assert existing_paths[existing_name].read_bytes() == b"old"


def test_stage_action_uses_only_exact_fk_x64_packaging():
    stage_action = (Path(__file__).parents[1] / ".github" / "actions" / "stage" / "index.js").read_text(
        encoding="utf-8"
    )

    successful_start = stage_action.index("if (retCode === 0)")
    successful_build = stage_action[
        successful_start : stage_action.index("core.setOutput('finished', true)", successful_start)
    ]
    assert "getBooleanInput('x86'" not in stage_action
    assert "getBooleanInput('arm'" not in stage_action
    assert "release_metadata.py" in successful_build
    assert "fk-chromium-windows-x64" in successful_build
    assert "FK-Chromium-*" not in successful_build


def test_stage_action_discovers_exact_x64_outputs_before_marking_finished():
    stage_action = (Path(__file__).parents[1] / ".github" / "actions" / "stage" / "index.js").read_text(
        encoding="utf-8"
    )

    assert "JSON.parse(packageOutput)" in stage_action
    assert "new Set(packageFiles).size !== 3" in stage_action
    assert "await Promise.all(packageList.map" in stage_action
    successful_build = stage_action.index("if (retCode === 0)")
    assert stage_action.index("await Promise.all(packageList.map", successful_build) < stage_action.index(
        "core.setOutput('finished', true)", successful_build
    )
