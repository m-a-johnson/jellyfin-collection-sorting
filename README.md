# Jellyfin Collection Manager

A small web app to see which collections each show is in, which shows are in none, and to move shows
in and out of a collection. Titles and years only. It reads from Jellyfin on demand and stores nothing.

What it can change: **only a collection's membership**, with `POST` / `DELETE /Collections/{id}/Items`.
That never deletes a show or any media. The write client in `app/jellyfin.py` validates the method, the path
and every ID, so it cannot reach `DELETE /Items` (which deletes media from disk). Set `read_only: true`
(or `READ_ONLY=1`) to disable editing completely.

## Run

1. Create the config folder and file yourself, owned by the container user (99:100 is `nobody:users` on Unraid):
   ```bash
   mkdir -p /mnt/user/appdata/jf-collections
   cp config.example.yml /mnt/user/appdata/jf-collections/config.yml
   nano /mnt/user/appdata/jf-collections/config.yml     # Jellyfin URL, admin API key (keep the quotes), user_id
   chown -R 99:100 /mnt/user/appdata/jf-collections
   chmod 600 /mnt/user/appdata/jf-collections/config.yml
   ```
   (If the file is missing, the app writes a template and exits, which with `restart: unless-stopped` becomes a restart
   loop, so create it first.)
2. Start the container (`docker compose up -d`; the compose file pulls the image published by GitHub Actions) and open
   `http://<host>:8099`.
3. Open the **Self-check** tab and run it before trusting anything else.

```bash
docker logs jf-collections
```

There is no login in the app itself, and the editor can change your collections. Put it behind your reverse
proxy's authentication and do not expose the port outside your network. Change requests also need a custom
`X-Requested-With` header, so a web page on another site cannot make your browser send changes to it.

## Views

- **Editor**: pick a collection. The left list is every show *not* in it (from the libraries you tick), the right
  list is the shows in it. Select shows (click, Shift-click for a range, or double-click / Enter to move one) and use
  **Add →** / **← Remove**. Moves are only staged on screen; **Apply…** shows exactly what will change, and after you
  confirm, the app re-reads the collection from Jellyfin and tells you whether every change is really there.
  Chips show each show's other collections. Collections owned by the Auto Collections plugin are read-only.
- **Shows**: every show with its collections as chips. Orange chips are collections owned by the Auto Collections plugin.
- **Uncollected**: shows in no collection. By default shows that are only in plugin-managed collections are counted as uncollected (toggle on the page).
- **Collections**: each collection with its member shows.
- **Self-check**: tests the Jellyfin calls the app relies on.

## What was verified and what was not

Seen in a real Jellyfin 12.1.0 OpenAPI spec: `GET /Items` and its parameters, `GET /Items/{itemId}/Collections`,
`POST /Collections`, `POST` and `DELETE /Collections/{collectionId}/Items`, and the `/AutoCollections/ExportConfiguration` response.

Not seen, assumed from earlier Jellyfin versions, and covered by the self-check:
- `GET /System/Info/Public` (server name and version)
- `GET /Library/VirtualFolders` (library list)
- `GET /Items?parentId=<collection id>` returning a collection's members
- That an admin API key can list items without a `user_id`
- That `ProductionYear` is present on series items
- The shape of Auto Collections `ExpressionCollections` entries (only the empty list has been seen)

Also not verified against a real server: the exact format Jellyfin 12.1 expects for `ids` on the write calls
(the app sends them comma-separated, as earlier versions accepted; a rejection is shown with Jellyfin's own error
text), and whether removing the last show from a collection deletes the collection. **Try the editor on a throwaway
collection first.**

The app was tested against a mock server that imitates these calls (including write failures and partial
failures), and the editor was exercised in a real browser against that mock. It has not been run against a real
Jellyfin 12.1 beyond the self-check.

## Config

See `config.example.yml`. Environment variables override the file.
