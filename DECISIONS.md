# Decisions

- **Reading and writing are separate classes.** `JellyfinReader` only sends GET. `CollectionWriter` can only send
  `POST`/`DELETE /Collections/{32-hex id}/Items`; method, path and every ID are validated before a request is built.
  `DELETE /Items` deletes media from the filesystem and must never be reachable. Tests assert this.
- **Staged edits with a review step.** The two-list editor only changes the screen until Apply is confirmed.
  Reason: collections drive ErsatzTV channel lineups, so a misclick should be cheap to undo.
- **The server re-validates every apply against a fresh read.** Adds of existing members and removals of non-members
  are skipped and reported, not sent. Afterwards the collection is re-read and any mismatch is shown.
- **Partial failures are reported, not hidden.** Adds run before removes; if Jellyfin refuses part-way, the error says
  how much was done and the page reloads the real state.
- **No login, so writes need a custom header** (`X-Requested-With: jf-collections`): browsers cannot attach one to a
  cross-site request without a CORS preflight, which the app never allows.
- **`read_only` switch** to disable all writes for deployments that only want the views.
- **Selection updates rows in place.** Rebuilding the list on every click made double-click unreliable in a real browser.
- **Membership map built by inverting collection member lists.** The editor needs those lists anyway.
  `GET /Items/{id}/Collections` is used only as a cross-check in the self-check.
- **Auto Collections plugin rules are treated as read-only** (matched by collection name from `/AutoCollections/ExportConfiguration`),
  because the plugin may re-add or remove items on its next run.
- **config.yml with environment overrides**, API key as a quoted string (an unquoted numeric key is rejected).
- **No database, no image fetching.** Jellyfin is the source of truth; titles and years only.
- **Verified on a real 12.1.0 server:** the self-check calls (server info, library list, show years, collection
  members, per-show lookup), comma-separated `ids` on collection writes (one and several shows), and that removing a
  collection's last item leaves the collection. Collection-member and per-show lookups need `user_id` set; without
  it members came back empty and the per-show call returned 401.
- **Still unverified:** the shape of Auto Collections `ExpressionCollections` entries (only an empty list has been seen).
