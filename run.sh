#!/bin/bash
. venv/bin/activate
./volclean.py --region eu-west-1 -t Name:^kubernetes-dynamic-pvc --age 7
