#!/bin/csh
#COBALT -n 2 -t 2:00:00 -q full-node
mpirun -np 20 -npernode 10 ./my_app
