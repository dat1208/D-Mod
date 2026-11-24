# D-Mod

A League of Legends skin changer with a desktop UI. Built on the [Rose](https://github.com/Alban1911/Rose) engine, with party mode and analytics removed and a Tauri interface added.

This file is also the orientation document for anyone (human or model) picking up the codebase. Read it before reviewing or changing anything: it records the architecture, the startup order, the invariants that are easy to break, and the traps that have already been hit.

## Skin health (1.0.0)

Every installed skin is classified from its archive before the queue, so a
player learns what will fail without losing a game to it:

| Verdict | Meaning | What the app does |
|---|---|---|
| `blocked` | LTK Manager refuses the archive (`c0000229`, skinhack rule) | Marks the skin red and refuses the click in the picker |
| `no_audio` | The archive declares a skill-sound bank it never stores | Marks the skin amber, still selectable, warns on use |
| `ok` | Neither applies | Nothing |

`blocked` is fatal: the overlay is never built, so the skin never appears.
`no_audio` is a warning: the skin loads but plays with the base events bank,
which leaves skill sounds silent.

**The audio problem cannot be repaired by this tool.** Rose's packer wrote
`META/rose_sounds.json` and copied the skin's SFX bank at runtime; LTK Manager
only overlays files that are already in the archive. The source bank is absent
from the mod, and scanning every WAD in the installed client found no copy of
it either. D-Mod therefore reports the problem and refuses to write into game
files: a half-written bank corrupts the client, and the missing bank cannot be
invented.

Measured on the 14 installed Zed skins: 2 blocked (`238013`, `238068`),
11 without skill audio, 1 clean (`238002`, an LTK-native archive).

---

## Important: read before installing

D-Mod injects into the League of Legends process. Riot's Terms of Service
prohibit third-party software that modifies the game, and **using this tool can
get your account banned**. There is no guarantee of safety, and the authors
accept no liability for account action. Use at your own risk, preferably on an
account you can afford to lose.

This build is not code-signed, so Windows SmartScreen will warn you the first
time you run the installer.

## Install

1. Download **`D-Mod_x.x.x_x64-setup.exe`** from [Releases](https://github.com/dat1208/D-Mod/releases/latest).
2. Run it. Windows may warn about an unknown publisher (the app is not code-signed): choose **More info â†’ Run anyway**.
3. Launch **D-Mod** from the Start menu and accept the UAC prompt. The app needs administrator rights to inject into the game.

The engine fetches the LTK patcher and skin data on first launch; the UI shows the progress.

## Use

1. Open the League client and the D-Mod window.
2. In champion select, pick your champion. The chroma panel appears in the client.
3. Choose a skin. It is applied when the game loads.

The **Profiles** tab keeps one set of skin choices per League account. **Diagnostics** shows upstream versions and any problem the engine hit. **Logs** shows the live engine output.

## Updates

The app checks for a new release on startup and shows a banner when one is available. Click **Update now** to install it and restart. No manual reinstall.

## Requirements

- Windows 10/11 (64-bit)
- League of Legends installed
- Administrator rights (for injection)

---

# Architecture

## Repository layout

```
D-Changer/
â”œâ”€â”€ backend/                  Python engine (bundled by PyInstaller as a Tauri sidecar)
â”‚   â”œâ”€â”€ main/                 entry point, startup, main loop, cleanup
â”‚   â”œâ”€â”€ launcher/             startup checks + self-update
â”‚   â”œâ”€â”€ updater/              pinned third-party binaries + upstream version checks
â”‚   â”œâ”€â”€ injection/            the actual skin injection engine
â”‚   â”œâ”€â”€ lcu/                  League Client API client (lockfile, HTTP, websocket)
â”‚   â”œâ”€â”€ pengu/                websocket bridge to the in-client JS plugins
â”‚   â”œâ”€â”€ threads/              champion-select phases, locks, timers, injection trigger
â”‚   â”œâ”€â”€ profiles/             per-account skin profiles
â”‚   â”œâ”€â”€ state/                shared app status
â”‚   â”œâ”€â”€ utils/                paths, logging, download, atomic file helpers
â”‚   â”œâ”€â”€ api.py                local JSON API the Tauri UI polls
â”‚   â”œâ”€â”€ startup_progress.py   progress sink shared by launcher and API
â”‚   â”œâ”€â”€ config.py             constants (APP_NAME, APP_VERSION, repos, paths)
â”‚   â”œâ”€â”€ D-Changer.spec        PyInstaller build definition
â”‚   â””â”€â”€ Pengu Loader/plugins/ JS plugins injected into the League client
â”œâ”€â”€ ui/                       Tauri + React front end
â”œâ”€â”€ scripts/build_dchanger.py end-to-end build (engine â†’ UI â†’ installer)
â””â”€â”€ .github/workflows/        ci.yml, release.yml
```

## Two halves that must agree

The app is a Python engine plus a React UI talking over a **local JSON API** (`backend/api.py`, polled by the UI). The engine also injects JS plugins into the League client through Pengu Loader.

Three separate processes are involved at runtime:

| Process | What it is | Who owns it |
|---|---|---|
| `dchanger-backend.exe` | the Python engine (PyInstaller onedir) | Tauri, as a sidecar |
| the Tauri window | React UI | the user |
| `LeagueClientUx.exe` | the game client, with Pengu Loader + Rose plugins inside | Riot |

The engine and the plugins share state through `%LOCALAPPDATA%\D-Changer\` and the Pengu websocket bridge (`backend/pengu/`).

## Startup order

`backend/main/__init__.py:main()` runs this sequence. The order matters; each step explains why.

1. **Elevate.** `request_admin_elevation()` relaunches the process elevated and exits the current one. `--dev` skips it.
   - Why: enabling auto-start registers a Task Scheduler task with `/rl highest`, which needs admin, and the launcher cannot prompt for it later without losing the user's settings change.
   - The LTK patcher does **not** need elevation: it is downloaded into the user's tools folder, not installed per machine.
2. **Start the local API** (`ApiServer`) so the UI can display startup progress.
3. **Run the launcher** (`launcher.run_launcher`), which reports progress to `PROGRESS`.
   - Update check first (a broken build must still be able to update itself).
   - Then three independent checks in parallel threads: hashes, patcher, skins. Startup takes the slowest, not the sum.
   - The patcher step runs in dev mode too; the other two are skipped there.
4. **Verify the DLL is present**, then hand off to `run_league_unlock` (the main loop).

## The launcher

`backend/launcher/` has two entry shapes:

- **Headless** (`_run_launcher_headless`): used by the real app. Reports to `startup_progress.PROGRESS`, which the UI polls through `/api/status`. This is the path that matters.
- **Win32 dialog** (`_run_launcher_dialog`): legacy, kept for running the engine without the UI.

Sequences live in `backend/launcher/sequences/`:

| Sequence | Does | Fails fatally? |
|---|---|---|
| `HashCheckSequence` | downloads `hashes.game.txt` from CommunityDragon | no |
| `PatcherSequence` | fetches the pinned LTK patcher | no |
| `SkinSyncSequence` | syncs the skin library from the LeagueSkins repo | no |

All three are non-fatal by contract: startup continues and the injection system reports the missing piece when it needs it.

## Self-update

`backend/launcher/update/`:

1. `github_client.py` asks the GitHub API for the latest release of `SELF_UPDATE_REPO`.
2. `update_sequence.py` downloads the release ZIP, **verifies it against the digest GitHub publishes for the asset** (`updater/verify.digest_of_asset`), extracts it to a staging dir, and optionally fetches `hashes.game.txt`.
3. `update_installer.py` writes a batch script that waits for the app to exit, `robocopy /MIR`s the staging tree over the install, then restarts the app.

Notes that are easy to get wrong:

- The ZIP check is not optional decoration. Before it existed, a tampered or truncated release was extracted and executed. `verify_file` accepts `None`, which is what GitHub returns for older assets; do not "simplify" the call away.
- `robocopy` excludes `config.ini`, `ltk_patcher_host.exe`, `ltk_patcher_dll.dll` and `hashes.game.txt`, so an update never overwrites the user's settings or the verified patcher pair.
- There is **no rollback**. A `robocopy` failure leaves a half-updated install. This is a known gap, not an oversight.
- The Tauri updater (`ui/src-tauri/tauri.conf.json`) is a separate mechanism pointing at `latest.json` on the release; the Python updater is what the engine uses.

## The LTK patcher (pinned fetch)

`backend/updater/ltk_patcher.py` is the only place the patcher binaries come from.

- `PIN_COMMIT` + `PIN` hold the commit, SHA-256 and size for `ltk_patcher_host.exe` and `ltk_patcher_dll.dll`. **One table, no duplication.**
- Files come from `raw.githubusercontent.com/LeagueToolkit/ltk-manager/<commit>/src-tauri/resources/<name>`.
- Each download goes to a temp file in the target directory, is checked for size, SHA-256 and (on Windows) Authenticode `Valid`, then moved in with `os.replace`. A failed download leaves the existing pair untouched.
- `fetch_patcher` compares the **installed pair against the pin**, not just its presence. A pair left by an older build is replaced on the next start. This was a real bug: presence-only checking meant an install kept a stale DLL forever.
- `_verifies_host` only logs a warning; the pin comparison decides what is installed.
- **Do not move the pin to a newer commit without testing an injection.** From `46198022` onward the DLL checks the process that started the game against a hardcoded allowlist (`ltk_patcher_host.exe`, `ltk-manager.exe`, `explorer.exe`) and refuses the overlay with `host verification failed, refusing overlay: [VRF00000008]` when the parent is anything else. D-Mod's engine spawns the host itself, so the parent is `dchanger-backend.exe`, which is not on that list: every injection fails and the game runs with the stock skin. The pinned pair (`08468a32`) has no such check. Its end-of-life date is 2026-10-18, already past, so it works only until League ships a build newer than that.
- `patcher_is_stale` compares the DLL's built-in end-of-life date against the **game executable's build timestamp**, not the wall clock. A DLL past its date keeps working until League itself updates.

The patcher is **not bundled** (`D-Changer.spec` excludes it). A shipped copy would only go stale and could shadow the verified one.

**Maintenance:** the DLL has an end-of-life date. When it passes, update `PIN_COMMIT` and both hashes after re-downloading and re-verifying by hand, and test an actual injection before shipping: a newer DLL may refuse D-Mod as its host (see the host verification note above). Do not trust a value copied from an issue or a previous session.

Two things help here:

- **Startup warning.** `launcher/sequences/patcher_sequence.py` compares the installed DLL's end-of-life date against the game build on every start and reports `LTK_PATCHER_EOL` in Diagnostics when the game is newer, instead of leaving the user to discover it when a game loads with the stock skin.
- **`scripts/check_ltk_patcher.py`.** Walks the commits that touched `src-tauri/resources`, downloads each DLL, and reports the newest one that is Authenticode-signed, has no host verification check, and has a later end-of-life date than the pin. `.github/workflows/ltk-patcher-watch.yml` runs it daily and opens an issue when one exists. It never edits the pin: an automated check cannot prove a DLL injects.

## Injection

`backend/injection/` is the Rose engine, kept largely intact.

1. `injection/core/injector.py:inject_skin` orchestrates a single injection.
2. `injection/overlay/overlay_manager.py` builds the overlay with `mod-tools.exe` (`mkoverlay`) and serves it with `ltk_patcher_host.exe`.
3. `injection/game/game_monitor.py` suspends and resumes the game process so the overlay can be attached.
4. `injection/mods/` holds mod storage, the zip resolver and the `modpkg` format.
5. `injection/loadingname/` rewrites the loading-screen name (RST v5 + XXH3/XXH64).

The host/patcher pair speaks a line protocol; `rose_runoverlay_*.log` in the log folder is the ground truth when an overlay fails.

## Skin library

`backend/utils/download/repo_downloader.py` downloads one ZIP of the whole [LeagueSkins](https://github.com/Alban1911/LeagueSkins) repo and extracts it, rather than looping per champion. It records the commit SHA so an unchanged repo is skipped.

- Layout on disk: `<champion>/<skin>[/<chroma>]/<id>.zip|.fantome|.png`.
- Cleanup after a sync removes only files that match that layout (`_is_library_file`). Anything else in the skins folder â€” a user's own archive, `failed_skins.json` â€” is kept. This is deliberate: an earlier version deleted user files.
- `hashes.game.txt` is written with `atomic_write` because injection threads read it while startup runs.

## Data and paths

`backend/utils/core/paths.py` is the single source of truth. Do not re-derive paths elsewhere.

| Path | Contents |
|---|---|
| `%LOCALAPPDATA%\D-Changer\` | user data root |
| `â€¦\skins\` | extracted skin library, `failed_skins.json` |
| `â€¦\profiles\` | per-account profiles |
| `â€¦\logs\` | `rose_*.log`, `log_updater_*.log`, `rose_runoverlay_*.log` |
| `<install>\_internal\injection\tools\` | `mod-tools.exe`, `cslol-dll.dll`, `hashes.game.txt`, fetched patcher pair |

`get_bundle_dir()` derives from `sys.executable`, **never** from `sys._MEIPASS`: in a PyInstaller onedir build `_MEIPASS` points at the builder's `_internal`, so an installed copy would look for its files on the build machine.

`get_user_data_dir()` resolves the real desktop user, not the elevated admin account, so the engine and Pengu Loader (running inside League) agree on where data lives.

## Configuration

`backend/config.py` holds `APP_NAME`, `APP_VERSION`, repo names and `SELF_UPDATE_*`. `config.ini` in the install directory holds user settings and is preserved across updates.

`APP_VERSION` in `config.py` is the **engine** version and is not the app version. The app version lives in `ui/package.json`, `ui/package-lock.json`, `ui/src-tauri/tauri.conf.json`, `ui/src-tauri/Cargo.toml` and `ui/src-tauri/Cargo.lock` â€” all five must match the release tag.

---

# Development workflow

## Running locally

```powershell
cd backend
python main.py --dev --verbose --logs
```

`--dev` skips elevation and the hash/skin steps. The patcher step still runs. `--logs` opens the log folder.

To test the frozen build:

```powershell
cd backend
python -m PyInstaller D-Changer.spec --noconfirm --clean
.\dist\dchanger-backend\dchanger-backend.exe --dev --verbose --logs
```

`backend/dist/` and `backend/build/` are gitignored.

## Tests

```powershell
cd backend
python -m pytest -q --ignore=pengu/tests/test_activation_warning.py
```

Expected: **254 passed**.

The ignored file has a **pre-existing collection error** unrelated to any feature work: importing `main` runs `main/setup/console.py:setup_console`, which calls `SetConsoleScreenBufferSize(handle, COORD(...))` without declaring `argtypes`, so ctypes raises `ArgumentError: argument 2: TypeError: 'COORD' object cannot be interpreted as an integer`. Leave it alone unless you are fixing that specific bug.

Test suites live next to their package (`backend/<pkg>/tests/`) plus `backend/tests/` for cross-cutting ones. CI runs each suite with `unittest discover`, so a new suite directory is picked up automatically.

## Lint

```powershell
cd backend
python -m ruff check .
```

CI only enforces `E9,F63,F7,F82` (syntax errors and undefined names). The repo has a large backlog of style findings (`BLE001`, `UP045`, `UP009`, `I001`) in untouched files. **Do not mass-fix them**: keep the files you touch clean and leave the rest alone, so a real change stays reviewable.

Broad `except Exception` is a deliberate pattern in the launcher and download paths; those carry an explicit `# noqa: BLE001` with a reason where it matters.

## Building and releasing

```powershell
python scripts/build_dchanger.py
```

Three stages: PyInstaller engine â†’ Vite UI â†’ Tauri bundle. The installer lands in `ui/src-tauri/target/release/bundle/nsis/`.

Releasing is tag-driven (`.github/workflows/release.yml`):

1. Bump the version in all five files listed under **Configuration**.
2. Commit `chore(release): bump the version to X.Y.Z` and push to `main`.
3. `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. The workflow builds, signs, publishes the release and writes `latest.json`.

CI (`.github/workflows/ci.yml`) runs on every push and PR to `main`: workflow security (actionlint, zizmor), Pengu plugin syntax, backend lint + tests, UI build + Rust check, and dependency review on PRs.

## Invariants worth not breaking

- **One pin table.** Patcher commit, hashes and sizes live only in `updater/ltk_patcher.py`.
- **Verify before install.** Every downloaded binary is checked against a pin or a published digest before it is used. Never add a path that runs unverified bytes.
- **Atomic writes.** Anything another thread may read while it is written goes through `utils/core/atomic_file.py`.
- **Non-fatal startup steps.** The launcher never blocks startup on a failed network step; it reports and continues.
- **Do not bundle the patcher.** It is fetched and verified at runtime.
- **Paths come from `paths.py`.** Especially `get_bundle_dir()`, which must not use `sys._MEIPASS`.
- **Preserve user data.** Cleanup routines only delete files the app itself wrote.
- **The UI is a separate process.** Engine state reaches it through `api.py` and `startup_progress.py`, not shared memory.

## Known gaps

These are real and deliberately not fixed yet:

- **No update rollback.** A failed `robocopy` leaves a half-updated install; the failed-update detector retries three times, then gives up.
- **Startup can block on network.** The update check, hash download and patcher fetch all run before the main loop, each with generous timeouts. On a bad connection startup can take minutes with only progress text as feedback.
- **Dead downloader code.** `utils/download/skin_downloader.py` (the `SkinDownloader` class) and `smart_skin_downloader.py` are unused; only `download_skins_on_startup` in the former is live, and it delegates to `repo_downloader`. `hashes_downloader.py` is likewise only re-exported.
- **`safe_extract` does not reject symlink entries.** Path traversal and absolute paths are blocked; a symlink entry would pass validation. Only relevant off Windows, where `zipfile` does not create symlinks by default.
- **Patcher DLL end-of-life.** The pinned DLL expires; after that date a League update breaks injection until the pin is refreshed. The app now reports this in Diagnostics at startup (before an injection can fail), and `.github/workflows/ltk-patcher-watch.yml` opens an issue when a newer usable pair appears. Neither can extend the date: only a new LTK build can.

## Troubleshooting & recovery (field notes)

Hard-won knowledge from a full teardown and rebuild of a working install. Read this before "resetting" anything â€” most of it exists because a reset broke a working setup.

### How Pengu Loader actually works here

This is the **Rose fork of Pengu Loader (v2.0.0)**, not upstream Pengu. They inject differently, and mistaking one for the other costs hours.

**It does NOT do any of these** (verified by reading `core.dll` and `Pengu Loader.exe` strings):

- no `plugin-manifest.json` â€” the string does not exist in either binary
- no `--remote-debugging-port` on the client command line
- no `system.yaml` edits
- no symlinks or junctions in the League folder
- no `version.dll` / `winmm.dll` / `dxgi.dll` proxy file dropped next to `LeagueClient.exe`
- no `PENGU_LOADER_HOME` environment variable

**What it actually does.** `core.dll` is a proxy DLL loaded into the **CEF renderer process** of `LeagueClientUx.exe`, then it uses `WriteProcessMemory` + `CreateRemoteThread` to work inside that process and drives the client webview over **DevTools over WebSocket on `127.0.0.1:<port>`**. Control-flow strings found in `core.dll`:

```
HookRendererProcess / ===== CORE DLL LOADED ===== / DLL_PROCESS_ATTACH received
Secondary proxy detected (winhttp dll) / winhttp dll not loaded, this proxy will be primary
CEF version check FAILED for renderer process / but type=renderer not command line
DevTools / 127.0.0.1: / WebSocket / /json / Chrome_WidgetWin_0
```

**Consequences that matter:**

1. Nothing on disk changes on a successful install. `system.yaml` keeping its old timestamp is **correct**, not a bug. Do not chase it.
2. Pengu must be running while the League client is open to inject. Installing with the client closed is fine; injecting is not.
3. The "you want full League Client restart?" prompt is Pengu asking to restart the client so the DLL proxy can attach. Letting it restart the client is the expected flow.

### Pengu Loader CLI reference (v2.0.0 Rose fork)

Flags are **UTF-16** in the binary, so `grep`/ASCII scanning finds nothing. Extracted with `[Text.Encoding]::Unicode.GetString()`:

| Flag | Meaning |
|---|---|
| `--install` / `/install` | activate: registers Pengu for the League install |
| `--uninstall` / `/uninstall` | deactivate |
| `--status` | report ACTIVE / INACTIVE |
| `--set-league-path <path>` | set the League folder (must contain `LeagueClient.exe`) |
| `--restart-client` | request a League client restart |
| `--silent` / `-s` / `/silent` | no UI |
| `--help` `-h` `/?` | usage |

Two behaviours worth knowing:

- `--status` prints `Pengu is currently ACTIVE.` / `INACTIVE` to stdout in **silent** mode. Without `--silent` it opens the WPF GUI window and prints nothing â€” which looks like a hang but is not. Always pass `--silent` when scripting, and capture stdout with `Start-Process -RedirectStandardOutput`.
- `--install` **fails with exit code -1** and the message `Please close the running League Client and Loader menu before installing it.` if `LeagueClient.exe` is running. `RiotClientServices.exe` running does **not** block it.

### The state files that must never be deleted

`backend/Pengu Loader/config` and `backend/Pengu Loader/datastore` are **not caches**. They are the live activation state.

`config` is a plain key/value file; the only key that matters is:

```
LeaguePath=D:\Games\Riot Games\League of Legends
```

Delete `config` and Pengu forgets which League install to attach to. Delete both and it reports INACTIVE with zero diagnostic output, and D-Changer's log keeps saying `Using existing external Pengu Loader with Rose plugins` â€” which reads like success but is not. The plugin panel silently disappears from champion select, no skin ID ever reaches the engine, and the game loads with the stock skin.

Symptom chain when this is broken:

```
no skin selector in champ select
  -> log: [INJECT] No skin ID available yet (last_hovered_skin_id=None, phase=FINALIZATION)
  -> log: [INJECT] Final name variable: 'None'
  -> game loads with stock skin
```

The engine blames the missing skin ID. The real cause is an INACTIVE loader two layers upstream. Check the loader first.

### Recovery: re-activating Pengu Loader

```powershell
$pl  = 'C:\Users\ledot\OneDrive\Desktop\D-Changer\backend\Pengu Loader'
$exe = Join-Path $pl 'Pengu Loader.exe'
$lg  = 'D:\Games\Riot Games\League of Legends'

# 1. League client MUST be fully closed. Check first â€” a leftover process fails the install.
Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'LeagueClient*' }

# 2. Delete the zero-byte state files left behind by a failed install
Remove-Item (Join-Path $pl 'config'),(Join-Path $pl 'datastore') -Force -ErrorAction SilentlyContinue

# 3. Set the path, then activate. Capture stdout â€” silent mode prints to it.
Start-Process $exe -ArgumentList '--set-league-path',"`"$lg`"",'--silent' `
  -RedirectStandardOutput C:\Users\ledot\pg1.txt -Wait -WindowStyle Hidden
Get-Content C:\Users\ledot\pg1.txt     # expect: League of Legends path set to '...'.

Start-Process $exe -ArgumentList '--install','--silent' `
  -RedirectStandardOutput C:\Users\ledot\pg2.txt -Wait -WindowStyle Hidden
Get-Content C:\Users\ledot\pg2.txt     # expect: Pengu has been activated.

# 4. Verify the state file was written
Get-Content (Join-Path $pl 'config')   # expect: LeaguePath=D:\Games\Riot Games\League of Legends
```

Then open the League client. Success is visible: the skin selector appears in champion select.

A healthy activation looks exactly like this â€” `config` is ~50 bytes, one line, nothing else:

```
LeaguePath=D:\Games\Riot Games\League of Legends
```

### Component health check

Run before blaming the injection engine. The whole path is four processes plus one registry key.

| Component | How to verify | Healthy |
|---|---|---|
| LTK Manager | `Get-NetTCPConnection -LocalPort 46321 -State Listen` | PID listening |
| LTK bridge token | `Get-ItemProperty HKCU:\Software\LTKBridge` | `ApiPort` + `ApiToken` present |
| D-Changer UI | process `d-changer.exe` | running in **Session 1** |
| D-Changer engine | process `dchanger-backend.exe` | running in **Session 1** |
| Pengu Loader | `Pengu Loader.exe --status --silent` | `Pengu is currently ACTIVE.` |
| Rose plugins | count dirs under `backend/Pengu Loader/plugins` | 10, each with `index.js` |
| Skin library | `(Get-ChildItem $env:LOCALAPPDATA\D-Changer\skins -Recurse -File).Count` | ~9,200 files |

**Session 0 vs Session 1 is a real failure mode.** This is a desktop app; anything in Session 0 cannot inject into or interact with a user's game. `SessionId` must be `1` for both D-Changer processes.

### Reading a successful injection log

`%LOCALAPPDATA%\D-Changer\logs\rose_*.log`. The happy path, in order:

```
INFO  | [Main] LCU reconnected after account swap - keeping Pengu Loader active...
INFO  | [PLUGIN:LU-ChromaWheel] debug | {'message': 'injected CSS rules'}
INFO  | [WS] Injection threshold refreshed for ChampSelect: 0.91s
INFO  | [INJECT] on_champion_locked called for: champ_157 (id=157)
DEBUG | [INJECT] Using base skin ID from state: 'skin_157087' (ID: 157087)
INFO  | PREPARING INJECTION >>> SKIN_157087 <<<
DEBUG | [INJECT] Found skin: C:\...\skins\157\157087\157087.fantome
INFO  | [HISTORIC] Stored last injected ID 157087 for champion 157
INFO  | INJECTION COMPLETED >>> SKIN_157087 <<<
```

The two lines that decide everything are `Using base skin ID from state: ...` (a plugin delivered a skin ID) and `INJECTION COMPLETED >>> SKIN_xxxxxx <<<`. If the first is missing, the problem is upstream in Pengu/plugins â€” not in the injection engine.

### Resetting without destroying a working install

A "clean reset" is a reasonable thing to want. It is also the fastest way to break a working setup, because most state in this app is load-bearing.

**Safe to delete** â€” regenerated automatically, costs only a re-sync or a log history:

```
%LOCALAPPDATA%\D-Changer\state\
%LOCALAPPDATA%\D-Changer\logs\
%LOCALAPPDATA%\D-Changer\historic.json
%APPDATA%\dev.leaguetoolkit.manager\profiles\      (patched overlay, rebuilt on demand)
%APPDATA%\dev.leaguetoolkit.manager\mods\          (imported mods)
%APPDATA%\dev.leaguetoolkit.manager\patcher\       (host cache)
%APPDATA%\dev.leaguetoolkit.manager\library.json   (mod library index)
%APPDATA%\dev.leaguetoolkit.manager\logs\, incidents\, telemetry\
```

**Never delete** â€” each of these breaks something that has no self-healing path:

| Path | Why |
|---|---|
| `backend\Pengu Loader\config` | holds `LeaguePath`; losing it deactivates the loader |
| `backend\Pengu Loader\datastore` | live plugin state |
| `backend\Pengu Loader\plugins\` | the 10 `ROSE-*` plugins, each with `index.js` |
| `backend\Pengu Loader\core.dll` | the injection proxy DLL itself |
| `%LOCALAPPDATA%\D-Changer\skins\` | ~4 GB / ~9,200 files; re-downloading is slow |
| `%LOCALAPPDATA%\D-Changer\classic\` | ~640 MB of base skins |
| `_internal\injection\tools\` | `hashes.game.txt`, `ltk_patcher_host.exe`, `ltk_patcher_dll.dll` |
| `C:\Program Files\LTK Manager Probe2\` | the mod backend; nothing else patches WADs |

Note the asymmetry: `Profiles` (the per-account skin choices) and `historic.json` (last injected ID per champion) look similar but are different things. Deleting `historic.json` just loses the "last used skin" memory. Deleting the LTK `library.json` loses which mods are enabled.

### The `injection/tools` warning is not fatal by itself

The engine logs this when the console-injection path is used:

```
WARNING | Missing runtime injection dependencies: ['mod-tools.exe', 'cslol-dll.dll']
WARNING | Please place the missing files in injection/tools/
```

Those two binaries are a **console/`mod-tools`-based fallback overlay path**. The production path goes through LTK Manager (`injection_backend = ltk` in `config.ini`) and only needs `ltk_patcher_host.exe` + `ltk_patcher_dll.dll` + `hashes.game.txt`. The warning is immediately followed by `Injection system initialized successfully` and injections work normally.

Do not delete them again if they exist, and do not treat this warning as the cause of a missing skin. In this install the real cause was always upstream (Pengu inactive), never this.

### Never modify the game's original WADs â€” and how to prove you didn't

LTK patches through an **overlay** in `%APPDATA%\dev.leaguetoolkit.manager\profiles\`, never by writing into `Game\DATA\FINAL\`. Verify a game install is untouched with:

```powershell
Get-Item "$lg\Game\DATA\FINAL\Champions\Yasuo.wad.client" | Select-Object Length,LastWriteTime
Get-Item "$lg\system.yaml"                                | Select-Object Length,LastWriteTime
```

Both must keep timestamps from **before** any modding session. If they moved, something wrote directly into the install and that is a real problem. In a healthy overlay-based setup these timestamps never change, which also means a reset never needs to repair game files.

### Process and path reference for this machine

| Item | Value |
|---|---|
| League install | `D:\Games\Riot Games\League of Legends` |
| Game folder | `D:\Games\Riot Games\League of Legends\Game` |
| D-Changer source | `C:\Users\ledot\OneDrive\Desktop\D-Changer` |
| D-Changer exe | `...\ui\src-tauri\target\release\d-changer.exe` |
| Pengu Loader | `...\backend\Pengu Loader\Pengu Loader.exe` |
| LTK Manager | `C:\Program Files\LTK Manager Probe2\ltk-manager.exe` (v1.33.0) |
| Bridge port | 46321 |
| Engine log | `%LOCALAPPDATA%\D-Changer\logs\rose_*.log` |

### Long-running and hanging commands

Two things that appear to hang when driven over SSH, and what to do instead:

- `Pengu Loader.exe` without `--silent` opens a GUI and never returns. Always add `--silent`, and redirect stdout to a file.
- Recursive scans of `C:\Users\...\AppData` with `-Depth` can exceed a remote timeout. Scope them to a specific folder, or filter with `Where-Object` early.

## Disclaimer

This project is for personal, educational use. It modifies local client assets only and provides no competitive advantage. Using it may violate Riot Games' Terms of Service; use at your own risk. Not affiliated with Riot Games.
