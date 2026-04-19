# themed-content

Remote configuration for the [themed](https://github.com/kjeffery/themed) iOS app. Served via GitHub Pages.

Contents here drive the app's routing graph, event closure footprints, and routing weight coefficients.

## Layout

```
/                         (served from the root of `main`)
  manifest.json           — index of all live content files with hashes
  graph-<sha256>.json     — routing graph: nodes + edges
  closures-<sha256>.json  — event-linked closure associations
  weights-<sha256>.json   — routing cost-function coefficients
  .nojekyll               — disables Jekyll processing on GH Pages
```

Content files are **content-addressed**: their filenames include a SHA-256 prefix of the file bytes. Once a hash is in `manifest.json`, that file's contents never change. New versions land under new filenames; old ones can be pruned once no clients reference them.

## manifest.json

```json
{
  "manifestVersion": 1,
  "generatedAt": "2026-04-19T00:00:00Z",
  "files": [
    { "role": "graph",    "path": "graph-abc123.json",    "sha256": "abc123...", "bytes": 12345 },
    { "role": "closures", "path": "closures-def456.json", "sha256": "def456...", "bytes":   678 },
    { "role": "weights",  "path": "weights-789abc.json",  "sha256": "789abc...", "bytes":    90 }
  ]
}
```

The app fetches `manifest.json` on launch, diffs the `files` array against its last-known cache, and downloads any entries whose hash changed.

## Why the hashed filenames

GitHub Pages serves via a Fastly CDN with `Cache-Control: max-age=600`. An in-place edit to `closures.json` can sit stale at edge PoPs for up to ~10 minutes. Hashed filenames sidestep this: once a URL is published, its content is immutable, so CDN caching becomes free performance instead of a liability. Only `manifest.json` itself is subject to the 10-minute window, and that staleness is acceptable for a ~1 KB file fetched once per launch.

## Publishing a change

1. Author the change in the app's debug editor (iPad).
2. Export the updated JSON via the share sheet.
3. Drop the exported file into this repo under a new `<role>-<sha>.json` name; update `manifest.json` to point at it.
4. Commit and push. Within ~10 minutes the manifest goes live; clients will pick up the new content on their next launch.

A helper script to automate step 3 (hash, rename, update manifest) will land here once the format stabilizes.
