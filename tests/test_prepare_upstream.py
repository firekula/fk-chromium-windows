import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tools import prepare_upstream


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _REPOSITORY_ROOT / "tests" / "fixtures"


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _initialise_repository(path, tag=None):
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Preparation tests")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "fixture")
    if tag:
        _git(path, "tag", tag)


def _copy_fixture(tmp_path):
    root = tmp_path / "upstream-tree"
    branding_root = tmp_path / "branding-root"
    shutil.copytree(_FIXTURES / "upstream-tree", root)
    shutil.copytree(_FIXTURES / "branding", branding_root)
    _initialise_repository(root / "ungoogled-chromium", "151.0.0-1")
    _initialise_repository(branding_root)
    return root, branding_root


def _configure_fixture_sources(tmp_path, monkeypatch):
    overlay_root = tmp_path / "windows-overlays"
    shutil.copytree(_FIXTURES / "windows-overlays", overlay_root)
    monkeypatch.setattr(prepare_upstream, "_REPOSITORY_ROOT", overlay_root)
    return overlay_root


def _read_nonblank(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_prepare_tree_appends_fk_overlays_once(tmp_path, monkeypatch):
    """Removing either staged overlay copy must break this build-tree contract."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)

    result = prepare_upstream.prepare_tree(root, branding_root)
    prepare_upstream.prepare_tree(root, branding_root)

    assert _read_nonblank(root / "ungoogled-chromium" / "patches" / "series")[-1] == (
        "extra/fk-chromium/fk-product-branding.patch"
    )
    assert _read_nonblank(root / "patches" / "series")[-2:] == [
        "windows-base.patch",
        "fk-chromium/windows-product-identity.patch",
    ]
    assert (
        root
        / "ungoogled-chromium"
        / "patches"
        / "extra"
        / "fk-chromium"
        / "fk-product-branding.patch"
    ).read_text(encoding="utf-8") == (
        branding_root / "branding" / "patches" / "fk-product-branding.patch"
    ).read_text(encoding="utf-8")
    assert result.manifest["product"]["full_name"] == "FK Chromium"
    assert result.metadata["upstream_tag"] == "151.0.0-1"
    assert result.metadata["upstream_commit"] == _git(root / "ungoogled-chromium", "rev-parse", "HEAD")
    assert result.metadata["branding_commit"] == _git(branding_root, "rev-parse", "HEAD")
    assert result.metadata["manifest_sha256"] == subprocess.run(
        ["sha256sum", str(branding_root / "branding" / "manifest.json")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0]
    assert result.metadata["windows_build_hook_patch_sha256"] == subprocess.run(
        [
            "sha256sum",
            str(
                _FIXTURES
                / "windows-overlays"
                / "patches"
                / "fk-chromium"
                / "windows-build-brand-assets.patch"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()[0]
    assert json.loads((root / "build" / "fk-build-metadata.json").read_text(encoding="utf-8")) == result.metadata
    assert (root / "build.py").read_text(encoding="utf-8").count("install_brand_assets.py") == 1


def test_prepare_tree_reorders_existing_fk_overlays_to_the_end(tmp_path, monkeypatch):
    """Leaving an FK series entry before later patches must fail this ordering test."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)
    (root / "ungoogled-chromium" / "patches" / "series").write_text(
        "common-base.patch\nextra/fk-chromium/fk-product-branding.patch\ncommon-last.patch\n",
        encoding="utf-8",
    )
    (root / "patches" / "series").write_text(
        "windows-base.patch\n"
        "fk-chromium/windows-product-identity.patch\n"
        "fk-chromium/windows-build-brand-assets.patch\n"
        "windows-last.patch\n",
        encoding="utf-8",
    )

    prepare_upstream.prepare_tree(root, branding_root)

    assert _read_nonblank(root / "ungoogled-chromium" / "patches" / "series") == [
        "common-base.patch",
        "common-last.patch",
        "extra/fk-chromium/fk-product-branding.patch",
    ]
    assert _read_nonblank(root / "patches" / "series") == [
        "windows-base.patch",
        "windows-last.patch",
        "fk-chromium/windows-product-identity.patch",
    ]


def test_prepare_tree_applies_the_wrapper_hook_idempotently(tmp_path, monkeypatch):
    """Skipping or twice-applying the wrapper hook must fail this preparation contract."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)

    prepare_upstream.prepare_tree(root, branding_root)
    once = (root / "build.py").read_text(encoding="utf-8")
    prepare_upstream.prepare_tree(root, branding_root)

    assert once.count("install_brand_assets.py") == 1
    assert (root / "build.py").read_text(encoding="utf-8") == once


def test_prepare_tree_rejects_wrapper_source_drift(tmp_path, monkeypatch):
    """Ignoring a hook patch conflict must fail before an unbranded build starts."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)
    build_path = root / "build.py"
    build_path.write_text(
        build_path.read_text(encoding="utf-8").replace("# Substitute domains", "# Changed upstream"),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="build hook patch.*neither applies nor reverses"):
        prepare_upstream.prepare_tree(root, branding_root)

    assert "install_brand_assets.py" not in build_path.read_text(encoding="utf-8")


def test_prepare_tree_rejects_a_missing_manifest(tmp_path, monkeypatch):
    """Replacing the missing-manifest guard with a default must fail this test."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)
    (branding_root / "branding" / "manifest.json").unlink()

    with pytest.raises(FileNotFoundError, match="branding manifest"):
        prepare_upstream.prepare_tree(root, branding_root)


def test_prepare_tree_rejects_manifest_destination_traversal(tmp_path, monkeypatch):
    """Allowing an asset destination to escape its source root must fail this test."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)
    manifest_path = branding_root / "branding" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0]["destination"] = "../outside.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="destination.*relative"):
        prepare_upstream.prepare_tree(root, branding_root)

    assert not (tmp_path / "outside.png").exists()


@pytest.mark.parametrize(
    "destination",
    [
        "/outside.png",
        "assets/../outside.png",
        r"C:\outside.png",
        r"\\server\share\outside.png",
        r"assets\..\outside.png",
    ],
)
def test_prepare_tree_rejects_cross_platform_manifest_destination_escape(tmp_path, monkeypatch, destination):
    """Accepting any absolute or traversal spelling must fail this boundary check."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)
    manifest_path = branding_root / "branding" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0]["destination"] = destination
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="relative"):
        prepare_upstream.prepare_tree(root, branding_root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("full_name", "Chromium"),
        ("chinese_name", "FK Chromium"),
    ],
)
def test_prepare_tree_rejects_an_unexpected_product_identity(tmp_path, monkeypatch, field, value):
    """Weakening either fixed visible product identity must fail this validation."""
    root, branding_root = _copy_fixture(tmp_path)
    _configure_fixture_sources(tmp_path, monkeypatch)
    manifest_path = branding_root / "branding" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["product"][field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=field):
        prepare_upstream.prepare_tree(root, branding_root)


def test_main_uses_planned_defaults_and_explicit_roots(tmp_path, monkeypatch, capsys):
    """Dropping either CLI root selection must fail this CI entrypoint contract."""
    root, branding_root = _copy_fixture(tmp_path)
    branding_root.rename(root / "fk-branding")
    _configure_fixture_sources(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    assert prepare_upstream.main([]) == 0
    default_output = json.loads(capsys.readouterr().out)
    assert default_output["prepared_root"] == str(root)
    assert default_output["metadata"]["upstream_tag"] == "151.0.0-1"

    monkeypatch.chdir(tmp_path)
    assert prepare_upstream.main(
        ["--upstream-root", str(root), "--branding-root", str(root / "fk-branding")]
    ) == 0
    explicit_output = json.loads(capsys.readouterr().out)
    assert explicit_output == default_output


def test_prepare_tree_reports_missing_windows_patch_input(tmp_path, monkeypatch):
    """Ignoring an absent Task 4 patch would leave a broken patch series."""
    root, branding_root = _copy_fixture(tmp_path)
    overlay_root = _configure_fixture_sources(tmp_path, monkeypatch)
    (overlay_root / "patches" / "fk-chromium" / "windows-product-identity.patch").unlink()

    with pytest.raises(FileNotFoundError, match="Missing Windows patch input"):
        prepare_upstream.prepare_tree(root, branding_root)


def test_prepare_tree_reports_missing_wrapper_hook_patch(tmp_path, monkeypatch):
    """Ignoring an absent wrapper hook would allow builds without binary branding."""
    root, branding_root = _copy_fixture(tmp_path)
    overlay_root = _configure_fixture_sources(tmp_path, monkeypatch)
    (overlay_root / "patches/fk-chromium/windows-build-brand-assets.patch").unlink()

    with pytest.raises(FileNotFoundError, match="Windows build hook patch"):
        prepare_upstream.prepare_tree(root, branding_root)


def test_submodule_uses_the_fk_chromium_fork():
    """Pointing manual clones at another source must fail this configuration contract."""
    url = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(_REPOSITORY_ROOT / ".gitmodules"),
            "--get",
            "submodule.ungoogled-chromium.url",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert url == "https://github.com/firekula/fk-chromium.git"
