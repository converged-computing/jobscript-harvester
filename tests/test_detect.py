import pathlib

import pytest

from detect import detect, managers

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def read(name):
    return (FIXTURES / name).read_text()


def test_slurm_doc_example():
    assert managers(detect(read("slurm_sbatch_doc.sh"))) == ["slurm"]


def test_pbs_doc_example():
    counts = detect(read("pbs_openpbs_doc.py"))
    assert counts == {"pbs_directive": 2}


def test_lsf_ignores_disabled_and_inline_mentions():
    counts = detect(read("lsf_dtu_array.sh"))
    # 9 active "#BSUB" lines; "##BSUB -u" and "# ... start with #BSUB" are not directives.
    assert counts == {"lsf_directive": 9}


@pytest.mark.parametrize("path", sorted((FIXTURES / "flux_valid").iterdir()) +
                         sorted((FIXTURES / "flux_invalid").iterdir()), ids=lambda p: p.name)
def test_flux_rfc36_fixtures_detected(path):
    counts = detect(path.read_text())
    assert counts.get("flux_directive", 0) + counts.get("flux_bare_directive", 0) > 0


def test_flux_docstring_directives_are_bare():
    counts = detect(read("flux_valid/002-python.py"))
    assert "flux_directive" not in counts and counts["flux_bare_directive"] > 0
    assert managers(counts) == []


def test_yaml_key_is_not_confident():
    counts = detect("spec:\n  flux:\n    view: latest\n")
    assert managers(counts) == []


def test_flux_command_is_a_signal_not_a_manager():
    counts = detect("#!/bin/bash\nflux run -N2 -n4 ./app\n")
    assert counts == {"flux_command": 1} and managers(counts) == []
    assert detect("echo flux run is mentioned here\n") == {}


def test_flux_sentinel_requires_option_argument():
    # Lines from flux-core C sources that match the RFC 36 sentinel regex.
    assert detect('                    "flux::subprocess::sign",\n') == {}
    assert detect("# flux: # comment only\n") == {}
    assert managers(detect("# flux: -N4\n")) == ["flux"]


def test_cobalt_doc_example():
    assert detect(read("cobalt_alcf_doc.csh")) == {"cobalt_directive": 1}


def test_oar_doc_example():
    assert detect(read("oar_grid5000_doc.sh")) == {"oar_directive": 6}
