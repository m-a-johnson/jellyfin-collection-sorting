# Decisions

- **Read-only client first.** `app/jellyfin.py` only sends GET. Editing will use only
  `POST`/`DELETE /Collections/{id}/Items`. `DELETE /Items` deletes media from the filesystem and must never be reachable.
- **Membership map built by inverting collection member lists.** The editor needs those lists anyway.
  `GET /Items/{id}/Collections` is used only as a cross-check in the self-check.
- **Auto Collections plugin rules are treated as read-only** (matched by collection name from `/AutoCollections/ExportConfiguration`),
  because the plugin may re-add or remove items on its next run.
- **config.yml with environment overrides**, API key as a quoted string (an unquoted numeric key is rejected).
- **No database, no image fetching.** Jellyfin is the source of truth; titles and years only.
- **Unverified against a real 12.1 server:** `/System/Info/Public`, `/Library/VirtualFolders`,
  `/Items?parentId=<collection>` member lookup, admin key without `userId`, `ProductionYear` on series.
  The Self-check tab covers these.
