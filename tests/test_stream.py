import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        names = ('xbmc', 'xbmcgui', 'xbmcplugin', 'xbmcaddon', 'xbmcvfs',
                 'inputstreamhelper', 'resources.lib.api', 'resources.lib.items',
                 'resources.lib.utils')
        self.modules = {name: types.ModuleType(name) for name in names}
        attributes = {
            'xbmc': {'log': MagicMock(), 'LOGINFO': 1, 'LOGERROR': 4},
            'xbmcgui': {'ListItem': MagicMock(), 'Dialog': MagicMock(), 'NOTIFICATION_ERROR': 'error'},
            'xbmcplugin': {'setResolvedUrl': MagicMock()},
            'xbmcvfs': {'translatePath': lambda path: path},
            'inputstreamhelper': {'Helper': MagicMock()},
            'resources.lib.api': {'call_api': MagicMock(), 'call_graphql': MagicMock()},
            'resources.lib.items': {'get_item_data': MagicMock()},
            'resources.lib.utils': {'PY2': False, 'iso639map': {}, 'ua': 'test-UA'},
        }
        for name, attrs in attributes.items():
            self.modules[name].__dict__.update(attrs)
        self.modules_patch = patch.dict(sys.modules, self.modules)
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        with patch.object(sys, 'argv', ['plugin://test/', '42']):
            path = Path(__file__).resolve().parents[1] / 'resources/lib/stream.py'
            self.stream = runpy.run_path(str(path))
        self.api = self.modules['resources.lib.api'].call_api
        self.helper = self.modules['inputstreamhelper'].Helper
        self.helper.return_value.check_inputstream.return_value = True
        self.item = self.modules['xbmcgui'].ListItem.return_value
        self.resolved = self.modules['xbmcplugin'].setResolvedUrl
        self.bootstrap = MagicMock(return_value='http://127.0.0.1:1234/session/manifest.mpd')
        self.stream['play_url'].__globals__['start_live_proxy'] = self.bootstrap

    def properties(self):
        return dict(call.args for call in self.item.setProperty.call_args_list)

    def test_clear_live_keeps_hls_without_helper_or_pssh_requests(self):
        self.api.return_value = {'streamUrls': {'main': 'https://cdn.test/?drmOnly=false'}}
        self.stream['play_channel']('CH_24')
        self.api.assert_called_once()
        self.assertIn('canPlayDrm=true', self.api.call_args.kwargs['url'])
        self.assertEqual(self.properties()['inputstream.adaptive.manifest_type'], 'hls')
        self.assertNotIn('inputstream.adaptive.license_type', self.properties())
        self.helper.assert_not_called()
        self.bootstrap.assert_not_called()
        self.resolved.assert_called_once_with(42, True, self.item)

    def test_protected_live_switches_fairplay_hls_to_widevine_dash(self):
        hls = 'https://cdn.test/?drmOnly=true&encryption=fp'
        dash = 'https://cdn.test/?drmOnly=true&encryption=wv'
        self.api.side_effect = [{'streamUrls': {'main': hls}}, {'streamUrls': {'main': dash}}]
        self.stream['play_channel']('CH_1')
        self.assertEqual(self.api.call_count, 2)
        self.assertIn('streamType=dash&encryption=wv', self.api.call_args.kwargs['url'])
        self.bootstrap.assert_called_once_with(dash, 'test-UA')
        self.item.setPath.assert_called_once_with('http://127.0.0.1:1234/session/manifest.mpd')
        self.assertNotIn('inputstream.adaptive.license_data', self.properties())
        self.assertEqual(self.properties()['inputstream.adaptive.license_type'], 'com.widevine.alpha')
        self.helper.assert_called_once_with('mpd', drm='com.widevine.alpha')

    def test_vod_drm_does_not_bootstrap_live_pssh(self):
        self.stream['play_url']('https://cdn.test/?drmOnly=true')
        self.helper.assert_called_once()
        self.bootstrap.assert_not_called()
        self.assertNotIn('inputstream.adaptive.license_data', self.properties())

    def test_live_proxy_keeps_per_period_manifest_pssh_without_global_override(self):
        self.stream['play_url']('https://cdn.test/?drmOnly=true', live=True)
        self.assertNotIn('inputstream.adaptive.license_data', self.properties())
        self.resolved.assert_called_once_with(42, True, self.item)

    def test_missing_helper_support_stops_before_network_bootstrap(self):
        self.helper.return_value.check_inputstream.return_value = False
        self.stream['play_url']('https://cdn.test/?drmOnly=true', live=True)
        self.bootstrap.assert_not_called()
        self.resolved.assert_called_once_with(42, False, self.item)

    def test_failed_bootstrap_reports_failure_without_signed_url_in_logs(self):
        self.bootstrap.side_effect = OSError('https://cdn.test/?token=secret')
        self.stream['play_url']('https://cdn.test/?drmOnly=true', live=True)
        self.resolved.assert_called_once_with(42, False, self.item)
        logs = str(self.modules['xbmc'].log.call_args_list)
        self.assertNotIn('token=secret', logs)
        self.modules['xbmcgui'].Dialog.return_value.notification.assert_called_once()

    def test_episode_currently_on_air_uses_live_channel(self):
        self.api.side_effect = [{'platformChannel': 'CH_1'},
                                {'streamUrls': {'main': 'https://cdn.test/?drmOnly=false'}}]
        self.stream['play_idec']('226544160450002')
        self.assertIn('/channel/CH_1?', self.api.call_args.kwargs['url'])
        self.resolved.assert_called_once_with(42, True, self.item)

    def test_missing_live_stream_reports_api_error(self):
        self.api.return_value = []
        self.stream['play_channel']('CH_1')
        self.resolved.assert_not_called()
        self.modules['xbmcgui'].Dialog.return_value.notification.assert_called_once()


if __name__ == '__main__':
    unittest.main()
