import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import omarchy_display as app


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.settings = app.initial(app.DEMO, {})
        self.connected = [m['name'] for m in app.DEMO]

    def test_multimonitor_and_disconnected_profiles(self):
        self.settings['monitors']['USB-C-1'] = dict(self.settings['monitors']['DP-1'], output='USB-C-1')
        result = app.initial(app.DEMO, {}, self.settings)
        self.assertEqual(len(result['monitors']), 3)
        app.validate(result, self.connected)

    def test_current_mode_retained_when_not_advertised(self):
        monitor = dict(app.DEMO[0], availableModes=['640x480@59.94Hz'])
        choices = app.mode_choices(monitor)
        self.assertEqual(choices[0][0], '2560x1440@144')
        self.assertEqual(choices[1][0], '640x480@59.94')

    def test_last_screen_cannot_be_disabled(self):
        for m in self.settings['monitors'].values():
            m['disabled'] = True
        with self.assertRaises(ValueError):
            app.validate(self.settings, self.connected)

    def test_bad_scaling_and_injection_rejected(self):
        for bad in ('nan', 'inf', '0', '5', '-1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.number(bad, .25, 4)
        self.settings['monitors']['DP-1']['position'] = '0x0"); os.execute("bad")'
        with self.assertRaises(ValueError):
            app.validate(self.settings, self.connected)

    def test_mirror_loop_rejected(self):
        self.settings['monitors']['DP-1']['mirror'] = 'HDMI-A-1'
        self.settings['monitors']['HDMI-A-1']['mirror'] = 'DP-1'
        with self.assertRaises(ValueError):
            app.validate(self.settings, self.connected)

    def test_lua_and_app_factors(self):
        self.settings['monitors']['DP-1']['position'] = '-2560x0'
        self.settings['monitors']['DP-1']['scale'] = 1.25
        app.validate(self.settings, self.connected)
        text = app.render(self.settings)
        self.assertIn('scale = 1.25', text)
        self.assertIn('disabled = false', text)
        self.assertIn('hl.env("STEAM_FORCE_DESKTOPUI_SCALING", "1")', text)
        self.assertIn('output = "HDMI-A-1"', text)

    def test_custom_mode_fractional_refresh(self):
        self.assertEqual(app.custom_mode('1920', '1080', '59.940'), '1920x1080@59.94')
        self.settings['monitors']['DP-1']['mode'] = '1024x768@84.94'
        app.validate(self.settings, self.connected)

    def test_custom_mode_invalid_values(self):
        for values in [('0', '1080', '60'), ('1920.5', '1080', '60'),
                       ('1920', '1080', 'nan'), ('1920', '1080', '0')]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                app.custom_mode(*values)
        self.settings['monitors']['DP-1']['mode'] = '0x1080@60'
        with self.assertRaises(ValueError):
            app.validate(self.settings, self.connected)

    def test_timeout_units_and_disabling(self):
        for value, expected in [('0', 0), ('45', 45), ('5m', 300), ('1h', 3600), ('10 s', 10)]:
            self.assertEqual(app.parse_timeout(value), expected)
        for value in ('-1', '1.5m', 'nan', '999999h', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.parse_timeout(value)

    def test_custom_wizard_cancel_does_not_change_profile(self):
        ui = object.__new__(app.UI)
        profile = self.settings['monitors']['DP-1']
        old = copy.deepcopy(profile)
        with patch.object(ui, 'prompt', side_effect=['1920', None]):
            self.assertIsNone(ui.custom_mode(app.DEMO[0], profile))
        self.assertEqual(profile, old)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for key, name in [('MAIN', 'hyprland.lua'), ('MANAGED', 'managed.lua'), ('SETTINGS', 'settings.json'), ('STATE', 'state'), ('SHELL', 'shell.json'), ('SHELL_DEFAULTS', 'defaults.json')]:
            p = patch.object(app, key, self.root / name)
            p.start()
            self.addCleanup(p.stop)
        app.MAIN.write_text('-- original\n')
        self.settings = app.initial(app.DEMO, {})
        self.connected = [m['name'] for m in app.DEMO]
        for target in ('reload_config', 'update_env', 'subprocess.Popen'):
            p = patch.object(app, target) if '.' not in target else patch('omarchy_display.' + target)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(app, 'session_env', return_value={'GDK_SCALE': '2'})
        p.start()
        self.addCleanup(p.stop)

    def test_revert_restores_original_files_and_environment(self):
        transaction = app.begin(self.settings, self.connected)
        self.assertIn(app.INCLUDE, app.MAIN.read_text())
        self.assertTrue(app.MANAGED.exists())
        app.finish(transaction, False)
        self.assertEqual(app.MAIN.read_text(), '-- original\n')
        self.assertFalse(app.MANAGED.exists())
        self.assertFalse(app.SETTINGS.exists())
        self.assertEqual(app.update_env.call_args.args[0]['GDK_SCALE'], '2')
        self.assertFalse(app.finish(transaction, True))

    def test_keep_prevents_watchdog_revert(self):
        transaction = app.begin(self.settings, self.connected)
        self.assertTrue(app.finish(transaction, True))
        self.assertFalse(app.finish(transaction, False))
        self.assertTrue(app.MANAGED.exists())

    def test_config_error_rolls_back(self):
        app.reload_config.side_effect = [None, RuntimeError('invalid Lua'), None]
        with self.assertRaisesRegex(RuntimeError, 'invalid Lua'):
            app.begin(self.settings, self.connected)
        self.assertEqual(app.MAIN.read_text(), '-- original\n')
        self.assertFalse(app.MANAGED.exists())

    def test_watchdog_calls_rollback(self):
        transaction = app.begin(self.settings, self.connected)
        with patch.object(app.time, 'sleep'):
            app.watchdog(transaction)
        self.assertEqual((transaction / 'status').read_text(), 'reverted')

    def test_idle_saved_only_on_keep_preserving_other_settings(self):
        original = {'bar': {'position': 'left'}, 'idle': {'lock': 300, 'extra': True}, 'plugins': ['example']}
        app.SHELL.write_text(json.dumps(original))
        self.settings['idle'] = {'screensaver': 120, 'lock': 600}
        transaction = app.begin(self.settings, self.connected)
        self.assertEqual(json.loads(app.SHELL.read_text()), original)
        app.finish(transaction, True)
        result = json.loads(app.SHELL.read_text())
        self.assertEqual(result['bar'], original['bar'])
        self.assertEqual(result['plugins'], original['plugins'])
        self.assertEqual(result['idle'], {'screensaver': 120, 'lock': 600, 'extra': True})

    def test_idle_revert_leaves_existing_settings(self):
        original = '{"idle": {"lock": 300, "screensaver": 600}}\n'
        app.SHELL.write_text(original)
        self.settings['idle'] = {'screensaver': 0, 'lock': 0}
        transaction = app.begin(self.settings, self.connected)
        app.finish(transaction, False)
        self.assertEqual(app.SHELL.read_text(), original)

    def test_idle_missing_file_and_defaults(self):
        app.SHELL_DEFAULTS.write_text('{"idle": {"screensaver": 180, "lock": 420}}')
        self.assertEqual(app.idle_settings(), {'screensaver': 180, 'lock': 420})
        self.settings['idle'] = {'screensaver': 0, 'lock': 600}
        transaction = app.begin(self.settings, self.connected)
        self.assertFalse(app.SHELL.exists())
        app.finish(transaction, True)
        self.assertEqual(app.idle_settings(), self.settings['idle'])

    def test_invalid_shell_does_not_start_transaction(self):
        app.SHELL.write_text('{broken')
        self.settings['idle'] = {'screensaver': 0, 'lock': 600}
        with self.assertRaises(ValueError):
            app.begin(self.settings, self.connected)
        self.assertFalse(app.MANAGED.exists())


if __name__ == '__main__':
    unittest.main()
