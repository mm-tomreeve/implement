# Vendored libraries

The wizard carries the three libraries it borrows rather than fetching them, so the page works on
a machine with no route out. That is the normal case here: the people planning an air-gapped estate
are usually sitting behind the air gap, and the wizard is the first thing they open.

It also settles the promise in the page footer. Nothing the wizard is told leaves the browser, and
with these here the page makes no requests at all.

| File | Library | Version | Licence | What it does |
|---|---|---|---|---|
| `pako.min.js` | [pako](https://github.com/nodeca/pako) | 2.1.0 | MIT (`LICENSE.pako`) | gzips the plan into the short `MME1:` line, and ungzips one you paste back |
| `js-yaml.min.js` | [js-yaml](https://github.com/nodeca/js-yaml) | 4.1.0 | MIT (`LICENSE.js-yaml`) | reads a pasted plan, encoded or plain YAML |
| `mermaid.min.js` | [mermaid](https://github.com/mermaid-js/mermaid) | 11.4.1 | MIT (`LICENSE.mermaid`) | draws the estate diagram |
| `inter.css`, `fonts/` | [Inter](https://github.com/rsms/inter) | v20 (Google Fonts) | OFL 1.1 (`LICENSE.inter`) | the typeface the page is set in |

Inter is one variable file per subset rather than one per weight, so all four weights the page uses
come out of the same 48 KB for latin, plus 83 KB for latin-ext where a server name has an accent in
it. `wizard.css` names the system font stack after Inter, so a missing file costs you the look and
nothing else.

`wizard.js` still copes with any of them missing: without pako the plan comes out as YAML only,
without js-yaml you cannot load a plan back in, and without mermaid the diagram falls back to its
own source text. Deleting one to save space costs you that and nothing else.

## Checksums

`tests/test_repo_consistency.py` checks the files against these, so a damaged or swapped copy fails
the build rather than reaching someone's browser. Update them in the same commit as the files.

```text
pako.min.js      sha384-rNlaE5fs9dGIjmxWDALQh/RBAaGRYT5ChrzHo6tRfgrZ36iRFAiquP5g41Jsv+0j
js-yaml.min.js   sha384-+pxiN6T7yvpryuJmE1gM9PX7yQit15auDb+ZwwvJOd/4be2Cie5/IuVXgQb/S9du
mermaid.min.js   sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I
```

## Updating one

Nothing watches these for new releases, so check them when you touch the wizard. Pick the version,
fetch it, and record what you got:

```bash
cd wizard/vendor
curl -sSfL https://cdn.jsdelivr.net/npm/pako@2.1.0/dist/pako.min.js -o pako.min.js
printf 'sha384-%s\n' "$(openssl dgst -sha384 -binary < pako.min.js | openssl base64 -A)"
```

Put that hash in the table above, then run `make test` and open `wizard/index.html` to see the
diagram draw and a plan encode and load back in.
