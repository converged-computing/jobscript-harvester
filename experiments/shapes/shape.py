"""Turn parsed directive labels into a job shape in the form of a Flux jobspec.

A shape is built in three steps:

1. blocks(labels)   what the script asked for, in manager-neutral terms. Nothing is
                    assumed here; a field the script did not set stays None.
2. resolve(block)   pick the slot (task, group, or job) and fill the counts a jobspec
                    needs. Every count that had to be assumed is listed in "assumed".
3. jobspec / keys   render the canonical jobspec (RFC 14) and three keys of decreasing
                    detail: exact, binned, topology.

A "group" is a node, or a PBS select chunk. Slots follow the flux-core convention
(one slot per task) when the per-node and per-job requests divide evenly across tasks;
otherwise the slot is the group, or the whole job.
"""

import json
import math
import re

SHAPE_VERSION = "1"


def _int(value):
    """Integer from a label value, or None for anything that is not a plain number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _block(kind=None):
    return {"kind": kind, "groups": None, "tasks": None, "tasks_per_group": None,
            "cores_per_task": None, "cores_per_group": None, "cores": None,
            "gpus": None, "memory": None}


def _options(labels, *names):
    return [o.get("value") or "" for o in labels.get("options", []) if o.get("option") in names]


# ------------------------------------------------------------ per manager

def _first(items, flags, flag):
    if len(items) > 1:
        flags.append(flag)
    return items[0] if items else None


def _nodes(labels, flags):
    nodes = labels.get("nodes")
    if not nodes:
        return None
    if nodes["min"] != nodes["max"]:
        flags.append("node_range")
    return nodes["min"]


def _common(labels, flags):
    """Fields that Slurm, Torque, LSF, Flux and Cobalt labels share."""
    b = _block("node")
    b["groups"] = _nodes(labels, flags)
    b["tasks"] = labels.get("tasks")
    b["tasks_per_group"] = labels.get("tasks_per_node")
    b["cores_per_task"] = labels.get("cpus_per_task")
    gpu = _first(labels.get("gpus") or [], flags, "several_gpu_requests")
    if gpu:
        per = {"node": "group", "slot": "task"}.get(gpu["per"], gpu["per"])
        b["gpus"] = (gpu["count"], per)
    mem = _first(labels.get("memory") or [], flags, "several_memory_requests")
    if mem:
        per = {"node": "group", "slot": "task", "chunk": "group"}.get(mem["per"], mem["per"])
        b["memory"] = (mem["mib"], per)
    return b


def _pbs(labels, flags):
    chunks = labels.get("chunks")
    if not chunks:
        return [_common(labels, flags)]          # Torque nodes=N:ppn=P, or nothing
    out = []
    for chunk in chunks:
        res = chunk.get("resources", {})
        b = _block("chunk")
        b["groups"] = chunk.get("count")
        b["cores_per_group"] = _int(res.get("ncpus"))
        b["tasks_per_group"] = _int(res.get("mpiprocs"))
        gpus = _int(res.get("ngpus"))            # site-defined resource, widely used
        if gpus:
            b["gpus"] = (gpus, "group")
            flags.append("pbs_ngpus")
        if res.get("mem"):
            from_label = [m for m in labels.get("memory") or [] if m["per"] == "chunk"]
            if len(chunks) == 1 and from_label:
                b["memory"] = (from_label[0]["mib"], "group")
        out.append(b)
    if len(out) > 1:
        flags.append("several_chunk_types")
    job_mem = [m for m in labels.get("memory") or [] if m["per"] == "job"]
    if job_mem and len(out) == 1 and out[0]["memory"] is None:
        out[0]["memory"] = (job_mem[0]["mib"], "job")
    return out


def _lsf(labels, flags):
    b = _common(labels, flags)
    for value in _options(labels, "-gpu"):
        m = re.search(r"num=(\d+)(/task)?", value)
        if m:                                    # per host unless "/task" is given
            b["gpus"] = (int(m.group(1)), "task" if m.group(2) else "group")
    return [b]


def _flux(labels, flags):
    b = _common(labels, flags)
    b["tasks"] = labels.get("slots")
    b["cores_per_task"] = labels.get("cores_per_slot")
    return [b]


def _oar(labels, flags):
    """-l /nodes=N/cpu=K/core=C; OAR has no task count."""
    b = _block("node")
    requests = []
    for value in _options(labels, "-l", "--resource"):
        for part in value.split(","):
            if "walltime" not in part and "=" in part:
                requests.append(part)
    if len(requests) > 1:
        flags.append("several_resource_requests")
    if not requests:
        return [b]
    levels = {}
    for level in requests[0].strip("/ {}").split("/"):
        name, _, count = level.partition("=")
        if _int(count) is not None:
            levels[name.strip()] = _int(count)
    nodes = next((levels[k] for k in ("nodes", "host", "network_address") if k in levels), None)
    cores = levels.get("core")
    if "cpu" in levels:
        cores = levels["cpu"] * (cores or 1)
        if "core" not in levels:
            flags.append("oar_cpu_as_core")
    b["groups"] = nodes
    if nodes is not None:
        b["cores_per_group"] = cores
    else:
        b["cores"] = cores
    if "gpu" in levels:
        b["gpus"] = (levels["gpu"], "group" if nodes is not None else "job")
    return [b]


def _cobalt(labels, flags):
    b = _common(labels, flags)
    for value in _options(labels, "--proccount"):
        b["tasks"] = _int(value)
    return [b]


BUILDERS = {"slurm": lambda l, f: [_common(l, f)], "pbs": _pbs, "lsf": _lsf,
            "flux": _flux, "oar": _oar, "cobalt": _cobalt}


def blocks(labels):
    """Return ([block], flags): the request as written, with nothing assumed."""
    flags = []
    if labels.get("components", 1) > 1:
        flags.append("hetjob_first_component")
    return BUILDERS[labels["manager"]](labels, flags), flags


# ---------------------------------------------------------------- resolve

def resolve(block):
    """Fill in the counts a jobspec needs and choose the slot level."""
    assumed, derived = [], []
    # A count of zero is treated as not given.
    block = {k: (None if isinstance(v, (int, float)) and not isinstance(v, bool) and v <= 0 else v)
             for k, v in block.items()}
    for field in ("gpus", "memory"):
        if block[field] and block[field][0] <= 0:
            block[field] = None
    groups, tasks, tpg = block["groups"], block["tasks"], block["tasks_per_group"]
    kind = block["kind"]

    if groups is None and tasks is not None and tpg:
        groups = math.ceil(tasks / tpg)
        derived.append("groups")
    elif groups is None and tpg is not None:
        groups = 1
        assumed.append("groups")
    grouped = groups is not None

    uneven = False
    if grouped:
        if tasks is not None:
            if tpg is None:
                tpg = math.ceil(tasks / groups)
            uneven = tasks != groups * tpg
        elif tpg is None:
            tpg = 1
            assumed.append("tasks")
        total_tasks = tasks if tasks is not None else groups * tpg
    else:
        if tasks is None:
            tasks = 1
            assumed.append("tasks")
        total_tasks, tpg = tasks, None
    n_groups = groups if grouped else 1

    # Total cores, then per-slot amounts are taken from the totals.
    cpt = block["cores_per_task"]
    if cpt is not None:
        cores = total_tasks * cpt
    elif block["cores_per_group"] is not None:
        cores = n_groups * block["cores_per_group"]
    elif block["cores"] is not None:
        cores = block["cores"]
    else:
        cores = total_tasks
        assumed.append("cores")

    gpus = 0
    if block["gpus"]:
        count, per = block["gpus"]
        gpus = count * {"task": total_tasks, "group": n_groups}.get(per, 1)
    memory = None
    if block["memory"]:
        mib, per = block["memory"]
        memory = mib * {"task": total_tasks, "group": n_groups, "cpu": cores,
                        "gpu": gpus or 1}.get(per, 1)

    def fits(n):
        return cores % n == 0 and gpus % n == 0

    if kind == "chunk":
        slot = "group"                     # a PBS chunk is already a slot
    elif not uneven and fits(total_tasks):
        slot = "task"
    elif grouped and fits(n_groups):
        slot = "group"
    else:
        slot = "job"
    n_slots = {"task": total_tasks, "group": n_groups, "job": 1}[slot]

    return {
        "kind": kind if grouped else None, "groups": groups, "slot": slot,
        "slots": n_slots, "tasks": total_tasks, "tasks_per_group": tpg,
        "cores": cores, "gpus": gpus, "memory_mib": memory,
        "per_slot": {"core": cores // n_slots, "gpu": gpus // n_slots,
                     "memory": None if memory is None else max(1, int(memory // n_slots))},
        "assumed": assumed, "derived": derived, "uneven_tasks": uneven,
    }


# ----------------------------------------------------------------- render

def _slot_tree(r, label):
    per = r["per_slot"]
    children = [{"type": "core", "count": per["core"]}]
    if per["gpu"]:
        children.append({"type": "gpu", "count": per["gpu"]})
    if per["memory"]:
        children.append({"type": "memory", "count": per["memory"], "unit": "MiB"})
    if r["slot"] == "task" and r["kind"] == "node":
        count = r["tasks_per_group"]
    else:
        count = r["slots"] if r["slot"] != "group" or r["kind"] != "node" else 1
    slot = {"type": "slot", "count": count, "label": label, "with": children}
    if r["kind"] == "node" and r["slot"] != "job":
        return {"type": "node", "count": r["groups"], "with": [slot]}
    return slot


def _task_count(r):
    if r["slot"] == "task":
        return {"per_slot": 1}
    if r["slot"] == "group" and not r["uneven_tasks"] and r["tasks_per_group"]:
        return {"per_slot": r["tasks_per_group"]}
    if r["slot"] == "group" and r["kind"] == "chunk":
        return {"per_slot": 1}
    return {"total": r["tasks"]}


def jobspec(resolved, labels):
    """Canonical jobspec (RFC 14) for the resolved blocks. The command is left empty."""
    resources, tasks = [], []
    for i, r in enumerate(resolved):
        label = "task" if len(resolved) == 1 else f"task{i}"
        tree = _slot_tree(r, label)
        if labels.get("exclusive") and tree["type"] == "node":
            tree["exclusive"] = True
        resources.append(tree)
        tasks.append({"command": [], "slot": label, "count": _task_count(r)})
    system = {}
    if labels.get("walltime_seconds"):
        system["duration"] = labels["walltime_seconds"]
    if labels.get("queue"):
        system["queue"] = labels["queue"]
    return {"version": 9999, "resources": resources, "tasks": tasks,
            "attributes": {"system": system}}


# ------------------------------------------------------------------- keys

def bin_count(n):
    """Power-of-two buckets: 1, 2, 3-4, 5-8, ... 513-1024, 1025+."""
    if n <= 2:
        return str(n)
    if n > 1024:
        return "1025+"
    hi = 2 ** math.ceil(math.log2(n))
    return f"{hi // 2 + 1}-{hi}"


def bin_memory(mib):
    for gib in (1, 2, 4, 8, 16, 32, 64, 128, 256):
        if mib <= gib * 1024:
            return f"<={gib}G"
    return ">256G"


DURATIONS = [(600, "<=10m"), (3600, "<=1h"), (4 * 3600, "<=4h"), (12 * 3600, "<=12h"),
             (24 * 3600, "<=24h"), (72 * 3600, "<=3d"), (7 * 86400, "<=7d")]


def bin_duration(seconds):
    if not seconds:
        return "unset"
    return next((name for limit, name in DURATIONS if seconds <= limit), ">7d")


def _key(r, level):
    """One block as text, e.g. node[2]>slot[16]>core[1]+gpu[1]. '*' marks an assumed count."""
    def show(name, value, assumed=False, memory=False):
        if level == "topology":
            return name
        if assumed:
            return f"{name}[*]"
        if level == "exact":
            return f"{name}[{value}]"
        return f"{name}[{bin_memory(value) if memory else bin_count(value)}]"

    per = r["per_slot"]
    tasks_assumed = "tasks" in r["assumed"]
    parts = []
    if r["kind"] == "node" and r["slot"] != "job":
        parts.append(show("node", r["groups"], "groups" in r["assumed"]))
        if r["slot"] == "task":
            parts.append(show("slot", r["tasks_per_group"], tasks_assumed))
        else:
            parts.append(show("slot", 1))
    else:
        parts.append(show("chunk" if r["kind"] == "chunk" else "slot", r["slots"],
                          tasks_assumed and r["slot"] == "task"))
    # Assumed cores (one per task) still add up to a known count when the slot holds
    # several specified tasks.
    cores_unknown = "cores" in r["assumed"] and (r["slot"] == "task" or tasks_assumed)
    leaves = [show("core", per["core"], cores_unknown)]
    if per["gpu"]:
        leaves.append(show("gpu", per["gpu"]))
    if per["memory"]:
        leaves.append(show("memory", per["memory"], memory=True))
    return ">".join(parts) + ">" + "+".join(leaves)


def keys(resolved):
    return {level: " & ".join(_key(r, level) for r in resolved)
            for level in ("exact", "binned", "topology")}


# ----------------------------------------------------------------- shape

REQUEST_FIELDS = ("groups", "tasks", "tasks_per_group", "cores_per_task",
                  "cores_per_group", "cores", "gpus", "memory")


def shape(labels):
    """Full shape record for one (script, manager) label set."""
    request, flags = blocks(labels)
    resolved = [resolve(b) for b in request]
    specified = sorted({f for b in request for f in REQUEST_FIELDS
                        if b[f] not in (None, 0) and (not isinstance(b[f], tuple) or b[f][0] > 0)})
    assumed = sorted({a for r in resolved for a in r["assumed"]})
    cores_known = any(b[f] is not None for b in request
                      for f in ("tasks", "tasks_per_group", "cores_per_task",
                                "cores_per_group", "cores"))
    tasks_known = any(b["tasks"] is not None or b["tasks_per_group"] is not None
                      for b in request)
    groups = [r["groups"] for r in resolved if r["groups"] is not None and "groups" not in r["assumed"]]
    memory = [r["memory_mib"] for r in resolved if r["memory_mib"] is not None]
    array = labels.get("array") or {}
    out = {
        "manager": labels["manager"],
        "specified": specified, "assumed": assumed, "flags": flags,
        "slot": "+".join(sorted({r["slot"] for r in resolved})),
        # Manager-neutral totals; None means the script did not say.
        "groups": sum(groups) if groups else None,
        "tasks": sum(r["tasks"] for r in resolved) if tasks_known else None,
        "cores": sum(r["cores"] for r in resolved) if cores_known else None,
        "gpus": sum(r["gpus"] for r in resolved),
        "memory_mib": int(sum(memory)) if memory else None,
        "duration": labels.get("walltime_seconds"),
        "duration_bin": bin_duration(labels.get("walltime_seconds")),
        "exclusive": bool(labels.get("exclusive")),
        "array_count": array.get("count"),
        "jobspec": jobspec(resolved, labels),
    }
    out.update(keys(resolved))
    if not specified:
        out["binned"] = out["exact"] = out["topology"] = "(no resources requested)"
    return out


def dumps(spec):
    return json.dumps(spec, separators=(",", ":"), sort_keys=True)
