"""Select one stable ungoogled Chromium Windows release for an FK build."""

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from .release_metadata import (
        CHROMIUM_VERSION_PATTERN,
        MAX_FK_REVISION,
        parse_upstream_tag,
    )
else:
    from release_metadata import (
        CHROMIUM_VERSION_PATTERN,
        MAX_FK_REVISION,
        parse_upstream_tag,
    )


_TAGS_URL = (
    "https://api.github.com/repos/ungoogled-software/"
    "ungoogled-chromium-windows/tags?per_page=100"
)
_TAGS_PATH = "/repos/ungoogled-software/ungoogled-chromium-windows/tags"
_TAGS_REPOSITORY_ID_PATH = "/repositories/177210827/tags"
_MAX_TAG_PAGES = 20
_MAX_PAGE_ATTEMPTS = 3


class _DuplicateReleaseStateKey(ValueError):
    pass


def _reject_duplicate_release_state_keys(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise _DuplicateReleaseStateKey
        value[key] = entry
    return value


def decode_release_state_json(document):
    """Decode release-state JSON while rejecting duplicate keys at every depth."""
    if not isinstance(document, str):
        raise ValueError("Release state JSON must be text")
    try:
        return json.loads(
            document, object_pairs_hook=_reject_duplicate_release_state_keys
        )
    except _DuplicateReleaseStateKey:
        raise ValueError("Release state JSON contains duplicate object keys") from None
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("Release state JSON is malformed") from None


class _NoRedirectHandler(HTTPRedirectHandler):
    """Fail closed because GitHub tag pagination is expressed through Link headers."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, "GitHub API redirect refused", headers, response)


_NO_REDIRECT_OPEN = build_opener(_NoRedirectHandler()).open


def _parse_stable_tag(tag: str):
    if not isinstance(tag, str):
        return None
    try:
        return parse_upstream_tag(tag)
    except ValueError:
        return None


@dataclass(frozen=True)
class Candidate:
    """An exact upstream tag and the FK release identity it should build."""

    upstream_tag: str
    upstream_version: str
    fk_revision: int

    def __post_init__(self):
        parsed = _parse_stable_tag(self.upstream_tag)
        if parsed is None or parsed.version != self.upstream_version:
            raise ValueError("Candidate must contain one consistent stable upstream tag")
        if (
            isinstance(self.fk_revision, bool)
            or not isinstance(self.fk_revision, int)
            or self.fk_revision < 1
            or self.fk_revision > MAX_FK_REVISION
        ):
            raise ValueError(
                f"Candidate FK revision must be between 1 and {MAX_FK_REVISION}"
            )

    def to_dict(self):
        return {
            "fk_revision": self.fk_revision,
            "upstream_tag": self.upstream_tag,
            "upstream_version": self.upstream_version,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {
            "fk_revision",
            "upstream_tag",
            "upstream_version",
        }:
            raise ValueError("last_success must be a complete candidate object or null")
        try:
            return cls(
                upstream_tag=value["upstream_tag"],
                upstream_version=value["upstream_version"],
                fk_revision=value["fk_revision"],
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid last_success candidate: {error}") from error


@dataclass(frozen=True)
class ReleaseState:
    """Published revisions plus exact reserved and successful release identities."""

    last_success: Candidate | None
    attempted: tuple[str, ...]
    revisions: Mapping[str, int] = field(default_factory=dict)
    assignments: Mapping[str, int] = field(default_factory=dict)
    successes: Mapping[str, int] = field(default_factory=dict)
    rerelease_assignments: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    rerelease_successes: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if self.last_success is not None and not isinstance(self.last_success, Candidate):
            raise ValueError("last_success must be a Candidate or null")
        if not isinstance(self.attempted, tuple):
            raise ValueError("attempted must be a tuple of stable upstream tags")
        if any(_parse_stable_tag(tag) is None for tag in self.attempted):
            raise ValueError("attempted must contain only stable upstream tags")
        if not isinstance(self.revisions, Mapping):
            raise ValueError("revisions must map Chromium versions to positive integers")
        revisions = dict(self.revisions)
        for version, revision in revisions.items():
            if (
                not isinstance(version, str)
                or CHROMIUM_VERSION_PATTERN.fullmatch(version) is None
            ):
                raise ValueError("revisions keys must be four-part Chromium versions")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or revision > MAX_FK_REVISION
            ):
                raise ValueError(
                    f"revisions values must be between 1 and {MAX_FK_REVISION}"
                )

        assignments = _validated_identity_map("assignments", self.assignments)
        successes = _validated_identity_map("successes", self.successes)
        rerelease_assignments = _validated_revision_lists(
            "rerelease_assignments", self.rerelease_assignments
        )
        rerelease_successes = _validated_revision_lists(
            "rerelease_successes", self.rerelease_successes
        )
        for tag, revision in successes.items():
            assigned = assignments.get(tag)
            if assigned is None:
                assignments[tag] = revision
            elif assigned != revision:
                raise ValueError("successes must match the exact reserved assignment")
            version = parse_upstream_tag(tag).version
            revisions[version] = max(revisions.get(version, 0), revision)

        if self.last_success is not None:
            version = self.last_success.upstream_version
            revisions[version] = max(revisions.get(version, 0), self.last_success.fk_revision)
            tag = self.last_success.upstream_tag
            for name, identities in (("assignments", assignments), ("successes", successes)):
                recorded = identities.get(tag)
                if recorded is None:
                    identities[tag] = self.last_success.fk_revision
                elif recorded != self.last_success.fk_revision:
                    if self.last_success.fk_revision not in (
                        rerelease_assignments.get(tag, ())
                        if name == "assignments"
                        else rerelease_successes.get(tag, ())
                    ):
                        raise ValueError(f"last_success must match its exact {name} identity")

        for tag, revisions_for_tag in rerelease_assignments.items():
            if tag not in assignments or successes.get(tag) is None:
                raise ValueError(
                    "rerelease assignments require an exact initial successful assignment"
                )
            successful = set(rerelease_successes.get(tag, ()))
            if not successful.issubset(revisions_for_tag):
                raise ValueError(
                    "rerelease successes must match exact rerelease assignments"
                )
            version = parse_upstream_tag(tag).version
            if successful:
                revisions[version] = max(revisions.get(version, 0), *successful)
        if set(rerelease_successes) - set(rerelease_assignments):
            raise ValueError("rerelease successes cannot lose their assignment history")

        attempted = set(self.attempted)
        for tag, revision in assignments.items():
            if tag not in attempted and successes.get(tag) != revision:
                raise ValueError("unresolved assignments require an exact attempt")
        for tag, values in rerelease_assignments.items():
            successful = set(rerelease_successes.get(tag, ()))
            if tag not in attempted and any(revision not in successful for revision in values):
                raise ValueError("unresolved assignments require an exact attempt")

        occupied = {}
        pairs = [
            (tag, revision, revision == successes.get(tag))
            for tag, revision in assignments.items()
        ]
        pairs.extend(
            (tag, revision, revision in rerelease_successes.get(tag, ()))
            for tag, values in rerelease_assignments.items()
            for revision in values
        )
        for tag, revision, is_success in pairs:
            version = parse_upstream_tag(tag).version
            identity = (version, revision)
            other = occupied.get(identity)
            if other is not None:
                raise ValueError("assignments cannot reuse an FK revision within a Chromium version")
            occupied[identity] = tag

            if revision <= revisions.get(version, 0) and not is_success:
                raise ValueError(
                    "An unresolved assignment cannot reuse the published FK revision"
                )

        object.__setattr__(self, "revisions", MappingProxyType(revisions))
        object.__setattr__(self, "assignments", MappingProxyType(assignments))
        object.__setattr__(self, "successes", MappingProxyType(successes))
        object.__setattr__(
            self, "rerelease_assignments", MappingProxyType(rerelease_assignments)
        )
        object.__setattr__(
            self, "rerelease_successes", MappingProxyType(rerelease_successes)
        )

    def to_dict(self):
        payload = {
            "last_success": None if self.last_success is None else self.last_success.to_dict(),
            "attempted": list(self.attempted),
            "assignments": dict(sorted(self.assignments.items())),
            "revisions": dict(sorted(self.revisions.items())),
            "successes": dict(sorted(self.successes.items())),
        }
        if self.rerelease_assignments or self.rerelease_successes:
            payload["rerelease_assignments"] = {
                tag: list(values)
                for tag, values in sorted(self.rerelease_assignments.items())
            }
            payload["rerelease_successes"] = {
                tag: list(values)
                for tag, values in sorted(self.rerelease_successes.items())
            }
        return payload

    @classmethod
    def from_dict(cls, value):
        required = {"last_success", "attempted"}
        optional = {
            "revisions", "assignments", "successes",
            "rerelease_assignments", "rerelease_successes",
        }
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
            raise ValueError(
                "State must contain last_success and attempted with only supported history fields"
            )
        attempted = value["attempted"]
        if not isinstance(attempted, list) or any(not isinstance(tag, str) for tag in attempted):
            raise ValueError("attempted must be a JSON array of strings")
        success_value = value["last_success"]
        success = None if success_value is None else Candidate.from_dict(success_value)
        revisions = value.get("revisions", {})
        if not isinstance(revisions, dict):
            raise ValueError("revisions must be a JSON object")
        assignments = value.get("assignments", {})
        successes = value.get("successes", {})
        rerelease_assignments = value.get("rerelease_assignments", {})
        rerelease_successes = value.get("rerelease_successes", {})
        if not isinstance(assignments, dict) or not isinstance(successes, dict):
            raise ValueError("assignments and successes must be JSON objects")
        if not isinstance(rerelease_assignments, dict) or not isinstance(
            rerelease_successes, dict
        ):
            raise ValueError("rerelease assignment and success history must be JSON objects")
        if ("rerelease_assignments" in value) != ("rerelease_successes" in value):
            raise ValueError("rerelease history fields must be recorded together")
        if "assignments" in value:
            known = set(attempted) | set(successes)
            if success is not None:
                known.add(success.upstream_tag)
            if set(assignments) - known:
                raise ValueError("assignments cannot contain an unattempted release identity")
        if "successes" in value and "assignments" in value:
            for tag, revision in successes.items():
                if assignments.get(tag) != revision:
                    raise ValueError("successes must match the exact reserved assignment")
        if success is not None and "revisions" in value:
            recorded = revisions.get(success.upstream_version)
            if (
                isinstance(recorded, bool)
                or not isinstance(recorded, int)
                or recorded < success.fk_revision
            ):
                raise ValueError("revisions must preserve at least the last successful FK revision")
        if success is not None and "successes" in value:
            if (
                successes.get(success.upstream_tag) != success.fk_revision
                and success.fk_revision
                not in rerelease_successes.get(success.upstream_tag, ())
            ):
                raise ValueError("successes must preserve the last_success identity")
        if "successes" in value and "revisions" in value:
            for tag, revision in _validated_identity_map("successes", successes).items():
                version = parse_upstream_tag(tag).version
                recorded = revisions.get(version)
                if isinstance(recorded, bool) or not isinstance(recorded, int) or recorded < revision:
                    raise ValueError("revisions must cover every successful FK revision")
        return cls(
            last_success=success,
            attempted=tuple(attempted),
            revisions=revisions,
            assignments=assignments,
            successes=successes,
            rerelease_assignments=rerelease_assignments,
            rerelease_successes=rerelease_successes,
        )

    def assigned_revisions(self, tag):
        values = []
        if tag in self.assignments:
            values.append(self.assignments[tag])
        values.extend(self.rerelease_assignments.get(tag, ()))
        return tuple(values)

    def successful_revisions(self, tag):
        values = []
        if tag in self.successes:
            values.append(self.successes[tag])
        values.extend(self.rerelease_successes.get(tag, ()))
        return tuple(values)

    def unresolved_reservations(self, version):
        """Return every explicit unsuccessful identity for one Chromium version."""
        unresolved = set()
        for tag, revision in self.assignments.items():
            if (
                parse_upstream_tag(tag).version == version
                and revision != self.successes.get(tag)
            ):
                unresolved.add((tag, revision))
        for tag, revisions in self.rerelease_assignments.items():
            if parse_upstream_tag(tag).version != version:
                continue
            successful = set(self.rerelease_successes.get(tag, ()))
            unresolved.update(
                (tag, revision) for revision in revisions if revision not in successful
            )
        return tuple(sorted(unresolved, key=lambda identity: (identity[1], identity[0])))

    def is_assigned(self, candidate):
        return candidate.fk_revision in self.assigned_revisions(candidate.upstream_tag)

    def is_successful(self, candidate):
        return candidate.fk_revision in self.successful_revisions(candidate.upstream_tag)


def _validated_identity_map(name: str, value: Mapping[str, int]):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must map exact stable upstream tags to positive integers")
    identities = dict(value)
    for tag, revision in identities.items():
        if _parse_stable_tag(tag) is None:
            raise ValueError(f"{name} keys must be exact stable upstream tags")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or revision > MAX_FK_REVISION
        ):
            raise ValueError(
                f"{name} values must be between 1 and {MAX_FK_REVISION}"
            )
    return identities


def _validated_revision_lists(name, value):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must map exact stable tags to revision arrays")
    result = {}
    for tag, revisions in value.items():
        if _parse_stable_tag(tag) is None or not isinstance(revisions, (list, tuple)):
            raise ValueError(f"{name} must map exact stable tags to revision arrays")
        if len(revisions) != len(set(revisions)):
            raise ValueError(f"{name} revision arrays cannot contain duplicates")
        for revision in revisions:
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or revision > MAX_FK_REVISION
            ):
                raise ValueError(
                    f"{name} revisions must be between 1 and {MAX_FK_REVISION}"
                )
        result[tag] = tuple(sorted(revisions))
    return result


def _next_revision_for_version(
    version, revisions, assignments, rerelease_assignments=None
):
    reserved = [
        revision
        for tag, revision in assignments.items()
        if parse_upstream_tag(tag).version == version
    ]
    for tag, values in (rerelease_assignments or {}).items():
        if parse_upstream_tag(tag).version == version:
            reserved.extend(values)
    revision = max([revisions.get(version, 0), *reserved]) + 1
    if revision > MAX_FK_REVISION:
        raise ValueError(f"FK revision allocation exceeds {MAX_FK_REVISION}")
    return revision


def _unresolved_reservation_error(version, unresolved):
    identities = ", ".join(f"{tag} fk.{revision}" for tag, revision in unresolved)
    return ValueError(
        f"Chromium version {version} has an unresolved reservation ({identities}); "
        "retry the exact blocked upstream tag with force_rebuild before allocating "
        "another FK revision"
    )


def choose_candidate(
    tags: Iterable[str], state: ReleaseState, force: bool, rerelease: bool = False
) -> Candidate | None:
    """Choose the numerically latest stable tag without mutating *state*."""
    if not isinstance(state, ReleaseState):
        raise TypeError("state must be a ReleaseState")
    if not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    if not isinstance(rerelease, bool):
        raise TypeError("rerelease must be a boolean")
    if force and rerelease:
        raise ValueError("force retry and post-success rerelease are mutually exclusive")

    stable = []
    for tag in tags:
        parsed = _parse_stable_tag(tag)
        if parsed is not None:
            stable.append((parsed.sort_key, tag, parsed.version))
    if not stable:
        return None

    tag_key, tag, version = max(stable, key=lambda item: item[0])
    success = state.last_success
    if rerelease:
        unresolved = state.unresolved_reservations(version)
        if unresolved:
            raise _unresolved_reservation_error(version, unresolved)
        if not state.successful_revisions(tag):
            raise ValueError("Post-success rerelease requires an existing exact-tag success")
        revision = _next_revision_for_version(
            version,
            state.revisions,
            state.assignments,
            state.rerelease_assignments,
        )
        return Candidate(upstream_tag=tag, upstream_version=version, fk_revision=revision)

    if not force:
        if tag in state.attempted or tag in state.successes:
            return None
        if success is not None:
            success_parsed = parse_upstream_tag(success.upstream_tag)
            if tag_key <= success_parsed.sort_key:
                return None
        if state.unresolved_reservations(version):
            return None

    if force:
        unresolved = state.unresolved_reservations(version)
        if not unresolved:
            raise ValueError(
                "Forced rebuild rejected: exact upstream tag has no unresolved reservation"
            )
        retry_tag, revision = unresolved[0]
        if tag != retry_tag:
            raise ValueError(
                f"Forced rebuild rejected: retry {retry_tag} fk.{revision} first"
            )
    else:
        revision = state.assignments.get(tag)
    if revision is None:
        revision = _next_revision_for_version(
            version,
            state.revisions,
            state.assignments,
            state.rerelease_assignments,
        )
    return Candidate(upstream_tag=tag, upstream_version=version, fk_revision=revision)


def record_attempt(state: ReleaseState, candidate: Candidate) -> ReleaseState:
    """Return a new state with this exact dispatch attempt appended."""
    if not isinstance(state, ReleaseState) or not isinstance(candidate, Candidate):
        raise TypeError("record_attempt requires ReleaseState and Candidate values")
    assignments = dict(state.assignments)
    reserved = assignments.get(candidate.upstream_tag)
    rerelease_assignments = {
        tag: tuple(values) for tag, values in state.rerelease_assignments.items()
    }
    if not state.is_assigned(candidate):
        unresolved = state.unresolved_reservations(candidate.upstream_version)
        if unresolved:
            raise _unresolved_reservation_error(candidate.upstream_version, unresolved)
    if reserved is None:
        expected = _next_revision_for_version(
            candidate.upstream_version,
            state.revisions,
            assignments,
            rerelease_assignments,
        )
        if candidate.fk_revision != expected:
            raise ValueError("A new attempt must reserve the next available FK revision")
        assignments[candidate.upstream_tag] = candidate.fk_revision
    elif candidate.fk_revision not in state.assigned_revisions(candidate.upstream_tag):
        if not state.successful_revisions(candidate.upstream_tag):
            raise ValueError("A new exact-tag revision requires a successful prior release")
        expected = _next_revision_for_version(
            candidate.upstream_version,
            state.revisions,
            assignments,
            rerelease_assignments,
        )
        if candidate.fk_revision != expected:
            raise ValueError("A rerelease must reserve the next available FK revision")
        rerelease_assignments[candidate.upstream_tag] = (
            *rerelease_assignments.get(candidate.upstream_tag, ()),
            candidate.fk_revision,
        )
    return ReleaseState(
        last_success=state.last_success,
        attempted=state.attempted + (candidate.upstream_tag,),
        revisions=state.revisions,
        assignments=assignments,
        successes=state.successes,
        rerelease_assignments=rerelease_assignments,
        rerelease_successes=state.rerelease_successes,
    )


def record_success(state: ReleaseState, candidate: Candidate) -> ReleaseState:
    """Return state updated after Task 8 publishes *candidate* successfully."""
    if not isinstance(state, ReleaseState) or not isinstance(candidate, Candidate):
        raise TypeError("record_success requires ReleaseState and Candidate values")
    if not state.is_assigned(candidate):
        raise ValueError("A successful release must match its exact attempted assignment")
    if state.is_successful(candidate):
        return state

    successes = dict(state.successes)
    rerelease_successes = {
        tag: tuple(values) for tag, values in state.rerelease_successes.items()
    }
    if successes.get(candidate.upstream_tag) is None:
        successes[candidate.upstream_tag] = candidate.fk_revision
    else:
        rerelease_successes[candidate.upstream_tag] = (
            *rerelease_successes.get(candidate.upstream_tag, ()),
            candidate.fk_revision,
        )
    revisions = dict(state.revisions)
    revisions[candidate.upstream_version] = max(
        revisions.get(candidate.upstream_version, 0), candidate.fk_revision
    )
    return ReleaseState(
        last_success=candidate,
        attempted=state.attempted,
        revisions=revisions,
        assignments=state.assignments,
        successes=successes,
        rerelease_assignments=state.rerelease_assignments,
        rerelease_successes=rerelease_successes,
    )


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read JSON from {path}: {error}") from error


def _read_release_state_json(path: Path):
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("Could not read release state file") from None
    return decode_release_state_json(document)


def _tag_names(payload):
    if not isinstance(payload, list):
        raise ValueError("GitHub tags payload must be an array")
    names = []
    for entry in payload:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ValueError("Each GitHub tag entry must contain a string name")
        names.append(entry["name"])
    return names


def _request_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "fk-chromium-upstream-detector",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _rate_limit_diagnostic(error: HTTPError):
    details = []
    remaining = error.headers.get("X-RateLimit-Remaining")
    reset = error.headers.get("X-RateLimit-Reset")
    retry_after = error.headers.get("Retry-After")
    if remaining is not None:
        details.append(f"rate-limit remaining={remaining}")
    if reset is not None:
        details.append(f"reset={reset}")
    if retry_after is not None:
        details.append(f"retry-after={retry_after}")
    return ", ".join(details)


def _retry_delay(error: HTTPError, attempt: int):
    retry_after = error.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), 10.0)
        except ValueError:
            pass
    return min(float(2**attempt), 10.0)


def _read_page(url, opener, sleep, max_attempts):
    for attempt in range(max_attempts):
        request = Request(url, headers=_request_headers())
        try:
            with opener(request, timeout=30) as response:
                return json.load(response), response.headers.get("Link")
        except HTTPError as error:
            transient = error.code == 429 or 500 <= error.code <= 599
            diagnostic = _rate_limit_diagnostic(error)
            suffix = f" ({diagnostic})" if diagnostic else ""
            if not transient:
                raise ValueError(f"GitHub tags request failed with HTTP {error.code}{suffix}") from error
            if attempt + 1 == max_attempts:
                raise ValueError(
                    f"GitHub tags request failed with HTTP {error.code} after "
                    f"{max_attempts} attempts{suffix}"
                ) from error
            sleep(_retry_delay(error, attempt))
        except (OSError, ValueError) as error:
            raise ValueError(f"Could not fetch upstream tags: {error}") from error
    raise AssertionError("unreachable retry loop")


def _next_link(link_header):
    if link_header is None:
        return None
    next_urls = []
    for part in link_header.split(","):
        match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="([^"]+)"\s*', part)
        if match is None:
            raise ValueError("Malformed GitHub Link pagination header")
        if "next" in match.group(2).split():
            next_urls.append(match.group(1))
    if len(next_urls) > 1:
        raise ValueError("Malformed GitHub Link header has multiple next relations")
    return next_urls[0] if next_urls else None


def _normalize_tags_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("GitHub pagination next link must keep the api.github.com origin")
    if parsed.path not in (_TAGS_PATH, _TAGS_REPOSITORY_ID_PATH) or parsed.fragment:
        raise ValueError("GitHub pagination next link must keep the tags endpoint")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"page", "per_page"} or query.get("per_page") != ["100"]:
        raise ValueError("GitHub pagination next link has an invalid query")
    page_values = query.get("page")
    if page_values is None:
        page = None
    elif len(page_values) == 1 and re.fullmatch(r"[1-9]\d*", page_values[0]):
        page = int(page_values[0])
    else:
        raise ValueError("GitHub pagination next link has an invalid page")
    normalized_query = {"per_page": "100"}
    if page is not None:
        normalized_query["page"] = str(page)
    return urlunsplit(("https", "api.github.com", _TAGS_PATH, urlencode(normalized_query), ""))


def fetch_tag_names(
    *,
    opener=_NO_REDIRECT_OPEN,
    sleep=time.sleep,
    max_pages=_MAX_TAG_PAGES,
    max_attempts=_MAX_PAGE_ATTEMPTS,
):
    """Fetch all bounded GitHub tag pages, failing closed on any unsafe or partial result."""
    limits = (
        ("max_pages", max_pages, _MAX_TAG_PAGES),
        ("max_attempts", max_attempts, _MAX_PAGE_ATTEMPTS),
    )
    for name, value, safety_bound in limits:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        if value > safety_bound:
            raise ValueError(f"{name} exceeds the production safety bound of {safety_bound}")
    url = _normalize_tags_url(_TAGS_URL)
    visited = set()
    names = []
    while True:
        if url in visited:
            raise ValueError("GitHub pagination loop detected")
        visited.add(url)
        payload, link_header = _read_page(url, opener, sleep, max_attempts)
        names.extend(_tag_names(payload))
        next_url = _next_link(link_header)
        if next_url is None:
            return names
        if len(visited) >= max_pages:
            raise ValueError(f"GitHub tag pagination exceeded the {max_pages}-page limit")
        url = _normalize_tags_url(next_url)
        if url in visited:
            raise ValueError("GitHub pagination loop detected")


def _write_state(path: Path, state: ReleaseState):
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(f"Could not write state to {path}: {error}") from error


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--tags-file", type=Path, help="read a GitHub tags fixture instead of the API")
    source.add_argument("--upstream-tag", help="consider exactly one operator-supplied tag")
    parser.add_argument("--state", type=Path, default=Path("release-state.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--force",
        action="store_true",
        help=(
            "retry the globally lowest unresolved reservation for its Chromium version "
            "when owned by the exact tag"
        ),
    )
    mode.add_argument(
        "--rerelease",
        action="store_true",
        help="allocate a new revision after an exact upstream tag was published",
    )
    parser.add_argument(
        "--record-attempt",
        action="store_true",
        help="atomically append the selected tag to the state file before printing it",
    )
    args = parser.parse_args(arguments)

    if args.rerelease and args.upstream_tag is None:
        parser.error("--rerelease requires --upstream-tag")
    if args.force and args.upstream_tag is None:
        parser.error("--force requires --upstream-tag")

    try:
        state = ReleaseState.from_dict(_read_release_state_json(args.state))
        if args.tags_file is not None:
            tags = _tag_names(_read_json(args.tags_file))
        elif args.upstream_tag is not None:
            tags = [args.upstream_tag]
        else:
            tags = fetch_tag_names()
        if args.upstream_tag is not None and not args.force:
            parsed = _parse_stable_tag(args.upstream_tag)
            if parsed is not None:
                unresolved = state.unresolved_reservations(parsed.version)
                if unresolved:
                    raise _unresolved_reservation_error(parsed.version, unresolved)
        candidate = choose_candidate(
            tags, state, force=args.force, rerelease=args.rerelease
        )
        if candidate is not None and args.record_attempt:
            _write_state(args.state, record_attempt(state, candidate))
        print(json.dumps(None if candidate is None else candidate.to_dict(), sort_keys=True))
        return 0
    except (TypeError, ValueError) as error:
        parser.exit(1, f"check_upstream: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
