# Job shapes

Every script in the corpus is turned into a job shape, written as a Flux jobspec, so that
requests made to different workload managers can be counted and compared in one form.

```bash
python3 dataset.py                       # once, from the repository root
python3 experiments/shapes/extract.py    # data/shapes.csv.gz, one row per script and manager
python3 experiments/shapes/analyze.py    # results/*.csv
python3 experiments/shapes/counts.py     # results/: resource histograms and attribute counts
python3 experiments/shapes/site.py       # docs/data/shapes.json, for docs/shapes.html
python3 -m pytest experiments/shapes
```

## What a shape is

`shape.py` reads the parsed directive labels and builds a jobspec resource tree:

```
node[2] > slot[16] > core[2] + gpu[1] + memory[4096 MiB]      duration 3600
```

A slot is one task when the request divides evenly across tasks, which is how flux-core
lays out a jobspec. When it does not (4 tasks sharing 2 GPUs on a node), the slot is the
node. A PBS `select` chunk is written as a slot. Anything the script did not say is never
filled in silently: counts that had to be assumed to make a valid jobspec are listed in
the `assumed` column and shown as `*` in the keys.

Each shape has four keys, from most to least detail:

| Key | Example |
|---|---|
| `exact` | `node[2]>slot[16]>core[2]+gpu[1]` |
| `binned` | `node[2]>slot[9-16]>core[2]+gpu[1]` (counts in powers of two) |
| `neutral` | `groups[2] tasks[17-32] cores[33-64] gpus[17-32]` (totals only) |
| `topology` | `node>slot>core+gpu` |

Use `neutral` to compare managers. It only has totals, so a PBS chunk request and a Slurm
node request for the same resources land on the same motif.

`context.py` reads what each script runs with fixed rules: launchers, the program after
the launcher, other commands, and loaded modules. Names are kept as written, so `gmx` and
`gmx_mpi` are separate. No model is involved.

## Results

| File | Contents |
|---|---|
| `summary.json` | per manager: how many shapes, what was specified, what was assumed |
| `motifs_*.csv` | motif counts by manager, at each key level |
| `fields_by_manager.csv` | quantiles of nodes, tasks, cores, GPUs, memory and walltime |
| `manager_similarity.csv` | how far apart two managers' motif distributions are |
| `manager_distinctive.csv` | motifs that one manager has far more of than the rest |
| `tokens.csv` | for each program or module: its usual shape and how specific it is |
| `token_neighbours.csv` | programs and modules with the most similar shape profiles |
| `token_by_manager.csv` | the same program under different managers |
| `token_motif_lift.csv` | program and motif pairs that occur together more than chance |

Counts come as scripts and as repository weight. Repository weight gives each repository
one vote per manager, split over its scripts, and counts a file copied verbatim into many
repositories once. Distances are Jensen-Shannon divergence in bits: 0 is identical, 1 is
no overlap.

## Limits

- The jobspecs have not been run through Flux itself.
- Flux, OAR and Cobalt have fewer than 200 repositories each.
- PBS job-wide requests such as `-l ncpus=`, `procs=` and `mppwidth=` are not read yet, so
  some PBS scripts show no resources.
- PBS `ngpus` and LSF `-gpu num=` are site conventions; both are read as GPUs per chunk or host.
- Templates copied with small edits across many repositories still count once per repository.
- Program names are noisy: shell functions and words from inline code get through.
