# YTarr 0.1.3-test

A small Dispatcharr plugin-interface test. It adds settings and two action buttons through Dispatcharr's documented `Plugin` class interface and manifest fields/actions.

## Test scope
- Checks whether the configured stream profile exists and whether Dispatcharr models can be accessed.
- Creates/updates one test channel and stream pointing at the canonical YouTube watch URL for a supplied video ID.
- Does **not** yet import a YouTube Music library, extract/refresh playable media URLs, or prove web-player playback. The canonical watch URL may not play directly in Dispatcharr; playback support is a later test.
- Requires an existing Dispatcharr stream profile. It does not add a service/container or modify Dispatcharr itself.

## Install
1. In Dispatcharr's **Plugins** page, use **Import** and select this ZIP.
2. Enable YTarr when prompted.
3. If the page was already open, refresh/reload the Plugins page or use the plugin discovery refresh control.
4. Open the YTarr plugin card. The settings fields and action buttons should now be present.
5. First click **Check Status**. Confirm the configured stream profile matches one of the returned available profiles. Then, if appropriate, click **Create Test Channel**.

If no settings/actions appear after importing this build, capture the Dispatcharr version and plugin page/server log error; do not keep running the previous 0.1.0-test ZIP.


## 0.1.3-test compatibility fix
- Corrected the StreamProfile import from `apps.core.models` to `core.models`, matching Dispatcharr's model module path.


Version 0.1.3-test fixes blank saved settings falling through to empty strings instead of defaults. The create/update action now reads the saved channel group back and reports the group/channel/stream IDs and verification status.
