# Arcana for macOS

**[Download the Mac app](https://github.com/drlatham18/arcana-macos/releases/latest)** · [Direct download: Arcana 1.2](https://github.com/drlatham18/arcana-macos/releases/download/v1.2.0/Arcana-1.2-universal.dmg)

This repository contains the Arcana 1.2 source, native Mac shell, offline engine, tests, and build scripts. Packaged installers are under Releases.

A self-contained, offline Magic: The Gathering Arena companion. Includes a compact floating analysis window, a full battlefield dashboard, rules search, personal notes, and automatic post-game reviews.

## Install and play

1. Open `Arcana-1.2-universal.dmg` and drag **Arcana** into **Applications**. Or unzip the app archive and move Arcana.app into Applications.
2. Open Arcana. It includes its own runtime; Python, Homebrew, an API key, and a subscription are not needed.
3. In Arena, enable **Settings → View Account → Detailed Logs (Plugin Support)**. Restart Arena and play a game.
4. Arcana normally finds the game log and card database automatically. Use **Connection** to select the files if your installation is elsewhere.
5. Click **Compact** to collapse to a 360 × 520 floating window. Resize it as small as 300 × 280, drag it beside the game, and expand with the upper-right button. **Command-Shift-M** toggles between the two views. The arrow toggles whether it floats above other windows. The ◎ button toggles Follow Arena, enabled by default. Bringing Arena forward automatically opens the compact companion; it follows the game across desktops, full screen, and displays without taking keyboard focus. It sits beside the game when space permits, otherwise inside its right edge. Drag it to adjust its position. Closing the panel pauses automatic showing until you reopen Arcana.

The compact view puts **If you want one action** first, followed by numbered move options with an **If you choose this** explanation. Board details expand below the choices. Suggestions are simple starting points, not predictions of the strongest play. New decisions return the panel to the top; routine refreshes preserve your place. Stale feeds hide the suggestion and label the old options.

The compact view changes with the phase: incoming attackers and available defenders during blocking, creatures ready to attack during attacking, stack contents, and timing options when you have priority. It distinguishes visible candidates from confirmed legal plays.

Closing the window leaves Arcana watching in the menu bar. Click its sparkle to reopen it. **Command-Q** quits the app and its engine. Watching does not require screen-recording or Accessibility permissions. It does not play the game or click for you.

Try **Explore demo** to inspect sample game states without Arena. Sample data is clearly marked and does not become match history. The real watcher continues separately if Arena is running.

## Sharing

Send **Arcana-1.2-universal.dmg** or **Arcana-1.2-universal.zip** to another Mac user. Both Apple Silicon and Intel are included; macOS 13 or newer is required. The build was run on Apple Silicon; the Intel launcher and runtime are included but have not been tested on physical Intel hardware.

This build is **ad-hoc signed, not Apple-notarized**. A recipient may see an unidentified-developer warning. If they trust their copy, Apple's documented per-app opening flow is **System Settings → Privacy & Security → Open Anyway**, after attempting to open it. No system-wide security setting needs to be disabled. See https://support.apple.com/en-us/102445.

For a smoother public release, the owner can sign the app and its nested executables with an Apple Developer ID, enable the hardened runtime with appropriate Python entitlements, notarize the distribution, and staple the ticket. No Developer ID identity was available when this build was made.

Sharing the app or source archive does not include personal data. Notes, preferences, and recorded reviews live in `~/Library/Application Support/Arcana/`. Export only a chosen match through **Match history → Export review** when you want to share that review.

## Rules and limitations

The bundled Comprehensive Rules snapshot is dated **August 7, 2026**, with Arena guides from the original project. The library is a dated offline snapshot; it is not an automatically updated source of bans, prices, or current card changes. Use **Official rules** to reach Wizards' current source.

Arcana uses a rule-based engine and local search, not a general-purpose AI model. Reviews use limited heuristics and cannot evaluate all complex card interactions or hidden information. Unknown rules text prevents confident combat counterfactuals. A move receiving no criticism is not necessarily optimal. Opponent hand contents are not inferred.

This Mac edition targets Arena. The original project's Windows-only Magic Online accessibility reader and OCR interfaces are not included. The live connection has automated coverage using synthetic Arena records. The installed app also discovered this Mac's Steam Arena log and card database and displayed a recorded board. A complete live match has not been validated end to end.

## Build from source

`Sources/Arcana.swift` is the native AppKit/WebKit shell. `Resources/engine` contains the offline engine. `Resources/web` contains the local interface. The shell uses private stdin/stdout messages, not a local network server. Remote navigation and web requests in the interface are blocked; explicit external links open in the user's browser.

Clone this repository, then run with Python 3 and Apple's Command Line Tools:

```sh
git clone https://github.com/drlatham18/arcana-macos.git
cd arcana-macos
python3 scripts/build.py
```

The script obtains checksum-pinned Python 3.12.14 runtimes from Astral's python-build-standalone release `20260901` when absent, builds both native architectures, packages the app, and ad-hoc signs the nested binaries. Output: `dist/Arcana.app`.

After the runtime download, run tests:

```sh
PYTHONPATH=Resources/engine vendor/arm64/python/bin/python3 -m unittest discover -s tests -v
```

Run the native placement checks with:

```sh
xcrun swiftc Sources/ArenaPlacement.swift tests/ArenaPlacementTests.swift -o /tmp/arcana-placement-tests
/tmp/arcana-placement-tests
```

Use the `x86_64` runtime on an Intel Mac. Run `python3 scripts/package.py` to produce sharing archives and a disk image after building.

Original core: https://github.com/drlatham18/mtgo-rules-companion at commit `570a4de2475be6e63c02a3eab319f62120f81f6c`. Ported with macOS file discovery, read-only database access, log-rotation recovery, private user storage, durable review history, and compact phase-aware analysis.

## Validation

52 automated tests cover the existing Arena reader/reviewer plus the Mac paths, log rotation and partial writes, read-only database access, service protocol, demo isolation, saved notes, review persistence, exact rule-number lookup, compact combat analysis, and move-choice presentation. The packaged app was visually exercised for launch, demo switching, chat, search, connection, collapse, and expansion. Version 1.1 adds seven native placement checks for side placement, full screen, multiple display coordinates, dragged offsets, and keeping the companion on screen. Arena relaunch was checked on this Mac: Arcana automatically switched into its floating compact panel. Code signatures and distribution checksums were verified.

See `THIRD_PARTY_NOTICES.md` for attribution.
