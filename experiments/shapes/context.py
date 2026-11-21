"""What a jobscript runs, read from its text with fixed rules (no model).

context(text) returns the launchers, the programs started through a launcher, the
other commands the script calls, and the modules it loads. These are evidence, not a
classification: "gmx_mpi" and "gmx" stay two different names.
"""

import re

LAUNCHERS = {"srun", "mpirun", "mpiexec", "mpiexec.hydra", "mpiexec_mpt", "aprun", "jsrun",
             "ibrun", "orterun", "prun", "lrun", "mpprun", "charmrun", "oarsh"}
FLUX_RUN = {"run", "submit", "batch", "alloc", "bulksubmit", "mini"}

# Launcher options that take a separate value, so the value is not mistaken for the program.
VALUE_OPTIONS = {"-n", "-np", "-N", "-c", "-ppn", "-npernode", "-x", "-a", "-g", "-r", "-d",
                 "-j", "-S", "-cc", "-ss", "-G", "-p", "-t", "-w", "-o", "-e", "-J", "-A",
                 "-m", "-l", "-b", "-H", "-host", "-hosts", "-hostfile", "-machinefile", "-f",
                 "-wdir", "-env", "-genv", "-x", "--map-by", "--bind-to", "--rank-by",
                 "--hostfile", "--host", "--np", "--oversubscribe-n", "-rf", "--report-bindings-n",
                 "--mpi", "-D", "-C", "-q", "-M", "-u", "-i", "--cpu-bind", "--cpu_bind",
                 "--distribution", "--multi-prog", "-E", "-L"}
TWO_VALUES = {"--mca", "-mca", "-gmca", "--gmca", "-genv", "-env", "-x"}

WRAPPERS = {"time", "exec", "nohup", "command", "eval", "sudo", "stdbuf", "nice", "/usr/bin/time"}

# Shell words and housekeeping tools that say nothing about the workload.
SKIP = set("""
if then else elif fi for do done while until case esac in function select return exit break
continue cd echo export source module ml mkdir cp mv rm ls cat set unset shift wait sleep date
pwd printf read touch ln chmod chown test true false trap ulimit umask alias local declare
typeset let exec eval hostname whoami env printenv which type head tail grep egrep fgrep sed awk
gawk cut sort uniq wc tr tee tar gzip gunzip zip unzip find xargs basename dirname seq bc kill
sync df du free uname id scontrol squeue sacct sbatch sstat sinfo scancel qsub qstat qdel bsub
bjobs nvidia-smi lscpu numactl taskset rsync scp ssh wget curl git make cmake rmdir pushd popd
ldd nproc tput clear exit history jobs fg bg times getconf lfs stat md5sum diff paste column
conda mamba pip pip3 spack micromamba virtualenv activate deactivate
""".split())

# Python and prose that leaks in from inline code and from files that are not shell scripts.
SKIP |= set("""
import from print def class with as try except raise assert pass lambda yield global del not
and or is none self args kwargs main end begin run show the this that these those to of on at
by it we you all each after before when where which use using used can will should must be are
was has have new add get put data job jobs create submit note warning error info usage example
""".split())

WORD = re.compile(r"^[A-Za-z_][\w.+-]*$")
ASSIGN = re.compile(r"^[A-Za-z_]\w*(\[[^\]]*\])?\+?=")
SPLIT = re.compile(r"&&|\|\||;")
VERSIONED = re.compile(r"^(python|pypy|perl|ruby|julia|R|gcc|g\+\+|java|lua|node)[\d.]+$")


def _name(token):
    """Program name from a command token; None for variables and non-words."""
    token = token.strip("\"'`(){}")
    if not token or "$" in token:
        return None
    base = token.rsplit("/", 1)[-1]
    m = VERSIONED.match(base)
    if m:
        base = m.group(1)
    if not WORD.match(base) or len(base) < 2 or len(base) > 40 or base.endswith(("++", "--")):
        return None
    return base if base == "R" else base.lower()


def _program(tokens):
    """First token after the launcher that is not an option or an option value."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            if tok in TWO_VALUES:
                i += 3
            elif "=" not in tok and tok in VALUE_OPTIONS:
                i += 2
            else:
                i += 1
            continue
        if tok.replace(".", "").replace(":", "").replace(",", "").isdigit() or tok.startswith("$"):
            i += 1
            continue
        return _name(tok)
    return None


HEREDOC = re.compile(r"<<-?\s*[\"']?([A-Za-z_]\w*)[\"']?")
ARRAY_OPEN = re.compile(r"^[A-Za-z_]\w*\+?=\([^)]*$")


def command_lines(text, max_lines=3000):
    """Yield command lines: continuations joined, heredoc bodies and array literals skipped."""
    heredoc, in_array, pending = None, False, ""
    for line in text.splitlines()[:max_lines]:
        stripped = line.strip()
        if heredoc:
            if stripped == heredoc:
                heredoc = None
            continue
        if in_array:
            in_array = ")" not in stripped
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        stripped, pending = pending + stripped, ""
        if ARRAY_OPEN.match(stripped):
            in_array = True
            continue
        m = HEREDOC.search(stripped)
        if m:
            heredoc = m.group(1)
        yield stripped


def context(text, max_lines=3000):
    launchers, programs, commands, modules = set(), set(), set(), set()
    for stripped in command_lines(text, max_lines):
        for segment in SPLIT.split(stripped):
            tokens = segment.split("|", 1)[0].split()
            while tokens and (ASSIGN.match(tokens[0]) or tokens[0] in WRAPPERS):
                tokens.pop(0)
            if not tokens:
                continue
            head = tokens[0].rsplit("/", 1)[-1]
            if head in ("module", "ml"):
                rest = tokens[1:]
                if head == "module":
                    if not rest or rest[0] not in ("load", "add", "swap", "switch"):
                        continue
                    rest = rest[1:]
                elif rest and rest[0] in ("purge", "list", "avail", "use", "unload", "spider",
                                          "reset", "restore", "save", "-"):
                    continue
                for tok in rest:
                    if tok[:1] in "-$":
                        continue
                    name = _name(tok.split("/", 1)[0])
                    if name:
                        modules.add(name)
            elif head in LAUNCHERS or (head == "flux" and len(tokens) > 1 and tokens[1] in FLUX_RUN):
                skip = 2 if head == "flux" else 1
                launchers.add(head)
                program = _program(tokens[skip:])
                if program and program not in LAUNCHERS:
                    programs.add(program)
            else:
                name = _name(tokens[0])
                if name and name not in SKIP:
                    commands.add(name)
    return {"launchers": sorted(launchers), "programs": sorted(programs),
            "commands": sorted(commands - programs), "modules": sorted(modules)}
