import json
from email.message import Message
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, addinfourl, build_opener

import pytest

from tools.check_upstream import (
    Candidate,
    _NoRedirectHandler,
    _normalize_tags_url,
    ReleaseState,
    choose_candidate,
    fetch_tag_names,
    record_attempt,
    record_success,
)


_ROOT = Path(__file__).resolve().parents[1]


def _success(tag="151.0.7922.173-1.1", revision=1):
    return Candidate(
        upstream_tag=tag,
        upstream_version=tag.rsplit("-", 1)[0],
        fk_revision=revision,
    )


def test_unsorted_tags_choose_the_latest_numeric_stable_release():
    """Lexical sorting, input ordering, or accepting a prerelease must not select the wrong tag."""
    candidate = choose_candidate(
        [
            "151.0.7922.99-9",
            "152.0.8000.1-beta",
            "151.0.7922.173-1.1",
            "151.0.7922.100-2.1",
            "151.0.7922.173-9.99",
            "151.0.7922.173-10.1",
            "151.0.7922.173-10.10",
            "151.0.7922.173",
            "151.0.7922.173-1",
        ],
        ReleaseState(last_success=None, attempted=()),
        force=False,
    )

    assert candidate == Candidate(
        upstream_tag="151.0.7922.173-10.10",
        upstream_version="151.0.7922.173",
        fk_revision=1,
    )


@pytest.mark.parametrize(
    "alias",
    (
        "0151.0.7922.173-1.1",
        "151.00.7922.173-1.1",
        "١٥١.٠.٧٩٢٢.١٧٣-1.1",
        "１５１.０.７９２２.１７３-1.1",
    ),
)
@pytest.mark.parametrize("reverse", (False, True))
def test_mixed_numeric_spellings_select_only_the_canonical_tag(alias, reverse):
    """Input ordering must not let a numeric alias win candidate selection."""
    canonical = "151.0.7922.173-1.1"
    tags = [alias, canonical]
    if reverse:
        tags.reverse()

    candidate = choose_candidate(tags, ReleaseState(None, ()), force=False)

    assert candidate == Candidate(canonical, "151.0.7922.173", 1)


@pytest.mark.parametrize(
    "tag",
    (
        "151.0.7922.173",
        "151.0.7922.173-0",
        "151.0.7922.173-1",
        "151.0.7922.173-0.1",
        "151.0.7922.173-1.0",
        "151.0.7922.173-beta",
        "151.0.7922.173-1.1.1",
        "v151.0.7922.173-1",
    ),
)
def test_non_stable_or_revisionless_tags_are_ignored(tag):
    """Relaxing the stable upstream tag grammar must not turn unsupported refs into builds."""
    assert choose_candidate(
        [tag], ReleaseState(last_success=None, attempted=()), force=False
    ) is None


def test_successful_tag_is_not_selected_twice_without_force():
    """Removing the successful-release guard must cause an already published tag to rebuild."""
    success = _success()

    assert choose_candidate(
        [success.upstream_tag],
        ReleaseState(last_success=success, attempted=(success.upstream_tag,)),
        force=False,
    ) is None


def test_failed_version_is_not_retried_without_force():
    """Removing the attempted-tag guard must cause a failed daily build to loop forever."""
    state = ReleaseState(last_success=None, attempted=("151.0.7922.173-1.1",))

    assert choose_candidate(["151.0.7922.173-1.1"], state, force=False) is None


def test_attempted_latest_tag_does_not_fall_back_to_an_older_release():
    """Filtering attempts before sorting must not dispatch a stale release as a fallback."""
    state = ReleaseState(last_success=None, attempted=("151.0.7922.173-1.1",))

    assert choose_candidate(
        ["150.0.7800.1-1.1", "151.0.7922.173-1.1"], state, force=False
    ) is None


def test_force_rejects_legacy_attempt_without_explicit_assignment():
    """An ambiguous old attempt must never be given an invented public identity."""
    state = ReleaseState(last_success=None, attempted=("151.0.7922.173-1.1",))

    assert dict(state.assignments) == {}
    with pytest.raises(ValueError, match="no unresolved reservation"):
        choose_candidate(["151.0.7922.173-1.1"], state, force=True)


def test_force_retries_an_explicit_attempted_unresolved_assignment():
    """Force retries the exact identity persisted before a failed dispatch."""
    state = ReleaseState(
        last_success=None,
        attempted=("151.0.7922.173-1.1",),
        assignments={"151.0.7922.173-1.1": 1},
    )

    assert choose_candidate(
        ["151.0.7922.173-1.1"], state, force=True
    ) == _success()


def test_direct_state_rejects_unattempted_unresolved_assignment():
    """Direct construction cannot bypass the exact attempted-tag reservation invariant."""
    with pytest.raises(ValueError, match="unresolved assignments require an exact attempt"):
        ReleaseState(
            last_success=None,
            attempted=(),
            assignments={"151.0.7922.173-1.1": 1},
        )


def test_post_success_force_rejects_instead_of_reusing_the_published_pair():
    """Force is only a failed-attempt retry and must not recreate a published release."""
    success = _success()
    state = ReleaseState(
        last_success=success,
        attempted=(success.upstream_tag,),
        revisions={success.upstream_version: 1},
    )

    with pytest.raises(ValueError, match="no unresolved reservation"):
        choose_candidate([success.upstream_tag], state, force=True, rerelease=False)
    assert choose_candidate(
        [success.upstream_tag], state, force=False, rerelease=True
    ) == Candidate(success.upstream_tag, success.upstream_version, 2)


def test_force_rejects_a_never_attempted_tag_without_an_assignment():
    """Force must not allocate a fresh identity when no failed attempt was reserved."""
    state = ReleaseState(last_success=None, attempted=())

    with pytest.raises(ValueError, match="no unresolved reservation"):
        choose_candidate(["151.0.7922.173-1.1"], state, force=True)


def test_force_retries_only_the_lowest_unresolved_same_tag_reservation():
    """Force must retry fk.2 before fk.3 even when both belong to the exact same tag."""
    first = _success()
    state = ReleaseState(
        last_success=first,
        attempted=(first.upstream_tag,) * 3,
        revisions={first.upstream_version: 1},
        assignments={first.upstream_tag: 1},
        successes={first.upstream_tag: 1},
        rerelease_assignments={first.upstream_tag: (2, 3)},
        rerelease_successes={},
    )

    assert choose_candidate([first.upstream_tag], state, force=True) == Candidate(
        first.upstream_tag, first.upstream_version, 2
    )


def test_force_rejects_when_every_reserved_pair_is_successful():
    """Force must not fall back when initial and rerelease reservations all succeeded."""
    first = _success()
    state = ReleaseState(
        last_success=Candidate(first.upstream_tag, first.upstream_version, 2),
        attempted=(first.upstream_tag, first.upstream_tag),
        revisions={first.upstream_version: 2},
        assignments={first.upstream_tag: 1},
        successes={first.upstream_tag: 1},
        rerelease_assignments={first.upstream_tag: (2,)},
        rerelease_successes={first.upstream_tag: (2,)},
    )

    with pytest.raises(ValueError, match="no unresolved reservation"):
        choose_candidate([first.upstream_tag], state, force=True)


def test_rerelease_pair_history_round_trips_and_force_retries_latest_reservation():
    """Serializing a failed re-release must retain both exact pairs and retry fk.2."""
    first = _success()
    state = record_success(record_attempt(ReleaseState(None, ()), first), first)
    second = choose_candidate(
        [first.upstream_tag], state, force=False, rerelease=True
    )

    attempted = record_attempt(state, second)
    restored = ReleaseState.from_dict(attempted.to_dict())

    assert restored.to_dict()["rerelease_assignments"] == {
        first.upstream_tag: [2]
    }
    assert restored.to_dict()["rerelease_successes"] == {}
    assert choose_candidate(
        [first.upstream_tag], restored, force=True, rerelease=False
    ) == second
    published = record_success(restored, second)
    assert published.to_dict()["rerelease_successes"] == {
        first.upstream_tag: [2]
    }
    assert published.revisions[first.upstream_version] == 2
    assert ReleaseState.from_dict(published.to_dict()) == published


def test_rerelease_rejects_an_unresolved_rerelease_reservation_without_mutation():
    """A failed fk.2 must be retried instead of allocating an unrecordable fk.3."""
    first = _success()
    published = record_success(record_attempt(ReleaseState(None, ()), first), first)
    second = choose_candidate(
        [first.upstream_tag], published, force=False, rerelease=True
    )
    unresolved = record_attempt(published, second)
    before = unresolved.to_dict()
    serialized_before = json.dumps(before, sort_keys=True).encode("utf-8")

    with pytest.raises(ValueError, match="unresolved reservation.*force_rebuild"):
        choose_candidate(
            [first.upstream_tag], unresolved, force=False, rerelease=True
        )

    assert unresolved.to_dict() == before
    assert (
        json.dumps(unresolved.to_dict(), sort_keys=True).encode("utf-8")
        == serialized_before
    )
    assert unresolved.rerelease_assignments[first.upstream_tag] == (2,)


def test_rerelease_rejects_an_unresolved_initial_reservation():
    """An initial failed reservation must remain on the explicit force-retry path."""
    first = _success()
    unresolved = record_attempt(ReleaseState(None, ()), first)

    with pytest.raises(ValueError, match="unresolved reservation.*force_rebuild"):
        choose_candidate(
            [first.upstream_tag], unresolved, force=False, rerelease=True
        )


def test_resolved_rerelease_allows_the_next_serial_revision():
    """After force-retrying and publishing fk.2, rerelease may reserve/publish fk.3."""
    first = _success()
    state = record_success(record_attempt(ReleaseState(None, ()), first), first)
    second = choose_candidate([first.upstream_tag], state, force=False, rerelease=True)
    state = record_attempt(state, second)

    assert choose_candidate([first.upstream_tag], state, force=True) == second

    state = record_success(state, second)
    third = choose_candidate([first.upstream_tag], state, force=False, rerelease=True)
    assert third == Candidate(first.upstream_tag, first.upstream_version, 3)
    state = record_attempt(state, third)
    state = record_success(state, third)

    assert state.rerelease_successes[first.upstream_tag] == (2, 3)
    assert state.revisions[first.upstream_version] == 3


def test_rerelease_cli_requires_one_exact_operator_supplied_upstream_tag(tmp_path):
    """Implicit latest-tag selection must not choose a post-success re-release target."""
    state_path = tmp_path / "release-state.json"
    first = _success()
    state = record_success(record_attempt(ReleaseState(None, ()), first), first)
    state_path.write_text(json.dumps(state.to_dict()), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "tools" / "check_upstream.py"),
            "--tags-file",
            str(_ROOT / "tests" / "fixtures" / "tags.json"),
            "--state",
            str(state_path),
            "--rerelease",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--rerelease requires --upstream-tag" in result.stderr


def test_force_cli_requires_one_exact_operator_supplied_upstream_tag(tmp_path):
    """A forced retry must not infer a target from the moving upstream tag list."""
    state_path = tmp_path / "release-state.json"
    state_path.write_text(
        json.dumps(ReleaseState(last_success=None, attempted=()).to_dict()),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "tools" / "check_upstream.py"),
            "--tags-file",
            str(_ROOT / "tests" / "fixtures" / "tags.json"),
            "--state",
            str(state_path),
            "--force",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "--force requires --upstream-tag" in result.stderr


def test_force_cli_help_describes_the_version_wide_lowest_retry_rule():
    """Operators must not be told force selects a later unresolved reservation."""
    result = subprocess.run(
        [sys.executable, str(_ROOT / "tools" / "check_upstream.py"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert (
        "globally lowest unresolved reservation for its Chromium version"
        in " ".join(result.stdout.split())
    )


@pytest.mark.parametrize("revision", (0, -1, 1_000_001))
def test_state_rejects_nonpositive_or_oversized_rerelease_revisions(revision):
    """Malformed or unbounded state revisions must not reach public tag allocation."""
    value = {
        "last_success": _success().to_dict(),
        "attempted": [_success().upstream_tag],
        "assignments": {_success().upstream_tag: 1},
        "successes": {_success().upstream_tag: 1},
        "revisions": {_success().upstream_version: 1},
        "rerelease_assignments": {_success().upstream_tag: [revision]},
        "rerelease_successes": {},
    }

    with pytest.raises(ValueError):
        ReleaseState.from_dict(value)


def test_state_rejects_a_rerelease_that_duplicates_the_initial_exact_pair():
    """Schema evolution must not represent the same tag/revision pair twice."""
    first = _success()
    value = {
        "last_success": first.to_dict(),
        "attempted": [first.upstream_tag, first.upstream_tag],
        "assignments": {first.upstream_tag: 1},
        "successes": {first.upstream_tag: 1},
        "revisions": {first.upstream_version: 1},
        "rerelease_assignments": {first.upstream_tag: [1]},
        "rerelease_successes": {first.upstream_tag: [1]},
    }

    with pytest.raises(ValueError, match="reuse an FK revision"):
        ReleaseState.from_dict(value)


def test_new_upstream_revision_of_released_chromium_increments_fk_revision():
    """Resetting the FK revision for the same Chromium version must not collide with an existing tag."""
    candidate = choose_candidate(
        ["151.0.7922.173-2.1"],
        ReleaseState(
            last_success=_success(tag="151.0.7922.173-1.1", revision=2),
            attempted=("151.0.7922.173-1.1",),
            revisions={"151.0.7922.173": 2},
        ),
        force=False,
    )

    assert candidate == Candidate(
        upstream_tag="151.0.7922.173-2.1",
        upstream_version="151.0.7922.173",
        fk_revision=3,
    )


def test_older_release_is_not_selected_after_a_newer_success_without_force():
    """Losing the successful-version floor must not automatically publish a downgrade."""
    assert choose_candidate(
        ["150.0.7800.1-1.1"],
        ReleaseState(last_success=_success(), attempted=()),
        force=False,
    ) is None


def test_record_attempt_returns_new_state_and_preserves_the_input_state():
    """Mutating shared state in the pure model must not hide an attempt from later serialization."""
    state = ReleaseState(
        last_success=_success(),
        attempted=("151.0.7922.173-1.1",),
        revisions={"151.0.7922.173": 1},
    )
    candidate = Candidate("152.0.8000.1-1.1", "152.0.8000.1", 1)

    updated = record_attempt(state, candidate)

    assert updated == ReleaseState(
        last_success=state.last_success,
        attempted=("151.0.7922.173-1.1", "152.0.8000.1-1.1"),
        revisions={"151.0.7922.173": 1},
        assignments={"152.0.8000.1-1.1": 1},
    )
    assert state.attempted == ("151.0.7922.173-1.1",)


def test_unresolved_initial_reservation_blocks_a_different_same_version_tag():
    """A failed initial reservation serializes all packaging tags for its Chromium version."""
    state = ReleaseState(last_success=None, attempted=())
    first = choose_candidate(["151.0.7922.173-1.1"], state, force=False)
    state = record_attempt(state, first)
    before = state.to_dict()

    second = choose_candidate(["151.0.7922.173-2.1"], state, force=False)

    assert first.fk_revision == 1
    assert second is None
    assert state.to_dict() == before
    assert dict(state.assignments) == {"151.0.7922.173-1.1": 1}


def test_unresolved_rerelease_blocks_normal_candidate_for_another_packaging_tag():
    """A failed fk.2 on tag A must not let tag B reserve fk.3 before state writeback."""
    first = Candidate("151.0.7922.173-1.1", "151.0.7922.173", 1)
    state = record_success(record_attempt(ReleaseState(None, ()), first), first)
    second = choose_candidate([first.upstream_tag], state, force=False, rerelease=True)
    state = record_attempt(state, second)
    before = state.to_dict()

    assert choose_candidate(["151.0.7922.173-2.1"], state, force=False) is None
    assert state.to_dict() == before


def test_unresolved_reservation_does_not_block_a_different_chromium_version():
    """Serialization is scoped to the Chromium four-part version, not the whole project."""
    failed = Candidate("151.0.7922.173-1.1", "151.0.7922.173", 1)
    state = record_attempt(ReleaseState(None, ()), failed)

    assert choose_candidate(["152.0.8000.1-1.1"], state, force=False) == Candidate(
        "152.0.8000.1-1.1", "152.0.8000.1", 1
    )


def test_same_version_force_retries_follow_one_cross_tag_revision_queue():
    """Publishing fk.4 before unresolved fk.3 must be rejected and leave state unchanged."""
    tag_a = "151.0.7922.173-2.1"
    tag_b = "151.0.7922.173-1.1"
    tag_c = "151.0.7922.173-3.1"
    state = ReleaseState(
        last_success=Candidate(tag_a, "151.0.7922.173", 2),
        attempted=(tag_a, tag_b),
        revisions={"151.0.7922.173": 2},
        assignments={tag_a: 1, tag_b: 4},
        successes={tag_a: 1},
        rerelease_assignments={tag_a: (2, 3)},
        rerelease_successes={tag_a: (2,)},
    )
    before = state.to_dict()

    assert state.unresolved_reservations("151.0.7922.173") == (
        (tag_a, 3),
        (tag_b, 4),
    )
    with pytest.raises(
        ValueError,
        match=r"retry 151\.0\.7922\.173-2\.1 fk\.3 first",
    ):
        choose_candidate([tag_b], state, force=True)
    assert state.to_dict() == before

    third = choose_candidate([tag_a], state, force=True)
    assert third == Candidate(tag_a, "151.0.7922.173", 3)
    state = record_success(state, third)

    fourth = choose_candidate([tag_b], state, force=True)
    assert fourth == Candidate(tag_b, "151.0.7922.173", 4)
    state = record_success(state, fourth)

    fifth = choose_candidate([tag_c], state, force=False)
    assert fifth == Candidate(tag_c, "151.0.7922.173", 5)
    state = record_success(record_attempt(state, fifth), fifth)
    assert state.successes[tag_b] == 4
    assert state.successes[tag_c] == 5
    assert state.revisions["151.0.7922.173"] == 5

    with pytest.raises(ValueError, match="no unresolved reservation"):
        choose_candidate([tag_a], state, force=True)


def test_resolving_version_blocker_resumes_at_version_high_water_plus_one():
    """After exact force success, another packaging tag receives the collision-free next revision."""
    tag_a = "151.0.7922.173-1.1"
    tag_b = "151.0.7922.173-2.1"
    first = Candidate(tag_a, "151.0.7922.173", 1)
    state = record_success(record_attempt(ReleaseState(None, ()), first), first)
    second = choose_candidate([tag_a], state, force=False, rerelease=True)
    state = record_attempt(state, second)

    state = record_success(state, choose_candidate([tag_a], state, force=True))
    third = choose_candidate([tag_b], state, force=False)

    assert third == Candidate(tag_b, "151.0.7922.173", 3)
    completed = record_success(record_attempt(state, third), third)
    assert completed.successes[tag_b] == 3
    assert completed.revisions["151.0.7922.173"] == 3


def test_record_attempt_rejects_direct_new_allocation_while_version_is_blocked():
    """Callers cannot bypass candidate selection and mutate a blocked version reservation."""
    failed = Candidate("151.0.7922.173-1.1", "151.0.7922.173", 1)
    state = record_attempt(ReleaseState(None, ()), failed)
    before = state.to_dict()

    with pytest.raises(ValueError, match="Chromium version.*unresolved reservation"):
        record_attempt(
            state,
            Candidate("151.0.7922.173-2.1", "151.0.7922.173", 2),
        )

    assert state.to_dict() == before


def test_forced_retry_reuses_the_exact_tags_reserved_revision():
    """Force must retry an assignment, not allocate another public release identity."""
    state = ReleaseState(
        last_success=None,
        attempted=("151.0.7922.173-1.1",),
        revisions={"151.0.7922.173": 2},
        assignments={"151.0.7922.173-1.1": 3},
    )

    candidate = choose_candidate(["151.0.7922.173-1.1"], state, force=True)

    assert candidate == Candidate("151.0.7922.173-1.1", "151.0.7922.173", 3)


def test_state_round_trip_preserves_success_revision_and_attempt_history():
    """Dropping the stored FK revision must not make the next same-version release collide."""
    state = ReleaseState(
        last_success=_success(revision=3),
        attempted=("151.0.7922.173-1.1", "151.0.7922.173-2.1"),
        revisions={"150.0.7800.1": 2, "151.0.7922.173": 3},
    )

    assert ReleaseState.from_dict(state.to_dict()) == state


@pytest.mark.parametrize(
    "value",
    (
        {},
        {"last_success": None, "attempted": "151.0.7922.173-1.1"},
        {"last_success": None, "attempted": [7]},
        {
            "last_success": {
                "upstream_tag": "151.0.7922.173-1.1",
                "upstream_version": "151.0.7922.173",
                "fk_revision": 0,
            },
            "attempted": [],
            "revisions": {"151.0.7922.173": 0},
        },
    ),
)
def test_state_rejects_incomplete_or_malformed_data(value):
    """Silently defaulting malformed state must not bypass the duplicate-attempt guard."""
    with pytest.raises(ValueError):
        ReleaseState.from_dict(value)


@pytest.mark.parametrize(
    "version",
    (
        "0151.0.7922.173",
        "151.00.7922.173",
        "١٥١.٠.٧٩٢٢.١٧٣",
        "１５１.０.７９２２.１７３",
    ),
)
def test_state_rejects_noncanonical_four_part_version_keys(version):
    """State high-water keys must not split one numeric version into alias queues."""
    with pytest.raises(ValueError):
        ReleaseState.from_dict(
            {
                "last_success": None,
                "attempted": [],
                "revisions": {version: 1},
            }
        )


def _rerelease_state_document_with_duplicate(field):
    tag = "151.0.7922.173-1.1"
    version = "151.0.7922.173"
    state = {
        "last_success": {
            "fk_revision": 1,
            "upstream_tag": tag,
            "upstream_version": version,
        },
        "attempted": [tag, tag],
        "assignments": {tag: 1},
        "revisions": {version: 1},
        "successes": {tag: 1},
        "rerelease_assignments": {tag: [2]},
        "rerelease_successes": {tag: []},
    }
    keys = {
        "assignments": tag,
        "revisions": version,
        "successes": tag,
        "rerelease_assignments": tag,
        "rerelease_successes": tag,
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
def test_detector_state_read_rejects_duplicate_nested_identity_keys(tmp_path, field):
    """Every nested identity map must reject a last-wins duplicate before selection."""
    state_path = tmp_path / "release-state.json"
    state_path.write_text(
        _rerelease_state_document_with_duplicate(field), encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "tools" / "check_upstream.py"),
            "--state",
            str(state_path),
            "--upstream-tag",
            "151.0.7922.173-1.1",
            "--force",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "duplicate object keys" in result.stderr


def test_detector_state_read_rejects_duplicate_top_level_assignment_history(tmp_path):
    """A later assignments member must not erase the lower reservation queue head."""
    tag_1 = "151.0.7922.173-1.1"
    tag_2 = "151.0.7922.173-2.1"
    tag_3 = "151.0.7922.173-3.1"
    state = {
        "last_success": {
            "fk_revision": 1,
            "upstream_tag": tag_1,
            "upstream_version": "151.0.7922.173",
        },
        "attempted": [tag_1, tag_2, tag_3],
        "assignments": {tag_1: 1, tag_3: 3},
        "revisions": {"151.0.7922.173": 1},
        "successes": {tag_1: 1},
    }
    compact = lambda value: json.dumps(value, separators=(",", ":"))
    document = compact(state)
    later = f'{compact("assignments")}:{compact(state["assignments"])}'
    duplicate = (
        f'{compact("assignments")}:{compact({tag_1: 1, tag_2: 2})},' + later
    )
    state_path = tmp_path / "release-state.json"
    state_path.write_text(document.replace(later, duplicate), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "tools" / "check_upstream.py"),
            "--state",
            str(state_path),
            "--upstream-tag",
            tag_3,
            "--force",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "duplicate object keys" in result.stderr


def test_detector_rejects_duplicate_keys_inside_irrelevant_nested_objects_first(tmp_path):
    """Duplicate detection must precede schema rejection without echoing hostile fields."""
    secret = "attacker-controlled-secret-field"
    document = (
        '{"last_success":null,"attempted":[],"irrelevant":'
        f'{{"{secret}":1,"{secret}":2}}}}'
    )
    state_path = tmp_path / "release-state.json"
    state_path.write_text(document, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "tools" / "check_upstream.py"),
            "--state",
            str(state_path),
            "--upstream-tag",
            "151.0.7922.173-1.1",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "duplicate object keys" in result.stderr
    assert secret not in result.stderr
    assert len(result.stderr) < 200


def test_fixture_cli_is_deterministic_and_read_only(tmp_path):
    """The offline detector command must not modify state unless recording is explicitly requested."""
    state_path = tmp_path / "release-state.json"
    state_path.write_text('{"last_success": null, "attempted": []}\n', encoding="utf-8")
    before = state_path.read_bytes()
    command = [
        sys.executable,
        str(_ROOT / "tools" / "check_upstream.py"),
        "--tags-file",
        str(_ROOT / "tests" / "fixtures" / "tags.json"),
        "--state",
        str(state_path),
    ]

    first = subprocess.run(command, check=True, capture_output=True, text=True)
    second = subprocess.run(command, check=True, capture_output=True, text=True)

    expected = {
        "fk_revision": 1,
        "upstream_tag": "151.0.7922.173-1.1",
        "upstream_version": "151.0.7922.173",
    }
    assert json.loads(first.stdout) == expected
    assert first.stdout == second.stdout
    assert state_path.read_bytes() == before


def test_record_attempt_cli_updates_state_before_returning_candidate(tmp_path):
    """A recording invocation that only prints must not leave the dispatch guard unchanged."""
    state_path = tmp_path / "release-state.json"
    state_path.write_text('{"last_success": null, "attempted": []}\n', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "tools" / "check_upstream.py"),
            "--upstream-tag",
            "151.0.7922.173-1.1",
            "--state",
            str(state_path),
            "--record-attempt",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["upstream_tag"] == "151.0.7922.173-1.1"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "last_success": None,
        "attempted": ["151.0.7922.173-1.1"],
        "assignments": {"151.0.7922.173-1.1": 1},
        "revisions": {},
        "successes": {},
    }


def test_explicit_cli_reports_version_blocker_without_mutating_state(tmp_path):
    """An operator-supplied tag receives a clear error instead of a silent new reservation."""
    state_path = tmp_path / "release-state.json"
    state_path.write_text('{"last_success": null, "attempted": []}\n', encoding="utf-8")
    command = [
        sys.executable,
        str(_ROOT / "tools" / "check_upstream.py"),
        "--state",
        str(state_path),
        "--record-attempt",
    ]

    first = subprocess.run(
        command + ["--upstream-tag", "151.0.7922.173-1.1"],
        check=True,
        capture_output=True,
        text=True,
    )
    before = state_path.read_bytes()
    second = subprocess.run(
        command + ["--upstream-tag", "151.0.7922.173-2.1"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert json.loads(first.stdout)["fk_revision"] == 1
    assert second.returncode == 1
    assert "Chromium version 151.0.7922.173 has an unresolved reservation" in second.stderr
    assert "force_rebuild" in second.stderr
    assert state_path.read_bytes() == before
    assert json.loads(state_path.read_text(encoding="utf-8"))["assignments"] == {
        "151.0.7922.173-1.1": 1,
    }


def test_force_rejects_ambiguous_older_attempt_even_with_revision_history():
    """A high-water mark cannot identify which revision an old attempt reserved."""
    state = ReleaseState(
        last_success=Candidate("152.0.8000.1-1.1", "152.0.8000.1", 1),
        attempted=("150.0.7800.1-1.1", "152.0.8000.1-1.1"),
        revisions={"150.0.7800.1": 2, "152.0.8000.1": 1},
    )

    with pytest.raises(ValueError, match="no unresolved reservation"):
        choose_candidate(["150.0.7800.1-1.1"], state, force=True)


def test_legacy_state_derives_revision_history_from_last_success():
    """Requiring the new field immediately must not break the committed pre-migration state."""
    legacy = {
        "last_success": _success(revision=3).to_dict(),
        "attempted": ["151.0.7922.173-1.1"],
    }

    state = ReleaseState.from_dict(legacy)

    assert dict(state.revisions) == {"151.0.7922.173": 3}
    assert dict(state.assignments) == {"151.0.7922.173-1.1": 3}
    assert dict(state.successes) == {"151.0.7922.173-1.1": 3}
    assert state.to_dict()["revisions"] == {"151.0.7922.173": 3}


def test_revisions_only_state_does_not_assign_unidentified_attempts():
    """Migration must preserve an unknown old attempt without inventing its revision."""
    state = ReleaseState.from_dict(
        {
            "last_success": _success(tag="151.0.7922.173-2.1", revision=2).to_dict(),
            "attempted": ["151.0.7922.173-1.1", "151.0.7922.173-2.1"],
            "revisions": {"151.0.7922.173": 2},
        }
    )

    assert dict(state.assignments) == {"151.0.7922.173-2.1": 2}
    with pytest.raises(ValueError, match="no unresolved reservation"):
        choose_candidate(["151.0.7922.173-1.1"], state, force=True)


def test_legacy_migration_leaves_all_unknown_attempts_unassigned():
    """Only the known success may recover an identity from old state."""
    state = ReleaseState.from_dict(
        {
            "last_success": _success(tag="151.0.7922.173-2.1", revision=2).to_dict(),
            "attempted": [
                "151.0.7922.173-1.1",
                "151.0.7922.173-2.1",
                "151.0.7922.173-3.1",
            ],
            "revisions": {"151.0.7922.173": 2},
        }
    )

    assert dict(state.assignments) == {"151.0.7922.173-2.1": 2}
    assert dict(state.successes) == {"151.0.7922.173-2.1": 2}


def test_modern_json_rejects_unresolved_assignment_at_published_high_water():
    """An explicit reservation cannot claim the public revision recorded for an unknown release."""
    with pytest.raises(ValueError, match="published FK revision"):
        ReleaseState.from_dict(
            {
                "last_success": None,
                "attempted": ["151.0.7922.173-1.1"],
                "revisions": {"151.0.7922.173": 1},
                "assignments": {"151.0.7922.173-1.1": 1},
                "successes": {},
            }
        )


def test_direct_state_rejects_unresolved_assignment_at_published_high_water():
    """Bypassing JSON construction must not bypass the published-identity collision guard."""
    with pytest.raises(ValueError, match="published FK revision"):
        ReleaseState(
            last_success=None,
            attempted=("151.0.7922.173-1.1",),
            revisions={"151.0.7922.173": 1},
            assignments={"151.0.7922.173-1.1": 1},
            successes={},
        )


def test_record_success_rejects_unassigned_legacy_attempt():
    """Task 8 cannot publish an identity that ambiguous old state never reserved."""
    state = ReleaseState.from_dict(
        {
            "last_success": None,
            "attempted": ["151.0.7922.173-1.1"],
            "revisions": {"151.0.7922.173": 1},
        }
    )

    assert dict(state.assignments) == {}
    with pytest.raises(ValueError, match="exact attempted assignment"):
        record_success(state, Candidate("151.0.7922.173-1.1", "151.0.7922.173", 1))


def test_explicit_revision_history_cannot_undercut_last_success():
    """Accepting a stale revision map must not reissue an already published FK tag."""
    with pytest.raises(ValueError):
        ReleaseState.from_dict(
            {
                "last_success": _success(revision=3).to_dict(),
                "attempted": ["151.0.7922.173-1.1"],
                "revisions": {"151.0.7922.173": 2},
            }
        )


def test_record_success_updates_last_success_and_highest_revision():
    """Task 8 must not need to hand-edit correlated state fields after publication."""
    state = ReleaseState(
        last_success=Candidate("152.0.8000.1-1.1", "152.0.8000.1", 1),
        attempted=("150.0.7800.1-2.1",),
        revisions={"150.0.7800.1": 2, "152.0.8000.1": 1},
        assignments={"150.0.7800.1-2.1": 3},
    )
    success = Candidate("150.0.7800.1-2.1", "150.0.7800.1", 3)

    updated = record_success(state, success)

    assert updated.last_success == success
    assert updated.attempted == state.attempted
    assert dict(updated.revisions) == {"150.0.7800.1": 3, "152.0.8000.1": 1}


def test_record_success_rejects_a_different_release_at_an_existing_revision():
    """Task 8 must not record two upstream builds under the same public FK release tag."""
    state = ReleaseState(
        last_success=_success(tag="151.0.7922.173-1.1", revision=3),
        attempted=("151.0.7922.173-1.1", "151.0.7922.173-2.1"),
        revisions={"151.0.7922.173": 3},
    )
    collision = _success(tag="151.0.7922.173-2.1", revision=3)

    with pytest.raises(ValueError):
        record_success(state, collision)


def test_historical_success_replay_after_newer_success_returns_state_unchanged():
    """Task 8 replay of an exact older publication must be idempotent, not an error or mutation."""
    first = Candidate("151.0.7922.173-1.1", "151.0.7922.173", 1)
    second = Candidate("151.0.7922.173-2.1", "151.0.7922.173", 2)
    state = ReleaseState(None, ())
    state = record_attempt(state, first)
    state = record_success(state, first)
    state = record_attempt(state, second)
    state = record_success(state, second)

    replayed = record_success(state, first)

    assert replayed is state
    assert replayed.last_success == second


def test_non_force_skips_historical_success_missing_from_attempt_history():
    """A regressed last_success and incomplete legacy attempts must not redispatch a publication."""
    state = ReleaseState(
        last_success=Candidate("150.0.7800.1-1.1", "150.0.7800.1", 1),
        attempted=("150.0.7800.1-1.1",),
        revisions={"150.0.7800.1": 1, "151.0.7922.173": 1},
        assignments={
            "150.0.7800.1-1.1": 1,
            "151.0.7922.173-1.1": 1,
        },
        successes={
            "150.0.7800.1-1.1": 1,
            "151.0.7922.173-1.1": 1,
        },
    )
    state = ReleaseState.from_dict(state.to_dict())

    candidate = choose_candidate(["151.0.7922.173-1.1"], state, force=False)

    assert "151.0.7922.173-1.1" not in state.attempted
    assert candidate is None


def test_assignment_and_success_history_round_trip_without_identity_loss():
    """Serialization must retain exact reserved and published identities for Task 8 replay."""
    state = ReleaseState(
        last_success=_success(tag="151.0.7922.173-1.1", revision=1),
        attempted=("151.0.7922.173-1.1", "151.0.7922.173-2.1"),
        revisions={"151.0.7922.173": 1},
        assignments={"151.0.7922.173-1.1": 1, "151.0.7922.173-2.1": 2},
        successes={"151.0.7922.173-1.1": 1},
    )

    restored = ReleaseState.from_dict(state.to_dict())

    assert restored == state
    assert dict(restored.assignments) == dict(state.assignments)
    assert dict(restored.successes) == dict(state.successes)


@pytest.mark.parametrize(
    "state",
    (
        {
            "last_success": None,
            "attempted": ["151.0.7922.173-1.1", "151.0.7922.173-2.1"],
            "revisions": {},
            "assignments": {"151.0.7922.173-1.1": 1, "151.0.7922.173-2.1": 1},
            "successes": {},
        },
        {
            "last_success": None,
            "attempted": ["151.0.7922.173-1.1"],
            "revisions": {"151.0.7922.173": 1},
            "assignments": {"151.0.7922.173-1.1": 1},
            "successes": {"151.0.7922.173-1.1": 2},
        },
        {
            "last_success": None,
            "attempted": ["151.0.7922.173-1.1"],
            "revisions": {},
            "assignments": {"151.0.7922.173-2.1": 1},
            "successes": {},
        },
        {
            "last_success": None,
            "attempted": [],
            "revisions": {},
            "assignments": {"151.0.7922.173-1.1": 1},
            "successes": {},
        },
    ),
)
def test_state_rejects_assignment_collisions_or_lost_identity(state):
    """Corrupt reservation/history data must fail closed before candidate allocation."""
    with pytest.raises(ValueError):
        ReleaseState.from_dict(state)


class _JsonResponse(BytesIO):
    def __init__(self, payload, link=None):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.headers = Message()
        if link is not None:
            self.headers["Link"] = link

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _SequenceOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def __call__(self, request, timeout):
        assert timeout == 30
        self.urls.append(request.full_url)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def _http_error(code, headers=None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return HTTPError("https://api.github.com/tags", code, "failure", message, None)


class _RedirectingTransport(HTTPSHandler):
    def __init__(self):
        super().__init__()
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        headers = Message()
        headers["Location"] = "https://attacker.invalid/collect"
        response = addinfourl(BytesIO(b""), headers, request.full_url, 302)
        response.msg = "Found"
        return response


def test_fetch_rejects_redirect_without_forwarding_authorization(monkeypatch):
    """A 3xx must not copy the GitHub token into a second cross-origin request."""
    monkeypatch.setenv("GITHUB_TOKEN", "offline-secret-token")
    transport = _RedirectingTransport()
    opener = build_opener(_NoRedirectHandler(), transport)

    with pytest.raises(ValueError, match="HTTP 302"):
        fetch_tag_names(opener=opener.open, sleep=lambda _delay: None)

    assert len(transport.requests) == 1
    assert transport.requests[0].full_url.startswith("https://api.github.com/")
    assert transport.requests[0].get_header("Authorization") == "Bearer offline-secret-token"


def test_paginated_fetch_aggregates_before_selecting_later_page_maximum():
    """Stopping after page one must not miss a numerically newer stable tag on a later page."""
    opener = _SequenceOpener(
        [
            _JsonResponse(
                [{"name": "151.0.7922.173-1.1"}],
                '<https://api.github.com/repos/ungoogled-software/ungoogled-chromium-windows/tags?per_page=100&page=2>; rel="next", '
                '<https://api.github.com/repos/ungoogled-software/ungoogled-chromium-windows/tags?per_page=100&page=2>; rel="last"',
            ),
            _JsonResponse([{"name": "152.0.8000.1-1.1"}]),
        ]
    )

    tags = fetch_tag_names(opener=opener, sleep=lambda _delay: None)
    candidate = choose_candidate(tags, ReleaseState(None, ()), force=False)

    assert candidate.upstream_tag == "152.0.8000.1-1.1"
    assert len(opener.urls) == 2


def test_paginated_fetch_allows_githubs_canonical_repository_id_tags_next_link():
    """GitHub may paginate this tags endpoint through its stable repository ID path."""
    opener = _SequenceOpener(
        [
            _JsonResponse(
                [{"name": "151.0.7922.173-1.1"}],
                '<https://api.github.com/repositories/177210827/tags?per_page=100&page=2>; rel="next"',
            ),
            _JsonResponse([{"name": "152.0.8000.1-1.1"}]),
        ]
    )

    assert fetch_tag_names(opener=opener, sleep=lambda _delay: None) == [
        "151.0.7922.173-1.1",
        "152.0.8000.1-1.1",
    ]
    assert opener.urls[1] == (
        "https://api.github.com/repos/ungoogled-software/"
        "ungoogled-chromium-windows/tags?per_page=100&page=2"
    )


@pytest.mark.parametrize(
    "url, message",
    (
        (
            "https://api.github.com/repositories/177210828/tags?per_page=100&page=2",
            "tags endpoint",
        ),
        (
            "https://api.github.com/repositories/177210827/branches?per_page=100&page=2",
            "tags endpoint",
        ),
        (
            "https://api.github.com/repositories/177210827/tags?per_page=100&page=2&state=open",
            "invalid query",
        ),
        (
            "https://api.github.com/repositories/177210827/tags?per_page=100&page=2#fragment",
            "tags endpoint",
        ),
    ),
)
def test_repository_id_pagination_rejects_noncanonical_urls(url, message):
    """The repository ID pagination alias must retain the canonical tags URL boundary."""
    with pytest.raises(ValueError, match=message):
        _normalize_tags_url(url)


@pytest.mark.parametrize(
    "link, message",
    (
        ("not-a-link", "Malformed GitHub Link"),
        ('<https://example.com/tags?page=2&per_page=100>; rel="next"', "origin"),
        (
            '<https://api.github.com/repos/ungoogled-software/ungoogled-chromium-windows/tags?per_page=100>; rel="next"',
            "loop",
        ),
    ),
)
def test_paginated_fetch_rejects_malformed_cross_origin_or_looping_next_links(link, message):
    """Trusting an unsafe next link must not leak authorization or loop indefinitely."""
    opener = _SequenceOpener([_JsonResponse([], link)])

    with pytest.raises(ValueError, match=message):
        fetch_tag_names(opener=opener, sleep=lambda _delay: None)


def test_paginated_fetch_stops_at_hard_page_bound():
    """Following an unlimited next chain must not consume unbounded API calls."""
    opener = _SequenceOpener(
        [
            _JsonResponse(
                [],
                '<https://api.github.com/repos/ungoogled-software/ungoogled-chromium-windows/tags?per_page=100&page=2>; rel="next"',
            )
        ]
    )

    with pytest.raises(ValueError, match="page limit"):
        fetch_tag_names(opener=opener, sleep=lambda _delay: None, max_pages=1)

    assert len(opener.urls) == 1


@pytest.mark.parametrize(
    "options",
    ({"max_pages": 21}, {"max_attempts": 4}),
)
def test_fetch_limits_cannot_exceed_production_safety_bounds(options):
    """A caller must not disable the hard page or retry bound with an oversized override."""
    opener = _SequenceOpener([])

    with pytest.raises(ValueError, match="safety bound"):
        fetch_tag_names(opener=opener, sleep=lambda _delay: None, **options)

    assert opener.urls == []


def test_fetch_retries_transient_rate_limit_then_succeeds_within_bound():
    """Treating a transient 429 as permanent must not skip a recoverable scheduled check."""
    sleeps = []
    opener = _SequenceOpener(
        [
            _http_error(429, {"Retry-After": "2"}),
            _JsonResponse([{"name": "151.0.7922.173-1.1"}]),
        ]
    )

    assert fetch_tag_names(opener=opener, sleep=sleeps.append) == ["151.0.7922.173-1.1"]
    assert sleeps == [2.0]
    assert len(opener.urls) == 2


def test_fetch_bounds_transient_server_retries_and_fails_closed():
    """Retrying 5xx forever or returning partial tags must not hide detector failure."""
    opener = _SequenceOpener([_http_error(503), _http_error(503), _http_error(503)])

    with pytest.raises(ValueError, match="HTTP 503.*3 attempts"):
        fetch_tag_names(opener=opener, sleep=lambda _delay: None, max_attempts=3)

    assert len(opener.urls) == 3


def test_fetch_reports_rate_limit_diagnostics_without_retrying_nontransient_403():
    """Dropping rate-limit headers must not leave scheduled failures unactionable."""
    opener = _SequenceOpener(
        [_http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1788278400"})]
    )

    with pytest.raises(
        ValueError, match=r"HTTP 403.*rate-limit remaining=0.*reset=1788278400"
    ):
        fetch_tag_names(opener=opener, sleep=lambda _delay: None)

    assert len(opener.urls) == 1
