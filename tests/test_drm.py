import struct
import unittest
from unittest.mock import MagicMock, patch

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


class TimingTests(unittest.TestCase):
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
