"""Parse jobscript directives into normalized, deterministic labels."""

import json
import os
import re
import shlex

PARSER_VERSION = "1"
HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    with open(os.path.join(HERE, "managers", name)) as fh:
        return json.load(fh)["options"]


SLURM_OPTIONS = _load("slurm_options.json")
FLUX_OPTIONS = _load("flux_options.json")
SLURM_SHORT = {v["short"]: v["canonical"] for v in SLURM_OPTIONS.values() if v["short"]}

# OpenPBS qsub getopt string (src/cmds/qsub_sup.c) plus Torque's -t (job arrays).
PBS_OPTSTRING = "a:A:c:C:e:fhIj:J:k:l:m:M:N:o:p:q:r:R:S:u:v:VW:XzP:t:"


def empty_labels(manager):
    """Return a label record with every normalized field unset."""
    return {
        "manager": manager, "parser_version": PARSER_VERSION,
        "nodes": None, "tasks": None, "tasks_per_node": None, "cpus_per_task": None,
        "slots": None, "cores_per_slot": None, "gpus": [], "memory": [],
        "walltime_seconds": None, "array": None, "queue": None, "account": None,
        "qos": None, "job_name": None, "constraint": None, "dependency": None,
        "exclusive": False, "email": None, "chunks": None, "components": 1,
        "options": [], "unknown": [], "errors": [],
    }


# ---------------------------------------------------------------- value parsing

def parse_count_range(text):
    """Parse 'N' or 'MIN-MAX' into {'min', 'max'}; None if not numeric."""
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", text.strip())
    if not m:
        return None
    lo = int(m.group(1))
    return {"min": lo, "max": int(m.group(2)) if m.group(2) else lo}


def index_count(spec):
    """Count indices in 'a-b:step,c,...' specs; a '%limit' suffix is ignored."""
    spec = spec.split("%", 1)[0]
    total = 0
    for part in spec.split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+)(?::(\d+))?)?", part.strip())
        if not m:
            return None
        if m.group(2) is None:
            total += 1
        else:
            lo, hi, step = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
            if hi < lo or step < 1:
                return None
            total += (hi - lo) // step + 1
    return total


def slurm_seconds(text):
    """Seconds for Slurm time formats (sbatch --time); None if not recognized."""
    t = text.strip()
    m = re.fullmatch(r"(\d+)-(\d+)(?::(\d+))?(?::(\d+))?", t)
    if m:  # days-hours[:minutes[:seconds]]
        d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        return ((d * 24 + h) * 60 + mi) * 60 + s
    parts = t.split(":")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 3:
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 1:  # minutes
        return nums[0] * 60
    if len(nums) == 2:  # minutes:seconds
        return nums[0] * 60 + nums[1]
    return (nums[0] * 60 + nums[1]) * 60 + nums[2]  # hours:minutes:seconds


def pbs_seconds(text):
    """Seconds for PBS duration '[[hours:]minutes:]seconds[.ms]'."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return int(total)


def hms_seconds(text):
    """Seconds for 'H:M:S' or 'H:M'; None otherwise."""
    parts = text.strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts] + ([0] if len(parts) == 2 else [])
    return (nums[0] * 60 + nums[1]) * 60 + nums[2]


def flux_seconds(text):
    """Seconds for Flux --time-limit: minutes, or FSD with s/m/h/d suffix (RFC 23)."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not m:
        return None
    scale = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return int(float(m.group(1)) * scale)


def slurm_mib(text):
    """MiB for Slurm sizes: default unit MiB, suffix K|M|G|T."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTkmgt]?)", text.strip())
    if not m:
        return None
    scale = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 ** 2}[m.group(2).upper()]
    return float(m.group(1)) * scale


def pbs_mib(text):
    """MiB for PBS sizes: plain integer is bytes, suffixes b|kb|mb|gb|tb."""
    m = re.fullmatch(r"(\d+)(b|kb|mb|gb|tb)?", text.strip().lower())
    if not m:
        return None
    scale = {None: 1, "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
    return int(m.group(1)) * scale[m.group(2)] / 1024 ** 2


def unit_mib(text):
    """MiB for sizes with an explicit unit (KB|MB|GB|TB, optional B); None otherwise."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT])B?", text.strip(), re.I)
    if not m:
        return None
    return float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 ** 2}[m.group(2).upper()]


# ------------------------------------------------------------------- Slurm

def slurm_tokens(rest):
    """Tokenize a #SBATCH line body like sbatch's get_argument (src/sbatch/opt.c)."""
    tokens, i, n = [], 0, len(rest)
    while True:
        while i < n and rest[i].isspace():
            i += 1
        if i >= n:
            return tokens
        arg, quoted, q_char = [], False, ""
        while i < n and (quoted or not rest[i].isspace()):
            ch = rest[i]
            if ch == "\\" and i + 1 < n:
                arg.append(rest[i + 1])
                i += 2
                continue
            if quoted:
                if ch == q_char:
                    quoted = False
                else:
                    arg.append(ch)
            elif ch in "\"'":
                quoted, q_char = True, ch
            elif ch == "#":
                return tokens + (["".join(arg)] if arg else [])
            else:
                arg.append(ch)
            i += 1
        if quoted:
            raise ValueError(f"unmatched {q_char}")
        tokens.append("".join(arg))


def slurm_lines(text):
    """Yield (lineno, body) for #SBATCH lines before the first command line."""
    for lineno, line in enumerate(text.splitlines(), 1):
        for magic in ("#SBATCH", "#SLURM"):
            if line.startswith(magic):
                yield lineno, line[len(magic):]
                break
        else:
            magic = None
        if magic:
            continue
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            return


def _slurm_long(name):
    """Resolve a long option, alias, or unambiguous prefix to its canonical name."""
    if name in SLURM_OPTIONS:
        return SLURM_OPTIONS[name]["canonical"]
    matches = {v["canonical"] for k, v in SLURM_OPTIONS.items() if k.startswith(name)}
    return matches.pop() if len(matches) == 1 else None


def slurm_options(text, labels):
    """Return [(lineno, component, long_name, value)] from #SBATCH lines."""
    out = []
    for lineno, body in slurm_lines(text):
        try:
            tokens = slurm_tokens(body)
        except ValueError as err:
            labels["errors"].append(f"line {lineno}: {err}")
            continue
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            i += 1
            if tok.lower() in ("hetjob", "packjob"):
                labels["components"] += 1
                continue
            if tok.startswith("--"):
                name, eq, value = tok[2:].partition("=")
                long = _slurm_long(name)
                if long is None:
                    labels["unknown"].append(tok)
                    continue
                arg = SLURM_OPTIONS[long]["arg"]
                if arg == "required" and not eq:
                    value = tokens[i] if i < len(tokens) else None
                    i += 1
                out.append((lineno, labels["components"], long,
                            value if (eq or arg == "required") else None))
            elif tok.startswith("-") and len(tok) > 1:
                long = SLURM_SHORT.get(tok[1])
                if long is None:
                    labels["unknown"].append(tok)
                    continue
                if SLURM_OPTIONS[long]["arg"] == "required":
                    value = tok[2:] or (tokens[i] if i < len(tokens) else None)
                    if not tok[2:]:
                        i += 1
                    out.append((lineno, labels["components"], long, value))
                else:
                    out.append((lineno, labels["components"], long, None))
            else:
                labels["unknown"].append(tok)
    return out


def _gpu_type_count(value):
    """Split '[type:]count' into (type, count)."""
    parts = value.split(":")
    if parts[-1].isdigit():
        return (":".join(parts[:-1]) or None), int(parts[-1])
    return None, None


def parse_slurm(text):
    """Normalized labels from #SBATCH directives."""
    labels = empty_labels("slurm")
    for lineno, component, name, value in slurm_options(text, labels):
        labels["options"].append({"line": lineno, "component": component,
                                  "option": f"--{name}", "value": value})
        if component > 1:
            continue  # normalize only the first hetjob component
        v = value or ""
        if name == "nodes":
            labels["nodes"] = parse_count_range(v)
        elif name == "ntasks" and v.isdigit():
            labels["tasks"] = int(v)
        elif name == "ntasks-per-node" and v.isdigit():
            labels["tasks_per_node"] = int(v)
        elif name == "cpus-per-task" and v.isdigit():
            labels["cpus_per_task"] = int(v)
        elif name in ("gpus", "gpus-per-node", "gpus-per-task"):
            gtype, count = _gpu_type_count(v)
            per = {"gpus": "job", "gpus-per-node": "node", "gpus-per-task": "task"}[name]
            if count is not None:
                labels["gpus"].append({"count": count, "per": per, "type": gtype})
        elif name == "gres":
            for entry in v.split(","):
                parts = entry.split(":")
                if parts[0] != "gpu":
                    continue
                count = int(parts[-1]) if len(parts) > 1 and parts[-1].isdigit() else 1
                gtype = parts[1] if len(parts) == 3 or (len(parts) == 2 and not parts[1].isdigit()) else None
                labels["gpus"].append({"count": count, "per": "node", "type": gtype})
        elif name in ("mem", "mem-per-cpu", "mem-per-gpu"):
            mib = slurm_mib(v)
            per = {"mem": "node", "mem-per-cpu": "cpu", "mem-per-gpu": "gpu"}[name]
            if mib is not None:
                labels["memory"].append({"mib": mib, "per": per})
        elif name == "time":
            labels["walltime_seconds"] = slurm_seconds(v)
        elif name == "array":
            labels["array"] = {"spec": v, "count": index_count(v)}
        elif name == "partition":
            labels["queue"] = v
        elif name == "account":
            labels["account"] = v
        elif name == "qos":
            labels["qos"] = v
        elif name == "job-name":
            labels["job_name"] = v
        elif name == "constraint":
            labels["constraint"] = v
        elif name == "dependency":
            labels["dependency"] = v
        elif name == "exclusive":
            labels["exclusive"] = True
        elif name == "mail-user":
            labels["email"] = v
    return labels


# --------------------------------------------------------------------- PBS

def pbs_lines(text):
    """Yield (lineno, body) for #PBS directives, following OpenPBS get_script."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line, lineno = lines[i], i + 1
        stripped = line.lstrip()
        if stripped.startswith("#PBS"):
            body = stripped[len("#PBS"):]
            while body.endswith("\\") and i + 1 < len(lines):  # continuation lines
                i += 1
                body = body[:-1] + lines[i]
            yield lineno, body
        elif line.startswith(":") or line.startswith("#!"):
            pass
        elif stripped and not stripped.startswith("#"):
            return
        i += 1


def _getopt(tokens, optstring, labels):
    """POSIX getopt over tokens; stops at the first non-option argument."""
    takes = {c: optstring[k + 1:k + 2] == ":" for k, c in enumerate(optstring) if c != ":"}
    out, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-") or tok == "-":
            labels["unknown"].extend(tokens[i:])
            break
        j = 1
        while j < len(tok):
            c = tok[j]
            if c not in takes:
                labels["unknown"].append(f"-{c}")
                j += 1
                continue
            if takes[c]:
                value = tok[j + 1:] or (tokens[i + 1] if i + 1 < len(tokens) else None)
                if not tok[j + 1:]:
                    i += 1
                out.append((c, value))
                break
            out.append((c, None))
            j += 1
        i += 1
    return out


def _pbs_resources(value, labels):
    """Apply one -l value (job-wide pairs, select, or Torque nodes) to labels."""
    for pair in value.split(","):
        key, _, val = pair.partition("=")
        if key == "select":
            # select=[N:]chunk[+[N:]chunk...], chunk=res=val[:res=val...] (qsub.1B)
            chunks = []
            for spec in val.split("+"):
                parts = spec.split(":")
                count = int(parts.pop(0)) if parts and parts[0].isdigit() else 1
                res = dict(p.split("=", 1) for p in parts if "=" in p)
                chunks.append({"count": count, "resources": res})
                if "mem" in res and pbs_mib(res["mem"]) is not None:
                    labels["memory"].append({"mib": pbs_mib(res["mem"]), "per": "chunk"})
            labels["chunks"] = chunks
        elif key == "walltime":
            labels["walltime_seconds"] = pbs_seconds(val)
        elif key == "nodes":
            # Torque: nodes={count|hostname}[:ppn=N][:gpus=G][+...]
            total, ppn, gpus = 0, set(), set()
            for spec in val.split("+"):
                parts = spec.split(":")
                total += int(parts[0]) if parts[0].isdigit() else 1
                for p in parts[1:]:
                    k, _, v = p.partition("=")
                    if k == "ppn" and v.isdigit():
                        ppn.add(int(v))
                    if k == "gpus" and v.isdigit():
                        gpus.add(int(v))
            labels["nodes"] = {"min": total, "max": total}
            if len(ppn) == 1:
                labels["tasks_per_node"] = ppn.pop()
            if len(gpus) == 1:
                labels["gpus"].append({"count": gpus.pop(), "per": "node", "type": None})
        elif key == "mem" and pbs_mib(val) is not None:
            labels["memory"].append({"mib": pbs_mib(val), "per": "job"})
        elif key == "place" and "excl" in val.split(":"):
            labels["exclusive"] = True


def parse_pbs(text):
    """Normalized labels from #PBS directives (OpenPBS and Torque)."""
    labels = empty_labels("pbs")
    for lineno, body in pbs_lines(text):
        try:
            tokens = shlex.split(body, comments=False, posix=True)
        except ValueError as err:
            labels["errors"].append(f"line {lineno}: {err}")
            continue
        for opt, value in _getopt(tokens, PBS_OPTSTRING, labels):
            labels["options"].append({"line": lineno, "option": f"-{opt}", "value": value})
            v = value or ""
            if opt == "l":
                _pbs_resources(v, labels)
            elif opt == "q":
                labels["queue"] = v
            elif opt == "A":
                labels["account"] = v
            elif opt == "N":
                labels["job_name"] = v
            elif opt in ("J", "t"):
                labels["array"] = {"spec": v, "count": index_count(v)}
            elif opt == "M":
                labels["email"] = v
            elif opt == "W" and v.startswith("depend="):
                labels["dependency"] = v[len("depend="):]
    return labels


# --------------------------------------------------------- LSF, Cobalt, OAR

def prefix_lines(text, prefix, stop="command"):
    """Yield (lineno, body) for lines starting with prefix.

    stop="command": stop at the first non-comment line; "block": stop at the first
    line after the shebang that is not a directive; "none": scan the whole script.
    """
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.startswith(prefix):
            yield lineno, line[len(prefix):]
            continue
        if stop == "block" and not (lineno == 1 and line.startswith("#!")):
            return
        stripped = line.lstrip()
        if stop == "command" and stripped and not stripped.startswith("#"):
            return


def generic_options(body):
    """Pair '-opt value' tokens; a token after an option is its value unless it starts with '-'."""
    tokens = shlex.split(body, posix=True)
    # Inline comments are taken to start at a token beginning with '#'.
    cut = next((k for k, t in enumerate(tokens) if t.startswith("#")), len(tokens))
    tokens = tokens[:cut]
    out, i = [], 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            name, eq, value = tok.partition("=")
            if not eq and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                value = tokens[i + 1]
                i += 1
            out.append((name, value if (eq or value) else None))
        i += 1
    return out


def _directives(text, prefix, labels, stop):
    """Return [(lineno, option, value)] for a generic directive prefix."""
    out = []
    for lineno, body in prefix_lines(text, prefix, stop):
        try:
            for opt, value in generic_options(body):
                labels["options"].append({"line": lineno, "option": opt, "value": value})
                out.append((lineno, opt, value or ""))
        except ValueError as err:
            labels["errors"].append(f"line {lineno}: {err}")
    return out


def parse_lsf(text):
    """Normalized labels from #BSUB directives."""
    labels = empty_labels("lsf")
    # LSF reads embedded options anywhere in the script (LSF "Write job scripts").
    for _, opt, v in _directives(text, "#BSUB", labels, "none"):
        if opt == "-n" and v.isdigit():
            labels["tasks"] = int(v)
        elif opt == "-nnodes" and v.isdigit():
            labels["nodes"] = {"min": int(v), "max": int(v)}
        elif opt == "-W":
            # [hours:]minutes
            if v.isdigit():
                labels["walltime_seconds"] = int(v) * 60
            elif re.fullmatch(r"\d+:\d+", v):
                h, m = v.split(":")
                labels["walltime_seconds"] = (int(h) * 60 + int(m)) * 60
        elif opt == "-q":
            labels["queue"] = v
        elif opt == "-J":
            labels["job_name"] = v
            m = re.search(r"\[([^\]]+)\](%\d+)?$", v)
            if m:
                labels["array"] = {"spec": m.group(1), "count": index_count(m.group(1))}
        elif opt in ("-G", "-P"):
            labels["account"] = v
        elif opt == "-u":
            labels["email"] = v
        elif opt == "-R":
            m = re.search(r"span\[ptile=(\d+)\]", v)
            if m:
                labels["tasks_per_node"] = int(m.group(1))
            if re.search(r"span\[hosts=1\]", v):
                labels["nodes"] = {"min": 1, "max": 1}
            m = re.search(r"rusage\[mem=([^\],:]+)", v)
            if m and unit_mib(m.group(1)) is not None:
                labels["memory"].append({"mib": unit_mib(m.group(1)), "per": "slot"})
        elif opt == "-x":
            labels["exclusive"] = True
    return labels


def parse_cobalt(text):
    """Normalized labels from #COBALT directives."""
    labels = empty_labels("cobalt")
    # ALCF: #COBALT directives must be the topmost lines of the script.
    for _, opt, v in _directives(text, "#COBALT", labels, "block"):
        if opt == "-n" and v.isdigit():
            labels["nodes"] = {"min": int(v), "max": int(v)}
        elif opt == "-t":
            # minutes, or hours:minutes:seconds
            labels["walltime_seconds"] = int(v) * 60 if v.isdigit() else hms_seconds(v)
        elif opt == "-q":
            labels["queue"] = v
        elif opt == "-A":
            labels["account"] = v
    return labels


def parse_oar(text):
    """Normalized labels from #OAR directives."""
    labels = empty_labels("oar")
    for _, opt, v in _directives(text, "#OAR", labels, "command"):
        if opt in ("-l", "--resource"):
            for part in v.split(","):
                key, _, val = part.partition("=")
                if key.strip() == "walltime":
                    labels["walltime_seconds"] = hms_seconds(val)
                    continue
                levels = [lvl for lvl in part.strip("/").split("/") if "=" in lvl]
                parent = None
                for level in levels:
                    name, _, count = level.partition("=")
                    if not count.isdigit():
                        continue
                    if name in ("nodes", "host", "network_address") and parent is None:
                        labels["nodes"] = {"min": int(count), "max": int(count)}
                    elif name == "gpu":
                        per = "node" if parent in ("nodes", "host", "network_address") else "job"
                        labels["gpus"].append({"count": int(count), "per": per, "type": None})
                    parent = name
        elif opt in ("-q", "--queue"):
            labels["queue"] = v
        elif opt in ("-n", "--name"):
            labels["job_name"] = v
        elif opt in ("-p", "--property"):
            labels["constraint"] = v
        elif opt == "--array" and v.isdigit():
            labels["array"] = {"spec": v, "count": int(v)}
    return labels


# -------------------------------------------------------------------- Flux

FLUX_SENTINEL = re.compile(r"^([^\w]*)((?:flux|FLUX):)(.*)$")


def flux_lines(text):
    """Yield (lineno, body) for RFC 36 directives using the first sentinel seen."""
    sentinel = None
    for lineno, line in enumerate(text.splitlines(), 1):
        m = FLUX_SENTINEL.match(line)
        if m and m.group(1).strip() and (sentinel is None or m.group(1) + m.group(2) == sentinel):
            sentinel = m.group(1) + m.group(2)
            yield lineno, m.group(3)


def parse_flux(text):
    """Normalized labels from Flux RFC 36 directives."""
    labels = empty_labels("flux")
    for lineno, body in flux_lines(text):
        try:
            tokens = shlex.split(body, comments=True, posix=True)
        except ValueError as err:
            labels["errors"].append(f"line {lineno}: {err}")
            continue
        if not tokens or not tokens[0].startswith("-"):
            continue
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            i += 1
            name, eq, value = tok.partition("=")
            short_value = None
            if not name.startswith("--") and len(name) > 2:  # -N4
                name, short_value = name[:2], name[2:]
            spec = FLUX_OPTIONS.get(name)
            if spec is None:
                labels["unknown"].append(tok)
                continue
            long = spec.get("long", name)
            if spec["arg"] == "required" and not eq and short_value is None:
                value = tokens[i] if i < len(tokens) else None
                i += 1
            elif short_value is not None:
                value = short_value
            labels["options"].append({"line": lineno, "option": long, "value": value or None})
            v = value or ""
            if long == "--nodes":
                labels["nodes"] = parse_count_range(v)
            elif long == "--nslots" and v.isdigit():
                labels["slots"] = int(v)
            elif long == "--cores-per-slot" and v.isdigit():
                labels["cores_per_slot"] = int(v)
            elif long == "--gpus-per-slot" and v.isdigit():
                labels["gpus"].append({"count": int(v), "per": "slot", "type": None})
            elif long == "--time-limit":
                labels["walltime_seconds"] = flux_seconds(v)
            elif long == "--queue":
                labels["queue"] = v
            elif long == "--job-name":
                labels["job_name"] = v
            elif long == "--exclusive":
                labels["exclusive"] = True
            elif long == "--requires":
                labels["constraint"] = v
            elif long == "--dependency":
                labels["dependency"] = v
    return labels


PARSERS = {"slurm": parse_slurm, "pbs": parse_pbs, "lsf": parse_lsf,
           "cobalt": parse_cobalt, "oar": parse_oar, "flux": parse_flux}


def _read_lines(manager, text):
    """Return the directive lines the manager itself would read."""
    readers = {"slurm": lambda t: slurm_lines(t), "pbs": lambda t: pbs_lines(t),
               "lsf": lambda t: prefix_lines(t, "#BSUB", "none"),
               "cobalt": lambda t: prefix_lines(t, "#COBALT", "block"),
               "oar": lambda t: prefix_lines(t, "#OAR", "command"),
               "flux": lambda t: flux_lines(t)}
    return [lineno for lineno, _ in readers[manager](text)]


def parse(text, managers):
    """Return {manager: labels} for each manager detected in the script.

    ignored_directive_lines counts directive-looking lines the manager would not
    read (e.g. after the first command, or inside a generated script).
    """
    from detect import detect

    counts = detect(text)
    out = {}
    for m in managers:
        if m not in PARSERS:
            continue
        labels = PARSERS[m](text)
        found = counts.get(f"{m}_directive", 0)
        labels["ignored_directive_lines"] = max(0, found - len(_read_lines(m, text)))
        out[m] = labels
    return out


def totals(labels):
    """Job-level values derivable without assumptions, for comparison with other labels."""
    out = {"nodes": None, "tasks": labels["tasks"], "gpus": None,
           "walltime_seconds": labels["walltime_seconds"], "queue": labels["queue"],
           "array_count": (labels["array"] or {}).get("count")}
    nodes = labels["nodes"]
    if nodes and nodes["min"] == nodes["max"]:
        out["nodes"] = nodes["min"]
    if out["tasks"] is None and out["nodes"] and labels["tasks_per_node"]:
        out["tasks"] = out["nodes"] * labels["tasks_per_node"]
    if labels["manager"] == "flux" and labels["slots"] is not None:
        out["tasks"] = labels["slots"]
    chunks = labels["chunks"] or []
    if out["tasks"] is None and chunks and all("mpiprocs" in c["resources"] and
                                               c["resources"]["mpiprocs"].isdigit() for c in chunks):
        # pbs_resources(7B): PBS_NODEFILE lines = sum of mpiprocs over chunks
        out["tasks"] = sum(c["count"] * int(c["resources"]["mpiprocs"]) for c in chunks)
    if len(labels["gpus"]) == 1:
        g = labels["gpus"][0]
        multiplier = {"job": 1, "node": out["nodes"], "task": out["tasks"],
                      "slot": labels["slots"]}.get(g["per"])
        if multiplier:
            out["gpus"] = g["count"] * multiplier
    return out
