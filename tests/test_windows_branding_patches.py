import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _REPOSITORY_ROOT / "tests" / "fixtures"
_PATCHES = _REPOSITORY_ROOT / "patches" / "fk-chromium"


def test_fk_patch_payloads_do_not_add_outer_diff_whitespace_errors():
    """Patch context must remain applicable without making the branch diff-check fail."""
    payloads = [*_PATCHES.glob("*.patch"), *(
        _FIXTURES / "windows-overlays" / "patches" / "fk-chromium"
    ).glob("*.patch")]

    assert payloads
    for payload in payloads:
        lines = payload.read_text(encoding="utf-8").splitlines()
        assert all(line == line.rstrip(" \t") for line in lines), payload


def _git_apply(root, patch):
    check = subprocess.run(
        ["git", "-C", str(root), "apply", "--check", str(patch)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr
    subprocess.run(
        ["git", "-C", str(root), "apply", str(patch)],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_blob(path):
    content = path.read_bytes()
    return hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_upstream_source_fixtures_match_recorded_provenance():
    """Substituting approximate target inputs must fail these exact-source fingerprints."""
    chromium_header = (
        _FIXTURES / "chromium-151/chrome/install_static/chromium_install_modes.h"
    )
    install_constants = (
        _FIXTURES / "chromium-151/chrome/install_static/install_constants.h"
    )
    windows_build = _FIXTURES / "windows-151/build.py"

    # Chromium tag 151.0.7922.173, commit a96602f30358e9b5d256a0464e7e4d4bec223004.
    assert _git_blob(chromium_header) == "f29a8228fe611cc2d0a9dcab08cd624f60d2ebdc"
    assert _sha256(chromium_header) == "23f38b39f941f596497a08b19b18cf58fbce8cbca3bc79355bba583cce05ea41"
    assert _git_blob(install_constants) == "82a827c2d075c00f74d50cd36a8a5937ac9cea06"
    assert _sha256(install_constants) == "664d25f2ba41441dfa29779f0f92bc9d2072a006782dfaf8d9fe43dc858ed9db"

    # ungoogled-chromium-windows tag 151.0.7922.173-1.1,
    # commit 63f51219bac808e0e5d1d5ba7958ad2aaa159dde.
    assert _git_blob(windows_build) == "2051e94f6187ecf9bc5f8d15645be074b06cc221"
    assert _sha256(windows_build) == "5d8c87805636d81563dbc2193984e13dda2eb8108ffb2890624c851ff1bd2cc2"


def test_exact_upstream_provenance_rejects_crlf_byte_drift(tmp_path):
    """Normalizing line endings before hashing must fail this byte provenance check."""
    source = _FIXTURES / "chromium-151/chrome/install_static/chromium_install_modes.h"
    drifted = tmp_path / "chromium_install_modes.h"
    drifted.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))

    assert _git_blob(drifted) != "f29a8228fe611cc2d0a9dcab08cd624f60d2ebdc"
    assert _sha256(drifted) != "23f38b39f941f596497a08b19b18cf58fbce8cbca3bc79355bba583cce05ea41"


def test_build_hook_patch_runs_asset_install_between_text_patches_and_substitution(tmp_path):
    """Moving or omitting binary installation must fail against the exact wrapper source."""
    root = tmp_path / "windows"
    root.mkdir()
    shutil.copy2(_FIXTURES / "windows-151/build.py", root / "build.py")
    patch = _PATCHES / "windows-build-brand-assets.patch"

    _git_apply(root, patch)

    modified = (root / "build.py").read_text(encoding="utf-8")
    invocation = """subprocess.run([
            sys.executable,
            str(_ROOT_DIR / 'tools' / 'install_brand_assets.py'),
            '--source-root', str(source_tree),
            '--branding-root', str(_ROOT_DIR / 'fk-branding'),
            '--manifest', str(_ROOT_DIR / 'fk-branding' / 'branding' / 'manifest.json'),
        ], check=True)"""
    assert invocation in modified
    assert modified.index("_ROOT_DIR / 'patches'") < modified.index(invocation)
    assert modified.index(invocation) < modified.index("# Substitute domains")
    reverse = subprocess.run(
        ["git", "-C", str(root), "apply", "--reverse", "--check", str(patch)],
        capture_output=True,
        text=True,
    )
    assert reverse.returncode == 0, reverse.stderr


def test_windows_identity_patch_isolates_the_current_chromium_mode_only(tmp_path):
    """Leaving an active Chromium 151 identity shared must fail this patched-source contract."""
    root = tmp_path / "chromium"
    shutil.copytree(_FIXTURES / "chromium-151", root)
    patch = _PATCHES / "windows-product-identity.patch"

    _git_apply(root, patch)

    target = (root / "chrome/install_static/chromium_install_modes.h").read_text(
        encoding="utf-8"
    )
    compact = re.sub(r"\s+", " ", target)
    assert 'kProductPathName[] = L"FK Chromium"' in target
    assert 'kSafeBrowsingName[] = "chromium"' in target
    assert '.install_suffix = L""' in compact
    assert '.app_guid = L"{385317E5-D454-45E9-9A3A-D240C07A3AC0}"' in compact
    assert '.base_app_name = L"FK Chromium"' in target
    assert '.base_app_id = L"FKChromium"' in target
    assert '.browser_prog_id_prefix = L"FKChromHTM"' in target
    assert '.direct_launch_url_scheme = "fkchromium"' in target
    assert '.pdf_prog_id_prefix = L"FKChromPDF"' in target
    assert 'L"{6782E06C-9D57-4F60-906F-7BD0B6C5C935}"' in target
    assert "{0xEFBECFD4, 0x69ED, 0x47B7," in compact
    assert "{0x90, 0xF9, 0x0A, 0xDD, 0x86, 0xCF, 0xD6, 0x3A}" in compact
    assert "{0xDA37F775, 0x297F, 0x4681," in compact
    assert "{0x8B, 0xE8, 0x02, 0xA1, 0x7F, 0x61, 0x88, 0x49}" in compact
    product_path = re.search(r'kProductPathName\[\] = L"([^"]+)"', target).group(1)
    install_suffix = re.search(r'\.install_suffix\s*=\s*L"([^"]*)"', target).group(1)
    assert product_path + install_suffix == "FK Chromium"
    assert (
        "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
        + product_path
        + install_suffix
        == "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\FK Chromium"
    )

    patch_text = patch.read_text(encoding="utf-8")
    assert "chromium_install_modes.h" in patch_text
    assert "chromium_install_modes.cc" not in patch_text
    assert "google_chrome_install_modes.h" not in patch_text
    assert "chrome.exe" not in patch_text


def test_retired_delegate_execute_id_remains_reserved_manifest_metadata_only():
    """Reintroducing Chromium's retired DelegateExecute field must fail this compatibility rule."""
    install_constants = (
        _FIXTURES / "chromium-151/chrome/install_static/install_constants.h"
    ).read_text(encoding="utf-8")
    manifest = json.loads(
        (_FIXTURES / "branding/branding/manifest.json").read_text(encoding="utf-8")
    )
    identity_patch = (_PATCHES / "windows-product-identity.patch").read_text(encoding="utf-8")

    assert "legacy_command_execute_clsid" not in install_constants
    assert (
        manifest["windows_identity"]["legacy_command_execute_clsid"]
        == "{15EEF2CB-C20F-4544-9E45-672B6F379E17}"
    )
    assert "15EEF2CB-C20F-4544-9E45-672B6F379E17" not in identity_patch
