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
DUSKMATE_BUILD = '44'
STATUS_FILE = f'{ADDONDATA}duskmate-status.json'
SOURCES_FILE = 'special://profile/sources.xml'
TRAKT_SYNC_ADDON = 'script.module.duskmate.sync.trakt'
CURATED_FOLDER = 'Duskmate Curated'
LIBRARY_SOURCES = {
    'movies': 'Duskmate Movies',
    'tvshows': 'Duskmate TV Shows',
}
SAFE_LIBRARY_PATHS = {
    'movies': 'special://profile/addon_data/plugin.video.duskmate/movies/',
    'tvshows': 'special://profile/addon_data/plugin.video.duskmate/tvshows/',
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


def _path_is_writable(path, attempts=1):
    """Verify a library root with a real VFS write, not merely exists().

    Network sources can remain readable after their credentials or share permissions stop allowing
    writes.  Treating ``exists()`` as readiness leaves the UI waiting for a STRM that can never be
    created.  A tiny probe catches that condition before a library builder starts.
    """
    from xbmc import sleep
    from xbmcvfs import File, delete, exists, mkdirs

    root = (path or '').replace(chr(92), '/').rstrip('/') + '/'
    if not root.strip('/'):
        return False
    probe = f'{root}.duskmate-write-probe-{os.getpid()}.tmp'
    for attempt in range(max(1, attempts)):
        try:
            if not exists(root):
                mkdirs(root)
            handle = File(probe, 'w')
            try:
                handle.write('duskmate')
            finally:
                handle.close()
            if exists(probe):
                delete(probe)
                return True
        except Exception:
            pass
        try:
            delete(probe)
        except Exception:
            pass
        if attempt + 1 < attempts:
            sleep(250 * (attempt + 1))
    return False


def _effective_library_paths(required_type):
    """Return writable paths while preserving the viewer's configured helper paths.

    The configured root is always preferred and retried on every independent operation.  If it is
    temporarily read-only or offline, Duskmate falls back to its app-private Kodi library instead
    of failing playback.  The setting itself is never overwritten, so a recovered NAS is used
    automatically next time.
    """
    configured = _library_paths()
    effective = dict(configured)
    preferred = configured[required_type]

    # Never put an interactive title click behind a network filesystem write. Kodi's VFS can
    # block inside SMB/NFS before Python regains control, so no retry or timeout around File.write
    # can guarantee responsiveness. Network-configured helper roots remain untouched for the
    # user's own workflows; Duskmate additions use its local managed source deterministically.
    network_preferred = preferred.lower().startswith(('smb://', 'nfs://', 'ftp://', 'sftp://'))

    # A disconnected/read-only network share can take seconds to reject every write. Once a real
    # probe has failed, keep using the verified local fallback for a short cooldown rather than
    # blocking every title click on the same doomed network operation. The user's configured path
    # remains untouched and is probed again after the cooldown, so recovery is automatic.
    state = _read_state()
    remembered = state.get('library_fallbacks', {}).get(required_type, {})
    remembered_at = int(remembered.get('at') or 0)
    remembered_preferred = remembered.get('preferred')
    remembered_active = remembered.get('active')
    if remembered_active and remembered_active.lower().startswith('special://'):
        # Kodi's Python VFS can write a special:// path, but the video scanner on Android does not
        # reliably enumerate it when used as a library source. Persist and scan the translated
        # filesystem path instead; it is still Kodi-private and requires no storage permission.
        from xbmcvfs import translatePath
        remembered_active = translatePath(remembered_active).replace(chr(92), '/').rstrip('/') + '/'
    if (remembered_preferred == preferred and remembered_active and
            int(time.time()) - remembered_at < 15 * 60 and
            _path_is_writable(remembered_active)):
        effective[required_type] = remembered_active
        # A fallback remembered by an older build may predate registration of the local source.
        # Reconcile its source and scraper binding before submitting a scan: successfully writing
        # STRM/NFO files alone does not make those files visible in Kodi's library.
        if not _ensure_video_sources(effective):
            raise RuntimeError('Kodi could not register the remembered safe library source')
        bindings = _ensure_content_bindings(effective)
        if not bindings.get(required_type):
            raise RuntimeError('Kodi could not assign metadata.local to the remembered safe library source')
        return effective

    if not network_preferred and _path_is_writable(preferred):
        return effective

    from xbmcvfs import translatePath
    fallback = translatePath(SAFE_LIBRARY_PATHS[required_type]).replace(chr(92), '/').rstrip('/') + '/'
    if not _path_is_writable(fallback):
        raise RuntimeError('Kodi cannot write to either the configured library or its local fallback')
    effective[required_type] = fallback

    # Kodi must know how to scrape the fallback before its targeted scan starts.  Keep every
    # unrelated source untouched; only Duskmate's managed source is moved when necessary.
    if not _ensure_video_sources(effective):
        raise RuntimeError('Kodi could not register the safe local library source')
    bindings = _ensure_content_bindings(effective)
    if not bindings.get(required_type):
        raise RuntimeError('Kodi could not assign metadata.local to the safe local library source')

    fallbacks = state.setdefault('library_fallbacks', {})
    fallbacks[required_type] = {
        'preferred': preferred,
        'active': fallback,
        'at': int(time.time()),
    }
    _write_state(state)
    return effective


def _normalise_path(path):
    return (path or '').replace(chr(92), '/').rstrip('/').lower()


def _path_covers(source, target):
    """Return True when a Kodi source is the target path or one of its parents."""
    source = _normalise_path(source)
    target = _normalise_path(target)
    return bool(source and target and (target == source or target.startswith(f'{source}/')))


def _ensure_video_sources(paths):
    """Ensure each helper library path is covered by one ordinary Kodi video source.

    An existing user source may point at the exact helper directory or at one of its parents. In
    either case it already covers the helper library and must be reused. This prevents overlapping
    “Duskmate Movies” / “Duskmate TV Shows” sources and duplicate Kodi indexing. A managed source
    is created only when no existing source covers that helper path.
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

    changed = False
    for key, path in paths.items():
        managed_name = LIBRARY_SOURCES[key]
        sources = list(video.findall('./source'))
        external_cover = any(
            (source.findtext('name') or '') != managed_name
            and any(_path_covers(node.text, path) for node in source.findall('./path') if node.text)
            for source in sources
        )
        if external_cover:
            # Clean up an older Duskmate source only when another user/source entry already covers
            # the same helper library. Never remove the sole route to a library path.
            for source in sources:
                if (source.findtext('name') or '') == managed_name:
                    video.remove(source)
                    changed = True
            continue
        if any(
            _path_covers(node.text, path)
            for source in sources
            for node in source.findall('./path')
            if node.text
        ):
            continue
        managed = next((
            source for source in sources
            if (source.findtext('name') or '') == managed_name
        ), None)
        if managed is None:
            managed = ElementTree.SubElement(video, 'source')
            ElementTree.SubElement(managed, 'name').text = managed_name
            ElementTree.SubElement(managed, 'allowsharing').text = 'true'
        for node in list(managed.findall('./path')):
            managed.remove(node)
        path_node = ElementTree.SubElement(managed, 'path', {'pathversion': '1'})
        path_node.text = path
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
    persisted_paths = [
        node.text
        for node in persisted.findall('./video/source/path')
        if node.text
    ]
    return all(any(_path_covers(source, path) for source in persisted_paths) for path in paths.values())


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


def _video_content_rows():
    """Read Kodi's content roots without modifying its video database."""
    try:
        import sqlite3
    except ImportError:
        return []
    database = _video_database_file()
    if not database:
        return []
    try:
        connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True, timeout=1)
        try:
            return connection.execute(
                "SELECT strPath, strContent FROM path WHERE strContent IN ('movies', 'tvshows')"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []


def _content_bindings(paths):
    """Read Kodi's own content bindings without ever modifying its database."""
    bindings = {'movies': False, 'tvshows': False}
    rows = _video_content_rows()
    from xbmcvfs import translatePath
    for key, path in paths.items():
        candidates = (path, translatePath(path))
        bindings[key] = any(
            content == key and any(_path_covers(row_path, candidate) for candidate in candidates)
            for row_path, content in rows
        )
    return bindings


def _is_managed_fallback(key, path):
    """Return whether *path* is Duskmate's safe local root, in either Kodi path form."""
    from xbmcvfs import translatePath
    safe_path = SAFE_LIBRARY_PATHS[key]
    candidates = (safe_path, translatePath(safe_path))
    return any(_normalise_path(path) == _normalise_path(candidate) for candidate in candidates)


def _ensure_content_bindings(paths):
    """Bind the helper's *effective* roots as Kodi movie/TV sources.

    Kodi exposes no JSON-RPC method for assigning content to a video source. The helper therefore
    performs the same narrowly scoped database update as Kodi's Set content dialog: only the two
    paths currently selected in the helper settings are touched. Existing parent bindings are
    reused, and no unrelated source, scraper setting, or library row is changed.

    TMDB Helper writes URL-NFO files, so these managed roots must use Kodi's official TMDB
    scrapers. ``metadata.local`` cannot resolve a URL-only NFO and leaves the scanner running
    without ever indexing the selected episode. Schema checks keep this fail-closed on an unknown
    Kodi database instead of making a speculative write.
    """
    bindings = _content_bindings(paths)
    managed_fallbacks = {
        key: _is_managed_fallback(key, path)
        for key, path in paths.items()
    }
    # Existing user-owned content roots are never rewritten. Duskmate's own safe roots are
    # deliberately reconciled on every setup/preparation so an older incorrect scan mode is
    # migrated in place instead of remaining permanently "ready" but unscannable.
    if all(bindings.values()) and not any(managed_fallbacks.values()):
        return bindings

    try:
        import sqlite3
    except ImportError:
        return bindings
    database = _video_database_file()
    if not database:
        return bindings

    from xbmcvfs import translatePath
    try:
        connection = sqlite3.connect(database, timeout=5)
        try:
            connection.execute('PRAGMA busy_timeout=5000')
            columns = {
                row[1]: row
                for row in connection.execute('PRAGMA table_info(path)').fetchall()
            }
            required = {'idPath', 'strPath', 'strContent', 'strScraper'}
            if not required.issubset(columns):
                return bindings

            rows = connection.execute('SELECT idPath, strPath, strContent FROM path').fetchall()
            connection.execute('BEGIN IMMEDIATE')
            for key, configured_path in paths.items():
                if bindings[key] and not managed_fallbacks[key]:
                    continue
                configured_path = configured_path.replace(chr(92), '/').rstrip('/') + '/'
                translated_path = translatePath(configured_path).replace(chr(92), '/').rstrip('/') + '/'
                candidates = [configured_path, translated_path]
                if managed_fallbacks[key]:
                    safe_path = SAFE_LIBRARY_PATHS[key]
                    candidates.extend((safe_path, translatePath(safe_path)))

                # Prefer an existing exact row in either Kodi or translated form. Only create a
                # row when Kodi has never seen this precise helper root.
                exact_rows = [row for row in rows if any(
                    _normalise_path(row[1]) == _normalise_path(candidate)
                    for candidate in candidates
                )]
                # Kodi's TV content dialog stores ``useFolderNames=0`` when the selected
                # directory contains multiple show folders.  A value of 1 means "this
                # directory contains a single TV show", which makes Kodi look for
                # tvshow.nfo at the managed root and skip every show below it.  Movies
                # retain folder-name matching because each managed movie has its own
                # directory.
                values = {
                    'strContent': key,
                    'strScraper': (
                        'metadata.tvshows.themoviedb.org.python'
                        if key == 'tvshows'
                        else 'metadata.themoviedb.org.python'
                    ),
                    'scanRecursive': 0,
                    'useFolderNames': 0 if key == 'tvshows' else 1,
                    'strSettings': '',
                    'noUpdate': 0,
                    'exclude': 0,
                    'allAudio': 0,
                }
                values = {name: value for name, value in values.items() if name in columns}
                if exact_rows:
                    assignments = ', '.join(f'{name}=?' for name in values)
                    for exact in exact_rows:
                        connection.execute(
                            f'UPDATE path SET {assignments} WHERE idPath=?',
                            (*values.values(), exact[0]),
                        )
                else:
                    insert_values = {'strPath': configured_path, **values}
                    names = ', '.join(insert_values)
                    placeholders = ', '.join('?' for _ in insert_values)
                    connection.execute(
                        f'INSERT INTO path ({names}) VALUES ({placeholders})',
                        tuple(insert_values.values()),
                    )
                    rows.append((connection.execute('SELECT last_insert_rowid()').fetchone()[0], configured_path, key))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return bindings
    return _content_bindings(paths)


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
        from tmdbhelper.lib.sync.providers.scrobbling_settings import ScrobblingProviderSetting
        scrobbling = 'trakt' in ScrobblingProviderSetting('scrobbling').provider_ids
    except (RuntimeError, TypeError, ValueError):
        return {
            'installed': False,
            'enabled': False,
            'authorized': False,
            'managed': False,
            'scrobbling': False,
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
        'scrobbling': scrobbling,
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
    pending_owner = int(curated.get('pending_owner_pid') or 0)
    pending_started = int(curated.get('pending_started_at') or 0)
    pending_is_live = (
        pending_owner == os.getpid()
        and pending_started > int(time.time()) - (6 * 60 * 60)
    )
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
        'last_curated_operation': state.get('last_curated_operation'),
        'curated': {
            'generation': curated.get('generation', ''),
            # A pending generation is live only while it belongs to this Kodi process. If Kodi
            # was restarted after an interrupted mirror, the Android client may safely retry it.
            'pending_generation': curated.get('pending_generation', '') if pending_is_live else '',
            'active_count': curated.get('active_count', 0),
            'owned_count': len(curated.get('entries', {})),
            'removed_files': curated.get('removed_files', 0),
        },
        'trakt_sync': _trakt_sync_status(),
    }


def setup(**kwargs):
    # Read the helper's effective settings after installation. These may be its vanilla defaults
    # or paths the user redirected earlier; Duskmate must never replace either choice.
    paths = _library_paths()
    paths_ready = _paths_ready(paths, create=True)
    sources_ready = _ensure_video_sources(paths) if paths_ready else False
    bindings = _ensure_content_bindings(paths) if sources_ready else _content_bindings(paths)
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
    # Authorizing Duskmate's managed provider is explicit consent to record Kodi playback in that
    # Trakt account. Select only the scrobbling capability; preserve the viewer's choices for
    # watched indicators, lists, collection, ratings and every other provider feature.
    from tmdbhelper.lib.sync.providers.scrobbling_settings import ScrobblingProviderSetting
    scrobbling = ScrobblingProviderSetting('scrobbling')
    scrobbling.add_provider('trakt')
    if 'trakt' not in scrobbling.provider_ids:
        raise RuntimeError('Kodi did not enable Trakt scrobbling')
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
    from jurialmunkey.ftools import cached_property
    from tmdbhelper.lib.update.items.movie import LibraryMovie

    effective_paths = _effective_library_paths('movies')
    target_root = effective_paths['movies']
    if curated:
        target_root = f'{target_root.replace(chr(92), "/").rstrip("/")}/{CURATED_FOLDER}/'

    class DuskmateMovie(LibraryMovie):
        @cached_property
        def basedir(self):
            return target_root

    class DuskmateMovieBuilder(_ResultCollector, LibraryBuilderMovies):
        @cached_property
        def library_item_class(self):
            return DuskmateMovie

    with DuskmateMovieBuilder() as library:
        library.forced = force
        library.create(tmdb_id=tmdb_id)
        return library.duskmate_items, target_root


def _scan_library_when_idle(path, timeout=600):
    """Serialize silent targeted scans; Kodi ignores or duplicates overlapping scan requests."""
    from xbmc import executeJSONRPC, getCondVisibility, sleep
    deadline = time.time() + timeout
    while getCondVisibility('Library.IsScanningVideo'):
        if time.time() >= deadline:
            return False
        sleep(500)
    executeJSONRPC(json.dumps({
        'jsonrpc': '2.0',
        'id': 'duskmate-priority-scan',
        'method': 'VideoLibrary.Scan',
        'params': {'directory': path, 'showdialogs': False},
    }))
    return True


def _kodi_episode_id(tmdb_id, season, episode):
    """Return Kodi's episode id when this TMDB episode is already indexed."""
    from xbmc import executeJSONRPC

    shows = json.loads(executeJSONRPC(json.dumps({
        'jsonrpc': '2.0',
        'id': 'duskmate-upnext-shows',
        'method': 'VideoLibrary.GetTVShows',
        'params': {'properties': ['uniqueid']},
    }))).get('result', {}).get('tvshows', [])
    tvshowid = next((
        item.get('tvshowid')
        for item in shows
        if str((item.get('uniqueid') or {}).get('tmdb') or '') == str(tmdb_id)
    ), None)
    if tvshowid is None:
        return None
    episodes = json.loads(executeJSONRPC(json.dumps({
        'jsonrpc': '2.0',
        'id': 'duskmate-upnext-episodes',
        'method': 'VideoLibrary.GetEpisodes',
        'params': {
            'tvshowid': tvshowid,
            'season': int(season),
            'properties': ['episode'],
        },
    }))).get('result', {}).get('episodes', [])
    return next((
        item.get('episodeid')
        for item in episodes
        if int(item.get('episode', -1)) == int(episode)
    ), None)


def _upnext_candidates(tmdb_id, season, episode):
    """Return aired, non-special episodes after the current episode in broadcast order."""
    from datetime import date
    from tmdbhelper.lib.items.database.baseview_factories.factory import BaseViewFactory

    current = (int(season), int(episode))
    today = date.today().isoformat()
    items = BaseViewFactory('flatseasons', 'tv', int(tmdb_id)).data or []
    return [
        item for item in items
        if int(item.get('infolabels', {}).get('season') or 0) > 0
        and (
            int(item.get('infolabels', {}).get('season') or 0),
            int(item.get('infolabels', {}).get('episode') or 0),
        ) > current
        and item.get('infolabels', {}).get('premiered')
        and str(item['infolabels']['premiered']) <= today
    ]


def _upnext_item(tmdb_id, season, episode):
    """Resolve the first aired episode after the current one that is not already watched.

    ListItemDetails merges the enabled Kodi/Trakt sync providers, so playcount reflects the
    viewer's combined watched state rather than TMDB metadata alone.
    """
    from tmdbhelper.lib.items.database.listitem import ListItemDetails

    candidates = _upnext_candidates(tmdb_id, season, episode)
    if not candidates:
        return None
    lidc = ListItemDetails()
    lidc.cache_refresh = 'basic'
    lidc.cache_translations = False
    lidc.extendedinfo = False
    # Resolve in bounded batches so a long show does not turn one popup into one huge request,
    # while still finding the true next unwatched episode after a long watched run.
    for start in range(0, len(candidates), 40):
        configured = lidc.configure_listitems_threaded(candidates[start:start + 40])
        selected = next((
            item for item in configured
            if item and int(item.infolabels.get('playcount') or 0) == 0
        ), None)
        if selected:
            return selected
    return None


def _serialise_upnext(item, tmdb_id):
    item.finalise()
    labels = item.infolabels
    season = int(labels.get('season') or 0)
    episode = int(labels.get('episode') or 0)
    kodi_id = _kodi_episode_id(tmdb_id, season, episode)
    play_url = (
        'plugin://plugin.video.duskmate/?info=play&tmdb_type=tv'
        f'&tmdb_id={int(tmdb_id)}&season={season}&episode={episode}'
        '&allow_playlist=false'
    )
    return {
        'next_episode': {
            'episodeid': kodi_id,
            'tvshowid': -1,
            'season': season,
            'episode': episode,
            'title': labels.get('title') or labels.get('label') or '',
            'showtitle': labels.get('tvshowtitle') or labels.get('showtitle') or '',
            'plot': labels.get('plot') or '',
            'firstaired': labels.get('premiered') or '',
            'runtime': labels.get('duration') or labels.get('runtime') or 0,
            'rating': labels.get('rating') or 0,
            'playcount': int(labels.get('playcount') or 0),
            'art': dict(item.art or {}),
            'file': play_url,
        },
        'play_url': play_url,
        'tmdb_id': int(tmdb_id),
        'library_episodeid': kodi_id,
    }


def resolve_upnext(request_id=None, tmdb_id=None, season=None, episode=None, **kwargs):
    """Resolve Up Next through Duskmate and return it through a request-scoped window property.

    The response is published before any missing library item is imported, so playback UI never
    waits for a full-series write/scan. The normal Duskmate priority import then writes and scans
    the selected episode first and continues the complete series in its established queue.
    """
    from xbmc import executebuiltin
    from xbmcgui import Window

    property_name = f'Duskmate.UpNext.Response.{request_id or "default"}'
    response = {'ok': False}
    try:
        if tmdb_id is None or season is None or episode is None:
            raise ValueError('TMDB show id, season and episode are required')
        item = _upnext_item(int(tmdb_id), int(season), int(episode))
        if item is None:
            response['reason'] = 'no_aired_unwatched_episode'
        else:
            response.update(_serialise_upnext(item, int(tmdb_id)))
            response['ok'] = True
    except Exception as error:
        response['reason'] = 'resolver_failed'
        response['error'] = str(error)
    Window(10000).setProperty(property_name, json.dumps(response, separators=(',', ':')))

    if response.get('ok') and response.get('library_episodeid') is None:
        next_item = response['next_episode']
        executebuiltin(
            'RunScript(plugin.video.duskmate,duskmate_add_to_library,'
            f'tmdb_type=tv,tmdb_id={int(tmdb_id)},season={next_item["season"]},'
            f'episode={next_item["episode"]},request_id=upnext-{request_id or "default"})'
        )
    return response


def _add_tvshow(tmdb_id, force, curated=False, priority_season=0, priority_episode=0,
                priority_only=False):
    from tmdbhelper.lib.update.builder.tvshows import LibraryBuilderTvshows
    from jurialmunkey.ftools import cached_property
    from tmdbhelper.lib.update.items.episode import LibraryEpisode
    from tmdbhelper.lib.update.items.season import LibrarySeason
    from tmdbhelper.lib.update.items.tvshow import LibraryTvshow

    effective_paths = _effective_library_paths('tvshows')
    target_root = effective_paths['tvshows']
    if curated:
        target_root = f'{target_root.replace(chr(92), "/").rstrip("/")}/{CURATED_FOLDER}/'

    class DuskmateEpisode(LibraryEpisode):
        @cached_property
        def basedir(self):
            return target_root

    class DuskmateSeason(LibrarySeason):
        @cached_property
        def basedir(self):
            return target_root

        def get_episode(self, sync_data):
            episode = DuskmateEpisode(self.tmdb_id)
            episode.sync_data = self.key_getter(sync_data)
            episode.cache = self.cache
            episode.tvshow_id = self.tvshow_id
            episode.folders = self.folders + episode.folders
            return episode

    class DuskmateTvshow(LibraryTvshow):
        @cached_property
        def basedir(self):
            return target_root

        def get_season(self, sync_data):
            season = DuskmateSeason(self.tmdb_id)
            season.sync_data = self.key_getter(sync_data)
            season.cache = self.cache
            season.tvshow_id = self.tvshow_id
            season.folders = self.folders + season.folders
            return season

    class DuskmateTvshowBuilder(_ResultCollector, LibraryBuilderTvshows):
        @cached_property
        def library_item_class(self):
            return DuskmateTvshow

        def add_library_item_seasons(self, tvshow):
            # For an episode explicitly selected in Duskmate, write that STRM first and start a
            # scoped scan immediately. Continue through the complete series afterwards, skipping
            # only the item already written. This preserves full-series imports without making the
            # viewer wait for hundreds of unrelated episodes before Play using Kodi is available.
            priority = None
            if not curated and priority_season > 0 and priority_episode > 0:
                for season_item in tvshow.seasons:
                    if season_item.season != priority_season:
                        continue
                    priority = next(
                        (item for item in season_item.episodes if item.episode == priority_episode),
                        None,
                    )
                    break
            if priority is not None:
                self.set_library_item(priority)
                _scan_library_when_idle(target_root)
                # The explicitly selected episode is the interactive operation. Return as soon as
                # its files exist and its scoped scan has started; the Android worker submits the
                # complete-series import later, when Kodi is not playing anything. Previously this
                # method continued through every season before publishing success, leaving both
                # Duskmate surfaces on "Preparing Kodi" for minutes.
                if priority_only:
                    return
            elif priority_only:
                # A provider can expose a just-aired/live episode before TMDB publishes it. Never
                # fall through to a full-series import in that case: report the precise condition
                # immediately so the UI can keep the provider action usable and offer a later retry.
                self.duskmate_items.append({
                    'tmdb_id': int(tmdb_id),
                    'season': int(priority_season),
                    'episode': int(priority_episode),
                    'status': 3,
                    'message': 'episode_not_available',
                    'file': '',
                    'owned_files': [],
                })
                return

            from tmdbhelper.lib.addon.plugin import get_localized
            total = len(tvshow.seasons)
            for count, season_item in enumerate(tvshow.seasons):
                message = f'{get_localized(32167)} {tvshow.name} - {get_localized(20373)} {season_item.season}...'
                self.dialog_msg(count, total, message=message)
                episodes = season_item.episodes
                episode_total = len(episodes)
                for episode_count, episode_item in enumerate(episodes, 1):
                    if (priority is not None and episode_item.season == priority_season
                            and episode_item.episode == priority_episode):
                        continue
                    self.set_library_item(episode_item)
                    self.dialog_msg(episode_count, episode_total)

    with DuskmateTvshowBuilder() as library:
        library.forced = force
        library.create(tmdb_id=tmdb_id)
        items = library.duskmate_items
    if not curated and priority_season > 0 and priority_episode > 0 and not priority_only:
        # Queue one final pass after the priority scan instead of overlapping scans per batch.
        _scan_library_when_idle(target_root)
    return items, target_root


@is_in_kwargs({'tmdb_type': ['movie', 'tv']})
@get_tmdb_id
def add_to_library(tmdb_type=None, tmdb_id=None, force=False, **kwargs):
    state = _read_state()
    curated = str(kwargs.get('curated', '')).lower() in ('1', 'true', 'yes')
    key = f'{tmdb_type}:{int(tmdb_id)}'
    if curated:
        from xbmcvfs import exists
        curated_state = state.setdefault('curated', {'generation': '', 'entries': {}})
        previous = curated_state.setdefault('entries', {}).get(key, {})
        owned_files = [filename for filename in previous.get('files', []) if filename]
        # A completed managed entry is the strongest possible duplicate check: it works before
        # Kodi has scanned the files into its database and avoids fetching every season again.
        if owned_files and all(exists(filename) for filename in owned_files):
            generation = str(kwargs.get('generation') or curated_state.get('generation') or '')
            previous['generation'] = generation
            pending_keys = curated_state.setdefault('pending_keys', [])
            if key not in pending_keys:
                pending_keys.append(key)
            result = {
                'tmdb_type': tmdb_type,
                'tmdb_id': int(tmdb_id),
                'outcome': 'already_present',
                'counts': {
                    'added': 0,
                    'already_present': len(owned_files),
                    'skipped': 0,
                    'failed': 0,
                },
                'item_count': len(owned_files),
                'failures': [],
                'request_id': str(kwargs.get('request_id') or ''),
            }
            state['last_add'] = result
            _write_state(state)
            return result
    try:
        priority_season = int(kwargs.get('season') or 0)
        priority_episode = int(kwargs.get('episode') or 0)
        priority_only = str(kwargs.get('priority_only', '')).lower() in ('1', 'true', 'yes')
        items, write_path = _add_movie(tmdb_id, force, curated) if tmdb_type == 'movie' else _add_tvshow(
            tmdb_id,
            force,
            curated,
            priority_season=priority_season,
            priority_episode=priority_episode,
            priority_only=priority_only,
        )
        result = _summary(tmdb_type, tmdb_id, items)
        result['library_path'] = write_path
        if any(item.get('message') == 'episode_not_available' for item in items):
            result['error'] = 'This episode is not available in TMDB yet. Try again after its metadata is published.'
        result['request_id'] = str(kwargs.get('request_id') or '')
        result['scan_managed'] = bool(not curated and (
            tmdb_type == 'movie'
            or (tmdb_type == 'tv' and priority_season > 0 and priority_episode > 0)
        ))
        if tmdb_type == 'movie' and not curated:
            _scan_library_when_idle(write_path)
        # Detached full-series imports are started only by the durable Android queue after it has
        # verified that Kodi is idle. They own their final targeted scan because no caller waits
        # for this background result.
        if (str(kwargs.get('background', '')).lower() in ('1', 'true', 'yes')
                and tmdb_type == 'tv' and not curated):
            _scan_library_when_idle(write_path)
    except Exception as error:
        result = {
            'tmdb_type': tmdb_type,
            'tmdb_id': int(tmdb_id),
            'outcome': 'failed',
            'counts': {'added': 0, 'already_present': 0, 'skipped': 0, 'failed': 1},
            'item_count': 0,
            'failures': [],
            'error': str(error),
            'request_id': str(kwargs.get('request_id') or ''),
        }
        state['last_add'] = result
        _write_state(state)
        raise
    state['last_add'] = result
    if curated:
        curated_state = state.setdefault('curated', {'generation': '', 'entries': {}})
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
    now = int(time.time())
    pending_owner = int(curated.get('pending_owner_pid') or 0)
    pending_started = int(curated.get('pending_started_at') or 0)
    if (
            curated.get('pending_generation')
            and pending_owner == os.getpid()
            and pending_started > now - (6 * 60 * 60)):
        raise RuntimeError('A Duskmate curated-library mirror is already running')
    curated['pending_generation'] = str(generation)
    curated['pending_keys'] = []
    curated['pending_owner_pid'] = os.getpid()
    curated['pending_started_at'] = now
    state['last_curated_operation'] = {
        'action': 'begin',
        'request_id': str(kwargs.get('request_id') or ''),
        'outcome': 'success',
    }
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
    state['last_curated_operation'] = {
        'action': 'touch',
        'request_id': str(kwargs.get('request_id') or ''),
        'outcome': 'success',
    }
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
    curated.pop('pending_owner_pid', None)
    curated.pop('pending_started_at', None)
    curated['active_count'] = len(active)
    curated['removed_files'] = removed
    state['last_curated_operation'] = {
        'action': 'commit',
        'request_id': str(kwargs.get('request_id') or ''),
        'outcome': 'success',
    }
    _write_state(state)
    return True


def curated_abort(generation=None, **kwargs):
    """Release only an unfinished transaction; completed entries and files stay untouched."""
    state = _read_state()
    curated = state.setdefault('curated', {'entries': {}})
    expected = str(generation or '')
    pending = str(curated.get('pending_generation') or '')
    if expected and pending and expected != pending:
        raise ValueError('The curated generation does not match the pending update')
    curated.pop('pending_generation', None)
    curated.pop('pending_keys', None)
    curated.pop('pending_owner_pid', None)
    curated.pop('pending_started_at', None)
    state['last_curated_operation'] = {
        'action': 'abort',
        'request_id': str(kwargs.get('request_id') or ''),
        'outcome': 'success',
    }
    _write_state(state)
    return True
