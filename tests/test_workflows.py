import re
from pathlib import Path

from tools.check_upstream import ReleaseState, choose_candidate
from tools.release_metadata import package_artifacts, parse_upstream_tag


_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github" / "workflows"
_ACTIONS = _ROOT / ".github" / "actions"


def _read(path):
    return path.read_text(encoding="utf-8-sig")


def test_reusable_build_has_exactly_twelve_bounded_windows_stages():
    """Adding a 13th build job or an unbounded/non-Windows job must break the free-runner contract."""
    workflow = _read(_WORKFLOWS / "reusable-build.yml")
    jobs = re.findall(r"^  build-(\d+):$", workflow, flags=re.MULTILINE)

    assert jobs == [str(number) for number in range(1, 13)]
    assert workflow.count("    runs-on: windows-2022") == 12
    timeouts = [int(value) for value in re.findall(r"^    timeout-minutes: (\d+)$", workflow, flags=re.MULTILINE)]
    assert len(timeouts) == 12
    assert all(0 < timeout < 360 for timeout in timeouts)
    assert 'if [ "${{ needs.build-12.outputs.finished }}" != "true" ]; then' in workflow


def test_build_workflows_are_x64_only_and_read_only():
    """Restoring an architecture selector, legacy entrypoint, or write permission must fail."""
    reusable = _read(_WORKFLOWS / "reusable-build.yml")
    entrypoint = _read(_WORKFLOWS / "build-x64.yml")
    stage_action = _read(_ACTIONS / "stage" / "action.yml")
    stage_script = _read(_ACTIONS / "stage" / "index.js")

    assert not (_WORKFLOWS / "build-x86.yml").exists()
    assert not (_WORKFLOWS / "build-arm.yml").exists()
    for text in (reusable, entrypoint, stage_action, stage_script):
        assert not re.search(r"(?im)^\s*(x86|arm):", text)
        assert "--x86" not in text
        assert "--arm" not in text
    assert "permissions:\n  contents: read" in reusable
    assert "permissions:\n  contents: read" in entrypoint
    assert "fk-chromium-windows-x64" in stage_script


def test_build_identity_reads_the_real_ungoogled_chromium_gitlink():
    """Reading the nonexistent historical `chromium` path must not emit unresolved provenance."""
    entrypoint = _read(_WORKFLOWS / "build-x64.yml")

    assert "ls-tree HEAD ungoogled-chromium" in entrypoint
    assert "ls-tree HEAD chromium" not in entrypoint


def test_build_inputs_include_safe_nonpublishing_default_and_outputs():
    """Defaulting publish true or dropping reusable release outputs must break the dispatch boundary."""
    reusable = _read(_WORKFLOWS / "reusable-build.yml")
    entrypoint = _read(_WORKFLOWS / "build-x64.yml")

    for workflow in (reusable, entrypoint):
        assert re.search(r"(?ms)^\s{6}upstream_tag:\n.*?^\s{8}type: string$", workflow)
        assert re.search(r"(?ms)^\s{6}fk_revision:\n.*?^\s{8}type: number$", workflow)
        assert re.search(r"(?ms)^\s{6}force_rebuild:\n.*?^\s{8}type: boolean$", workflow)
        assert re.search(
            r"(?ms)^\s{6}publish:\n(?:(?!^\s{6}\w).)*?^\s{8}type: boolean$"
            r"(?:(?!^\s{6}\w).)*?^\s{8}default: false$",
            workflow,
        )

    assert "  workflow_call:" not in entrypoint
    assert re.search(r"(?m)^\s{6}finished:$", reusable)
    assert re.search(r"(?m)^\s{6}upstream_version:$", reusable)
    assert re.search(r"(?m)^\s{6}release_tag:$", reusable)
    assert re.search(r"(?m)^\s{6}publish:$", reusable)

    assert "concurrency:" not in reusable
    assert "group: fk-x64-${{ inputs.upstream_tag || github.ref_name }}" in entrypoint
    assert "cancel-in-progress: false" in entrypoint


def test_publish_gate_has_no_event_based_bypass():
    """Pushes must pass boolean false while callers retain an explicit publish choice."""
    entrypoint = _read(_WORKFLOWS / "build-x64.yml")

    assert "publish: ${{ inputs.publish || false }}" in entrypoint
    assert "publish: ${{ inputs.publish }}" not in entrypoint
    assert not re.search(r"publish:.*github\.event_name\s*==\s*['\"]push['\"]", entrypoint)


def test_entrypoint_defaults_revision_only_for_push_without_coercing_caller_zero():
    """An explicit zero must reach validation; only a push lacking inputs gets revision one."""
    entrypoint = _read(_WORKFLOWS / "build-x64.yml")

    assert "fk_revision: ${{ github.event_name == 'push' && 1 || inputs.fk_revision }}" in entrypoint
    assert "fk_revision: ${{ inputs.fk_revision || 1 }}" not in entrypoint

    # The other fallback expressions are type-safe: false remains false, while a
    # push supplies its tag because it has no inputs context.
    assert "force_rebuild: ${{ inputs.force_rebuild || false }}" in entrypoint
    assert "upstream_tag: ${{ inputs.upstream_tag || github.ref_name }}" in entrypoint


def test_each_stage_prepares_the_requested_upstream_and_preserves_resume_contract():
    """Dropping an upstream tag, short-circuit input, or resume flag from any stage must fail."""
    workflow = _read(_WORKFLOWS / "reusable-build.yml")

    assert workflow.count("uses: ./.github/actions/prepare-fk") == 12
    assert workflow.count("upstream_tag: ${{ inputs.upstream_tag }}") == 12
    assert workflow.count("upstream_ref: ${{ inputs.upstream_ref }}") == 12
    assert workflow.count("fk_revision: ${{ inputs.fk_revision }}") == 12
    assert workflow.count("force_rebuild: ${{ inputs.force_rebuild }}") == 12
    assert workflow.count("publish: ${{ inputs.publish }}") == 12
    assert workflow.count("from_artifact: false") == 1
    assert workflow.count("from_artifact: true") == 11
    assert workflow.count("finished: ${{ join(needs.*.outputs.finished) }}") == 11
    assert workflow.count("finished: false") == 1


def test_branding_commit_is_resolved_once_and_propagated_through_every_stage():
    """Resolving the branding default branch independently in any Windows stage must fail."""
    entrypoint = _read(_WORKFLOWS / "build-x64.yml")
    workflow = _read(_WORKFLOWS / "reusable-build.yml")
    action = _read(_ACTIONS / "prepare-fk" / "action.yml")

    assert "  resolve-branding:" in entrypoint
    assert "branding_commit: ${{ steps.branding.outputs.branding_commit }}" in entrypoint
    assert "branding_ref: ${{ needs.resolve-branding.outputs.branding_commit }}" in entrypoint
    assert "    needs: resolve-branding" in entrypoint
    assert workflow.count("branding_ref: ${{ inputs.branding_ref }}") == 12
    assert "  branding_ref:" in action
    assert "ref: ${{ inputs.branding_ref }}" in action
    assert "branding_commit:" in action


def test_upstream_wrapper_tag_is_resolved_once_and_pinned_across_all_stages():
    """A moving upstream tag must not resolve differently between Windows stages."""
    entrypoint = _read(_WORKFLOWS / "build-x64.yml")
    workflow = _read(_WORKFLOWS / "reusable-build.yml")
    action = _read(_ACTIONS / "prepare-fk" / "action.yml")

    assert "upstream_windows_commit: ${{ steps.upstream.outputs.upstream_windows_commit }}" in entrypoint
    assert "upstream_ref: ${{ needs.resolve-branding.outputs.upstream_windows_commit }}" in entrypoint
    assert "  upstream_ref:" in workflow
    assert workflow.count("upstream_ref: ${{ inputs.upstream_ref }}") == 12
    assert "  upstream_ref:" in action
    assert "ref: ${{ inputs.upstream_ref }}" in action
    assert "UPSTREAM_REF: ${{ inputs.upstream_ref }}" in action
    assert "upstream wrapper checkout does not match pinned SHA" in action
    assert "ref: ${{ inputs.upstream_tag }}" not in action


def test_prepare_action_materializes_sources_and_records_checkout_provenance():
    """Skipping a checkout/copy/preparation step or checkout SHA must break provenance."""
    action = _read(_ACTIONS / "prepare-fk" / "action.yml")

    assert action.count("uses: actions/checkout@") == 3
    assert "repository: ungoogled-software/ungoogled-chromium-windows" in action
    assert "ref: ${{ inputs.upstream_ref }}" in action
    assert "submodules: recursive" in action
    assert "repository: firekula/fk-chromium" in action
    assert "path: fk-windows" in action
    assert "path: upstream-windows" in action
    assert "path: fk-branding" in action
    assert "tools/prepare_upstream.py" in action
    assert "patches\\fk-chromium" in action
    assert "python tools/prepare_upstream.py" in action
    assert "C:\\ungoogled-chromium-windows" in action
    for field in (
        "windows_commit",
        "upstream_windows_commit",
        "upstream_commit",
        "branding_commit",
        "upstream_tag",
        "fk_revision",
        "force_rebuild",
        "publish",
        "upstream_version",
        "release_tag",
    ):
        assert field in action
    assert "fk-build-metadata.json" in action


def test_prepare_action_rejects_non_integer_fk_revision_before_conversion():
    """A fractional, zero, or negative FK revision must fail before integer conversion."""
    action = _read(_ACTIONS / "prepare-fk" / "action.yml")

    validation = action.index("$env:FK_REVISION -notmatch '^[1-9][0-9]*$'")
    conversion = action.index("[int]$env:FK_REVISION")
    assert validation < conversion


def test_prepare_action_uses_shared_canonical_tag_parser_before_recording_identity():
    """A duplicated shortened-tag regex must not validate a ref the package parser rejects."""
    action = _read(_ACTIONS / "prepare-fk" / "action.yml")

    assert "release_metadata.py parse-tag" in action
    validation = action.index("    - name: Validate exact upstream Windows tag")
    checkout = action.index("    - name: Checkout exact upstream Windows tag")
    assert validation < checkout
    assert "UPSTREAM_VERSION: ${{ steps.upstream_tag.outputs.upstream_version }}" in action
    assert "(?:-\\d+)?" not in action
    assert "ref: ${{ inputs.upstream_ref }}" in action


def test_final_artifact_keeps_exact_packages_and_carries_publish_metadata():
    """Uploading guessed package names or omitting gated build metadata must fail Task 8 handoff."""
    stage_script = _read(_ACTIONS / "stage" / "index.js")

    assert "JSON.parse(packageOutput)" in stage_script
    assert "new Set(packageFiles).size !== 3" in stage_script
    assert "FK-Chromium-*" not in stage_script
    assert "fk-build-metadata.json" in stage_script
    assert "packageList.push(metadataFile)" in stage_script
    assert "fk-chromium-windows-x64" in stage_script


def test_resume_artifact_preserves_and_validates_the_complete_build_identity():
    """Restoring source state without its origin tuple or before validating it must fail."""
    stage_script = _read(_ACTIONS / "stage" / "index.js")

    assert "resume-artifact" in stage_script
    assert "assertMatchingBuildIdentity" in stage_script
    assert "fk-build-metadata.json" in stage_script
    assert "'src', 'fk-build-metadata.json'" in stage_script
    validation = stage_script.index("assertMatchingBuildIdentity")
    restore = stage_script.index("fs.rename", validation)
    assert validation < restore


def test_finished_outputs_follow_successful_retriable_artifact_uploads():
    """Exhausting either upload budget must fail without reporting a staged result."""
    stage_script = _read(_ACTIONS / "stage" / "index.js")
    successful_start = stage_script.index("if (retCode === 0)")
    upload = stage_script.index("await uploadArtifactWithRetries", successful_start)
    finished = stage_script.index("core.setOutput('finished', true)", successful_start)
    timeout_start = stage_script.index("else if (retCode === BUILD_TIMEOUT_EXIT_CODE)")
    resume_upload = stage_script.index("await uploadArtifactWithRetries", timeout_start)
    unfinished = stage_script.index("core.setOutput('finished', false)", timeout_start)

    assert upload < finished
    assert resume_upload < unfinished
    assert "artifact.uploadArtifact" not in stage_script


def test_task6_actions_and_node_dependencies_are_immutable():
    """A floating action ref, caret dependency, or unlocked npm install must fail reproducibility."""
    task6_yaml = "\n".join(
        _read(path)
        for path in (
            _ACTIONS / "prepare-fk" / "action.yml",
            _WORKFLOWS / "build-x64.yml",
            _WORKFLOWS / "reusable-build.yml",
        )
    )
    external_uses = re.findall(r"(?m)^\s*uses: (actions/[^@\s]+)@([^\s]+)", task6_yaml)
    assert external_uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for _, ref in external_uses)

    package = _read(_ACTIONS / "stage" / "package.json")
    assert (_ACTIONS / "stage" / "package-lock.json").exists()
    assert not re.search(r'"[~^]', package)
    assert '"@actions/glob"' not in package
    prepare = _read(_ACTIONS / "prepare-fk" / "action.yml")
    assert "npm ci --ignore-scripts" in prepare
    assert "npm install" not in prepare


def test_every_build_checkout_disables_persisted_credentials():
    """Upstream build code must never inherit a repository credential helper."""
    import yaml

    for path in (
        _WORKFLOWS / "reusable-build.yml",
        _ACTIONS / "prepare-fk" / "action.yml",
    ):
        payload = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        if path.name == "reusable-build.yml":
            steps = [
                step
                for job in payload["jobs"].values()
                for step in job.get("steps", [])
            ]
        else:
            steps = payload["runs"]["steps"]
        checkouts = [
            step for step in steps
            if step.get("uses", "").startswith("actions/checkout@")
        ]
        assert checkouts
        assert all(
            step.get("with", {}).get("persist-credentials") == "false"
            for step in checkouts
        )


def test_check_upstream_has_serial_scheduled_and_typed_manual_entrypoints():
    """Changing the schedule, input types, or serialization must break duplicate prevention."""
    workflow = _read(_WORKFLOWS / "check-upstream.yml")

    assert "    - cron: '17 3 * * *'" in workflow
    assert (
        "Optional exact canonical stable tag; required for force_rebuild or rerelease; blank selects latest"
        in workflow
    )
    assert (
        "Retry only when this exact tag owns the lowest unresolved revision for its Chromium version"
        in workflow
    )
    assert (
        "Rejects while this Chromium version has an unresolved reservation; retry its exact tag first with force_rebuild"
        in workflow
    )
    assert re.search(
        r"(?ms)^      upstream_tag:\n(?:(?!^      \w).)*?^        type: string$"
        r"(?:(?!^      \w).)*?^        required: false$",
        workflow,
    )
    assert re.search(
        r"(?ms)^      rerelease:\n(?:(?!^      \w).)*?^        type: boolean$"
        r"(?:(?!^      \w).)*?^        default: false$",
        workflow,
    )
    assert "force_rebuild and rerelease are mutually exclusive" in workflow
    assert workflow.count("rerelease requires an exact upstream_tag") == 2
    assert workflow.count("force_rebuild requires an exact upstream_tag") == 2
    assert re.search(
        r"(?ms)^      force_rebuild:\n(?:(?!^      \w).)*?^        type: boolean$"
        r"(?:(?!^      \w).)*?^        default: false$",
        workflow,
    )
    assert "concurrency:\n  group: check-upstream\n  cancel-in-progress: false" in workflow


def test_check_upstream_exposes_the_detector_contract_outputs():
    """Dropping a candidate field must not dispatch a build with an incomplete identity tuple."""
    workflow = _read(_WORKFLOWS / "check-upstream.yml")

    for output in ("should_build", "upstream_tag", "upstream_version", "fk_revision"):
        assert workflow.count(f"      {output}: ${{{{ steps.") == 2


def test_check_upstream_keeps_write_permissions_in_the_transaction_job_only():
    """Moving write access into detection or workflow scope must break least privilege."""
    workflow = _read(_WORKFLOWS / "check-upstream.yml")

    assert workflow.count("permissions:\n  contents: read") == 1
    assert workflow.count("    permissions:\n      contents: read") == 1
    assert workflow.count(
        "    permissions:\n"
        "      actions: write\n"
        "      contents: write"
    ) == 1
    assert "issues: write" not in workflow


def test_check_upstream_rechecks_and_commits_attempt_before_dispatch():
    """Dispatching before a fresh-state attempt commit must not permit concurrent duplicate builds."""
    workflow = _read(_WORKFLOWS / "check-upstream.yml")
    transaction = workflow[workflow.index("  record-and-dispatch:") :]
    recheck_start = transaction.index("      - name: Recheck candidate and record attempt")
    commit_start = transaction.index("      - name: Commit recorded attempt")
    dispatch_start = transaction.index("      - name: Dispatch FK x64 build")
    recheck = transaction[recheck_start:commit_start]
    commit = transaction[commit_start:dispatch_start]
    dispatch = transaction[dispatch_start:]

    assert "    needs: detect" in transaction
    assert recheck_start < commit_start < dispatch_start
    assert "git pull --ff-only" in recheck
    assert "--record-attempt" in recheck
    assert "--rerelease" in recheck
    assert "git push" in commit
    assert "build-x64.yml" in dispatch
    for input_name in ("upstream_tag", "fk_revision", "force_rebuild", "publish"):
        assert input_name in dispatch
    assert "publish=true" in dispatch


def test_check_upstream_pins_every_external_action_to_a_full_sha():
    """Introducing a movable action ref must not let upstream code change detector behavior."""
    workflow = _read(_WORKFLOWS / "check-upstream.yml")
    external_uses = re.findall(r"(?m)^\s*uses: ([^./][^@\s]+)@([^\s]+)", workflow)

    assert external_uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for _, ref in external_uses)


def test_exact_windows_tag_flows_from_detector_through_build_and_package_discovery(tmp_path):
    """Shortening or reconstructing the real Windows tag at any layer must break the build contract."""
    upstream_tag = "151.0.7922.173-1.1"
    candidate = choose_candidate([upstream_tag], ReleaseState(None, ()), force=False)
    assert candidate.upstream_tag == upstream_tag

    entrypoint = _read(_WORKFLOWS / "build-x64.yml")
    reusable = _read(_WORKFLOWS / "reusable-build.yml")
    prepare = _read(_ACTIONS / "prepare-fk" / "action.yml")
    assert "upstream_tag: ${{ inputs.upstream_tag || github.ref_name }}" in entrypoint
    assert reusable.count("upstream_tag: ${{ inputs.upstream_tag }}") == 12
    assert "ref: ${{ inputs.upstream_ref }}" in prepare
    assert "release_metadata.py parse-tag" in prepare

    parsed = parse_upstream_tag(candidate.upstream_tag)
    assert parsed.tag == upstream_tag
    installer = tmp_path / f"ungoogled-chromium_{upstream_tag}_installer_x64.exe"
    portable = tmp_path / f"ungoogled-chromium_{upstream_tag}_windows_x64.zip"
    installer.write_bytes(b"installer")
    portable.write_bytes(b"portable")

    package_artifacts(tmp_path, candidate.upstream_tag)

    assert not installer.exists()
    assert not portable.exists()
    assert (tmp_path / "FK-Chromium-151.0.7922.173-Windows-x64-Installer.exe").is_file()
