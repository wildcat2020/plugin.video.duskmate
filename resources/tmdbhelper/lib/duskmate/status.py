# Module: Duskmate status directory
# Upstream: jurialmunkey/plugin.video.themoviedb.helper
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html
import json
from urllib.parse import quote

from tmdbhelper.lib.duskmate.integration import get_status


class DuskmateStatusDirectory:
    def __init__(self, handle, paramstring, **kwargs):
        self.handle = handle

    def get_tmdb_id(self):
        return None

    def get_directory(self, items_only=False, build_items=True):
        payload = quote(json.dumps(get_status(), separators=(',', ':'), sort_keys=True), safe='')
        url = f'duskmate://status?payload={payload}'
        if items_only or not build_items:
            return [url]

        from xbmcgui import ListItem
        from xbmcplugin import addDirectoryItem, endOfDirectory
        list_item = ListItem(label='Duskmate integration status', path=url, offscreen=True)
        addDirectoryItem(self.handle, url, list_item, isFolder=False)
        endOfDirectory(self.handle, succeeded=True, cacheToDisc=False)
        return [url]
