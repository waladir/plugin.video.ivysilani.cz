import base64
import io
import struct
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

from resources.lib import drm


def box(kind, payload, extended=False):
    if extended:
        return struct.pack('>I4sQ', 1, kind, 16 + len(payload)) + payload
    return struct.pack('>I4s', 8 + len(payload), kind) + payload


def pssh(system_id=drm.WIDEVINE_ID, version=0):
    payload = b'public initialization metadata'
    kids = struct.pack('>I', 1) + bytes(range(16)) if version == 1 else b''
    return box(b'pssh', bytes([version, 0, 0, 0]) + system_id + kids +
               struct.pack('>I', len(payload)) + payload)


PSSH = pssh()
SEGMENT = box(b'styp', b'isom') + box(b'moof', box(b'traf', b'') +
                                      pssh(bytes(range(16))) + PSSH) + box(b'mdat', b'video')
MPD = b'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic"
 availabilityStartTime="2026-10-17T00:00:00Z" timeShiftBufferDepth="PT76.16S">
 <BaseURL>https://cdn.example.test/live/</BaseURL><Period start="PT3.538S">
 <AdaptationSet mimeType="video/mp4">
  <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
  <SegmentTemplate duration="345600" timescale="90000" startNumber="1000"
   media="$RepresentationID$/$Number%06d$.m4s"/>
  <Representation id="video" bandwidth="6000000"/>
 </AdaptationSet>
 <AdaptationSet mimeType="audio/mp4">
  <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
  <SegmentTemplate duration="345600" timescale="90000" startNumber="1000"
   media="$RepresentationID$/$Number%06d$.m4s"/>
  <Representation id="audio" bandwidth="128000"/>
 </AdaptationSet></Period></MPD>'''
DATE = 'Sat, 17 Oct 2026 00:10:00 GMT'


class DetectionTests(unittest.TestCase):
    def test_uses_explicit_query_parameter(self):
        for url, expected in (
            ('https://x/?drmOnly=true&token=abc', True),
            ('https://x/?drmOnly=%74rue', True),
            ('https://x/?drmOnly=false', False),
            ('https://x/drmOnly=true?note=drmOnly%3Dtrue', False),
            ('https://x/?notdrmOnly=true', False),
        ):
            with self.subTest(url=url):
                self.assertEqual(drm.is_drm_stream(url), expected)


class PsshTests(unittest.TestCase):
    def test_reads_widevine_from_moof_and_ignores_other_systems(self):
        self.assertEqual(drm.find_widevine_pssh(SEGMENT), PSSH)

    def test_handles_extended_box_and_version_one(self):
        value = pssh(version=1)
        self.assertEqual(drm.find_widevine_pssh(box(b'moof', value, extended=True)), value)

    def test_does_not_search_media_payload_for_fake_pssh(self):
        self.assertIsNone(drm.find_widevine_pssh(box(b'mdat', PSSH)))

    def test_handles_truncated_invalid_and_empty_boxes(self):
        for value in (b'', PSSH[:-1], box(b'moof', PSSH)[:-1],
                      struct.pack('>I4s', 4, b'moof'), b'\0\0\0\1moof'):
            with self.subTest(value=value):
                self.assertIsNone(drm.find_widevine_pssh(value))

    def test_rejects_invalid_pssh_payload_size(self):
        value = bytearray(PSSH)
        struct.pack_into('>I', value, 28, 100000)
        self.assertIsNone(drm.find_widevine_pssh(bytes(value)))

    def test_can_read_metadata_before_truncated_mdat(self):
        self.assertEqual(drm.find_widevine_pssh(SEGMENT[:-3]), PSSH)


class LiveBootstrapTests(unittest.TestCase):
    @patch.object(drm, '_download')
    def test_selects_small_audio_and_complete_segment_using_server_time(self, download):
        download.side_effect = [(MPD, 'https://redirect.test/mpd', DATE),
                                (SEGMENT, 'https://cdn.example.test/segment', None)]
        self.assertEqual(drm.live_license_data('https://api.test/manifest', 'test-UA'),
                         base64.b64encode(PSSH).decode('ascii'))
        self.assertEqual(download.call_count, 2)
        download.assert_called_with('https://cdn.example.test/live/audio/001152.m4s',
                                    'test-UA', prefix=True)

    @patch.object(drm, '_download')
    def test_manifest_pssh_avoids_segment_request(self, download):
        data = MPD.replace(b'<ContentProtection schemeIdUri=',
                           b'<ContentProtection xmlns:cenc="urn:mpeg:cenc:2013" schemeIdUri=', 1)
        data = data.replace(b'51d21ed"/>', b'51d21ed"><cenc:pssh>' +
                            base64.b64encode(PSSH) + b'</cenc:pssh></ContentProtection>', 1)
        download.return_value = (data, 'https://cdn.test/mpd', DATE)
        self.assertIsNone(drm.live_license_data('https://api.test/manifest', 'test-UA'))
        download.assert_called_once()

    @patch.object(drm, '_download')
    def test_retries_adjacent_segment_after_download_error(self, download):
        download.side_effect = [(MPD, 'https://cdn.test/mpd', DATE),
                                OSError('not yet published'), (SEGMENT, 'https://cdn.test/a', None)]
        self.assertTrue(drm.live_license_data('https://api.test/mpd', 'test-UA'))
        self.assertIn('/001151.m4s', download.call_args.args[0])

    @patch.object(drm, '_download')
    def test_reports_missing_pssh(self, download):
        download.side_effect = [(MPD, 'https://cdn.test/mpd', DATE),
                                (box(b'mdat', b'video'), '', None),
                                (box(b'mdat', b'video'), '', None)]
        with self.assertRaisesRegex(ValueError, 'Cannot bootstrap'):
            drm.live_license_data('https://api.test/mpd', 'test-UA')

    @patch.object(drm, '_download')
    def test_malformed_manifest_fails_cleanly(self, download):
        download.return_value = (b'<bad', '', None)
        with self.assertRaisesRegex(ValueError, 'Invalid DASH manifest'):
            drm.live_license_data('https://api.test/mpd', 'test-UA')

    def test_inherits_templates_and_resolves_relative_base_urls(self):
        data = MPD.replace(b'https://cdn.example.test/live/', b'../live/')
        root = ET.fromstring(data)
        urls = list(drm._segment_urls(root, 'https://cdn.test/manifest/mpd',
                                     drm._timestamp('2026-10-17T00:10:00Z')))
        self.assertEqual(urls[0], 'https://cdn.test/live/audio/001152.m4s')

    def test_timestamp_offsets_and_duration(self):
        self.assertEqual(drm._timestamp('2026-10-17T02:00:00+02:00'),
                         drm._timestamp('2026-10-17T00:00:00Z'))
        self.assertEqual(drm._duration('P1DT1H2M3.5S'), 90123.5)


class DownloadTests(unittest.TestCase):
    @patch.object(drm, 'urlopen')
    def test_range_is_bounded_even_if_server_ignores_it(self, urlopen):
        response = MagicMock()
        response.read.return_value = b'prefix'
        response.geturl.return_value = 'https://cdn.test/a'
        response.headers = {}
        urlopen.return_value.__enter__.return_value = response
        drm._download('https://cdn.test/a', 'test-UA', prefix=True)
        response.read.assert_called_once_with(drm.SEGMENT_PREFIX_LIMIT)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header('Range'), 'bytes=0-131071')
        self.assertEqual(request.get_header('User-agent'), 'test-UA')
        urlopen.return_value.__exit__.assert_called_once()

    @patch.object(drm, 'urlopen')
    def test_rejects_oversized_manifest(self, urlopen):
        response = MagicMock()
        response.read.return_value = b'x' * (drm.MANIFEST_LIMIT + 1)
        urlopen.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(ValueError, 'size limit'):
            drm._download('https://cdn.test/mpd', 'test-UA')
        urlopen.return_value.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
