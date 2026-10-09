# YTarr 0.2.4

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
- This build creates individual selectable track channels. A single continuous radio channel with automatic song advancement is not implemented yet.
- No Dispatcharr source changes, extra service, or extra container.

## Dummy EPG
- Imports automatically create/update a native Dispatcharr EPG source named `YTarr Dummy EPG`, link each `ytarr:<video_id>` channel to its EPG entry, and generate seven days of placeholder programme listings. Each listing displays the track/channel name in the guide.
- Use **Generate/Refresh Dummy EPG** to rebuild listings for already-imported YTarr channels without re-fetching playlists.
- The generated guide is intentionally dummy data; listings repeat the channel's track name in two-hour blocks and do not represent actual song duration or a live schedule.
