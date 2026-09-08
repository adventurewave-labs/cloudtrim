#!/usr/bin/env bash
# CloudTrim Codespaces bootstrap: bring up the full demo stack
# (LocalStack + engine + dashboard) with docker compose.
set -uo pipefail
cd /workspaces/cloudtrim

# Make sure dockerd is up (universal image normally ships it running).
if ! docker info >/dev/null 2>&1; then
  echo "CloudTrim: starting dockerd..."
  sudo bash -c 'nohup dockerd >/tmp/dockerd.log 2>&1 &' || true
  for _ in $(seq 1 45); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
if ! docker info >/dev/null 2>&1; then
  echo "CloudTrim: docker unavailable — run 'docker compose up -d --build' manually."
  exit 0
fi

echo "CloudTrim: building + starting localstack / engine / web (takes a few minutes)..."
COMPOSE="docker compose"
if ! $COMPOSE version >/dev/null 2>&1; then COMPOSE="docker-compose"; fi
$COMPOSE up -d --build

echo "CloudTrim: stack is up. Once containers are healthy, run:"
echo "    make demo        # seed -> audit -> remediate -> verify"
echo "Dashboard: http://localhost:3000"
