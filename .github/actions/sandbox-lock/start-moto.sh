#!/usr/bin/env bash
# Start moto (fake AWS) on 127.0.0.1:5555 and create the selftest lock table.
set -euo pipefail
python3 -m venv "$RUNNER_TEMP/moto"
"$RUNNER_TEMP/moto/bin/pip" install --quiet 'moto[server]==5.1.4'
nohup "$RUNNER_TEMP/moto/bin/moto_server" -p 5555 > "$RUNNER_TEMP/moto.log" 2>&1 &
for _ in $(seq 60); do
  if curl -sf http://127.0.0.1:5555/moto-api/ > /dev/null; then
    aws dynamodb create-table --table-name "$SANDBOX_LOCK_TABLE" \
      --attribute-definitions AttributeName=pk,AttributeType=S \
      --key-schema AttributeName=pk,KeyType=HASH --billing-mode PAY_PER_REQUEST > /dev/null
    exit 0
  fi
  sleep 1
done
cat "$RUNNER_TEMP/moto.log"
exit 1
