# DRM playback in Kodi 21

The playlist API marks protected streams with the exact URL parameter
`drmOnly=true`. Clear HLS/DASH stays direct and does not start InputStream
Helper or the local adapter. Protected channels request Widevine DASH instead
of FairPlay HLS and prefer the separate three-hour `timeshift` URL (`main`
retains about 76 seconds). A VOD response containing `platformChannel` redirects
to the live channel, matching the website.

ČT uses an all-zero `tenc.default_KID` in the observed live and archive init
files; the actual KID is in fragment `sgpd/seig`. InputStream Adaptive 21.5.25
keeps the initialization KID when decoding. On macOS, the tested StarDance
archive acquired a license with four usable KIDs but then failed on the zero
KID, just as live TV originally did on SHIELD.

All protected DASH uses one `ManifestAdapter`, started per playback through
Kodi RunScript (`proxy_service.py`). It serves only the manifest and small
initialization files on a loopback URL with a random session path. Encrypted
media and Widevine license requests remain direct to ČT. It neither decrypts
media nor requests licenses or content keys. Helper checks prerequisites;
InputStream Adaptive and the platform CDM handle licensing and decoding.

- **Static archive:** preserves source periods, PSSH, media URLs and complete
  timelines, including variable segment durations. Only initialization URLs
  are overridden. On request, a valid init KID is returned unchanged; a zero
  KID is replaced with the KID from the first fragment of that particular
  track/period. Unused qualities cause no fragment requests. Different tracks
  and source periods can have different KIDs. A key change *within* one static
  source period is not supported. Samples from the tested StarDance archive
  (audio beginning/middle/end and two video qualities) used the same KID;
  this does not establish that the entire catalogue has no rotation.
- **Dynamic live/timeshift:** groups fragments into stable per-key periods,
  supplying PSSH, `cenc.default_KID` and patched initialization. Original
  segment numbers and timestamps are preserved. Live-only inspects at most
  32 reference fragments. Timeshift requires non-overlapping, constant-duration
  segments and locates key boundaries by bisecting public Widevine
  crypto-period indices (at most 512 probes). Other tracks are checked only
  when their init is requested, against a still-available fragment of the
  expected key period. Successful initializations are cached. Updates inspect
  only new reference fragments. The oldest edge has a 30-second safety margin.
  Tracks must be aligned with matching KIDs; one source period and at most
  12000 fragments are supported. Unsupported layouts fail explicitly.

Positive gaps in live SegmentTimeline runs are retained. A compact list of
run offsets/timestamps maps segment numbers to real sample times; each key
period emits the corresponding original S runs, including gaps. Period timing,
the live edge and the oldest available segment use these timestamps. On ČT2,
both observed gaps were inside crypto periods, so source S boundaries cannot
substitute for DRM key boundaries. The corrected MPD and current/historical
audio/video inits passed checks against actual ČT2 metadata. After deployment,
macOS logs confirmed ČT1/ČT2 startup without adapter or decryption failures.
A subsequent ČT1 session completed four cross-period seeks and license updates,
but logged an audio renderer timeout after the last seek, alongside video
buffer timeouts and decoder reopenings. Seeking across the actual ČT2 gaps
and playback of this gap fix on Android remain unverified.

Fragment metadata reads start at 8 KiB, with a bounded 128 KiB retry for larger
headers. Manifest/init reads are limited to 1 MiB. Media payloads are not scanned.
The service stops when Kodi aborts, or after 90 seconds without requests once
its manifest is no longer the playing file. This keeps static VOD init routes
available for a later seek or quality change without requiring MPD refreshes.

The live source template is pinned: identical ČT MPD requests can alternate
packagers with different availabilityStartTime, Period IDs and startNumber.
The local MPD advances using this template and a server-calibrated clock.
Old init URLs have a bounded history. Signed URLs are not renewed mid-playback.

SHIELD (Kodi 21.2, ISA 21.5.25, Widevine L1) played live and completed eight
seeks, including 75 minutes backwards across four KIDs with successful license
updates. Decoder buffer-release errors and an audio timeout recovered.
Before on-demand track checks, startup took about 15 seconds on SHIELD; a local
comparison measured 13.83 s / 302 requests versus 7.79 s / 170 requests after
that optimization. Its startup/seek still needs confirmation on the device.
The unified archive path was checked against actual StarDance MPD and all six
track initializations, with original timelines/PSSH preserved. On macOS (Kodi
21.3 arm64, ISA 21.5.25, Widevine 4.10.3112.0), the user confirmed playback;
the log showed successful licensing, no decryption failures and five successful
seeks, including near the end. Manifest preparation took 0.11 s; audio/video
started about 1.6 s after playback resolution began. Short video buffer timeouts
at seeks recovered. The source MPD still lacks default_KID, so ISA logs empty-KID
conversion errors during capability checks despite working decoding with the
patched init. A separate subtitle error remains: the addon saves the API's first
subtitle file (JSON for this asset) as `.sub`, which Kodi cannot parse.
The archive fix on Android remains unverified. Uninterrupted multi-hour playback,
automatic live key transitions and CDN timeline rollovers also remain unverified.

The complete historical key map is prepared before playback to preserve native
three-hour seeking. ISA 21.5.25 appends newly discovered periods instead of
inserting older periods in time order, so deferring the history until seek has
no verified safe implementation in this manifest/init adapter. Using the
live-only source would reduce history; its startup time has not been measured.

References:

- [Kodi 21 DRM properties](https://github.com/xbmc/inputstream.adaptive/wiki/Integration-DRM-%28old%29)
- [ISA 21.5.25 DASH template inheritance](https://github.com/xbmc/inputstream.adaptive/blob/21.5.25-Omega/src/parser/DASHTree.cpp)
- [Kodi 21.3 playing-file query used for service lifetime](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/legacy/Player.cpp)
- [Related upstream limitation](https://github.com/xbmc/inputstream.adaptive/issues/1602)

Offline checks: `python3 -m unittest discover -s tests -v`
