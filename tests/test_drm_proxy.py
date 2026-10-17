import base64
import copy
import http.client
import struct
import threading
import unittest
from email.utils import formatdate
import xml.etree.ElementTree as ET
from unittest.mock import patch

from resources.lib import drm, drm_proxy
from test_drm import box, pssh


KID_A, KID_B, KID_C = bytes(range(16)), bytes(range(16, 32)), bytes(range(32, 48))


def media_segment(kid, number=1000):
    tfdt = box(b'tfdt', b'\x01\0\0\0' + struct.pack('>Q', (number - 1000) * 360000))
    sgpd = box(b'sgpd', b'\x01\0\0\0seig' + struct.pack('>II', 20, 1) + b'\0\0\x01\x08' + kid)
    return box(b'moof', box(b'traf', tfdt + sgpd) + pssh()) + box(b'mdat', b'encrypted-media-bytes')


def initialization(audio=False):
    tenc = box(b'tenc', b'\0\0\0\0\0\0\x01\x08' + bytes(16))
    sinf = box(b'sinf', box(b'schi', tenc))
    sample = box(b'enca' if audio else b'encv', bytes(28 if audio else 78) + sinf)
    track = box(b'trak', box(b'mdia', box(b'minf', box(b'stbl', box(b'stsd', bytes(4) + struct.pack('>I', 1) + sample)))))
    return box(b'ftyp', b'isom') + box(b'moov', track)


MANIFEST = b'''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic"
 availabilityStartTime="2026-10-17T00:00:00Z" timeShiftBufferDepth="PT16S">
 <BaseURL>https://cdn.test/live/</BaseURL><Location>https://cdn.test/live/mpd</Location>
 <Period id="3" start="PT0S">
 <AdaptationSet id="video" mimeType="video/mp4">
 <ContentProtection schemeIdUri="urn:mpeg:dash:mp4protection:2011" value="cenc"/>
 <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
 <SegmentTemplate duration="360000" timescale="90000" startNumber="1000"
  media="$RepresentationID$/$Number$.m4s" initialization="$RepresentationID$/IS.mp4"/>
 <Representation id="v" bandwidth="1000000"/></AdaptationSet>
 <AdaptationSet id="audio" mimeType="audio/mp4">
 <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/>
 <SegmentTemplate duration="360000" timescale="90000" startNumber="1000"
  media="$RepresentationID$/$Number$.m4s" initialization="$RepresentationID$/IS.mp4"/>
 <Representation id="a" bandwidth="128000"/></AdaptationSet>
 </Period></MPD>'''


class MetadataTests(unittest.TestCase):
    def test_reads_per_fragment_kid_and_time(self):
        data = media_segment(KID_A, 1003)
        info = drm_proxy.segment_metadata(data)
        self.assertEqual(info['kid'], KID_A)
        self.assertEqual(info['time'], 1080000)
        self.assertEqual(info['pssh'], pssh())

    def test_rejects_missing_and_zero_kid(self):
        for data in (box(b'mdat', media_segment(KID_A)), media_segment(bytes(16))):
            with self.assertRaises(ValueError):
                drm_proxy.segment_metadata(data)

    def test_init_patch_changes_only_the_sixteen_identifier_bytes(self):
        for audio in (False, True):
            data = initialization(audio)
            changed = drm_proxy.patch_init_kid(data, KID_B)
            offset = data.index(b'tenc') + 12
            self.assertEqual(changed[:offset], data[:offset])
            self.assertEqual(changed[offset:offset + 16], KID_B)
            self.assertEqual(changed[offset + 16:], data[offset + 16:])
            self.assertEqual(len(changed), len(data))

    def test_init_patch_rejects_unencrypted_or_invalid_files(self):
        for data in (box(b'ftyp', b'isom'), initialization()[:-20]):
            with self.assertRaises(ValueError):
                drm_proxy.patch_init_kid(data, KID_B)
        with self.assertRaises(ValueError):
            drm_proxy.patch_init_kid(initialization(), bytes(16))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.seconds = 26
        self.downloads = []
        self.state = drm_proxy.LiveManifest('https://source.test/mpd', 'test-UA',
                                            'http://127.0.0.1:1234/secret')
        self.patcher = patch.object(drm, '_download', side_effect=self.download)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        clock = patch.object(drm_proxy.time, 'time', side_effect=lambda: 1792195200 + self.seconds)
        clock.start()
        self.addCleanup(clock.stop)

    def download(self, url, agent, prefix=False, prefix_bytes=None):
        self.downloads.append((url, prefix))
        date = 'Sat, 17 Oct 2026 00:00:{:02d} GMT'.format(self.seconds)
        if url == 'https://source.test/mpd':
            return MANIFEST, url, date
        if url.endswith('IS.mp4'):
            return initialization('/a/' in url), url, date
        number = int(url.rsplit('/', 1)[-1].split('.')[0])
        kid = KID_A if number < 1004 else KID_B if number < 1007 else KID_C
        return media_segment(kid, number), url, date

    def manifest(self):
        self.state.refreshed_at = 0
        return ET.fromstring(self.state.manifest())

    def test_splits_keys_into_periods_with_correct_timing_and_direct_media(self):
        root = self.manifest()
        periods = root.findall('d:Period', drm.DASH)
        self.assertEqual(len(periods), 2)
        self.assertEqual([p.get('id') for p in periods], ['3-1002', '3-1004'])
        self.assertEqual([p.get('start') for p in periods], ['PT8.000000S', 'PT16.000000S'])
        self.assertEqual([p.get('duration') for p in periods], ['PT8.000000S', 'PT8.000000S'])
        self.assertEqual(root.find('d:Location', drm.DASH).text, self.state.local_url + '/manifest.mpd')
        for period, expected in zip(periods, (KID_A, KID_B)):
            for template in period.findall('.//d:Representation/d:SegmentTemplate', drm.DASH):
                self.assertTrue(template.get('media').startswith('https://cdn.test/live/'))
                self.assertTrue(template.get('initialization').startswith(self.state.local_url + '/init/'))
                self.assertEqual(template.get('presentationTimeOffset'),
                                 str((int(template.get('startNumber')) - 1000) * 360000))
            for cp in period.findall('.//d:ContentProtection', drm.DASH):
                kid = cp.get('{{{}}}default_KID'.format(drm_proxy.CENC))
                if kid:
                    self.assertEqual(kid.replace('-', ''), expected.hex())
            encoded = period.find('.//{urn:mpeg:cenc:2013}pssh').text
            self.assertEqual(base64.b64decode(encoded), pssh())

    def test_rotation_has_stable_existing_period_and_new_initialization(self):
        root = self.manifest()
        old = root.findall('d:Period', drm.DASH)[1]
        old_url = old.find('.//d:Representation/d:SegmentTemplate', drm.DASH).get('initialization')
        expired_url = root.findall('d:Period', drm.DASH)[0].find('.//d:Representation/d:SegmentTemplate', drm.DASH).get('initialization')
        self.seconds = 34
        root = self.manifest()
        periods = root.findall('d:Period', drm.DASH)
        self.assertEqual([p.get('id') for p in periods], ['3-1004', '3-1007'])
        self.assertEqual(periods[0].find('.//d:Representation/d:SegmentTemplate', drm.DASH).get('initialization'), old_url)
        self.assertNotEqual(periods[1].find('.//d:Representation/d:SegmentTemplate', drm.DASH).get('initialization'), old_url)
        self.assertEqual(sum(url == 'https://source.test/mpd' for url, _ in self.downloads), 1)
        self.assertIn(KID_A, self.state.initialization(expired_url.rsplit('/', 1)[-1][:-4]))
        for period, kid in zip(periods, (KID_B, KID_C)):
            template = period.find('.//d:Representation/d:SegmentTemplate', drm.DASH)
            init_id = template.get('initialization').rsplit('/', 1)[-1][:-4]
            data = self.state.initialization(init_id)
            self.assertIn(kid, data)
            self.assertNotIn(bytes(16), data[data.index(b'tenc') + 12:data.index(b'tenc') + 28])

    def test_short_repeat_uses_cached_manifest_without_network(self):
        document = self.state.manifest()
        calls = len(self.downloads)
        self.assertEqual(self.state.manifest(), document)
        self.assertEqual(len(self.downloads), calls)

    def test_unmapped_initialization_cannot_fetch_arbitrary_url(self):
        with self.assertRaises(KeyError):
            self.state.initialization('https://example.test/private')
        self.assertEqual(self.downloads, [])

    def test_rejects_divergent_video_key(self):
        original = self.state._metadata
        def metadata(spec, number):
            info = dict(original(spec, number))
            if spec['id'] == 'v':
                info['kid'] = KID_C
            return info
        self.state._metadata = metadata
        with self.assertRaisesRegex(ValueError, 'Independent track key rotation'):
            self.state.manifest()


class TimeshiftTests(unittest.TestCase):
    def setUp(self):
        self.seconds = 11702
        self.downloads = []
        self.state = drm_proxy.LiveManifest('https://source.test/mpd', 'test-UA', 'http://127.0.0.1:1234/secret')
        self.root = ET.fromstring(MANIFEST)
        self.root.set('timeShiftBufferDepth', 'PT10800S')
        for template in self.root.findall('.//d:SegmentTemplate', drm.DASH):
            template.attrib.pop('duration')
            template.set('timescale', '1000')
            template.set('startNumber', '100000')
            timeline = ET.SubElement(template, drm_proxy._tag('SegmentTimeline'))
            ET.SubElement(timeline, drm_proxy._tag('S'), {'t': '900000', 'd': '2000', 'r': '5399'})
        for patcher in (patch.object(drm, '_download', side_effect=self.download),
                        patch.object(drm_proxy.time, 'time', side_effect=lambda: 1792195200 + self.seconds)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def download(self, url, agent, prefix=False, prefix_bytes=None):
        self.downloads.append(url)
        date = formatdate(1792195200 + self.seconds, usegmt=True)
        if url == 'https://source.test/mpd':
            return ET.tostring(self.root), url, date
        if url.endswith('IS.mp4'):
            return initialization('/a/' in url), url, date
        number = int(url.rsplit('/', 1)[-1].split('.')[0])
        index = (number - 100000) // 300
        kid = bytes([index + 1]) * 16
        payload = b'\x22\x06public\x38' + bytes([index])
        init_data = box(b'pssh', bytes(4) + drm.WIDEVINE_ID + struct.pack('>I', len(payload)) + payload)
        tfdt = box(b'tfdt', b'\x01\0\0\0' + struct.pack('>Q', 900000 + (number - 100000) * 2000))
        seig = box(b'sgpd', b'\x01\0\0\0seig' + struct.pack('>II', 20, 1) + b'\0\0\x01\x08' + kid)
        return box(b'moof', box(b'traf', tfdt + seig) + init_data), url, date

    def manifest(self):
        self.state.refreshed_at = 0
        return ET.fromstring(self.state.manifest())

    def test_hour_back_seek_maps_to_historical_kid_and_original_sample_time(self):
        root = self.manifest()
        target = self.seconds - 3600
        period = next(p for p in root.findall('d:Period', drm.DASH)
                      if drm._duration(p.get('start')) <= target < drm._duration(p.get('start')) + drm._duration(p.get('duration')))
        template = period.find('.//d:Representation/d:SegmentTemplate', drm.DASH)
        entry = template.find('d:SegmentTimeline/d:S', drm.DASH)
        self.assertEqual(int(entry.get('t')) / 1000, drm._duration(period.get('start')))
        self.assertEqual(template.get('presentationTimeOffset'), entry.get('t'))
        init_id = template.get('initialization').rsplit('/', 1)[-1][:-4]
        self.assertIn(bytes([13]) * 16, self.state.initialization(init_id))
        # Three hours contain 5400 fragments, but only boundaries are probed.
        self.assertLess(len(self.downloads), 400)
        self.assertEqual(len(root.findall('d:Period', drm.DASH)), 19)

    def test_refresh_extends_timeline_without_changing_period_ids_or_refetching_history(self):
        before = self.manifest().findall('d:Period', drm.DASH)
        previous_id = before[-1].get('id')
        previous_calls = len(self.downloads)
        self.seconds += 4
        after = self.manifest().findall('d:Period', drm.DASH)
        self.assertEqual(after[-1].get('id'), previous_id)
        self.assertEqual(drm._duration(after[-1].get('duration')) - drm._duration(before[-1].get('duration')), 4)
        self.assertLessEqual(len(self.downloads) - previous_calls, 2)
        self.assertEqual(sum(url == 'https://source.test/mpd' for url in self.downloads), 1)
        # Sliding oldest entry stays within the CDN history while its period ID/PTO remain stable.
        old_template = before[0].find('.//d:Representation/d:SegmentTemplate', drm.DASH)
        new_template = after[0].find('.//d:Representation/d:SegmentTemplate', drm.DASH)
        self.assertEqual(before[0].get('id'), after[0].get('id'))
        self.assertEqual(old_template.get('presentationTimeOffset'), new_template.get('presentationTimeOffset'))
        self.assertEqual(int(new_template.get('startNumber')) - int(old_template.get('startNumber')), 2)


class HttpTests(unittest.TestCase):
    def test_only_tokenized_manifest_and_known_init_are_served(self):
        server = drm_proxy.ProxyServer('https://unused.test/mpd', 'test-UA', 'secret')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server.manifest_state, 'manifest', return_value=b'<MPD/>'):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                connection.request('GET', '/wrong/manifest.mpd')
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
                connection.close()
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                connection.request('GET', '/secret/manifest.mpd')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('Content-Type'), 'application/dash+xml')
                self.assertEqual(response.read(), b'<MPD/>')
                connection.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == '__main__':
    unittest.main()
