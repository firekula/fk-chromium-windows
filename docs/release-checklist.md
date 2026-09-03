# FK Chromium first-release Windows checklist

Complete this checklist on a real Windows 10 or Windows 11 x64 machine before the first public release. Use a standard user account for the current-user installation checks. Attach the completed record to the release decision.

## Test record

- Tester and date: ____________________
- Windows version, edition, build, and x64 architecture: ____________________
- FK Chromium release tag (`<chromium-version>-fk.<revision>`): ____________________
- Upstream tag: ____________________
- Build workflow run URL: ____________________
- Installer filename: ____________________
- Installer SHA-256 from `SHA256SUMS.txt`: ____________________
- Independently calculated installer SHA-256: ____________________
- Portable filename: ____________________
- Portable SHA-256 from `SHA256SUMS.txt`: ____________________
- Independently calculated portable SHA-256: ____________________
- Installer outcome: PASS / FAIL — notes: ____________________
- Upgrade outcome: PASS / FAIL / NOT APPLICABLE — notes: ____________________
- Portable outcome: PASS / FAIL — notes: ____________________
- User data isolation outcome: PASS / FAIL — notes: ____________________
- Uninstall outcome: PASS / FAIL — notes: ____________________

## Artifact and install checks

- [ ] Download the installer, portable archive, and `SHA256SUMS.txt` from the same release tag.
- [ ] Recalculate both artifact SHA-256 values and confirm exact matches before executing either artifact.
- [ ] Record whether a Microsoft Defender SmartScreen warning appeared: YES / NO — notes: ____________________
- [ ] If a warning appeared, confirm it described an unsigned or unknown-publisher installer; if no warning appeared, record that outcome without treating it as a failure. Do not record the build as signed or automatically trusted.
- [ ] Start `FK-Chromium-<version>-Windows-x64-Installer.exe` and complete the current-user install.
- [ ] Confirm the expected default executable path `%LOCALAPPDATA%\FK Chromium\Application\chrome.exe`, or record the actual selected install path.
- [ ] Confirm Apps & features / Installed apps displays `FK Chromium` and the selected FK icon.
- [ ] Launch the installed browser and open a local page and a new tab.

## Upgrade and shell integration

- [ ] When a previous FK Chromium release is available, create a temporary profile marker, install the newer release over it, and confirm the browser version changes while the marker remains.
- [ ] Confirm the Start Menu entry is named `FK Chromium`, uses the FK icon, and launches the installed executable.
- [ ] If desktop shortcut creation was selected, confirm the desktop shortcut is named `FK Chromium`, uses the FK icon, and launches correctly.
- [ ] Pin and launch FK Chromium from the taskbar; confirm the FK icon and window grouping remain distinct from Chrome, Chromium, and ungoogled-chromium.
- [ ] Open the About page and confirm it shows `FK Chromium` and the expected Chromium version.

## User data isolation

- [ ] Record the actual installed user data directory: ____________________
- [ ] Confirm the expected default `%LOCALAPPDATA%\FK Chromium\User Data`, or explain any intentional difference.
- [ ] Confirm a new FK Chromium profile does not read, change, or overwrite Chrome, Chromium, or ungoogled-chromium profiles.
- [ ] Restart FK Chromium and confirm its own profile data persists.

## Portable package

- [ ] Extract `FK-Chromium-<version>-Windows-x64-Portable.zip` into a new directory without installing it.
- [ ] Confirm the archive contains `chrome.exe` and its required runtime files.
- [ ] Start the portable browser directly from the extracted directory, open a local page, and create a new tab.
- [ ] Record the portable data location and confirm it remains in the FK Chromium namespace rather than another Chromium-family browser's profile: ____________________

## Uninstall and optional cleanup

- [ ] Start uninstall from Windows Installed apps and confirm the uninstaller clearly identifies `FK Chromium`.
- [ ] Leave any personal-data deletion option **unselected**; confirm uninstall removes application files and shortcuts without deleting user data unless the user explicitly opts in.
- [ ] Confirm the default application path is removed and the independent user data directory remains.
- [ ] Confirm Chrome, Chromium, and ungoogled-chromium installations and profiles remain unchanged.
- [ ] **Optional, destructive, opt-in only:** after recording all results and obtaining the test user's consent, select the uninstaller's user-data deletion option or manually remove only the previously recorded FK Chromium test profile. Record exactly what was deleted: ____________________

## Release decision

- Blocking failures and links: ____________________
- Residual unsigned-build / SmartScreen observations: ____________________
- Approved for release by and date: ____________________
