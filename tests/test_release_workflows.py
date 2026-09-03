import hashlib
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import traceback

import pytest

from tools.release_metadata import verify_release_artifact
from tools.release_workflow import (
    _safe_asset_redirect_url,
    _sanitized_created_draft_note,
    _validate_release_payload,
    authorize_release_state,
    bind_release_to_run,
    classify_publication,
    classify_publication_failure_jobs,
    classify_failure_jobs,
    create_build_metadata,
    create_publication_attempt,
    find_failure_issue,
    format_failure_report,
    format_publication_failure_report,
    format_release_notes,
    main,
    publish_release_via_api,
    read_publication_attempt,
    record_success_json,
    report_failure_issue_via_api,
    validate_release_destination,
    update_release_state_via_contents_api,
    validate_workflow_run,
)


UPSTREAM_TAG = "151.0.7922.173-2.1"
UPSTREAM_VERSION = "151.0.7922.173"
RELEASE_TAG = "151.0.7922.173-fk.2"
WINDOWS_COMMIT = "1" * 40
UPSTREAM_WINDOWS_COMMIT = "2" * 40
UPSTREAM_COMMIT = "3" * 40
BRANDING_COMMIT = "4" * 40
RUN_ID = 987654321
RUN_ATTEMPT = 3
REPOSITORY = "firekula/fk-chromium-windows"
DEFAULT_BRANCH = "main"
ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _release_metadata(**overrides):
    metadata = {
        "branding_commit": BRANDING_COMMIT,
        "fk_revision": 2,
        "force_rebuild": False,
        "manifest_sha256": "5" * 64,
        "publish": True,
        "release_tag": RELEASE_TAG,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_tag": UPSTREAM_TAG,
        "upstream_version": UPSTREAM_VERSION,
        "upstream_windows_commit": UPSTREAM_WINDOWS_COMMIT,
        "windows_build_hook_patch_sha256": "6" * 64,
        "windows_commit": WINDOWS_COMMIT,
    }
    metadata.update(overrides)
    return metadata


def _write_release_artifact(directory: Path, **metadata_overrides):
    directory.mkdir()
    installer = directory / f"FK-Chromium-{UPSTREAM_VERSION}-Windows-x64-Installer.exe"
    portable = directory / f"FK-Chromium-{UPSTREAM_VERSION}-Windows-x64-Portable.zip"
    installer.write_bytes(b"installer")
    portable.write_bytes(b"portable")
    checksums = directory / "SHA256SUMS.txt"
    checksums.write_text(
        "".join(
            (
                f"{hashlib.sha256(installer.read_bytes()).hexdigest()}  {installer.name}\n",
                f"{hashlib.sha256(portable.read_bytes()).hexdigest()}  {portable.name}\n",
            )
        ),
        encoding="utf-8",
    )
    (directory / "fk-build-metadata.json").write_text(
        json.dumps(_release_metadata(**metadata_overrides)), encoding="utf-8"
    )
    return installer, portable, checksums


def _workflow_run(**overrides):
    run = {
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": DEFAULT_BRANCH,
        "head_repository": {"full_name": REPOSITORY},
        "head_sha": WINDOWS_COMMIT,
        "id": RUN_ID,
        "name": "build-x64",
        "path": ".github/workflows/build-x64.yml",
        "repository": {"full_name": REPOSITORY},
        "run_attempt": RUN_ATTEMPT,
        "status": "completed",
        "workflow_id": 2468,
    }
    run.update(overrides)
    return run


def _workflow():
    return {
        "id": 2468,
        "name": "build-x64",
        "path": ".github/workflows/build-x64.yml",
        "state": "active",
    }


def _repository_info(**overrides):
    repository = {
        "archived": False,
        "default_branch": DEFAULT_BRANCH,
        "disabled": False,
        "fork": False,
        "full_name": REPOSITORY,
        "visibility": "public",
    }
    repository.update(overrides)
    return repository


def _artifacts(name="fk-chromium-windows-x64"):
    return {
        "total_count": 1,
        "artifacts": [
            {
                "archive_download_url": (
                    f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/13579/zip"
                ),
                "expired": False,
                "id": 13579,
                "name": name,
                "workflow_run": {"id": RUN_ID, "head_sha": WINDOWS_COMMIT},
            }
        ],
    }


def test_validate_workflow_run_binds_attempt_and_exact_artifact_id():
    """A rerun or artifact replacement must not inherit an earlier validation decision."""
    context = validate_workflow_run(
        _workflow_run(),
        _workflow(),
        _artifacts(),
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="success",
        artifact_name="fk-chromium-windows-x64",
        expected_run_attempt=RUN_ATTEMPT,
        expected_artifact_id=13579,
    )

    assert context.run_attempt == RUN_ATTEMPT
    for changed in (
        {"expected_run_attempt": RUN_ATTEMPT + 1, "expected_artifact_id": 13579},
        {"expected_run_attempt": RUN_ATTEMPT, "expected_artifact_id": 24680},
    ):
        with pytest.raises(ValueError):
            validate_workflow_run(
                _workflow_run(),
                _workflow(),
                _artifacts(),
                repository_info=_repository_info(),
                repository=REPOSITORY,
                default_branch=DEFAULT_BRANCH,
                expected_conclusion="success",
                artifact_name="fk-chromium-windows-x64",
                **changed,
            )


def test_validate_workflow_run_authorizes_exact_publisher_workflow_and_attempt_artifact():
    """A build artifact or similarly named workflow must not impersonate publisher diagnostics."""
    context = validate_workflow_run(
        _workflow_run(
            name="publish-release",
            path=".github/workflows/publish-release.yml",
            workflow_id=8642,
            event="workflow_run",
            conclusion="failure",
        ),
        {
            "id": 8642,
            "name": "publish-release",
            "path": ".github/workflows/publish-release.yml",
            "state": "active",
        },
        _artifacts("fk-chromium-publication-attempt"),
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="failure",
        artifact_name="fk-chromium-publication-attempt",
        allowed_events=("workflow_run",),
        expected_workflow_name="publish-release",
        expected_workflow_path=".github/workflows/publish-release.yml",
    )

    assert context.artifact_name == "fk-chromium-publication-attempt"


def test_verify_release_artifact_accepts_only_the_exact_gated_x64_payload(tmp_path):
    """A verifier that guesses names or coerces publish values could release the wrong build."""
    installer, portable, checksums = _write_release_artifact(tmp_path / "artifact")

    verified = verify_release_artifact(tmp_path / "artifact", require_publish=True)

    assert verified.upstream_tag == UPSTREAM_TAG
    assert verified.upstream_version == UPSTREAM_VERSION
    assert verified.fk_revision == 2
    assert verified.release_tag == RELEASE_TAG
    assert verified.windows_commit == WINDOWS_COMMIT
    assert verified.files == (installer, portable, checksums)


@pytest.mark.parametrize(
    ("metadata_override", "extra_name", "checksum_replacement"),
    (
        ({"publish": False}, None, None),
        ({"publish": "true"}, None, None),
        ({"release_tag": f"{UPSTREAM_VERSION}-fk.3"}, None, None),
        ({"upstream_version": "151.0.7922.174"}, None, None),
        ({"windows_commit": "not-a-sha"}, None, None),
        ({}, "unexpected.exe", None),
        ({}, None, "0" * 64),
    ),
)
def test_verify_release_artifact_rejects_ungated_mismatched_or_tampered_payloads(
    tmp_path, metadata_override, extra_name, checksum_replacement
):
    """Weak metadata, allow-list, or checksum validation must fail before publication."""
    directory = tmp_path / "artifact"
    _, _, checksums = _write_release_artifact(directory, **metadata_override)
    if extra_name is not None:
        (directory / extra_name).write_bytes(b"unexpected")
    if checksum_replacement is not None:
        text = checksums.read_text(encoding="utf-8")
        checksums.write_text(checksum_replacement + text[64:], encoding="utf-8")

    with pytest.raises(ValueError):
        verify_release_artifact(directory, require_publish=True)


def test_verify_release_artifact_rejects_duplicate_metadata_keys(tmp_path):
    """JSON duplicate-key shadowing must not change a checked publication decision."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    metadata_path = directory / "fk-build-metadata.json"
    metadata_path.write_text(
        json.dumps(_release_metadata())[:-1] + ', "publish": true}', encoding="utf-8"
    )

    with pytest.raises(ValueError):
        verify_release_artifact(directory, require_publish=True)


def test_release_metadata_verify_cli_emits_only_validated_release_fields(tmp_path):
    """The workflow-facing command must fail closed and expose only sanitized identity values."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "tools" / "release_metadata.py"),
            "verify",
            str(directory),
            "--require-publish",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "branding_commit": BRANDING_COMMIT,
        "checksums": "SHA256SUMS.txt",
        "fk_revision": 2,
        "installer": f"FK-Chromium-{UPSTREAM_VERSION}-Windows-x64-Installer.exe",
        "portable": f"FK-Chromium-{UPSTREAM_VERSION}-Windows-x64-Portable.zip",
        "release_tag": RELEASE_TAG,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_tag": UPSTREAM_TAG,
        "upstream_version": UPSTREAM_VERSION,
        "upstream_windows_commit": UPSTREAM_WINDOWS_COMMIT,
        "windows_commit": WINDOWS_COMMIT,
    }


def test_validate_workflow_run_refetches_one_trusted_completed_build_and_artifact():
    """Trusting workflow_run payload fields could grant writes to a fork or wrong run."""
    context = validate_workflow_run(
        _workflow_run(),
        _workflow(),
        _artifacts(),
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="success",
        artifact_name="fk-chromium-windows-x64",
    )

    assert context.run_id == RUN_ID
    assert context.head_sha == WINDOWS_COMMIT
    assert context.event == "workflow_dispatch"
    assert context.artifact_id == 13579
    assert context.run_url == f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}"


@pytest.mark.parametrize(
    "run_override",
    (
        {"id": True},
        {"workflow_id": 9999},
        {"path": ".github/workflows/other.yml"},
        {"name": "other"},
        {"repository": {"full_name": "attacker/fork"}},
        {"head_repository": {"full_name": "attacker/fork"}},
        {"head_branch": "feature/untrusted"},
        {"event": "pull_request"},
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"head_sha": "not-a-sha"},
    ),
)
def test_validate_workflow_run_rejects_untrusted_identity_fields(run_override):
    """Every authorization field must be derived from the API response and fail closed."""
    with pytest.raises(ValueError):
        validate_workflow_run(
            _workflow_run(**run_override),
            _workflow(),
            _artifacts(),
            repository_info=_repository_info(),
            repository=REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            expected_conclusion="success",
            artifact_name="fk-chromium-windows-x64",
        )


@pytest.mark.parametrize(
    "repository_override",
    (
        {"full_name": "attacker/fork"},
        {"default_branch": "other"},
        {"fork": True},
        {"archived": True},
        {"disabled": True},
        {"visibility": "private"},
    ),
)
def test_validate_workflow_run_derives_repository_and_default_branch_from_api(
    repository_override,
):
    """Repository/default-branch fields from workflow_run are locators, never authorization."""
    with pytest.raises(ValueError):
        validate_workflow_run(
            _workflow_run(),
            _workflow(),
            _artifacts(),
            repository_info=_repository_info(**repository_override),
            repository=REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            expected_conclusion="success",
            artifact_name="fk-chromium-windows-x64",
        )


@pytest.mark.parametrize(
    "artifact_payload",
    (
        {"total_count": 0, "artifacts": []},
        {"total_count": 2, "artifacts": _artifacts()["artifacts"] * 2},
        {"total_count": 1, "artifacts": [{**_artifacts()["artifacts"][0], "expired": True}]},
        {
            "total_count": 1,
            "artifacts": [
                {**_artifacts()["artifacts"][0], "workflow_run": {"id": RUN_ID + 1, "head_sha": WINDOWS_COMMIT}}
            ],
        },
    ),
)
def test_validate_workflow_run_rejects_missing_duplicate_expired_or_foreign_artifacts(
    artifact_payload,
):
    """Artifact selection must bind one immutable artifact to this exact run."""
    with pytest.raises(ValueError):
        validate_workflow_run(
            _workflow_run(),
            _workflow(),
            artifact_payload,
            repository_info=_repository_info(),
            repository=REPOSITORY,
            default_branch=DEFAULT_BRANCH,
            expected_conclusion="success",
            artifact_name="fk-chromium-windows-x64",
        )


def test_bind_release_to_run_rejects_metadata_from_a_different_windows_commit(tmp_path):
    """A valid artifact from another run must not inherit this run's publication authority."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    verified = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(head_sha="a" * 40),
        _workflow(),
        {
            "total_count": 1,
            "artifacts": [
                {
                    **_artifacts()["artifacts"][0],
                    "workflow_run": {"id": RUN_ID, "head_sha": "a" * 40},
                }
            ],
        },
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="success",
        artifact_name="fk-chromium-windows-x64",
    )

    with pytest.raises(ValueError):
        bind_release_to_run(verified, context)


def test_publish_false_artifact_is_a_valid_clean_noop_bound_to_its_build(tmp_path):
    """A successful nonpublishing build must classify false without weakening run binding."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory, publish=False)
    verified = verify_release_artifact(directory)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )

    assert classify_publication(verified, context) is False

    changed = verify_release_artifact(directory)
    changed = changed.__class__(**{**changed.__dict__, "windows_commit": "a" * 40})
    with pytest.raises(ValueError):
        classify_publication(changed, context)


def test_publication_attempt_round_trip_binds_both_runs_and_source_artifact(tmp_path):
    """Dropping any run/attempt/artifact identity must make the reporter payload invalid."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    verified = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    payload = create_publication_attempt(
        verified,
        context,
        publisher_run_id=123456789,
        publisher_run_attempt=2,
    )
    path = tmp_path / "publication-attempt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = read_publication_attempt(path)

    assert restored.publisher_run_id == 123456789
    assert restored.publisher_run_attempt == 2
    assert restored.source_run_id == RUN_ID
    assert restored.source_run_attempt == RUN_ATTEMPT
    assert restored.source_artifact_id == 13579
    assert restored.source_artifact_name == "fk-chromium-windows-x64"
    assert restored.release_tag == RELEASE_TAG
    assert "token" not in json.dumps(payload).lower()


def test_publication_failure_report_marker_is_attempt_and_artifact_specific(tmp_path):
    """A replay may be idempotent only for the exact publisher attempt and source artifact."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    verified = verify_release_artifact(directory, require_publish=True)
    source = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    payload = create_publication_attempt(
        verified, source, publisher_run_id=123456789, publisher_run_attempt=2
    )
    path = tmp_path / "publication-attempt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    attempt = read_publication_attempt(path)
    publisher = source.__class__(
        run_id=123456789,
        run_attempt=2,
        head_sha=WINDOWS_COMMIT,
        event="workflow_run",
        artifact_id=24680,
        artifact_name="fk-chromium-publication-attempt",
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
    )

    stage = classify_publication_failure_jobs(
        {
            "total_count": 2,
            "jobs": [
                {"name": "validate", "status": "completed", "conclusion": "success", "run_id": 123456789, "run_attempt": 2},
                {"name": "publish", "status": "completed", "conclusion": "failure", "run_id": 123456789, "run_attempt": 2},
            ],
        },
        123456789,
        2,
    )
    report = format_publication_failure_report(attempt, publisher, stage)

    assert stage == "publish"
    assert report.marker == f"fk-publish-failed:{RELEASE_TAG}:run:123456789:attempt:2"
    assert report.run_marker == "fk-publish-source-artifact:13579"
    assert UPSTREAM_TAG in report.body
    assert RELEASE_TAG in report.body
    assert str(RUN_ID) in report.body
    assert "logs" not in report.body.lower()


def _reserved_state():
    return {
        "last_success": {
            "fk_revision": 1,
            "upstream_tag": "151.0.7922.173-1.1",
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": ["151.0.7922.173-1.1", UPSTREAM_TAG],
        "assignments": {"151.0.7922.173-1.1": 1, UPSTREAM_TAG: 2},
        "revisions": {UPSTREAM_VERSION: 1},
        "successes": {"151.0.7922.173-1.1": 1},
    }


def test_record_success_json_uses_task7_api_and_preserves_all_histories():
    """Replacing state from metadata would erase reservations and permit revision collisions."""
    before = _reserved_state()

    updated = record_success_json(before, _release_metadata())

    assert updated == {
        "last_success": {
            "fk_revision": 2,
            "upstream_tag": UPSTREAM_TAG,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": before["attempted"],
        "assignments": before["assignments"],
        "revisions": {UPSTREAM_VERSION: 2},
        "successes": {"151.0.7922.173-1.1": 1, UPSTREAM_TAG: 2},
    }
    assert record_success_json(updated, _release_metadata()) == updated


def test_record_success_json_rejects_unreserved_or_ungated_metadata():
    """A successful release cannot invent an identity that Task 7 never reserved."""
    unreserved = _reserved_state()
    unreserved["assignments"] = {"151.0.7922.173-1.1": 1}
    unreserved["attempted"] = ["151.0.7922.173-1.1"]

    with pytest.raises(ValueError):
        record_success_json(unreserved, _release_metadata())
    with pytest.raises(ValueError):
        record_success_json(_reserved_state(), _release_metadata(publish=False))


def _contents_response(state, sha="a" * 40):
    encoded = base64.b64encode(
        (json.dumps(state, sort_keys=True) + "\n").encode("utf-8")
    ).decode("ascii")
    return {
        "content": encoded,
        "encoding": "base64",
        "name": "release-state.json",
        "path": "release-state.json",
        "sha": sha,
        "type": "file",
    }


def _contents_response_document(document, sha="a" * 40):
    encoded = base64.b64encode(document.encode("utf-8")).decode("ascii")
    return {
        "content": encoded,
        "encoding": "base64",
        "name": "release-state.json",
        "path": "release-state.json",
        "sha": sha,
        "type": "file",
    }


def _authorized_rerelease_state_document_with_duplicate(field):
    state = {
        "last_success": {
            "fk_revision": 1,
            "upstream_tag": UPSTREAM_TAG,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": [UPSTREAM_TAG, UPSTREAM_TAG],
        "assignments": {UPSTREAM_TAG: 1},
        "revisions": {UPSTREAM_VERSION: 1},
        "successes": {UPSTREAM_TAG: 1},
        "rerelease_assignments": {UPSTREAM_TAG: [2]},
        "rerelease_successes": {UPSTREAM_TAG: []},
    }
    keys = {
        "assignments": UPSTREAM_TAG,
        "revisions": UPSTREAM_VERSION,
        "successes": UPSTREAM_TAG,
        "rerelease_assignments": UPSTREAM_TAG,
        "rerelease_successes": UPSTREAM_TAG,
    }
    first_values = {
        "assignments": 999,
        "revisions": 999,
        "successes": 999,
        "rerelease_assignments": [999],
        "rerelease_successes": [999],
    }
    key = keys[field]
    canonical_value = state[field][key]
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    document = compact(state)
    canonical_member = f'{compact(field)}:{compact(state[field])}'
    duplicate_member = (
        f'{compact(field)}:{{{compact(key)}:{compact(first_values[field])},'
        f'{compact(key)}:{compact(canonical_value)}}}'
    )
    assert document.count(canonical_member) == 1
    return document.replace(canonical_member, duplicate_member)


def test_contents_api_accepts_only_crlf_wrapping_around_strict_base64():
    """GitHub's line-wrapped Base64 is valid, but spaces and other junk must remain invalid."""
    response = _contents_response(_reserved_state())
    encoded = response["content"]
    response["content"] = "\r\n".join(
        encoded[index : index + 20] for index in range(0, len(encoded), 20)
    )

    assert authorize_release_state(response, _release_metadata(), contents_response=True)

    response["content"] += " "
    with pytest.raises(ValueError):
        authorize_release_state(response, _release_metadata(), contents_response=True)


@pytest.mark.parametrize(
    "field",
    (
        "assignments",
        "revisions",
        "successes",
        "rerelease_assignments",
        "rerelease_successes",
    ),
)
def test_contents_api_state_read_rejects_duplicate_nested_identity_keys(field):
    """Contents API state cannot authorize a last-wins duplicate identity map."""
    response = _contents_response_document(
        _authorized_rerelease_state_document_with_duplicate(field)
    )

    with pytest.raises(ValueError, match="duplicate object keys"):
        authorize_release_state(response, _release_metadata(), contents_response=True)


def test_contents_api_state_read_rejects_duplicate_top_level_assignment_history():
    """Erasing a lower reservation with a second assignments member must fail closed."""
    tag_1 = "151.0.7922.173-1.1"
    tag_3 = "151.0.7922.173-3.1"
    state = {
        "last_success": {
            "fk_revision": 1,
            "upstream_tag": tag_1,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": [tag_1, UPSTREAM_TAG, tag_3],
        "assignments": {tag_1: 1, tag_3: 3},
        "revisions": {UPSTREAM_VERSION: 1},
        "successes": {tag_1: 1},
    }
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    document = compact(state)
    later = f'{compact("assignments")}:{compact(state["assignments"])}'
    duplicate = (
        f'{compact("assignments")}:{compact({tag_1: 1, UPSTREAM_TAG: 2})},' + later
    )
    metadata = _release_metadata(
        fk_revision=3,
        release_tag="151.0.7922.173-fk.3",
        upstream_tag=tag_3,
    )

    with pytest.raises(ValueError, match="duplicate object keys"):
        authorize_release_state(
            _contents_response_document(document.replace(later, duplicate)),
            metadata,
            contents_response=True,
        )


def test_contents_api_rejects_irrelevant_nested_duplicates_before_schema_validation():
    """Publisher diagnostics stay bounded and never echo duplicate hostile field names."""
    secret = "attacker-controlled-secret-field"
    document = (
        '{"last_success":null,"attempted":[],"irrelevant":'
        f'{{"{secret}":1,"{secret}":2}}}}'
    )

    with pytest.raises(ValueError) as raised:
        authorize_release_state(
            _contents_response_document(document),
            _release_metadata(),
            contents_response=True,
        )

    assert "duplicate object keys" in str(raised.value)
    assert secret not in str(raised.value)
    assert len(str(raised.value)) < 160


def test_release_state_authorization_requires_exact_assignment_and_no_conflicting_success():
    """A release must not mutate GitHub before Task 7's exact reservation is authorized."""
    assert authorize_release_state(_reserved_state(), _release_metadata()) == RELEASE_TAG

    wrong_assignment = _reserved_state()
    wrong_assignment["assignments"][UPSTREAM_TAG] = 3
    with pytest.raises(ValueError):
        authorize_release_state(wrong_assignment, _release_metadata())

    conflicting_success = _reserved_state()
    conflicting_success["successes"][UPSTREAM_TAG] = 1
    with pytest.raises(ValueError):
        authorize_release_state(conflicting_success, _release_metadata())


def test_release_state_authorization_rejects_an_already_successful_initial_pair():
    """Missing remote residue must not authorize republishing a recorded success."""
    successful = _reserved_state()
    successful["last_success"] = {
        "fk_revision": 2,
        "upstream_tag": UPSTREAM_TAG,
        "upstream_version": UPSTREAM_VERSION,
    }
    successful["successes"][UPSTREAM_TAG] = 2
    successful["revisions"][UPSTREAM_VERSION] = 2

    with pytest.raises(ValueError, match="already successful"):
        authorize_release_state(successful, _release_metadata())


def test_release_state_authorization_rejects_an_already_successful_rerelease_pair():
    """A recorded rerelease success cannot be published again through authorization."""
    state = {
        "last_success": {
            "fk_revision": 2,
            "upstream_tag": UPSTREAM_TAG,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": [UPSTREAM_TAG, UPSTREAM_TAG],
        "assignments": {UPSTREAM_TAG: 1},
        "successes": {UPSTREAM_TAG: 1},
        "revisions": {UPSTREAM_VERSION: 2},
        "rerelease_assignments": {UPSTREAM_TAG: [2]},
        "rerelease_successes": {UPSTREAM_TAG: [2]},
    }

    with pytest.raises(ValueError, match="already successful"):
        authorize_release_state(state, _release_metadata())


def test_release_state_authorizes_and_records_an_exact_rerelease_pair():
    """Task 8 must accept fk.2 for the same exact upstream tag without losing fk.1."""
    state = {
        "last_success": {
            "fk_revision": 1,
            "upstream_tag": UPSTREAM_TAG,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": [UPSTREAM_TAG, UPSTREAM_TAG],
        "assignments": {UPSTREAM_TAG: 1},
        "successes": {UPSTREAM_TAG: 1},
        "revisions": {UPSTREAM_VERSION: 1},
        "rerelease_assignments": {UPSTREAM_TAG: [2]},
        "rerelease_successes": {},
    }

    assert authorize_release_state(state, _release_metadata()) == RELEASE_TAG
    updated = record_success_json(state, _release_metadata())
    assert updated["assignments"] == {UPSTREAM_TAG: 1}
    assert updated["successes"] == {UPSTREAM_TAG: 1}
    assert updated["rerelease_assignments"] == {UPSTREAM_TAG: [2]}
    assert updated["rerelease_successes"] == {UPSTREAM_TAG: [2]}


def test_release_state_authorization_dry_run_rejects_higher_initial_reservation():
    """Authorization must fail before publication when fk.3 remains ahead of initial fk.4."""
    tag_a = UPSTREAM_TAG
    tag_b = "151.0.7922.173-1.1"
    state = {
        "last_success": {
            "fk_revision": 2,
            "upstream_tag": tag_a,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": [tag_a, tag_b],
        "assignments": {tag_a: 1, tag_b: 4},
        "successes": {tag_a: 1},
        "revisions": {UPSTREAM_VERSION: 2},
        "rerelease_assignments": {tag_a: [2, 3]},
        "rerelease_successes": {tag_a: [2]},
    }
    before = json.loads(json.dumps(state))
    metadata = _release_metadata(
        fk_revision=4,
        release_tag="151.0.7922.173-fk.4",
        upstream_tag=tag_b,
    )

    with pytest.raises(ValueError, match="published FK revision"):
        authorize_release_state(state, metadata)

    assert state == before


def test_release_state_authorization_accepts_lowest_then_final_success_records():
    """The globally lowest rerelease may authorize and complete without mutating input state."""
    tag_a = UPSTREAM_TAG
    tag_b = "151.0.7922.173-1.1"
    state = {
        "last_success": {
            "fk_revision": 2,
            "upstream_tag": tag_a,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": [tag_a, tag_b],
        "assignments": {tag_a: 1, tag_b: 4},
        "successes": {tag_a: 1},
        "revisions": {UPSTREAM_VERSION: 2},
        "rerelease_assignments": {tag_a: [2, 3]},
        "rerelease_successes": {tag_a: [2]},
    }
    before = json.loads(json.dumps(state))
    metadata = _release_metadata(
        fk_revision=3,
        release_tag="151.0.7922.173-fk.3",
    )

    assert authorize_release_state(state, metadata) == "151.0.7922.173-fk.3"
    assert state == before

    updated = record_success_json(state, metadata)
    assert updated["rerelease_successes"] == {tag_a: [2, 3]}
    assert updated["revisions"] == {UPSTREAM_VERSION: 3}
    assert updated["assignments"] == {tag_a: 1, tag_b: 4}


def test_release_state_authorization_dry_run_rejects_higher_rerelease():
    """A same-tag fk.3 rerelease cannot publish while that tag's fk.2 is unresolved."""
    state = {
        "last_success": {
            "fk_revision": 1,
            "upstream_tag": UPSTREAM_TAG,
            "upstream_version": UPSTREAM_VERSION,
        },
        "attempted": [UPSTREAM_TAG],
        "assignments": {UPSTREAM_TAG: 1},
        "successes": {UPSTREAM_TAG: 1},
        "revisions": {UPSTREAM_VERSION: 1},
        "rerelease_assignments": {UPSTREAM_TAG: [2, 3]},
        "rerelease_successes": {},
    }
    before = json.loads(json.dumps(state))
    metadata = _release_metadata(
        fk_revision=3,
        release_tag="151.0.7922.173-fk.3",
    )

    with pytest.raises(ValueError, match="published FK revision"):
        authorize_release_state(state, metadata)

    assert state == before


def test_contents_api_state_update_retries_conflict_and_observes_idempotent_winner():
    """A stale state PUT must never overwrite an independently committed state transition."""
    updated = record_success_json(_reserved_state(), _release_metadata())
    responses = iter(
        (
            (200, _contents_response(_reserved_state())),
            (409, {"message": "sha does not match"}),
            (200, _contents_response(updated, sha="b" * 40)),
        )
    )
    requests = []

    def requester(method, url, token, payload):
        requests.append((method, url, token, payload))
        return next(responses)

    result = update_release_state_via_contents_api(
        repository=REPOSITORY,
        branch=DEFAULT_BRANCH,
        metadata=_release_metadata(),
        token="test-token",
        requester=requester,
    )

    assert result == "unchanged"
    assert [request[0] for request in requests] == ["GET", "PUT", "GET"]
    put_payload = requests[1][3]
    assert put_payload["sha"] == "a" * 40
    assert put_payload["branch"] == DEFAULT_BRANCH
    assert "[skip ci]" in put_payload["message"]
    assert json.loads(base64.b64decode(put_payload["content"])) == updated


def test_contents_api_state_update_commits_once_after_release_success():
    """The success updater must send one checked SHA and the full preserved Task 7 state."""
    requests = []

    def requester(method, url, token, payload):
        requests.append((method, url, token, payload))
        if method == "GET":
            return 200, _contents_response(_reserved_state())
        return 200, {"commit": {"sha": "c" * 40}}

    result = update_release_state_via_contents_api(
        repository=REPOSITORY,
        branch=DEFAULT_BRANCH,
        metadata=_release_metadata(),
        token="test-token",
        requester=requester,
    )

    assert result == "updated"
    assert [request[0] for request in requests] == ["GET", "PUT"]


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    (
        (404, {"message": "Not Found"}, "available"),
        (
            200,
            {
                "object": {"sha": WINDOWS_COMMIT, "type": "commit"},
                "ref": f"refs/tags/{RELEASE_TAG}",
            },
            "existing",
        ),
    ),
)
def test_release_destination_allows_only_absent_or_same_commit_public_tag(
    status, payload, expected
):
    """An occupied public tag must never receive assets from a different build identity."""
    def requester(method, url, token, request_payload):
        assert method == "GET"
        assert request_payload is None
        return status, payload

    assert validate_release_destination(
        repository=REPOSITORY,
        public_tag=RELEASE_TAG,
        windows_commit=WINDOWS_COMMIT,
        token="test-token",
        requester=requester,
    ) == expected

    if status == 200:
        with pytest.raises(ValueError):
            validate_release_destination(
                repository=REPOSITORY,
                public_tag=RELEASE_TAG,
                windows_commit="a" * 40,
                token="test-token",
                requester=requester,
            )


@pytest.mark.parametrize(
    "public_tag",
    (
        "0151.0.7922.173-fk.2",
        "151.00.7922.173-fk.2",
        "151.0.7922.173-fk.02",
        "١٥١.٠.٧٩٢٢.١٧٣-fk.2",
        "１５１.０.７９２２.１７３-fk.2",
    ),
)
def test_release_destination_rejects_noncanonical_public_numeric_spellings(public_tag):
    """Public tag aliases must fail before any destination API request."""
    def requester(*_args):
        pytest.fail("noncanonical public tag reached the API")

    with pytest.raises(ValueError, match="canonical FK public release tag"):
        validate_release_destination(
            repository=REPOSITORY,
            public_tag=public_tag,
            windows_commit=WINDOWS_COMMIT,
            token="test-token",
            requester=requester,
        )


def _job(name, conclusion):
    return {
        "conclusion": conclusion,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}/job/1",
        "id": 1,
        "name": name,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "status": "completed",
        "steps": [],
    }


def test_failure_classifier_distinguishes_fatal_stage_from_genuine_twelve_stage_exhaustion():
    """A normal success/cancellation must never be mislabeled as compiler exhaustion."""
    fatal = {"total_count": 2, "jobs": [_job("build / build-1", "success"), _job("build / build-2", "failure")]}
    exhausted_jobs = [_job(f"build / build-{number}", "success") for number in range(1, 13)]
    exhausted_jobs.append(_job("build / complete", "failure"))

    assert classify_failure_jobs(fatal, RUN_ID, RUN_ATTEMPT) == "build-2"
    assert classify_failure_jobs(
        {"total_count": len(exhausted_jobs), "jobs": exhausted_jobs}, RUN_ID, RUN_ATTEMPT
    ) == "12-stage exhaustion"

    not_exhausted = list(exhausted_jobs)
    not_exhausted[7] = _job("build / build-8", "skipped")
    with pytest.raises(ValueError):
        classify_failure_jobs(
            {"total_count": len(not_exhausted), "jobs": not_exhausted}, RUN_ID, RUN_ATTEMPT
        )

    drifted = {"total_count": 1, "jobs": [{**_job("build / build-2", "failure"), "run_attempt": RUN_ATTEMPT + 1}]}
    with pytest.raises(ValueError):
        classify_failure_jobs(drifted, RUN_ID, RUN_ATTEMPT)


def test_failure_report_contains_only_validated_required_diagnostics(tmp_path):
    """Issue text must contain provenance and recovery data without copying logs or environments."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory, publish=False)
    verified = verify_release_artifact(directory, require_publish=False)
    context = validate_workflow_run(
        _workflow_run(conclusion="failure"),
        _workflow(),
        _artifacts(name="fk-chromium-windows-build-metadata"),
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="failure",
        artifact_name="fk-chromium-windows-build-metadata",
    )

    report = format_failure_report(verified, context, "build-7")

    assert report.marker == f"fk-build-failed:{UPSTREAM_VERSION}"
    assert report.run_marker == f"fk-build-run:{RUN_ID}:attempt:{RUN_ATTEMPT}"
    assert report.title == f"FK Chromium build failed: {UPSTREAM_VERSION}"
    for value in (
        "Failure stage: `build-7`",
        f"{context.run_url}/attempts/{RUN_ATTEMPT}",
        f"Upstream tag: `{UPSTREAM_TAG}`",
        f"Upstream commit: `{UPSTREAM_COMMIT}`",
        f"FK branding commit: `{BRANDING_COMMIT}`",
        f"Windows build commit: `{WINDOWS_COMMIT}`",
        "force_rebuild=true",
    ):
        assert value in report.body
    assert "env" not in report.body.lower()
    assert "secret" not in report.body.lower()


def test_find_failure_issue_matches_label_and_marker_across_closed_issues():
    """Ignoring closed issues would create duplicate version reports after manual closure."""
    marker = f"fk-build-failed:{UPSTREAM_VERSION}"
    issues = [
        {
            "body": f"<!-- {marker} -->\nold report",
            "labels": [{"name": "fk-build-failure"}],
            "number": 42,
            "pull_request": None,
            "state": "closed",
            "title": "old",
        },
        {
            "body": f"<!-- {marker} -->\nwrong label",
            "labels": [{"name": "other"}],
            "number": 43,
            "state": "open",
            "title": "other",
        },
    ]

    assert find_failure_issue(issues, marker) == 42

    with pytest.raises(ValueError):
        find_failure_issue([issues[0], {**issues[0], "number": 44}], marker)


def test_failure_issue_api_is_idempotent_for_a_closed_issue_and_same_run_comment(tmp_path):
    """Rerunning the reporter must neither reopen/duplicate the issue nor repeat its comment."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory, publish=False)
    verified = verify_release_artifact(directory, require_publish=False)
    context = validate_workflow_run(
        _workflow_run(conclusion="failure"),
        _workflow(),
        _artifacts(name="fk-chromium-windows-build-metadata"),
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="failure",
        artifact_name="fk-chromium-windows-build-metadata",
    )
    report = format_failure_report(verified, context, "build-7")
    requests = []

    def requester(method, url, token, payload):
        requests.append((method, url, payload))
        if "/labels/fk-build-failure" in url:
            return 200, {"name": "fk-build-failure"}
        if url.endswith("/issues?state=all&labels=fk-build-failure&per_page=100&page=1"):
            return 200, [
                {
                    "body": f"<!-- {report.marker} -->",
                    "labels": [{"name": "fk-build-failure"}],
                    "number": 42,
                    "state": "closed",
                    "title": report.title,
                }
            ]
        if url.endswith("/issues/42/comments?per_page=100&page=1"):
            return 200, [
                {
                    "body": (
                        f"<!-- fk-build-run:{RUN_ID}:attempt:{RUN_ATTEMPT} -->\n"
                        "already reported"
                    )
                }
            ]
        raise AssertionError((method, url, payload))

    result = report_failure_issue_via_api(
        repository=REPOSITORY,
        report=report,
        token="test-token",
        requester=requester,
    )

    assert result == "unchanged"
    assert all(method == "GET" for method, _, _ in requests)


@pytest.mark.parametrize(("existing", "expected"), ((False, "created"), (True, "commented")))
def test_failure_issue_api_creates_once_then_comments_the_same_open_or_closed_issue(
    tmp_path, existing, expected
):
    """Missing versions create one labeled issue; later distinct runs append one sanitized comment."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory, publish=False)
    verified = verify_release_artifact(directory, require_publish=False)
    context = validate_workflow_run(
        _workflow_run(conclusion="failure"),
        _workflow(),
        _artifacts(name="fk-chromium-windows-build-metadata"),
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="failure",
        artifact_name="fk-chromium-windows-build-metadata",
    )
    report = format_failure_report(verified, context, "build-3")
    posted = []

    def requester(method, url, token, payload):
        if method == "POST":
            posted.append((url, payload))
            return 201, {"id": 1}
        if "/labels/fk-build-failure" in url:
            return 200, {"name": "fk-build-failure"}
        if url.endswith("/issues?state=all&labels=fk-build-failure&per_page=100&page=1"):
            if not existing:
                return 200, []
            return 200, [
                {
                    "body": f"<!-- {report.marker} -->",
                    "labels": [{"name": "fk-build-failure"}],
                    "number": 42,
                    "state": "closed",
                }
            ]
        if url.endswith("/issues/42/comments?per_page=100&page=1"):
            return 200, [
                {
                    "body": (
                        f"<!-- fk-build-run:{RUN_ID}:attempt:{RUN_ATTEMPT - 1} -->\n"
                        "same run, earlier attempt"
                    )
                }
            ]
        raise AssertionError((method, url, payload))

    result = report_failure_issue_via_api(
        repository=REPOSITORY,
        report=report,
        token="test-token",
        requester=requester,
    )

    assert result == expected
    assert len(posted) == 1
    assert report.body in posted[0][1].values()
    if not existing:
        assert posted[0][1]["labels"] == ["fk-build-failure"]


def test_release_notes_have_exact_provenance_fields_and_unsigned_chinese_warning(tmp_path):
    """Dropping provenance or the SmartScreen disclosure would make a public release misleading."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    verified = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(),
        _workflow(),
        _artifacts(),
        repository_info=_repository_info(),
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        expected_conclusion="success",
        artifact_name="fk-chromium-windows-x64",
    )

    notes = format_release_notes(verified, context)

    assert notes.splitlines() == [
        "Product: FK Chromium (火焰库拉浏览器)",
        f"Upstream tag: `{UPSTREAM_TAG}`",
        f"Upstream commit: `{UPSTREAM_COMMIT}`",
        f"FK branding commit: `{BRANDING_COMMIT}`",
        f"Windows build commit: `{WINDOWS_COMMIT}`",
        f"Workflow run: {context.run_url}",
        "",
        "此安装程序尚未进行 Windows 代码签名，Microsoft Defender SmartScreen 可能显示未知发布者警告。",
    ]


UNTAGGED_SLUG = "untagged-0123456789abcdefabcd"


def _release_api_payload(
    release, notes, *, release_id=7654, draft=False, assets=(), draft_slug=UNTAGGED_SLUG
):
    api_root = f"https://api.github.com/repos/{REPOSITORY}"
    release_path = draft_slug if draft else RELEASE_TAG
    return {
        "assets": list(assets),
        "body": notes,
        "draft": draft,
        "html_url": f"https://github.com/{REPOSITORY}/releases/tag/{release_path}",
        "id": release_id,
        "name": RELEASE_TAG,
        "prerelease": False,
        "tag_name": RELEASE_TAG,
        "target_commitish": WINDOWS_COMMIT,
        "upload_url": (
            f"https://uploads.github.com/repos/{REPOSITORY}/releases/"
            f"{release_id}/assets{{?name,label}}"
        ),
        "url": f"{api_root}/releases/{release_id}",
    }


def _asset_api_payload(path, asset_id, *, draft_slug=None):
    release_path = RELEASE_TAG if draft_slug is None else draft_slug
    return {
        "browser_download_url": (
            f"https://github.com/{REPOSITORY}/releases/download/{release_path}/{path.name}"
        ),
        "content_type": {
            ".exe": "application/vnd.microsoft.portable-executable",
            ".zip": "application/zip",
            ".txt": "text/plain",
        }[path.suffix],
        "id": asset_id,
        "label": None,
        "name": path.name,
        "size": path.stat().st_size,
        "state": "uploaded",
        "url": f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{asset_id}",
    }


def _ref_api_payload():
    return {
        "object": {
            "sha": WINDOWS_COMMIT,
            "type": "commit",
            "url": f"https://api.github.com/repos/{REPOSITORY}/git/commits/{WINDOWS_COMMIT}",
        },
        "ref": f"refs/tags/{RELEASE_TAG}",
        "url": f"https://api.github.com/repos/{REPOSITORY}/git/refs/tags/{RELEASE_TAG}",
    }


def test_any_existing_public_release_or_tag_residue_fails_closed(tmp_path):
    """A rerun must never adopt a public destination, even when every field is exact."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    release = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    notes = format_release_notes(release, context)
    assets = [_asset_api_payload(path, 8000 + index) for index, path in enumerate(release.files)]

    def requester(method, url, token, payload):
        assert method == "GET" and payload is None
        if "/git/ref/tags/" in url:
            return 200, {"ref": f"refs/tags/{RELEASE_TAG}", "object": {"type": "commit", "sha": WINDOWS_COMMIT}}
        if "/releases/tags/" in url:
            return 200, _release_api_payload(release, notes, assets=assets)
        raise AssertionError(url)

    with pytest.raises(ValueError, match="already exists.*manual inspection"):
        publish_release_via_api(
            repository=REPOSITORY, release=release, notes=notes, token="test-token",
            requester=requester,
            asset_requester=lambda *args: pytest.fail("existing release was adopted"),
        )

    def tag_only_requester(method, url, token, payload):
        if "/git/ref/tags/" in url:
            return 200, {
                "ref": f"refs/tags/{RELEASE_TAG}",
                "object": {"type": "commit", "sha": WINDOWS_COMMIT},
            }
        if "/releases/tags/" in url:
            return 404, {"message": "Not Found"}
        raise AssertionError(url)

    with pytest.raises(ValueError, match="already exists.*manual inspection"):
        publish_release_via_api(
            repository=REPOSITORY, release=release, notes=notes, token="test-token",
            requester=tag_only_requester,
        )


def test_release_asset_redirects_allow_only_https_github_cdn_without_userinfo():
    """A bearer token must never follow an attacker-controlled asset redirect."""
    accepted = (
        "https://release-assets.githubusercontent.com/github-production-release-asset/1/file?sig=x"
    )
    assert _safe_asset_redirect_url(accepted) == accepted
    for unsafe in (
        "http://release-assets.githubusercontent.com/file",
        "https://release-assets.githubusercontent.com.attacker.invalid/file",
        "https://token@release-assets.githubusercontent.com/file",
        "https://github.com/firekula/fk-chromium-windows/releases/download/file",
    ):
        with pytest.raises(ValueError):
            _safe_asset_redirect_url(unsafe)


def test_create_only_release_preserves_draft_and_emits_bound_locator_on_failure(tmp_path):
    """A failed upload must leave the new draft untouched and disclose only its trusted locator."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    release = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    notes = format_release_notes(release, context)
    requests = []
    ref_created = False

    def requester(method, url, token, payload):
        nonlocal ref_created
        requests.append((method, url, payload))
        if "/git/ref/tags/" in url:
            return (200, _ref_api_payload()) if ref_created else (404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/git/refs"):
            assert payload == {"ref": f"refs/tags/{RELEASE_TAG}", "sha": WINDOWS_COMMIT}
            ref_created = True
            return 201, _ref_api_payload()
        if method == "GET" and "/releases/tags/" in url:
            return 404, {"message": "Not Found"}
        if method == "POST" and url.endswith("/releases"):
            return 201, _release_api_payload(release, notes, draft=True, assets=())
        raise AssertionError((method, url, payload))

    def failing_upload(method, url, token, payload, content_type=None):
        raise RuntimeError("upload failed with token secret-value")

    with pytest.raises(RuntimeError) as raised:
        publish_release_via_api(
            repository=REPOSITORY, release=release, notes=notes, token="test-token",
            requester=requester, asset_requester=failing_upload,
        )

    assert all(method not in {"DELETE", "PATCH"} for method, _, _ in requests)
    assert _sanitized_created_draft_note(
        raised.value, expected_repository=REPOSITORY,
        expected_release_tag=RELEASE_TAG, expected_release_id=7654,
    ) == (
        "release draft preserved: "
        f"https://github.com/{REPOSITORY}/releases/tag/{UNTAGGED_SLUG} (id 7654)"
    )


@pytest.mark.parametrize(
    "draft_url",
    (
        f"https://attacker.invalid/{UNTAGGED_SLUG}",
        f"https://user@github.com/{REPOSITORY}/releases/tag/{UNTAGGED_SLUG}",
        f"https://github.com:444/{REPOSITORY}/releases/tag/{UNTAGGED_SLUG}",
        f"https://github.com/{REPOSITORY}/releases/download/{UNTAGGED_SLUG}",
        f"https://github.com/{REPOSITORY}/releases/tag/untagged-0123456789abcdefabcg",
        f"https://github.com/{REPOSITORY}/releases/tag/untagged-{'a' * 200}",
        f"https://github.com/{REPOSITORY}/releases/tag/{UNTAGGED_SLUG}\nevil",
        f"https://github.com/{REPOSITORY}/releases/tag/{UNTAGGED_SLUG}%0aevil",
    ),
)
def test_draft_release_rejects_untrusted_or_malformed_untagged_locator(tmp_path, draft_url):
    """A draft locator must be one bounded GitHub-owned untagged slug for this repository."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    release = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    notes = format_release_notes(release, context)
    ref_created = False

    def requester(method, url, token, payload):
        nonlocal ref_created
        if "/git/ref/tags/" in url:
            return (200, _ref_api_payload()) if ref_created else (404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/git/refs"):
            ref_created = True
            return 201, _ref_api_payload()
        if method == "GET" and "/releases/tags/" in url:
            return 404, {"message": "Not Found"}
        if method == "POST" and url.endswith("/releases"):
            created = _release_api_payload(release, notes, draft=True)
            created["html_url"] = draft_url
            return 201, created
        raise AssertionError((method, url, payload))

    with pytest.raises(ValueError):
        publish_release_via_api(
            repository=REPOSITORY, release=release, notes=notes, token="test-token",
            requester=requester,
            asset_requester=lambda *args: pytest.fail("invalid draft reached upload"),
        )


def test_draft_release_rejects_asset_with_a_different_valid_opaque_slug(tmp_path):
    """A regex-valid asset slug must still be identical to the created draft locator."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    release = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    notes = format_release_notes(release, context)
    path = release.files[0]
    asset = _asset_api_payload(
        path, 8100, draft_slug="untagged-fedcba9876543210fedc"
    )

    with pytest.raises(ValueError, match="release asset metadata is not exact"):
        _validate_release_payload(
            _release_api_payload(release, notes, draft=True, assets=[asset]),
            repository=REPOSITORY,
            release=release,
            notes=notes,
            draft=True,
            expected_release_id=7654,
            expected_asset_ids={path.name: 8100},
            expected_draft_slug=UNTAGGED_SLUG,
        )


def test_create_only_release_publishes_exact_created_id_and_asset_ids(tmp_path):
    """Only the newly created draft and its three newly uploaded assets may be published."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    release = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    notes = format_release_notes(release, context)
    uploaded = []
    published = False
    ref_created = False
    requests = []

    def current_payload(draft):
        assets = [
            {
                **asset,
                "browser_download_url": (
                    f"https://github.com/{REPOSITORY}/releases/download/"
                    f"{UNTAGGED_SLUG if draft else RELEASE_TAG}/{asset['name']}"
                ),
            }
            for asset in uploaded
        ]
        if draft and len(assets) == 3:
            assets.reverse()
        return _release_api_payload(release, notes, draft=draft, assets=assets)

    def requester(method, url, token, payload):
        nonlocal published, ref_created
        requests.append((method, url, payload))
        if "/git/ref/tags/" in url:
            return (200, _ref_api_payload()) if ref_created else (404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/git/refs"):
            assert payload == {"ref": f"refs/tags/{RELEASE_TAG}", "sha": WINDOWS_COMMIT}
            ref_created = True
            return 201, _ref_api_payload()
        if method == "GET" and "/releases/tags/" in url:
            return (200, current_payload(False)) if published else (404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/releases"):
            return 201, current_payload(True)
        if method == "GET" and url.endswith("/releases/7654"):
            return 200, current_payload(True)
        if method == "PATCH" and url.endswith("/releases/7654"):
            assert payload == {"draft": False}
            published = True
            return 200, current_payload(False)
        raise AssertionError((method, url, payload))

    def asset_requester(method, url, token, payload, content_type=None):
        if method == "POST":
            name = url.split("name=", 1)[1]
            path = next(path for path in release.files if path.name == name)
            assert payload == path.read_bytes()
            asset = _asset_api_payload(
                path, 8100 + len(uploaded), draft_slug=UNTAGGED_SLUG
            )
            uploaded.append(asset)
            return 201, asset
        asset = next(item for item in uploaded if item["url"] == url)
        path = next(path for path in release.files if path.name == asset["name"])
        return 200, path.read_bytes()

    assert publish_release_via_api(
        repository=REPOSITORY, release=release, notes=notes, token="test-token",
        requester=requester, asset_requester=asset_requester,
    ) == "published"
    assert published is True
    assert [asset["id"] for asset in uploaded] == [8100, 8101, 8102]
    mutating = [(method, url, payload) for method, url, payload in requests if method != "GET"]
    assert [method for method, _, _ in mutating] == ["POST", "POST", "PATCH"]
    assert mutating[0][1].endswith("/git/refs")
    assert mutating[1][1].endswith("/releases")
    assert all(method != "DELETE" for method, _, _ in requests)


def test_release_ref_creation_collision_fails_before_draft_without_cleanup(tmp_path):
    """A 422 creating the exact lightweight tag is a hard race, never an adoption path."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    release = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    notes = format_release_notes(release, context)
    requests = []

    def requester(method, url, token, payload):
        requests.append((method, url, payload))
        if method == "GET" and "/git/ref/tags/" in url:
            return 404, {"message": "Not Found"}
        if method == "GET" and "/releases/tags/" in url:
            return 404, {"message": "Not Found"}
        if method == "POST" and url.endswith("/git/refs"):
            return 422, {"message": "Reference already exists"}
        raise AssertionError((method, url, payload))

    with pytest.raises(ValueError):
        publish_release_via_api(
            repository=REPOSITORY, release=release, notes=notes,
            token="test-token", requester=requester,
        )

    assert all(not url.endswith("/releases") for _, url, _ in requests)
    assert all(method not in {"PATCH", "DELETE"} for method, _, _ in requests)


@pytest.mark.parametrize("failure_point", ("patch_transport", "patch_response", "final_hash"))
def test_release_failures_after_patch_begins_report_uncertain_publication(
    tmp_path, failure_point
):
    """After PATCH starts, neither a transport result nor later validation proves draft state."""
    directory = tmp_path / "artifact"
    _write_release_artifact(directory)
    release = verify_release_artifact(directory, require_publish=True)
    context = validate_workflow_run(
        _workflow_run(), _workflow(), _artifacts(), repository_info=_repository_info(),
        repository=REPOSITORY, default_branch=DEFAULT_BRANCH,
        expected_conclusion="success", artifact_name="fk-chromium-windows-x64",
    )
    notes = format_release_notes(release, context)
    uploaded = []
    ref_created = False
    published = False

    def current_payload(draft):
        release_path = UNTAGGED_SLUG if draft else RELEASE_TAG
        assets = [
            {
                **asset,
                "browser_download_url": (
                    f"https://github.com/{REPOSITORY}/releases/download/"
                    f"{release_path}/{asset['name']}"
                ),
            }
            for asset in uploaded
        ]
        return _release_api_payload(release, notes, draft=draft, assets=assets)

    def requester(method, url, token, payload):
        nonlocal ref_created, published
        if "/git/ref/tags/" in url:
            return (200, _ref_api_payload()) if ref_created else (404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/git/refs"):
            ref_created = True
            return 201, _ref_api_payload()
        if method == "GET" and "/releases/tags/" in url:
            return (200, current_payload(False)) if published else (404, {"message": "Not Found"})
        if method == "POST" and url.endswith("/releases"):
            return 201, current_payload(True)
        if method == "GET" and url.endswith("/releases/7654"):
            return 200, current_payload(True)
        if method == "PATCH" and url.endswith("/releases/7654"):
            if failure_point == "patch_transport":
                raise RuntimeError("secret-token hidden success")
            published = True
            response = current_payload(False)
            if failure_point == "patch_response":
                response["id"] = 9999
            return 200, response
        raise AssertionError((method, url, payload))

    def asset_requester(method, url, token, payload, content_type=None):
        if method == "POST":
            name = url.split("name=", 1)[1]
            path = next(path for path in release.files if path.name == name)
            asset = _asset_api_payload(
                path, 8200 + len(uploaded), draft_slug=UNTAGGED_SLUG
            )
            uploaded.append(asset)
            return 201, asset
        asset = next(item for item in uploaded if item["url"] == url)
        path = next(path for path in release.files if path.name == asset["name"])
        if published and failure_point == "final_hash":
            return 200, b"corrupt final secret-token"
        return 200, path.read_bytes()

    with pytest.raises((RuntimeError, ValueError)) as raised:
        publish_release_via_api(
            repository=REPOSITORY, release=release, notes=notes,
            token="test-token", requester=requester,
            asset_requester=asset_requester,
        )

    diagnostic = _sanitized_created_draft_note(
        raised.value, expected_repository=REPOSITORY,
        expected_release_tag=RELEASE_TAG, expected_release_id=7654,
    )
    assert diagnostic == (
        "publication state uncertain: inspect "
        f"https://github.com/{REPOSITORY}/releases/tag/{RELEASE_TAG} (id 7654)"
    )
    assert "secret-token" not in diagnostic


def test_draft_locator_sanitizer_rejects_valid_forged_repository_tag_and_tokens():
    """Syntactic validity alone must not let exception notes redirect the operator."""
    error = RuntimeError("secret-token")
    error._trusted_created_draft_release_id = 7654
    error._trusted_created_draft_slug = UNTAGGED_SLUG
    error._trusted_release_failure_phase = "draft"
    error.add_note(
        "created draft release repository=attacker/fork "
        f"tag={RELEASE_TAG} id=7654 draft_slug={UNTAGGED_SLUG}"
    )
    error.add_note(
        f"created draft release repository={REPOSITORY} "
        f"tag=151.0.7922.174-fk.2 id=7654 draft_slug={UNTAGGED_SLUG}"
    )
    error.add_note(
        f"created draft release repository={REPOSITORY} "
        f"tag={RELEASE_TAG} id=7654 token=secret-token"
    )

    assert _sanitized_created_draft_note(
        error, expected_repository=REPOSITORY,
        expected_release_tag=RELEASE_TAG, expected_release_id=7654,
    ) is None

    error.add_note(
        f"created draft release repository={REPOSITORY} tag={RELEASE_TAG} "
        f"id=7654 draft_slug={UNTAGGED_SLUG}"
    )
    assert _sanitized_created_draft_note(
        error, expected_repository=REPOSITORY,
        expected_release_tag=RELEASE_TAG, expected_release_id=7654,
    ) == (
        "release draft preserved: "
        f"https://github.com/{REPOSITORY}/releases/tag/{UNTAGGED_SLUG} (id 7654)"
    )


@pytest.mark.parametrize("fail_always", (False, True))
def test_cli_suppresses_stderr_failures_and_original_exception_context(
    monkeypatch, fail_always
):
    """Diagnostic output failure must still end in one context-suppressed sanitized exit."""
    import tools.release_workflow as workflow

    class SecretFailure(Exception):
        pass

    class BrokenStderr:
        def __init__(self):
            self.calls = 0

        def write(self, text):
            self.calls += 1
            if fail_always or self.calls == 1:
                raise OSError("stderr leaked secret-token")
            return len(text)

        def flush(self):
            raise OSError("flush leaked secret-token")

    def fail(**kwargs):
        error = SecretFailure("original secret-token")
        error.add_note("note secret-token")
        error.add_note(
            f"release publication uncertain repository={REPOSITORY} "
            f"tag={RELEASE_TAG} id=7654"
        )
        error._trusted_created_draft_release_id = 7654
        error._trusted_release_failure_phase = "uncertain"
        raise error

    monkeypatch.setattr(workflow, "validate_release_destination", fail)
    broken = BrokenStderr()
    monkeypatch.setattr(sys, "stderr", broken)

    with pytest.raises(SystemExit) as raised:
        main([
            "validate-destination", "--repository", REPOSITORY,
            "--public-tag", RELEASE_TAG, "--windows-commit", WINDOWS_COMMIT,
        ])

    assert raised.value.code == 1
    assert raised.value.__context__ is None
    rendered = "".join(traceback.format_exception(raised.value))
    for forbidden in ("SecretFailure", "original secret-token", "note secret-token", "stderr leaked"):
        assert forbidden not in rendered


@pytest.mark.parametrize("exception", (KeyboardInterrupt(), SystemExit(23)))
def test_cli_does_not_intercept_process_control_exceptions(monkeypatch, exception):
    """KeyboardInterrupt and SystemExit from the transaction remain process-control signals."""
    import tools.release_workflow as workflow

    def stop(**kwargs):
        raise exception

    monkeypatch.setattr(workflow, "validate_release_destination", stop)
    with pytest.raises(type(exception)) as raised:
        main([
            "validate-destination", "--repository", REPOSITORY,
            "--public-tag", RELEASE_TAG, "--windows-commit", WINDOWS_COMMIT,
        ])
    if isinstance(exception, SystemExit):
        assert raised.value.code == 23


def _workflow_text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _assert_external_actions_are_pinned(workflow):
    external = re.findall(r"(?m)^\s*uses: ([^./][^@\s]+)@([^\s]+)", workflow)
    assert external
    assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for _, reference in external)


def _init_repository(path, filename, contents, tag=None):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Tests"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "tests@example.invalid"], check=True)
    target = path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "fixture"], check=True)
    if tag is not None:
        subprocess.run(["git", "-C", str(path), "tag", tag], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def build_identity_cli_repositories(tmp_path_factory):
    root = tmp_path_factory.mktemp("build-identity-cli")
    windows = root / "windows"
    upstream_windows = root / "upstream-windows"
    common = upstream_windows / "ungoogled-chromium"
    branding = root / "branding"
    _init_repository(
        windows, "patches/fk-chromium/windows-build-brand-assets.patch", "patch\n"
    )
    _init_repository(upstream_windows, "README", "wrapper\n", UPSTREAM_TAG)
    _init_repository(common, "README", "common\n")
    _init_repository(branding, "branding/manifest.json", "{}\n")
    return windows, upstream_windows, branding


def _release_identity_cli_arguments(
    command, raw_revision, output, build_repositories
):
    arguments = [
        sys.executable,
        str(ROOT / "tools" / "release_workflow.py"),
        command,
        "--upstream-tag",
        UPSTREAM_TAG,
        "--fk-revision",
        raw_revision,
        "--publish",
        "false",
    ]
    if command == "create-build-metadata":
        windows, upstream_windows, branding = build_repositories
        arguments.extend(
            (
                "--windows-repository",
                str(windows),
                "--upstream-windows-repository",
                str(upstream_windows),
                "--branding-repository",
                str(branding),
                "--force-rebuild",
                "false",
            )
        )
    else:
        arguments.extend(("--windows-commit", WINDOWS_COMMIT))
    arguments.extend(("--output", str(output)))
    return arguments


@pytest.mark.parametrize(
    "raw_revision",
    ("02", "+2", "-2", " 2", "2 ", "٢", "２", "", "0", "1000001"),
    ids=(
        "leading-zero",
        "plus-sign",
        "minus-sign",
        "leading-space",
        "trailing-space",
        "arabic-indic",
        "fullwidth",
        "empty",
        "zero",
        "over-bound",
    ),
)
@pytest.mark.parametrize(
    "command", ("create-build-metadata", "create-failure-identity")
)
def test_release_identity_clis_reject_noncanonical_raw_fk_revisions_without_output(
    tmp_path, build_identity_cli_repositories, command, raw_revision
):
    """Integer coercion must not erase an untrusted revision token's spelling."""
    output = tmp_path / "identity.json"
    arguments = _release_identity_cli_arguments(
        command, raw_revision, output, build_identity_cli_repositories
    )
    expected_error = (
        f"release_workflow.py {command}: error: argument --fk-revision: "
        "FK revision must be "
        "a canonical ASCII positive decimal within the allowed range"
    )

    for existing_contents in (None, b"existing identity\n"):
        if existing_contents is not None:
            output.write_bytes(existing_contents)
        result = subprocess.run(arguments, capture_output=True, text=True)

        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr.splitlines()[-1] == expected_error
        assert len(result.stderr.encode("utf-8")) < 2048
        if raw_revision:
            assert raw_revision not in result.stderr
        if existing_contents is None:
            assert not output.exists()
        else:
            assert output.read_bytes() == existing_contents


@pytest.mark.parametrize(
    ("raw_revision", "expected_revision", "expected_release_tag"),
    (
        ("2", 2, "151.0.7922.173-fk.2"),
        ("1000000", 1_000_000, "151.0.7922.173-fk.1000000"),
    ),
)
@pytest.mark.parametrize(
    "command", ("create-build-metadata", "create-failure-identity")
)
def test_release_identity_clis_accept_canonical_raw_fk_revisions(
    tmp_path,
    build_identity_cli_repositories,
    command,
    raw_revision,
    expected_revision,
    expected_release_tag,
):
    """Canonical CLI values, including the maximum, must retain integer identity output."""
    output = tmp_path / "identity.json"
    result = subprocess.run(
        _release_identity_cli_arguments(
            command, raw_revision, output, build_identity_cli_repositories
        ),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    identity = json.loads(output.read_text(encoding="utf-8"))
    assert identity["fk_revision"] == expected_revision
    assert isinstance(identity["fk_revision"], int)
    assert identity["release_tag"] == expected_release_tag


def test_create_build_metadata_derives_every_commit_and_hash_from_exact_checkouts(tmp_path):
    """A caller-supplied provenance SHA must not reach failure reports or publication metadata."""
    windows = tmp_path / "windows"
    upstream_windows = tmp_path / "upstream-windows"
    common = upstream_windows / "ungoogled-chromium"
    branding = tmp_path / "branding"
    windows_sha = _init_repository(
        windows, "patches/fk-chromium/windows-build-brand-assets.patch", "patch\n"
    )
    upstream_windows_sha = _init_repository(upstream_windows, "README", "wrapper\n", UPSTREAM_TAG)
    upstream_sha = _init_repository(common, "README", "common\n")
    branding_sha = _init_repository(branding, "branding/manifest.json", "{}\n")

    metadata = create_build_metadata(
        windows_repository=windows,
        upstream_windows_repository=upstream_windows,
        branding_repository=branding,
        upstream_tag=UPSTREAM_TAG,
        fk_revision=2,
        force_rebuild=False,
        publish=False,
    )

    assert metadata == {
        "branding_commit": branding_sha,
        "fk_revision": 2,
        "force_rebuild": False,
        "manifest_sha256": hashlib.sha256(b"{}\n").hexdigest(),
        "publish": False,
        "release_tag": RELEASE_TAG,
        "upstream_commit": upstream_sha,
        "upstream_tag": UPSTREAM_TAG,
        "upstream_version": UPSTREAM_VERSION,
        "upstream_windows_commit": upstream_windows_sha,
        "windows_build_hook_patch_sha256": hashlib.sha256(b"patch\n").hexdigest(),
        "windows_commit": windows_sha,
    }


def test_publish_workflow_is_completed_build_x64_only_and_default_read_only():
    """Manual/tag/legacy architecture triggers must not bypass build metadata publish=false."""
    workflow = _workflow_text("publish-release.yml")

    assert "workflow_dispatch" not in workflow
    assert re.search(
        r"(?ms)^on:\n  workflow_run:\n    workflows:\n      - build-x64\n    types:\n      - completed$",
        workflow,
    )
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "build-x86" not in workflow
    assert "build-arm" not in workflow
    assert "winget" not in workflow.lower()
    assert "binaries" not in workflow.lower()
    assert "classify-publication" in workflow
    assert "publish=false" in workflow
    assert "if: steps.publication.outputs.publish == 'true'" in workflow


def test_publish_workflow_validates_exact_artifact_before_pinned_release_then_state():
    """Granting release/state writes before fresh run and payload verification must fail."""
    workflow = _workflow_text("publish-release.yml")

    assert "fk-chromium-windows-x64" in workflow
    assert "path: artifacts/x64" in workflow
    assert workflow.count("merge-multiple: true") == 2
    assert "python tools/release_metadata.py verify artifacts/x64" in workflow
    assert "python tools/release_workflow.py classify-publication" in workflow
    assert "softprops/action-gh-release" not in workflow
    assert "python tools/release_workflow.py publish-release" in workflow
    assert "python tools/release_workflow.py validate-state" in workflow
    assert "python tools/release_workflow.py update-state" in workflow
    verification = workflow.index("python tools/release_metadata.py verify artifacts/x64")
    authorization = workflow.index("python tools/release_workflow.py validate-state")
    publication = workflow.index("python tools/release_workflow.py publish-release")
    state_update = workflow.index("python tools/release_workflow.py update-state")
    assert verification < authorization < publication < state_update
    assert workflow.count("contents: write") == 1
    assert "artifact-ids: ${{ needs.validate.outputs.artifact_id }}" in workflow
    assert "--expected-run-attempt \"$RUN_ATTEMPT\"" in workflow
    assert "--expected-artifact-id \"$ARTIFACT_ID\"" in workflow
    assert "group: publish-release-${{ needs.validate.outputs.release_tag }}" in workflow
    assert "--artifact-directory artifacts/x64" in workflow
    _assert_external_actions_are_pinned(workflow)


def test_publisher_emits_sanitized_attempt_identity_before_any_write_capability():
    """A failed publish=true attempt must leave a reporter artifact with exact source identity."""
    workflow = _workflow_text("publish-release.yml")

    assert "fk-chromium-publication-attempt" in workflow
    assert "create-publication-attempt" in workflow
    assert "github.run_id" in workflow
    assert "github.run_attempt" in workflow
    assert "steps.context.outputs.artifact_id" in workflow
    validate = workflow[workflow.index("  validate:") : workflow.index("  publish:")]
    assert "actions/upload-artifact@" in validate
    assert "contents: write" not in validate
    assert "issues: write" not in validate


def test_failure_workflow_validates_failure_before_issues_write_and_searches_all_states():
    """Cancelled/success runs and closed issues must not create duplicate failure reports."""
    workflow = _workflow_text("report-failure.yml")

    assert "workflow_dispatch" not in workflow
    assert "github.event.workflow_run.conclusion == 'failure'" in workflow
    assert "      - publish-release" in workflow
    assert re.search(r"(?m)^concurrency:$", workflow) is None
    assert "group: report-failure-${{ needs.validate.outputs.version }}" in workflow
    for artifact_name in (
        "fk-chromium-windows-build-metadata",
        "fk-chromium-windows-build-identity",
        "fk-chromium-windows-build-locator",
    ):
        assert artifact_name in workflow
    assert "fk-chromium-publication-attempt" in workflow
    assert workflow.count("merge-multiple: true") == 2
    assert "artifact-ids: ${{ needs.validate.outputs.artifact_id }}" in workflow
    assert "/attempts/$RUN_ATTEMPT/jobs?per_page=100" in workflow
    assert "filter=latest" not in workflow
    assert "python tools/release_workflow.py classify-failure" in workflow
    assert "python tools/release_workflow.py classify-publication-failure" in workflow
    assert "python tools/release_workflow.py report-failure" in workflow
    validate = workflow[workflow.index("  validate:") : workflow.index("  report:")]
    report = workflow[workflow.index("  report:") :]
    assert "classify-failure" in validate
    assert "issues: write" not in validate
    assert "    needs: validate" in report
    assert "issues: write" in report
    assert workflow.count("issues: write") == 1
    assert "state=all" in (ROOT / "tools" / "release_workflow.py").read_text(encoding="utf-8")
    assert "fk-build-failure" in workflow
    assert "printenv" not in workflow
    assert "env |" not in workflow
    _assert_external_actions_are_pinned(workflow)


def test_publication_failure_report_is_attempt_and_artifact_specific_without_logs():
    """A publisher failure must authorize one exact attempt marker without log or secret input."""
    workflow = _workflow_text("report-failure.yml")

    for binding in (
        "publisher_run_id",
        "publisher_run_attempt",
        "source_run_id",
        "source_run_attempt",
        "source_artifact_id",
        "release_tag",
        "upstream_tag",
    ):
        assert binding in (ROOT / "tools" / "release_workflow.py").read_text(encoding="utf-8")
    assert "/logs" not in workflow
    assert "download-logs" not in workflow


def test_build_entrypoint_emits_one_early_validated_diagnostic_metadata_artifact():
    """A preparation/compiler failure must still carry the exact tuple needed for one safe Issue."""
    workflow = _workflow_text("build-x64.yml")

    for artifact_name in (
        "fk-chromium-windows-build-locator",
        "fk-chromium-windows-build-identity",
        "fk-chromium-windows-build-metadata",
    ):
        assert artifact_name in workflow
    assert "python fk-windows/tools/release_workflow.py create-build-metadata" in workflow
    assert "repository: ungoogled-software/ungoogled-chromium-windows" in workflow
    assert "submodules: recursive" in workflow
    assert "uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    assert workflow.index("create-build-metadata") < workflow.index("    uses: ./.github/workflows/reusable-build.yml")
    assert workflow.index("fk-chromium-windows-build-locator") < workflow.index("submodules: recursive")
    assert workflow.index("fk-chromium-windows-build-identity") < workflow.index("submodules: recursive")
    _assert_external_actions_are_pinned(workflow)
