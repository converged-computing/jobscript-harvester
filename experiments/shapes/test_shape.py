import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import context  # noqa: E402
import parse  # noqa: E402
import shape  # noqa: E402


def shape_of(manager, text):
    return shape.shape(parse.parse(text, [manager])[manager])


def test_slurm_tasks_per_node():
    s = shape_of("slurm", "#!/bin/bash\n#SBATCH -N 2\n#SBATCH --ntasks-per-node=16\n"
                          "#SBATCH -c 2\n#SBATCH -t 1:00:00\nsrun ./a.out\n")
    assert s["exact"] == "node[2]>slot[16]>core[2]"
    assert (s["groups"], s["tasks"], s["cores"], s["gpus"]) == (2, 32, 64, 0)
    assert s["assumed"] == [] and s["duration_bin"] == "<=1h"
    node = s["jobspec"]["resources"][0]
    assert node["type"] == "node" and node["count"] == 2
    assert node["with"][0]["count"] == 16 and node["with"][0]["with"][0] == {"type": "core", "count": 2}
    assert s["jobspec"]["tasks"][0]["count"] == {"per_slot": 1}


def test_slurm_gpu_per_node_without_tasks_marks_assumptions():
    s = shape_of("slurm", "#!/bin/bash\n#SBATCH --nodes=1\n#SBATCH --gres=gpu:2\n"
                          "#SBATCH --cpus-per-task=8\n#SBATCH --mem=64G\n")
    assert s["exact"] == "node[1]>slot[*]>core[8]+gpu[2]+memory[65536]"
    assert s["binned"] == "node[1]>slot[*]>core[5-8]+gpu[2]+memory[<=64G]"
    assert s["topology"] == "node>slot>core+gpu+memory"
    assert s["assumed"] == ["tasks"] and s["tasks"] is None and s["cores"] == 8


def test_slurm_gpus_that_do_not_divide_use_the_node_as_slot():
    s = shape_of("slurm", "#!/bin/bash\n#SBATCH -N 2\n#SBATCH --ntasks-per-node=4\n"
                          "#SBATCH --gpus-per-node=2\n")
    assert s["slot"] == "group"
    assert s["exact"] == "node[2]>slot[1]>core[4]+gpu[2]"
    assert s["jobspec"]["tasks"][0]["count"] == {"per_slot": 4}
    assert s["gpus"] == 4


def test_slurm_tasks_only_and_mem_per_cpu():
    s = shape_of("slurm", "#!/bin/bash\n#SBATCH -n 8\n#SBATCH --mem-per-cpu=2G\n")
    assert s["exact"] == "slot[8]>core[*]+memory[2048]"
    assert s["groups"] is None and s["memory_mib"] == 16384


def test_nothing_requested():
    s = shape_of("slurm", "#!/bin/bash\n#SBATCH -J x\n#SBATCH -t 10\n")
    assert s["binned"] == "(no resources requested)"
    assert s["cores"] is None and s["tasks"] is None


def test_pbs_select_chunk_is_the_slot():
    s = shape_of("pbs", "#!/bin/bash\n#PBS -l select=2:ncpus=36:mpiprocs=36:ngpus=1:mem=80gb\n"
                        "#PBS -l walltime=02:00:00\n")
    assert s["exact"] == "chunk[2]>core[36]+gpu[1]+memory[81920]"
    assert (s["groups"], s["tasks"], s["cores"], s["gpus"]) == (2, 72, 72, 2)
    assert s["jobspec"]["resources"][0]["type"] == "slot"
    assert s["jobspec"]["tasks"][0]["count"] == {"per_slot": 36}
    assert "pbs_ngpus" in s["flags"]


def test_torque_nodes_ppn():
    s = shape_of("pbs", "#!/bin/bash\n#PBS -l nodes=4:ppn=8,walltime=12:00:00\n")
    assert s["exact"] == "node[4]>slot[8]>core[*]"
    assert s["tasks"] == 32


def test_lsf_tasks_and_ptile_derive_nodes():
    s = shape_of("lsf", "#!/bin/bash\n#BSUB -n 400\n#BSUB -R span[ptile=4]\n#BSUB -W 0:10\n")
    assert s["exact"] == "node[100]>slot[4]>core[*]"
    assert s["duration"] == 600


def test_lsf_gpu_per_host():
    s = shape_of("lsf", "#!/bin/bash\n#BSUB -n 4\n#BSUB -R span[hosts=1]\n#BSUB -gpu num=1\n")
    assert s["slot"] == "group"
    assert s["exact"] == "node[1]>slot[1]>core[4]+gpu[1]"


def test_flux_slots():
    s = shape_of("flux", "#!/bin/bash\n#flux: -N 2\n#flux: -n 8\n#flux: -c 4\n#flux: -g 1\n")
    assert s["exact"] == "node[2]>slot[4]>core[4]+gpu[1]"


def test_oar_hierarchy():
    s = shape_of("oar", "#!/bin/bash\n#OAR -l /nodes=2/core=16,walltime=24:00:00\n")
    assert s["exact"] == "node[2]>slot[*]>core[16]"
    assert s["cores"] == 32 and s["tasks"] is None


def test_cobalt_nodes():
    s = shape_of("cobalt", "#!/bin/bash\n#COBALT -n 128\n#COBALT -t 60\n")
    assert s["binned"] == "node[65-128]>slot[*]>core[*]"


def test_bins():
    assert [shape.bin_count(n) for n in (1, 2, 3, 4, 5, 16, 17, 1024, 1025)] == \
        ["1", "2", "3-4", "3-4", "5-8", "9-16", "17-32", "513-1024", "1025+"]
    assert shape.bin_memory(4096) == "<=4G" and shape.bin_memory(4097) == "<=8G"
    assert shape.bin_duration(None) == "unset" and shape.bin_duration(86400) == "<=24h"


def test_context():
    c = context.context(
        "#!/bin/bash\n#SBATCH -n 4\nmodule purge\nmodule load gromacs/2023.1 intel\nml cuda\n"
        "export OMP_NUM_THREADS=4\ncd $SLURM_SUBMIT_DIR\n"
        "srun -n 4 --cpu-bind=cores gmx_mpi mdrun -deffnm md\n"
        "mpirun -np 16 /opt/lammps/bin/lmp -in in.lj\n"
        "python3.9 train.py && echo done\n$EXE input\n")
    assert c["modules"] == ["cuda", "gromacs", "intel"]
    assert c["launchers"] == ["mpirun", "srun"]
    assert c["programs"] == ["gmx_mpi", "lmp"]
    assert c["commands"] == ["python"]


def test_zero_counts_are_ignored():
    s = shape_of("slurm", "#!/bin/bash\n#SBATCH -N 0\n#SBATCH -n 0\n#SBATCH --gres=gpu:0\n")
    assert s["binned"] == "(no resources requested)"


def test_context_skips_heredocs_arrays_and_joins_continuations():
    c = context.context(
        "#!/bin/bash\nMODELS=(\n  alpha\n  beta\n)\ncat > in.py <<EOF\nimport os\nprint(1)\nEOF\n"
        "srun -n 4 \\\n  --cpu-bind=cores \\\n  ./xhpl\nRscript fit.R\n")
    assert c["programs"] == ["xhpl"]
    assert c["commands"] == ["rscript"]
