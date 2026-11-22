# Freemium

Chromium for Windows (x64 and arm64) that sends no network request without an explicit user
action: no Google services, no telemetry, no background pings.

It is built from the Chromium release (currently 157.0.8103.78) with a reviewed selection of
[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) patches, a few
own patches and GN args, and checked by an audit of the source and of the running browser.
The browser itself is still called Chromium; there is no installer and no auto-update.

## What is changed

- **Upstream patches** ([patches/series](patches/series), [patches/series.windows](patches/series.windows)):
  every privacy, Google and telemetry patch of ungoogled-chromium and ungoogled-chromium-windows,
  plus the ungoogled chrome://flags block. [patches/UPSTREAM.md](patches/UPSTREAM.md) records
  why each upstream patch is taken or skipped. They are fetched from the pinned upstream
  repositories at build time, not copied here.
- **GN args** ([args/](args)): Safe Browsing, Reporting/NEL, field trial configs, remoting,
  mDNS/service discovery and the on-device model service compiled out; no Google API keys.
- **Own patches** ([patches/own/](patches/own)), each with a description at its top:
  - `os-dns-and-no-system-proxy.patch`: names are resolved only by the OS resolver; the OS proxy
    settings (and WPAD) are ignored.
  - `disable-unwanted-features.patch`: AI features, Privacy Sandbox ad APIs, in-product help
    and other nags off.
  - `pref-defaults.patch`: Do Not Track and Global Privacy Control on, session restore, one
    profile only, no Web Store tile, no e-mail verification.
  - `remove-profile-and-password-ui.patch`: no profile and password manager UI, no
    "You and Google".

## Audit

[audit/](audit) looks for anything that can still send requests on its own:

- a static pass over the Chromium source: every traffic annotation, unannotated request and
  URL literal in the build graph is triaged by a local model (llama.cpp, no code leaves the
  machine), checked against verified gates (code paths that are dead in this build) and
  decided in `decisions.toml`; the same for enabled-by-default features and pref defaults;
- a runtime check: `netlog.py` lists every host the browser contacted in a NetLog, idle and in
  everyday use, separating browser-initiated requests from requests made for a page.

See [audit/README.md](audit/README.md).

## Downloads and verification

Releases are built by GitHub Actions ([.github/workflows/release.yml](.github/workflows/release.yml))
on GitHub-hosted runners, and the executables are Authenticode-signed with a key kept on a
hardware token ([github-codesign](https://github.com/mfilippov/github-codesign)). Each release
has a portable zip per CPU and `SHA256SUMS`. Unpack it anywhere and run `chrome.exe`.

To check that a zip was built by this repository's workflow:

```
gh attestation verify chromium_<version>_<commit>_windows_x64.zip --repo mfilippov/freemium
```

`chrome.exe` and the other executables carry an Authenticode signature by Mikhail Filippov
(file Properties → Digital Signatures, or `Get-AuthenticodeSignature chrome.exe`).

## Building locally

Requirements: Windows, Git for Windows, Visual Studio 2026 with the C++ desktop workload,
the Windows SDK version Chromium pins (10.0.28000.0 for 157), ~100 GB of disk.

```bat
chromium-build-env.cmd
rem in build\chromium (first time only):
fetch --nohooks chromium
cd src
git checkout 157.0.8103.78 && gclient sync -D

python3 ..\..\..\apply-patches.py
python3 ..\..\..\apply-patches.py --args out/Release
gn gen out/Release
autoninja -C out/Release chrome
```

Then `uv run --no-project python package.py` packs `build/dist/chromium_<version>_<commit>_windows_x64.zip`.
For arm64 use `--args out/Arm64 --cpu arm64` and `package.py --arm64`.
`apply-patches.py --reset` reverts the patches before switching versions.

The CI build does the same in stages ([ci/stage.py](ci/stage.py)), since one GitHub-hosted job
runs at most 6 hours.

## License

The scripts, own patches and documents in this repository are under the Apache License 2.0
([LICENSE](LICENSE)). Chromium, whose code the patches change, and ungoogled-chromium, whose
patches are fetched at build time, are under the BSD 3-Clause license.
