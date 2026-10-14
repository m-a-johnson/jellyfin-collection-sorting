# Jellyfin Collection Manager

A small web app to see which collections each show is in, which shows are in none, and to move shows
in and out of a collection. Titles and years only. It reads from Jellyfin on demand and stores nothing.

What it can change: **only collections and their membership**: create an empty collection (`POST /Collections`)
and add or remove shows (`POST` / `DELETE /Collections/{id}/Items`). That never deletes a show or any media. It
cannot delete collections; do that in Jellyfin. The write client in `app/jellyfin.py` validates the method, the path
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
- **Shows / Movies switch** (top right, shown when you have a movie library): every tab works on one kind at a time.
  A collection can hold both, so the same collection stays selected when you switch, staged changes carry over, and one
  Apply can add or remove shows and movies together. Libraries are read from Jellyfin: TV libraries appear in Shows
  mode, movie libraries in Movies mode, and mixed-content libraries in both.
- **New collection…** (Editor tab): type a name and Create. It makes an empty collection, switches the editor to it,
  and confirms Jellyfin lists it. Duplicate names and names owned by Auto Collections rules are refused.
- **Shows**: every show with its collections as chips. Orange chips are collections owned by the Auto Collections plugin.
- **Uncollected**: shows (or movies) in no collection. By default shows that are only in plugin-managed collections are counted as uncollected (toggle on the page).
- **Collections**: each collection with its member shows.
- **Self-check**: tests the Jellyfin calls the app relies on.

## What was verified and what was not

Seen in a real Jellyfin 12.1.0 OpenAPI spec: `GET /Items` and its parameters, `GET /Items/{itemId}/Collections`,
`POST /Collections`, `POST` and `DELETE /Collections/{collectionId}/Items`, and the `/AutoCollections/ExportConfiguration` response.

Checked on a real Jellyfin 12.1.0 server (the author's):
- The **Self-check** passes: server info, library list, show years, collection members, and the per-show
  collection lookup agreeing with the member lists.
- **Set `user_id`** (an admin user) in the config. Without it, the collection-member lookup returned nothing and
  `GET /Items/{id}/Collections` returned HTTP 401, so every show looked uncollected.
- Adding and removing shows with comma-separated `ids` works, for one show and for several at once.
- Removing the last show from a collection leaves the empty collection in place.

Creating an empty collection with **New collection…** works on a real 12.1 server.

Not yet tried on a real server: **Movies mode** (`includeItemTypes=Movie`). Run the Self-check first; it samples movies too.

Still not seen: the shape of Auto Collections `ExpressionCollections` entries (only the empty list has been
seen). If you use expression-based collections, check that they show up as managed (orange) before editing them.

The app was also tested against a mock server that imitates these calls (including write failures and partial
failures), and the editor was exercised in a real browser against that mock. **Try the editor on a throwaway
collection first.**

## Config

See `config.example.yml`. Environment variables override the file.
