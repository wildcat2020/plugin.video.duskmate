# Module: Duskmate integration
# Upstream: jurialmunkey/plugin.video.themoviedb.helper
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html
import json
import time
from base64 import urlsafe_b64decode
from binascii import Error as Base64Error
from hashlib import sha256

from tmdbhelper.lib.addon.plugin import ADDONDATA, get_version
from tmdbhelper.lib.script.method.decorators import get_tmdb_id, is_in_kwargs
from tmdbhelper.lib.update.common import LibraryCommon


UPSTREAM_VERSION = '6.21.5'
DUSKMATE_BUILD = '2'
STATUS_FILE = f'{ADDONDATA}duskmate-status.json'
TRAKT_SYNC_ADDON = 'script.module.duskmate.sync.trakt'


def _library_paths():
    return {
        'movies': LibraryCommon.get_basedir('movies'),
        'tvshows': LibraryCommon.get_basedir('tvshows'),
    }


def _paths_ready(paths, create=False):
    from xbmcvfs import exists, mkdirs, translatePath
    if create and not exists(ADDONDATA):
        mkdirs(ADDONDATA)
    ready = True
    for path in paths.values():
        # Some Kodi platforms do not report an empty special:// directory through exists()
        # until its translated filesystem path has been touched. Check both forms so setup is
        # deterministic on Android as well as desktop Kodi.
        translated = translatePath(path)
        path_exists = exists(path) or exists(translated)
        if create and not path_exists:
            mkdirs(path)
            path_exists = exists(path) or exists(translated)
        ready = path_exists and ready
    return ready


def _read_state():
    from xbmcvfs import File, exists
    if not exists(STATUS_FILE):
        return {}
    status_file = File(STATUS_FILE)
    try:
        return json.loads(status_file.read() or '{}')
    except (TypeError, ValueError):
        return {}
    finally:
        status_file.close()


def _write_state(state):
    from xbmcvfs import File, mkdirs
    mkdirs(ADDONDATA)
    status_file = File(STATUS_FILE, 'w')
    try:
        status_file.write(json.dumps(state, separators=(',', ':'), sort_keys=True))
    finally:
        status_file.close()


def _trakt_sync_status():
    try:
        from xbmc import getCondVisibility
        from xbmcaddon import Addon

        addon = Addon(TRAKT_SYNC_ADDON)
        authorization = json.loads(addon.getSetting('token') or '{}')
        expires_at = int(authorization.get('created_at') or 0) + int(authorization.get('expires_in') or 0)
    except (RuntimeError, TypeError, ValueError):
        return {
            'installed': False,
            'enabled': False,
            'authorized': False,
            'managed': False,
            'expires_at': 0,
            'token_fingerprint': '',
            'version': '',
        }

    access_token = authorization.get('access_token') or ''
    return {
        'installed': True,
        'enabled': bool(getCondVisibility(f'System.HasAddon({TRAKT_SYNC_ADDON})')),
        'authorized': bool(access_token and expires_at > int(time.time()) + 30),
        'managed': authorization.get('managed_by') == 'duskmate',
        'expires_at': expires_at,
        'token_fingerprint': sha256(access_token.encode('utf-8')).hexdigest()[:16] if access_token else '',
        'version': addon.getAddonInfo('version'),
    }


def get_status():
    paths = _library_paths()
    state = _read_state()
    return {
        'schema': 2,
        'addon_version': get_version(),
        'upstream_version': UPSTREAM_VERSION,
        'duskmate_build': DUSKMATE_BUILD,
        'library_ready': _paths_ready(paths),
        'library_paths': paths,
        'last_add': state.get('last_add'),
        'trakt_sync': _trakt_sync_status(),
    }


def setup(**kwargs):
    paths = _library_paths()
    ready = _paths_ready(paths, create=True)
    state = _read_state()
    state['setup'] = {'library_ready': ready, 'library_paths': paths}
    _write_state(state)
    return ready


def provision_trakt(authorization=None, **kwargs):
    if not authorization:
        raise ValueError('A Trakt authorization payload is required')
    try:
        payload = json.loads(urlsafe_b64decode(f'{authorization}===').decode('utf-8'))
    except (Base64Error, TypeError, UnicodeDecodeError, ValueError) as error:
        raise ValueError('The Trakt authorization payload is invalid') from error

    from tmdbhelper_sync_trakt.provisioning import provision
    return provision(
        access_token=payload.get('access_token'),
        expires_at=payload.get('expires_at'),
    )


class _ResultCollector:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.duskmate_items = []

    def set_library_item(self, library_item):
        result = super().set_library_item(library_item)
        self.duskmate_items.append({
            'tmdb_id': library_item.tmdb_id,
            'season': library_item.season,
            'episode': library_item.episode,
            'status': library_item.log_status,
            'message': library_item.log_message,
            'file': library_item.file,
        })
        return result


def _summary(tmdb_type, tmdb_id, items):
    counts = {
        'added': sum(1 for item in items if item['status'] == 0),
        'already_present': sum(1 for item in items if item['status'] == 1),
        'skipped': sum(1 for item in items if item['status'] == 2),
        'failed': sum(1 for item in items if item['status'] >= 3),
    }
    if counts['failed']:
        outcome = 'failed'
    elif counts['added']:
        outcome = 'added'
    elif counts['already_present']:
        outcome = 'already_present'
    elif counts['skipped']:
        outcome = 'skipped'
    else:
        outcome = 'no_change'
    return {
        'tmdb_type': tmdb_type,
        'tmdb_id': int(tmdb_id),
        'outcome': outcome,
        'counts': counts,
        'item_count': len(items),
        # Keep the status URL small even for long-running series while retaining actionable errors.
        'failures': [item for item in items if item['status'] >= 3][:20],
    }


def _add_movie(tmdb_id, force):
    from tmdbhelper.lib.update.builder.movies import LibraryBuilderMovies

    class DuskmateMovieBuilder(_ResultCollector, LibraryBuilderMovies):
        pass

    with DuskmateMovieBuilder() as library:
        library.forced = force
        library.create(tmdb_id=tmdb_id)
        return library.duskmate_items


def _add_tvshow(tmdb_id, force):
    from tmdbhelper.lib.update.builder.tvshows import LibraryBuilderTvshows

    class DuskmateTvshowBuilder(_ResultCollector, LibraryBuilderTvshows):
        pass

    with DuskmateTvshowBuilder() as library:
        library.forced = force
        library.create(tmdb_id=tmdb_id)
        return library.duskmate_items


@is_in_kwargs({'tmdb_type': ['movie', 'tv']})
@get_tmdb_id
def add_to_library(tmdb_type=None, tmdb_id=None, force=False, **kwargs):
    state = _read_state()
    try:
        items = _add_movie(tmdb_id, force) if tmdb_type == 'movie' else _add_tvshow(tmdb_id, force)
        result = _summary(tmdb_type, tmdb_id, items)
    except Exception as error:
        result = {
            'tmdb_type': tmdb_type,
            'tmdb_id': int(tmdb_id),
            'outcome': 'failed',
            'counts': {'added': 0, 'already_present': 0, 'skipped': 0, 'failed': 1},
            'item_count': 0,
            'failures': [],
            'error': str(error),
        }
        state['last_add'] = result
        _write_state(state)
        raise
    state['last_add'] = result
    _write_state(state)
    return result
