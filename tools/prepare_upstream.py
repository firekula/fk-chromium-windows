"""Stage FK Chromium overlays in a checked-out upstream build tree."""

import argparse
from pathlib import Path
import json
import shutil
import subprocess


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_COMMON_PATCH_ENTRY = "extra/fk-chromium/fk-product-branding.patch"
_WINDOWS_PATCH_ENTRIES = (
    "fk-chromium/windows-product-identity.patch",
)
_WINDOWS_BUILD_HOOK_PATCH = "windows-build-brand-assets.patch"
_WINDOWS_BUILD_HOOK_SERIES_ENTRY = f"fk-chromium/{_WINDOWS_BUILD_HOOK_PATCH}"


class PreparedTree:
    """The manifest and provenance recorded for a prepared build tree."""

    def __init__(self, root: Path, manifest: dict, metadata: dict):
        self.root = root
        self.manifest = manifest
        self.metadata = metadata


def _require_relative_path(value, field_name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"Manifest {field_name} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    has_drive_prefix = len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
    candidate = Path(normalized)
    if (
        candidate.is_absolute()
        or normalized.startswith("/")
        or has_drive_prefix
        or ".." in normalized.split("/")
    ):
        raise ValueError(f"Manifest {field_name} must be a relative path without '..'")
    return candidate


def _load_manifest(branding_root: Path):
    manifest_path = branding_root / "branding" / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing branding manifest: {manifest_path}")
    with manifest_path.open(encoding="utf-8") as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, dict):
        raise ValueError("Branding manifest must contain a JSON object")
    product = manifest.get("product")
    if not isinstance(product, dict):
        raise ValueError("Branding manifest must contain a product object")
    if product.get("full_name") != "FK Chromium":
        raise ValueError("Branding manifest product.full_name must equal 'FK Chromium'")
    if product.get("chinese_name") != "火焰库拉浏览器":
        raise ValueError("Branding manifest product.chinese_name must equal '火焰库拉浏览器'")
    assets = manifest.get("assets", [])
    if not isinstance(assets, list):
        raise ValueError("Branding manifest assets must be a list")
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("Branding manifest assets must contain objects")
        _require_relative_path(asset.get("source"), "asset source")
        _require_relative_path(asset.get("destination"), "asset destination")
    return manifest, manifest_path


def _copy_overlay(source: Path, destination: Path, description: str):
    if not source.is_file():
        raise FileNotFoundError(f"Missing {description}: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)


def _append_series_entries(series_path: Path, entries, remove_entries=()):
    if not series_path.is_file():
        raise FileNotFoundError(f"Missing patch series: {series_path}")
    lines = series_path.read_text(encoding="utf-8").splitlines()
    entries = tuple(entries)
    managed_entries = set((*entries, *remove_entries))
    retained = [line for line in lines if line.strip() not in managed_entries]
    series_path.write_text("\n".join((*retained, *entries)) + "\n", encoding="utf-8")


def _git_apply_check(root: Path, patch_path: Path, reverse=False):
    command = ["git", "-C", str(root), "apply", "--check"]
    if reverse:
        command.append("--reverse")
    command.append(str(patch_path.resolve()))
    return subprocess.run(command, capture_output=True, text=True)


def _apply_windows_build_hook(upstream_root: Path, patch_path: Path):
    if not patch_path.is_file():
        raise FileNotFoundError(f"Missing Windows build hook patch: {patch_path}")
    forward_check = _git_apply_check(upstream_root, patch_path)
    if forward_check.returncode == 0:
        subprocess.run(
            ["git", "-C", str(upstream_root), "apply", str(patch_path.resolve())],
            check=True,
            capture_output=True,
            text=True,
        )
        return
    reverse_check = _git_apply_check(upstream_root, patch_path, reverse=True)
    if reverse_check.returncode == 0:
        return
    details = "\n".join(
        part.strip()
        for part in (forward_check.stderr, reverse_check.stderr)
        if part.strip()
    )
    raise RuntimeError(
        "Windows build hook patch neither applies nor reverses cleanly"
        + (f":\n{details}" if details else "")
    )


def _git_output(repository: Path, *args):
    resolved_repository = repository.resolve()
    top_level = subprocess.run(
        ["git", "-C", str(resolved_repository), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if Path(top_level).resolve() != resolved_repository:
        raise RuntimeError(f"Expected a Git repository at {resolved_repository}")
    return subprocess.run(
        ["git", "-C", str(resolved_repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _sha256(path: Path):
    try:
        output = subprocess.run(
            ["sha256sum", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return output.split()[0].lower()
    except FileNotFoundError:
        output = subprocess.run(
            ["certutil", "-hashfile", str(path), "SHA256"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for line in output.splitlines():
            digest = "".join(character for character in line if character in "0123456789abcdefABCDEF")
            if len(digest) == 64:
                return digest.lower()
        raise RuntimeError(f"certutil did not produce a SHA-256 digest for {path}")


def _metadata(
    upstream_root: Path,
    branding_root: Path,
    manifest_path: Path,
    windows_build_hook_patch: Path,
):
    common_root = upstream_root / "ungoogled-chromium"
    return {
        "upstream_tag": _git_output(common_root, "describe", "--tags", "--exact-match", "HEAD"),
        "upstream_commit": _git_output(common_root, "rev-parse", "HEAD"),
        "branding_commit": _git_output(branding_root, "rev-parse", "HEAD"),
        "manifest_sha256": _sha256(manifest_path),
        "windows_build_hook_patch_sha256": _sha256(windows_build_hook_patch),
    }


def prepare_tree(upstream_root: Path, branding_root: Path) -> PreparedTree:
    """Validate and stage FK overlays in *upstream_root* without duplicating series entries."""
    upstream_root = Path(upstream_root).resolve()
    branding_root = Path(branding_root).resolve()
    manifest, manifest_path = _load_manifest(branding_root)

    common_patch = branding_root / "branding" / "patches" / "fk-product-branding.patch"
    common_patch_destination = (
        upstream_root / "ungoogled-chromium" / "patches" / "extra" / "fk-chromium" / "fk-product-branding.patch"
    )
    _copy_overlay(common_patch, common_patch_destination, "common branding patch")

    windows_source_root = _REPOSITORY_ROOT / "patches" / "fk-chromium"
    windows_destination_root = upstream_root / "patches" / "fk-chromium"
    for entry in _WINDOWS_PATCH_ENTRIES:
        patch_name = Path(entry).name
        _copy_overlay(
            windows_source_root / patch_name,
            windows_destination_root / patch_name,
            "Windows patch input",
        )

    windows_build_hook_patch = windows_source_root / _WINDOWS_BUILD_HOOK_PATCH
    _apply_windows_build_hook(upstream_root, windows_build_hook_patch)

    _append_series_entries(
        upstream_root / "ungoogled-chromium" / "patches" / "series",
        (_COMMON_PATCH_ENTRY,),
    )
    _append_series_entries(
        upstream_root / "patches" / "series",
        _WINDOWS_PATCH_ENTRIES,
        (_WINDOWS_BUILD_HOOK_SERIES_ENTRY,),
    )

    metadata = _metadata(
        upstream_root,
        branding_root,
        manifest_path,
        windows_build_hook_patch,
    )
    metadata_path = upstream_root / "build" / "fk-build-metadata.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return PreparedTree(upstream_root, manifest, metadata)


def main(arguments=None):
    """Prepare the planned checkout and emit deterministic provenance for CI logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream-root",
        type=Path,
        default=Path.cwd(),
        help="Build checkout root (default: current working directory)",
    )
    parser.add_argument(
        "--branding-root",
        type=Path,
        help="Branding checkout root (default: <upstream-root>/fk-branding)",
    )
    args = parser.parse_args(arguments)
    upstream_root = args.upstream_root
    branding_root = args.branding_root or upstream_root / "fk-branding"
    try:
        prepared = prepare_tree(upstream_root, branding_root)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"prepare_upstream: {error}\n")
    print(
        json.dumps(
            {"metadata": prepared.metadata, "prepared_root": str(prepared.root)},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
