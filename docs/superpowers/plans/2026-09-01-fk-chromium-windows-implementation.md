# FK Chromium Windows x64 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and automatically publish a branded, unsigned FK Chromium Windows x64 installer and portable archive from upstream ungoogled-chromium stable tags using free GitHub-hosted runners.

**Architecture:** `firekula/fk-chromium` owns the selected brand source, generated icon set, and a patch overlay that is deliberately kept outside upstream's patch series. `firekula/fk-chromium-windows` prepares an exact upstream Windows tag in CI, injects the FK overlay, resumes the official staged build across free Windows runners, validates the outputs, and publishes only successful builds.

**Tech Stack:** Python 3.12, Pillow, Chromium GN/Ninja, GNU patch, Node 24 composite Actions, GitHub Actions, GitHub REST API, PowerShell.

**Spec:** `docs/superpowers/specs/2026-09-01-fk-chromium-windows-design.md`

## Global Constraints

- Product English name is exactly `FK Chromium`; Chinese descriptive name is exactly `火焰库拉浏览器`.
- Target only Windows x64; do not build x86 or ARM64.
- Preserve internal executable name `chrome.exe`.
- Use the approved first orange-red/navy/white FK flame image; do not substitute the second simplified image.
- Use only public-repository standard GitHub-hosted runners; no paid runner or cloud compute.
- Build at most 12 sequential Windows stages and publish no release after any fatal failure.
- First release is unsigned and must disclose the possible Microsoft Defender SmartScreen warning.
- Do not change homepage, search engine, privacy defaults, extensions, or browser features.
- Release names are `FK-Chromium-<version>-Windows-x64-Installer.exe`, `FK-Chromium-<version>-Windows-x64-Portable.zip`, and `SHA256SUMS.txt`.
- Release tag format is `<chromium-version>-fk.<revision>`.

---

## File Map

### `firekula/fk-chromium`

- `branding/source/fk-chromium.png`: approved master raster with transparency.
- `branding/generate_assets.py`: deterministic crop, resize, sharpening, PNG, and multi-resolution ICO generation.
- `branding/requirements.txt`: pinned Pillow dependency for asset generation.
- `branding/manifest.json`: source-to-Chromium asset mapping and fixed product identity metadata.
- `branding/patches/fk-product-branding.patch`: product text changes applied after upstream common patches.
- `branding/tests/test_generate_assets.py`: asset dimensions, alpha, and ICO-frame tests.
- `branding/tests/test_branding_overlay.py`: manifest and patch contract tests.

### `firekula/fk-chromium-windows`

- `tools/prepare_upstream.py`: materializes an official Windows tag and injects FK common/Windows overlays.
- `tools/install_brand_assets.py`: copies generated binary assets into a prepared Chromium source tree.
- `tools/release_metadata.py`: parses versions, builds file names/checksums, and formats failure issue metadata.
- `patches/fk-chromium/windows-product-identity.patch`: unique Windows install path, registry, GUID, and AppUserModelID.
- `patches/fk-chromium/windows-build-brand-assets.patch`: invokes binary asset installation at the correct point in `build.py`.
- `tests/fixtures/upstream-tree/`: minimal upstream-shaped tree for preparation tests.
- `tests/test_prepare_upstream.py`: overlay order and idempotency tests.
- `tests/test_install_brand_assets.py`: manifest copy and missing-input tests.
- `tests/test_release_metadata.py`: tag, file name, checksum, and failure-key tests.
- `.github/actions/prepare-fk/action.yml`: prepares a clean FK build root on every stage.
- `.github/actions/stage/index.js`: resumes/builds x64 and uploads FK-named artifacts.
- `.github/workflows/build-x64.yml`: manual/tag/called x64 entrypoint.
- `.github/workflows/reusable-build.yml`: at most 12 sequential stages.
- `.github/workflows/check-upstream.yml`: daily stable-tag detector and duplicate-attempt guard.
- `.github/workflows/publish-release.yml`: validates, attests metadata, and creates the release.
- `.github/workflows/report-failure.yml`: creates or updates one failure Issue per upstream version.
- `release-state.json`: last successful and attempted upstream versions.
- `README.md`: project, local build, CI setup, unsigned-build warning, and recovery instructions.

---

### Task 1: Produce Reproducible Brand Assets

**Repository:** `firekula/fk-chromium`

**Files:**
- Create: `branding/source/fk-chromium.png`
- Create: `branding/generate_assets.py`
- Create: `branding/requirements.txt`
- Create: `branding/generated/product_logo_16.png`
- Create: `branding/generated/product_logo_24.png`
- Create: `branding/generated/product_logo_32.png`
- Create: `branding/generated/product_logo_48.png`
- Create: `branding/generated/product_logo_64.png`
- Create: `branding/generated/product_logo_128.png`
- Create: `branding/generated/product_logo_256.png`
- Create: `branding/generated/fk_chromium.ico`
- Create: `branding/tests/test_generate_assets.py`

**Interfaces:**
- Consumes: approved RGBA image `generated_images/exec-7b325b3c-1fc7-489a-af2a-d9203685885a.png`.
- Produces: `generate_assets(source: Path, output_dir: Path) -> list[Path]` and committed assets consumed by Task 2 and Task 4.

- [ ] **Step 1: Copy the approved first image and write failing tests**

Use a binary-safe file copy for the approved source. Add tests that call `generate_assets()` into `tmp_path`, assert exact PNG sizes `(16, 24, 32, 48, 64, 128, 256)`, assert RGBA output with at least one alpha-zero pixel, and assert the ICO reports sizes `{16, 20, 24, 32, 40, 48, 64, 128, 256}` through Pillow.

```python
def test_generate_assets_has_expected_sizes(tmp_path):
    outputs = generate_assets(SOURCE, tmp_path)
    assert {p.name for p in outputs} >= {
        "product_logo_16.png", "product_logo_256.png", "fk_chromium.ico"
    }
    assert Image.open(tmp_path / "product_logo_16.png").size == (16, 16)
    assert Image.open(tmp_path / "product_logo_256.png").getextrema()[3][0] == 0
    assert set(Image.open(tmp_path / "fk_chromium.ico").info["sizes"]) == {
        (16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
        (48, 48), (64, 64), (128, 128), (256, 256),
    }
```

- [ ] **Step 2: Verify the tests fail before the generator exists**

Run: `python -m pytest branding/tests/test_generate_assets.py -q`

Expected: FAIL because `branding.generate_assets` is not importable.

- [ ] **Step 3: Implement deterministic asset generation**

Implement `alpha_bbox()`, square transparent padding, Lanczos resize, one mild unsharp-mask pass only for sizes up to 48 px, and ICO generation with the exact frame list above. Pin `Pillow==11.3.0` in `branding/requirements.txt`. Do not recolor, redraw, flatten alpha, or add a background.

- [ ] **Step 4: Generate and verify committed outputs**

Run:

```bash
python -m pip install -r branding/requirements.txt
python branding/generate_assets.py branding/source/fk-chromium.png branding/generated
python -m pytest branding/tests/test_generate_assets.py -q
```

Expected: all tests PASS and a second generator run produces no Git diff.

- [ ] **Step 5: Commit the brand asset pipeline**

```bash
git add branding/source branding/generated branding/generate_assets.py branding/requirements.txt branding/tests/test_generate_assets.py
git commit -m "feat: add FK Chromium brand assets"
```

---

### Task 2: Define the Common FK Branding Overlay

**Repository:** `firekula/fk-chromium`

**Files:**
- Create: `branding/manifest.json`
- Create: `branding/patches/fk-product-branding.patch`
- Create: `branding/tests/test_branding_overlay.py`

**Interfaces:**
- Consumes: generated files from Task 1.
- Produces: manifest schema `{product, windows_identity, assets}` and a text patch consumed by `prepare_upstream.py` and `install_brand_assets.py`.

- [ ] **Step 1: Write failing overlay contract tests**

Test that the manifest contains exact product values, every source asset exists, every destination is a normalized relative path beneath `chrome/app/theme/chromium`, all GUID strings parse with `uuid.UUID`, and the patch changes `chrome/app/theme/chromium/BRANDING` to the exact FK names.

```python
def test_manifest_product_contract():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["product"]["full_name"] == "FK Chromium"
    assert manifest["product"]["chinese_name"] == "火焰库拉浏览器"
    assert manifest["windows_identity"]["app_guid"] == "{385317E5-D454-45E9-9A3A-D240C07A3AC0}"
    assert manifest["windows_identity"]["base_app_id"] == "FKChromium"
```

- [ ] **Step 2: Verify contract tests fail**

Run: `python -m pytest branding/tests/test_branding_overlay.py -q`

Expected: FAIL because the manifest and patch do not exist.

- [ ] **Step 3: Add fixed identity values and binary mapping**

Use these immutable IDs:

```json
{
  "app_guid": "{385317E5-D454-45E9-9A3A-D240C07A3AC0}",
  "active_setup_guid": "{6782E06C-9D57-4F60-906F-7BD0B6C5C935}",
  "toast_activator_clsid": "{EFBECFD4-69ED-47B7-90F9-0ADD86CFD63A}",
  "legacy_command_execute_clsid": "{15EEF2CB-C20F-4544-9E45-672B6F379E17}",
  "elevation_service_clsid": "{DA37F775-297F-4681-8BE8-02A17F618849}",
  "base_app_id": "FKChromium",
  "install_subdir": "FK Chromium"
}
```

Map the ICO to Chromium's Windows icon target and each PNG to its matching `product_logo_<size>.png` target. Keep this overlay outside `patches/series`; the Windows preparation tool appends it to a temporary upstream tree.

- [ ] **Step 4: Add the BRANDING patch**

Patch the current Chromium `chrome/app/theme/chromium/BRANDING` keys to:

```text
COMPANY_FULLNAME=Firekula
COMPANY_SHORTNAME=Firekula
PRODUCT_FULLNAME=FK Chromium
PRODUCT_SHORTNAME=FK Chromium
PRODUCT_INSTALLER_FULLNAME=FK Chromium Installer
PRODUCT_APPNAME=FK Chromium
PRODUCT_LOGO=FKChromium
COPYRIGHT=Copyright 2026 Firekula. All rights reserved.
```

- [ ] **Step 5: Run overlay tests and upstream patch validation**

Run:

```bash
python -m pytest branding/tests/test_branding_overlay.py -q
python devutils/validate_patches.py -s patches/series
```

Expected: all tests and existing patch validation PASS.

- [ ] **Step 6: Commit the overlay contract**

```bash
git add branding/manifest.json branding/patches branding/tests/test_branding_overlay.py
git commit -m "feat: define FK Chromium branding overlay"
```

---

### Task 3: Prepare an Exact Upstream Build Tree

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Create: `tools/__init__.py`
- Create: `tools/prepare_upstream.py`
- Create: `tests/fixtures/upstream-tree/patches/series`
- Create: `tests/fixtures/upstream-tree/ungoogled-chromium/patches/series`
- Create: `tests/test_prepare_upstream.py`

**Interfaces:**
- Consumes: `prepare_tree(upstream_root: Path, branding_root: Path) -> PreparedTree`.
- Produces: a build root whose common series ends with `branding/patches/fk-product-branding.patch`, whose Windows series ends with both FK Windows patches, and whose preparation metadata records exact upstream and branding SHAs.

- [ ] **Step 1: Write failing preparation tests**

Cover clean preparation, missing manifest, duplicate invocation, path traversal in manifest destinations, and exact overlay ordering. Require idempotency: a second call must not duplicate series entries.

```python
def test_prepare_tree_appends_fk_overlays_once(tmp_path):
    root = copy_fixture(tmp_path)
    result = prepare_tree(root, BRANDING_ROOT)
    prepare_tree(root, BRANDING_ROOT)
    assert read_nonblank(root / "ungoogled-chromium/patches/series")[-1] == \
        "extra/fk-chromium/fk-product-branding.patch"
    assert read_nonblank(root / "patches/series")[-2:] == [
        "fk-chromium/windows-product-identity.patch",
        "fk-chromium/windows-build-brand-assets.patch",
    ]
    assert result.manifest["product"]["full_name"] == "FK Chromium"
```

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/test_prepare_upstream.py -q`

Expected: FAIL because `tools.prepare_upstream` does not exist.

- [ ] **Step 3: Implement preparation with strict validation**

Use only `pathlib`, `json`, `shutil`, and `subprocess`. Reject absolute paths and `..` components. Copy the common patch into `ungoogled-chromium/patches/extra/fk-chromium/`, copy Windows patches into `patches/fk-chromium/`, and write `build/fk-build-metadata.json` with upstream tag, upstream commit, brand commit, and manifest SHA-256.

- [ ] **Step 4: Run the focused and repository tests**

Run:

```bash
python -m pytest tests/test_prepare_upstream.py -q
python -m compileall tools
```

Expected: PASS.

- [ ] **Step 5: Commit the preparation adapter**

```bash
git add tools tests/fixtures tests/test_prepare_upstream.py
git commit -m "feat: prepare branded upstream build trees"
```

---

### Task 4: Install Binary Assets and Isolate Windows Product Identity

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Create: `tools/install_brand_assets.py`
- Create: `tests/test_install_brand_assets.py`
- Create: `patches/fk-chromium/windows-build-brand-assets.patch`
- Create: `patches/fk-chromium/windows-product-identity.patch`

**Interfaces:**
- Consumes: `install_assets(source_root: Path, branding_root: Path, manifest_path: Path) -> list[Path]`.
- Produces: Chromium theme files plus install constants fixed to the manifest identities.

- [ ] **Step 1: Write failing binary-copy tests**

Test exact copies, SHA-256 equality, missing source rejection, destination traversal rejection, and no writes outside `source_root`.

```python
def test_install_assets_copies_exact_bytes(tmp_path):
    copied = install_assets(tmp_path / "src", BRANDING_ROOT, MANIFEST)
    assert copied
    for destination in copied:
        assert destination.is_relative_to(tmp_path / "src")
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest tests/test_install_brand_assets.py -q`

Expected: FAIL because the installer module does not exist.

- [ ] **Step 3: Implement the binary installer**

Copy atomically through a sibling `.tmp` file followed by `Path.replace()`. Log relative destinations only. Return copied paths sorted lexically so tests and build logs are deterministic.

- [ ] **Step 4: Add the build hook patch**

Patch upstream Windows `build.py` immediately after common and Windows text patches are applied and before domain substitution/GN generation. Invoke:

```python
subprocess.run([
    sys.executable,
    str(_ROOT_DIR / 'tools' / 'install_brand_assets.py'),
    '--source-root', str(source_tree),
    '--branding-root', str(_ROOT_DIR / 'fk-branding'),
    '--manifest', str(_ROOT_DIR / 'fk-branding' / 'branding' / 'manifest.json'),
], check=True)
```

- [ ] **Step 5: Add the Windows identity patch**

Patch the current `chrome/install_static/chromium_install_modes.cc` Chromium mode to use the five fixed GUID/CLSID values from Task 2, `base_app_id` `FKChromium`, product path `FK Chromium`, uninstall registry suffix `FKChromium`, and visible application name `FK Chromium`. Do not rename `chrome.exe` and do not alter Chrome-branded modes.

- [ ] **Step 6: Validate patch application against Chromium 151 source**

Run the preparation tool against the current pinned upstream tag, then run the existing patch validation/apply stage without starting Ninja. Expected: both FK patches apply cleanly and asset SHA-256 values match the manifest sources.

- [ ] **Step 7: Run tests and commit**

```bash
python -m pytest tests/test_install_brand_assets.py tests/test_prepare_upstream.py -q
git add tools/install_brand_assets.py tests/test_install_brand_assets.py patches/fk-chromium
git commit -m "feat: apply FK Chromium Windows identity"
```

---

### Task 5: Create FK Packages and Release Metadata

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Create: `tools/release_metadata.py`
- Create: `tests/test_release_metadata.py`
- Modify: `.github/actions/stage/index.js`

**Interfaces:**
- Produces: `parse_upstream_tag(tag: str) -> UpstreamVersion`, `release_tag(version: str, revision: int) -> str`, `artifact_names(version: str) -> ArtifactNames`, and `write_sha256s(paths: Sequence[Path], output: Path) -> None`.
- Consumed by: Tasks 6–8.

- [ ] **Step 1: Write failing metadata tests**

```python
def test_artifact_names_are_exact():
    names = artifact_names("151.0.7922.173")
    assert names.installer == "FK-Chromium-151.0.7922.173-Windows-x64-Installer.exe"
    assert names.portable == "FK-Chromium-151.0.7922.173-Windows-x64-Portable.zip"
    assert release_tag("151.0.7922.173", 1) == "151.0.7922.173-fk.1"
```

Also reject prerelease/non-four-part tags and verify `SHA256SUMS.txt` uses lowercase 64-digit hashes, two spaces, and lexical file ordering.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/test_release_metadata.py -q`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement metadata functions with no network access**

Use frozen dataclasses and strict `re.fullmatch(r"(\d+)\.(\d+)\.(\d+)\.(\d+)(?:-(\d+))?")`. Keep GitHub API concerns outside this module.

- [ ] **Step 4: Update the stage action's final artifact discovery**

Replace the `ungoogled-chromium*` final glob with `FK-Chromium-*`, call `tools/release_metadata.py package` after upstream `package.py`, rename the upstream installer/ZIP into the exact FK names, create `SHA256SUMS.txt`, and upload artifact name `fk-chromium-windows-x64`.

- [ ] **Step 5: Run Python and Node syntax checks**

```bash
python -m pytest tests/test_release_metadata.py -q
node --check .github/actions/stage/index.js
```

Expected: PASS.

- [ ] **Step 6: Commit packaging metadata**

```bash
git add tools/release_metadata.py tests/test_release_metadata.py .github/actions/stage/index.js
git commit -m "feat: package FK Chromium release artifacts"
```

---

### Task 6: Reduce the Staged Workflow to Windows x64

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Create: `.github/actions/prepare-fk/action.yml`
- Modify: `.github/workflows/build-x64.yml`
- Modify: `.github/workflows/reusable-build.yml`
- Delete: `.github/workflows/build-x86.yml`
- Delete: `.github/workflows/build-arm.yml`
- Create: `tests/test_workflows.py`

**Interfaces:**
- Consumes: workflow inputs `upstream_tag: string`, `fk_revision: number`, and `force_rebuild: boolean`.
- Produces: final artifact `fk-chromium-windows-x64` and outputs `finished`, `upstream_version`, `release_tag`.

- [ ] **Step 1: Write failing workflow contract tests**

Read YAML as text to avoid introducing a YAML parser dependency. Assert exactly 12 `build-N:` jobs, all `runs-on: windows-2022`, no `x86`/`arm` inputs, final artifact name present, `timeout-minutes` set below GitHub's job maximum, permissions are `contents: read`, and deleted workflows are absent.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/test_workflows.py -q`

Expected: FAIL because ARM/x86 workflows and inputs still exist.

- [ ] **Step 3: Implement `prepare-fk`**

The composite action must:

1. check out this repository into `fk-windows`;
2. check out `ungoogled-software/ungoogled-chromium-windows` at `inputs.upstream-tag` into `upstream-windows`, recursively;
3. check out `firekula/fk-chromium` default branch into `fk-branding`;
4. copy `tools/`, `patches/fk-chromium/`, and `fk-branding` beneath the upstream working root;
5. call `python tools/prepare_upstream.py`;
6. copy the prepared root to `C:\ungoogled-chromium-windows`.

Every checkout must persist its commit SHA into `build/fk-build-metadata.json`.

- [ ] **Step 4: Make all build stages x64-only**

Remove architecture booleans from the reusable workflow and stage action. Pass `upstream_tag` through all 12 stages. Preserve the official `finished` short-circuit and `from_artifact` behavior. Set concurrency to `fk-x64-${{ inputs.upstream_tag }}` with `cancel-in-progress: false`.

- [ ] **Step 5: Run workflow and action checks**

```bash
python -m pytest tests/test_workflows.py -q
node --check .github/actions/stage/index.js
git diff --check
```

Expected: PASS.

- [ ] **Step 6: Commit the x64-only pipeline**

```bash
git add .github tests/test_workflows.py
git commit -m "ci: build FK Chromium x64 in staged jobs"
```

---

### Task 7: Detect New Stable Upstream Versions Once

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Create: `tools/check_upstream.py`
- Create: `tests/fixtures/tags.json`
- Create: `tests/test_check_upstream.py`
- Create: `release-state.json`
- Create: `.github/workflows/check-upstream.yml`

**Interfaces:**
- Produces: `choose_candidate(tags: Iterable[str], state: ReleaseState, force: bool) -> Candidate | None`.
- Workflow outputs: `should_build`, `upstream_tag`, `upstream_version`, `fk_revision`.

- [ ] **Step 1: Write failing detector tests**

Cover unsorted tags, prereleases, duplicate successes, duplicate failed attempts, forced retry, and incrementing FK revision for an already released Chromium version.

```python
def test_failed_version_is_not_retried_without_force():
    state = ReleaseState(last_success=None, attempted=("151.0.7922.173-1",))
    assert choose_candidate(["151.0.7922.173-1"], state, force=False) is None
```

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/test_check_upstream.py -q`

Expected: FAIL because the detector does not exist.

- [ ] **Step 3: Implement deterministic selection**

Fetch `https://api.github.com/repos/ungoogled-software/ungoogled-chromium-windows/tags?per_page=100` only in the CLI wrapper; keep `choose_candidate()` pure. Accept only stable four-part Chromium tags with the upstream packaging revision. Initialize state as:

```json
{"last_success": null, "attempted": []}
```

- [ ] **Step 4: Add scheduled and manual workflow triggers**

Use cron `17 3 * * *` and `workflow_dispatch` inputs `upstream_tag` and `force_rebuild`. Grant `actions: write`, `contents: write`, and `issues: write` only to the job that dispatches the build and records attempts. Record an attempt before dispatch so concurrent daily runs cannot duplicate it.

- [ ] **Step 5: Run detector and workflow contract tests**

```bash
python -m pytest tests/test_check_upstream.py tests/test_workflows.py -q
python tools/check_upstream.py --tags-file tests/fixtures/tags.json --state release-state.json
```

Expected: tests PASS and the fixture CLI prints one JSON candidate without changing repository files.

- [ ] **Step 6: Commit upstream detection**

```bash
git add tools/check_upstream.py tests release-state.json .github/workflows/check-upstream.yml
git commit -m "ci: detect new ungoogled Chromium releases"
```

---

### Task 8: Publish Successes and Report Failures

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Modify: `.github/workflows/publish-release.yml`
- Create: `.github/workflows/report-failure.yml`
- Create: `tests/test_release_workflows.py`

**Interfaces:**
- Consumes: completed `build-x64` workflow run and artifact `fk-chromium-windows-x64`.
- Produces: GitHub Release plus updated `release-state.json`, or one Issue keyed by `fk-build-failed:<upstream-version>`.

- [ ] **Step 1: Write failing release workflow tests**

Assert the publish workflow reacts only to successful `build-x64`, downloads the exact artifact, verifies all three required files and SHA-256 before `softprops/action-gh-release`, includes upstream/build SHAs in release notes, and has no Winget/binaries-site jobs. Assert failure reporting runs only on `failure` or 12-stage exhaustion and uses `issues: write`.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/test_release_workflows.py -q`

Expected: FAIL because the current publisher expects x64/x86/ARM and publishes Winget.

- [ ] **Step 3: Implement gated x64 publishing**

Download into `artifacts/x64`, run `python tools/release_metadata.py verify artifacts/x64`, then publish the installer, portable ZIP, and `SHA256SUMS.txt`. Release notes must contain `Upstream tag`, `Upstream commit`, `FK branding commit`, `Windows build commit`, `Workflow run`, and this exact warning:

```text
此安装程序尚未进行 Windows 代码签名，Microsoft Defender SmartScreen 可能显示未知发布者警告。
```

- [ ] **Step 4: Update successful state without recursive workflow loops**

After release creation, update `release-state.json` through the GitHub Contents API with the successful upstream tag and preserve the attempted history. Include `[skip ci]` in the state commit message. The check workflow must ignore commits that only change `release-state.json`.

- [ ] **Step 5: Implement idempotent failure Issues**

Search open and closed Issues for label `fk-build-failure` plus marker `fk-build-failed:<version>`. Create one if absent; otherwise add a comment containing failure stage, run URL, upstream tag, and commits. Never expose secrets or dump the environment.

- [ ] **Step 6: Run release workflow tests**

```bash
python -m pytest tests/test_release_workflows.py tests/test_workflows.py -q
git diff --check
```

Expected: PASS.

- [ ] **Step 7: Commit publication and failure handling**

```bash
git add .github/workflows tools tests release-state.json
git commit -m "ci: publish verified FK Chromium releases"
```

---

### Task 9: Document Setup and Run the Preflight Suite

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Modify: `README.md`
- Create: `docs/release-checklist.md`
- Create: `tests/test_documentation.py`

**Interfaces:**
- Consumes: all commands and workflow names from Tasks 1–8.
- Produces: operator instructions and the manual Windows acceptance checklist required before the first public release.

- [ ] **Step 1: Write failing documentation tests**

Assert README contains product names, x64-only scope, automatic schedule, manual retry path, unsigned/SmartScreen warning, expected release files, and links to both source repositories. Assert the release checklist covers install, upgrade, start menu, desktop shortcut, taskbar icon, About page, independent user-data directory, portable start, and uninstall behavior.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/test_documentation.py -q`

Expected: FAIL because upstream README does not describe FK Chromium.

- [ ] **Step 3: Rewrite README for FK Chromium**

Keep BSD-3-Clause attribution and upstream credits. Document that full builds may require up to 12 stages and roughly 1–2 days. Explain manual dispatch fields and how to reopen/retry a failed upstream version. Do not promise code signing or in-browser automatic updates.

- [ ] **Step 4: Add the first-release acceptance checklist**

Include expected paths and names, but make deletion steps opt-in. Record tested Windows version, artifact SHA-256, release tag, installer outcome, portable outcome, user-data isolation, and uninstall outcome.

- [ ] **Step 5: Run both repositories' preflight suites**

In `fk-chromium`:

```bash
python -m pytest branding/tests utils/tests devutils/tests -q
python devutils/validate_patches.py -s patches/series
git diff --check
```

In `fk-chromium-windows`:

```bash
python -m pytest tests -q
python -m compileall tools
node --check .github/actions/stage/index.js
git diff --check
```

Expected: all commands PASS.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md docs/release-checklist.md tests/test_documentation.py
git commit -m "docs: add FK Chromium build and release guide"
```

---

### Task 10: Validate the First Chromium 151 Build Without Premature Release

**Repository:** `firekula/fk-chromium-windows`

**Files:**
- Modify only if validation exposes a defect in files owned by Tasks 1–9.

**Interfaces:**
- Consumes: current upstream tag matching Chromium `151.0.7922.173` and FK revision `1`.
- Produces: successful Actions artifacts ready for manual Windows acceptance; publication remains gated until validation passes.

- [ ] **Step 1: Push both implementation branches and verify remote SHAs**

Run `git ls-remote` for both pushed heads and compare them with local `git rev-parse HEAD`. Do not dispatch using unpushed commits.

- [ ] **Step 2: Run the manual x64 workflow in non-publishing mode**

Dispatch `build-x64.yml` with the current stable upstream tag, `fk_revision=1`, and `publish=false`. Confirm each resumed stage uses the same upstream, brand, and Windows commit metadata.

- [ ] **Step 3: Inspect the final CI artifact**

Verify the exact three file names, run `release_metadata.py verify`, inspect PE version strings and embedded icon resources, and scan user-visible resources for `ungoogled-chromium`. Any occurrence must be classified as either required upstream attribution or a branding defect.

- [ ] **Step 4: Perform manual Windows 10/11 x64 acceptance**

Follow `docs/release-checklist.md`. Confirm the internal process is still `chrome.exe`, the visible product is FK Chromium, and its user data is isolated.

- [ ] **Step 5: Enable and dispatch publication**

Only after Steps 1–4 pass, rerun or promote the same commit tuple with `publish=true`. Confirm tag `151.0.7922.173-fk.1`, release warning, assets, hashes, and workflow provenance.

- [ ] **Step 6: Record first-release evidence**

Attach the completed checklist to the release or a repository Issue and close any version-specific failure Issue. No code commit is needed unless validation required a fix.
