"""Round-trip the human-curated subset of `ride_metadata.json` through a
name-keyed CSV so you can edit it in any spreadsheet tool without ever
typing a UUID.

The CSV owns eight things:
    Appeal              — comma-separated AppealTag rawValues
    BucketFit_<bucket>  — six columns, one per AgeBucket; each cell is
                          empty or one of: skip, okay, great, mustDo
    Popularity          — empty OR one of: low, medium, high, iconic
                          Drives the suggester ranking when live wait-
                          time data is missing (pre-park-open planning).
    FeaturedCharacters  — comma-separated character names that appear
                          on this attraction (e.g. "Mickey, Minnie").
                          Future roster picker work uses this; scaffold
                          seeds it from a substring match on the ride's
                          name, which the user reviews + edits.
    Mobility            — comma-separated MobilityAccess flags (e.g.
                          "mayRemainInWheelchair, mustTransferFromWheelchair").
                          Drives wheelchair-conflict warnings in the app.
                          Curate from Disney's Guide for Guests with
                          Disabilities — see docs/accessibility_curation.md.
    ServiceAnimals      — empty OR one of: permitted_with_caution,
                          not_permitted. Empty = "permitted normally"
                          (the default, no warning fires).
    SensoryHazards      — comma-separated SensoryHazard rawValues (e.g.
                          "strobe, loudSounds, darkness"). Drives the
                          accessibility soft-warning chips.
    Notes               — freeform string

`traits` (Disney-scraped from Ride Characteristics) and the structural
fields (minHeightInches, hasSingleRider, etc.) are NOT in the CSV —
those have other sources of truth and the importer leaves them alone.

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
POPULARITY_VALUES = ['low', 'medium', 'high', 'iconic']

# Disney's 8-way mobility taxonomy — mirrors MobilityAccess fields in
# RideMetadata.swift. CSV cells carry the rawValues of *true* flags only;
# omitted flags decode to nil (absent / uncurated) on the Swift side.
MOBILITY_VALUES = {
    'mayRemainInWheelchair',
    'mustBeAmbulatory',
    'mustTransferFromWheelchair',
    'mustTransferFromECVToWheelchair',
    'wheelchairAccessVehicle',
    'transferDeviceAvailable',
    'designatedTransferAreas',
    'transferAccessVehicle',
}

# ServiceAnimalPolicy in RideMetadata.swift. Empty CSV cell = "permitted
# normally" (no special handling); the Swift side treats that as the
# default and emits no warning chip.
SERVICE_ANIMAL_VALUES = {'permitted_with_caution', 'not_permitted'}

# SensoryHazard rawValues — mirrors the Swift enum in RideMetadata.swift.
# Curate honestly: omit when uncurated, write the explicit list when
# checked. See docs/accessibility_curation.md for source pointers.
SENSORY_HAZARD_VALUES = {
    'strobe', 'loudSounds', 'darkness', 'drops', 'smokeOrFog',
}

# Canonical character names paired with the substring patterns the
# scaffold should look for in attraction / show names. Each entry is
# (canonical, [pattern, ...]); a match on *any* pattern emits the
# canonical. The short-form patterns ("Mickey" matches "Mickey's
# Toontown" / "Mickey & Minnie's Runaway Railway") matter because
# Disney rarely uses full character names in attraction signage.
#
# The guess is a starting point, never authoritative — the scaffold
# only seeds blank rows and re-runs preserve curated values, so
# review-and-tweak is the expected workflow.
KNOWN_CHARACTERS: list[tuple[str, list[str]]] = [
    # Mickey & Friends
    ('Mickey Mouse', ['Mickey Mouse', 'Mickey']),
    ('Minnie Mouse', ['Minnie Mouse', 'Minnie']),
    ('Donald Duck', ['Donald Duck', 'Donald']),
    ('Daisy Duck', ['Daisy Duck', 'Daisy']),
    ('Goofy', ['Goofy']),
    ('Pluto', ['Pluto']),
    ('Chip', ['Chip']),
    ('Dale', ['Dale']),
    # Princesses
    ('Cinderella', ['Cinderella']),
    ('Aurora', ['Aurora', 'Sleeping Beauty']),
    ('Snow White', ['Snow White']),
    ('Ariel', ['Ariel', 'Little Mermaid']),
    ('Belle', ["Belle's", 'Belle ', 'Beauty and the Beast']),
    ('Tiana', ['Tiana']),
    ('Anna', ['Anna ', "Anna's"]),
    ('Elsa', ['Elsa']),
    ('Rapunzel', ['Rapunzel', 'Tangled']),
    ('Mulan', ['Mulan']),
    ('Pocahontas', ['Pocahontas']),
    ('Moana', ['Moana']),
    ('Merida', ['Merida', 'Brave']),
    ('Jasmine', ['Jasmine']),
    ('Aladdin', ['Aladdin']),
    # Pixar
    ('Buzz Lightyear', ['Buzz Lightyear', 'Buzz']),
    ('Woody', ['Woody']),
    ('Jessie', ['Jessie']),
    ('Mr. Incredible', ['Mr. Incredible', 'Incredibles']),
    ('Elastigirl', ['Elastigirl']),
    # Star Wars
    ('Darth Vader', ['Darth Vader', 'Vader']),
    ('Luke Skywalker', ['Luke Skywalker', 'Luke']),
    ('Kylo Ren', ['Kylo']),
    ('Rey', ['Rey']),
    ('Chewbacca', ['Chewbacca', 'Chewie']),
    ('BB-8', ['BB-8']),
    # Marvel
    ('Spider-Man', ['Spider-Man', 'Spiderman']),
    ('Iron Man', ['Iron Man']),
    ('Captain America', ['Captain America']),
    ('Black Widow', ['Black Widow']),
    ('Thor', ['Thor']),
    # Misc Disney
    ('Peter Pan', ['Peter Pan']),
    ('Tinker Bell', ['Tinker Bell', 'Tinkerbell']),
    ('Alice', ['Alice in Wonderland', "Alice's"]),
    ('Mad Hatter', ['Mad Hatter', "Mad T Party"]),
    ('Pinocchio', ['Pinocchio']),
    ('Stitch', ['Stitch']),
    ('Lilo', ['Lilo']),
    ('Winnie the Pooh', ['Winnie the Pooh', 'Pooh']),
    ('Tigger', ['Tigger']),
    ('Eeyore', ['Eeyore']),
    ('Piglet', ['Piglet']),
    ('Roger Rabbit', ['Roger Rabbit']),
    ('Mr. Toad', ['Mr. Toad']),
    ('Dumbo', ['Dumbo']),
    ('Indiana Jones', ['Indiana Jones']),
]

CSV_FIELDS = (
    ['Attraction', 'EntityID', 'Park', 'Appeal']
    + [BUCKET_COL[b] for b in BUCKETS]
    + [
        'Popularity', 'FeaturedCharacters',
        # Accessibility cluster — kept adjacent in the CSV so a curator
        # filling these in for one ride sees all three together rather
        # than scrolling through the layout. Order matches the natural
        # flow of how Disney's accessibility page is structured (mobility
        # first, then service animals, then sensory advisories).
        'Mobility', 'ServiceAnimals', 'SensoryHazards',
        'Notes',
    ]
)


def guess_characters(attraction_name: str) -> list[str]:
    """Substring-match each known character's patterns against the
    attraction name (case-insensitive) and emit the canonical name on
    any hit. Returns matches in source order so multi-character
    attractions emit deterministically ("Mickey Mouse, Minnie Mouse")
    — easier for the user to review than an order that drifts with
    implementation details."""
    haystack = attraction_name.lower()
    matched: list[str] = []
    for canonical, patterns in KNOWN_CHARACTERS:
        if any(p.lower() in haystack for p in patterns):
            matched.append(canonical)
    return matched

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
    '#   Appeal              comma-separated, any of:',
    '#                       characterMeet, princess, nostalgia, icon,',
    '#                       photoOp, parade, fireworks, airConditioned',
    '#   BucketFit_*         empty (= no opinion) OR one of:',
    '#                       skip, okay, great, mustDo',
    '#   Popularity          empty OR one of: low, medium, high, iconic',
    '#                       Drives the suggester ranking when live wait-',
    '#                       time data is missing (closed-hour planning).',
    '#   FeaturedCharacters  comma-separated character names (e.g.',
    '#                       "Mickey Mouse, Minnie Mouse"). Scaffold',
    '#                       guesses these from the ride name; review.',
    '#   Mobility            comma-separated MobilityAccess flags. Any of:',
    '#                       mayRemainInWheelchair, mustBeAmbulatory,',
    '#                       mustTransferFromWheelchair,',
    '#                       mustTransferFromECVToWheelchair,',
    '#                       wheelchairAccessVehicle, transferDeviceAvailable,',
    '#                       designatedTransferAreas, transferAccessVehicle.',
    '#                       Empty = uncurated; the app emits no wheelchair',
    '#                       warning. Curate from Disney\'s Guide for Guests',
    '#                       with Disabilities — see docs/accessibility_curation.md.',
    '#   ServiceAnimals      empty (= permitted normally) OR one of:',
    '#                       permitted_with_caution, not_permitted.',
    '#                       `not_permitted` hard-blocks for service-animal',
    '#                       users; `permitted_with_caution` shows a soft',
    '#                       warning.',
    '#   SensoryHazards      comma-separated SensoryHazard rawValues. Any of:',
    '#                       strobe, loudSounds, darkness, drops, smokeOrFog.',
    '#                       Empty = uncurated (no warning). Write the',
    '#                       explicit list when you\'ve verified against',
    '#                       official sources, even if no hazards apply.',
    '#   Notes               freeform; sparingly, for facts the typed',
    '#                       fields miss',
    '#',
    '# TIPS',
    "#   - Tag the ~15 park-defining attractions with 'icon' Appeal AND",
    "#     'iconic' Popularity first — biggest win for the suggester before",
    '#     the rest of the data lands.',
    '#   - BucketFit can stay sparse; resolver defaults absent buckets to okay.',
    "#   - Re-running 'scaffold' preserves your edits and adds rows for any",
    '#     newly-graphed attractions. Character guesses are only seeded on',
    '#     blank rows so re-runs never overwrite reviewed values.',
    '#   - Accessibility data is high-stakes; the in-app disclaimer assumes',
    '#     curated entries are correct. Under-promise: omit a field when',
    "#     you're unsure rather than guess (uncurated = no warning fires,",
    "#     which lets the user's own judgement carry).",
]


def normalize_name(s: str) -> str:
    """Looser matcher than the Disney-import script; we only key on names
    the user types, which are usually the canonical graph-node spellings."""
    s = s.lower().replace('"', '').replace('‘', '').replace('’', '').replace("'", '')
    s = s.replace('&', 'and')
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def load_named_attractions(graph: dict) -> list[dict]:
    """Returns every attraction-or-show graph POI with a themeparks.wiki
    entity ID, sorted by park then name. Restaurants are excluded — they
    don't carry the kind of party-fit metadata the curated CSV is for.

    Accepts both the v2 schema (`pois`) and the legacy one (`nodes`); the
    array was renamed when the node graph became a POI catalog."""
    pois = graph.get('pois')
    if pois is None:
        pois = graph.get('nodes')
    if pois is None:
        raise SystemExit(
            "graph file has no 'pois' (or legacy 'nodes') array — is this a graph.json?"
        )
    named = [
        n for n in pois
        if n.get('themeParksEntityID')
        and n.get('kind') in {'attraction', 'show'}
        and n.get('name')
    ]
    return sorted(named, key=lambda n: (n.get('park') or '', n['name'].lower()))


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
        # Popularity: preserve any curated value; otherwise seed `iconic`
        # when Appeal already contains `icon` so the new column starts
        # half-populated for park-defining attractions.
        popularity = existing.get('popularity')
        if not popularity and 'icon' in appeal:
            popularity = 'iconic'
        # Featured characters: preserve any curated list; otherwise
        # seed via name match. Only seed when blank so re-running
        # scaffold after a curation pass doesn't overwrite the user's
        # reviewed picks with a fresh guess.
        characters = existing.get('featuredCharacters') or []
        if not characters:
            characters = guess_characters(node['name'])
        # Accessibility fields — flatten back from JSON shape into the
        # CSV's comma-separated rawValue format. `mobility` is a dict
        # of boolean flags; we emit the keys whose value is true. The
        # other two are scalar/set already so they map directly.
        mobility_obj = existing.get('mobility') or {}
        mobility_flags = [k for k, v in mobility_obj.items() if v is True]
        service_animals = existing.get('serviceAnimals') or ''
        sensory_hazards = existing.get('sensoryHazards') or []
        row = {
            'Attraction': node['name'],
            'EntityID': eid,
            'Park': node.get('park') or '',
            'Appeal': ', '.join(appeal),
            'Popularity': popularity or '',
            'FeaturedCharacters': ', '.join(characters),
            'Mobility': ', '.join(mobility_flags),
            'ServiceAnimals': service_animals,
            'SensoryHazards': ', '.join(sensory_hazards),
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
    pre_curated = sum(
        1 for r in rows if r['Appeal'] or r['Notes'] or r['Popularity']
        or any(r[BUCKET_COL[b]] for b in BUCKETS)
    )
    accessibility_curated = sum(
        1 for r in rows
        if r['Mobility'] or r['ServiceAnimals'] or r['SensoryHazards']
    )
    guessed_chars = sum(1 for r in rows if r['FeaturedCharacters'])
    print(f'  {pre_curated} entries already had curated values (preserved).')
    print(f'  {len(rows) - pre_curated} entries are blank and ready to fill in.')
    print(f'  {accessibility_curated} entries have accessibility fields set.')
    print(f'  {guessed_chars} rows have a FeaturedCharacters guess to review.')
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

            popularity = (row.get('Popularity') or '').strip()
            if popularity and popularity not in POPULARITY_VALUES:
                vocab_errors.append(
                    (name, f'Popularity={popularity!r} not in {POPULARITY_VALUES}')
                )
                popularity = ''

            # Featured characters: same comma-split shape as Appeal but no
            # closed vocabulary — the iOS side treats these as freeform
            # strings. Trim, drop blanks, de-dup while preserving the
            # author's order so review-friendly groupings ("Mickey Mouse,
            # Minnie Mouse") stay together.
            char_raw = [c.strip() for c in (row.get('FeaturedCharacters') or '').split(',')]
            seen: set[str] = set()
            characters: list[str] = []
            for c in char_raw:
                if not c:
                    continue
                key = c.lower()
                if key in seen:
                    continue
                seen.add(key)
                characters.append(c)

            # Mobility: comma-split rawValues, validated against
            # MOBILITY_VALUES, written back to JSON as
            # `{"<flag>": true, ...}` (Swift decodes missing keys as
            # nil = absent flag). Empty cell → no `mobility` key emitted
            # at all (uncurated).
            mobility_raw = [
                m.strip() for m in (row.get('Mobility') or '').split(',')
            ]
            mobility_flags: list[str] = []
            for m in mobility_raw:
                if not m:
                    continue
                if m not in MOBILITY_VALUES:
                    vocab_errors.append(
                        (name, f'unknown Mobility flag {m!r}')
                    )
                    continue
                if m not in mobility_flags:
                    mobility_flags.append(m)

            # ServiceAnimals: scalar enum. Empty = no key emitted
            # (default behavior in the app: no warning).
            service_animals = (row.get('ServiceAnimals') or '').strip()
            if service_animals and service_animals not in SERVICE_ANIMAL_VALUES:
                vocab_errors.append(
                    (name, f'ServiceAnimals={service_animals!r} not in '
                          f'{sorted(SERVICE_ANIMAL_VALUES)}')
                )
                service_animals = ''

            # SensoryHazards: comma-split rawValues, validated against
            # SENSORY_HAZARD_VALUES. De-duped, order-preserving — the
            # JSON stays human-readable and re-runs produce stable diffs.
            hazard_raw = [
                h.strip() for h in (row.get('SensoryHazards') or '').split(',')
            ]
            sensory_hazards: list[str] = []
            for h in hazard_raw:
                if not h:
                    continue
                if h not in SENSORY_HAZARD_VALUES:
                    vocab_errors.append(
                        (name, f'unknown SensoryHazards value {h!r}')
                    )
                    continue
                if h not in sensory_hazards:
                    sensory_hazards.append(h)

            existing = entries.get(eid, {})
            # Preserve every field except the ones we authoritatively own.
            merged = {
                k: v for k, v in existing.items()
                if k not in (
                    'appeal', 'bucketFit', 'notes',
                    'popularity', 'featuredCharacters',
                    'mobility', 'serviceAnimals', 'sensoryHazards',
                )
            }
            if appeal:
                merged['appeal'] = appeal
            if bucketFit:
                merged['bucketFit'] = bucketFit
            if popularity:
                merged['popularity'] = popularity
            if characters:
                merged['featuredCharacters'] = characters
            if mobility_flags:
                merged['mobility'] = {flag: True for flag in mobility_flags}
            if service_animals:
                merged['serviceAnimals'] = service_animals
            if sensory_hazards:
                merged['sensoryHazards'] = sensory_hazards
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
