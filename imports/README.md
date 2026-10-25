# Paper collections: importing folders of PDFs

`scripts/import_pdfs.py` uploads a folder of PDFs into
[Karakeep](https://karakeep.app), one bookmark per PDF, sorted into lists.
Each run adds only what Karakeep doesn't have yet. So when the folder grows,
you run it again and only the new papers go up.

A **collection file** (`imports/<name>.json`) describes one folder: where
the PDFs are, which Karakeep list they go into, and, optionally, catalogues
with each paper's DOI, title and themes. This document is the full
specification of that file and of what the importer does with it.

In this homelab, paperbridge then hands every bookmark tagged `paper` over
to Zotero, and mirrors the lists under `Papers` as Zotero collections (see
the main README). The importer itself only talks to Karakeep.

## Try it: one paper, end to end

`imports/example/` is a complete collection with a single paper.

```
imports/example/
├── collection.json   the collection file
├── papers.csv        its catalogue: one row per PDF
└── pdfs/             the PDFs (ignored by git: imports/**/*.pdf)
```

1. **Download the PDF** into the collection's folder:

   ```bash
   mkdir -p imports/example/pdfs
   curl -fL -o imports/example/pdfs/vaswani-2017-attention.pdf https://arxiv.org/pdf/1706.03762
   ```

2. **The catalogue**, `papers.csv`, describes it. The `file` column names
   the PDF, relative to the PDF folder:

   ```csv
   file,doi,arxiv,title,themes,note
   vaswani-2017-attention.pdf,,1706.03762,Attention Is All You Need,sequence-models attention,Introduces the Transformer: sequence transduction built on attention alone.
   ```

3. **The collection file**, `collection.json`, says where things are and
   how to read the catalogue:

   ```json
   {
     "pdfs": "pdfs",
     "list": ["Papers", "Example"],
     "default_group": "Unsorted",
     "label": "the example collection",
     "csv": {
       "files": ["papers.csv"],
       "columns": {"path": "file", "doi": "doi", "arxiv": "arxiv", "title": "title",
                   "themes": "themes", "note": ["note"]},
       "group_by_theme": {"sequence-models": "Sequence models"}
     }
   }
   ```

   It has no `root`, so paths are relative to the collection file's own
   folder. The example works wherever the repo is checked out.

4. **Dry run** (the default; it changes nothing):

   ```bash
   scripts/import_pdfs.py imports/example/collection.json
   ```

   On an empty Karakeep:

   ```
   1 PDFs: 1 new (would import 1), 0 already in Karakeep, 0 to finish, ...
   lists to create: Papers, Papers/Example, Papers/Example/Sequence models
   ```

   This homelab's Karakeep has the paper already (paperbridge's first
   test), so here the dry run says `1 already in Karakeep`: the duplicate
   check at work.

5. **Import:**

   ```bash
   scripts/import_pdfs.py imports/example/collection.json --apply
   ```

   Karakeep gets one PDF bookmark:

   | | |
   |---|---|
   | List | `Papers` → `Example` → `Sequence models` (from the first theme) |
   | Tags | `paper`, `sequence-models`, `attention` |
   | Title | Attention Is All You Need |
   | Source URL | `https://arxiv.org/abs/1706.03762` |
   | Note | the CSV note, `From the example collection: vaswani-2017-attention.pdf`, `import-id: arxiv:1706.03762` |

   Run it again and it reports `1 already in Karakeep` and uploads
   nothing.

`imports/race.json` is a real collection of 343 papers. It uses every
feature below, including two CSV catalogues and a markdown reference list.

## The collection file

A JSON object. Only `pdfs`, `list` and `default_group` are required.

| Key | Type | Meaning |
|---|---|---|
| `root` | path | The folder the other paths are relative to; `~` is expanded. **Optional.** `--root` on the command line overrides it, for a machine where the papers live elsewhere. Without either, it is the collection file's folder. |
| `pdfs` | path | **Required.** The folder of PDFs, relative to `root`. It is searched recursively; every file ending in `.pdf` (any case) is a paper. A PDF's *path* below is relative to this folder, e.g. `map/smith-2020.pdf`. |
| `list` | list of names | **Required.** The Karakeep list path, e.g. `["Papers", "RACE"]`. Each PDF goes into `list` + its group, as a sublist. Missing lists are created. |
| `default_group` | name | **Required.** The group for PDFs nothing else places. |
| `label` | text | **Optional.** Names the source in each note: `From <label>: <path>`. Default: the last part of `pdfs`. |
| `group_by_file` | object | **Optional.** File name (not path) → group. Wins over every other rule. |
| `csv` | object | **Optional.** CSV catalogues; see below. |
| `markdown` | object | **Optional.** A markdown reference list, for PDFs without a CSV row; see below. |

### `csv`: catalogues

CSV files with a header row; one row per PDF, or several, which are merged.

| Key | Type | Meaning |
|---|---|---|
| `files` | list of paths | **Required.** The CSV files, relative to `root`. |
| `columns` | object | **Required.** Which CSV column holds which field. Only `path` is required. |
| `group_by_theme` | object | Theme → group, looked up for the PDF's **first** theme. A first theme missing here sends the PDF to `default_group`; every run, dry runs included, names such themes so they get mapped. |
| `horizon_tags` | object | Horizon value → tag, e.g. `{"short": "now", "long": "long-term"}`. The order is the precedence when rows disagree. |
| `relevance_tag_from` | integer | With `3`, a PDF whose relevance is 3 or more gets the tag `relevance-3`. |

`columns` maps these fields to column names:

| Field | Column holds | Used for |
|---|---|---|
| `path` | the PDF's path, relative to `pdfs` | **Required.** Rows with this empty are skipped. Rows naming no PDF on disk are ignored: only PDFs are imported. |
| `doi` | a DOI (`10.…`) | import ID, source URL, duplicate checks |
| `arxiv` | an arXiv ID (`1706.03762`; `arXiv:…` and arxiv.org links work too) | the same, when there's no DOI |
| `title` | the title | the bookmark's title, and the title duplicate check |
| `themes` | themes, separated by spaces, commas or semicolons | tags; the first picks the group |
| `relevance` | an integer; empty counts as 0 | the summary line and `relevance_tag_from` |
| `horizon` | a key of `horizon_tags` | a tag and the summary line |
| `note` | **a list of columns**: the first non-empty one per row counts | the note |
| `facts` | **a list of columns** whose values are listed | the summary line |

When several rows name the same PDF, in one file or across files:
- the themes are combined, in order of appearance;
- the highest relevance counts;
- the horizon is the first key of `horizon_tags` that any row has;
- the first non-empty DOI, arXiv ID and title count;
- each row's note is kept, minus repeats;
- each `facts` column contributes its distinct values, sorted.

### `markdown`: a reference list

For PDFs **without** a CSV row: a markdown file that mentions PDFs by
file name, e.g. a bibliography table with a "local file" column.

| Key | Type | Meaning |
|---|---|---|
| `file` | path | **Required.** The markdown file, relative to `root`. |
| `group_by_section` | object | Section → group. A section is the text of the nearest `## ` heading above the line. |
| `role_cell` | integer | For table rows: which `\|` cell to quote in the note, counted from 0. Markdown formatting and link targets are stripped. |
| `note` | text | The note's opening words. Default `Cited`. |
| `tags` | list | The PDF's tags. Default `["paper"]`. |

The first line that contains a PDF's file name counts for that PDF.
- **DOI:** taken from a `doi.org` link on that line.
- **arXiv ID:** taken from an `arxiv.org/abs/…` or `/pdf/…` link on that line.
- **Group:** `group_by_file` decides first, then the section's group. If neither places the PDF, the run **stops** and names it, rather than file it in the wrong place.
- **Note:** `<note> (<section>): <role cell>`.

## What each PDF becomes

**Where its data comes from:**
1. its CSV rows;
2. otherwise its line in the markdown list;
3. otherwise nothing.

A PDF in neither gets `default_group`, the tag `paper` and no identifiers. paperbridge then has to identify it from the PDF itself.

**Group:** the first rule that applies.
1. `group_by_file`;
2. from CSV: `group_by_theme` for the first theme;
3. from markdown: `group_by_section`;
4. `default_group`.

**Bookmark:**
- **File:** the PDF itself, uploaded as an asset. Never a link: papers often need a manual download.
- **List:** `list` + group.
- **Tags:** `paper`, the themes, the horizon tag and the relevance tag. A PDF placed by the markdown list gets that list's `tags` instead. The importer adds `paper` last, after the list and the other tags, so a consumer watching that tag only ever sees finished bookmarks.
- **Title:** from the catalogue, if any.
- **Source URL:** `https://doi.org/<DOI>`, else `https://arxiv.org/abs/<arXiv ID>`, else none.
- **Note:** one line per part, in this order:
  1. the notes;
  2. a summary line, e.g. `Relevance 2 · horizon long · <facts>`;
  3. `From <label>: <path>`;
  4. `import-id: <ID>`.

**Import ID:** `doi:<DOI>`, else `arxiv:<ID>`, else `sha256:<hash of the file>`, all lowercase. It is what makes reruns safe.

## Reruns: only what's new

Each run first reads every bookmark in Karakeep. A PDF counts as **already
there** if any bookmark has:

1. its import ID in the note,
2. its DOI or arXiv ID in its URL or source URL (arXiv's own
   `10.48550/arXiv.…` DOIs match the arXiv ID),
3. an uploaded file with the same file name, or
4. the same title (20 or more characters, ignoring case, accents and
   punctuation).

So a paper you saved by hand, e.g. as an arXiv link, isn't uploaded again.
Renaming or moving a PDF changes nothing. The same file twice on disk is
uploaded once.

A bookmark the importer created but didn't finish, e.g. because the run was
interrupted, is finished next run. Two bookmarks with the same import ID are
reported and left alone, for you to resolve.

**Never done:**
- **Deleting.** A removed PDF deletes nothing.
- **Updating.** A changed catalogue row doesn't change the existing bookmark's tags, list or note.
- **Moving.** Organise in Karakeep after import.
- **Replacing a PDF** whose paper is already there, e.g. a newer version with the same DOI.

## Running it

```bash
scripts/import_pdfs.py imports/<name>.json                  # dry run: report only
scripts/import_pdfs.py imports/<name>.json --apply          # import
scripts/import_pdfs.py imports/<name>.json --apply --limit 25
scripts/import_pdfs.py imports/<name>.json --root /other/checkout
```

| Option | Meaning |
|---|---|
| `--apply` | Upload. Without it nothing changes. |
| `--limit N` | Upload at most N new PDFs this run. Unfinished bookmarks are finished regardless. |
| `--root DIR` | The folder the collection's paths are relative to, instead of its `root`. |
| `--url URL` | Karakeep's API, e.g. `https://karakeep.example/api/v1`. Default: this homelab's. |

**API key:** from the environment, else from the repo's `.env`: `KARAKEEP_IMPORT_API_KEY`, else `KARAKEEP_API_KEY`. Create one in Karakeep under Settings → API Keys.

**Needs:** Python 3.10 or newer, standard library only, and a Karakeep
server (written against 0.33).

**The summary line** counts:
- **new:** PDFs to upload;
- **already in Karakeep:** PDFs found by any of the four checks;
- **to finish:** bookmarks a stopped run left without tags;
- **identical duplicates on disk:** the same file twice;
- **extra bookmarks:** more than one bookmark with the same import ID.

A dry run also lists the Karakeep lists it would create.

**Errors:** when Karakeep is restarting or busy, requests that are safe to
repeat are retried for about two minutes. A bookmark that failed partway is
never created twice: the importer re-reads Karakeep for its import ID first.
If Karakeep stays unreachable, the run stops; rerun it, and it continues
where it stopped.

**Large imports:** use `--limit 25` per run. Karakeep processes each
uploaded PDF in the background, and while it does, its SQLite database is
busy for other writes. Hundreds at once also need memory: here Karakeep has
8 GiB.
