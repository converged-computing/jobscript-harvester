import pathlib

import pytest

import parse

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def read(name):
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize("text,seconds", [
    ("30", 1800), ("30:15", 1815), ("2:00:00", 7200), ("1-12", 129600),
    ("1-12:30", 131400), ("1-0:0:5", 86405), ("abc", None)])
def test_slurm_time_formats(text, seconds):
    # Formats listed under sbatch --time
    assert parse.slurm_seconds(text) == seconds


@pytest.mark.parametrize("spec,count", [
    ("0-15", 16), ("0,6,16-32", 19), ("0-15:4", 4), ("0-15%4", 16), ("x", None)])
def test_array_counts(spec, count):
    # Examples from sbatch --array
    assert parse.index_count(spec) == count


def test_slurm_tokenizer_matches_sbatch_rules():
    # get_argument: quotes group, backslash escapes, unescaped '#' ends the line
    assert parse.slurm_tokens(' --job-name="a b" -N 2 # two nodes') == ["--job-name=a b", "-N", "2"]
    assert parse.slurm_tokens(r" --comment=a\#b") == ["--comment=a#b"]
    with pytest.raises(ValueError):
        parse.slurm_tokens(' --job-name="oops')


def test_slurm_stops_at_first_command():
    text = "#!/bin/bash\n#SBATCH -N 2\n\n# note\nsrun hostname\n#SBATCH -N 8\n"
    assert parse.parse_slurm(text)["nodes"] == {"min": 2, "max": 2}


def test_slurm_doc_example_fixture():
    labels = parse.parse_slurm(read("slurm_sbatch_doc.sh"))
    assert labels["walltime_seconds"] == 60 and labels["errors"] == []


def test_slurm_fields():
    text = ("#!/bin/bash\n#SBATCH --nodes=2-4\n#SBATCH --ntasks-per-node 8\n"
            "#SBATCH -c4\n#SBATCH --gres=gpu:a100:2\n#SBATCH --mem=16G\n"
            "#SBATCH -t 1-0\n#SBATCH -p pbatch -A bank\n#SBATCH --exclusive\n"
            "#SBATCH --array=1-10%2\n#SBATCH --mail-user=x@y.org\n#SBATCH --ntasks-per-n=3\n")
    labels = parse.parse_slurm(text)
    assert labels["nodes"] == {"min": 2, "max": 4}
    # unambiguous long-option prefix resolves like getopt_long
    assert labels["tasks_per_node"] == 3
    assert labels["cpus_per_task"] == 4
    assert labels["gpus"] == [{"count": 2, "per": "node", "type": "a100"}]
    assert labels["memory"] == [{"mib": 16384.0, "per": "node"}]
    assert labels["walltime_seconds"] == 86400
    assert (labels["queue"], labels["account"]) == ("pbatch", "bank")
    assert labels["exclusive"] and labels["array"] == {"spec": "1-10%2", "count": 10}
    assert labels["email"] == "x@y.org"


def test_slurm_gpu_forms_and_totals():
    labels = parse.parse_slurm("#SBATCH -N 2\n#SBATCH --gpus-per-node=4\n")
    assert parse.totals(labels)["gpus"] == 8
    labels = parse.parse_slurm("#SBATCH --gpus=volta:3\n")
    assert labels["gpus"] == [{"count": 3, "per": "job", "type": "volta"}]
    assert parse.parse_slurm("#SBATCH --gres=gpu\n")["gpus"][0]["count"] == 1


def test_slurm_hetjob_counts_components():
    labels = parse.parse_slurm("#SBATCH -N 1\n#SBATCH hetjob\n#SBATCH -N 4\n")
    assert labels["components"] == 2 and labels["nodes"] == {"min": 1, "max": 1}


def test_pbs_openpbs_fixture():
    labels = parse.parse_pbs(read("pbs_openpbs_doc.py"))
    assert labels["chunks"] == [{"count": 1, "resources": {"ncpus": "3", "mem": "1gb"}}]
    assert labels["memory"] == [{"mib": 1024.0, "per": "chunk"}]
    assert labels["job_name"] == "HelloJob"


def test_pbs_select_mpiprocs_total_and_walltime():
    text = "#PBS -l select=2:ncpus=8:mpiprocs=8+1:ncpus=4:mpiprocs=2,walltime=1:30:00\n#PBS -q debug\n"
    labels = parse.parse_pbs(text)
    assert parse.totals(labels)["tasks"] == 18
    assert labels["walltime_seconds"] == 5400 and labels["queue"] == "debug"


def test_pbs_torque_nodes_ppn_and_array():
    # Torque: nodes={count|hostname}[:ppn=N][:gpus=G][+...]
    labels = parse.parse_pbs("#PBS -l nodes=2:ppn=8:gpus=1,walltime=1:00:00\n#PBS -t 0-4,6\n")
    assert labels["nodes"] == {"min": 2, "max": 2} and labels["tasks_per_node"] == 8
    assert parse.totals(labels) | {} == parse.totals(labels)
    assert parse.totals(labels)["tasks"] == 16 and parse.totals(labels)["gpus"] == 2
    assert labels["array"]["count"] == 6


def test_pbs_stops_at_executable_line_and_follows_continuation():
    text = "#!/bin/bash\n#PBS -N a \\\n -q b\necho hi\n#PBS -q late\n"
    labels = parse.parse_pbs(text)
    assert labels["job_name"] == "a" and labels["queue"] == "b"


def test_lsf_fixture_and_lassen_style():
    labels = parse.parse_lsf(read("lsf_dtu_array.sh"))
    assert labels["tasks"] == 4 and labels["walltime_seconds"] == 3 * 3600
    assert labels["array"] == {"spec": "1-25", "count": 25}
    assert labels["memory"] == [{"mib": 4096.0, "per": "slot"}]
    # Lassen: -nnodes N, -W minutes
    lassen = parse.parse_lsf("#BSUB -nnodes 4\n#BSUB -W 30\n#BSUB -q pbatch\n")
    assert lassen["nodes"] == {"min": 4, "max": 4} and lassen["walltime_seconds"] == 1800


def test_cobalt_fixture():
    labels = parse.parse_cobalt(read("cobalt_alcf_doc.csh"))
    assert labels["nodes"] == {"min": 2, "max": 2}
    assert labels["walltime_seconds"] == 7200 and labels["queue"] == "full-node"
    assert parse.parse_cobalt("#COBALT -t 30\n")["walltime_seconds"] == 1800


def test_oar_fixture():
    labels = parse.parse_oar(read("oar_grid5000_doc.sh"))
    assert labels["nodes"] == {"min": 1, "max": 1}
    assert labels["gpus"] == [{"count": 1, "per": "node", "type": None}]
    assert labels["walltime_seconds"] == 3 * 3600 and labels["queue"] == "production"


def test_flux_fixtures():
    labels = parse.parse_flux(read("flux_valid/001-simple.sh"))
    assert labels["nodes"] == {"min": 4, "max": 4}
    assert labels["queue"] == "batch" and labels["job_name"] == "app001"
    noop = parse.parse_flux(read("flux_valid/010-noop.sh"))
    assert noop["slots"] == 32


@pytest.mark.parametrize("text,seconds", [("30", 1800), ("30s", 30), ("1.5h", 5400), ("2d", 172800)])
def test_flux_time_limit(text, seconds):
    assert parse.flux_seconds(text) == seconds


def test_parse_dispatches_on_detected_managers():
    out = parse.parse("#!/bin/bash\n#SBATCH -N 1\n#PBS -q x\n", ["slurm", "pbs"])
    assert set(out) == {"slurm", "pbs"}


def test_slurm_aliases_from_source_table():
    # slurm_opt.c: --tasks-per-node is a deprecated alias of --ntasks-per-node
    labels = parse.parse_slurm("#SBATCH --tasks-per-node=6\n#SBATCH --cluster=c1\n")
    assert labels["tasks_per_node"] == 6 and labels["unknown"] == []
    assert labels["options"][1]["option"] == "--clusters"


def test_ignored_directive_lines_after_first_command():
    # sbatch stops reading directives at the first command line
    text = 'echo "Use job_runner instead"\nexit\n#!/bin/bash\n#SBATCH --nodes=1\n#SBATCH --time=96:00:00\n'
    labels = parse.parse(text, ["slurm"])["slurm"]
    assert labels["nodes"] is None and labels["ignored_directive_lines"] == 2
