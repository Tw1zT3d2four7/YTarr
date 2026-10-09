"""YTarr 0.2.5-test: YouTube / YouTube Music playlist importer for Dispatcharr.

This plugin uses only Python's standard library and Dispatcharr's own models.
It does not add a companion service/container or change Dispatcharr itself.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PLUGIN_ID = "ytarr"
RADIO_HOST = "127.0.0.1"
RADIO_PORT = 8765
_RADIO_SERVER = None
_RADIO_SERVER_LOCK = threading.Lock()
_SCAN_LOCK = threading.Lock()
_SCAN_STOP = threading.Event()
_SCAN_THREAD = None
_SCAN_SETTINGS = {}
_DEFAULT_SCAN_INTERVAL_MINUTES = 30
logger = logging.getLogger("ytarr")
DEFAULT_GROUP = "Country Music"
DEFAULT_PROFILE = "Streamlink"
DEFAULT_PLAYLIST = "https://music.youtube.com/playlist?list=PL5LF_xiPbHiDadP4pjl3h28-1y6oBvY7y"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _setting(settings, key, default=""):
    value = settings.get(key, default) if isinstance(settings, dict) else default
    value = "" if value is None else str(value).strip()
    return value if value else default


def _video_id(raw_url):
    """Extract a video ID from standard YouTube URLs or a bare 11-character ID."""
    value = (raw_url or "").strip()
    if VIDEO_ID_RE.fullmatch(value):
        return value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host in ("youtu.be", "www.youtu.be"):
            candidate = parsed.path.strip("/").split("/")[0]
        elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
            query = parse_qs(parsed.query)
            if parsed.path == "/watch":
                candidate = query.get("v", [""])[0]
            else:
                parts = [part for part in parsed.path.split("/") if part]
                candidate = parts[1] if len(parts) >= 2 and parts[0] in ("embed", "shorts", "live") else ""
        else:
            return None
        return candidate if VIDEO_ID_RE.fullmatch(candidate or "") else None
    except Exception:
        return None


def _playlist_id(raw_url):
    """Accept regular YouTube and YouTube Music playlist URLs, or a playlist ID."""
    value = (raw_url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,80}", value):
        return value
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host not in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"):
            return None
        query = parse_qs(parsed.query)
        candidate = query.get("list", [""])[0].strip()
        # A playlist URL is identified by its list parameter, including music.youtube.com/playlist.
        if parsed.path.rstrip("/") not in ("/playlist", "/watch") and not candidate:
            return None
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{10,80}", candidate or "") else None
    except Exception:
        return None


def _models():
    from apps.channels.models import Channel, ChannelGroup, Logo, Stream
    from core.models import StreamProfile
    return Channel, ChannelGroup, Logo, Stream, StreamProfile


def _profile_by_name(StreamProfile, name):
    try:
        for profile in StreamProfile.objects.all():
            if getattr(profile, "name", "").strip().casefold() == name.casefold():
                return profile
    except Exception:
        pass
    return None


def _profile_names(StreamProfile):
    try:
        return sorted({p.name for p in StreamProfile.objects.all() if getattr(p, "name", "")})
    except Exception:
        return []


def _text_runs(node):
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"].strip()
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict)).strip()
    return ""


def _collect_renderers(obj, out):
    """Walk Innertube response trees and collect playlist item renderers."""
    if isinstance(obj, dict):
        renderer = obj.get("musicResponsiveListItemRenderer")
        if isinstance(renderer, dict):
            out.append(renderer)
        # Some YouTube responses use the ordinary playlist renderer instead.
        renderer = obj.get("playlistVideoRenderer")
        if isinstance(renderer, dict):
            out.append(renderer)
        for value in obj.values():
            _collect_renderers(value, out)
    elif isinstance(obj, list):
        for value in obj:
            _collect_renderers(value, out)


def _renderer_track(renderer):
    video_id = ""
    playlist_data = renderer.get("playlistItemData") or {}
    if isinstance(playlist_data, dict):
        video_id = playlist_data.get("videoId", "")
    if not video_id:
        for key in ("navigationEndpoint", "playlistItemData"):
            endpoint = renderer.get(key)
            if isinstance(endpoint, dict):
                video_id = (((endpoint.get("watchEndpoint") or {}).get("videoId")) or "")
                if video_id:
                    break
    if not video_id:
        for _, value in renderer.items():
            if isinstance(value, dict):
                endpoint = value.get("navigationEndpoint") or {}
                video_id = (((endpoint.get("watchEndpoint") or {}).get("videoId")) or "")
                if video_id:
                    break
    if not VIDEO_ID_RE.fullmatch(video_id or ""):
        return None

    # YouTube Music playlist rows usually carry album-cover artwork in a
    # musicThumbnailRenderer. Prefer that over smaller generic thumbnails.
    artwork_url = _renderer_artwork_url(renderer)

    columns = renderer.get("flexColumns") or []
    title = ""
    artist = "Unknown Artist"
    if columns:
        first = columns[0].get("musicResponsiveListItemFlexColumnRenderer", {})
        title = _text_runs(first.get("text", {}))
    if len(columns) > 1:
        second = columns[1].get("musicResponsiveListItemFlexColumnRenderer", {})
        artist_text = _text_runs(second.get("text", {}))
        if artist_text:
            # Strip common album/date/duration text after separators when present.
            artist = artist_text.split(" • ")[0].strip() or artist
    if not title:
        title = _text_runs(renderer.get("title", {}))
    if not title:
        title = "YouTube Track " + video_id
    # Standard YouTube video thumbnails are a fallback if a playlist row has
    # no album art. They still provide a useful per-track image in Dispatcharr.
    if not artwork_url:
        artwork_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return {"video_id": video_id, "title": title, "artist": artist,
            "artwork_url": artwork_url}


def _renderer_artwork_url(renderer):
    """Return the best album-art thumbnail URL available on a playlist row."""
    candidates = []

    def add_thumbnails(node):
        if not isinstance(node, dict):
            return
        thumbs = node.get("thumbnails")
        if isinstance(thumbs, list):
            for thumb in thumbs:
                if isinstance(thumb, dict) and isinstance(thumb.get("url"), str):
                    url = thumb["url"].strip()
                    if url.startswith("https://") or url.startswith("http://"):
                        try:
                            width = int(thumb.get("width") or 0)
                            height = int(thumb.get("height") or 0)
                        except (TypeError, ValueError):
                            width = height = 0
                        candidates.append((width * height, url))
        for value in node.values():
            if isinstance(value, dict):
                add_thumbnails(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        add_thumbnails(item)

    # Prefer the row's primary thumbnail branch; then inspect all nested
    # thumbnail renderers for YouTube's slightly varying response formats.
    preferred = []
    for key in ("thumbnail", "thumbnailRenderer", "musicThumbnailRenderer"):
        value = renderer.get(key)
        if isinstance(value, dict):
            preferred.append(value)
    for node in preferred:
        add_thumbnails(node)
    if not candidates:
        add_thumbnails(renderer)
    if not candidates:
        return ""
    # Highest-resolution variant first; stable ordering preserves YouTube's
    # ordering when dimensions are not supplied.
    return max(candidates, key=lambda item: item[0])[1]


def _innertube_post(payload, timeout=20):
    endpoint = "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Origin": "https://music.youtube.com", "Referer": "https://music.youtube.com/"
    }, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _playlist_tracks(playlist_id, max_tracks=500):
    """Fetch public playlist metadata and tracks using YouTube Music's web browse endpoint."""
    context = {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20261007.01.00", "hl": "en", "gl": "US"}}
    response = _innertube_post({"context": context, "browseId": "VL" + playlist_id})
    title = "Country Music"
    # Find a title in header renderer variants.
    def scan_title(node):
        nonlocal title
        if isinstance(node, dict):
            for key in ("musicDetailHeaderRenderer", "musicEditablePlaylistDetailHeaderRenderer", "playlistHeaderRenderer"):
                value = node.get(key)
                if isinstance(value, dict):
                    candidate = _text_runs(value.get("title", {}))
                    if candidate:
                        title = candidate
                        return True
            for value in node.values():
                if scan_title(value):
                    return True
        elif isinstance(node, list):
            for value in node:
                if scan_title(value):
                    return True
        return False
    scan_title(response)

    tracks, seen = [], set()
    continuation = None
    page = response
    pages = 0
    while pages < 20 and len(tracks) < max_tracks:
        renderers = []
        _collect_renderers(page, renderers)
        for renderer in renderers:
            track = _renderer_track(renderer)
            if track and track["video_id"] not in seen:
                seen.add(track["video_id"])
                tracks.append(track)
                if len(tracks) >= max_tracks:
                    break
        if len(tracks) >= max_tracks:
            break
        # Find continuation token without depending on one fixed response layout.
        continuation = None
        def find_cont(node):
            if isinstance(node, dict):
                if "continuationCommand" in node and isinstance(node["continuationCommand"], dict):
                    token = node["continuationCommand"].get("token")
                    if token:
                        return token
                if node.get("continuation") and isinstance(node["continuation"], dict):
                    token = node["continuation"].get("continuation")
                    if token:
                        return token
                for value in node.values():
                    found = find_cont(value)
                    if found:
                        return found
            elif isinstance(node, list):
                for value in node:
                    found = find_cont(value)
                    if found:
                        return found
            return None
        continuation = find_cont(page)
        if not continuation:
            break
        page = _innertube_post({"context": context, "continuation": continuation})
        pages += 1
    return title, tracks



def _radio_dependencies():
    """Return missing runtime tools required by the continuous radio endpoint."""
    return [name for name in ("streamlink", "ffmpeg") if shutil.which(name) is None]


class _RadioRequestHandler(BaseHTTPRequestHandler):
    """Serve an endless MP3 stream by playing a YouTube playlist in order."""

    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        logger.info("YTarr radio HTTP: " + fmt, *args)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ytarr/health":
            body = b"YTarr radio ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        match = re.fullmatch(r"/ytarr/radio/([A-Za-z0-9_-]{10,80})", path)
        if not match:
            self.send_error(404, "Unknown YTarr radio endpoint")
            return
        missing = _radio_dependencies()
        if missing:
            self.send_error(503, "YTarr radio requires: " + ", ".join(missing))
            return

        playlist_id = match.group(1)
        try:
            playlist_title, tracks = _playlist_tracks(playlist_id, max_tracks=500)
        except Exception as exc:
            logger.warning("YTarr radio could not load playlist %s: %s", playlist_id, exc)
            self.send_error(502, "Could not load the YouTube playlist")
            return
        video_ids = [track["video_id"] for track in tracks if track.get("video_id")]
        if not video_ids:
            self.send_error(502, "The YouTube playlist contains no playable tracks")
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        # Keep one HTTP response open and resolve each track only when needed.
        # That avoids expired YouTube media URLs and gives players one continuous
        # audio stream rather than requiring the user to select each channel.
        while True:
            failed_tracks = 0
            for video_id in video_ids:
                watch_url = "https://www.youtube.com/watch?v=" + video_id
                process = None
                try:
                    resolved = subprocess.run(
                        ["streamlink", "--loglevel", "error", "--stream-url", watch_url, "best"],
                        capture_output=True, text=True, timeout=40, check=True,
                    ).stdout.strip().splitlines()
                    if not resolved or not resolved[-1].startswith(("http://", "https://")):
                        raise RuntimeError("Streamlink did not return a media URL")
                    media_url = resolved[-1]
                    process = subprocess.Popen(
                        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                         "-i", media_url, "-map", "0:a:0?", "-vn", "-ac", "2",
                         "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k",
                         "-f", "mp3", "-write_xing", "0", "-id3v2_version", "0", "pipe:1"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                    )
                    while True:
                        chunk = process.stdout.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    exit_code = process.wait()
                    if exit_code:
                        failed_tracks += 1
                        logger.warning("YTarr radio ffmpeg exited %s for video %s", exit_code, video_id)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    if process and process.poll() is None:
                        process.terminate()
                    return
                except Exception as exc:
                    failed_tracks += 1
                    logger.warning("YTarr radio skipped video %s: %s", video_id, exc)
                    if process and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                if failed_tracks >= len(video_ids):
                    # Avoid a hot loop if YouTube blocks requests or all tracks fail.
                    time.sleep(5)
                    break
            # Re-fetch the playlist at the end of each complete pass. Newly added
            # tracks are appended to this live stream without restarting playback.
            try:
                _, refreshed_tracks = _playlist_tracks(playlist_id, max_tracks=500)
                refreshed_ids = [track["video_id"] for track in refreshed_tracks
                                 if track.get("video_id")]
                known_ids = set(video_ids)
                added_ids = [video_id for video_id in refreshed_ids if video_id not in known_ids]
                if added_ids:
                    video_ids.extend(added_ids)
                    logger.info("YTarr radio playlist %s discovered %d newly added track(s)",
                                playlist_id, len(added_ids))
            except Exception as exc:
                logger.warning("YTarr radio could not refresh playlist %s: %s", playlist_id, exc)


def _ensure_radio_server():
    """Start the local HTTP endpoint in this Dispatcharr process, if needed."""
    global _RADIO_SERVER
    missing = _radio_dependencies()
    if missing:
        return False, "Continuous radio requires executable(s) not found in Dispatcharr: " + ", ".join(missing)
    with _RADIO_SERVER_LOCK:
        if _RADIO_SERVER is not None:
            return True, "YTarr radio endpoint is running."
        try:
            server = ThreadingHTTPServer((RADIO_HOST, RADIO_PORT), _RadioRequestHandler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, name="ytarr-radio-http", daemon=True)
            thread.start()
            _RADIO_SERVER = server
            return True, f"YTarr radio endpoint listening on {RADIO_HOST}:{RADIO_PORT}."
        except OSError as exc:
            # A different Dispatcharr worker may already own the port. Accept it
            # only when it answers our health check, never for an arbitrary service.
            try:
                with urllib.request.urlopen(
                    f"http://{RADIO_HOST}:{RADIO_PORT}/ytarr/health", timeout=2
                ) as response:
                    healthy = response.status == 200 and response.read(128).strip() == b"YTarr radio ok"
                if healthy:
                    return True, "YTarr radio endpoint is running in another Dispatcharr worker."
            except Exception:
                pass
            return False, f"Could not start YTarr radio endpoint on port {RADIO_PORT}: {exc}"


def _radio_stream_url(profile_command, playlist_id):
    """Format the endpoint URL for the configured Dispatcharr source command."""
    endpoint_url = f"http://{RADIO_HOST}:{RADIO_PORT}/ytarr/radio/{playlist_id}"
    command_name = os.path.basename((profile_command or "").strip()).lower()
    # Streamlink needs its explicit protocol prefix for arbitrary HTTP audio
    # endpoints whose path isn't a recognized media-file extension.
    if "streamlink" in command_name:
        return "httpstream://" + endpoint_url
    return endpoint_url


def _create_radio_channel(playlist_id, playlist_title, group_name, profile, channel_number):
    """Create one stable, continuous radio channel for a configured playlist."""
    Channel, ChannelGroup, Logo, Stream, StreamProfile = _models()
    group, _ = ChannelGroup.objects.get_or_create(name=group_name)
    safe_title = (playlist_title or "YouTube Playlist").strip() or "YouTube Playlist"
    channel_name = (safe_title + " Radio")[:512]
    tvg_id = f"ytarr:radio:{playlist_id}"
    endpoint_url = f"http://{RADIO_HOST}:{RADIO_PORT}/ytarr/radio/{playlist_id}"
    radio_url = _radio_stream_url(getattr(profile, "command", ""), playlist_id)
    stream_defaults = {
        "name": channel_name, "url": radio_url, "channel_group": group,
        "stream_profile": profile, "is_custom": True, "is_radio": True,
        "custom_properties": {
            "provider": "ytarr", "continuous_radio": True,
            "playlist_id": playlist_id, "playlist_title": safe_title,
            "playback_endpoint": endpoint_url,
        },
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
    channel.name = channel_name
    channel.channel_number = channel_number
    channel.channel_group = group
    channel.stream_profile = profile
    channel.tvg_id = tvg_id
    channel.is_radio = True
    channel.save()
    channel.streams.add(stream)
    channel.refresh_from_db()
    return {
        "channel": channel.name, "channel_number": channel_number,
        "channel_id": channel.pk, "stream_id": stream.pk,
        "playlist_id": playlist_id, "playback_url": radio_url,
        "playback_endpoint": endpoint_url, "created": channel_created or stream_created,
    }


def _create_track(settings, track, group_name, profile, channel_number):
    Channel, ChannelGroup, Logo, Stream, StreamProfile = _models()
    group, _ = ChannelGroup.objects.get_or_create(name=group_name)
    video_id = track["video_id"]
    title = track.get("title") or ("YouTube Track " + video_id)
    artist = track.get("artist") or "Unknown Artist"
    stream_name = f"{artist} - {title}" if artist else title
    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    artwork_url = (track.get("artwork_url") or "").strip()
    tvg_id = f"ytarr:{video_id}"
    stream_defaults = {
        "name": stream_name, "url": canonical_url, "channel_group": group,
        "stream_profile": profile, "is_custom": True, "is_radio": True,
        "custom_properties": {"provider": "ytarr", "video_id": video_id, "canonical_url": canonical_url,
                              "artist": artist, "title": title, "playlist_imported": True}
    }
    if artwork_url:
        stream_defaults["logo_url"] = artwork_url
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
    if artwork_url:
        # Channel.logo is a foreign key to Dispatcharr's Logo table, while the
        # stream also receives logo_url for M3U/export paths.
        logo, _ = Logo.objects.get_or_create(
            url=artwork_url,
            defaults={"name": (f"{artist} - {title}" if title else artist)[:255]},
        )
        channel.logo = logo
    channel.save()
    channel.streams.add(stream)
    channel.refresh_from_db()
    return {"channel": channel.name, "channel_number": channel_number, "channel_id": channel.pk,
            "stream_id": stream.pk, "video_id": video_id, "created": channel_created or stream_created}



def _scan_interval_minutes(settings=None):
    """Parse the periodic playlist scan interval, clamped to 5–1440 minutes."""
    settings = settings or {}
    try:
        interval = int(_setting(settings, "playlist_scan_interval_minutes",
                                str(_DEFAULT_SCAN_INTERVAL_MINUTES)))
    except (TypeError, ValueError):
        interval = _DEFAULT_SCAN_INTERVAL_MINUTES
    return max(5, min(interval, 1440))


def _scan_and_import_new_tracks(settings):
    """Compare configured public playlists with existing YTarr channels and add missing tracks."""
    from django.db import close_old_connections

    close_old_connections()
    try:
        playlists = _configured_playlists(settings)
        if not playlists:
            return {"success": False, "new_tracks": 0, "message": "No valid configured playlists."}
        Channel, ChannelGroup, Logo, Stream, StreamProfile = _models()
        profile_name = _setting(settings, "stream_profile", DEFAULT_PROFILE)
        profile = _profile_by_name(StreamProfile, profile_name)
        if profile is None:
            return {"success": False, "new_tracks": 0,
                    "message": f"Stream profile '{profile_name}' was not found."}
        group_name = _setting(settings, "channel_group", DEFAULT_GROUP)
        try:
            start_number = max(0, int(_setting(settings, "channel_number", "9901")))
            max_tracks = max(1, min(500, int(_setting(settings, "max_tracks", "200"))))
        except (TypeError, ValueError):
            start_number, max_tracks = 9901, 200

        existing_channels = list(Channel.objects.filter(tvg_id__startswith="ytarr:"))
        existing_ids = {channel.tvg_id[5:] for channel in existing_channels
                        if channel.tvg_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", channel.tvg_id[5:])}
        try:
            next_number = max([start_number - 1] + [
                int(channel.channel_number) for channel in existing_channels
                if channel.channel_number is not None
            ]) + 1
        except (TypeError, ValueError):
            next_number = start_number

        seen_this_scan = set()
        added = []
        errors = []
        for slot, url, playlist_id in playlists:
            try:
                playlist_title, tracks = _playlist_tracks(playlist_id, max_tracks=max_tracks)
                playlist_added = 0
                for track in tracks:
                    video_id = track.get("video_id")
                    if not video_id or video_id in existing_ids or video_id in seen_this_scan:
                        continue
                    seen_this_scan.add(video_id)
                    try:
                        result = _create_track(settings, track, group_name, profile, next_number)
                        result.update({"source_playlist_id": playlist_id,
                                       "source_playlist_title": playlist_title})
                        added.append(result)
                        existing_ids.add(video_id)
                        next_number += 1
                        playlist_added += 1
                    except Exception as exc:
                        errors.append({"playlist_id": playlist_id, "video_id": video_id,
                                       "error": f"{type(exc).__name__}: {exc}"})
                if playlist_added:
                    logger.info("YTarr scan added %d new track(s) from playlist %s (%s)",
                                playlist_added, playlist_title, playlist_id)
            except Exception as exc:
                errors.append({"playlist_id": playlist_id,
                               "error": f"{type(exc).__name__}: {exc}"})
                logger.warning("YTarr periodic scan failed for playlist %s: %s", playlist_id, exc)

        epg_result = None
        if added:
            try:
                epg_result = _sync_dummy_epg()
            except Exception as exc:
                logger.warning("YTarr added tracks but dummy EPG refresh failed: %s", exc)
        return {"success": not errors or bool(added), "new_tracks": len(added),
                "added_tracks": added[:50], "errors": errors[:20],
                "dummy_epg": epg_result}
    finally:
        close_old_connections()


def _playlist_scan_worker():
    """Background worker that periodically compares configured playlists to Dispatcharr."""
    while not _SCAN_STOP.is_set():
        interval = _scan_interval_minutes(_SCAN_SETTINGS)
        if _SCAN_STOP.wait(interval * 60):
            break
        with _SCAN_LOCK:
            settings = dict(_SCAN_SETTINGS)
        try:
            result = _scan_and_import_new_tracks(settings)
            logger.info("YTarr periodic playlist scan complete: %d new track(s) added.",
                        result.get("new_tracks", 0))
        except Exception:
            logger.exception("YTarr periodic playlist scan failed unexpectedly")


def _start_playlist_scanner(settings=None):
    """Start or reconfigure the periodic playlist scanner."""
    global _SCAN_THREAD, _SCAN_SETTINGS
    settings = dict(settings or {})
    playlists = _configured_playlists(settings)
    if not playlists or any(not playlist_id for _, _, playlist_id in playlists):
        return {"running": False, "interval_minutes": _scan_interval_minutes(settings),
                "message": "No valid playlists configured; periodic scan is not running."}
    with _SCAN_LOCK:
        _SCAN_SETTINGS = settings
        if _SCAN_THREAD is not None and _SCAN_THREAD.is_alive():
            return {"running": True, "interval_minutes": _scan_interval_minutes(settings),
                    "message": "Periodic YouTube playlist scan is running."}
        _SCAN_STOP.clear()
        _SCAN_THREAD = threading.Thread(target=_playlist_scan_worker,
                                        name="ytarr-playlist-scanner", daemon=True)
        _SCAN_THREAD.start()
    return {"running": True, "interval_minutes": _scan_interval_minutes(settings),
            "message": "Periodic YouTube playlist scan started."}


def _settings_from_existing_radio_channels():
    """Recover scan configuration from persisted YTarr radio channels after restart."""
    try:
        Channel, ChannelGroup, Logo, Stream, StreamProfile = _models()
        radio_streams = list(Stream.objects.filter(tvg_id__startswith="ytarr:radio:"))
        if not radio_streams:
            return {}
        settings = {
            "channel_group": DEFAULT_GROUP,
            "stream_profile": DEFAULT_PROFILE,
            "channel_number": 9901,
            "max_tracks": 500,
            "playlist_scan_interval_minutes": _DEFAULT_SCAN_INTERVAL_MINUTES,
            "continuous_radio": True,
        }
        playlist_ids = []
        for stream in radio_streams:
            playlist_id = (getattr(stream, "tvg_id", "") or "").removeprefix("ytarr:radio:")
            if re.fullmatch(r"[A-Za-z0-9_-]{10,80}", playlist_id) and playlist_id not in playlist_ids:
                playlist_ids.append(playlist_id)
            group = getattr(stream, "channel_group", None)
            if group and getattr(group, "name", None):
                settings["channel_group"] = group.name
            profile = getattr(stream, "stream_profile", None)
            if profile and getattr(profile, "name", None):
                settings["stream_profile"] = profile.name
        for index, playlist_id in enumerate(playlist_ids[:5], 1):
            settings[f"playlist_url_{index}"] = playlist_id
        channels = Channel.objects.filter(tvg_id__startswith="ytarr:")
        numbers = [int(value) for value in channels.values_list("channel_number", flat=True)
                   if value is not None]
        if numbers:
            settings["channel_number"] = max(numbers) + 1
        return settings if playlist_ids else {}
    except Exception:
        logger.debug("Could not recover YTarr playlist scanner settings from existing channels",
                     exc_info=True)
        return {}


def _sync_dummy_epg():
    """Attach YTarr channels to a native Dispatcharr dummy EPG with placeholder listings.

    A seven-day schedule is generated in two-hour blocks. Each block is titled
    with the channel/track name so the guide has visible programme text without
    requiring an external XMLTV URL or companion service.
    """
    from apps.channels.models import Channel
    from apps.epg.models import EPGSource, EPGData, ProgramData
    from django.utils import timezone

    source, _ = EPGSource.objects.get_or_create(
        name="YTarr Dummy EPG",
        defaults={
            "source_type": "dummy",
            "is_active": True,
            "refresh_interval": 0,
            "status": "success",
            "last_message": "Generated by YTarr for track-channel guide labels.",
            "custom_properties": {"provider": "ytarr", "managed": True},
        },
    )
    # If an older/source row already exists, ensure it remains a dummy source.
    changed = False
    for field, value in (("source_type", "dummy"), ("is_active", True),
                         ("status", "success"),
                         ("last_message", "Generated by YTarr for track-channel guide labels.")):
        if getattr(source, field, None) != value:
            setattr(source, field, value)
            changed = True
    if changed:
        source.save()

    channels = list(Channel.objects.filter(tvg_id__startswith="ytarr:").select_related("logo"))
    now = timezone.now()
    # Begin half an hour ago so every channel has an active listing immediately.
    schedule_start = now - timedelta(minutes=30)
    schedule_end = now + timedelta(days=7)
    created_programmes = 0
    linked_channels = 0
    for channel in channels:
        if not channel.tvg_id:
            continue
        icon_url = ""
        try:
            icon_url = (channel.logo.url or "")[:500] if channel.logo else ""
        except Exception:
            icon_url = ""
        epg, _ = EPGData.objects.get_or_create(
            tvg_id=channel.tvg_id,
            epg_source=source,
            defaults={"name": channel.name[:512], "icon_url": icon_url or None},
        )
        update_epg = False
        if epg.name != channel.name[:512]:
            epg.name = channel.name[:512]
            update_epg = True
        if icon_url and epg.icon_url != icon_url:
            epg.icon_url = icon_url
            update_epg = True
        if update_epg:
            epg.save()
        if channel.epg_data_id != epg.pk:
            channel.epg_data = epg
            channel.save()
        linked_channels += 1

        # Replace only this YTarr EPG entry's generated rows, leaving all other
        # EPG sources and user-created programme data untouched.
        ProgramData.objects.filter(epg=epg).delete()
        slot_start = schedule_start
        while slot_start < schedule_end:
            slot_end = min(slot_start + timedelta(hours=2), schedule_end)
            ProgramData.objects.create(
                epg=epg,
                start_time=slot_start,
                end_time=slot_end,
                title=channel.name[:255],
                sub_title="YTarr placeholder listing",
                description="Dummy EPG listing generated by YTarr. The channel plays the associated YouTube Music track.",
                tvg_id=channel.tvg_id,
                custom_properties={"provider": "ytarr", "dummy": True},
            )
            created_programmes += 1
            slot_start = slot_end

    source.last_message = f"YTarr generated dummy listings for {linked_channels} channel(s)."
    source.status = "success"
    source.save()
    return {"epg_source": source.name, "channels_linked": linked_channels,
            "programmes_created": created_programmes,
            "schedule_days": 7, "programme_block_hours": 2}


def generate_dummy_epg(settings=None):
    """Refresh dummy EPG data for already-imported YTarr channels."""
    try:
        result = _sync_dummy_epg()
        return {"success": True,
                "message": f"Generated dummy EPG listings for {result['channels_linked']} YTarr channel(s).",
                **result}
    except Exception as exc:
        return {"success": False,
                "message": f"Could not generate dummy EPG: {type(exc).__name__}: {exc}"}


def check_status(settings=None):
    settings = settings or {}
    profile_name = _setting(settings, "stream_profile", DEFAULT_PROFILE)
    try:
        Channel, ChannelGroup, Logo, Stream, StreamProfile = _models()
        names = _profile_names(StreamProfile)
        missing_radio_tools = _radio_dependencies()
        return {"success": True, "message": "YTarr can access Dispatcharr models.",
                "configured_profile": profile_name, "profile_found": _profile_by_name(StreamProfile, profile_name) is not None,
                "available_profiles": names,
                "radio_dependencies": {"required": ["streamlink", "ffmpeg"], "missing": missing_radio_tools},
                "continuous_radio_ready": not missing_radio_tools,
                "playlist_scanner": {"running": bool(_SCAN_THREAD and _SCAN_THREAD.is_alive()),
                                     "interval_minutes": _scan_interval_minutes(settings)},
                "playlist_url_validation": "Supports YouTube and YouTube Music playlist URLs and individual video URLs."}
    except Exception as exc:
        return {"success": False, "message": f"Could not access Dispatcharr models: {type(exc).__name__}: {exc}"}


def create_test_channel(settings=None):
    settings = settings or {}
    raw_url = _setting(settings, "youtube_url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    video_id = _video_id(raw_url)
    if not video_id:
        if _playlist_id(raw_url):
            return {"success": False, "message": "That is a playlist URL. Use Import YouTube Music Playlist to import its tracks, or enter an individual video URL here."}
        return {"success": False, "message": "Enter a valid YouTube / YouTube Music video URL, playlist URL, or video ID."}
    settings = dict(settings)
    settings["title"] = _setting(settings, "title", "YTarr Playback Test")
    settings["artist"] = _setting(settings, "artist", "YTarr Test")
    return _create_one_video(settings, video_id)


def _create_one_video(settings, video_id):
    title = _setting(settings, "title", "YTarr Playback Test")
    artist = _setting(settings, "artist", "YTarr Test")
    group_name = _setting(settings, "channel_group", DEFAULT_GROUP)
    profile_name = _setting(settings, "stream_profile", DEFAULT_PROFILE)
    try:
        channel_number = int(_setting(settings, "channel_number", "9901"))
        if channel_number < 0:
            raise ValueError
    except ValueError:
        return {"success": False, "message": "Channel number must be a non-negative integer."}
    try:
        Channel, ChannelGroup, Logo, Stream, StreamProfile = _models()
        profile = _profile_by_name(StreamProfile, profile_name)
        if profile is None:
            return {"success": False, "message": f"Stream profile '{profile_name}' was not found.", "available_profiles": _profile_names(StreamProfile)}
        result = _create_track(settings, {"video_id": video_id, "title": title, "artist": artist}, group_name, profile, channel_number)
        result.update({"success": True, "message": "Created/updated one track channel. Playback must be tested separately.",
                       "channel_group": group_name, "stream_profile": profile_name,
                       "canonical_url": f"https://www.youtube.com/watch?v={video_id}", "playback_verified": False})
        return result
    except Exception as exc:
        return {"success": False, "message": f"Could not create/update the test channel: {type(exc).__name__}: {exc}"}


def _configured_playlists(settings):
    """Read up to five independently configurable playlist URLs.

    Keep youtube_url as a backward-compatible fallback for older installs.
    """
    urls = []
    for index in range(1, 6):
        key = f"playlist_url_{index}"
        fallback = DEFAULT_PLAYLIST if index == 1 else ""
        value = _setting(settings, key, fallback)
        if value:
            urls.append((index, value))
    if not urls:
        legacy = _setting(settings, "youtube_url", DEFAULT_PLAYLIST)
        if legacy:
            urls.append((1, legacy))
    # Deduplicate repeated playlist IDs while preserving order.
    unique, seen = [], set()
    for slot, url in urls:
        playlist_id = _playlist_id(url)
        identity = playlist_id or url.strip()
        if identity not in seen:
            unique.append((slot, url, playlist_id))
            seen.add(identity)
    return unique


def import_playlist(settings=None):
    """Import tracks from up to five public playlists in one action."""
    settings = settings or {}
    playlists = _configured_playlists(settings)
    if not playlists:
        return {"success": False, "message": "Add at least one valid YouTube or YouTube Music playlist URL."}
    invalid = [{"slot": slot, "url": url} for slot, url, playlist_id in playlists if not playlist_id]
    if invalid:
        return {"success": False, "message": "One or more playlist fields do not contain a valid YouTube playlist URL with a list= ID.", "invalid_playlists": invalid}
    group_name = _setting(settings, "channel_group", DEFAULT_GROUP)
    profile_name = _setting(settings, "stream_profile", DEFAULT_PROFILE)
    try:
        start_number = int(_setting(settings, "channel_number", "9901"))
        max_tracks = int(_setting(settings, "max_tracks", "200"))
        if start_number < 0 or max_tracks < 1 or max_tracks > 500:
            raise ValueError
    except ValueError:
        return {"success": False, "message": "Starting channel number must be non-negative and max tracks must be between 1 and 500."}
    try:
        Channel, ChannelGroup, Logo, Stream, StreamProfile = _models()
        profile = _profile_by_name(StreamProfile, profile_name)
        if profile is None:
            return {"success": False, "message": f"Stream profile '{profile_name}' was not found.", "available_profiles": _profile_names(StreamProfile)}

        imported, errors, playlist_results, seen_video_ids = [], [], [], set()
        radio_channels, radio_errors = [], []
        continuous_radio = str(settings.get("continuous_radio", True)).strip().lower() not in ("false", "0", "no", "off", "")
        radio_ready, radio_status = _ensure_radio_server() if continuous_radio else (False, "Continuous radio disabled.")
        next_channel_number = start_number
        for slot, url, playlist_id in playlists:
            try:
                playlist_title, tracks = _playlist_tracks(playlist_id, max_tracks=max_tracks)
                if not tracks:
                    playlist_results.append({"slot": slot, "playlist_id": playlist_id, "playlist_title": playlist_title,
                                             "tracks_found": 0, "imported_or_updated": 0,
                                             "error": "No track video IDs found; playlist may be private/unavailable or YouTube changed its response."})
                    continue
                playlist_imported = 0
                for track in tracks:
                    video_id = track.get("video_id")
                    if not video_id or video_id in seen_video_ids:
                        continue
                    seen_video_ids.add(video_id)
                    try:
                        result = _create_track(settings, track, group_name, profile, next_channel_number)
                        result.update({"source_playlist_id": playlist_id, "source_playlist_title": playlist_title})
                        imported.append(result)
                        playlist_imported += 1
                        next_channel_number += 1
                    except Exception as exc:
                        errors.append({"playlist_id": playlist_id, "video_id": video_id, "error": f"{type(exc).__name__}: {exc}"})
                playlist_results.append({"slot": slot, "playlist_id": playlist_id, "playlist_title": playlist_title,
                                         "tracks_found": len(tracks), "imported_or_updated": playlist_imported})
                if continuous_radio:
                    if radio_ready:
                        try:
                            radio = _create_radio_channel(
                                playlist_id, playlist_title, group_name, profile, next_channel_number
                            )
                            radio_channels.append(radio)
                            next_channel_number += 1
                        except Exception as exc:
                            radio_errors.append({"playlist_id": playlist_id, "error": f"{type(exc).__name__}: {exc}"})
                    else:
                        radio_errors.append({"playlist_id": playlist_id, "error": radio_status})
            except urllib.error.HTTPError as exc:
                playlist_results.append({"slot": slot, "playlist_id": playlist_id, "error": f"YouTube Music rejected request (HTTP {exc.code}). Playlist may be private or endpoint changed."})
            except urllib.error.URLError as exc:
                playlist_results.append({"slot": slot, "playlist_id": playlist_id, "error": f"Could not reach YouTube Music: {exc.reason}"})
            except Exception as exc:
                playlist_results.append({"slot": slot, "playlist_id": playlist_id, "error": f"{type(exc).__name__}: {exc}"})

        epg_result = None
        epg_error = None
        try:
            epg_result = _sync_dummy_epg()
        except Exception as exc:
            epg_error = f"{type(exc).__name__}: {exc}"
        ok_count = sum(1 for item in playlist_results if item.get("imported_or_updated", 0) > 0)
        scan_status = _start_playlist_scanner(settings)
        return {"success": bool(imported),
                "message": f"Imported/updated {len(imported)} unique tracks from {ok_count} of {len(playlists)} configured playlist(s).",
                "configured_playlists": len(playlists), "playlist_results": playlist_results,
                "channel_group": group_name, "stream_profile": profile_name,
                "tracks_imported_or_updated": len(imported), "errors": errors[:20],
                "dummy_epg": epg_result, "dummy_epg_error": epg_error,
                "duplicate_tracks_skipped": "Duplicate video IDs across playlists are imported only once.",
                "continuous_radio_enabled": continuous_radio,
                "continuous_radio_status": radio_status,
                "continuous_radio_channels": radio_channels,
                "continuous_radio_errors": radio_errors,
                "playlist_scanner": scan_status,
                "note": "YTarr periodically compares each configured playlist with existing channels and imports newly added tracks; the continuous radio also refreshes its playlist after each pass."}
    except Exception as exc:
        return {"success": False, "message": f"Playlist import failed: {type(exc).__name__}: {exc}"}


def get_settings():
    fields = {
        "channel_group": {"label": "Imported channel group", "type": "string", "default": DEFAULT_GROUP},
        "channel_number": {"label": "Starting channel number", "type": "number", "default": 9901},
        "stream_profile": {"label": "Stream profile name", "type": "string", "default": DEFAULT_PROFILE,
                           "description": "Must match an existing Dispatcharr stream profile."},
        "max_tracks": {"label": "Maximum tracks per playlist", "type": "number", "default": 200,
                       "description": "Safety cap for each playlist; allowed range 1-500."},
        "continuous_radio": {"label": "Create continuous radio channel(s)", "type": "boolean", "default": True,
                             "description": "Create one radio channel per playlist that automatically advances through tracks. Requires streamlink and ffmpeg in the Dispatcharr container."},
        "playlist_scan_interval_minutes": {"label": "Playlist rescan interval (minutes)", "type": "number",
                                           "default": 30, "description": "Automatically compare configured YouTube playlists and import newly added songs. Allowed range 5–1440 minutes."}
    }
    for index in range(1, 6):
        fields[f"playlist_url_{index}"] = {
            "label": f"YouTube Music playlist {index} URL",
            "type": "string",
            "default": DEFAULT_PLAYLIST if index == 1 else "",
            "description": "Paste a public YouTube or YouTube Music playlist URL. Leave blank to skip this slot."
        }
    return fields


def get_actions():
    return [
        {"id": "check_status", "name": "Check YTarr Status", "description": "Check Dispatcharr model access and stream profile."},
        {"id": "import_playlist", "name": "Import YouTube Music Playlists (up to 5)", "description": "Import all configured playlist URLs and create/update one selectable channel per unique track."},
        {"id": "generate_dummy_epg", "name": "Generate/Refresh Dummy EPG", "description": "Attach existing YTarr channels to a dummy EPG and create seven days of placeholder programme listings."}
    ]


def run(action, settings=None, **kwargs):
    settings = settings or {}
    action_id = action.get("id") or action.get("action") or action.get("name") if isinstance(action, dict) else action
    action_id = str(action_id or kwargs.get("action_id", "")).strip().lower()
    if action_id in ("check_status", "check ytarr status"):
        return check_status(settings)
    if action_id in ("import_playlist", "import youtube music playlist"):
        return import_playlist(settings)
    if action_id in ("generate_dummy_epg", "generate dummy epg", "refresh_dummy_epg"):
        return generate_dummy_epg(settings)
    return {"success": False, "message": f"Unknown YTarr action: {action_id}"}


class Plugin:
    name = "YTarr"
    version = "0.2.6-test"
    description = "Import YouTube Music playlists with artwork, dummy EPG, continuous radio, and automatic playlist rescans."
    author = "Tw1zT3d2four7"
    fields = [
        {"id": "playlist_url_1", "label": "YouTube Music playlist 1 URL", "type": "string", "default": DEFAULT_PLAYLIST,
         "help_text": "Public YouTube / YouTube Music playlist URL. Leave other slots blank to skip them."},
        {"id": "playlist_url_2", "label": "YouTube Music playlist 2 URL", "type": "string", "default": ""},
        {"id": "playlist_url_3", "label": "YouTube Music playlist 3 URL", "type": "string", "default": ""},
        {"id": "playlist_url_4", "label": "YouTube Music playlist 4 URL", "type": "string", "default": ""},
        {"id": "playlist_url_5", "label": "YouTube Music playlist 5 URL", "type": "string", "default": ""},
        {"id": "channel_group", "label": "Imported channel group", "type": "string", "default": DEFAULT_GROUP},
        {"id": "channel_number", "label": "Starting channel number", "type": "number", "default": 9901},
        {"id": "stream_profile", "label": "Stream profile name", "type": "string", "default": DEFAULT_PROFILE,
         "help_text": "Must exactly match an existing Dispatcharr stream profile."},
        {"id": "max_tracks", "label": "Maximum tracks per playlist", "type": "number", "default": 200},
        {"id": "continuous_radio", "label": "Create continuous radio channel(s)", "type": "boolean", "default": True,
         "help_text": "Create one radio channel per playlist that automatically advances through tracks. Requires streamlink and ffmpeg in Dispatcharr."},
        {"id": "playlist_scan_interval_minutes", "label": "Playlist rescan interval (minutes)", "type": "number", "default": 30,
         "help_text": "Automatically compare configured YouTube playlists and import newly added songs. Allowed range 5–1440 minutes."},
    ]
    actions = [
        {"id": "check_status", "label": "Check YTarr Status", "description": "Check model access and configured profile.",
         "button_label": "Check Status", "button_variant": "filled", "button_color": "blue"},
        {"id": "import_playlist", "label": "Import YouTube Music Playlists (up to 5)", "description": "Import all configured playlist URLs and create/update one selectable channel for each unique track.",
         "button_label": "Import Playlists", "button_variant": "filled", "button_color": "green",
         "confirm": {"required": True, "title": "Import playlists?", "message": "This will create or update channels for tracks found in up to five configured public playlists."}},
        {"id": "generate_dummy_epg", "label": "Generate/Refresh Dummy EPG", "description": "Generate placeholder programme listings for existing YTarr channels so names appear in the guide.",
         "button_label": "Refresh Dummy EPG", "button_variant": "filled", "button_color": "blue"},
    ]

    def run(self, action: str, params: dict, context: dict):
        context = context or {}
        settings = context.get("settings", {}) or {}
        logger = context.get("logger")
        if logger:
            logger.info("YTarr action requested: %s", action)
        # Reconfigure/start the scanner whenever Dispatcharr invokes the plugin
        # with its persisted settings, not only when Import Playlists is clicked.
        if _configured_playlists(settings):
            _start_playlist_scanner(settings)
        result = run(action, settings)
        return {"status": "ok" if result.get("success") else "error",
                "message": result.get("message", "YTarr action finished."), "details": result}

# The radio channels persist in Dispatcharr's database across restarts, so bring
# their local playback endpoint up whenever the enabled plugin is loaded.
try:
    _radio_start_ok, _radio_start_message = _ensure_radio_server()
    if _radio_start_ok:
        logger.info(_radio_start_message)
    else:
        logger.warning(_radio_start_message)
    # On container/plugin restart, rebuild scanner configuration from persisted
    # radio streams so rescans resume without a manual re-import.
    _recovered_scan_settings = _settings_from_existing_radio_channels()
    if _recovered_scan_settings:
        _scan_status = _start_playlist_scanner(_recovered_scan_settings)
        logger.info(_scan_status.get("message", "YTarr scanner startup attempted"))
except Exception:
    logger.exception("Could not initialize the YTarr continuous radio endpoint")

