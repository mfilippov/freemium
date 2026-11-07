# Upstream patches: what we take and why

Upstream: ungoogled-chromium 157.0.8103.38-1 (108 patches) and ungoogled-chromium-windows
f85bc554 (26 patches). Pinned in `apply-patches.py` (SOURCES). Our series keeps upstream order.

Build goal: no network request without an explicit user action, no Google services, no
telemetry. UI features and chrome://flags knobs are optional. GN args
(`args/common.gn`) already compile out Safe Browsing (`safe_browsing_mode=0`), Reporting/NEL,
field-trial configs, mDNS/service discovery, remoting, Hangouts extension, on-device model
service, and drop all Google API keys.

Rules of thumb used:
- Take anything that removes or prevents a request, a Google contact or telemetry, and anything
  required to build with our GN args.
- Take the ungoogled flag patches as one block: they only add off-by-default knobs, and their
  hunks chain textually (each appends to `ungoogled_flag_entries.h` after the previous one), so
  partial sets do not apply without local fixups.
- Skip cosmetic/branding patches, toolchain workarounds for ungoogled's no-depot_tools build,
  and patches that only matter with domain substitution or binary pruning (see "Mechanisms").

## Taken

### Safe Browsing removal ([sb], goes with `safe_browsing_mode=0`)
- `core/inox-patchset/0001-fix-building-without-safebrowsing.patch`
- `core/ungoogled-chromium/fix-building-without-safebrowsing.patch`
- `core/ungoogled-chromium/remove-unused-preferences-fields.patch` (also drops sign-in prefs)
- `core/ungoogled-chromium/move-js-optimizer-unfamiliar-sites.patch`
- `core/iridium-browser/safe_browsing-disable-incident-reporting.patch`: incident uploads off,
  error-page report links to `trk:`; mostly dead at sb=0, `safe_browsing_loud_error_ui.cc` is live.
- `core/iridium-browser/safe_browsing-disable-reporting-of-safebrowsing-over.patch`: dead code
  at sb=0, kept as insurance should the GN arg ever change.
- Windows: `windows-fix-building-without-safebrowsing.patch`,
  `windows-fix-remove-unused-preferences-fields.patch`.

### Telemetry / uploads
- `core/inox-patchset/0003-disable-autofill-download-manager.patch`: autofill crowdsourcing.
- `core/inox-patchset/0021-disable-rlz.patch` + `core/ungoogled-chromium/disable-untraceable-urls.patch`
  + Windows `windows-disable-rlz.patch`: RLZ library compiled out (financial ping to
  clients1.google.com); the Windows patch removes setup.exe's dependency on it.
- `core/ungoogled-chromium/disable-crash-reporter.patch`: Crashpad not started on Win/Mac,
  upload URL (clients2.google.com/cr/report) blanked.
- `core/ungoogled-chromium/disable-domain-reliability.patch`
- `core/ungoogled-chromium/disable-webrtc-log-uploader.patch`
- `core/ungoogled-chromium/remove-navigation-source-param.patch`: tracking param on omnibox navigations.
- `core/bromite/disable-fetching-field-trials.patch`: variations seed (with `disable_fieldtrial_testing_config`).

### Google services and URLs
- `upstream-fixes/missing-dependencies.patch`: GN deps the patches below rely on.
- `core/inox-patchset/0005-disable-default-extensions.patch`: Web Store hosted app, in-app
  payments component extension, Web Store CRX download path.
- `core/inox-patchset/0015-disable-update-pings.patch`: chrome/updater update URL emptied
  (relevant to the macOS bundle; one line).
- `core/ungoogled-chromium/disable-google-host-detection.patch`
- `core/ungoogled-chromium/replace-google-search-engine-with-nosearch.patch`
- `core/ungoogled-chromium/toggle-translation-via-switch.patch`
- `core/ungoogled-chromium/disable-profile-avatar-downloading.patch`
- `core/ungoogled-chromium/disable-gcm.patch`
- `core/ungoogled-chromium/disable-gaia.patch`
- `core/ungoogled-chromium/disable-fonts-googleapis-references.patch`
- `core/ungoogled-chromium/disable-webstore-urls.patch`: extension auto-update to
  clients2.google.com (ExtensionDownloader, not covered by block-requests), Web Store origin
  handling, OAuth credentials on google.com fetches.
- `core/ungoogled-chromium/disable-ai.patch` (with `use_on_device_model_service=false`)
- `core/ungoogled-chromium/disable-network-time-tracker.patch`
- `core/ungoogled-chromium/disable-mei-preload.patch`: Google-curated Media Engagement preload
  blob dropped from the build.
- `core/ungoogled-chromium/block-requests.patch`: OGB, NTP backgrounds/promos, affiliation,
  hunspell download, translate ranker, component update checks, devtools css data.
- `core/ungoogled-chromium/disable-privacy-sandbox.patch`: attestations preload component
  dropped from the macOS bundle (Windows only gets it via the blocked component updater).
- `extra/inox-patchset/0008-restore-classic-ntp.patch`: no remote NTP.
- `extra/ungoogled-chromium/remove-uneeded-ui.patch`: sign-in / AI / Google links in UI.
- `extra/ungoogled-chromium/prepopulated-search-engines.patch`,
  `extra/ungoogled-chromium/disable-ai-search-shortcuts.patch`: AI omnibox shortcuts.
- `extra/iridium-browser/updater-disable-auto-update.patch`: Keystone check interval 0 (macOS
  file; applies everywhere, taken now so the macOS build needs no re-review).
- `extra/ungoogled-chromium/bundle-hyphenation-patterns.patch`: needed because the component
  updater is blocked.

### Requests without user action (non-Google)
- `core/ungoogled-chromium/block-trk-and-subdomains.patch`: `trk:` scheme and `qjz9zk` hosts
  blocked at the URLRequest level with a notification. Safety net for anything missed, makes the
  `trk:` URLs in other patches fail loudly, prerequisite for domain substitution.
- `core/ungoogled-chromium/doh-changes.patch`: Secure DNS default Off instead of Automatic (no
  silent upgrade to dns.google), Google DoH providers removed, DNS probe host google.com ->
  example.com.
- `core/ungoogled-chromium/remove-f1-shortcut.patch`: F1 opened support.google.com.
- `extra/ungoogled-chromium/disable-intranet-redirect-detector.patch`: random-host DNS lookups at startup.
- `extra/ungoogled-chromium/disable-dial-repeating-discovery.patch`: periodic SSDP multicast.
- `extra/ungoogled-chromium/add-ipv6-probing-option.patch`: IPv6 probe toward Google DNS removed (+ flag).
- `extra/ungoogled-chromium/disable-fedcm-by-default.patch`: sites cannot trigger IdP fetches by default.
- `extra/iridium-browser/browser-disable-profile-auto-import-on-first-run.patch`: no silent
  import of other browsers' data / master_preferences extensions on first run.

### Defaults and fingerprinting
- `extra/inox-patchset/0006-modify-default-prefs.patch`: safer defaults, password manager off.
- `extra/ungoogled-chromium/default-webrtc-ip-handling-policy.patch`: `disable_non_proxied_udp`
  (no local/VPN IP leak; P2P calls relay via TURN; changeable via the flag below).
- `extra/inox-patchset/0019-disable-battery-status-service.patch`: Battery API returns constants.
- `extra/debian/disable/google-api-warning.patch` + `extra/inox-patchset/0013-disable-missing-key-warning.patch`:
  the two halves of the "API keys missing" infobar.

### Flags block (all off by default; take as a whole, see rules above)
Infrastructure: `extra/ungoogled-chromium/add-ungoogled-flag-headers.patch`,
`extra/ungoogled-chromium/add-components-ungoogled.patch`,
`extra/ungoogled-chromium/add-flags-for-existing-switches.patch` (own headers; exposes
`webrtc-ip-handling-policy` and other existing switches).

Privacy / request shaping: `extra/bromite/fingerprinting-flags-client-rects-and-measuretext.patch`,
`extra/bromite/flag-fingerprinting-canvas-image-data-noise.patch`,
`add-flag-to-spoof-webgl-renderer-info`, `add-flag-to-reduce-system-info`,
`add-flag-to-remove-client-hints`, `add-flags-for-referrer-customization`,
`add-flag-to-disable-tls-grease`, `add-flag-to-change-http-accept-header`,
`add-flag-for-search-engine-collection`, `add-flag-to-clear-data-on-exit`,
`add-flag-for-omnibox-autocomplete-filtering`, `add-flag-to-configure-extension-downloading`;
Windows: `windows-disable-encryption`, `windows-disable-machine-id` (portable profile, no metrics machine id).

Security: `add-flag-to-force-punycode-hostnames`, `add-flag-for-disabling-jit` (note: also
flips the GN default `v8_enable_drumbrake`, which is why `v8_drumbrake_bounds_checks=true` is
in args), `enable-certificate-transparency-and-add-flag` (mostly inert: the baked-in CT log
list expires after 70 days without component updates).

UI knobs carried because they sit inside the chain: `flag-max-connections-per-host`,
`add-flag-to-disable-beforeunload`, `add-flag-to-show-avatar-button`,
`add-flag-to-hide-crashed-bubble`, `add-flag-to-scroll-tabs`, `add-flag-for-bookmark-bar-ntp`,
`add-flag-to-close-window-with-last-tab`, `add-flag-to-convert-popups-to-tabs`,
`add-flag-to-disable-local-history-expiration`, `add-flag-for-qr-generator`,
`add-flag-for-grab-handle`, `add-flag-for-close-confirmation`, `add-flag-for-custom-ntp`,
`add-flag-for-tab-hover-cards`, `add-flag-to-hide-tab-close-buttons`,
`add-flag-for-disabling-link-drag`, `add-flag-to-hide-extensions-menu`,
`add-flag-to-hide-fullscreen-exit-ui`, `add-flag-for-incognito-themes`,
`add-flag-to-increase-incognito-storage-quota`, `add-flag-to-hide-window-buttons`.

If the block is ever dropped, the verified minimal subset is `add-ungoogled-flag-headers` +
`add-ipv6-probing-option` + `add-flags-for-existing-switches`.

## Skipped

### Only needed with domain substitution / binary pruning (not applied to our tree)
- `core/ungoogled-chromium/fix-learn-doubleclick-hsts.patch` (domain substitution)
- `core/ungoogled-chromium/fix-building-with-prunned-binaries.patch` (pruning; its
  `use_on_device_model_service=false` default is set in our GN args)
- Windows `windows-disable-win-build-output.patch`, `windows-compile-mini-installer.patch` (pruning)

### Cosmetic, branding, convenience (no request or data effect)
- `core/ungoogled-chromium/extensions-manifestv2.patch`: keep MV2 loadable, hide deprecation UI.
- `extra/iridium-browser/mime_util-force-text-x-suse-ymp-to-be-downloaded.patch`
- `extra/iridium-browser/prefs-always-prompt-for-download-directory-by-defaul.patch`
- `extra/iridium-browser/Remove-EV-certificates.patch`
- `extra/ungoogled-chromium/disable-formatting-in-omnibox.patch`
- `extra/ungoogled-chromium/add-suggestions-url-field.patch`
- `extra/ungoogled-chromium/enable-paste-and-go-new-tab-button.patch`
- `extra/ungoogled-chromium/enable-menu-on-reload-button.patch`
- `extra/ungoogled-chromium/add-extra-channel-info.patch`, `extra/ungoogled-chromium/first-run-page.patch`
  (ungoogled branding)
- `extra/ungoogled-chromium/keep-expired-flags.patch`
- `extra/ungoogled-chromium/enable-extra-locales.patch`
- `extra/ungoogled-chromium/remove-pac-size-limit.patch`
- `extra/ungoogled-chromium/add-credits.patch` (reconsider if binaries are redistributed: BSD attribution)

### Security trade-offs we do not want
- `extra/ungoogled-chromium/disable-download-quarantine.patch`: removes Mark-of-the-Web.
- Windows `windows-disable-download-warning-prompt.patch`: every download marked not dangerous.

### Linux-only, revisit for the Linux build
- `extra/inox-patchset/0016-chromium-sandbox-pie.patch`
- `extra/ungoogled-chromium/remove-disable-setuid-sandbox-as-bad-flag.patch`

### Not needed with our toolchain / tree
- `extra/ungoogled-chromium/fix-building-without-mdns-and-service-discovery.patch`: 157 already
  guards the code; our build with `enable_mdns=false`, `enable_service_discovery=false` succeeded.
- Windows toolchain workarounds for ungoogled's no-depot_tools build (system LLVM/Rust, MS
  rc.exe, GN from source): `windows-disable-reorder-fix-linking`, `windows-disable-rcpy`,
  `windows-fix-building-gn` (files absent), `windows-no-unknown-warnings`, `windows-fix-rc`,
  `windows-fix-clang-format-exe`, `windows-fix-command-ids`, `windows-fix-rc-terminating-error`,
  `windows-disable-clang-version-check`, `windows-fix-licenses-gn-path`,
  `windows-fix-building-with-rust`, `windows-fix-complex-builtins`, `windows-fix-missing-includes`,
  `windows-fix-unsupported-llvm-flags`, `windows-fix-building-without-crubit`, `windows-fix-python`,
  `windows-fix-typescript-lib-dom` (already in the 157 tree).
- Windows `windows-disable-event-log.patch`: local Event Log source only; was a build workaround.

## Mechanisms not applied

- Domain substitution (`domain_substitution.list`, `domain_regex.list`): not applied. Planned as
  a verification step in the audit build together with `block-trk-and-subdomains`; promote to
  release only after it shows no hits. See build/review/patch-review.md, section 4.
- Binary pruning (`pruning.list`): not adopted; it forces the no-depot_tools toolchain. The two
  Google data blobs that reach the product are removed by `disable-mei-preload` and
  `disable-privacy-sandbox` instead.
