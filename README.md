# Reading tool

Speed-read any PDF one word at a time (RSVP), with every figure, table and footnote one click away.
Runs entirely on your Mac.

It works with any PDF that has a text layer. I mainly use it for research papers, so that is what the
conversion is tuned for: two-column layouts, numbered sections, figure and table captions, equations,
footnotes and reference lists.

## Get it

```sh
git clone https://github.com/KustDavid/reading_tool.git
```

Built and tested on a Mac. It needs [uv](https://docs.astral.sh/uv/) (`brew install uv`); everything
else is installed automatically on the first start. No PDFs are included — add your own.
To look around without one, open <http://127.0.0.1:8377/?mock=1> for a made-up demo paper.

## Start

Double-click **`start.command`** in Finder. A Terminal window opens, the server starts, and the app
opens at <http://127.0.0.1:8377>. Close that Terminal window to stop it.

From a terminal instead:

```sh
cd app && uv run python -m backend.server
```

(Needs [uv](https://docs.astral.sh/uv/): `brew install uv`. The first start downloads the Python
packages, which takes a minute.)

## What's in this folder

- `start.command` – double-click to start
- `papers/` – your PDFs and everything made from them (`inbox/` to add, `library/` to keep)
- `app/` – the program itself; nothing in there needs touching

## Where the PDFs go

- **Add a paper:** upload it in the app, or drop the PDF into **`papers/inbox/`**. Anything in the inbox
  is converted automatically (also files that were already there when the server starts) and then moved
  out of the inbox.
- **Your library:** `papers/library/<paper-id>/` holds everything for one paper:
  - `original.pdf` – the PDF exactly as you added it
  - `paper.json` – the converted text, sections, figures and tables
  - `assets/` – the figure, table and equation images
  - `progress.json` – where you stopped reading
  - `annotations.json` and `annotated.pdf` – your highlights and notes, and a copy of the PDF with
    them marked in yellow (only once you have highlighted something)
- Adding the same PDF twice does nothing (papers are recognised by their content).
- To remove a paper, delete it in the app (or delete its folder while the server is stopped).

## Re-convert a paper

From a terminal, inside `app/`:

```sh
uv run python -m backend.ingest --reingest <paper-id>    # one paper
uv run python -m backend.ingest --reingest-all           # everything
uv run python -m backend.ingest path/to/paper.pdf        # add + convert a PDF without the server
```

## What the conversion does

Reading order for one- and two-column layouts; running headers, page numbers, publisher watermarks
and footnotes removed from the text; hyphenated line breaks rejoined; sections detected (References,
Acknowledgements and Appendices are skipped by default); figures and tables cropped as images
(multi-page and sideways landscape tables included) and linked from the sentences that mention them;
display equations shown as images. Scanned PDFs without a text layer are not supported (no OCR).

## Development

```sh
cd app
uv run pytest                        # backend tests
node --test web/tests/*.test.mjs     # reader logic tests
```

Backend code is in `app/backend/`, the browser app in `app/web/` (plain HTML/JS, no build step);
`app/samples/` holds the PDFs the tests run on.

## Licence

MIT — see `LICENSE`.
