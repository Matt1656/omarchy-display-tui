#!/usr/bin/env python3
"""Install Omarchy Display for the current user, without root privileges."""
from pathlib import Path
import shutil


def main():
    home = Path.home()
    source = Path(__file__).resolve().parent
    target = home / '.local/share/omarchy-display'
    target.mkdir(parents=True, exist_ok=True)
    for name in ('omarchy_display.py', 'README.md'):
        shutil.copy2(source / name, target / name)
    binary = home / '.local/bin/omarchy-display'
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text('#!/bin/sh\nexec /usr/bin/python3 "$HOME/.local/share/omarchy-display/omarchy_display.py" "$@"\n')
    binary.chmod(0o755)
    # Desktop Exec is not shell syntax; quote its executable argument explicitly.
    executable = str(binary).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    desktop = home / '.local/share/applications/omarchy-display.desktop'
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(
        '[Desktop Entry]\nType=Application\nName=Omarchy Display\n'
        'Comment=Monitor modes, layouts and application scaling\n'
        f'Exec=/usr/bin/foot -T "Omarchy Display" "{executable}"\n'
        'Icon=video-display\nTerminal=false\nCategories=Settings;HardwareSettings;\n'
        'Keywords=monitor;resolution;refresh;scaling;display;\n'
    )
    print(f'Installed {binary}\nSearch for Omarchy Display in your launcher.')


if __name__ == '__main__':
    main()
