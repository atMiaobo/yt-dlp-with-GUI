#!/usr/bin/env python3

# Allow direct execution
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from yt_dlp import webui


class TestWebUI(unittest.TestCase):
    def _payload(self, **kwargs):
        payload = {
            'urls': 'https://example.com/video',
            'options': {},
        }
        payload.update(kwargs)
        return payload

    def test_build_command_with_download_path(self):
        command = webui._build_command(self._payload(download_path='  ./DOWNLOADS  '))
        self.assertEqual(command, [sys.executable, '-m', 'yt_dlp', '-P', './DOWNLOADS', 'https://example.com/video'])

    def test_build_command_rejects_non_string_download_path(self):
        with self.assertRaisesRegex(ValueError, 'Download location must be a string'):
            webui._build_command(self._payload(download_path=['./DOWNLOADS']))

    def test_build_command_rejects_conflicting_paths(self):
        paths_option = next(option for option in webui.SCHEMA_OPTIONS if option['primary_flag'] == '--paths')
        with self.assertRaisesRegex(ValueError, 'Download location conflicts with --paths option'):
            webui._build_command(self._payload(
                download_path='./DOWNLOADS',
                options={
                    paths_option['id']: {
                        'select': 'custom',
                        'custom': 'home:./OTHER',
                    },
                },
            ))


if __name__ == '__main__':
    unittest.main()
