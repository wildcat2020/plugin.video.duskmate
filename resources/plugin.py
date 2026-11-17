# Module: default
# Author: jurialmunkey
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html
if __name__ == '__main__':
    import sys
    from urllib.parse import parse_qsl

    handle = int(sys.argv[1])
    query = sys.argv[2][1:]
    pairs = parse_qsl(query, keep_blank_values=True)
    bare_action = query.split('&', 1)[0] if query and '=' not in query.split('&', 1)[0] else ''
    info_action = next((value for key, value in pairs if key == 'info'), '')
    action = bare_action or info_action
    script_actions = {
        'duskmate_setup',
        'duskmate_provision_trakt',
        'duskmate_add_to_library',
        'duskmate_resolve_upnext',
        'duskmate_curated_begin',
        'duskmate_curated_touch',
        'duskmate_curated_commit',
        'duskmate_curated_abort',
    }

    if action in script_actions:
        # Addons.ExecuteAddon targets the plug-in extension when an add-on also exposes a
        # script extension. Never run a slow library job while this directory handle is open:
        # Kodi 21 on 32-bit ARM can abort when Android pauses Kodi during that work. Hand the
        # job to the add-on's script entry point and close the directory immediately.
        import xbmc
        from xbmcplugin import endOfDirectory
        args = [action]
        args.extend(
            f'{key}={value}'
            for key, value in pairs
            if key != 'info' and key != bare_action
        )
        if handle >= 0:
            endOfDirectory(handle, succeeded=True, cacheToDisc=False)
        xbmc.executebuiltin(
            'RunScript(plugin.video.duskmate,{})'.format(
                ','.join(args)
            )
        )
    else:
        from tmdbhelper.lib.items.router import Router
        Router(handle, query).run()
