"""Create deterministic FK Chromium Windows release metadata and packages."""

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Sequence


CANONICAL_DECIMAL_COMPONENT_PATTERN = r"(?:0|[1-9][0-9]*)"
CANONICAL_POSITIVE_DECIMAL_COMPONENT_PATTERN = r"(?:[1-9][0-9]*)"
CHROMIUM_VERSION_PATTERN = re.compile(
    rf"{CANONICAL_DECIMAL_COMPONENT_PATTERN}"
    rf"(?:\.{CANONICAL_DECIMAL_COMPONENT_PATTERN}){{3}}"
)
PUBLIC_RELEASE_TAG_PATTERN = re.compile(
    rf"{CHROMIUM_VERSION_PATTERN.pattern}-fk\."
    rf"{CANONICAL_POSITIVE_DECIMAL_COMPONENT_PATTERN}"
)
_UPSTREAM_TAG_PATTERN = re.compile(
    rf"({CANONICAL_DECIMAL_COMPONENT_PATTERN})\."
    rf"({CANONICAL_DECIMAL_COMPONENT_PATTERN})\."
    rf"({CANONICAL_DECIMAL_COMPONENT_PATTERN})\."
    rf"({CANONICAL_DECIMAL_COMPONENT_PATTERN})-"
    rf"({CANONICAL_POSITIVE_DECIMAL_COMPONENT_PATTERN})\."
    rf"({CANONICAL_POSITIVE_DECIMAL_COMPONENT_PATTERN})"
)
MAX_FK_REVISION = 1_000_000


@dataclass(frozen=True)
class UpstreamVersion:
    """Every numeric component of one canonical upstream Windows tag."""

    tag: str
    version: str
    version_components: tuple[int, int, int, int]
    packaging_revision: int
    package_revision: int

    @property
    def sort_key(self):
        return self.version_components + (self.packaging_revision, self.package_revision)


@dataclass(frozen=True)
class ArtifactNames:
    """The two Windows x64 files published for an FK Chromium release."""

    installer: str
    portable: str


@dataclass(frozen=True)
class BuildIdentity:
    """One validated staged-build identity tuple."""

    upstream_tag: str
    upstream_version: str
    fk_revision: int
    release_tag: str
    windows_commit: str
    upstream_windows_commit: str
    upstream_commit: str
    branding_commit: str
    publish: bool


@dataclass(frozen=True)
class VerifiedReleaseArtifact(BuildIdentity):
    """One completely validated release artifact and its immutable identity."""

    files: tuple[Path, Path, Path]


def parse_upstream_tag(tag: str) -> UpstreamVersion:
    """Parse an exact ``<four-part>-<packaging>.<package>`` Windows tag."""
    match = re.fullmatch(_UPSTREAM_TAG_PATTERN, tag)
    if not match:
        raise ValueError(
            "Expected stable upstream Windows tag "
            f"<chromium-four-part>-<packaging>.<package>, got {tag!r}"
        )
    version = ".".join(match.group(index) for index in range(1, 5))
    return UpstreamVersion(
        tag=tag,
        version=version,
        version_components=tuple(int(match.group(index)) for index in range(1, 5)),
        packaging_revision=int(match.group(5)),
        package_revision=int(match.group(6)),
    )


def parse_fk_revision(value: str) -> int:
    """Parse one bounded canonical ASCII FK revision without normalizing its text."""
    maximum = str(MAX_FK_REVISION)
    if (
        not isinstance(value, str)
        or re.fullmatch(CANONICAL_DECIMAL_COMPONENT_PATTERN, value) is None
        or value == "0"
        or len(value) > len(maximum)
        or (len(value) == len(maximum) and value > maximum)
    ):
        raise ValueError(
            "FK revision must be a canonical ASCII positive decimal within the allowed range"
        )
    return int(value)


def _require_version(version: str) -> str:
    if (
        not isinstance(version, str)
        or CHROMIUM_VERSION_PATTERN.fullmatch(version) is None
    ):
        raise ValueError(f"Expected four-part Chromium version, got {version!r}")
    return version


def release_tag(version: str, revision: int) -> str:
    """Return the FK release tag for *version* and a positive FK revision."""
    _require_version(version)
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or revision > MAX_FK_REVISION
    ):
        raise ValueError(f"FK revision must be between 1 and {MAX_FK_REVISION}")
    return f"{version}-fk.{revision}"


def artifact_names(version: str) -> ArtifactNames:
    """Return the exact public installer and portable archive names."""
    _require_version(version)
    prefix = f"FK-Chromium-{version}-Windows-x64"
    return ArtifactNames(installer=f"{prefix}-Installer.exe", portable=f"{prefix}-Portable.zip")


_RELEASE_METADATA_FIELDS = {
    "branding_commit",
    "fk_revision",
    "force_rebuild",
    "manifest_sha256",
    "publish",
    "release_tag",
    "upstream_commit",
    "upstream_tag",
    "upstream_version",
    "upstream_windows_commit",
    "windows_build_hook_patch_sha256",
    "windows_commit",
}


def _read_release_metadata(path: Path):
    def reject_duplicate_keys(pairs):
        value = {}
        for key, entry in pairs:
            if key in value:
                raise ValueError(f"Duplicate release metadata field: {key}")
            value[key] = entry
        return value

    try:
        metadata = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read release metadata {path}: {error}") from error
    if not isinstance(metadata, dict) or set(metadata) != _RELEASE_METADATA_FIELDS:
        raise ValueError("Release metadata must contain exactly the supported identity fields")
    return metadata


def _require_hash(value, field, length):
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError(f"Release metadata {field} must be a lowercase {length}-digit hash")
    return value


def read_build_identity(path: Path):
    """Read and strictly validate the complete metadata sidecar."""
    metadata = _read_release_metadata(Path(path))
    upstream_tag = metadata.get("upstream_tag")
    if not isinstance(upstream_tag, str):
        raise ValueError("Release metadata upstream_tag must be a string")
    parsed = parse_upstream_tag(upstream_tag)
    if metadata.get("upstream_version") != parsed.version:
        raise ValueError("Release metadata upstream version does not match its canonical tag")
    revision = metadata.get("fk_revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or revision > MAX_FK_REVISION
    ):
        raise ValueError(
            f"Release metadata FK revision must be between 1 and {MAX_FK_REVISION}"
        )
    expected_release_tag = release_tag(parsed.version, revision)
    if metadata.get("release_tag") != expected_release_tag:
        raise ValueError("Release metadata public tag does not match its upstream identity")
    for field in ("force_rebuild", "publish"):
        if not isinstance(metadata.get(field), bool):
            raise ValueError(f"Release metadata {field} must be a JSON boolean")
    for field in (
        "windows_commit",
        "upstream_windows_commit",
        "upstream_commit",
        "branding_commit",
    ):
        _require_hash(metadata.get(field), field, 40)
    for field in ("manifest_sha256", "windows_build_hook_patch_sha256"):
        _require_hash(metadata.get(field), field, 64)
    return BuildIdentity(
        upstream_tag=parsed.tag,
        upstream_version=parsed.version,
        fk_revision=revision,
        release_tag=expected_release_tag,
        windows_commit=metadata["windows_commit"],
        upstream_windows_commit=metadata["upstream_windows_commit"],
        upstream_commit=metadata["upstream_commit"],
        branding_commit=metadata["branding_commit"],
        publish=metadata["publish"],
    )


def verify_release_artifact(directory: Path, *, require_publish: bool = False):
    """Validate exact filenames, identity metadata, and both public file hashes."""
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Release artifact directory does not exist: {directory}")
    metadata_path = directory / "fk-build-metadata.json"
    identity = read_build_identity(metadata_path)
    if require_publish and identity.publish is not True:
        raise ValueError("Release metadata does not grant publication")

    names = artifact_names(identity.upstream_version)
    installer = directory / names.installer
    portable = directory / names.portable
    checksums = directory / "SHA256SUMS.txt"
    expected_names = {
        installer.name,
        portable.name,
        checksums.name,
        metadata_path.name,
    }
    actual_names = set()
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Release artifact may contain only regular files")
        actual_names.add(path.name)
    if actual_names != expected_names:
        raise ValueError("Release artifact does not contain the exact four allowed files")

    expected_checksum_lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in (installer, portable)
    ]
    try:
        checksum_text = checksums.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Could not read {checksums}: {error}") from error
    if checksum_text != "\n".join(expected_checksum_lines) + "\n":
        raise ValueError("SHA256SUMS.txt does not exactly match the release files")

    return VerifiedReleaseArtifact(
        upstream_tag=identity.upstream_tag,
        upstream_version=identity.upstream_version,
        fk_revision=identity.fk_revision,
        release_tag=identity.release_tag,
        windows_commit=identity.windows_commit,
        upstream_windows_commit=identity.upstream_windows_commit,
        upstream_commit=identity.upstream_commit,
        branding_commit=identity.branding_commit,
        publish=identity.publish,
        files=(installer, portable, checksums),
    )


def write_sha256s(paths: Sequence[Path], output: Path) -> None:
    """Write standard, deterministic SHA-256 checksums for *paths*."""
    records = []
    for path in paths:
        path = Path(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append((path.name, digest))
    records.sort(key=lambda record: record[0])
    output = Path(output)
    output.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in records), encoding="utf-8"
    )


def _find_single_package(build_directory: Path, upstream_version: UpstreamVersion, suffix: str) -> Path:
    """Find one package whose source name contains the exact upstream tag."""
    pattern = re.compile(
        rf"ungoogled-chromium_{re.escape(upstream_version.tag)}{suffix}"
    )
    candidates = sorted(
        path for path in build_directory.iterdir() if path.is_file() and pattern.fullmatch(path.name)
    )
    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly one upstream {upstream_version.tag} {suffix} package "
            f"in {build_directory}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def package_artifacts(build_directory: Path, upstream_tag: str) -> ArtifactNames:
    """Rename upstream Windows packages and write checksums for the FK release files."""
    build_directory = Path(build_directory)
    upstream_version = parse_upstream_tag(upstream_tag)
    names = artifact_names(upstream_version.version)
    installer = _find_single_package(build_directory, upstream_version, r"_installer_x64\.exe")
    portable = _find_single_package(build_directory, upstream_version, r"_windows_x64\.zip")
    installer_target = build_directory / names.installer
    portable_target = build_directory / names.portable
    checksums_target = build_directory / "SHA256SUMS.txt"
    existing_outputs = (installer_target, portable_target, checksums_target)
    if any(path.exists() for path in existing_outputs):
        raise ValueError("FK package output already exists; refusing to overwrite an existing release artifact")
    installer.replace(installer_target)
    portable.replace(portable_target)
    write_sha256s((installer_target, portable_target), checksums_target)
    return names


def _metadata_tag(build_directory: Path) -> str:
    metadata_path = build_directory / "fk-build-metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read build metadata {metadata_path}: {error}") from error
    tag = metadata.get("upstream_tag") if isinstance(metadata, dict) else None
    if not isinstance(tag, str):
        raise ValueError(f"Build metadata {metadata_path} has no string upstream_tag")
    return tag


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    package_parser = subcommands.add_parser("package", help="rename packages and write checksums")
    package_parser.add_argument("--build-dir", type=Path, default=Path("build"))
    package_parser.add_argument("--upstream-tag")
    tag_parser = subcommands.add_parser("parse-tag", help="validate and describe an upstream tag")
    tag_parser.add_argument("--upstream-tag", required=True)
    verify_parser = subcommands.add_parser(
        "verify", help="validate the exact FK x64 release artifact"
    )
    verify_parser.add_argument("artifact_directory", type=Path)
    verify_parser.add_argument("--require-publish", action="store_true")
    args = parser.parse_args(arguments)
    try:
        if args.command == "package":
            upstream_tag = args.upstream_tag or _metadata_tag(args.build_dir)
            names = package_artifacts(args.build_dir, upstream_tag)
            print(
                json.dumps(
                    {"checksums": "SHA256SUMS.txt", "installer": names.installer, "portable": names.portable},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "parse-tag":
            parsed = parse_upstream_tag(args.upstream_tag)
            print(
                json.dumps(
                    {
                        "package_revision": parsed.package_revision,
                        "packaging_revision": parsed.packaging_revision,
                        "tag": parsed.tag,
                        "version": parsed.version,
                        "version_components": list(parsed.version_components),
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "verify":
            verified = verify_release_artifact(
                args.artifact_directory, require_publish=args.require_publish
            )
            print(
                json.dumps(
                    {
                        "branding_commit": verified.branding_commit,
                        "checksums": verified.files[2].name,
                        "fk_revision": verified.fk_revision,
                        "installer": verified.files[0].name,
                        "portable": verified.files[1].name,
                        "release_tag": verified.release_tag,
                        "upstream_commit": verified.upstream_commit,
                        "upstream_tag": verified.upstream_tag,
                        "upstream_version": verified.upstream_version,
                        "upstream_windows_commit": verified.upstream_windows_commit,
                        "windows_commit": verified.windows_commit,
                    },
                    sort_keys=True,
                )
            )
            return 0
    except (OSError, ValueError) as error:
        parser.exit(1, f"release_metadata: {error}\n")
    raise AssertionError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
