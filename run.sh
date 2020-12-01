#!/bin/bash
. venv/bin/activate
./volclean.py --region eu-west-1 -t Name:^kubernetes-dynamic-pvc --age 7 -y

./volclean.py --region eu-west-1 -t OS_Version:^Ubuntu -R ami --age 365 -v -y
./volclean.py --region eu-west-1 -t OS_Version:^Ubuntu -R snapshots --age 365 -v -y
