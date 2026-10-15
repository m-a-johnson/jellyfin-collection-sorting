# Decisions

- **Reading and writing are separate classes.** `JellyfinReader` only sends GET. `CollectionWriter` can only send
  `POST /Collections` (create, name only), `POST`/`DELETE /Collections/{32-hex id}/Items`, and `DELETE /Items/{32-hex id}`
  for one empty collection; method, path, name and every ID are validated before a request is built.
  The bulk `DELETE /Items?ids=...` (it deletes in a loop and stops partway) is never built. Tests assert both.
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
- **Per-item editing is one server call, validated up front.** `POST /api/items/{id}/collections` checks every named
  collection against a fresh read (missing or plugin-managed collections refuse the whole request before any write),
  skips no-ops, writes one collection at a time, stops at the first failure and reports what already went through,
  then re-reads to verify. Bulk add from the Uncollected tab reuses the existing apply endpoint.
- **Health is liveness only.** `/api/health` never calls Jellyfin, so a Jellyfin restart does not mark this container
  unhealthy. The version is baked in at build time (`APP_VERSION` build-arg: release tag, else `main-<sha>`).
- **Library chips only when there is more than one library of that kind**, to avoid noise for single-library setups.
- **Unsaved-changes warning** covers staged editor changes and an open per-show edit with a diff; checkbox selections on the
  Uncollected tab are not changes and do not warn.
- **Verified on a real 12.1.0 server:** the self-check calls (server info, library list, show years, collection
  members, per-show lookup), comma-separated `ids` on collection writes (one and several shows), and that removing a
  collection's last item leaves the collection. Collection-member and per-show lookups need `user_id` set; without
  it members came back empty and the per-show call returned 401.
- **Create collection:** sends `POST /Collections?name=...` and nothing else, then re-reads Jellyfin to confirm. Names
  that already exist (case-insensitive) or that an Auto Collections rule owns are refused.
- **Delete collection is empty-only, and why.** Jellyfin's collection controller (read from `master` on GitHub, not the
  12.1.0 tag) has no delete action. The only way is `DELETE /Items/{id}` (or the bulk form), which passes
  `DeleteFileLocation = true` and removes the item's files. For a collection that should be its own folder under
  Jellyfin's data directory, but I did not see `Folder.GetRecursiveChildren` or `BoxSet.GetDeletePaths`, so I could not
  prove what happens to members. The app therefore deletes only a collection that a fresh read and a second direct read
  both show empty, that is not plugin-managed, and whose exact name was typed. It needs `user_id` (without it member
  reads come back empty, which would make "empty" meaningless). Single-id route chosen over the bulk route because the
  bulk one deletes in a loop and returns early on the first failure.
- **Movies are a mode, not a mixed list.** Show and movie titles can collide, and a collection may hold both, so a
  Shows/Movies switch scopes every tab while staged changes and the selected collection carry across it. Library kinds
  come from the Jellyfin library type (tvshows, movies; mixed or untyped counts as both). Music and other types are ignored.
- **Verified on a real 12.1 server (Oct 2026):** `POST /Collections` with no `ids` creates an empty collection;
  `includeItemTypes=Movie` works (Movies mode); the per-show and Uncollected multi-collection writes work; deleting an
  empty collection with `DELETE /Items/{id}` works. Not verified: what that delete does to a collection that still has
  members, which is why the app never sends it for one.
- **Still unverified:** the shape of Auto Collections `ExpressionCollections` entries (only an empty list has been seen).
