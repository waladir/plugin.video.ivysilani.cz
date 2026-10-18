import json
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class ServiceLifetimeTests(unittest.TestCase):
    def test_idle_vod_survives_until_its_playback_stops_or_changes(self):
        for next_file in ('https://other.test/video', RuntimeError('not playing')):
            with self.subTest(next_file=next_file), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'session.json'
                config.write_text(json.dumps({'source': 'https://source.test/mpd', 'user_agent': 'UA'}))
                xbmc = types.ModuleType('xbmc')
                xbmc.LOGINFO, xbmc.LOGERROR = 1, 4
                xbmc.log = MagicMock()
                xbmc.Monitor = MagicMock()
                xbmc.Monitor.return_value.waitForAbort.side_effect = [False, False, True]
                xbmc.Player = MagicMock()
                xbmc.Player.return_value.getPlayingFile.side_effect = [
                    'http://127.0.0.1:1234/secret/manifest.mpd', next_file]
                server = MagicMock(local_url='http://127.0.0.1:1234/secret', last_request=0)
                with patch.dict(sys.modules, {'xbmc': xbmc}), \
                        patch('resources.lib.drm_proxy.ProxyServer', return_value=server), \
                        patch('time.monotonic', return_value=200):
                    script = Path(__file__).resolve().parents[1] / 'proxy_service.py'
                    runpy.run_path(str(script))['serve'](str(config))
                self.assertEqual(xbmc.Player.return_value.getPlayingFile.call_count, 2)
                server.shutdown.assert_called_once()
                server.server_close.assert_called_once()
                self.assertFalse(config.exists())
                self.assertNotIn('error', json.loads(Path(str(config) + '.ready').read_text()))


if __name__ == '__main__':
    unittest.main()
