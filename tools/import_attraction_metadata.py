"""Import the disneyland.disney.go.com attractions CSV into ride_metadata.json.

Reads a CSV with columns:
    Attraction, Park, Ride Characteristics, Description

For each row whose `Attraction` name matches a graph node by normalized name,
maps the comma-separated `Ride Characteristics` to the project's `RideTrait`
and `AppealTag` enums and writes them into the matching entity's metadata
entry. All other fields on existing entries (minHeightInches, heightNote,
hasSingleRider, mobility, etc.) are preserved untouched — this script is
intentionally narrow to the two fields the CSV authoritatively populates.

Run from the repo root with no arguments to use the canonical paths:
    python3 themed-content/tools/import_attraction_metadata.py

Or override any of the paths:
    python3 themed-content/tools/import_attraction_metadata.py \\
        --csv themed-content/sources/disneyland_attractions.csv \\
        --graph themed-content/graph.json \\
        --metadata themed-content/ride_metadata.json

Reports unmatched CSV rows (likely seasonal overlays / photo-ops not in the
graph) and any tokens it didn't recognize so the vocabulary stays in sync.
The Description column is intentionally NOT imported — the schema's `notes`
field is for short non-obvious facts, not Disney's marketing copy (copyright
plus the field would crowd the detail sheet).
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

# CSV token → enum rawValue (case-by-case mapping from Disney's filter
# vocabulary to the app's RideTrait / AppealTag cases). Tokens not listed
# here are reported as unrecognized so we know to extend the schema or
# update the script when Disney adds a new filter.
TRAIT_MAP = {
    'Big Drops':   'bigDrops',
    'Dark':        'dark',
    'Loud':        'loud',
    'Scary':       'scary',
    'Slow Rides':  'slow',
    'Small Drops': 'smallDrops',
    'Spinning':    'spinning',
    'Thrill Rides': 'thrill',
    'Water Rides': 'water',
}
APPEAL_MAP = {
    'Character Experiences': 'characterMeet',
    'Fireworks':             'fireworks',
    'Parades':               'parade',
}
# Tokens we deliberately ignore. Live Entertainment / Stage Shows overlap
# with the existing `show` entity type from themeparks.wiki and would just
# duplicate that signal as appeal data.
IGNORED_TOKENS = {'Live Entertainment', 'Stage Shows'}


def normalize(s: str) -> str:
    """Loose name-matching: lowercase, strip apostrophes / punctuation,
    collapse whitespace, drop the 'inspired by Walt Disney's …' suffix that
    appears on the Adventureland Treehouse name."""
    s = s.lower()
    s = s.replace('"', '').replace('‘', '').replace('’', '')
    s = s.replace("'", '')
    s = s.replace('&', 'and')
    s = re.sub(r'\binspired by walt disney.s\b.*$', '', s)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def build_graph_index(graph_path: Path) -> dict:
    """Returns {normalized_name: [(entityID, kind, original_name), ...]}.

    Restricted to entity kinds we'd plausibly attach metadata to so that a
    name collision between an attraction and a same-named restroom can't
    misroute the import.
    """
    g = json.loads(graph_path.read_text())
    index: dict[str, list] = {}
    for n in g['nodes']:
        eid = n.get('themeParksEntityID')
        if not eid or n.get('kind') not in {'attraction', 'show', 'restaurant'}:
            continue
        index.setdefault(normalize(n['name']), []).append(
            (eid.lower(), n['kind'], n['name'])
        )
    return index


def parse_characteristics(raw: str) -> tuple[list[str], list[str], list[str]]:
    """Returns (trait_rawvalues, appeal_rawvalues, unrecognized_tokens)."""
    if not raw:
        return [], [], []
    traits, appeal, unknown = [], [], []
    for token in (t.strip() for t in raw.split(',')):
        if not token:
            continue
        if token in TRAIT_MAP:
            traits.append(TRAIT_MAP[token])
        elif token in APPEAL_MAP:
            appeal.append(APPEAL_MAP[token])
        elif token not in IGNORED_TOKENS:
            unknown.append(token)
    # Dedup while preserving first-seen order; the JSON readers tolerate
    # either order but keeping it stable makes diffs cleaner.
    return list(dict.fromkeys(traits)), list(dict.fromkeys(appeal)), unknown


REPO_ROOT = Path(__file__).resolve().parent.parent  # themed-content/
DEFAULT_CSV = REPO_ROOT / 'sources' / 'disneyland_attractions.csv'
DEFAULT_GRAPH = REPO_ROOT / 'graph.json'
DEFAULT_METADATA = REPO_ROOT / 'ride_metadata.json'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', type=Path, default=DEFAULT_CSV)
    ap.add_argument('--graph', type=Path, default=DEFAULT_GRAPH)
    ap.add_argument('--metadata', type=Path, default=DEFAULT_METADATA)
    ap.add_argument(
        '--dry-run',
        action='store_true',
        help='Report what would change without writing the metadata file.',
    )
    args = ap.parse_args()

    graph_index = build_graph_index(args.graph)
    metadata = json.loads(args.metadata.read_text())
    entries: dict = metadata.setdefault('entries', {})

    matched, unmatched, ambiguous = [], [], []
    unknown_token_counter = Counter()
    fields_changed = 0

    rows = list(csv.DictReader(args.csv.open()))
    for r in rows:
        name = r['Attraction']
        hits = graph_index.get(normalize(name), [])
        if len(hits) > 1:
            ambiguous.append((name, hits))
            continue
        if not hits:
            unmatched.append(name)
            continue

        eid, _, _ = hits[0]
        traits, appeal, unknown = parse_characteristics(r.get('Ride Characteristics') or '')
        for t in unknown:
            unknown_token_counter[t] += 1

        existing = entries.get(eid, {})
        # Preserve every field except `traits` / `appeal`; those are the
        # only two the CSV authoritatively replaces. Removing them from the
        # incoming record means "no opinion" (which renders as no chips)
        # rather than empty-array clutter in the JSON.
        merged = {k: v for k, v in existing.items() if k not in ('traits', 'appeal')}
        if traits:
            merged['traits'] = traits
        if appeal:
            merged['appeal'] = appeal

        # Skip writing entries that are entirely empty after merging — keeps
        # the file from accruing zero-information stubs for matched-but-
        # untagged attractions.
        if not merged:
            continue
        if merged != existing:
            fields_changed += 1
        entries[eid] = merged
        matched.append((name, eid, traits, appeal))

    # --- Report ---
    print(f'CSV rows: {len(rows)}')
    print(f'  matched cleanly:        {len(matched)}')
    print(f'  no graph match:         {len(unmatched)}')
    print(f'  ambiguous (>1 hit):     {len(ambiguous)}')
    print(f'  metadata entries with field changes: {fields_changed}')
    if unknown_token_counter:
        print('\nUnrecognized characteristic tokens (extend TRAIT_MAP/APPEAL_MAP if real):')
        for tok, n in unknown_token_counter.most_common():
            print(f'  {n:3d}  {tok!r}')
    if ambiguous:
        print('\nAmbiguous matches (resolve by hand-editing the script or graph):')
        for name, hits in ambiguous:
            for h in hits:
                print(f'  {name!r}  ->  {h[2]!r} ({h[1]})')
    if unmatched:
        print('\nUnmatched CSV rows (mostly seasonal overlays / photo-ops; '
              'review and either add to graph or ignore):')
        for u in unmatched:
            print(f'  - {u}')

    if args.dry_run:
        print('\n[dry-run] Not writing metadata file.')
        return 0

    args.metadata.write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'\nWrote {args.metadata}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
