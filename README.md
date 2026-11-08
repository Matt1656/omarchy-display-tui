# Omarchy Display

A Python standard-library TUI for Omarchy with Hyprland's Lua configuration (0.55+),
written by **OpenAI Astra**.

## Installation

Requires Linux, Python 3.10+, Omarchy with Hyprland's Lua configuration, systemd's
user session, and `dbus-update-activation-environment`. Run it as your desktop user,
not root. The application launcher uses Foot, supplied with the target Omarchy setup.

```sh
git clone https://github.com/Matt1656/omarchy-display-tui.git
cd omarchy-display-tui
python3 install.py
```

Alternatively, run `python3 omarchy_display.py` directly from the checkout.

## Usage

Run `omarchy-display`, or choose **Omarchy Display** from your application launcher.
Use arrows and Enter to navigate, Escape to go back. Edits remain staged until
**Apply staged settings**. Press **Y** within 20 seconds to keep the result;
otherwise the previous files and session scaling are restored. A detached
25-second watchdog also handles terminal closure or a TUI crash.

## Features

- Discover all connected outputs, including disabled outputs, with `hyprctl`.
- List reported resolution/refresh combinations and the current mode separately.
- Choose automatic preferred/highest-resolution/highest-refresh modes or enter a custom mode.
- Dedicated custom-mode editor with separate width, height and refresh-rate fields (including fractional Hz).
- Per-output fractional scaling, logical position, rotation/flip, enable/disable, VRR and mirroring.
- Preserve profiles for disconnected outputs across saves.
- GTK scale and text multiplier; Qt global and per-output factors, font DPI and high-DPI switches;
  Steam UI scale; Hyprland/X11 cursor sizes; XWayland compositor scaling.
- Preview, configuration-error checking, backups and timed rollback.
- Screensaver and automatic lock timeouts, with seconds/minutes/hours input and a disable option.
- No third-party Python dependencies.

```sh
omarchy-display --list       # live monitor/mode discovery as JSON
omarchy-display --demo       # simulated two-monitor UI; writes nothing
python -m unittest -v        # tests from this source directory
```

For a custom mode, select a monitor, then **Custom resolution and refresh rate**.
Enter width, height and Hz, for example `1920`, `1080`, `59.94`. The mode is staged
until Apply and remains subject to the confirmation timeout; hardware may reject
unadvertised modes. This specifies a mode request, not a custom modeline/EDID override.

Select **Screensaver and lock settings** from the main menu to configure each
timeout. Enter seconds, `5m`, or `1h`; `0` disables that timer. Both are measured
from the last activity, not from one another. Set the screensaver timeout below
the lock timeout if you want to see the screensaver before locking.

Idle settings are merged into `~/.config/omarchy/shell.json` only after you press
**Y** to keep the settings. Omarchy hot-reloads the file. Other shell settings,
including your bar and plugins, are preserved. This requires an Omarchy version
with the Quickshell idle service; existing stay-awake/idle inhibition still applies.

The app writes `~/.config/hypr/omarchy-display.lua` and adds a `dofile` at the end
of `~/.config/hypr/hyprland.lua` on first Apply. Original monitor files remain intact;
the later TUI rules override them. Avoid alternating this app and nwg-displays:
the TUI's saved rules take precedence while its include is active.

Backups and saved profiles live in `~/.local/state/omarchy-display/`. Each
`backup-*/snapshot.json` contains the previous managed files and session scaling.
To stop using these overrides, remove the `omarchy-display.lua` dofile line from
`hyprland.lua`, run `hyprctl reload`, and log out/in to reset application environments.

Application scaling is published to the user session for newly launched apps.
Fully quit and restart affected apps; log out/in if launchers retain old environments.
GTK/Qt support varies by application/version. Qt 6 may ignore legacy switches.
These are desktop/toolkit factors, not universal app zoom: browser/Electron zoom,
game UI sizes, terminal font sizes, and application-specific settings remain in those
apps. Flatpak applications may filter environment overrides.

Some monitors/adapters provide incomplete EDID mode lists. The TUI reports what
Hyprland supplies and does not claim that a custom or current mode is advertised.
Negative positions are supported; positions use scaled logical pixels.

Reference: [Hyprland monitor configuration](https://wiki.hypr.land/configuring/core/monitors/)
and [mode discovery](https://wiki.hypr.land/configuring/core/monitors/modes/).
