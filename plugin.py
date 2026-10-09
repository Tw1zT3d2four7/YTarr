"""YTarr proof-of-concept plugin for Dispatcharr.

This test build validates plugin loading and Dispatcharr channel/stream creation.
It does not yet implement a YouTube Music library or guarantee web-player playback.
"""

import re
from urllib.parse import urlparse, parse_qs

PLUGIN_ID = "ytarr"
DEFAULT_GROUP = "YTarr Music TEST"
DEFAULT_PROFILE = "Streamlink"


def _setting(settings, key, default=""):
    value = settings.get(key, default) if isinstance(settings, dict) else default
    normalized = "" if value is None else str(value).strip()
    return normalized if normalized else default


def _video_id(raw_url):
    """Extract an 11-character YouTube video ID from common URL forms or a bare ID."""
    value = (raw_url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host in ("youtu.be", "www.youtu.be"):
            candidate = parsed.path.strip("/").split("/")[0]
        elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
            if parsed.path == "/watch":
                candidate = parse_qs(parsed.query).get("v", [""])[0]
            else:
                parts = [part for part in parsed.path.split("/") if part]
                candidate = parts[1] if len(parts) >= 2 and parts[0] in ("embed", "shorts", "live") else ""
        else:
            return None
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate or "") else None
    except Exception:
        return None


def _models():
    """Import Dispatcharr models lazily so the plugin can load in the plugin manager."""
    from apps.channels.models import Channel, ChannelGroup, Stream
    from core.models import StreamProfile
    return Channel, ChannelGroup, Stream, StreamProfile


def _profile_by_name(StreamProfile, profile_name):
    try:
        profiles = StreamProfile.objects.all()
        for profile in profiles:
            if getattr(profile, "name", "").strip().casefold() == profile_name.casefold():
                return profile
    except Exception:
        return None
    return None


def _profile_names(StreamProfile):
    try:
        return sorted({getattr(profile, "name", "") for profile in StreamProfile.objects.all() if getattr(profile, "name", "")})
    except Exception:
        return []


def get_settings():
    """Settings schema consumed by Dispatcharr's plugin settings UI."""
    return {
        "youtube_url": {
            "label": "YouTube video URL or video ID",
            "type": "string",
            "default": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "description": "Test only. Use a public video you are authorized to access."
        },
        "title": {"label": "Track title", "type": "string", "default": "YTarr Playback Test"},
        "artist": {"label": "Artist", "type": "string", "default": "YTarr Test"},
        "channel_group": {"label": "Channel group", "type": "string", "default": DEFAULT_GROUP},
        "channel_number": {"label": "Channel number", "type": "string", "default": "9901"},
        "stream_profile": {"label": "Stream profile name", "type": "string", "default": DEFAULT_PROFILE,
                           "description": "Must match an existing Dispatcharr stream profile."}
    }


def get_actions():
    return [
        {"id": "check_status", "name": "Check YTarr Test Status", "description": "Check model access and the configured stream profile."},
        {"id": "create_test_channel", "name": "Create / Update Test Track Channel", "description": "Create or update one test channel and its canonical YouTube URL stream."}
    ]


def run(action, settings=None, **kwargs):
    """Run a plugin action. Supports the common Dispatcharr action invocation forms."""
    settings = settings or {}
    action_id = action
    if isinstance(action, dict):
        action_id = action.get("id") or action.get("action") or action.get("name")
    action_id = str(action_id or kwargs.get("action_id", "")).strip().lower()
    if action_id in ("check_status", "check ytarr test status"):
        return check_status(settings)
    if action_id in ("create_test_channel", "create / update test track channel", "create_test_track_channel"):
        return create_test_channel(settings)
    return {"success": False, "message": f"Unknown YTarr action: {action_id}"}


def check_status(settings=None):
    settings = settings or {}
    profile_name = _setting(settings, "stream_profile", DEFAULT_PROFILE)
    try:
        Channel, ChannelGroup, Stream, StreamProfile = _models()
        names = _profile_names(StreamProfile)
        profile = _profile_by_name(StreamProfile, profile_name)
        return {
            "success": True,
            "message": "YTarr plugin code is callable and Dispatcharr channel models are accessible.",
            "configured_profile": profile_name,
            "profile_found": profile is not None,
            "available_profiles": names,
            "next_step": "Run Create / Update Test Track Channel, then test playback in the Dispatcharr webplayer."
        }
    except Exception as exc:
        return {"success": False, "message": f"Could not access Dispatcharr models: {type(exc).__name__}: {exc}"}


def create_test_channel(settings=None):
    settings = settings or {}
    raw_url = _setting(settings, "youtube_url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    video_id = _video_id(raw_url)
    if not video_id:
        return {"success": False, "message": "Enter a valid YouTube video URL or 11-character video ID."}

    title = _setting(settings, "title", "YTarr Playback Test") or "YTarr Playback Test"
    artist = _setting(settings, "artist", "YTarr Test") or "YTarr Test"
    group_name = _setting(settings, "channel_group", DEFAULT_GROUP) or DEFAULT_GROUP
    profile_name = _setting(settings, "stream_profile", DEFAULT_PROFILE) or DEFAULT_PROFILE
    try:
        channel_number = int(_setting(settings, "channel_number", "9901"))
        if channel_number < 0:
            raise ValueError
    except ValueError:
        return {"success": False, "message": "Channel number must be a non-negative integer."}

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    stream_name = f"{artist} - {title}" if artist else title
    tvg_id = f"ytarr:{video_id}"

    try:
        Channel, ChannelGroup, Stream, StreamProfile = _models()
        profile = _profile_by_name(StreamProfile, profile_name)
        if profile is None:
            return {
                "success": False,
                "message": f"Stream profile '{profile_name}' was not found. Set the plugin setting to an existing profile name.",
                "available_profiles": _profile_names(StreamProfile)
            }

        group, _ = ChannelGroup.objects.get_or_create(name=group_name)

        stream_defaults = {
            "name": stream_name,
            "url": canonical_url,
            "channel_group": group,
            "stream_profile": profile,
            "is_custom": True,
            "is_radio": True,
            "custom_properties": {
                "provider": "ytarr",
                "video_id": video_id,
                "canonical_url": canonical_url,
                "artist": artist,
                "title": title
            }
        }
        stream, stream_created = Stream.objects.get_or_create(tvg_id=tvg_id, defaults=stream_defaults)
        if not stream_created:
            for key, value in stream_defaults.items():
                setattr(stream, key, value)
            stream.save()

        channel = Channel.objects.filter(tvg_id=tvg_id).first()
        channel_created = channel is None
        if channel is None:
            channel = Channel()
        channel.name = stream_name
        channel.channel_number = channel_number
        channel.channel_group = group
        channel.stream_profile = profile
        channel.tvg_id = tvg_id
        channel.is_radio = True
        channel.save()
        channel.streams.add(stream)

        # Read back the persisted relationships instead of assuming save() succeeded.
        channel.refresh_from_db()
        group_exists = ChannelGroup.objects.filter(pk=group.pk, name=group_name).exists()
        channel_group_name = channel.channel_group.name if channel.channel_group_id else None
        stream_group_name = stream.channel_group.name if stream.channel_group_id else None
        if not group_exists or channel_group_name != group_name:
            return {
                "success": False,
                "message": "YTarr saved the test channel but could not verify its channel-group assignment.",
                "channel": channel.name,
                "channel_id": channel.pk,
                "channel_group_expected": group_name,
                "channel_group_actual": channel_group_name,
                "stream_group_actual": stream_group_name,
                "group_exists": group_exists,
            }

        return {
            "success": True,
            "message": ("Created" if channel_created else "Updated") + " YTarr test channel. Channel and group records were saved; playback is a separate verification.",
            "channel": channel.name,
            "channel_number": channel_number,
            "channel_group": group_name,
            "channel_group_id": group.pk,
            "channel_group_verified": group_exists and channel_group_name == group_name,
            "channel_group_actual": channel_group_name,
            "stream_group_actual": stream_group_name,
            "channel_id": channel.pk,
            "stream_id": stream.pk,
            "stream_profile": profile_name,
            "video_id": video_id,
            "canonical_url": canonical_url,
            "stream_created": stream_created,
            "channel_created": channel_created,
            "playback_verified": False
        }
    except Exception as exc:
        return {"success": False, "message": f"Could not create/update the test channel: {type(exc).__name__}: {exc}"}


class Plugin:
    """Dispatcharr's documented class-based plugin interface."""
    name = "YTarr"
    version = "0.1.3-test"
    description = "YTarr proof-of-concept: create a Dispatcharr test channel from a YouTube video URL."
    author = "Tw1zT3d2four7"

    fields = [
        {"id": "youtube_url", "label": "YouTube video URL or video ID", "type": "string",
         "default": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
         "help_text": "Test only. Use a public video you are authorized to access."},
        {"id": "title", "label": "Track title", "type": "string", "default": "YTarr Playback Test"},
        {"id": "artist", "label": "Artist", "type": "string", "default": "YTarr Test"},
        {"id": "channel_group", "label": "Channel group", "type": "string", "default": DEFAULT_GROUP},
        {"id": "channel_number", "label": "Channel number", "type": "number", "default": 9901},
        {"id": "stream_profile", "label": "Stream profile name", "type": "string", "default": DEFAULT_PROFILE,
         "help_text": "Must exactly match an existing Dispatcharr stream profile."},
    ]

    actions = [
        {"id": "check_status", "label": "Check YTarr Test Status",
         "description": "Check access to Dispatcharr models and the selected stream profile.",
         "button_label": "Check Status", "button_variant": "filled", "button_color": "blue"},
        {"id": "create_test_channel", "label": "Create / Update Test Track Channel",
         "description": "Create or update one test channel using a canonical YouTube URL.",
         "button_label": "Create Test Channel", "button_variant": "filled", "button_color": "green",
         "confirm": {"required": True, "title": "Create test channel?",
                     "message": "This will create or update a test channel in Dispatcharr."}},
    ]

    def run(self, action: str, params: dict, context: dict):
        settings = (context or {}).get("settings", {}) or {}
        logger = (context or {}).get("logger")
        if logger:
            logger.info("YTarr test action requested: %s", action)
        if action == "check_status":
            result = check_status(settings)
        elif action == "create_test_channel":
            result = create_test_channel(settings)
        else:
            result = {"success": False, "message": f"Unknown YTarr action: {action}"}
        # Dispatcharr's plugin UI expects a status/message result.
        return {
            "status": "ok" if result.get("success") else "error",
            "message": result.get("message", "YTarr action finished."),
            "details": result,
        }
