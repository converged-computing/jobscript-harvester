# jobscript-harvester

A corpus of Slurm, PBS, LSF, Cobalt, OAR, and Flux jobscripts from public GitHub repositories. Every script is stored with the repository, commit, and path it came from, the repository license, and labels parsed from its directives. This is a follow up to [jobspec-database](https://github.com/converged-computing/jobspec-database).

The current harvest has about 783,000 unique scripts from 48,600 repositories.

## Usage

```bash
pip install requests selenium
export GITHUB_TOKEN=...

# find repositories with GitHub code search (opens Chrome, log in once)
python3 harvest.py search

# clone each repository and keep the files that have directives
python3 harvest.py scan --workers 4

# license, stars, fork and archived for each repository
python3 harvest.py repos --graphql

# parse the directives into labels
python3 harvest.py label

python3 harvest.py stats
```

Everything is saved to `harvest.sqlite`, and every step picks up where it left off if you run it again.

## How it works

Search is only used to find repositories. We use the web search in a logged-in browser because the code search API ignores `#` and `:`, and only covers repositories with recent activity. A search returns at most 100 results, so any query that fills up is split into narrower ones (by language, extension, license, directive options, and so on) following `plan.json`.

The scan then clones each repository and reads every file in it, so a repository found through one script contributes all of its scripts. A file is kept if it has a directive line:

| Manager | Directive |
|---|---|
| Slurm | `#SBATCH` |
| PBS | `#PBS` |
| LSF | `#BSUB` |
| Cobalt | `#COBALT` |
| OAR | `#OAR` |
| Flux | `#flux:` (RFC 36) |

Labels come from the directives alone, read the way each manager reads them. For example, sbatch stops reading at the first command, so a `#SBATCH` line after that point is not counted. Each script gets nodes, tasks, CPUs, GPUs, memory, walltime, queue, account, array size and the like, plus a list of anything the parser did not recognize. The option tables are in `managers/` and the rules are in `parse.py`.

There is also an optional step to ask a model for labels that directives can't give you, such as the application and software a script runs. See `harvest.py soft-label` and `harvest.py agree`.

## Dataset

```bash
python3 dataset.py
```

This writes the corpus to `dataset/` as plain files, so you don't need SQLite to use it:

- `scripts/scripts-XX.jsonl.gz` has one record per unique script, with the content, labels, and every place it was found
- `index.csv.gz` is a flat table of metadata and labels without the content
- `repos.csv.gz` has one row per repository
- `manifest.json` has counts and checksums

Add `--scrub-emails` to remove email addresses. The database and `dataset/` are too large for git, so they are not in this repository.

## Explorer

```bash
python3 explorer.py
```

Open http://127.0.0.1:8000 to search and filter every script by manager, repository, license, resources, or text in the script.

## Site

The `docs/` directory is a static site for GitHub Pages. To update the numbers and preview it:

```bash
python3 build_docs.py
python3 -m http.server -d docs
```

## Notes

- Repositories already in jobspec-database can be imported with `harvest.py import-legacy`, and are skipped by the scan unless you ask for them with `--include-legacy`.
- Scripts can contain usernames, emails, and home paths.
- GitHub's Acceptable Use Policies allow research use of public information as long as the resulting publications are open access.

## License

HPCIC DevTools is distributed under the terms of the MIT license.
All new contributions must be made under this license.

See [LICENSE](https://github.com/converged-computing/cloud-select/blob/main/LICENSE),
[COPYRIGHT](https://github.com/converged-computing/cloud-select/blob/main/COPYRIGHT), and
[NOTICE](https://github.com/converged-computing/cloud-select/blob/main/NOTICE) for details.

SPDX-License-Identifier: (MIT)

LLNL-CODE- 842614
