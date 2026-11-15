# Module: Duskmate integration
# Upstream: jurialmunkey/plugin.video.themoviedb.helper
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html
import json
import os
import re
import time
from xml.etree import ElementTree
from base64 import urlsafe_b64decode
from binascii import Error as Base64Error
from hashlib import sha256

from tmdbhelper.lib.addon.plugin import ADDONDATA, get_version
from tmdbhelper.lib.script.method.decorators import get_tmdb_id, is_in_kwargs
from tmdbhelper.lib.update.common import LibraryCommon


UPSTREAM_VERSION = '6.21.5'
DUSKMATE_BUILD = '12'
STATUS_FILE = f'{ADDONDATA}duskmate-status.json'
SOURCES_FILE = 'special://profile/sources.xml'
TRAKT_SYNC_ADDON = 'script.module.duskmate.sync.trakt'
CURATED_FOLDER = 'Duskmate Curated'
LIBRARY_SOURCES = {
    'movies': 'Duskmate Movies',
    'tvshows': 'Duskmate TV Shows',
}


def _library_paths():
    return {
        'movies': LibraryCommon.get_basedir('movies'),
        'tvshows': LibraryCommon.get_basedir('tvshows'),
    }


def _curated_paths():
    paths = {}
    for key, value in _library_paths().items():
        root = value.replace(chr(92), '/').rstrip('/')
        paths[key] = f'{root}/{CURATED_FOLDER}/'
    return paths


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


def _normalise_path(path):
    return (path or '').replace(chr(92), '/').rstrip('/').lower()


def _ensure_video_sources(paths):
    """Add the two Duskmate folders to Kodi's ordinary video-source file.

    Kodi 21 has no JSON-RPC method for adding a source or assigning its content.  Writing the
    documented sources.xml entry is safe and leaves the content decision to Kodi's own native
    Set content dialog, which the Duskmate installation wizard opens immediately afterwards.
    """
    from xbmcvfs import File, exists, mkdirs, translatePath

    profile = 'special://profile/'
    if not exists(profile):
        mkdirs(profile)
    root = None
    if exists(SOURCES_FILE):
        source_file = File(SOURCES_FILE)
        try:
            raw = source_file.read() or ''
        finally:
            source_file.close()
        if raw.strip():
            try:
                root = ElementTree.fromstring(raw)
            except ElementTree.ParseError:
                # Never replace a malformed user file. The wizard will report setup as incomplete
                # and the Kodi log will retain the parsing failure for diagnosis.
                return False
    if root is None:
        root = ElementTree.Element('sources')
    video = root.find('video')
    if video is None:
        video = ElementTree.SubElement(root, 'video')

    existing = {
        _normalise_path(path_node.text)
        for path_node in video.findall('./source/path')
        if path_node.text
    }
    changed = False
    for key, path in paths.items():
        if _normalise_path(path) in existing:
            continue
        source = ElementTree.SubElement(video, 'source')
        ElementTree.SubElement(source, 'name').text = LIBRARY_SOURCES[key]
        path_node = ElementTree.SubElement(source, 'path', {'pathversion': '1'})
        path_node.text = path
        ElementTree.SubElement(source, 'allowsharing').text = 'true'
        changed = True

    if changed or not exists(SOURCES_FILE):
        xml = ElementTree.tostring(root, encoding='utf-8', xml_declaration=True).decode('utf-8')
        source_file = File(SOURCES_FILE, 'w')
        try:
            source_file.write(xml)
        finally:
            source_file.close()

    # Re-read what was persisted rather than treating a successful write call as proof.
    source_file = File(SOURCES_FILE)
    try:
        persisted = ElementTree.fromstring(source_file.read() or '')
    except (ElementTree.ParseError, TypeError, ValueError):
        return False
    finally:
        source_file.close()
    persisted_paths = {
        _normalise_path(node.text)
        for node in persisted.findall('./video/source/path')
        if node.text
    }
    return all(_normalise_path(path) in persisted_paths for path in paths.values())


def _video_database_file():
    from xbmcvfs import listdir, translatePath
    database_dir = 'special://database/'
    _, files = listdir(database_dir)
    candidates = []
    for filename in files:
        match = re.fullmatch(r'MyVideos(\d+)\.db', filename, re.IGNORECASE)
        if match:
            candidates.append((int(match.group(1)), filename))
    if not candidates:
        return ''
    return os.path.join(translatePath(database_dir), max(candidates)[1])


def _content_bindings(paths):
    """Read Kodi's own content bindings without ever modifying its database."""
    try:
        import sqlite3
    except ImportError:
        return {'movies': False, 'tvshows': False}
    bindings = {'movies': False, 'tvshows': False}
    database = _video_database_file()
    if not database:
        return bindings
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=1)
        try:
            rows = connection.execute(
                "SELECT strPath, strContent FROM path WHERE strContent IN ('movies', 'tvshows')"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return bindings

    from xbmcvfs import translatePath
    for key, path in paths.items():
        candidates = {_normalise_path(path), _normalise_path(translatePath(path))}
        bindings[key] = any(
            content == key and _normalise_path(row_path) in candidates
            for row_path, content in rows
        )
    return bindings


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
    from xbmc import getLocalizedString
    paths = _library_paths()
    bindings = _content_bindings(paths)
    state = _read_state()
    curated = state.get('curated', {})
    return {
        'schema': 3,
        'addon_version': get_version(),
        'upstream_version': UPSTREAM_VERSION,
        'duskmate_build': DUSKMATE_BUILD,
        'library_ready': _paths_ready(paths) and all(bindings.values()),
        'library_paths': paths,
        'library_sources': LIBRARY_SOURCES,
        'library_content': bindings,
        'kodi_labels': {
            'set_content': getLocalizedString(20333),
            'change_content': getLocalizedString(20442),
            'movies': getLocalizedString(20342),
            'tvshows': getLocalizedString(20343),
            'ok': getLocalizedString(186),
        },
        'last_add': state.get('last_add'),
        'curated': {
            'generation': curated.get('generation', ''),
            'active_count': curated.get('active_count', 0),
            'owned_count': len(curated.get('entries', {})),
            'removed_files': curated.get('removed_files', 0),
        },
        'trakt_sync': _trakt_sync_status(),
    }


def setup(**kwargs):
    paths = _library_paths()
    paths_ready = _paths_ready(paths, create=True)
    sources_ready = _ensure_video_sources(paths) if paths_ready else False
    bindings = _content_bindings(paths)
    ready = paths_ready and sources_ready and all(bindings.values())
    state = _read_state()
    state['setup'] = {
        'library_ready': ready,
        'library_paths': paths,
        'library_sources_ready': sources_ready,
        'library_content': bindings,
    }
    _write_state(state)
    return ready


def provision_trakt(authorization=None, **kwargs):
    if not authorization:
        raise ValueError('A Trakt authorization payload is required')
    try:
        payload = json.loads(urlsafe_b64decode(f'{authorization}===').decode('utf-8'))
    except (Base64Error, TypeError, UnicodeDecodeError, ValueError) as error:
        raise ValueError('The Trakt authorization payload is invalid') from error

    access_token = (payload.get('access_token') or '').strip()
    now = int(time.time())
    try:
        expires_at = int(payload.get('expires_at'))
    except (TypeError, ValueError) as error:
        raise ValueError('A valid Trakt token expiry is required') from error
    if not access_token:
        raise ValueError('A Trakt access token is required')
    if expires_at - now < 60:
        raise ValueError('The Trakt access token expires too soon')

    from xbmcaddon import Addon
    from xbmcgui import Window
    addon = Addon(TRAKT_SYNC_ADDON)
    stored = json.dumps({
        'access_token': access_token,
        'created_at': now,
        'expires_in': expires_at - now,
        'managed_by': 'duskmate',
        'scope': 'public',
        'token_type': 'bearer',
    }, separators=(',', ':'), sort_keys=True)
    addon.setSetting('token', stored)
    addon.setSetting('credentials_migrated', 'true')
    if addon.getSetting('token') != stored:
        raise RuntimeError('Kodi did not retain the Duskmate Trakt authorization')
    window = Window(10000)
    for name in (
            'TraktUserToken', 'TraktIsAuth', 'TraktRefreshAttempts',
            'TraktRefreshTimeStamp', 'TraktAttemptedLogin'):
        window.clearProperty(name)
    return True


class _ResultCollector:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.duskmate_items = []

    def set_library_item(self, library_item):
        result = super().set_library_item(library_item)
        owned_files = []
        for writer_name in ('strm_filewriter', 'info_filewriter'):
            writer = getattr(library_item, writer_name, None)
            if writer and writer.success:
                owned_files.append(writer.filename_and_path)
        self.duskmate_items.append({
            'tmdb_id': library_item.tmdb_id,
            'season': library_item.season,
            'episode': library_item.episode,
            'status': library_item.log_status,
            'message': library_item.log_message,
            'file': library_item.file,
            'owned_files': owned_files,
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


def _add_movie(tmdb_id, force, curated=False):
    from tmdbhelper.lib.update.builder.movies import LibraryBuilderMovies

    if curated:
        from jurialmunkey.ftools import cached_property
        from tmdbhelper.lib.update.items.movie import LibraryMovie

        class DuskmateCuratedMovie(LibraryMovie):
            @cached_property
            def basedir(self):
                return _curated_paths()['movies']

    class DuskmateMovieBuilder(_ResultCollector, LibraryBuilderMovies):
        if curated:
            @cached_property
            def library_item_class(self):
                return DuskmateCuratedMovie

    with DuskmateMovieBuilder() as library:
        library.forced = force
        library.create(tmdb_id=tmdb_id)
        return library.duskmate_items


def _add_tvshow(tmdb_id, force, curated=False):
    from tmdbhelper.lib.update.builder.tvshows import LibraryBuilderTvshows

    if curated:
        from jurialmunkey.ftools import cached_property
        from tmdbhelper.lib.update.items.episode import LibraryEpisode
        from tmdbhelper.lib.update.items.season import LibrarySeason
        from tmdbhelper.lib.update.items.tvshow import LibraryTvshow

        class DuskmateCuratedEpisode(LibraryEpisode):
            @cached_property
            def basedir(self):
                return _curated_paths()['tvshows']

        class DuskmateCuratedSeason(LibrarySeason):
            def get_episode(self, sync_data):
                episode = DuskmateCuratedEpisode(self.tmdb_id)
                episode.sync_data = self.key_getter(sync_data)
                episode.cache = self.cache
                episode.tvshow_id = self.tvshow_id
                episode.folders = self.folders + episode.folders
                return episode

        class DuskmateCuratedTvshow(LibraryTvshow):
            @cached_property
            def basedir(self):
                return _curated_paths()['tvshows']

            def get_season(self, sync_data):
                season = DuskmateCuratedSeason(self.tmdb_id)
                season.sync_data = self.key_getter(sync_data)
                season.cache = self.cache
                season.tvshow_id = self.tvshow_id
                season.folders = self.folders + season.folders
                return season

    class DuskmateTvshowBuilder(_ResultCollector, LibraryBuilderTvshows):
        if curated:
            @cached_property
            def library_item_class(self):
                return DuskmateCuratedTvshow

    with DuskmateTvshowBuilder() as library:
        library.forced = force
        library.create(tmdb_id=tmdb_id)
        return library.duskmate_items


@is_in_kwargs({'tmdb_type': ['movie', 'tv']})
@get_tmdb_id
def add_to_library(tmdb_type=None, tmdb_id=None, force=False, **kwargs):
    state = _read_state()
    curated = str(kwargs.get('curated', '')).lower() in ('1', 'true', 'yes')
    try:
        items = _add_movie(tmdb_id, force, curated) if tmdb_type == 'movie' else _add_tvshow(tmdb_id, force, curated)
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
    if curated:
        curated_state = state.setdefault('curated', {'generation': '', 'entries': {}})
        key = f'{tmdb_type}:{int(tmdb_id)}'
        previous = curated_state.setdefault('entries', {}).get(key, {})
        owned_files = sorted({
            filename
            for item in items
            for filename in item.get('owned_files', [])
            if filename
        })
        curated_state['entries'][key] = {
            'tmdb_type': tmdb_type,
            'tmdb_id': int(tmdb_id),
            'generation': str(kwargs.get('generation') or curated_state.get('generation') or ''),
            'retained': bool(previous.get('retained')),
            'files': sorted(set(previous.get('files', [])) | set(owned_files)),
        }
        pending_keys = curated_state.setdefault('pending_keys', [])
        if key not in pending_keys:
            pending_keys.append(key)
    _write_state(state)
    return result


def curated_begin(generation=None, **kwargs):
    if not generation:
        raise ValueError('A curated generation is required')
    state = _read_state()
    curated = state.setdefault('curated', {'entries': {}})
    curated['pending_generation'] = str(generation)
    curated['pending_keys'] = []
    _write_state(state)
    return True


def curated_touch(tmdb_type=None, tmdb_id=None, generation=None, retain=False, **kwargs):
    if tmdb_type not in ('movie', 'tv') or not tmdb_id:
        raise ValueError('A movie or TV TMDB id is required')
    state = _read_state()
    curated = state.setdefault('curated', {'entries': {}})
    key = f'{tmdb_type}:{int(tmdb_id)}'
    keys = curated.setdefault('pending_keys', [])
    if key not in keys:
        keys.append(key)
    entry = curated.setdefault('entries', {}).get(key)
    if entry:
        entry['generation'] = str(generation or curated.get('pending_generation') or '')
        if str(retain).lower() in ('1', 'true', 'yes'):
            entry['retained'] = True
    _write_state(state)
    return True


def _safe_curated_file(filename):
    from xbmcvfs import translatePath
    translated = translatePath(filename).replace('\\', '/').lower()
    return any(
        translated.startswith(translatePath(path).replace('\\', '/').lower())
        for path in _curated_paths().values()
    )


def curated_commit(generation=None, **kwargs):
    from xbmcvfs import delete, exists
    state = _read_state()
    curated = state.setdefault('curated', {'entries': {}})
    expected = str(generation or '')
    if not expected or expected != str(curated.get('pending_generation') or ''):
        raise ValueError('The curated generation does not match the pending update')
    active = set(curated.get('pending_keys', []))
    entries = curated.setdefault('entries', {})
    removed = 0
    for key, entry in list(entries.items()):
        if key in active or entry.get('retained'):
            continue
        for filename in entry.get('files', []):
            if _safe_curated_file(filename) and exists(filename) and delete(filename):
                removed += 1
        del entries[key]
    curated['generation'] = expected
    curated.pop('pending_generation', None)
    curated.pop('pending_keys', None)
    curated['active_count'] = len(active)
    curated['removed_files'] = removed
    _write_state(state)
    return True
