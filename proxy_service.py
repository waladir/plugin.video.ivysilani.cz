"""Per-playback loopback manifest/init adapter, started on demand by the addon."""
import json
import os
import sys
import threading
import time

import xbmc

from resources.lib.drm_proxy import ProxyServer


def serve(config_path):
    ready = config_path + '.ready'
    server = None
    thread = None
    try:
        with open(config_path) as source:
            config = json.load(source)
        server = ProxyServer(config['source'], config['user_agent'])
        server.manifest_state.manifest()
        temporary = ready + '.tmp'
        with open(temporary, 'w') as output:
            json.dump({'url': server.local_url + '/manifest.mpd'}, output)
        os.replace(temporary, ready)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        xbmc.log('[iVysilani] Live DRM proxy: per-key periods and initialization KIDs ready', xbmc.LOGINFO)
        monitor = xbmc.Monitor()
        while not monitor.waitForAbort(1):
            if time.monotonic() - server.last_request > 90:
                break
    except Exception as exc:
        xbmc.log('[iVysilani] Live DRM proxy failed ({})'.format(type(exc).__name__), xbmc.LOGERROR)
        if not os.path.exists(ready):
            temporary = ready + '.tmp'
            with open(temporary, 'w') as output:
                json.dump({'error': type(exc).__name__}, output)
            os.replace(temporary, ready)
    finally:
        if server:
            if thread:
                server.shutdown()
                thread.join(timeout=2)
            server.server_close()
        if os.path.exists(config_path):
            os.remove(config_path)


if __name__ == '__main__':
    serve(sys.argv[1])
