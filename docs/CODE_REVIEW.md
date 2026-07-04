# SmartFrame — Production-Grade Code & Architecture Review

**Date:** 2026-07-05
**Scope:** Full repository (`main.py`, `modes/`, `scripts/`, systemd unit, setup scripts, git history)
**Deployment context:** Raspberry Pi Zero 2 WH on a home LAN, systemd service, controlled via MQTT/Home Assistant.

---

## 1. 🚨 Critical Security Vulnerabilities

### SEC-1 — Orchestrator depends on blanket passwordless root (`sudo`) — **HIGH**

`main.py` shells out to `sudo` non-interactively from a systemd service:

- `sudo ddcutil setvcp …` ([main.py:733](main.py#L733), [main.py:524](main.py#L524))
- `sudo sh -c "echo … > /sys/class/graphics/fb0/blank"` ([main.py:544](main.py#L544))
- `sudo modprobe i2c-dev` ([main.py:1307](main.py#L1307))

This only works because the default Raspberry Pi OS user has `NOPASSWD: ALL` in sudoers. The consequence: **any code execution inside the orchestrator process — or inside the unsandboxed browser it launches (see SEC-2) — is instantly root.** You've built a privilege-escalation bridge into your own service.

The bitter irony: `setup_pi.sh` already adds the user to the `i2c` group ([setup_pi.sh:19](scripts/setup_pi.sh#L19)), which is *exactly* what `ddcutil` needs to run **without** sudo. The `sudo` calls are unnecessary.

**Fix:** see Remediation R1.

### SEC-2 — Browser launched with TLS validation disabled + no sandbox — **HIGH**

[mirror_mode.sh:119-138](modes/mirror_mode.sh#L119-L138) launches Chromium with:

```
--no-sandbox --single-process --test-type
--ignore-certificate-errors --allow-running-insecure-content
--remote-allow-origins=*
```

- `--ignore-certificate-errors` disables **all** TLS validation. Anyone on your LAN who can ARP-spoof or DNS-poison the MagicMirror host serves arbitrary content to the kiosk.
- That content runs in a `--no-sandbox --single-process` browser — a renderer exploit is a full process compromise, no sandbox to escape.
- The compromised process runs as a user with passwordless sudo (SEC-1). **Chain: LAN MITM → renderer bug → root on the Pi.**
- `--remote-allow-origins=*` is currently inert (no `--remote-debugging-port`), but it's a loaded gun: the moment anyone adds a debug port "temporarily", every website in the browser can attach to the DevTools protocol.

`--no-sandbox`/`--single-process` are genuinely required on 512MB — accepted. The TLS flags are not. Your MagicMirror is HTTP on a LAN; if it were HTTPS you'd want the cert *checked*.

### SEC-3 — MQTT control plane: plaintext transport, optional auth — **MEDIUM**

- Port 1883, no TLS support in code ([main.py:1334](main.py#L1334)) — credentials and all commands cross the LAN in cleartext.
- Auth is silently skipped when the username is still the placeholder ([main.py:1298](main.py#L1298)) — an unconfigured system connects anonymous without warning.
- Anyone who can publish to `smartframe/set_*` controls the display and triggers the sudo'd hardware commands.

**What's done right (credit where due):** every inbound payload is validated against an allowlist or `int()`-parsed before use ([main.py:1239-1266](main.py#L1239-L1266)); mode names resolve only to files already in `modes/`; preset/source names go through fixed dicts. **There is no command-injection path from MQTT.** This is genuinely better than most hobby projects. The weakness is transport and broker policy, not parsing.

- `config.yaml` holds the MQTT password in cleartext with default (0644) permissions — readable by every local process.

### SEC-4 — Fixed world-writable IPC file `/tmp/smartframe_dba` — **MEDIUM**

The dBA bridge ([audio_mode.py:602](modes/audio_mode.py#L602), [main.py:233](main.py#L233)) is a fixed path in sticky-but-shared `/tmp`. Any local user/process can:

1. **Spoof sensor data** to Home Assistant (write a fresh file every second).
2. **Flip orchestrator state**: the "GUARDIAN" loop ([main.py:1381-1391](main.py#L1381-L1391)) trusts a fresh bridge file and forces `current_mode = "audio"`.
3. **Suppress the crash watchdog**: [main.py:1365-1371](main.py#L1365-L1371) skips crash recovery while the bridge file is fresh — a fake bridge file keeps a dead mode "alive" forever.
4. **Symlink attack**: `open(bridge_file, "w")` follows symlinks — pre-plant `/tmp/smartframe_dba → /home/pi/.bashrc` and the service truncates/overwrites the target as its user (who has sudo, see SEC-1).

Single-user Pi lowers likelihood, but this is three design flaws (spoofable input, trust-based state machine, symlink-following write) stacked on one file.

### SEC-5 — TOCTOU in `install_service.sh` — **LOW**

[install_service.sh:18-37](scripts/install_service.sh#L18-L37) writes the rendered unit to fixed `/tmp/smartframe.service`, then `sudo cp`s it to `/etc/systemd/system/`. A local attacker who pre-creates that path (or races the window between write and copy) gets attacker-controlled content installed root-owned as a boot service. Use `mktemp`.

### SEC-6 — Over-broad `pkill -f` — **LOW**

`pkill -f chromium|cog|labwc` ([main.py:758-765](main.py#L758-L765), [main.py:920](main.py#L920)) matches *substrings anywhere in any user's command line*. `pkill -f cog` kills a process running `~/recognizer.py`; `pkill -f labwc` kills a desktop labwc session the user might be running on the same box. Kill by process group / PID you own, or at minimum `pkill -x`.

### SEC-7 — `config.yaml` existed in git history — **LOW (near miss)**

`config.yaml` was committed at `866c055` and removed at `812ea5c`. It contained **only placeholders** — no live secrets leaked — but the pattern nearly bit you. The `.gitignore` entry now prevents recurrence. If you ever committed real credentials to a *local* branch, remember history is forever on GitHub once pushed.

### Dependency & supply-chain notes — **INFO**

- `yaml.safe_load` used everywhere — correct, no deserialization RCE. ✅
- Pinned `paho-mqtt==2.1.0`, `PyYAML==6.0.3`, `numpy==2.4.3` — no known critical CVEs at review time; but pinning is inconsistent (`pygame>=2.6.1`, `pyaudio>=0.2.14` float). Pin everything and add `pip-audit` to your workflow.
- No listening sockets are opened by the app itself — attack surface is MQTT-out + browser + local IPC. Good.

---

## 2. 🏗️ Architecture & Component Design

### A-1 — 1,419-line monolith with global mutable state shared across ~6 threads

`main.py` is simultaneously: config loader, MQTT client, HA discovery publisher, display hardware driver (4 strategies), Wayland compositor lifecycle manager, process supervisor/watchdog, audio DSP engine, and cache layer. State (`current_mode`, `current_process`, `_working_methods`, `_labwc_process`) is naked module globals mutated concurrently by:

- the main guardian loop,
- the `command_worker` thread,
- the `AudioMonitor` thread,
- the paho MQTT network thread (callbacks),
- ad-hoc daemon threads (`set_display_power`, `_discover_audio_device`, labwc starter),
- the signal handler.

**There is not a single lock in the file.** Concrete crashes this causes are listed in §3 (Q-1). Structurally, this needs to become packages: `smartframe/{config,mqtt,display,compositor,supervisor,audio}.py` with one owner per piece of state.

### A-2 — Duplicated DSP that has already drifted

A-weighting, asymmetric EMA, and the ad-hoc "noise floor compensation" exist **twice**: [main.py:214-316](main.py#L214-L316) (`AudioMonitor`) and [audio_mode.py:234-583](modes/audio_mode.py#L234-L583). They already disagree: EMA time constant 0.125s vs 0.08s, chunk 4096 vs 1536, no windowing in the monitor vs Hann in audio mode. Your HA "Ambient Sound" sensor reports *different numbers depending on which code path produced them* — for the same room. Extract a shared `dsp.py`.

### A-3 — File-mtime polling as IPC

The `/tmp` bridge (SEC-4) is also architecturally the weakest link: mtime-freshness as a liveness protocol, 5-second staleness windows, and three separate readers interpreting it differently (monitor, watchdog, guardian). `audio_mode.py` already imports the config with the MQTT broker address in it — the honest designs are either (a) audio_mode publishes dBA to MQTT itself, or (b) a Unix socket in `$XDG_RUNTIME_DIR` (0700, per-user, tmpfs). Either deletes ~80 lines of freshness-checking heuristics.

### A-4 — Optimistic state machine with scattered recovery

`start_mode()` publishes the *new* mode to HA **before** anything has launched ([main.py:947-949](main.py#L947-L949)). If labwc or the browser fails, HA shows "mirror" while the screen shows nothing until the watchdog notices, resets to "off", and republishes. Recovery logic lives in four places (watchdog, guardian, `on_message`, `start_mode` failure branch) with subtly different behavior. Model it as one supervisor with explicit states (`OFF → STARTING → RUNNING → FAILED`) and publish `STARTING` honestly.

### A-5 — Non-atomic, unlocked cache writes

`.smartframe_cache` is JSON-dumped in place ([main.py:374-379](main.py#L374-L379)) from at least three threads (audio discovery thread, worker, main). Two interleaved writes = truncated/corrupt JSON. You "fail open" by ignoring parse errors — good instinct — but the fix is one line: write to `CACHE_FILE + ".tmp"` then `os.replace()` under a lock.

### A-6 — Deployment & ops

- **No containerization — and that's the right call.** On a 512MB Pi needing DRM/KMS, I2S, I2C and seatd, Docker would add overhead and permission pain for zero isolation benefit (the service effectively has root anyway, see SEC-1). Document this as a decision, not an omission.
- Git-pull deployment with a post-merge hook is fine for one device, but there is no rollback and no "known-good" tag. `git tag stable` before experimenting costs nothing.
- The systemd unit has zero hardening (no `ProtectSystem`, `ProtectHome`, `NoNewPrivileges` — the last is currently impossible *because of* the sudo dependency; fixing R1 unlocks it) and `After=network-online.target` without corresponding waits does nothing on Wi-Fi that comes up late — though `connect_async` covers you here.
- **1.5GB swapfile on the SD card** (README) will grind the card to death under Chromium memory pressure. `zram` (which the README explicitly *removes*) is the better tool on a Zero 2: compressed RAM swap, zero SD wear, and faster. Recommendation reversed: keep zram, add a *small* SD swapfile as overflow only.

### A-7 — Watchdog gives up instead of healing

When a mode process dies, the watchdog resets to `off` ([main.py:1372-1378](main.py#L1372-L1378)). For an appliance, the correct behavior is restart-with-backoff into the *same* mode (e.g. 3 attempts, then fall to `off`). A transient WebProcess crash currently turns your picture frame into a black rectangle until a human touches HA.

---

## 3. 🔍 Code Quality & Robustness

### Q-1 — TOCTOU races that crash the whole orchestrator (most important bug in the repo)

The guardian loop:

```python
if current_mode != "off" and current_process:   # main.py:1362
    poll_res = current_process.poll()           # main.py:1363
```

`command_worker` can set `current_process = None` ([main.py:755](main.py#L755)) between the check and the `.poll()` → `AttributeError`. This propagates to the outer `except Exception` ([main.py:1410](main.py#L1410)) → **the entire orchestrator dies**, systemd restarts it 10s later, the screen goes through a full power cycle. Same class of bug two paragraphs down: `os.path.exists(bridge_file)` then `os.path.getmtime(bridge_file)` ([main.py:1368](main.py#L1368)) — file deleted between the two calls → `FileNotFoundError` → same total crash. (The guardian block at 1381 *is* wrapped in try/except; the watchdog block at 1362-1378 is not.)

Fix: snapshot globals into locals once per iteration (`proc = current_process`), wrap `getmtime` — see R5.

### Q-2 — `audio_mode.py` never retries the microphone

If the stream fails to open **once** at startup ([audio_mode.py:260-274](modes/audio_mode.py#L260-L274)) — which is *likely*, because `AudioMonitor` in the parent may still hold the device during the mode-switch window (it releases it reactively inside its loop, up to ~100ms+ later) — audio mode renders "Waiting for I2S Microphone Signal…" **forever**. The message promises waiting; the code contains no retry. One periodic reopen attempt in the main loop fixes it.

### Q-3 — Bare `except:` epidemic

~10 occurrences of bare `except:` / `except Exception: pass` in `main.py` and `audio_mode.py` (e.g. [main.py:256](main.py#L256), [main.py:270](main.py#L270), [main.py:324](main.py#L324), [main.py:339](main.py#L339)). Bare `except:` also swallows `SystemExit`/`KeyboardInterrupt` inside threads. Every one of these is a debugging session you've pre-scheduled for future-you. Minimum: `except Exception as e: logging.debug(...)`.

### Q-4 — `mirror_mode.sh` details

- The Chromium-dialog dismisser comment says "Try at 8s, 12s, 16s" but the sleeps are **sequential**, so keys fire at 8s, 20s, 36s, 56s ([mirror_mode.sh:186-191](modes/mirror_mode.sh#L186-L191)). At 56s the dialog is long gone and the `Return` keypress goes into MagicMirror itself.
- `--user-data-dir=$PROFILE_DIR` and `--disk-cache-dir=$CACHE_DIR` are unquoted inside `CHROME_FLAGS`, and `$FULL_CMD` is word-split at launch — the whole scheme breaks if the project path ever contains a space.
- The YAML "parser" `grep 'url:' | head -n 1` ([mirror_mode.sh:16](modes/mirror_mode.sh#L16)) grabs the first `url:` anywhere in the file, comments included — order-dependent and silently wrong the day a second `url:` key appears above `magic_mirror`. The orchestrator already passes `MIRROR_URL` via env; keep the fallback but anchor it (`grep -A3 '^magic_mirror:' | grep 'url:'`).

### Q-5 — Config rewrites destroy user documentation

`update_config.py` and `list_audio_devices.py` round-trip `config.yaml` through `yaml.safe_load`/`yaml.dump` — **every comment in the file is deleted** on first use. Your config file's comments *are* its documentation (`# Volume in dB to trigger…`). Use `ruamel.yaml` (round-trip mode) or accept and document the loss.

### Q-6 — Module "constants" mutated, duplicate imports, dead weight

- `CHANNELS` and `SAMPLE_RATE` are declared as constants then reassigned at runtime ([audio_mode.py:215-218](modes/audio_mode.py#L215-L218)).
- `import pyaudio` at [main.py:145](main.py#L145) duplicates the top-level import.
- `available_modes_cache` never invalidates — a new mode file requires a service restart (fine, but undocumented).
- `np.roll` on the 8192-sample buffer every frame is O(N) copy; a ring buffer (or `collections.deque`-of-chunks + `np.concatenate` on demand) is cheaper on the Zero 2. Two FFTs per frame at ~31Hz is defensible; this is the next knob if CPU becomes tight.

### Q-7 — Things done well (so this review stays honest)

- MQTT payload validation via allowlists — no injection surface.
- `yaml.safe_load` everywhere, LWT + retained state topics, HA discovery re-announcement.
- The layered display-strategy discovery with persistent cache is genuinely clever and well-commented.
- ALSA error-handler suppression via ctypes, correct `exception_on_overflow=False`, Parseval-normalized RMS — the DSP is more correct than most "dB meter" projects.
- `start_new_session=True` + `killpg` for process-group cleanup is the right pattern.

---

## 4. 🎚️ Algorithm & Signal-Processing Accuracy

The DSP here is more careful than most hobby "dB meter" projects — but "careful" and "accurate" are different claims, and the two dB paths quietly disagree with each other. Findings ordered by impact on the number a user actually reads.

### DSP-1 — The dBA/dBZ numbers are **not SPL** — single-point calibration with an assumed 1:1 slope — **accuracy: fundamental**

Every dB value is computed as `20·log10(rms) + calibration_offset` where `rms` is the RMS of **int16 sample values** ([audio_mode.py:576-577](modes/audio_mode.py#L576-L577), [main.py:312](main.py#L312)). The log reference is therefore amplitude `1.0` (one LSB) — an arbitrary electrical reference, **not** the acoustic 20 µPa that "dB SPL" means. The only thing tying the reading to real-world SPL is the user setting `calibration_offset_db` against a phone app.

This is defensible for a digital MEMS mic — the INMP441 is acoustically linear, so a single offset *does* roughly map dBFS→dB SPL across the range. But two caveats must be documented:

1. **It's a one-point calibration assuming unity slope.** If the phone app is used at 70 dBA, readings at 40 and 95 dBA inherit whatever slope error the mic + int16 truncation introduce. No two-point (gain+offset) calibration is offered.
2. **int16 truncation of a 24-bit I2S mic throws away the bottom 8 bits** — you're capturing `paInt16` from a device whose usable dynamic range is 24-bit left-justified. The quantization floor you keep is ~ -90 dBFS, which is *near* the INMP441's own noise floor, so it's tolerable — but it's the reason the low end needs the fudge in DSP-3.

**What's correct:** the A-weighting transfer function itself ([audio_mode.py:234-247](modes/audio_mode.py#L234-L247)) is the exact IEC 61672 rational form, and `w *= 1.2589` is the correct +2.00 dB gain normalization (10^(2/20)) that makes A(1 kHz)=0 dB. This part is right.

### DSP-2 — The two dB paths use **different time-weighting**, so the HA sensor jumps when you switch modes — **accuracy: the headline bug**

The same physical sound produces two different dBA numbers depending on which code path is publishing:

| | `AudioMonitor` (main.py) | `audio_mode.py` |
|---|---|---|
| Integration | **symmetric** EMA, τ≈125 ms ([main.py:236](main.py#L236)) | **instant attack**, 80 ms release ([audio_mode.py:258](modes/audio_mode.py#L258), [566-573](modes/audio_mode.py#L566-L573)) |
| Display hold | none | +250 ms peak-hold ([audio_mode.py:588-595](modes/audio_mode.py#L588-L595)) |
| Chunk size | 4096 (85 ms) | 1536 (32 ms) |
| Windowing | none (rectangular) | Hann |

When audio mode is running, HA receives the **bridge** value (instant-attack + peak-hold → biased *high* on any transient — music, speech, a door). When audio mode is *not* running, HA receives the **monitor** value (symmetric 125 ms → a proper IEC "FAST"-like exponential). So the "SmartFrame Ambient Sound" sensor **steps by several dB the instant you switch in/out of audio mode**, for an unchanged room. This is a visible discontinuity in a logged HA sensor.

The monitor's symmetric-exponential version is the metrologically correct one; the audio-mode instant-attack is a *display* aesthetic ("reactive") that leaked into the *measurement*. Standards-compliant FAST/SLOW weighting uses the **same** time constant for rise and fall. Unify both on a shared symmetric implementation (see DSP-4 / R14); keep the instant-attack strictly for the on-screen animation if you like the look, but don't publish it.

### DSP-3 — The sub-45 dBA "noise-floor correction" makes quiet-room readings fictional — **accuracy: high**

```python
if db_a < 45:
    correction = 8.0 * (1.0 - (max(30, db_a) - 30) / 15)
    db_a -= correction        # audio_mode.py:581-584  AND  main.py:314-316
```

This bends the transfer curve below 45 dBA: it subtracts up to 8 dB, ramping to 0 at 45 and flat-8 below 30. It exists to stop the mic's own noise floor from making silence read too high — but it does so by **fabricating a non-linear scale exactly where most home ambient monitoring lives** (a quiet bedroom is 30 dBA, a quiet living room 35–40). Any reading your sensor reports below 45 dBA is a cosmetic curve, not a measurement. At least it's applied identically in both paths (unlike DSP-2), so it's consistent — but it should be (a) documented as "readings <45 dBA are indicative only", and (b) ideally replaced with an honest measured noise-floor subtraction in the *power* domain (`P_signal = max(0, P_measured − P_floor)`) using a one-time silence calibration, rather than a hardcoded dB fudge.

### DSP-4 — `AudioMonitor` does a wasted inverse FFT and skips windowing — **accuracy: minor, efficiency: major**

```python
fft_complex = np.fft.rfft(data)          # main.py:302  — no window (rectangular)
fft_aw = fft_complex * a_gains
data_aw = np.fft.irfft(fft_aw)           # main.py:304  — full inverse transform...
rms_a = np.sqrt(np.mean(data_aw**2))     # main.py:305  — ...just to get one scalar
```

Three problems, in order of severity:

1. **The `irfft` is pure wasted work.** By Parseval, the A-weighted RMS is obtainable directly from the weighted spectrum — which is *exactly what `audio_mode.py` already does* for its chunk path ([audio_mode.py:556-559](modes/audio_mode.py#L556-L559)). The inverse transform doubles the FFT cost of the always-on monitor for no result. (See EFF-5.)
2. **Freq-domain multiply + irfft is circular convolution** — the A-weighting impulse response wraps around the block edge (time-domain aliasing) because there's no overlap-add. For a broadband RMS level this is a small error, but it's a real one that the Parseval approach avoids entirely.
3. **No window** before the FFT → spectral leakage. Minor for a broadband level, but inconsistent with the Hann-windowed audio-mode path — another source of the DSP-2 mismatch.

Fixing this by switching the monitor to the Parseval method (or, better, a 6th-order A-weighting **IIR biquad cascade** in the time domain — the standard way to build an SPL meter, far cheaper than any FFT) resolves the accuracy nit *and* the efficiency finding at once.

### DSP-5 — The spectrum analyzer is a *visualizer*, not a measurement, and averages the wrong quantity — **accuracy: aesthetic, but one real bug**

The 120-band display is explicitly artistic — `visual_tilt` of 4 dB/octave, a `HIGH_FREQ_BOOST`, and a hand-tuned 65–145 dB window over **un-normalized** FFT bin magnitudes ([audio_mode.py:609-625](modes/audio_mode.py#L609-L625)). That's fine and expected for a "mastering-grade" look. Two notes:

1. **Band aggregation averages magnitude, not power:** `mag = np.mean(fft_mag[indices])` ([audio_mode.py:615](modes/audio_mode.py#L615)). Energy per band is the sum of `|X|²`, so the correct aggregate is `sqrt(mean(|X|²))` (RMS of the bins). Mean-of-magnitudes under-represents peaky bands relative to broadband ones — the bars slightly misstate relative band energy. One-line fix, and it makes the display track real spectral balance.
2. **Sub-bass is bin-limited.** At FFT_SIZE 8192 / 48 kHz the bin spacing is 5.86 Hz, but the lowest log-spaced bands (25–60 Hz) are narrower than one bin, so `get_log_bands` falls back to "nearest bin" ([audio_mode.py:349-353](modes/audio_mode.py#L349-L353)) and several adjacent bands collapse onto the same 1–2 bins → a visible staircase in the bottom ~2 octaves. Inherent to the FFT size; only a longer window (more latency) or a dedicated low-band CZT/Goertzel would fix it. Worth a comment so it's not mistaken for a bug.

`CALIBRATION_OFFSET` is also added to each band's dB ([audio_mode.py:617](modes/audio_mode.py#L617)) — harmless (a few dB shift on an arbitrary 65–145 scale is invisible) but conceptually meaningless, since the bands aren't SPL-referenced. Drop it from the band path.

### DSP-6 — dBZ has no defined bandwidth — **accuracy: minor**

IEC 61672 Z-weighting is flat but band-limited (nominally 10 Hz–20 kHz). The code zeroes DC ([audio_mode.py:549](modes/audio_mode.py#L549)) but otherwise integrates the full spectrum up to Nyquist, so the "dBZ" figure includes any near-Nyquist content the mic passes. For an INMP441 this is negligible, but strictly the number isn't a standards dBZ.

**DSP verdict:** the *forward* math (A-weighting curve, Parseval normalization in the chunk path, window-power compensation) is correct and better than average. The accuracy problems are all in the *statistics* layered on top — inconsistent time-weighting between paths (DSP-2), a fabricated low-end curve (DSP-3), and an un-referenced scale sold as "dBA" (DSP-1). None of these are hard to fix; DSP-2 is the one that produces a visibly wrong logged sensor.

---

## 5. ⚡ Orchestrator & Runtime Efficiency

Everything here is about a 4-core 1 GHz Cortex-A53 with 512 MB. The audio renderer is the hot path; the orchestrator itself is mostly idle-efficient (1 Hz guardian loop, cached discovery) with a few sharp edges.

### EFF-1 — "60 FPS" is fictional; the loop is capped at ~31 Hz by the blocking mic read — **efficiency: clarifying, not a bug**

`clock.tick(60)` ([audio_mode.py:907](modes/audio_mode.py#L907)) targets 60 FPS, but each iteration calls `stream.read(CHUNK_READ)` = `read(1536)` ([audio_mode.py:523](modes/audio_mode.py#L523)), which **blocks until 1536 samples exist** = 32 ms at 48 kHz. So the loop can never exceed ~31 Hz and `clock.tick` never actually throttles anything — the frame rate is dictated by the audio read, and the CPU runs flat-out at ~31 Hz. The dead code comment "lower to 30 to save 50% CPU" ([audio_mode.py:908](modes/audio_mode.py#L908)) is therefore misleading: you're already at ~31 Hz, and lowering `tick` does nothing because the read is the gate. To actually cut CPU you must read *larger* chunks less often, or decouple rendering from capture (EFF-2).

### EFF-2 — The 8192-point Blackman FFT is recomputed at 88% overlap every frame — the single biggest CPU cost — **efficiency: high**

Each frame rolls 1536 new samples into the 8192 buffer and computes a **fresh 8192-point Blackman FFT** ([audio_mode.py:535-539](modes/audio_mode.py#L535-L539)). Consecutive FFTs overlap by 6656/8192 = **81%** — you're recomputing almost the same transform 31×/s. An 8192 real FFT in single-threaded NumPy is the dominant per-frame cost on a Zero 2 (no guaranteed NEON path in pocketfft for this size). The visual analyzer does **not** need 8192-bin resolution refreshed every 32 ms:

- Compute the big spectrum FFT every **other** frame (or every 3rd) and interpolate the bars — halves/thirds the FFT load, visually indistinguishable at these smoothing factors.
- Or drop FFT_SIZE to 4096 (still 11.7 Hz resolution) — quarter the FFT cost (N log N), at the price of the sub-bass resolution already limited in DSP-5.
- The small 1536-point A/Z chunk FFTs ([audio_mode.py:547-558](modes/audio_mode.py#L547-L558)) are cheap and can stay per-frame for responsive level metering.

### EFF-3 — `np.roll` copies the whole 8192 buffer every frame — **efficiency: medium**

`audio_buffer = np.roll(audio_buffer, -len(new_data))` ([audio_mode.py:531](modes/audio_mode.py#L531)) is an O(N) allocation+copy of 8192 float32 (32 KB) every frame, 31×/s ≈ 1 MB/s of pure memmove plus a fresh allocation churning the GC. Use a fixed ring buffer with an index, or shift in place (`buf[:-n]=buf[n:]; buf[-n:]=new`) to avoid the allocation. Minor next to the FFT, but free.

### EFF-4 — Four interpreted Python loops over 120 bands per frame — **efficiency: medium**

Per frame the code runs four separate `for i in range(120)` / `enumerate(band_indices)` passes — band magnitude aggregation ([audio_mode.py:612](modes/audio_mode.py#L612)), smoothing/gate/peak ([audio_mode.py:631](modes/audio_mode.py#L631)), curve ([audio_mode.py:675](modes/audio_mode.py#L675)), and drawing ([audio_mode.py:695](modes/audio_mode.py#L695)) — ~480 interpreted iterations at 31 Hz. The band aggregation especially (`np.mean(fft_mag[indices])` with fancy indexing, in a Python loop) is vectorizable: precompute a band-assignment and use `np.add.reduceat` or a sparse (120 × n_bins) averaging matrix to get all band powers in one BLAS call. The smoothing/decay/gate pass is likewise vectorizable with masked NumPy ops. Drawing must stay a loop (pygame calls), but the two DSP loops shouldn't be.

### EFF-5 — `AudioMonitor` runs continuous capture + FFT + **inverse** FFT forever, in every mode, to emit one value per second — **efficiency: high (always-on waste)**

The monitor thread ([main.py:229-352](main.py#L229-L352)) captures 4096-frame blocks and runs an rfft **and** an irfft (DSP-4) roughly 12×/s — **even in `off` and `mirror` mode**, where nothing is displaying audio — just to publish one dBA number per second. Three compounding wastes:

1. The `irfft` is unnecessary (DSP-4) — drop it via Parseval and halve the transform cost immediately.
2. Better still, replace the FFT entirely with a **time-domain A-weighting IIR biquad cascade** + running RMS: this is O(N) per sample with tiny constant cost, the textbook SPL-meter design, and eliminates ~12 FFTs/s of always-on background load.
3. You only need 1 Hz output — there's no reason to wake every 85 ms. A larger block (e.g. 1 s of audio) or a lower duty cycle would cut wakeups 10×. (The current 85 ms cadence exists only to make the mic-release handoff to audio mode snappy — but that handoff is itself the Q-2 bug; fixing it with an explicit release removes the reason for the tight loop.)

On a Zero 2 with the mirror on-screen, this thread is a permanent background tax for a sensor most users glance at occasionally.

### EFF-6 — Double `aaline` per band + software rendering — **efficiency: low**

The "curve" is drawn as two offset `aaline` calls per segment for a thickness effect ([audio_mode.py:757-761](modes/audio_mode.py#L757-L761)) → ~240 anti-aliased line calls/frame, on top of 120 `draw.rect` calls, composited onto a full-screen `SRCALPHA` overlay. Whether any of this is GPU-accelerated depends on SDL landing the `opengles2` renderer — but pygame's `draw.*` primitives render on the **CPU** surface regardless of the renderer hint, so on the Zero 2 this is all software rasterization every frame. Minor relative to the FFT, but if CPU stays tight after EFF-2, halving the curve to a single `aaline` (or using `aalines` once with the full point list) is a cheap trim.

### EFF-7 — Display power-on serializes 3–4 ddcutil I2C round-trips — **efficiency: low, but user-visible latency**

On every power-**on**, `set_display_power(True)` restores brightness, contrast, and color preset ([main.py:651-657](main.py#L651-L657)) — each a separate `ddcutil setvcp` with up to a 3.5 s timeout, run **serially** over the slow I2C/DDC bus after the power command itself. Worst case that's several seconds of I2C chatter on the critical path of "turn the frame on". Since these VCP writes are independent, and DDC has no atomic multi-write, the practical win is to (a) skip the restore when the cached value already equals the target (the setters already short-circuit on unchanged values — but `force=True` here defeats that), and (b) only force-restore settings that actually differ from the monitor's power-on defaults. Or accept the latency and note it.

**Efficiency verdict:** the orchestrator's control plane is efficient (good caching, 1 Hz idle loop, parallelized cold-boot). The waste is concentrated in two always-on DSP hot spots — the 81%-overlap 8192 FFT in the renderer (EFF-2) and the FFT+IFFT monitor that runs in *every* mode (EFF-5). Fixing those two is where the Zero 2's headroom comes back.

---

## 6. 🛠️ Actionable Remediation Plan

Ordered by (impact × effort). Items 1–5 are the "do this weekend" set; R13–R18 cover the DSP/efficiency findings above.

### R1 — Remove `sudo` from the service (kills the root-escalation bridge) — SEC-1

```bash
# One-time setup (add to setup_pi.sh):
# i2c-dev at boot instead of sudo modprobe:
echo "i2c-dev" | sudo tee /etc/modules-load.d/smartframe.conf
# fb blanking without sudo:
echo 'SUBSYSTEM=="graphics", KERNEL=="fb0", RUN+="/bin/chgrp video /sys/class/graphics/fb0/blank", RUN+="/bin/chmod g+w /sys/class/graphics/fb0/blank"' \
  | sudo tee /etc/udev/rules.d/99-smartframe-fb.rules
```

```python
# main.py — before:
cmd = ["sudo", "ddcutil", "setvcp", vcp_code, value]
# after (user is already in the i2c group — sudo was never needed):
cmd = ["ddcutil", "setvcp", vcp_code, value]
```

Then remove `sudo modprobe i2c-dev` from `main.py` entirely, and change the FB strategy to a plain `sh -c "echo … > /sys/class/graphics/fb0/blank"` (group-writable via the udev rule). Finally, replace Pi OS's blanket `NOPASSWD: ALL` with nothing — the service no longer needs it.

### R2 — Restore TLS validation in the kiosk browser — SEC-2

```bash
# mirror_mode.sh — remove from CHROME_FLAGS:
--ignore-certificate-errors --allow-running-insecure-content --remote-allow-origins=*
```

If you ever need a self-signed MagicMirror cert, trust *that one cert* (`--ignore-certificate-errors-spki-list=<hash>`), never all of them.

### R3 — Move the IPC bridge out of /tmp — SEC-4

```python
# both main.py and audio_mode.py — before:
bridge_file = "/tmp/smartframe_dba"
# after:
runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
bridge_file = os.path.join(runtime_dir, "smartframe_dba")   # 0700 dir, tmpfs, per-user
```

(Longer term: delete the file bridge and have `audio_mode.py` publish dBA over MQTT directly — A-3.)

### R4 — Harden MQTT — SEC-3

```yaml
# config.example.yaml — add:
mqtt:
  port: 8883
  tls: true          # set false only on a trusted VLAN
  ca_cert: ""        # path to broker CA, empty = system store
```

```python
# main.py — before connect_async:
if mqtt_config.get("tls"):
    mqtt_client.tls_set(ca_certs=mqtt_config.get("ca_cert") or None)
if not MQTT_USER or MQTT_USER == "[MQTT_USERNAME]":
    logging.warning("MQTT running WITHOUT authentication — configure credentials.")
```

Plus: `chmod 600 config.yaml` in `setup_pi.sh`, and per-device credentials + topic ACLs on the Mosquitto side (`user smartframe` may publish only `smartframe/#`).

### R5 — Fix the orchestrator-killing races — Q-1

```python
# main.py guardian loop — before:
if current_mode != "off" and current_process:
    poll_res = current_process.poll()

# after (snapshot once, guard file races):
proc = current_process
if current_mode != "off" and proc:
    poll_res = proc.poll()
    if poll_res is not None:
        try:
            is_active = (now - os.path.getmtime(bridge_file)) < 5.0
        except OSError:
            is_active = False
        ...
```

Also wrap the *body* of the while-loop in its own `try/except Exception: logging.exception(...)` so one bad iteration never kills the appliance.

### R6 — Make audio mode retry the mic — Q-2

```python
# audio_mode.py main loop — before:
else:
    # draws "Waiting for I2S Microphone Signal..." forever

# after:
else:
    if time.time() - last_retry > 3.0:
        last_retry = time.time()
        try:
            stream = p.open(**stream_params)
        except Exception:
            stream = None
    # ... draw waiting screen
```

### R7 — `mktemp` in install_service.sh — SEC-5

```bash
# before:
sed ... scripts/smartframe.service > /tmp/smartframe.service
# after:
TMP_UNIT="$(mktemp)"
sed ... scripts/smartframe.service > "$TMP_UNIT"
sudo cp "$TMP_UNIT" /etc/systemd/system/smartframe.service
rm -f "$TMP_UNIT"
```

### R8 — Scope the process kills — SEC-6

```python
# before:
subprocess.run(["pkill", "-f", proc_name], ...)
# after — exact process-name match only:
subprocess.run(["pkill", "-x", proc_name], ...)
```

Better: you already have the PIDs (`current_process`, `_labwc_process`) and use `killpg` — trust that and drop the pkill sweep except as a `-x` last resort.

### R9 — Atomic, locked cache writes — A-5

```python
_cache_lock = threading.Lock()

def _save_cache():
    with _cache_lock:
        try:
            tmp = CACHE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_working_methods, f)
            os.replace(tmp, CACHE_FILE)
        except Exception as e:
            logging.debug(f"Cache save failed: {e}")
```

### R10 — systemd hardening (unlocked by R1)

```ini
[Service]
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={{WORKING_DIRECTORY}} /run/user/%U
PrivateTmp=true          # also neutralizes any remaining /tmp attacks
MemoryMax=200M           # orchestrator only; browser runs in user slice
```

(`PrivateTmp` requires R3 first, since the bridge file must be visible to both processes — putting it in `XDG_RUNTIME_DIR` solves that too.)

### R11 — Structural refactor (background task, not urgent)

Split `main.py` → `smartframe/` package: `config.py`, `mqtt_bridge.py`, `display.py` (strategies), `compositor.py` (labwc), `supervisor.py` (state machine + watchdog with restart-backoff, A-4/A-7), `dsp.py` (shared A-weighting — A-2). Target: no module over ~300 lines, exactly one owner per mutable state, `threading.Lock` around shared state.

### R12 — Ops hygiene

- Pin all requirements exactly; run `pip-audit` before deploys.
- Swap README's swapfile advice for zram (`systemd-zram-generator`, `zram-size = 1024`) + small SD swap as overflow — saves the SD card.
- `git tag stable` before experiments; post-merge hook already handles config sync.
- Fix the dismiss-timer comment/behavior mismatch in `mirror_mode.sh` (use absolute schedule: `for t in 8 4 4 4; do sleep $t; _dismiss_dialog; done`).

### R13 — Unify the two dB paths on one time-weighting — DSP-2 (highest-value DSP fix)

Extract one `dsp.py` with a single symmetric-exponential A/Z level function (IEC "FAST", τ=125 ms) used by **both** `AudioMonitor` and `audio_mode.py`. Publish that value to MQTT from one place. Keep the instant-attack + 250 ms peak-hold *only* for the on-screen number if you like the reactive look — never for the HA sensor.

```python
# dsp.py — shared, symmetric FAST weighting (no instant-attack in the published path)
def update_level(ema, rms, dt, tau=0.125):
    alpha = 1.0 - math.exp(-dt / tau)
    return rms if ema == 0 else ema + alpha * (rms - ema)
```

### R14 — Get A-weighted RMS via Parseval (or an IIR biquad); delete the inverse FFT — DSP-4 / EFF-5

```python
# main.py AudioMonitor — before:
fft_complex = np.fft.rfft(data)
fft_aw = fft_complex * a_gains
data_aw = np.fft.irfft(fft_aw)            # wasted
rms_a = np.sqrt(np.mean(data_aw**2))

# after — RMS straight from the weighted spectrum (matches audio_mode's chunk path):
X = np.fft.rfft(data * np.hanning(len(data)))
mag_sq = np.abs(X * a_gains) ** 2
energy = mag_sq[0] + 2 * mag_sq[1:-1].sum() + mag_sq[-1]
rms_a = np.sqrt(max(1e-12, energy)) * norm      # norm = 1/(N·window_rms)
```

Better long-term: replace the FFT entirely with a 6th-order A-weighting IIR biquad cascade + running RMS — the standard SPL-meter design, O(N) and far cheaper for an always-on monitor.

### R15 — Decouple the spectrum FFT from the frame rate — EFF-2

```python
# audio_mode.py main loop — compute the expensive 8192 FFT every Nth frame:
FFT_EVERY = 2                      # 8192 FFT at ~15 Hz; bars interpolate between
if frame_count % FFT_EVERY == 0:
    windowed = audio_buffer * fft_window
    fft_complex = np.fft.rfft(windowed)
    # ... band aggregation ...
frame_count += 1
# the cheap 1536 A/Z chunk FFTs stay per-frame for responsive dBA
```

Also drop `clock.tick(60)`→ the blocking `stream.read` already paces the loop (EFF-1); or read a larger chunk to lower the frame rate deliberately.

### R16 — Vectorize band aggregation; power-average, not magnitude-average — DSP-5 / EFF-4

```python
# before (Python loop, mean of magnitudes):
for i, indices in enumerate(band_indices):
    mag = np.mean(fft_mag[indices])

# after (one vectorized call, RMS of bin power):
power = fft_mag ** 2
band_power = np.add.reduceat(power, band_starts) / band_counts   # precomputed
band_rms = np.sqrt(band_power)                                    # correct band energy
```

### R17 — Ring buffer instead of `np.roll` — EFF-3

```python
# before:
audio_buffer = np.roll(audio_buffer, -len(new_data))
audio_buffer[-len(new_data):] = new_data
# after (in-place shift, no allocation):
n = len(new_data)
audio_buffer[:-n] = audio_buffer[n:]
audio_buffer[-n:] = new_data
```

### R18 — Document the meter's limits + honest low-end handling — DSP-1 / DSP-3 / DSP-6

- README/config: state that the reading is a **single-point-calibrated relative dBA**, accurate near the calibration level; readings **<45 dBA are indicative only** (DSP-3).
- Replace the hardcoded sub-45 fudge with a measured noise-floor subtraction in the power domain (`P = max(0, P_meas − P_floor)`) from a one-time silence sample.
- Offer an optional two-point (gain+offset) calibration for users who want better absolute accuracy across the range.

---

## Verdict

For a solo hobby appliance this is **well above average** — validated MQTT inputs, safe YAML loading, thoughtful hardware fallback chains, and a forward-DSP layer (A-weighting curve, Parseval normalization) that is genuinely correct. But it is currently **one LAN MITM away from root** (SEC-1 + SEC-2 chain), the orchestrator **can crash itself via its own thread races** (Q-1), and audio mode **cannot recover from a busy microphone** (Q-2). R1–R6 close all three and are each under an hour of work.

On the algorithms specifically: the math is right but the **statistics layered on top are inconsistent** — the published dBA sensor visibly steps by several dB when you switch modes because the two code paths use different time-weighting (DSP-2), and every reading below ~45 dBA is a cosmetic curve rather than a measurement (DSP-3). On efficiency, the Zero 2's headroom is eaten almost entirely by two always-on hot spots: an 81%-overlap 8192-point FFT recomputed 31×/s in the renderer (EFF-2) and a redundant FFT+**inverse**-FFT monitor thread that runs in *every* mode, even when no audio is on screen (EFF-4/EFF-5). R13–R15 recover most of that headroom and, conveniently, R14 fixes a correctness nit and an efficiency waste in the same edit.
