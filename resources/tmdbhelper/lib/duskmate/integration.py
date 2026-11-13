# Module: Duskmate integration
# Upstream: jurialmunkey/plugin.video.themoviedb.helper
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html
import json

from tmdbhelper.lib.addon.plugin import ADDONDATA, get_version
from tmdbhelper.lib.script.method.decorators import get_tmdb_id, is_in_kwargs
from tmdbhelper.lib.update.common import LibraryCommon


UPSTREAM_VERSION = '6.21.5'
DUSKMATE_BUILD = '1'
STATUS_FILE = f'{ADDONDATA}duskmate-status.json'


def _library_paths():
    return {
        'movies': LibraryCommon.get_basedir('movies'),
        'tvshows': LibraryCommon.get_basedir('tvshows'),
    }


def _paths_ready(paths, create=False):
    from xbmcvfs import exists, mkdirs
    ready = True
    for path in paths.values():
        if create and not exists(path):
            mkdirs(path)
        ready = exists(path) and ready
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


def get_status():
    paths = _library_paths()
    state = _read_state()
    return {
        'schema': 1,
        'addon_version': get_version(),
        'upstream_version': UPSTREAM_VERSION,
        'duskmate_build': DUSKMATE_BUILD,
        'library_ready': _paths_ready(paths),
        'library_paths': paths,
        'last_add': state.get('last_add'),
    }


def setup(**kwargs):
    paths = _library_paths()
    ready = _paths_ready(paths, create=True)
    state = _read_state()
    state['setup'] = {'library_ready': ready, 'library_paths': paths}
    _write_state(state)
    return ready


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
