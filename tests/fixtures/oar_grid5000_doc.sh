#!/bin/bash
#OAR -q production
#OAR -l host=1/gpu=1
#OAR -l walltime=3:00:00
#OAR -p gpu-16GB AND gpu_compute_capability_major>=5
#OAR -O OAR_%jobid%.out
#OAR -E OAR_%jobid%.err
