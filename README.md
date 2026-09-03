# FK Chromium for Windows

This repository builds **FK Chromium** (**火焰库拉浏览器**) for **Windows x64**. It combines the Windows packaging from [ungoogled-chromium-windows](https://github.com/ungoogled-software/ungoogled-chromium-windows) with the FK brand overlay from [firekula/fk-chromium](https://github.com/firekula/fk-chromium). The Windows automation and packaging source lives in [firekula/fk-chromium-windows](https://github.com/firekula/fk-chromium-windows).

The first release changes branding and release automation only. Windows code signing is not provided. In-browser automatic updates are not provided. It does not add browser features, change the homepage or search engine, or build x86/ARM64 packages.

## Downloads and security

Each release contains exactly:

- `FK-Chromium-<version>-Windows-x64-Installer.exe`
- `FK-Chromium-<version>-Windows-x64-Portable.zip`
- `SHA256SUMS.txt`

Verify each download against `SHA256SUMS.txt` before running it.

These builds are **unsigned**. They have no Windows code-signing certificate, so Microsoft Defender SmartScreen may show a warning. Check the release tag, source commit, and SHA-256 checksum before deciding whether to continue. The project does not claim that an unsigned installer is trusted merely because it was downloaded from GitHub.

## Automatic builds

`.github/workflows/check-upstream.yml` runs daily at the cron schedule `17 3 * * *` (03:17 UTC). It accepts only canonical stable `ungoogled-chromium-windows` tags, records a revision reservation, and dispatches the x64 build. A full build can use up to **12** sequential GitHub-hosted Windows stages and typically needs about **1–2 days**. A fatal stage or an unfinished twelfth stage prevents publication.

Successful publishing produces a tag of the form `<chromium-version>-fk.<revision>`. Publication validates the exact successful workflow attempt, release-state reservation, artifact names, and checksums before creating a release. Failed runs create or update one `fk-build-failure` Issue for that Chromium version.

### Manual detection and retry

Use **Actions → check-upstream → Run workflow** for the normal operator path:

- `upstream_tag`: leave blank to select the latest stable tag, or enter the exact six-component upstream tag such as `151.0.7922.173-1.1`.
- `force_rebuild`: set to `true` only after fixing a failed build; this reuses a reservation only when the exact supplied tag owns the lowest unresolved FK revision for that four-part Chromium version.
- `rerelease`: after an exact upstream tag has already published successfully, set this to `true` with that `upstream_tag` to allocate the next collision-free FK revision (`fk.2`, then `fk.3`, and so on). This post-success re-release path is distinct from failure retry and must not be combined with `force_rebuild=true`. Any unresolved reservation serializes every packaging tag for the same four-part Chromium version; retry the exact blocked `upstream_tag` with `force_rebuild=true`, always processing the lowest unresolved FK revision first, before requesting any new revision.

After a failure, inspect the linked run, fix and merge the cause, manually reopen the matching `fk-build-failure` Issue if it is closed, then run `check-upstream` with the same `upstream_tag` and `force_rebuild=true`. The reporter appends attempt-specific diagnostics to the existing Issue; close it only after the retry succeeds.

For a corrected post-success release of the same upstream source, review the prior release and public tags, then run `check-upstream` with the exact `upstream_tag` and `rerelease=true`. The detector allocates the next FK revision from recorded reservation and success high-water marks, and the publisher separately preflights the exact public tag. It never adopts, deletes, or overwrites an existing release; any public residue at the selected tag fails closed for manual inspection.

For a direct diagnostic build, use **Actions → build-x64 → Run workflow**:

- `upstream_tag`: exact canonical upstream Windows tag (required).
- `fk_revision`: positive reserved FK release revision; normally use the value recorded in `release-state.json`.
- `force_rebuild`: recorded in build metadata as provenance for a direct build; it does not block or bypass duplicate direct workflow execution. Use the `check-upstream` retry path above when the attempted-version guard must be bypassed.
- `publish`: defaults to `false`; leave it false for diagnostics. Set it true only when the state reservation and release intent have been reviewed.

Tag-push builds are also non-publishing by default. Never increment `fk_revision` merely to work around a failed attempt; the scheduled detector owns revision allocation.

## Repository setup

Keep both FK repositories public so the standard GitHub-hosted runners remain free. Enable GitHub Actions and allow the repository `GITHUB_TOKEN` to receive the permissions declared by each job; no separate publishing secret is expected. Keep the Windows default branch protected and review changes to workflows, `release-state.json`, brand assets, and patches before merging. Scheduled workflows run from the default branch, so merge a tested configuration there before expecting daily detection.

Do not add a signing secret until there is a real Windows code-signing process and certificate. This first version deliberately publishes unsigned files.

## Local verification

The automated build is designed for public repositories on free GitHub-hosted runners. Before changing build or release logic, run:

```bash
python -m pytest tests -q
python -m compileall tools
node --check .github/actions/stage/index.js
git diff --check
```

The FK brand repository should also pass:

```bash
python -m pytest branding/tests utils/tests devutils/tests -q
python devutils/validate_patches.py -l <clean-chromium-source> -s patches/series
git diff --check
```

The first public release also requires the real Windows checks in [docs/release-checklist.md](docs/release-checklist.md). GitHub-hosted runners do not replace interactive installation, upgrade, shortcut, user-data isolation, portable, and uninstall testing.

Release notes identify FK Chromium as 火焰库拉浏览器.

## Upstream and license

This project is derived from [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium), [ungoogled-chromium-windows](https://github.com/ungoogled-software/ungoogled-chromium-windows), and Chromium. Their copyright notices and upstream credits are preserved.

The repository is distributed under the **BSD-3-Clause** license. See [LICENSE](LICENSE).
