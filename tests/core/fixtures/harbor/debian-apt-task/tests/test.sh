#!/bin/sh
mkdir -p /logs/verifier
apt-get update -qq && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt
