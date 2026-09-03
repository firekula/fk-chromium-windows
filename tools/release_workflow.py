"""Validate release workflow authorization boundaries and state transitions."""

import argparse
import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from .check_upstream import (
        Candidate,
        ReleaseState,
        decode_release_state_json,
        record_success,
    )
    from .release_metadata import (
        PUBLIC_RELEASE_TAG_PATTERN,
        parse_fk_revision,
        parse_upstream_tag,
        read_build_identity,
        release_tag,
        verify_release_artifact,
    )
else:
    from check_upstream import (
        Candidate,
        ReleaseState,
        decode_release_state_json,
        record_success,
    )
    from release_metadata import (
        PUBLIC_RELEASE_TAG_PATTERN,
        parse_fk_revision,
        parse_upstream_tag,
        read_build_identity,
        release_tag,
        verify_release_artifact,
    )


_BUILD_WORKFLOW_PATH = ".github/workflows/build-x64.yml"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_MAX_STATE_UPDATE_ATTEMPTS = 5
_PUBLIC_TAG_PATTERN = PUBLIC_RELEASE_TAG_PATTERN
_DRAFT_SLUG_PATTERN = re.compile(r"untagged-[0-9a-f]{20}")
_CREATED_DRAFT_NOTE_PATTERN = re.compile(
    r"created draft release repository=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    rf"tag=({_PUBLIC_TAG_PATTERN.pattern}) id=([1-9][0-9]*) "
    r"draft_slug=(untagged-[0-9a-f]{20})"
)
_UNCERTAIN_PUBLICATION_NOTE_PATTERN = re.compile(
    r"release publication uncertain repository=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) "
    rf"tag=({_PUBLIC_TAG_PATTERN.pattern}) id=([1-9][0-9]*)"
)
_RELEASE_CONTENT_TYPES = {
    ".exe": "application/vnd.microsoft.portable-executable",
    ".zip": "application/zip",
    ".txt": "text/plain",
}


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, "GitHub API redirect refused", headers, response)


_NO_REDIRECT_OPEN = build_opener(_NoRedirectHandler()).open


@dataclass(frozen=True)
class ValidatedRun:
    """Trusted fields derived from fresh Actions API responses."""

    run_id: int
    run_attempt: int
    head_sha: str
    event: str
    artifact_id: int
    artifact_name: str
    repository: str
    default_branch: str

    @property
    def run_url(self):
        return f"https://github.com/{self.repository}/actions/runs/{self.run_id}"

    @property
    def run_attempt_url(self):
        return f"{self.run_url}/attempts/{self.run_attempt}"


@dataclass(frozen=True)
class FailureReport:
    """Sanitized text and stable keys for one failed upstream version."""

    marker: str
    run_marker: str
    title: str
    body: str


@dataclass(frozen=True)
class FailureIdentity:
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
class PublicationAttempt:
    """Sanitized identity linking one publisher attempt to its verified build artifact."""

    publisher_run_id: int
    publisher_run_attempt: int
    source_run_id: int
    source_run_attempt: int
    source_artifact_id: int
    source_artifact_name: str
    upstream_tag: str
    upstream_version: str
    fk_revision: int
    release_tag: str
    windows_commit: str
    upstream_windows_commit: str
    upstream_commit: str
    branding_commit: str


def _positive_integer(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nested_full_name(value, field):
    if not isinstance(value, dict) or not isinstance(value.get("full_name"), str):
        raise ValueError(f"Workflow run {field} must identify a repository")
    return value["full_name"]


def validate_workflow_run(
    run,
    workflow,
    artifacts,
    *,
    repository_info,
    repository,
    default_branch,
    expected_conclusion,
    artifact_name,
    allowed_events=("workflow_dispatch", "workflow_call"),
    expected_run_attempt=None,
    expected_artifact_id=None,
    expected_workflow_name="build-x64",
    expected_workflow_path=_BUILD_WORKFLOW_PATH,
):
    """Authorize one completed same-repository build and its exact named artifact."""
    if not isinstance(repository, str) or _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Expected an exact owner/repository name")
    if not isinstance(default_branch, str) or not default_branch or "\n" in default_branch:
        raise ValueError("Expected a non-empty default branch")
    if expected_conclusion not in {"success", "failure"}:
        raise ValueError("Unsupported expected workflow conclusion")
    artifact_names = (artifact_name,) if isinstance(artifact_name, str) else tuple(artifact_name)
    if (
        not artifact_names
        or any(not isinstance(name, str) or not name or "/" in name for name in artifact_names)
        or len(set(artifact_names)) != len(artifact_names)
    ):
        raise ValueError("Expected one or more ordered literal artifact names")
    if not isinstance(run, dict) or not isinstance(workflow, dict):
        raise ValueError("Workflow API responses must be JSON objects")
    if not isinstance(repository_info, dict):
        raise ValueError("Repository API response must be a JSON object")
    if (
        repository_info.get("full_name") != repository
        or repository_info.get("default_branch") != default_branch
        or repository_info.get("fork") is not False
        or repository_info.get("archived") is not False
        or repository_info.get("disabled") is not False
        or repository_info.get("visibility") != "public"
    ):
        raise ValueError("Repository API response does not authorize this public default branch")

    run_id = _positive_integer(run.get("id"), "Workflow run id")
    run_attempt = _positive_integer(run.get("run_attempt"), "Workflow run attempt")
    if expected_run_attempt is not None and run_attempt != _positive_integer(
        expected_run_attempt, "Expected workflow run attempt"
    ):
        raise ValueError("Workflow run attempt changed after authorization")
    workflow_id = _positive_integer(workflow.get("id"), "Workflow id")
    if _positive_integer(run.get("workflow_id"), "Workflow run workflow_id") != workflow_id:
        raise ValueError("Workflow run does not belong to the fetched build workflow")
    if expected_workflow_name not in {"build-x64", "publish-release"}:
        raise ValueError("Expected workflow name is not authorized")
    canonical_paths = {
        "build-x64": _BUILD_WORKFLOW_PATH,
        "publish-release": ".github/workflows/publish-release.yml",
    }
    if expected_workflow_path != canonical_paths[expected_workflow_name]:
        raise ValueError("Expected workflow path is not canonical")
    if (
        workflow.get("name") != expected_workflow_name
        or run.get("name") != expected_workflow_name
    ):
        raise ValueError("Workflow run has the wrong canonical name")
    if (
        workflow.get("path") != expected_workflow_path
        or run.get("path") != expected_workflow_path
    ):
        raise ValueError("Workflow run path is not canonical")
    if workflow.get("state") != "active":
        raise ValueError("Authorized workflow is not active")
    if _nested_full_name(run.get("repository"), "repository") != repository:
        raise ValueError("Workflow run repository does not match this repository")
    if _nested_full_name(run.get("head_repository"), "head_repository") != repository:
        raise ValueError("Workflow run originated from a different repository")
    if run.get("head_branch") != default_branch:
        raise ValueError("Workflow run did not originate from the default branch")
    event = run.get("event")
    if not isinstance(event, str) or event not in set(allowed_events):
        raise ValueError("Workflow run event is not an authorized build entrypoint")
    if run.get("status") != "completed" or run.get("conclusion") != expected_conclusion:
        raise ValueError("Workflow run has the wrong completion status or conclusion")
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or _SHA_PATTERN.fullmatch(head_sha) is None:
        raise ValueError("Workflow run head SHA is invalid")

    if not isinstance(artifacts, dict):
        raise ValueError("Artifact API response must be a JSON object")
    entries = artifacts.get("artifacts")
    total_count = artifacts.get("total_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 0
        or total_count > 100
        or not isinstance(entries, list)
        or len(entries) != total_count
    ):
        raise ValueError("Artifact listing must be one complete bounded API page")
    matches = []
    for candidate_name in artifact_names:
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("name") == candidate_name
        ]
        if matches:
            break
    if len(matches) != 1:
        raise ValueError("Expected exactly one highest-priority authorized artifact")
    artifact = matches[0]
    artifact_id = _positive_integer(artifact.get("id"), "Artifact id")
    if expected_artifact_id is not None and artifact_id != _positive_integer(
        expected_artifact_id, "Expected artifact id"
    ):
        raise ValueError("Workflow artifact changed after authorization")
    if artifact.get("expired") is not False:
        raise ValueError("Authorized artifact is expired")
    artifact_run = artifact.get("workflow_run")
    if not isinstance(artifact_run, dict):
        raise ValueError("Artifact does not identify its workflow run")
    if artifact_run.get("id") != run_id or artifact_run.get("head_sha") != head_sha:
        raise ValueError("Artifact does not belong to the authorized workflow run")

    return ValidatedRun(
        run_id=run_id,
        run_attempt=run_attempt,
        head_sha=head_sha,
        event=event,
        artifact_id=artifact_id,
        artifact_name=artifact.get("name"),
        repository=repository,
        default_branch=default_branch,
    )


def bind_release_to_run(release, run: ValidatedRun):
    """Require artifact metadata to name the exact Windows commit that produced the run."""
    if classify_publication(release, run) is not True:
        raise ValueError("Release metadata does not grant publication")
    return release


def classify_publication(release, run: ValidatedRun):
    """Bind a validated artifact to its build and return its literal publication gate."""
    if not isinstance(run, ValidatedRun):
        raise TypeError("run must be a ValidatedRun")
    if release.windows_commit != run.head_sha:
        raise ValueError("Release metadata Windows commit does not match workflow run head SHA")
    if not isinstance(release.publish, bool):
        raise ValueError("Release metadata publish gate must be a boolean")
    return release.publish


def create_publication_attempt(
    release,
    source_run: ValidatedRun,
    *,
    publisher_run_id,
    publisher_run_attempt,
):
    """Create the only sanitized payload a publisher-failure reporter may consume."""
    if classify_publication(release, source_run) is not True:
        raise ValueError("Only publish=true builds may create a publication attempt")
    return {
        "branding_commit": release.branding_commit,
        "fk_revision": release.fk_revision,
        "publisher_run_attempt": _positive_integer(
            publisher_run_attempt, "Publisher run attempt"
        ),
        "publisher_run_id": _positive_integer(publisher_run_id, "Publisher run id"),
        "release_tag": release.release_tag,
        "source_artifact_id": source_run.artifact_id,
        "source_artifact_name": source_run.artifact_name,
        "source_run_attempt": source_run.run_attempt,
        "source_run_id": source_run.run_id,
        "upstream_commit": release.upstream_commit,
        "upstream_tag": release.upstream_tag,
        "upstream_version": release.upstream_version,
        "upstream_windows_commit": release.upstream_windows_commit,
        "windows_commit": release.windows_commit,
    }


def _publication_attempt_from_dict(value):
    fields = {
        "branding_commit", "fk_revision", "publisher_run_attempt", "publisher_run_id",
        "release_tag", "source_artifact_id", "source_artifact_name",
        "source_run_attempt", "source_run_id", "upstream_commit", "upstream_tag",
        "upstream_version", "upstream_windows_commit", "windows_commit",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Publication attempt JSON has the wrong fields")
    parsed = parse_upstream_tag(value["upstream_tag"])
    candidate = Candidate(
        parsed.tag, value["upstream_version"], value["fk_revision"]
    )
    if value["release_tag"] != release_tag(
        candidate.upstream_version, candidate.fk_revision
    ):
        raise ValueError("Publication attempt public tag is not canonical")
    for field in (
        "branding_commit", "upstream_commit", "upstream_windows_commit", "windows_commit"
    ):
        if not isinstance(value[field], str) or _SHA_PATTERN.fullmatch(value[field]) is None:
            raise ValueError(f"Publication attempt {field} must be a commit SHA")
    for field in (
        "publisher_run_attempt", "publisher_run_id", "source_artifact_id",
        "source_run_attempt", "source_run_id",
    ):
        _positive_integer(value[field], field)
    if value["source_artifact_name"] != "fk-chromium-windows-x64":
        raise ValueError("Publication attempt source artifact name is not authorized")
    return PublicationAttempt(**value)


def read_publication_attempt(path):
    return _publication_attempt_from_dict(_load_json(path))


def _candidate_from_metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Release metadata must be a JSON object")
    upstream_tag = metadata.get("upstream_tag")
    if not isinstance(upstream_tag, str):
        raise ValueError("Release metadata upstream_tag must be a string")
    parsed = parse_upstream_tag(upstream_tag)
    if metadata.get("upstream_version") != parsed.version:
        raise ValueError("Release metadata upstream version does not match its canonical tag")
    revision = metadata.get("fk_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Release metadata FK revision must be a positive integer")
    if metadata.get("release_tag") != release_tag(parsed.version, revision):
        raise ValueError("Release metadata public tag does not match its candidate")
    if metadata.get("publish") is not True:
        raise ValueError("Release metadata does not grant publication")
    return Candidate(
        upstream_tag=parsed.tag,
        upstream_version=parsed.version,
        fk_revision=revision,
    )


def _git_output(repository, *arguments):
    repository = Path(repository).resolve()
    try:
        top_level = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if Path(top_level).resolve() != repository:
            raise ValueError(f"Expected a Git repository at {repository}")
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Could not read Git identity at {repository}: {error}") from error


def _file_sha256(path):
    path = Path(path)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError(f"Could not hash release identity input {path}: {error}") from error


def create_build_metadata(
    *,
    windows_repository,
    upstream_windows_repository,
    branding_repository,
    upstream_tag,
    fk_revision,
    force_rebuild,
    publish,
):
    """Derive the complete build tuple before compilation from exact Git checkouts."""
    parsed = parse_upstream_tag(upstream_tag)
    if isinstance(fk_revision, bool) or not isinstance(fk_revision, int) or fk_revision < 1:
        raise ValueError("FK revision must be a positive integer")
    if not isinstance(force_rebuild, bool) or not isinstance(publish, bool):
        raise ValueError("Build flags must be booleans")
    windows_repository = Path(windows_repository).resolve()
    upstream_windows_repository = Path(upstream_windows_repository).resolve()
    branding_repository = Path(branding_repository).resolve()
    checked_out_tag = _git_output(
        upstream_windows_repository, "describe", "--tags", "--exact-match", "HEAD"
    )
    if checked_out_tag != parsed.tag:
        raise ValueError("Upstream Windows checkout does not match the requested exact tag")
    return {
        "branding_commit": _git_output(branding_repository, "rev-parse", "HEAD"),
        "fk_revision": fk_revision,
        "force_rebuild": force_rebuild,
        "manifest_sha256": _file_sha256(branding_repository / "branding" / "manifest.json"),
        "publish": publish,
        "release_tag": release_tag(parsed.version, fk_revision),
        "upstream_commit": _git_output(
            upstream_windows_repository / "ungoogled-chromium", "rev-parse", "HEAD"
        ),
        "upstream_tag": parsed.tag,
        "upstream_version": parsed.version,
        "upstream_windows_commit": _git_output(
            upstream_windows_repository, "rev-parse", "HEAD"
        ),
        "windows_build_hook_patch_sha256": _file_sha256(
            windows_repository / "patches" / "fk-chromium" / "windows-build-brand-assets.patch"
        ),
        "windows_commit": _git_output(windows_repository, "rev-parse", "HEAD"),
    }


def create_failure_identity(
    *, upstream_tag, fk_revision, publish, windows_commit,
    upstream_windows_commit="unresolved", upstream_commit="unresolved",
    branding_commit="unresolved",
):
    """Create immutable failure identity, allowing only explicit unresolved SHAs."""
    parsed = parse_upstream_tag(upstream_tag)
    if isinstance(fk_revision, bool) or not isinstance(fk_revision, int) or fk_revision < 1:
        raise ValueError("FK revision must be a positive integer")
    if not isinstance(publish, bool):
        raise ValueError("Publish must be a boolean")
    values = {
        "windows_commit": windows_commit,
        "upstream_windows_commit": upstream_windows_commit,
        "upstream_commit": upstream_commit,
        "branding_commit": branding_commit,
    }
    for field, value in values.items():
        if value != "unresolved" and (not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None):
            raise ValueError(f"Failure identity {field} must be a SHA or unresolved")
    wrapper_resolved = upstream_windows_commit != "unresolved"
    common_resolved = upstream_commit != "unresolved"
    if wrapper_resolved != common_resolved:
        raise ValueError("Upstream Windows wrapper/common must resolve jointly")
    return {
        "branding_commit": branding_commit,
        "fk_revision": fk_revision,
        "publish": publish,
        "release_tag": release_tag(parsed.version, fk_revision),
        "upstream_commit": upstream_commit,
        "upstream_tag": parsed.tag,
        "upstream_version": parsed.version,
        "upstream_windows_commit": upstream_windows_commit,
        "windows_commit": windows_commit,
    }


def read_failure_identity(path):
    """Read full metadata or the smaller immutable identity/locator format."""
    try:
        full = read_build_identity(path)
        return FailureIdentity(**asdict(full))
    except ValueError:
        value = _load_json(path)
        if not isinstance(value, dict) or set(value) != {
            "branding_commit", "fk_revision", "publish", "release_tag",
            "upstream_commit", "upstream_tag", "upstream_version",
            "upstream_windows_commit", "windows_commit",
        }:
            raise ValueError("Failure identity JSON has the wrong fields")
        canonical = create_failure_identity(
            upstream_tag=value["upstream_tag"],
            fk_revision=value["fk_revision"],
            publish=value["publish"],
            windows_commit=value["windows_commit"],
            upstream_windows_commit=value["upstream_windows_commit"],
            upstream_commit=value["upstream_commit"],
            branding_commit=value["branding_commit"],
        )
        if canonical != value:
            raise ValueError("Failure identity JSON is not canonical")
        return FailureIdentity(**canonical)


def record_success_json(state_payload, metadata):
    """Apply Task 7's validated success transition without dropping any history."""
    state = ReleaseState.from_dict(state_payload)
    candidate = _candidate_from_metadata(metadata)
    return record_success(state, candidate).to_dict()


def _github_request(method, url, token, payload):
    body = None if payload is None else json.dumps(payload, sort_keys=True).encode("utf-8")
    request = Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "fk-chromium-release-state",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with _NO_REDIRECT_OPEN(request, timeout=30) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        try:
            response_payload = json.load(error)
        except (OSError, UnicodeError, json.JSONDecodeError):
            response_payload = {"message": f"GitHub API returned HTTP {error.code}"}
        return error.code, response_payload
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"GitHub Contents API request failed: {error}") from error


def _decode_state_contents(payload):
    if not isinstance(payload, dict):
        raise ValueError("GitHub Contents API response must be an object")
    if (
        payload.get("encoding") != "base64"
        or payload.get("name") != "release-state.json"
        or payload.get("path") != "release-state.json"
        or payload.get("type") != "file"
    ):
        raise ValueError("GitHub Contents API returned the wrong state object")
    sha = payload.get("sha")
    content = payload.get("content")
    if not isinstance(sha, str) or _SHA_PATTERN.fullmatch(sha) is None:
        raise ValueError("GitHub Contents API returned an invalid blob SHA")
    if not isinstance(content, str) or len(content) > 2_000_000:
        raise ValueError("GitHub Contents API returned invalid state content")
    try:
        decoded = base64.b64decode(
            content.replace("\r", "").replace("\n", ""), validate=True
        ).decode("utf-8")
    except (ValueError, UnicodeError):
        raise ValueError("GitHub Contents API returned malformed release state content") from None
    try:
        state_payload = decode_release_state_json(decoded)
    except ValueError as error:
        raise ValueError(
            f"GitHub Contents API returned malformed release state: {error}"
        ) from None
    return sha, state_payload


def authorize_release_state(state_payload, metadata, *, contents_response=False):
    """Authorize one Task 7 reservation before any release-side mutation."""
    if contents_response:
        _, state_payload = _decode_state_contents(state_payload)
    state = ReleaseState.from_dict(state_payload)
    candidate = _candidate_from_metadata(metadata)
    if not state.is_assigned(candidate):
        raise ValueError("Release candidate does not match its exact Task 7 assignment")
    if state.is_successful(candidate):
        raise ValueError("Release candidate is already successful")
    trial_state = ReleaseState.from_dict(state.to_dict())
    record_success(trial_state, candidate)
    return metadata["release_tag"]


def authorize_release_state_via_contents_api(
    *, repository, branch, metadata, token, requester=_github_request
):
    """Read and authorize Task 7 state without performing any mutation."""
    if not isinstance(repository, str) or _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Expected an exact owner/repository name")
    if not isinstance(branch, str) or not branch or any(character in branch for character in "\r\n"):
        raise ValueError("Expected a safe non-empty default branch")
    if not isinstance(token, str) or not token:
        raise ValueError("GITHUB_TOKEN is required for release state authorization")
    owner, name = repository.split("/", 1)
    endpoint = (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(name, safe='')}/contents/release-state.json"
    )
    status, payload = requester(
        "GET", f"{endpoint}?{urlencode({'ref': branch})}", token, None
    )
    if status != 200:
        raise ValueError(f"GitHub Contents API state read failed with HTTP {status}")
    return authorize_release_state(payload, metadata, contents_response=True)


def update_release_state_via_contents_api(
    *,
    repository,
    branch,
    metadata,
    token,
    requester=_github_request,
    max_attempts=_MAX_STATE_UPDATE_ATTEMPTS,
):
    """Compare-and-swap Task 7 state after a release, retrying only write conflicts."""
    if not isinstance(repository, str) or _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Expected an exact owner/repository name")
    if not isinstance(branch, str) or not branch or any(character in branch for character in "\r\n"):
        raise ValueError("Expected a safe non-empty default branch")
    if not isinstance(token, str) or not token:
        raise ValueError("GITHUB_TOKEN is required for the state update")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
        or max_attempts > _MAX_STATE_UPDATE_ATTEMPTS
    ):
        raise ValueError("Invalid release state update retry bound")
    # Validate the candidate before making a network request.
    candidate = _candidate_from_metadata(metadata)
    owner, name = repository.split("/", 1)
    endpoint = (
        "https://api.github.com/repos/"
        f"{quote(owner, safe='')}/{quote(name, safe='')}/contents/release-state.json"
    )
    get_url = f"{endpoint}?{urlencode({'ref': branch})}"

    for attempt in range(max_attempts):
        status, current_payload = requester("GET", get_url, token, None)
        if status != 200:
            raise ValueError(f"GitHub Contents API state read failed with HTTP {status}")
        blob_sha, state_payload = _decode_state_contents(current_payload)
        updated = record_success_json(state_payload, metadata)
        # A concurrent rerun may already have recorded this exact success.
        if updated == ReleaseState.from_dict(state_payload).to_dict():
            return "unchanged"
        encoded = base64.b64encode(
            (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).decode("ascii")
        put_payload = {
            "branch": branch,
            "content": encoded,
            "message": f"ci: record successful release {candidate.upstream_tag} [skip ci]",
            "sha": blob_sha,
        }
        status, _ = requester("PUT", endpoint, token, put_payload)
        if status in {200, 201}:
            return "updated"
        if status not in {409, 422}:
            raise ValueError(f"GitHub Contents API state update failed with HTTP {status}")
        if attempt + 1 == max_attempts:
            raise ValueError("GitHub Contents API state update conflicted too many times")
    raise AssertionError("unreachable state update loop")


def validate_release_destination(
    *, repository, public_tag, windows_commit, token, requester=_github_request
):
    """Allow a new tag or an idempotent same-commit tag, rejecting occupied identities."""
    if not isinstance(repository, str) or _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Expected an exact owner/repository name")
    if (
        not isinstance(public_tag, str)
        or _PUBLIC_TAG_PATTERN.fullmatch(public_tag) is None
    ):
        raise ValueError("Expected a canonical FK public release tag")
    if not isinstance(windows_commit, str) or _SHA_PATTERN.fullmatch(windows_commit) is None:
        raise ValueError("Expected a lowercase Windows commit SHA")
    if not isinstance(token, str) or not token:
        raise ValueError("GITHUB_TOKEN is required for release destination validation")
    owner, name = repository.split("/", 1)
    root = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    ref_url = f"{root}/git/ref/tags/{quote(public_tag, safe='')}"
    status, payload = requester("GET", ref_url, token, None)
    if status == 404:
        return "available"
    if status != 200 or not isinstance(payload, dict):
        raise ValueError(f"GitHub release tag lookup failed with HTTP {status}")
    if payload.get("ref") != f"refs/tags/{public_tag}":
        raise ValueError("GitHub returned the wrong public release tag")
    target = payload.get("object")
    for _ in range(5):
        if not isinstance(target, dict):
            raise ValueError("GitHub release tag has no target object")
        target_type = target.get("type")
        target_sha = target.get("sha")
        if not isinstance(target_sha, str) or _SHA_PATTERN.fullmatch(target_sha) is None:
            raise ValueError("GitHub release tag target has an invalid SHA")
        if target_type == "commit":
            if target_sha != windows_commit:
                raise ValueError("Public release tag already points to a different commit")
            return "existing"
        if target_type != "tag":
            raise ValueError("Public release tag points to an unsupported Git object")
        status, annotated = requester("GET", f"{root}/git/tags/{target_sha}", token, None)
        if status != 200 or not isinstance(annotated, dict):
            raise ValueError(f"GitHub annotated tag lookup failed with HTTP {status}")
        target = annotated.get("object")
    raise ValueError("Public release tag annotation nesting exceeded the safety bound")


def _validate_created_release_ref(payload, *, repository, public_tag, windows_commit):
    if not isinstance(payload, dict):
        raise ValueError("GitHub created release ref response must be an object")
    api_root = f"https://api.github.com/repos/{repository}"
    target = payload.get("object")
    if (
        payload.get("ref") != f"refs/tags/{public_tag}"
        or payload.get("url") != f"{api_root}/git/refs/tags/{public_tag}"
        or not isinstance(target, dict)
        or target.get("type") != "commit"
        or target.get("sha") != windows_commit
        or target.get("url") != f"{api_root}/git/commits/{windows_commit}"
    ):
        raise ValueError("GitHub created release ref does not match the exact requested tag")


def _sanitized_created_draft_note(
    error,
    *,
    expected_repository,
    expected_release_tag,
    expected_release_id=None,
):
    """Return only a locator bound to trusted invocation and transaction values."""
    if (
        not isinstance(expected_repository, str)
        or _REPOSITORY_PATTERN.fullmatch(expected_repository) is None
        or not isinstance(expected_release_tag, str)
        or _PUBLIC_TAG_PATTERN.fullmatch(expected_release_tag) is None
    ):
        return None
    trusted_id = getattr(error, "_trusted_created_draft_release_id", None)
    if expected_release_id is not None:
        try:
            expected_release_id = _positive_integer(expected_release_id, "Expected release id")
        except (TypeError, ValueError):
            return None
        if trusted_id != expected_release_id:
            return None
    elif trusted_id is not None:
        try:
            expected_release_id = _positive_integer(trusted_id, "Created release id")
        except (TypeError, ValueError):
            return None
    trusted_phase = getattr(error, "_trusted_release_failure_phase", None)
    trusted_draft_slug = getattr(error, "_trusted_created_draft_slug", None)
    for note in getattr(error, "__notes__", ()):
        if not isinstance(note, str):
            continue
        draft_match = _CREATED_DRAFT_NOTE_PATTERN.fullmatch(note)
        if draft_match is not None:
            repository, release_tag, release_id_text, draft_slug = draft_match.groups()
            release_id = int(release_id_text)
            if (
                trusted_phase == "draft"
                and trusted_draft_slug == draft_slug
                and _DRAFT_SLUG_PATTERN.fullmatch(draft_slug) is not None
                and repository == expected_repository
                and release_tag == expected_release_tag
                and expected_release_id is not None
                and release_id == expected_release_id
            ):
                return (
                    "release draft preserved: "
                    f"https://github.com/{repository}/releases/tag/{draft_slug} "
                    f"(id {release_id})"
                )
            continue
        uncertain_match = _UNCERTAIN_PUBLICATION_NOTE_PATTERN.fullmatch(note)
        if uncertain_match is not None:
            repository, release_tag, release_id_text = uncertain_match.groups()
            release_id = int(release_id_text)
            if (
                trusted_phase == "uncertain"
                and repository == expected_repository
                and release_tag == expected_release_tag
                and expected_release_id is not None
                and release_id == expected_release_id
            ):
                return (
                    "publication state uncertain: inspect "
                    f"https://github.com/{repository}/releases/tag/{release_tag} "
                    f"(id {release_id})"
                )
    return None


def _release_asset_expectations(release):
    expectations = {}
    for path in release.files:
        content_type = _RELEASE_CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            raise ValueError("Release contains an unsupported asset type")
        expectations[path.name] = {
            "content_type": content_type,
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(),
            "path": path,
            "size": path.stat().st_size,
        }
    if len(expectations) != 3:
        raise ValueError("Release must contain exactly three public assets")
    return expectations


def _draft_slug_from_release_payload(payload, repository):
    if not isinstance(payload, dict):
        raise ValueError("GitHub draft release response must be an object")
    html_url = payload.get("html_url")
    prefix = f"https://github.com/{repository}/releases/tag/"
    if not isinstance(html_url, str) or len(html_url) > len(prefix) + 29:
        raise ValueError("GitHub draft release locator is invalid")
    if not html_url.startswith(prefix):
        raise ValueError("GitHub draft release locator is not GitHub-owned")
    slug = html_url[len(prefix) :]
    if _DRAFT_SLUG_PATTERN.fullmatch(slug) is None:
        raise ValueError("GitHub draft release locator has an invalid opaque slug")
    return slug


def _validate_release_payload(
    payload,
    *,
    repository,
    release,
    notes,
    draft,
    expected_release_id=None,
    expected_asset_ids=None,
    expected_draft_slug=None,
    asset_requester=None,
    token=None,
):
    if not isinstance(payload, dict):
        raise ValueError("GitHub Releases API response must be an object")
    release_id = _positive_integer(payload.get("id"), "Release id")
    if expected_release_id is not None and release_id != expected_release_id:
        raise ValueError("GitHub release id changed during publication")
    api_root = f"https://api.github.com/repos/{repository}"
    html_root = f"https://github.com/{repository}"
    upload_root = f"https://uploads.github.com/repos/{repository}"
    if draft:
        draft_slug = _draft_slug_from_release_payload(payload, repository)
        if expected_draft_slug is not None and draft_slug != expected_draft_slug:
            raise ValueError("GitHub draft release locator changed during publication")
        expected_html_url = f"{html_root}/releases/tag/{draft_slug}"
        asset_release_path = draft_slug
    else:
        expected_html_url = f"{html_root}/releases/tag/{release.release_tag}"
        asset_release_path = release.release_tag
    if (
        payload.get("url") != f"{api_root}/releases/{release_id}"
        or payload.get("upload_url")
        != f"{upload_root}/releases/{release_id}/assets{{?name,label}}"
        or payload.get("html_url") != expected_html_url
        or payload.get("tag_name") != release.release_tag
        or payload.get("target_commitish") != release.windows_commit
        or payload.get("name") != release.release_tag
        or payload.get("body") != notes
        or payload.get("draft") is not draft
        or payload.get("prerelease") is not False
    ):
        raise ValueError("GitHub release response does not match the exact requested release")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ValueError("GitHub release assets must be a list")
    if draft and expected_asset_ids == {} and assets:
        raise ValueError("A newly created draft release must start with no assets")
    if expected_asset_ids is not None and len(assets) != len(expected_asset_ids):
        raise ValueError("GitHub release does not contain the exact asset count")
    if not assets:
        return release_id, {}

    expectations = _release_asset_expectations(release)
    if len(assets) != len(expectations) and not (draft and asset_requester is None):
        raise ValueError("GitHub release does not contain exactly three assets")
    seen_names = set()
    seen_ids = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("GitHub release asset must be an object")
        name = asset.get("name")
        expected = expectations.get(name)
        asset_id = _positive_integer(asset.get("id"), "Release asset id")
        if name in seen_names or expected is None:
            raise ValueError("GitHub release contains a duplicate or unexpected asset")
        seen_names.add(name)
        if (
            asset.get("url") != f"{api_root}/releases/assets/{asset_id}"
            or asset.get("browser_download_url")
            != f"{html_root}/releases/download/{asset_release_path}/{name}"
            or asset.get("label") is not None
            or asset.get("state") != "uploaded"
            or asset.get("content_type") != expected["content_type"]
            or asset.get("size") != expected["size"]
        ):
            raise ValueError("GitHub release asset metadata is not exact")
        if asset_requester is not None:
            status, body = asset_requester("GET", asset["url"], token, None, None)
            if status != 200 or not isinstance(body, bytes):
                raise ValueError("GitHub release asset download failed")
            if hashlib.sha256(body).hexdigest() != expected["digest"]:
                raise ValueError("GitHub release asset hash does not match the verified artifact")
        seen_ids[name] = asset_id
    if expected_asset_ids is not None and seen_ids != expected_asset_ids:
        raise ValueError("GitHub release asset ids changed during publication")
    return release_id, seen_ids


def _github_asset_request(method, url, token, payload, content_type=None):
    if method not in {"GET", "POST"}:
        raise ValueError("Unsupported release asset request method")
    headers = {
        "Accept": "application/octet-stream" if method == "GET" else "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "fk-chromium-release-publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = Request(url, data=payload, method=method, headers=headers)
    try:
        with _NO_REDIRECT_OPEN(request, timeout=60) as response:
            body = response.read()
            if method == "POST":
                body = json.loads(body)
            return response.status, body
    except HTTPError as error:
        if method == "GET" and error.code in {301, 302, 303, 307, 308}:
            redirected_url = _safe_asset_redirect_url(error.headers.get("Location"))
            redirected_request = Request(
                redirected_url,
                method="GET",
                headers={"Accept": "application/octet-stream", "User-Agent": headers["User-Agent"]},
            )
            try:
                with _NO_REDIRECT_OPEN(redirected_request, timeout=60) as response:
                    return response.status, response.read()
            except HTTPError as redirected_error:
                return redirected_error.code, b""
            except OSError as redirected_error:
                raise ValueError("GitHub release asset redirect failed") from redirected_error
        return error.code, b""
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("GitHub release asset request failed") from error


def _safe_asset_redirect_url(value):
    """Allow one credential-free redirect to GitHub's release asset CDN only."""
    if not isinstance(value, str):
        raise ValueError("GitHub release asset redirect is missing")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.hostname not in {
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
            "github-releases.githubusercontent.com",
        }
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise ValueError("GitHub release asset redirect target is not trusted")
    return value


def publish_release_via_api(
    *,
    repository,
    release,
    notes,
    token,
    requester=_github_request,
    asset_requester=_github_asset_request,
):
    """Create one draft, upload exact assets, and publish it; never adopt or clean up."""
    if not isinstance(repository, str) or _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Expected an exact owner/repository name")
    if not isinstance(token, str) or not token:
        raise ValueError("GITHUB_TOKEN is required for release publication")
    if not isinstance(notes, str) or "\r" in notes or not notes:
        raise ValueError("Release notes must be non-empty normalized text")
    if _PUBLIC_TAG_PATTERN.fullmatch(release.release_tag) is None:
        raise ValueError("Release has a non-canonical public tag")
    _release_asset_expectations(release)
    api_root = f"https://api.github.com/repos/{repository}"
    destination = validate_release_destination(
        repository=repository,
        public_tag=release.release_tag,
        windows_commit=release.windows_commit,
        token=token,
        requester=requester,
    )
    if destination != "available":
        raise ValueError(
            "Public release tag already exists; manual inspection is required"
        )
    tag_url = f"{api_root}/releases/tags/{quote(release.release_tag, safe='')}"
    status, existing = requester("GET", tag_url, token, None)
    if status != 404:
        if status == 200:
            raise ValueError(
                "Public release already exists; manual inspection is required"
            )
        raise ValueError("Release lookup did not confirm an absent destination")

    ref_payload = {"ref": f"refs/tags/{release.release_tag}", "sha": release.windows_commit}
    status, created_ref = requester("POST", f"{api_root}/git/refs", token, ref_payload)
    if status != 201:
        raise ValueError(f"GitHub release tag creation failed with HTTP {status}")
    _validate_created_release_ref(
        created_ref,
        repository=repository,
        public_tag=release.release_tag,
        windows_commit=release.windows_commit,
    )
    if validate_release_destination(
        repository=repository,
        public_tag=release.release_tag,
        windows_commit=release.windows_commit,
        token=token,
        requester=requester,
    ) != "existing":
        raise ValueError("Created release tag could not be re-authorized")

    create_payload = {
        "body": notes,
        "draft": True,
        "name": release.release_tag,
        "prerelease": False,
        "tag_name": release.release_tag,
        "target_commitish": release.windows_commit,
    }
    status, created = requester("POST", f"{api_root}/releases", token, create_payload)
    if status != 201:
        raise ValueError(f"GitHub draft release creation failed with HTTP {status}")
    release_id, _ = _validate_release_payload(
        created,
        repository=repository,
        release=release,
        notes=notes,
        draft=True,
        expected_asset_ids={},
    )
    draft_slug = _draft_slug_from_release_payload(created, repository)
    publication_started = False
    try:
        uploaded_ids = {}
        upload_root = f"https://uploads.github.com/repos/{repository}/releases/{release_id}/assets"
        for path in release.files:
            content_type = _RELEASE_CONTENT_TYPES[path.suffix.lower()]
            upload_url = f"{upload_root}?{urlencode({'name': path.name})}"
            status, asset = asset_requester(
                "POST", upload_url, token, path.read_bytes(), content_type
            )
            if status != 201 or not isinstance(asset, dict):
                raise ValueError(f"GitHub release asset upload failed with HTTP {status}")
            shell = {**created, "assets": [asset]}
            _, one_id = _validate_release_payload(
                shell,
                repository=repository,
                release=release,
                notes=notes,
                draft=True,
                expected_release_id=release_id,
                expected_asset_ids={path.name: asset.get("id")},
                expected_draft_slug=draft_slug,
            )
            uploaded_ids.update(one_id)

        release_url = f"{api_root}/releases/{release_id}"
        status, draft_payload = requester("GET", release_url, token, None)
        if status != 200:
            raise ValueError(f"GitHub draft release verification failed with HTTP {status}")
        _validate_release_payload(
            draft_payload,
            repository=repository,
            release=release,
            notes=notes,
            draft=True,
            expected_release_id=release_id,
            expected_asset_ids=uploaded_ids,
            expected_draft_slug=draft_slug,
            asset_requester=asset_requester,
            token=token,
        )
        if validate_release_destination(
            repository=repository,
            public_tag=release.release_tag,
            windows_commit=release.windows_commit,
            token=token,
            requester=requester,
        ) != "existing":
            raise ValueError("Release tag changed before publication")
        publication_started = True
        status, published = requester("PATCH", release_url, token, {"draft": False})
        if status != 200:
            raise ValueError(f"GitHub release publication failed with HTTP {status}")
        _validate_release_payload(
            published,
            repository=repository,
            release=release,
            notes=notes,
            draft=False,
            expected_release_id=release_id,
            expected_asset_ids=uploaded_ids,
        )
        status, final = requester("GET", tag_url, token, None)
        if status != 200:
            raise ValueError(f"GitHub public release verification failed with HTTP {status}")
        _validate_release_payload(
            final,
            repository=repository,
            release=release,
            notes=notes,
            draft=False,
            expected_release_id=release_id,
            expected_asset_ids=uploaded_ids,
            asset_requester=asset_requester,
            token=token,
        )
        if validate_release_destination(
            repository=repository,
            public_tag=release.release_tag,
            windows_commit=release.windows_commit,
            token=token,
            requester=requester,
        ) != "existing":
            raise ValueError("Release tag changed after publication")
        return "published"
    except Exception as error:
        if publication_started:
            note = (
                f"release publication uncertain repository={repository} "
                f"tag={release.release_tag} id={release_id}"
            )
            failure_phase = "uncertain"
        else:
            note = (
                f"created draft release repository={repository} "
                f"tag={release.release_tag} id={release_id} draft_slug={draft_slug}"
            )
            failure_phase = "draft"
        try:
            error.add_note(note)
            error._trusted_created_draft_release_id = release_id
            error._trusted_created_draft_slug = draft_slug
            error._trusted_release_failure_phase = failure_phase
        except Exception:
            pass
        raise


def _normalized_job_name(name):
    if not isinstance(name, str):
        raise ValueError("Workflow job name must be a string")
    return name.removeprefix("build / ")


def classify_failure_jobs(payload, run_id, run_attempt):
    """Return a bounded trusted failure stage, detecting real twelve-stage exhaustion."""
    _positive_integer(run_id, "Workflow run id")
    _positive_integer(run_attempt, "Workflow run attempt")
    if not isinstance(payload, dict):
        raise ValueError("Workflow jobs API response must be an object")
    jobs = payload.get("jobs")
    total_count = payload.get("total_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 1
        or total_count > 100
        or not isinstance(jobs, list)
        or len(jobs) != total_count
    ):
        raise ValueError("Workflow jobs must be one complete bounded API page")

    stages = {}
    failures = []
    allowed = {"resolve-branding", "complete", *(f"build-{number}" for number in range(1, 13))}
    for job in jobs:
        if (
            not isinstance(job, dict)
            or job.get("run_id") != run_id
            or job.get("run_attempt") != run_attempt
        ):
            raise ValueError("Workflow job does not belong to the authorized run")
        name = _normalized_job_name(job.get("name"))
        conclusion = job.get("conclusion")
        if job.get("status") != "completed" or conclusion not in {
            "success",
            "failure",
            "skipped",
            "cancelled",
        }:
            raise ValueError("Workflow job has an invalid terminal state")
        if name in stages:
            raise ValueError("Workflow jobs response contains a duplicate stage")
        if name in allowed:
            stages[name] = conclusion
            if conclusion == "failure":
                failures.append(name)
        elif conclusion == "failure":
            raise ValueError("Unknown failed workflow job cannot be reported safely")

    if failures == ["complete"] or set(failures) == {"complete"}:
        if all(stages.get(f"build-{number}") == "success" for number in range(1, 13)):
            return "12-stage exhaustion"
        raise ValueError("Complete job failure is not a genuine twelve-stage exhaustion")
    failed_builds = sorted(
        (name for name in failures if re.fullmatch(r"build-(?:[1-9]|1[0-2])", name)),
        key=lambda name: int(name.split("-", 1)[1]),
    )
    if failed_builds:
        return failed_builds[0]
    if failures == ["resolve-branding"]:
        return "resolve-branding"
    raise ValueError("Workflow run has no safely reportable failure stage")


def classify_publication_failure_jobs(payload, run_id, run_attempt):
    """Return the exact failed publisher phase from one bounded attempt job page."""
    _positive_integer(run_id, "Publisher run id")
    _positive_integer(run_attempt, "Publisher run attempt")
    if not isinstance(payload, dict):
        raise ValueError("Publisher jobs API response must be an object")
    jobs = payload.get("jobs")
    total_count = payload.get("total_count")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count < 1
        or total_count > 100
        or not isinstance(jobs, list)
        or len(jobs) != total_count
    ):
        raise ValueError("Publisher jobs must be one complete bounded API page")
    stages = {}
    for job in jobs:
        if (
            not isinstance(job, dict)
            or job.get("run_id") != run_id
            or job.get("run_attempt") != run_attempt
        ):
            raise ValueError("Publisher job does not belong to the authorized attempt")
        name = job.get("name")
        conclusion = job.get("conclusion")
        if job.get("status") != "completed" or conclusion not in {
            "success", "failure", "skipped", "cancelled"
        }:
            raise ValueError("Publisher job has an invalid terminal state")
        if name in stages:
            raise ValueError("Publisher jobs response contains a duplicate stage")
        if name in {"validate", "publish"}:
            stages[name] = conclusion
        elif conclusion == "failure":
            raise ValueError("Unknown failed publisher job cannot be reported safely")
    failures = [name for name in ("validate", "publish") if stages.get(name) == "failure"]
    if len(failures) != 1:
        raise ValueError("Publisher run has no unique safely reportable failure phase")
    return failures[0]


def format_failure_report(release, run: ValidatedRun, stage):
    """Build issue text exclusively from already validated bounded values."""
    if not isinstance(run, ValidatedRun):
        raise TypeError("run must be a ValidatedRun")
    if stage != "12-stage exhaustion" and re.fullmatch(
        r"resolve-branding|build-(?:[1-9]|1[0-2])", stage or ""
    ) is None:
        raise ValueError("Failure stage is not a supported workflow stage")
    if release.windows_commit != run.head_sha:
        raise ValueError("Failure metadata Windows commit does not match the workflow run")
    marker = f"fk-build-failed:{release.upstream_version}"
    run_marker = f"fk-build-run:{run.run_id}:attempt:{run.run_attempt}"
    title = f"FK Chromium build failed: {release.upstream_version}"
    body = "\n".join(
        (
            f"<!-- {marker} -->",
            f"<!-- {run_marker} -->",
            f"Failure stage: `{stage}`",
            f"Workflow run: {run.run_attempt_url}",
            f"Upstream tag: `{release.upstream_tag}`",
            f"Upstream commit: `{release.upstream_commit}`",
            f"Upstream Windows commit: `{release.upstream_windows_commit}`",
            f"FK branding commit: `{release.branding_commit}`",
            f"Windows build commit: `{release.windows_commit}`",
            "Manual retry: run `check-upstream` with this exact upstream tag and "
            "`force_rebuild=true` after fixing the failure.",
            "",
        )
    )
    return FailureReport(marker=marker, run_marker=run_marker, title=title, body=body)


def format_publication_failure_report(
    attempt: PublicationAttempt, run: ValidatedRun, stage
):
    """Build sanitized attempt-specific publication failure Issue text."""
    if not isinstance(attempt, PublicationAttempt) or not isinstance(run, ValidatedRun):
        raise TypeError("Publication failure inputs must be validated identities")
    if stage not in {"validate", "publish"}:
        raise ValueError("Publication failure phase is not supported")
    if (
        attempt.publisher_run_id != run.run_id
        or attempt.publisher_run_attempt != run.run_attempt
        or run.artifact_name != "fk-chromium-publication-attempt"
    ):
        raise ValueError("Publication attempt does not match the authorized publisher run")
    marker = (
        f"fk-publish-failed:{attempt.release_tag}:run:{run.run_id}:"
        f"attempt:{run.run_attempt}"
    )
    run_marker = f"fk-publish-source-artifact:{attempt.source_artifact_id}"
    source_url = (
        f"https://github.com/{run.repository}/actions/runs/{attempt.source_run_id}/"
        f"attempts/{attempt.source_run_attempt}"
    )
    title = f"FK Chromium publication failed: {attempt.release_tag}"
    body = "\n".join(
        (
            f"<!-- {marker} -->",
            f"<!-- {run_marker} -->",
            f"Failure stage: `publication-{stage}`",
            f"Publisher workflow run: {run.run_attempt_url}",
            f"Source build run: {source_url}",
            f"Source artifact: `fk-chromium-windows-x64` (id {attempt.source_artifact_id})",
            f"Upstream tag: `{attempt.upstream_tag}`",
            f"Public release tag: `{attempt.release_tag}`",
            f"Upstream commit: `{attempt.upstream_commit}`",
            f"Upstream Windows commit: `{attempt.upstream_windows_commit}`",
            f"FK branding commit: `{attempt.branding_commit}`",
            f"Windows build commit: `{attempt.windows_commit}`",
            "Manual inspection is required before retrying this exact reservation; "
            "the automation does not adopt, delete, or overwrite release residue.",
            "",
        )
    )
    return FailureReport(marker=marker, run_marker=run_marker, title=title, body=body)


def format_release_notes(release, run: ValidatedRun):
    """Return deterministic public provenance and the required unsigned-build warning."""
    bind_release_to_run(release, run)
    return "\n".join(
        (
            "Product: FK Chromium (火焰库拉浏览器)",
            f"Upstream tag: `{release.upstream_tag}`",
            f"Upstream commit: `{release.upstream_commit}`",
            f"FK branding commit: `{release.branding_commit}`",
            f"Windows build commit: `{release.windows_commit}`",
            f"Workflow run: {run.run_url}",
            "",
            "此安装程序尚未进行 Windows 代码签名，Microsoft Defender SmartScreen 可能显示未知发布者警告。",
        )
    )


def find_failure_issue(issues, marker):
    """Find one labeled marker in an all-states issue listing, rejecting ambiguity."""
    if not isinstance(issues, list) or not isinstance(marker, str):
        raise ValueError("Failure issue search inputs are invalid")
    html_marker = f"<!-- {marker} -->"
    matches = []
    for issue in issues:
        if not isinstance(issue, dict) or isinstance(issue.get("pull_request"), dict):
            continue
        labels = issue.get("labels")
        body = issue.get("body")
        number = issue.get("number")
        if not isinstance(labels, list) or not isinstance(body, str):
            continue
        has_label = any(
            isinstance(label, dict) and label.get("name") == "fk-build-failure"
            for label in labels
        )
        if has_label and html_marker in body:
            matches.append(_positive_integer(number, "Failure issue number"))
    if len(matches) > 1:
        raise ValueError("Multiple failure issues contain the same version marker")
    return matches[0] if matches else None


def _paged_issue_values(requester, endpoint, token):
    values = []
    for page in range(1, 11):
        separator = "&" if "?" in endpoint else "?"
        url = f"{endpoint}{separator}per_page=100&page={page}"
        status, payload = requester("GET", url, token, None)
        if status != 200 or not isinstance(payload, list):
            raise ValueError(f"GitHub Issues API listing failed with HTTP {status}")
        values.extend(payload)
        if len(payload) < 100:
            return values
    raise ValueError("GitHub Issues API listing exceeded the 10-page safety bound")


def report_failure_issue_via_api(*, repository, report, token, requester=_github_request):
    """Create one version issue or one run comment across open and closed issues."""
    if not isinstance(repository, str) or _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("Expected an exact owner/repository name")
    if not isinstance(report, FailureReport):
        raise TypeError("report must be a FailureReport")
    if not isinstance(token, str) or not token:
        raise ValueError("GITHUB_TOKEN is required for failure reporting")
    owner, name = repository.split("/", 1)
    root = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    label_url = f"{root}/labels/fk-build-failure"
    status, _ = requester("GET", label_url, token, None)
    if status == 404:
        label_payload = {
            "color": "b60205",
            "description": "Automated FK Chromium build failure",
            "name": "fk-build-failure",
        }
        status, _ = requester("POST", f"{root}/labels", token, label_payload)
        if status not in {201, 422}:
            raise ValueError(f"GitHub label creation failed with HTTP {status}")
    elif status != 200:
        raise ValueError(f"GitHub label lookup failed with HTTP {status}")

    issues = _paged_issue_values(
        requester, f"{root}/issues?state=all&labels=fk-build-failure", token
    )
    issue_number = find_failure_issue(issues, report.marker)
    if issue_number is None:
        status, _ = requester(
            "POST",
            f"{root}/issues",
            token,
            {"body": report.body, "labels": ["fk-build-failure"], "title": report.title},
        )
        if status != 201:
            raise ValueError(f"GitHub failure issue creation failed with HTTP {status}")
        return "created"

    comments = _paged_issue_values(requester, f"{root}/issues/{issue_number}/comments", token)
    html_run_marker = f"<!-- {report.run_marker} -->"
    if any(isinstance(comment, dict) and html_run_marker in (comment.get("body") or "") for comment in comments):
        return "unchanged"
    status, _ = requester(
        "POST", f"{root}/issues/{issue_number}/comments", token, {"body": report.body}
    )
    if status != 201:
        raise ValueError(f"GitHub failure comment creation failed with HTTP {status}")
    return "commented"


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read JSON from {path}: {error}") from error


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_outputs(path, values):
    if path is None:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        for name, value in values.items():
            text = str(value)
            if "\n" in text or "\r" in text:
                raise ValueError(f"GitHub output {name} is not a single line")
            print(f"{name}={text}", file=output)


def _context_from_dict(value):
    if not isinstance(value, dict) or set(value) != {
        "artifact_id",
        "artifact_name",
        "default_branch",
        "event",
        "head_sha",
        "repository",
        "run_id",
        "run_attempt",
    }:
        raise ValueError("Validated run context has the wrong fields")
    context = ValidatedRun(**value)
    _positive_integer(context.run_id, "Workflow run id")
    _positive_integer(context.run_attempt, "Workflow run attempt")
    _positive_integer(context.artifact_id, "Artifact id")
    if _SHA_PATTERN.fullmatch(context.head_sha) is None:
        raise ValueError("Validated run context has an invalid head SHA")
    return context


def _parse_boolean(value):
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("Boolean workflow values must be exactly true or false")


def _parse_fk_revision_argument(value):
    try:
        return parse_fk_revision(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _state_metadata(identity):
    return {
        "fk_revision": identity.fk_revision,
        "publish": identity.publish,
        "release_tag": identity.release_tag,
        "upstream_tag": identity.upstream_tag,
        "upstream_version": identity.upstream_version,
    }


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-build-metadata")
    create.add_argument("--windows-repository", type=Path, required=True)
    create.add_argument("--upstream-windows-repository", type=Path, required=True)
    create.add_argument("--branding-repository", type=Path, required=True)
    create.add_argument("--upstream-tag", required=True)
    create.add_argument("--fk-revision", type=_parse_fk_revision_argument, required=True)
    create.add_argument("--force-rebuild", required=True)
    create.add_argument("--publish", required=True)
    create.add_argument("--output", type=Path, required=True)

    failure_identity = commands.add_parser("create-failure-identity")
    failure_identity.add_argument("--upstream-tag", required=True)
    failure_identity.add_argument(
        "--fk-revision", type=_parse_fk_revision_argument, required=True
    )
    failure_identity.add_argument("--publish", required=True)
    failure_identity.add_argument("--windows-commit", required=True)
    failure_identity.add_argument("--upstream-windows-commit", default="unresolved")
    failure_identity.add_argument("--upstream-commit", default="unresolved")
    failure_identity.add_argument("--branding-commit", default="unresolved")
    failure_identity.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate-run")
    validate.add_argument("--run-json", type=Path, required=True)
    validate.add_argument("--workflow-json", type=Path, required=True)
    validate.add_argument("--artifacts-json", type=Path, required=True)
    validate.add_argument("--repository-json", type=Path, required=True)
    validate.add_argument("--repository", required=True)
    validate.add_argument("--default-branch", required=True)
    validate.add_argument("--expected-conclusion", choices=("success", "failure"), required=True)
    validate.add_argument("--artifact-name", action="append", required=True)
    validate.add_argument("--expected-run-attempt", type=int)
    validate.add_argument("--expected-artifact-id", type=int)
    validate.add_argument(
        "--expected-workflow-name",
        choices=("build-x64", "publish-release"),
        default="build-x64",
    )
    validate.add_argument(
        "--expected-workflow-path",
        default=_BUILD_WORKFLOW_PATH,
    )
    validate.add_argument("--allowed-event", action="append")
    validate.add_argument("--context-json", type=Path, required=True)
    validate.add_argument("--github-output", type=Path)

    bind = commands.add_parser("bind-release")
    bind.add_argument("--artifact-directory", type=Path, required=True)
    bind.add_argument("--context-json", type=Path, required=True)
    bind.add_argument("--notes", type=Path, required=True)
    bind.add_argument("--github-output", type=Path)

    publication = commands.add_parser("classify-publication")
    publication.add_argument("--artifact-directory", type=Path, required=True)
    publication.add_argument("--context-json", type=Path, required=True)
    publication.add_argument("--github-output", type=Path, required=True)

    publication_attempt = commands.add_parser("create-publication-attempt")
    publication_attempt.add_argument("--artifact-directory", type=Path, required=True)
    publication_attempt.add_argument("--context-json", type=Path, required=True)
    publication_attempt.add_argument("--publisher-run-id", type=int, required=True)
    publication_attempt.add_argument("--publisher-run-attempt", type=int, required=True)
    publication_attempt.add_argument("--output", type=Path, required=True)

    classify = commands.add_parser("classify-failure")
    classify.add_argument("--metadata", type=Path, required=True)
    classify.add_argument("--context-json", type=Path, required=True)
    classify.add_argument("--jobs-json", type=Path, required=True)
    classify.add_argument("--report-json", type=Path, required=True)
    classify.add_argument("--github-output", type=Path)

    classify_publication_failure = commands.add_parser(
        "classify-publication-failure"
    )
    classify_publication_failure.add_argument("--attempt", type=Path, required=True)
    classify_publication_failure.add_argument("--context-json", type=Path, required=True)
    classify_publication_failure.add_argument("--jobs-json", type=Path, required=True)
    classify_publication_failure.add_argument("--report-json", type=Path, required=True)
    classify_publication_failure.add_argument("--github-output", type=Path)

    update = commands.add_parser("update-state")
    update.add_argument("--repository", required=True)
    update.add_argument("--branch", required=True)
    update.add_argument("--metadata", type=Path, required=True)

    validate_state = commands.add_parser("validate-state")
    validate_state.add_argument("--repository", required=True)
    validate_state.add_argument("--branch", required=True)
    validate_state.add_argument("--metadata", type=Path, required=True)

    publish = commands.add_parser("publish-release")
    publish.add_argument("--repository", required=True)
    publish.add_argument("--expected-release-tag", required=True)
    publish.add_argument("--artifact-directory", type=Path, required=True)
    publish.add_argument("--context-json", type=Path, required=True)
    publish.add_argument("--notes", type=Path, required=True)

    destination = commands.add_parser("validate-destination")
    destination.add_argument("--repository", required=True)
    destination.add_argument("--public-tag", required=True)
    destination.add_argument("--windows-commit", required=True)

    report = commands.add_parser("report-failure")
    report.add_argument("--repository", required=True)
    report.add_argument("--report-json", type=Path, required=True)

    args = None
    failed = False
    try:
        args = parser.parse_args(arguments)
        if args.command == "create-build-metadata":
            metadata = create_build_metadata(
                windows_repository=args.windows_repository,
                upstream_windows_repository=args.upstream_windows_repository,
                branding_repository=args.branding_repository,
                upstream_tag=args.upstream_tag,
                fk_revision=args.fk_revision,
                force_rebuild=_parse_boolean(args.force_rebuild),
                publish=_parse_boolean(args.publish),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write_json(args.output, metadata)
            return 0
        if args.command == "create-failure-identity":
            identity = create_failure_identity(
                upstream_tag=args.upstream_tag,
                fk_revision=args.fk_revision,
                publish=_parse_boolean(args.publish),
                windows_commit=args.windows_commit,
                upstream_windows_commit=args.upstream_windows_commit,
                upstream_commit=args.upstream_commit,
                branding_commit=args.branding_commit,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write_json(args.output, identity)
            return 0
        if args.command == "validate-run":
            context = validate_workflow_run(
                _load_json(args.run_json),
                _load_json(args.workflow_json),
                _load_json(args.artifacts_json),
                repository_info=_load_json(args.repository_json),
                repository=args.repository,
                default_branch=args.default_branch,
                expected_conclusion=args.expected_conclusion,
                artifact_name=args.artifact_name,
                expected_run_attempt=args.expected_run_attempt,
                expected_artifact_id=args.expected_artifact_id,
                expected_workflow_name=args.expected_workflow_name,
                expected_workflow_path=args.expected_workflow_path,
                allowed_events=(
                    tuple(args.allowed_event)
                    if args.allowed_event
                    else ("workflow_dispatch", "workflow_call")
                ),
            )
            _write_json(args.context_json, asdict(context))
            _write_outputs(
                args.github_output,
                {
                    "artifact_id": context.artifact_id,
                    "artifact_name": context.artifact_name,
                    "run_attempt": context.run_attempt,
                    "run_id": context.run_id,
                },
            )
            return 0
        if args.command == "bind-release":
            context = _context_from_dict(_load_json(args.context_json))
            release = verify_release_artifact(args.artifact_directory, require_publish=True)
            bind_release_to_run(release, context)
            args.notes.write_text(format_release_notes(release, context), encoding="utf-8")
            _write_outputs(
                args.github_output,
                {
                    "checksums": release.files[2],
                    "installer": release.files[0],
                    "portable": release.files[1],
                    "release_tag": release.release_tag,
                    "windows_commit": release.windows_commit,
                },
            )
            return 0
        if args.command == "classify-publication":
            context = _context_from_dict(_load_json(args.context_json))
            release = verify_release_artifact(args.artifact_directory)
            publish = classify_publication(release, context)
            _write_outputs(
                args.github_output,
                {"publish": "true" if publish else "false"},
            )
            return 0
        if args.command == "create-publication-attempt":
            context = _context_from_dict(_load_json(args.context_json))
            release = verify_release_artifact(
                args.artifact_directory, require_publish=True
            )
            payload = create_publication_attempt(
                release,
                context,
                publisher_run_id=args.publisher_run_id,
                publisher_run_attempt=args.publisher_run_attempt,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write_json(args.output, payload)
            return 0
        if args.command == "classify-failure":
            context = _context_from_dict(_load_json(args.context_json))
            identity = read_failure_identity(args.metadata)
            stage = classify_failure_jobs(
                _load_json(args.jobs_json), context.run_id, context.run_attempt
            )
            failure_report = format_failure_report(identity, context, stage)
            _write_json(args.report_json, asdict(failure_report))
            _write_outputs(
                args.github_output,
                {"stage": stage, "version": identity.upstream_version},
            )
            return 0
        if args.command == "classify-publication-failure":
            context = _context_from_dict(_load_json(args.context_json))
            attempt = read_publication_attempt(args.attempt)
            stage = classify_publication_failure_jobs(
                _load_json(args.jobs_json), context.run_id, context.run_attempt
            )
            failure_report = format_publication_failure_report(
                attempt, context, stage
            )
            _write_json(args.report_json, asdict(failure_report))
            _write_outputs(
                args.github_output,
                {"stage": stage, "version": attempt.upstream_version},
            )
            return 0
        if args.command == "update-state":
            identity = read_build_identity(args.metadata)
            result = update_release_state_via_contents_api(
                repository=args.repository,
                branch=args.branch,
                metadata=_state_metadata(identity),
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
            print(result)
            return 0
        if args.command == "validate-state":
            identity = read_build_identity(args.metadata)
            result = authorize_release_state_via_contents_api(
                repository=args.repository,
                branch=args.branch,
                metadata=_state_metadata(identity),
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
            print(result)
            return 0
        if args.command == "publish-release":
            if _PUBLIC_TAG_PATTERN.fullmatch(args.expected_release_tag) is None:
                raise ValueError("Expected a canonical invocation release tag")
            context = _context_from_dict(_load_json(args.context_json))
            release = verify_release_artifact(args.artifact_directory, require_publish=True)
            if release.release_tag != args.expected_release_tag:
                raise ValueError("Release artifact tag does not match the authorized invocation")
            bind_release_to_run(release, context)
            notes = args.notes.read_text(encoding="utf-8")
            if notes != format_release_notes(release, context):
                raise ValueError("Release notes do not match the authorized artifact")
            result = publish_release_via_api(
                repository=args.repository,
                release=release,
                notes=notes,
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
            print(result)
            return 0
        if args.command == "validate-destination":
            result = validate_release_destination(
                repository=args.repository,
                public_tag=args.public_tag,
                windows_commit=args.windows_commit,
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
            print(result)
            return 0
        if args.command == "report-failure":
            value = _load_json(args.report_json)
            if not isinstance(value, dict) or set(value) != {"body", "marker", "run_marker", "title"}:
                raise ValueError("Failure report JSON has the wrong fields")
            result = report_failure_issue_via_api(
                repository=args.repository,
                report=FailureReport(**value),
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
            print(result)
            return 0
    except Exception as error:
        expected_repository = getattr(args, "repository", None)
        expected_release_tag = getattr(args, "expected_release_tag", None)
        if expected_release_tag is None:
            expected_release_tag = getattr(args, "public_tag", None)
        locator = _sanitized_created_draft_note(
            error,
            expected_repository=expected_repository,
            expected_release_tag=expected_release_tag,
            expected_release_id=getattr(error, "_trusted_created_draft_release_id", None),
        )
        diagnostic = "release_workflow: operation failed\n"
        if locator is not None:
            diagnostic += locator + "\n"
        try:
            sys.stderr.write(diagnostic)
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        failed = True
    if failed:
        raise SystemExit(1) from None
    raise AssertionError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
