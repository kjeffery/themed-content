"""Round-trip the human-curated subset of `ride_metadata.json` through a
name-keyed CSV so you can edit it in any spreadsheet tool without ever
typing a UUID.

The CSV owns three things:
    Appeal             — comma-separated AppealTag rawValues
    BucketFit_<bucket> — six columns, one per AgeBucket; each cell is
                         empty or one of: skip, okay, great, mustDo
    Notes              — freeform string

`traits` (Disney-scraped) and the structural fields (minHeightInches,
mobility, etc.) are NOT in the CSV — those have other sources of truth and
the importer leaves them alone.

Usage:
    python3 themed-content/tools/curated_metadata.py scaffold
        Writes a fresh CSV from the current state of graph.json +
        ride_metadata.json. Preserves any curation already in the JSON so
        re-running scaffold doesn't blow away your edits.

    python3 themed-content/tools/curated_metadata.py import
        Reads the CSV and merges appeal/bucketFit/notes into the JSON,
        leaving traits and structural fields untouched. Reports any rows
        whose Attraction name doesn't match a graph node so you can fix
        the typo or update the graph.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / 'sources' / 'curated_metadata.csv'
DEFAULT_GRAPH = REPO_ROOT / 'graph.json'
DEFAULT_METADATA = REPO_ROOT / 'ride_metadata.json'

# Order matches RideMetadata.swift; six fixed columns rather than one
# packed cell, because spreadsheet apps sort and validate per-column much
# better than per-token. Empty cell == "no opinion" (resolver default).
BUCKETS = ['toddler', 'child', 'tween', 'teen', 'adult', 'senior']
BUCKET_COL = {b: f'BucketFit_{b}' for b in BUCKETS}
FIT_LEVELS = {'skip', 'okay', 'great', 'mustDo'}
APPEAL_VALUES = {
    'characterMeet', 'princess', 'nostalgia', 'icon',
    'photoOp', 'parade', 'fireworks', 'airConditioned',
}

CSV_FIELDS = (
    ['Attraction', 'EntityID', 'Park', 'Appeal']
    + [BUCKET_COL[b] for b in BUCKETS]
    + ['Notes']
)

# Header comment lines written by `scaffold`. Each appears in the CSV as a
# row whose first cell starts with '#' — the importer skips those rows, and
# spreadsheet apps display them as plain rows the user can ignore. Re-emitted
# verbatim every scaffold run so the documentation stays current.
HEADER_COMMENTS = [
    '# Human-editable source of truth for ride_metadata.json curated fields.',
    "# Rows whose first cell starts with '#' are comments — ignored on import.",
    '#',
    '# WORKFLOW',
    '#   1. Edit any cell. Sort/filter by Park to bulk-curate a land.',
    '#   2. Save the file (CSV format, not xlsx).',
    '#   3. From repo root:',
    '#        python3 themed-content/tools/curated_metadata.py import',
    '#      Use --dry-run for a preview. Vocabulary errors abort the write.',
    '#',
    '# VOCABULARY',
    '#   Appeal     comma-separated, any of:',
    '#              characterMeet, princess, nostalgia, icon,',
    '#              photoOp, parade, fireworks, airConditioned',
    '#   BucketFit_*  empty (= no opinion) OR one of: skip, okay, great, mustDo',
    '#   Notes      freeform; sparingly, for facts the typed fields miss',
    '#',
    '# TIPS',
    "#   - Tag the ~15 park-defining attractions with 'icon' first — biggest",
    '#     win for the suggester before the rest of the data lands.',
    '#   - BucketFit can stay sparse; resolver defaults absent buckets to okay.',
    "#   - Re-running 'scaffold' preserves your edits and adds rows for any",
    '#     newly-graphed attractions.',
]


def normalize_name(s: str) -> str:
    """Looser matcher than the Disney-import script; we only key on names
    the user types, which are usually the canonical graph-node spellings."""
    s = s.lower().replace('"', '').replace('‘', '').replace('’', '').replace("'", '')
    s = s.replace('&', 'and')
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def load_named_attractions(graph: dict) -> list[dict]:
    """Returns every attraction-or-show graph node with a themeparks.wiki
    entity ID, sorted by park then name. Restaurants are excluded — they
    don't carry the kind of party-fit metadata the curated CSV is for."""
    nodes = [
        n for n in graph['nodes']
        if n.get('themeParksEntityID')
        and n.get('kind') in {'attraction', 'show'}
        and n.get('name')
    ]
    return sorted(nodes, key=lambda n: (n.get('park') or '', n['name'].lower()))


def scaffold(args) -> int:
    graph = json.loads(args.graph.read_text())
    metadata = json.loads(args.metadata.read_text())
    entries: dict = metadata.get('entries', {})

    rows: list[dict] = []
    for node in load_named_attractions(graph):
        eid = node['themeParksEntityID'].lower()
        existing = entries.get(eid) or entries.get(eid.upper()) or {}
        appeal = existing.get('appeal') or []
        bucketFit = existing.get('bucketFit') or {}
        row = {
            'Attraction': node['name'],
            'EntityID': eid,
            'Park': node.get('park') or '',
            'Appeal': ', '.join(appeal),
            'Notes': existing.get('notes') or '',
        }
        for b in BUCKETS:
            row[BUCKET_COL[b]] = bucketFit.get(b, '')
        rows.append(row)

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open('w', newline='') as f:
        # Write comment lines as quoted single-cell rows. Quoting keeps any
        # embedded commas (and there are several in the vocabulary list)
        # contained in the Attraction column so spreadsheet apps render the
        # docs as a tidy column down the left edge instead of splitting
        # them across columns.
        for line in HEADER_COMMENTS:
            quoted = '"' + line.replace('"', '""') + '"'
            f.write(quoted + ',' * (len(CSV_FIELDS) - 1) + '\n')
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    print(f'Wrote {len(rows)} attractions to {args.csv}')
    pre_curated = sum(1 for r in rows if r['Appeal'] or r['Notes']
                      or any(r[BUCKET_COL[b]] for b in BUCKETS))
    print(f'  {pre_curated} entries already had curated values (preserved).')
    print(f'  {len(rows) - pre_curated} entries are blank and ready to fill in.')
    return 0


def import_curated(args) -> int:
    graph = json.loads(args.graph.read_text())
    metadata = json.loads(args.metadata.read_text())
    entries: dict = metadata.setdefault('entries', {})

    # Build a name → entity ID map, lowercased and normalized. We also
    # accept a literal EntityID column on the CSV for cases where a user
    # cares about exact id over name (helps with attractions whose names
    # have ambiguous casing or punctuation).
    name_to_eid: dict[str, str] = {}
    for node in load_named_attractions(graph):
        name_to_eid[normalize_name(node['name'])] = node['themeParksEntityID'].lower()

    matched, unmatched, vocab_errors = 0, [], []
    fields_changed = 0

    with args.csv.open(newline='') as f:
        # Drop comment lines BEFORE handing to DictReader so the actual
        # header row becomes fieldnames. Comments may be either bare
        # ('# foo,,,...') or quoted ('"# foo",,,...') — the scaffold writes
        # the quoted form so embedded commas stay in column 1, but we
        # accept both for forgiving hand-editing.
        non_comment = (
            line for line in f
            if not line.lstrip().startswith(('#', '"#'))
        )
        for row in csv.DictReader(non_comment):
            name = (row.get('Attraction') or '').strip()
            # Skip blank spacer rows so users can break the CSV up visually.
            if not name:
                continue
            csv_eid = (row.get('EntityID') or '').strip().lower()
            if csv_eid:
                eid = csv_eid
            else:
                eid = name_to_eid.get(normalize_name(name), '')
            if not eid:
                unmatched.append(name)
                continue

            appeal = [a.strip() for a in (row.get('Appeal') or '').split(',') if a.strip()]
            bad_appeal = [a for a in appeal if a not in APPEAL_VALUES]
            for a in bad_appeal:
                vocab_errors.append((name, f'unknown Appeal value {a!r}'))
            appeal = [a for a in appeal if a in APPEAL_VALUES]

            bucketFit: dict[str, str] = {}
            for b in BUCKETS:
                cell = (row.get(BUCKET_COL[b]) or '').strip()
                if not cell:
                    continue
                if cell not in FIT_LEVELS:
                    vocab_errors.append(
                        (name, f'BucketFit_{b}={cell!r} not in {sorted(FIT_LEVELS)}')
                    )
                    continue
                bucketFit[b] = cell

            notes = (row.get('Notes') or '').strip()

            existing = entries.get(eid, {})
            # Preserve every field except the three we authoritatively own.
            merged = {k: v for k, v in existing.items()
                      if k not in ('appeal', 'bucketFit', 'notes')}
            if appeal:
                merged['appeal'] = appeal
            if bucketFit:
                merged['bucketFit'] = bucketFit
            if notes:
                merged['notes'] = notes
            if not merged:
                # Don't accrete empty stubs.
                entries.pop(eid, None)
                continue
            if merged != existing:
                fields_changed += 1
            entries[eid] = merged
            matched += 1

    print(f'CSV rows matched:           {matched}')
    print(f'  metadata entries changed: {fields_changed}')
    print(f'unmatched rows:             {len(unmatched)}')
    print(f'vocabulary errors:          {len(vocab_errors)}')
    for name in unmatched:
        print(f'  {name!r} — no graph node matches')
    for name, msg in vocab_errors:
        print(f'  {name!r}: {msg}')

    if vocab_errors:
        print('\nFix the vocabulary errors and re-run; nothing was written.')
        return 1
    if args.dry_run:
        print('\n[dry-run] Not writing metadata file.')
        return 0

    args.metadata.write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'\nWrote {args.metadata}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('scaffold', help='Generate the editable CSV.')
    s.add_argument('--csv', type=Path, default=DEFAULT_CSV)
    s.add_argument('--graph', type=Path, default=DEFAULT_GRAPH)
    s.add_argument('--metadata', type=Path, default=DEFAULT_METADATA)
    s.set_defaults(func=scaffold)

    i = sub.add_parser('import', help='Merge edits from the CSV into the JSON.')
    i.add_argument('--csv', type=Path, default=DEFAULT_CSV)
    i.add_argument('--graph', type=Path, default=DEFAULT_GRAPH)
    i.add_argument('--metadata', type=Path, default=DEFAULT_METADATA)
    i.add_argument('--dry-run', action='store_true')
    i.set_defaults(func=import_curated)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
