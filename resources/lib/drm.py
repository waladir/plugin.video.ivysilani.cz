# -*- coding: utf-8 -*-
"""Read Widevine initialization metadata, without requesting or decrypting keys."""
import base64
import re
import struct
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen


DASH = {'d': 'urn:mpeg:dash:schema:mpd:2011'}
WIDEVINE_ID = bytes.fromhex('edef8ba979d64acea3c827dcd51d21ed')
WIDEVINE_SCHEME = 'urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed'
MANIFEST_LIMIT = 1024 * 1024
SEGMENT_PREFIX_LIMIT = 128 * 1024
NETWORK_TIMEOUT = 10


def is_drm_stream(url):
    """ČT marks the selected stream explicitly; do not guess from its title."""
    return any(value.lower() == 'true' for value in
               parse_qs(urlsplit(url).query).get('drmOnly', []))


def _download(url, user_agent, prefix=False, prefix_bytes=SEGMENT_PREFIX_LIMIT):
    headers = {'User-Agent': user_agent}
    limit = min(prefix_bytes, SEGMENT_PREFIX_LIMIT) if prefix else MANIFEST_LIMIT
    if prefix:
        headers['Range'] = 'bytes=0-{}'.format(limit - 1)
    with urlopen(Request(url, headers=headers), timeout=NETWORK_TIMEOUT) as response:
        # Bound reads even when a server ignores Range and sends the whole segment.
        data = response.read(limit if prefix else limit + 1)
        if not prefix and len(data) > limit:
            raise ValueError('DASH manifest exceeds size limit')
        return data, response.geturl(), response.headers.get('Date')


def find_widevine_pssh(data):
    """Find a complete Widevine PSSH in moov/moof; never scan encrypted mdat."""
    def boxes(start, end, depth=0):
        if depth > 8:
            return None
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack_from('>I4s', data, pos)
            header = 8
            if size == 1:
                if pos + 16 > end:
                    return None
                size = struct.unpack_from('>Q', data, pos + 8)[0]
                header = 16
            elif size == 0:
                size = end - pos
            if size < header:
                return None
            box_end = pos + size
            if kind in (b'moov', b'moof'):
                found = boxes(pos + header, min(box_end, end), depth + 1)
                if found:
                    return found
            elif (kind == b'pssh' and box_end <= end and size >= header + 24
                  and data[pos + header + 4:pos + header + 20] == WIDEVINE_ID):
                version = data[pos + header]
                offset = pos + header + 20
                if version == 1:
                    if offset + 4 > box_end:
                        return None
                    count = struct.unpack_from('>I', data, offset)[0]
                    offset += 4 + 16 * count
                elif version != 0:
                    return None
                if offset + 4 <= box_end:
                    payload_size = struct.unpack_from('>I', data, offset)[0]
                    if offset + 4 + payload_size == box_end:
                        return data[pos:box_end]
            if box_end > end:
                return None
            pos = box_end
        return None
    return boxes(0, len(data))


def _duration(value):
    match = re.fullmatch(r'P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?'
                         r'(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?', value)
    if not match:
        raise ValueError('Unsupported DASH duration')
    return sum(float(part or 0) * scale for part, scale in
               zip(match.groups(), (86400, 3600, 60, 1)))


def _timestamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def _media_name(template, representation, number, segment_time):
    values = {'RepresentationID': representation.get('id', ''),
              'Bandwidth': representation.get('bandwidth', '0'),
              'Number': number, 'Time': segment_time}
    # Protect the DASH escape $$ from substitution.
    template = template.replace('$$', '\x00')
    def replace(match):
        value = values[match.group(1)]
        return ('{:0{}d}'.format(int(value), int(match.group(2)))
                if match.group(2) else str(value))
    template = re.sub(r'\$(RepresentationID|Bandwidth|Number|Time)(?:%0(\d{1,2})d)?\$',
                      replace, template)
    if '$' in template:
        raise ValueError('Unsupported DASH segment placeholder')
    return template.replace('\x00', '$')


def _segment_urls(root, manifest_url, now):
    """Choose a small audio representation and a completed live segment."""
    candidates = []
    for period in root.findall('d:Period', DASH):
        for adaptation in period.findall('d:AdaptationSet', DASH):
            for representation in adaptation.findall('d:Representation', DASH):
                mime = representation.get('mimeType', adaptation.get('mimeType', ''))
                kind = representation.get('contentType', adaptation.get('contentType', ''))
                if not (mime.startswith(('audio/', 'video/')) or kind in ('audio', 'video')):
                    continue
                nodes = (root, period, adaptation, representation)
                protections = [cp for node in nodes for cp in node.findall('d:ContentProtection', DASH)]
                if not any(cp.get('schemeIdUri', '').lower() == WIDEVINE_SCHEME for cp in protections):
                    continue
                candidates.append((not (mime.startswith('audio/') or kind == 'audio'),
                                   int(representation.get('bandwidth', '0')), nodes))
    if not candidates:
        raise ValueError('No Widevine audio/video representation in live manifest')
    _, _, nodes = min(candidates, key=lambda candidate: candidate[:2])
    _, period, _, representation = nodes
    base = manifest_url
    attributes = {}
    for node in nodes:
        location = node.find('d:BaseURL', DASH)
        if location is not None and location.text:
            base = urljoin(base, location.text.strip())
        template = node.find('d:SegmentTemplate', DASH)
        if template is not None:
            if template.find('d:SegmentTimeline', DASH) is not None:
                raise ValueError('Live PSSH bootstrap requires a duration-based SegmentTemplate')
            attributes.update(template.attrib)
    timescale = int(attributes.get('timescale', '1'))
    duration = int(attributes.get('duration', '0'))
    if duration <= 0 or timescale <= 0 or not attributes.get('media'):
        raise ValueError('Invalid live DASH segment template')
    start_number = int(attributes.get('startNumber', '1'))
    elapsed = now - _timestamp(root.attrib['availabilityStartTime']) - _duration(period.get('start', 'PT0S'))
    # Leave three complete segments behind the live edge (CDN publication delay).
    offset = max(0, int(elapsed * timescale / duration) - 3)
    window = _duration(root.get('timeShiftBufferDepth', 'PT0S'))
    if window and window < 4 * duration / timescale:
        offset = max(0, int(elapsed * timescale / duration) - 1)
    for behind in (0, 1):
        index = max(0, offset - behind)
        media = _media_name(attributes['media'], representation, start_number + index,
                            int(attributes.get('presentationTimeOffset', '0')) + index * duration)
        yield urljoin(base, media)


def live_license_data(url, user_agent):
    """Bootstrap only live DRM streams whose manifest has no Widevine PSSH.

    The PSSH is passed to InputStream Adaptive; license requests and playback
    remain with Kodi/Widevine. No video, content keys or licenses are cached.
    """
    manifest, effective_url, server_date = _download(url, user_agent)
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError as exc:
        raise ValueError('Invalid DASH manifest') from exc
    if root.get('type') != 'dynamic':
        return None
    for protection in root.findall('.//d:ContentProtection', DASH):
        if protection.get('schemeIdUri', '').lower() == WIDEVINE_SCHEME:
            if any(child.tag.rsplit('}', 1)[-1] == 'pssh' and (child.text or '').strip()
                   for child in protection):
                return None  # Adaptive already supports initialization from the manifest.
    now = time.time()
    if server_date:
        try:
            now = parsedate_to_datetime(server_date).timestamp()
        except (ValueError, TypeError, OverflowError):
            pass
    last_error = None
    for segment_url in _segment_urls(root, effective_url, now):
        try:
            data, _, _ = _download(segment_url, user_agent, prefix=True)
            pssh = find_widevine_pssh(data)
            if pssh:
                return base64.b64encode(pssh).decode('ascii')
            last_error = ValueError('No complete Widevine PSSH in segment prefix')
        except OSError as exc:
            last_error = exc
    raise ValueError('Cannot bootstrap live Widevine initialization') from last_error
