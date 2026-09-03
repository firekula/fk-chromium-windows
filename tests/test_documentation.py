import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    path = ROOT / relative_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _section(document: str, heading: str) -> str:
    marker = f"## {heading}\n"
    assert marker in document
    return document.split(marker, 1)[1].split("\n## ", 1)[0]


def test_readme_documents_product_artifacts_sources_and_license():
    readme = _read("README.md")

    for required_text in (
        "FK Chromium",
        "火焰库拉浏览器",
        "Windows x64",
        "FK-Chromium-<version>-Windows-x64-Installer.exe",
        "FK-Chromium-<version>-Windows-x64-Portable.zip",
        "SHA256SUMS.txt",
        "https://github.com/firekula/fk-chromium",
        "https://github.com/firekula/fk-chromium-windows",
        "BSD-3-Clause",
        "ungoogled-chromium",
    ):
        assert required_text in readme


def test_readme_anchors_automatic_timing_and_retry_to_the_right_workflow():
    automatic = _section(_read("README.md"), "Automatic builds")
    direct_build = automatic.split(
        "For a direct diagnostic build, use **Actions → build-x64 → Run workflow**:", 1
    )[1].split("Tag-push builds", 1)[0]

    assert "17 3 * * *" in automatic
    assert re.search(
        r"up to \*\*12\*\* sequential GitHub-hosted Windows stages "
        r"and typically needs about \*\*1–2 days\*\*",
        automatic,
    )
    assert "manually reopen the matching `fk-build-failure` Issue" in automatic
    assert "run `check-upstream` with the same `upstream_tag` and `force_rebuild=true`" in automatic
    assert "`rerelease=true`" in automatic
    assert "allocates the next FK revision" in automatic
    assert "must not be combined with `force_rebuild=true`" in automatic
    assert "same four-part Chromium version" in automatic
    assert "exact blocked `upstream_tag` with `force_rebuild=true`" in automatic
    assert "lowest unresolved FK revision first" in automatic
    for field in ("upstream_tag", "fk_revision", "force_rebuild", "publish"):
        assert f"- `{field}`:" in automatic
    assert "recorded in build metadata as provenance" in direct_build
    assert "does not block or bypass duplicate direct workflow execution" in direct_build
    assert "bypass the duplicate-attempt guard" not in direct_build


def test_release_notes_identify_the_exact_chinese_descriptive_name():
    """Release guidance must carry the approved Chinese descriptive name verbatim."""
    readme = _read("README.md")

    assert "Release notes identify FK Chromium as 火焰库拉浏览器." in readme


def test_readme_explicitly_disclaims_signing_and_in_browser_updates():
    readme = _read("README.md")

    assert "These builds are **unsigned**." in readme
    assert "Microsoft Defender SmartScreen may show a warning" in readme
    assert "Windows code signing is not provided." in readme
    assert "In-browser automatic updates are not provided." in readme
    assert "These builds are signed." not in readme
    assert "In-browser automatic updates are provided." not in readme


def test_checklist_has_distinct_exact_test_record_fields():
    test_record = _section(_read("docs/release-checklist.md"), "Test record")

    for field in (
        "Windows version, edition, build, and x64 architecture",
        "FK Chromium release tag (`<chromium-version>-fk.<revision>`)",
        "Installer SHA-256 from `SHA256SUMS.txt`",
        "Independently calculated installer SHA-256",
        "Portable SHA-256 from `SHA256SUMS.txt`",
        "Independently calculated portable SHA-256",
        "Installer outcome: PASS / FAIL",
        "Upgrade outcome: PASS / FAIL / NOT APPLICABLE",
        "Portable outcome: PASS / FAIL",
        "User data isolation outcome: PASS / FAIL",
        "Uninstall outcome: PASS / FAIL",
    ):
        assert f"- {field}" in test_record
    assert test_record.count("Installer outcome:") == 1
    assert test_record.count("Uninstall outcome:") == 1


def test_checklist_requires_install_shell_data_portable_and_uninstall_checks():
    checklist = _read("docs/release-checklist.md")
    install = _section(checklist, "Artifact and install checks")
    shell = _section(checklist, "Upgrade and shell integration")
    user_data = _section(checklist, "User data isolation")
    portable = _section(checklist, "Portable package")
    uninstall = _section(checklist, "Uninstall and optional cleanup")

    assert "complete the current-user install" in install
    assert "install the newer release over it" in shell
    assert "Start Menu entry is named `FK Chromium`" in shell
    assert "desktop shortcut is named `FK Chromium`" in shell
    assert "taskbar" in shell
    assert "About page" in shell
    assert "installed user data directory" in user_data
    assert "does not read, change, or overwrite Chrome, Chromium, or ungoogled-chromium profiles" in user_data
    assert "Start the portable browser directly from the extracted directory" in portable
    assert "Start uninstall from Windows Installed apps" in uninstall
    assert "independent user data directory remains" in uninstall
    assert "Optional, destructive, opt-in only" in uninstall
    assert "unless the user explicitly opts in" in uninstall


def test_checklist_records_smartscreen_observation_conditionally():
    install = _section(_read("docs/release-checklist.md"), "Artifact and install checks")

    assert "Record whether a Microsoft Defender SmartScreen warning appeared" in install
    assert "If a warning appeared" in install
    assert "unsigned or unknown-publisher installer" in install
    assert "if no warning appeared, record that outcome without treating it as a failure" in install
    assert "warning as expected" not in install
