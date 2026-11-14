"""Detect workload-manager signals in jobscript text."""

import re

# Directive lines as documented by each manager:
#   Slurm: lines beginning with "#SBATCH" (sbatch man page)
#   PBS:   default directive prefix "#PBS" (OpenPBS qsub.1B)
#   LSF:   lines starting with "#BSUB" (LLNL/IBM LSF docs)
#   Cobalt: lines starting with "#COBALT" (ALCF docs)
#   OAR:   lines starting with "#OAR" (oarsub man page)
#   Flux:  RFC 36 sentinel, regex copied from flux-core
#          src/bindings/python/flux/job/directives.py
DIRECTIVES = {
    "slurm": re.compile(r"^#SBATCH\b"),
    "pbs": re.compile(r"^#PBS\b"),
    "lsf": re.compile(r"^#BSUB\b"),
    "cobalt": re.compile(r"^#COBALT\b"),
    "oar": re.compile(r"^#OAR\b"),
}
FLUX_SENTINEL = re.compile(r"^([^\w]*)((?:flux|FLUX):)(.*)$")
FLUX_COMMAND = re.compile(
    r"^\s*(?:exec\s+)?flux\s+(run|submit|batch|alloc|bulksubmit)\b"
)


def detect(text):
    """Return per-manager counts of directive and command lines found in text."""
    counts = {}

    def bump(key):
        counts[key] = counts.get(key, 0) + 1

    for line in text.splitlines():
        for name, pattern in DIRECTIVES.items():
            if pattern.match(line):
                bump(f"{name}_directive")
        match = FLUX_SENTINEL.match(line)
        # flux-core only accepts a directive whose first argument starts with "-"
        # (Directive.action == "SETARGS"); anything else is a no-op or an error.
        if match and match.group(3).strip().startswith("-"):
            # A whitespace-only prefix is valid RFC 36 (e.g. inside a Python
            # docstring) but also matches YAML keys, so count it separately.
            if match.group(1).strip():
                bump("flux_directive")
            else:
                bump("flux_bare_directive")
        if FLUX_COMMAND.match(line):
            bump("flux_command")
    return counts


def managers(counts):
    """Return the sorted set of managers with a confident signal in counts."""
    confident = {f"{name}_directive" for name in DIRECTIVES} | {"flux_directive"}
    return sorted({key.split("_")[0] for key in counts if key in confident})
