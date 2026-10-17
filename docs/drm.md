# DRM playback in Kodi 21

The playlist API marks protected streams with the exact URL parameter
`drmOnly=true`. Clear live streams retain HLS and do not start InputStream
Helper or the local DRM adapter. Protected live streams request Widevine DASH;
ČT's protected HLS otherwise defaults to FairPlay. A VOD response containing
`platformChannel` is followed to the live channel, matching the website.

On the tested SHIELD (Kodi 21.2, InputStream Adaptive 21.5.25), Widevine L1
initialization and licensing succeeded, but decoding failed with an all-zero
KID. ČT's live MPD does not contain PSSH or default_KID, its initialization
contains an all-zero tenc.default_KID, and fragments carry the actual KID in
sgpd/seig. This ISA version keeps using the initialization KID when decoding.

For protected live playback only, the addon starts `proxy_service.py` through
Kodi RunScript. A loopback HTTP server adapts the MPD into periods grouped by
fragment KID. Each period supplies Widevine PSSH, cenc.default_KID and a small
initialization file whose tenc.default_KID matches that period. Segment numbers,
period starts and presentationTimeOffset preserve the original media timeline.
Encrypted media segments continue directly from ČT's CDN. Licenses go directly
to ČT's Widevine endpoint with the website's public token; the adapter does not
request licenses, decrypt content or obtain content keys. VOD retains its
existing direct playback path.

Metadata reads use at most 128 KiB per fragment, prefer audio, inspect at most
32 reference fragments and cache metadata across MPD refreshes. Initialization
and manifest downloads are limited to 1 MiB. The adapter requires aligned,
duration-based SegmentTemplates, one source period and matching track KIDs.
Unsupported layouts fail explicitly. Its loopback URL has a random session
path and exposes only the manifest and known initialization files. It stops
after 90 seconds without requests or when Kodi aborts.

InputStream Helper checks prerequisites; InputStream Adaptive and Android's
Widevine CDM handle licensing and decoding. Local checks cover metadata parsing,
initialization patching, period transitions and stream routing. Live ČT metadata
and initialization files were also checked locally. Actual playback and key
rotation must still be verified on the target device by the user.

The source timing template is pinned for the playback session: identical ČT
MPD requests can alternate packagers with different availabilityStartTime,
Period identifiers and startNumber. The local MPD advances using the pinned
duration-based template and server-calibrated clock. Old initialization URLs
remain available in a bounded history for readers using earlier MPD snapshots.

References:

- [Kodi DRM properties for Kodi 21](https://github.com/xbmc/inputstream.adaptive/wiki/Integration-DRM-%28old%29)
- [Related upstream limitation](https://github.com/xbmc/inputstream.adaptive/issues/1602)

Offline checks: `python3 -m unittest discover -s tests -v`
