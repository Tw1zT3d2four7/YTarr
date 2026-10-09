# YTarr 0.2.5-test

Dispatcharr plugin for importing up to five public YouTube / YouTube Music playlists. It creates or updates one selectable channel per unique track in the configured group (default: `Country Music`). Track title, artist, and album-art thumbnail are read from playlist metadata automatically.

## Configure
1. Import this ZIP through Dispatcharr → Plugins → Import and enable YTarr.
2. Paste up to five public YouTube or YouTube Music playlist URLs into Playlist URL 1–5. Playlist 1 defaults to the Country Music playlist supplied for testing; unused slots may remain blank.
3. Select an existing Dispatcharr stream profile (default `Streamlink`), a channel group, a starting channel number, and a per-playlist track cap.
4. Run **Import YouTube Music Playlists (up to 5)**. The result reports per-playlist outcomes and the total tracks imported/updated.

## Artwork behavior
- Uses the playlist row's highest-resolution album-art thumbnail where available.
- Saves artwork to Dispatcharr's channel Logo relation and the stream's `logo_url`, so it can appear in the channel UI and stream/M3U output.
- If album art is absent, falls back to the track's YouTube video thumbnail.
- Re-import the playlists to update existing channels with artwork; stable `ytarr:<video_id>` identifiers are retained.

## Behavior and limits
- No manual track title, artist, or image fields are needed.
- Duplicate tracks across configured playlists are imported only once by YouTube video ID. Existing channels are updated using stable `ytarr:<video_id>` identifiers.
- Public playlists only. YouTube may change its internal web endpoint or block requests; test the import from inside Dispatcharr.
- This test build creates individual selectable track channels and, by default, one continuous radio channel per configured playlist. The radio channel streams audio continuously and automatically advances through playlist tracks.
- No Dispatcharr source changes, extra service, or extra container.

## Dummy EPG
- Imports automatically create/update a native Dispatcharr EPG source named `YTarr Dummy EPG`, link each `ytarr:<video_id>` channel to its EPG entry, and generate seven days of placeholder programme listings. Each listing displays the track/channel name in the guide.
- Use **Generate/Refresh Dummy EPG** to rebuild listings for already-imported YTarr channels without re-fetching playlists.
- The generated guide is intentionally dummy data; listings repeat the channel's track name in two-hour blocks and do not represent actual song duration or a live schedule.

## Continuous radio playback (test build)

- **Create continuous radio channel(s)** is enabled by default. Each successfully fetched playlist gets a separate channel named `<playlist title> Radio`, alongside the individual track channels.
- The radio endpoint resolves each YouTube track when it is about to play, converts its audio to a continuous MP3 stream, and advances to the next playlist item automatically. This avoids relying on the Dispatcharr web player to advance between individual channels.
- The endpoint runs inside the Dispatcharr plugin process on `127.0.0.1:8765`; it does not require a separate container or a published host port. The channel uses that local endpoint as its stream URL.
- The Dispatcharr container must have both `streamlink` and `ffmpeg` available on PATH. **Check YTarr Status** reports missing executables. If either is missing, playlist imports still work but radio channels are not created.
- This is an experimental test-branch implementation. Test the radio channel in Dispatcharr and confirm that it moves from one song to the next before considering a release.
