# Riksarkivet Book Fetcher

Standalone script that fetches a complete Riksarkivet volume (all pages) from:
- a `bildvisning` URL, or
- a direct IIIF manifest URL.

Downloads go to a subfolder inside this project:
- `media/<book_title_sanitized>/images`

The folder name is built from the book title and sanitized automatically
(special characters normalized/removed).

## Auth file (outside this folder)

The script reads optional auth credentials from a separate file outside this
project folder.

Default path:
- `../riksarkivet-auth.json`

Create it from the example:
- `../riksarkivet-auth.json.example`

Repository-safe example file is also included here:
- `./riksarkivet-auth.json.example`

Notes:
- Never commit your real `riksarkivet-auth.json`.
- Use `--auth-file` if your auth file is in a different location.

## Usage

```bash
cd riksarkivet-book-fetcher
python3 fetch_riksarkivet_book.py "https://sok.riksarkivet.se/bildvisning/00054882_00069"
```

Batch mode from file:

```bash
python3 fetch_riksarkivet_book.py \
  --batch-file ./batch-urls.example.txt
```

Batch file format:
- one URL per line
- optional title override per line with `|` or TAB:
  - `<url>|<title>`
  - `<url><TAB><title>`
- empty lines and `# comments` are ignored

Continue/stop behavior:
- default: continue on errors
- stop at first failure with `--stop-on-error`

Batch queue behavior:
- after each successful `fetch + PDF` run, that row is removed from the batch file
- if IIIF index structures are available, bookmark creation is required for success
- if no index structures exist for a book, the run is still treated as success
- failed rows are kept in the batch file
- failures are written to a separate log file:
  - default: `<batch-file>.errors.log`
  - override with `--error-log <path>`

With explicit auth file:

```bash
python3 fetch_riksarkivet_book.py \
  "https://sok.riksarkivet.se/bildvisning/00054882_00069" \
  --auth-file ../riksarkivet-auth.json
```

Skip PDF build:

```bash
python3 fetch_riksarkivet_book.py "<url>" --skip-pdf
```

## Output

Per fetched book:
- `metadata.json` - full run metadata, pages and URLs.
- `indexing.json` - saved index/structure (`manifest.structures`) for reuse.
- `images/` - downloaded pages named by Riksarkivet page id when available (for example `C0021353_00001.jpg`), otherwise numeric fallback.
- `<book_title_sanitized>.pdf` - built if `img2pdf` or `Pillow` is available.
  - if `pikepdf` is available, IIIF `structures` are also written as PDF bookmarks (index).
