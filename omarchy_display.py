#!/usr/bin/env python3
"""Dependency-free monitor and application scaling manager for Omarchy/Hyprland Lua."""
import argparse
import copy
import curses
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

CONFIG = Path.home() / '.config/hypr'
STATE = Path.home() / '.local/state/omarchy-display'
MANAGED = CONFIG / 'omarchy-display.lua'
MAIN = CONFIG / 'hyprland.lua'
SETTINGS = STATE / 'settings.json'
SHELL = Path.home() / '.config/omarchy/shell.json'
SHELL_DEFAULTS = Path(os.getenv('OMARCHY_PATH', '/usr/share/omarchy')) / 'config/omarchy/shell.json'
INCLUDE = 'dofile(os.getenv("HOME") .. "/.config/hypr/omarchy-display.lua")'
ENV = {
    'GDK_SCALE': ('GTK integer scale', '1', 1, 4, True),
    'GDK_DPI_SCALE': ('GTK text multiplier', '1', .25, 4, False),
    'QT_SCALE_FACTOR': ('Qt global multiplier', '1', .25, 4, False),
    'QT_FONT_DPI': ('Qt font DPI (96 = normal)', '96', 48, 384, True),
    'QT_ENABLE_HIGHDPI_SCALING': ('Qt high-DPI support (0/1)', '1', 0, 1, True),
    'QT_AUTO_SCREEN_SCALE_FACTOR': ('Qt automatic screen scale (0/1)', '0', 0, 1, True),
    'STEAM_FORCE_DESKTOPUI_SCALING': ('Steam interface multiplier', '1', .5, 4, False),
    'XCURSOR_SIZE': ('X11 cursor size', '24', 8, 128, True),
    'HYPRCURSOR_SIZE': ('Hyprland cursor size', '24', 8, 128, True),
    'QT_SCREEN_SCALE_FACTORS': ('Qt per-output factors (blank = automatic)', '', 0, 0, False),
}


def run(*args, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=12)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f'{args[0]} failed')
    return result.stdout.strip()


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def session_env():
    # JSON avoids shell quoting in values returned by show-environment.
    raw = run('systemctl', '--user', 'show-environment', '--output=json')
    value = json.loads(raw)
    if isinstance(value, list):
        return dict(item.split('=', 1) for item in value if '=' in item)
    return value


def discover():
    return json.loads(run('hyprctl', '-j', 'monitors', 'all'))


def current_mode(monitor):
    if monitor.get('width', 0) and monitor.get('height', 0):
        return f"{monitor['width']}x{monitor['height']}@{monitor.get('refreshRate', 60):.5f}".rstrip('0').rstrip('.')
    return 'preferred'


def mode_choices(monitor):
    reported = list(dict.fromkeys(x.removesuffix('Hz') for x in monitor.get('availableModes', [])))
    return [(current_mode(monitor), 'Current mode (may not be advertised)')] + [(m, 'Reported by monitor') for m in reported] + [(m, 'Automatic selection') for m in ('preferred', 'highres', 'highrr')]


def from_monitor(m):
    return dict(output=m['name'], mode=current_mode(m), scale=m.get('scale', 1) or 1,
                position=f"{m.get('x', 0)}x{m.get('y', 0)}", transform=m.get('transform', 0),
                disabled=m.get('disabled', False), vrr=int(m.get('vrr', False)), mirror=m.get('mirrorOf', 'none').replace('none', ''))


def initial(monitors, environ, saved=None):
    saved = saved or {}
    # Preserve profiles for unplugged monitors, use live values for connected outputs.
    profiles = copy.deepcopy(saved.get('monitors', {}))
    for m in monitors:
        profiles[m['name']] = from_monitor(m)
    values = {k: environ.get(k, spec[1]) for k, spec in ENV.items()}
    return {'monitors': profiles, 'env': values, 'xwayland': saved.get('xwayland', True)}


def number(value, low, high, integer=False):
    n = float(value)
    if not math.isfinite(n) or not low <= n <= high or (integer and not n.is_integer()):
        raise ValueError(f'Expected {"integer" if integer else "number"} between {low} and {high}')
    return int(n) if integer else n


def env_value(key, value):
    if key == 'QT_SCREEN_SCALE_FACTORS':
        if value:
            for item in value.split(';'):
                if not re.fullmatch(r'[A-Za-z0-9_.:-]+=[0-9.]+', item):
                    raise ValueError('Use OUTPUT=factor;OUTPUT=factor, or leave blank')
                number(item.split('=')[1], .25, 4)
        return value
    return str(number(value, *ENV[key][2:]))


def custom_mode(width, height, refresh):
    width = number(width, 1, 32768, True)
    height = number(height, 1, 32768, True)
    refresh = number(refresh, .01, 1000)
    return f'{width}x{height}@{refresh:g}'


def shell_config(path):
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get('idle', {}), dict):
        raise ValueError(f'{path}: expected a JSON object with an idle object')
    return data


def idle_settings():
    defaults = {'screensaver': 150, 'lock': 300}
    defaults.update(shell_config(SHELL_DEFAULTS).get('idle', {}))
    defaults.update(shell_config(SHELL).get('idle', {}))
    return {k: int(number(defaults[k], 0, 2147483, True)) for k in ('screensaver', 'lock')}


def parse_timeout(value):
    match = re.fullmatch(r'(\d+)\s*([smh]?)', value.strip().lower())
    if not match:
        raise ValueError('Enter whole seconds, or a duration such as 5m or 1h; 0 disables.')
    seconds = int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[match[2]]
    return number(seconds, 0, 2147483, True)


def render_shell(idle):
    # Merge against the latest file so bar/plugin settings are preserved.
    data = shell_config(SHELL)
    data.setdefault('idle', {}).update(idle)
    return json.dumps(data, indent=2) + '\n'


def validate(settings, connected):
    profiles = settings['monitors']
    if not connected or not any(not profiles[n]['disabled'] for n in connected):
        raise ValueError('Keep at least one connected monitor enabled.')
    for name, m in profiles.items():
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', name):
            raise ValueError('Invalid output name')
        if not re.fullmatch(r'(preferred|highres|highrr|\d+x\d+@\d+(\.\d+)?)', m['mode']):
            raise ValueError('Mode must be WIDTHxHEIGHT@HZ or preferred/highres/highrr')
        if '@' in m['mode']:
            width, height, refresh = re.split('[x@]', m['mode'])
            custom_mode(width, height, refresh)
        if m['scale'] != 'auto':
            number(m['scale'], .25, 4)
        if not re.fullmatch(r'(auto|-?\d+x-?\d+)', m['position']):
            raise ValueError('Position must be XxY (e.g. 1920x0 or -1920x0), or auto')
        number(m['transform'], 0, 7, True)
        number(m['vrr'], 0, 3, True)
        mirror = m.get('mirror', '')
        if mirror and (mirror == name or mirror not in connected or profiles[mirror]['disabled'] or profiles[mirror].get('mirror')):
            raise ValueError('Mirror target must be a different enabled, non-mirrored connected output')
    for key in ENV:
        env_value(key, settings['env'][key])
    if 'idle' in settings:
        for key in ('screensaver', 'lock'):
            number(settings['idle'][key], 0, 2147483, True)


def lua(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render(settings):
    lines = ['-- Managed by omarchy-display. Edit using the TUI.']
    for m in settings['monitors'].values():
        fields = ', '.join(f'{k} = {lua(v)}' for k, v in m.items())
        lines.append('hl.monitor({ ' + fields + ' })')
    lines.append('hl.config({ xwayland = { force_zero_scaling = ' + lua(settings['xwayland']) + ' } })')
    lines.extend(f'hl.env({lua(k)}, {lua(v)})' for k, v in settings['env'].items())
    return '\n'.join(lines) + '\n'


def reload_config():
    run('hyprctl', 'reload')
    errors = run('hyprctl', 'configerrors')
    if errors.strip():
        raise RuntimeError(errors)


def update_env(values):
    entries = [f'{k}={v}' for k, v in values.items() if v is not None]
    if entries:
        run('dbus-update-activation-environment', '--systemd', *entries)
    missing = [k for k, v in values.items() if v is None]
    if missing:
        # D-Bus cannot remove values, clear them there, then remove from systemd.
        run('dbus-update-activation-environment', *[k + '=' for k in missing])
        run('systemctl', '--user', 'unset-environment', *missing)


def restore(transaction):
    data = json.loads((transaction / 'snapshot.json').read_text())
    for path, content in data['files'].items():
        p = Path(path)
        if content is None:
            p.unlink(missing_ok=True)
        else:
            atomic(p, content)
    reload_config()
    update_env(data['env'])


def finish(transaction, keep):
    with (transaction / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = transaction / 'status'
        if status.read_text() != 'pending':
            return False
        if keep:
            pending_idle = transaction / 'idle.json'
            if pending_idle.exists():
                try:
                    atomic(SHELL, render_shell(json.loads(pending_idle.read_text())))
                except Exception:
                    restore(transaction)
                    atomic(status, 'reverted')
                    raise
        if not keep:
            try:
                restore(transaction)
            except Exception as exc:
                atomic(transaction / 'error', str(exc))
                atomic(status, 'rollback-error')
                raise
        atomic(status, 'kept' if keep else 'reverted')
        return True


def watchdog(transaction):
    time.sleep(25)
    finish(transaction, False)


def begin(settings, connected):
    validate(settings, connected)
    if not MAIN.exists():
        raise RuntimeError('Expected Omarchy Hyprland Lua config at ' + str(MAIN))
    reload_config()  # Refuse to layer changes onto an already invalid configuration.
    STATE.mkdir(parents=True, exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix='backup-', dir=STATE))
    old_env = session_env()
    files = {str(p): p.read_text() if p.exists() else None for p in (MAIN, MANAGED, SETTINGS)}
    if 'idle' in settings and settings['idle'] != idle_settings():
        # Delay idle changes until confirmation: a short lock timeout must not
        # lock the user out of the display confirmation screen.
        files[str(SHELL)] = SHELL.read_text() if SHELL.exists() else None
        atomic(transaction / 'idle.json', json.dumps(settings['idle']))
    atomic(transaction / 'snapshot.json', json.dumps({'files': files, 'env': {k: old_env.get(k) for k in ENV}}))
    atomic(transaction / 'status', 'pending')
    # Detached rollback survives a dead terminal or crashed TUI.
    try:
        with (transaction / 'watchdog.log').open('w') as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--watchdog', str(transaction)],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        with (transaction / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (transaction / 'status').read_text() != 'pending':
                raise RuntimeError('Apply expired before it could start')
            atomic(MANAGED, render(settings))
            main = files[str(MAIN)]
            if INCLUDE not in main:
                atomic(MAIN, main.rstrip() + '\n\n-- Omarchy Display TUI overrides\n' + INCLUDE + '\n')
            atomic(SETTINGS, json.dumps(settings, indent=2) + '\n')
            reload_config()
            update_env(settings['env'])
        return transaction
    except Exception:
        finish(transaction, False)
        raise


DEMO = [dict(name='DP-1', description='Example 27-inch display', width=2560, height=1440,
             refreshRate=144., scale=1., x=0, y=0, availableModes=['2560x1440@144.00Hz', '2560x1440@60.00Hz', '1920x1080@60.00Hz']),
        dict(name='HDMI-A-1', description='Example secondary display', width=1920, height=1080,
             refreshRate=60., scale=1., x=2560, y=0, availableModes=['1920x1080@60.00Hz', '1280x720@60.00Hz'])]


class UI:
    def __init__(self, screen, demo=False):
        self.s = screen
        self.demo = demo
        self.message = 'Changes are staged until Apply. App scaling takes effect after restarting apps.'
        self.dirty = False
        curses.curs_set(0)
        self.s.keypad(True)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
        self.refresh()

    def refresh(self):
        self.monitors = copy.deepcopy(DEMO) if self.demo else discover()
        saved = json.loads(SETTINGS.read_text()) if SETTINGS.exists() and not self.demo else {}
        self.settings = initial(self.monitors, {} if self.demo else session_env(), saved)
        self.settings['idle'] = {'screensaver': 150, 'lock': 300} if self.demo else idle_settings()
        if not self.demo:
            option = json.loads(run('hyprctl', '-j', 'getoption', 'xwayland:force_zero_scaling'))
            self.settings['xwayland'] = bool(option.get('bool', option.get('int', 1)))
        self.dirty = False

    def put(self, y, text, attr=0):
        h, w = self.s.getmaxyx()
        if 0 <= y < h - 1:
            try:
                self.s.addnstr(y, 2, str(text), max(0, w - 4), attr)
            except curses.error:
                pass

    def menu(self, title, items, note='', selected=0):
        while True:
            self.s.erase()
            h, w = self.s.getmaxyx()
            self.put(0, 'OMARCHY / DISPLAY' + ('   DEMO — no changes written' if self.demo else ''), curses.A_BOLD)
            self.put(2, title, curses.A_BOLD)
            capacity = max(1, h - 10)
            start = max(0, selected - capacity + 1)
            for row, label in enumerate(items[start:start + capacity], 4):
                idx = start + row - 4
                self.put(row, ('› ' if idx == selected else '  ') + label, curses.A_REVERSE if idx == selected else 0)
            self.put(h - 5, note)
            self.put(h - 3, self.message)
            self.put(h - 2, '↑/↓ select  Enter edit  Esc back  PgUp/PgDn scroll')
            self.s.refresh()
            key = self.s.getch()
            if key in (27, ord('q')):
                return None
            if key in (curses.KEY_UP, ord('k')):
                selected = (selected - 1) % len(items)
            elif key in (curses.KEY_DOWN, ord('j')):
                selected = (selected + 1) % len(items)
            elif key == curses.KEY_NPAGE:
                selected = min(len(items) - 1, selected + capacity)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - capacity)
            elif key in (10, 13, curses.KEY_ENTER):
                return selected

    def prompt(self, title, value, help_text):
        text = str(value)
        while True:
            self.s.erase()
            self.put(1, title, curses.A_BOLD)
            self.put(3, help_text)
            self.put(5, '> ' + text + '▏')
            self.put(8, 'Type value; Backspace edits; Ctrl+U clears; Enter accepts; Esc cancels')
            self.s.refresh()
            key = self.s.get_wch()
            if key == '\x1b':
                return None
            if key in ('\n', '\r'):
                return text.strip()
            if key in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                text = text[:-1]
            elif key == '\x15':
                text = ''
            elif isinstance(key, str) and key.isprintable() and len(text) < 100:
                text += key

    def monitor(self, monitor):
        m = self.settings['monitors'][monitor['name']]
        while True:
            keys = ['mode', 'custom', 'scale', 'position', 'transform', 'disabled', 'vrr', 'mirror']
            labels = ['Resolution @ refresh', 'Custom resolution and refresh rate', 'Monitor scale', 'Position (logical pixels)', 'Rotation / flip (0–7)', 'Disabled', 'Variable refresh (0–3)', 'Mirror output']
            idx = self.menu(monitor['name'] + ' — ' + (monitor.get('description') or 'No display identification'),
                            [f'{label}: {m[key]}' if key != 'custom' else label + '…' for label, key in zip(labels, keys)] + ['Back'],
                            'Rotation: 0 normal, 1=90°, 2=180°, 3=270°; 4–7 flipped')
            if idx is None or idx == len(keys):
                return
            key = keys[idx]
            try:
                if key == 'custom':
                    value = self.custom_mode(monitor, m)
                    key = 'mode'
                elif key == 'mode':
                    choices = mode_choices(monitor)
                    choice = self.menu('Detected modes for ' + monitor['name'], [f'{v}  — {label}' for v, label in choices] + ['Enter custom mode…'], 'Modes come from Hyprland; missing EDID can limit this list.')
                    if choice is None:
                        continue
                    value = choices[choice][0] if choice < len(choices) else self.custom_mode(monitor, m)
                elif key == 'disabled':
                    value = not m[key]
                elif key == 'mirror':
                    choices = [''] + [x['name'] for x in self.monitors if x['name'] != monitor['name']]
                    choice = self.menu('Mirror another monitor', [x or 'No mirroring' for x in choices])
                    value = None if choice is None else choices[choice]
                else:
                    hints = {'scale': '0.25–4 or auto. 1 = 100%; 1.5 = 150%.', 'position': 'XxY or auto. Negative coordinates allowed. Use scaled logical dimensions.', 'transform': '0–3 rotations; 4–7 flipped rotations', 'vrr': '0 off; 1 on; 2 fullscreen; 3 fullscreen games/video'}
                    value = self.prompt(labels[idx], m[key], hints[key])
                    if value is not None and key in ('scale', 'transform', 'vrr'):
                        value = 'auto' if key == 'scale' and value == 'auto' else number(value, .25 if key == 'scale' else 0, 4 if key == 'scale' else 7 if key == 'transform' else 3, key != 'scale')
                if value is not None:
                    m[key] = value
                    self.dirty = True
            except ValueError as exc:
                self.message = str(exc)

    def custom_mode(self, monitor, profile):
        mode = profile['mode'] if '@' in profile['mode'] else current_mode(monitor)
        values = re.split('[x@]', mode) if '@' in mode else ['1920', '1080', '60']
        for i, label in enumerate(('Width in pixels', 'Height in pixels', 'Refresh rate in Hz')):
            value = self.prompt('Custom mode — ' + label, values[i],
                                'Decimal refresh rates supported, e.g. 59.94. Escape cancels the whole edit.')
            if value is None:
                return None
            number(value, .01 if i == 2 else 1, 1000 if i == 2 else 32768, i != 2)
            values[i] = value
        return custom_mode(*values)

    def idle(self):
        keys = ('screensaver', 'lock')
        while True:
            values = self.settings['idle']
            labels = [f'{name}: ' + (f'{values[key]} seconds' if values[key] else 'Disabled')
                      for name, key in zip(('Screensaver timeout', 'Automatic lock timeout'), keys)]
            note = 'Both timers start at last activity; 0 disables. Saved only after Apply + Y.'
            if values['lock'] and values['screensaver'] and values['lock'] <= values['screensaver']:
                note = 'Lock runs before/with screensaver. Set screensaver earlier to see it before locking.'
            idx = self.menu('Screensaver and lock settings', labels + ['Back'], note)
            if idx is None or idx == 2:
                return
            key = keys[idx]
            value = self.prompt(labels[idx].split(':')[0], values[key],
                                'Seconds since last activity; also accepts 5m or 1h. Enter 0 to disable.')
            if value is not None:
                try:
                    values[key] = parse_timeout(value)
                    self.dirty = True
                except ValueError as exc:
                    self.message = str(exc)

    def scaling(self):
        keys = list(ENV)
        while True:
            idx = self.menu('Application scaling', [f'{ENV[k][0]}: {self.settings["env"][k]}' for k in keys] + [f'XWayland: disable compositor scaling: {self.settings["xwayland"]}', 'Set neutral scaling (100%, cursors 24px)', 'Back'],
                            'App factors can multiply monitor scaling. Restart apps after Apply.')
            if idx is None or idx == len(keys) + 2:
                return
            if idx == len(keys):
                self.settings['xwayland'] = not self.settings['xwayland']
                self.dirty = True
            elif idx == len(keys) + 1:
                self.settings['env'] = {k: spec[1] for k, spec in ENV.items()}
                self.settings['xwayland'] = True
                self.dirty = True
            else:
                key = keys[idx]
                spec = ENV[key]
                hint = 'Example: DP-1=1.5;HDMI-A-1=1. Blank uses automatic scaling.' if key == 'QT_SCREEN_SCALE_FACTORS' else f'{key} — range {spec[2]}–{spec[3]}'
                value = self.prompt(spec[0], self.settings['env'][key], hint)
                if value is not None:
                    try:
                        self.settings['env'][key] = env_value(key, value)
                        self.dirty = True
                    except ValueError as exc:
                        self.message = str(exc)

    def apply(self):
        if self.demo:
            validate(self.settings, [m['name'] for m in self.monitors])
            self.message = 'Demo settings validated. Nothing written.'
            return
        transaction = begin(self.settings, [m['name'] for m in discover()])
        deadline = time.monotonic() + 20
        self.s.timeout(200)
        try:
            while time.monotonic() < deadline:
                self.s.erase()
                self.put(2, 'KEEP THESE DISPLAY SETTINGS?', curses.A_BOLD)
                self.put(4, f'Press Y to keep. Any other key reverts. {math.ceil(deadline - time.monotonic())} seconds remaining.')
                self.put(6, 'An independent watchdog restores the old configuration if this app exits.')
                self.s.refresh()
                key = self.s.getch()
                if key == curses.KEY_RESIZE:
                    continue
                if key != -1:
                    kept = finish(transaction, key in (ord('y'), ord('Y')))
                    self.message = 'Saved. Restart affected apps; log out/in if needed.' if kept and key in (ord('y'), ord('Y')) else 'Previous settings restored.'
                    break
            else:
                finish(transaction, False)
                self.message = 'Timed out: previous settings restored.'
        finally:
            self.s.timeout(-1)
        self.refresh()

    def loop(self):
        selected = 0
        while True:
            items = [f'{m["name"]}  {self.settings["monitors"][m["name"]]["mode"]}  scale {self.settings["monitors"][m["name"]]["scale"]}' for m in self.monitors]
            actions = ['Application scaling', 'Screensaver and lock settings', 'Preview generated configuration', 'Apply staged settings', 'Rescan monitors / discard staged changes', 'Help', 'Quit']
            selected = self.menu('Connected monitors' + ('  • unsaved changes' if self.dirty else ''), items + actions,
                                 'Multi-monitor profiles are preserved when outputs disconnect.', min(selected or 0, len(items + actions) - 1))
            if selected is None or selected == len(items) + 6:
                if not self.dirty or self.menu('Discard staged changes and quit?', ['Keep editing', 'Discard and quit']) == 1:
                    return
                continue
            try:
                if selected < len(items):
                    self.monitor(self.monitors[selected])
                elif selected == len(items):
                    self.scaling()
                elif selected == len(items) + 1:
                    self.idle()
                elif selected == len(items) + 2:
                    self.menu('Preview — nothing written', render(self.settings).splitlines() +
                              ['', 'Omarchy shell idle settings (applied after confirmation):',
                               json.dumps(self.settings['idle'])])
                elif selected == len(items) + 3:
                    self.apply()
                elif selected == len(items) + 4:
                    if not self.dirty or self.menu('Discard staged changes and rescan?', ['Cancel', 'Rescan']) == 1:
                        self.refresh()
                elif selected == len(items) + 5:
                    self.menu('Help', [
                        'Choose a monitor to edit its mode, scale, position, rotation or mirroring.',
                        'Each mode combines resolution and refresh rate. Current mode is listed separately.',
                        'Custom resolution and refresh rate: enter width, height and Hz separately.',
                        'Screensaver/lock: seconds since last activity, or 5m/1h; 0 disables.',
                        'Idle timers are saved to shell.json only after you confirm display settings.',
                        'Position uses logical pixels: 3840px at scale 2 occupies 1920px.',
                        'Apply writes a separate Lua file, then reloads and validates Hyprland.',
                        'Confirm within 20 seconds; otherwise settings are rolled back.',
                        'Application scaling affects newly launched apps; existing apps need restarting.',
                        'Electron/browser zoom and game-specific UI scales remain in each app.',
                        'Qt per-screen environment overrides can multiply the global scale.',
                        'This app overrides nwg-displays while its include is active.',
                        'Backups: ~/.local/state/omarchy-display/backup-*',
                        'Remove the omarchy-display dofile line to return to your original monitor config.',
                        'Use --list for JSON discovery or --demo for a safe preview.'])
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, KeyError) as exc:
                self.message = 'Error: ' + str(exc).replace('\n', ' ')[:300]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='two simulated monitors; no changes written')
    parser.add_argument('--list', action='store_true', help='print detected monitors and modes as JSON')
    parser.add_argument('--watchdog', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.watchdog:
        watchdog(args.watchdog)
        return
    if args.list:
        print(json.dumps(DEMO if args.demo else discover(), indent=2))
        return
    if args.demo:
        curses.wrapper(lambda screen: UI(screen, True).loop())
        return
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'app.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another omarchy-display instance is already running.')
        pending = [p for p in STATE.glob('backup-*/status') if p.read_text() == 'pending']
        if pending and not args.demo:
            raise RuntimeError('A display change is pending rollback. Retry in 30 seconds.')
        curses.wrapper(lambda screen: UI(screen, args.demo).loop())


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'omarchy-display: {exc}', file=sys.stderr)
        sys.exit(1)
