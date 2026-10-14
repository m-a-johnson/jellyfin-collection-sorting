# Jellyfin Collection Manager (step 1: read-only)

A small web app that shows which collections each show is in, and which shows are in no collection.
Titles and years only. It reads from Jellyfin on demand and stores nothing.

Step 1 cannot change anything: the Jellyfin client in `app/jellyfin.py` only sends GET requests.
Editing (move shows in and out of a collection) comes next and will use only
`POST` / `DELETE /Collections/{id}/Items`, never `DELETE /Items` (which deletes media files).

## Run

1. Start the container once. It writes a template to `/config/config.yml` and exits.
2. Edit that file: Jellyfin URL and an admin API key (keep the quotes), `chmod 600` it.
3. Start again and open `http://<host>:8099`.
4. Open the **Self-check** tab and run it before trusting anything else.

```bash
docker compose up -d --build
docker logs jf-collections
```

There is no login in the app itself. Put it behind your reverse proxy's authentication.

## Views

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

The app was tested against a mock server that imitates these calls, not against a real Jellyfin 12.1.

## Config

See `config.example.yml`. Environment variables override the file.
