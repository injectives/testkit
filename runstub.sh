#!/bin/bash
# ./runstub.sh meter/router_adb.script 9000
# ./runstub.sh meter/reader.script 9010
# ./runstub.sh meter/writer.script 9020

docker run \
  --rm \
  -ti \
  -v $(pwd):/vol \
  -w /vol \
  -p $2:$2 \
  python sh -c "python -m pip install -Ur requirements.txt && python -m boltstub -l 0.0.0.0:$2 -t 3600 -v $1"
