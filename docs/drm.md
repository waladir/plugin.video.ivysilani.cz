# DRM playback in Kodi 21

The playlist API marks protected streams with the exact URL parameter
`drmOnly=true`. Clear live streams retain HLS and do not start InputStream
Helper or the local DRM adapter. Protected live streams request Widevine DASH;
ČT's protected HLS otherwise defaults to FairPlay. A VOD response containing
`platformChannel` is followed to the live channel, matching the website.
For protected channels the addon prefers the API's separate `timeshift` URL,
which retains three hours of media; `main` retains only about 76 seconds.

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

Metadata reads initially use 8 KiB per fragment, with a bounded 128 KiB retry
for larger video headers. The live-only path inspects at most 32 reference
fragments. Timeshift accepts contiguous, constant-duration SegmentTimeline
runs and locates key boundaries by bisecting public Widevine crypto-period
indices (at most 512 reference probes), instead of downloading the whole
history. Track KIDs are checked at each boundary. Subsequent updates inspect
only new fragments. Explicit per-period timelines retain original sample
timestamps and restrict seeks to available fragments. A 30-second safety margin
at the oldest edge prevents fragments expiring during startup or a seek.
Initialization and manifest downloads are limited to 1 MiB. The adapter requires
aligned tracks, one source period, matching KIDs and monotonic crypto-period
indices for timeshift. At most 12000 fragments fit the supported history window.
Unsupported layouts fail explicitly. Its loopback URL has a random session
path and exposes only the manifest and known initialization files. It stops
after 90 seconds without requests or when Kodi aborts.

InputStream Helper checks prerequisites; InputStream Adaptive and Android's
Widevine CDM handle licensing and decoding. Local checks cover metadata parsing,
initialization patching, period transitions and stream routing. Live ČT metadata
and initialization files were also checked locally. On the target SHIELD,
playback and eight seeks succeeded, including about 75 minutes backwards and
switching between four KIDs with successful license updates. There were no
HTTP or DRM failures in that session. Android decoder buffer-release errors,
temporary InstanceGuard contention and one audio renderer timeout occurred
during seeking, followed by successful playback recovery.

Startup currently takes about 15 seconds to discover the historical key
boundaries. Uninterrupted multi-hour playback, automatic key transitions at
the live edge and CDN timeline rollovers remain unverified. The pinned source
template and signed URLs are not renewed during a playback session.

The source timing template is pinned for the playback session: identical ČT
MPD requests can alternate packagers with different availabilityStartTime,
Period identifiers and startNumber. The local MPD advances using the pinned
duration-based template and server-calibrated clock. Old initialization URLs
remain available in a bounded history for readers using earlier MPD snapshots.

References:

- [Kodi DRM properties for Kodi 21](https://github.com/xbmc/inputstream.adaptive/wiki/Integration-DRM-%28old%29)
- [Related upstream limitation](https://github.com/xbmc/inputstream.adaptive/issues/1602)

Offline checks: `python3 -m unittest discover -s tests -v`
