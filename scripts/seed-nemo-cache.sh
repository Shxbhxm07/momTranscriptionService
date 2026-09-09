#!/usr/bin/env bash
# Seed the nemo-cache volume with TitaNet + MarbleNet VAD weights (~150 MB).
#
# WHY THIS EXISTS: NeMo downloads its models lazily, on the FIRST diarization request.
# On an air-gapped box that request fails. Run this once, while you still have a network
# (or a machine that already has the models), and diarization is offline from then on.
#
#   ./scripts/seed-nemo-cache.sh                  # copy from an existing cache if present
#   ./scripts/seed-nemo-cache.sh <source-volume>  # copy from a specific docker volume
set -euo pipefail

TARGET="${TARGET_VOLUME:-offline-mom-api_nemo-cache}"
SOURCE="${1:-mom-ai_nemo-cache}"

docker volume create "$TARGET" >/dev/null

if docker volume inspect "$SOURCE" >/dev/null 2>&1; then
  echo "Copying NeMo models from volume '$SOURCE' → '$TARGET' ..."
  docker run --rm -v "$SOURCE":/from -v "$TARGET":/to alpine \
    sh -c 'cd /from && cp -a . /to/'
  echo "Done. Contents:"
  docker run --rm -v "$TARGET":/c alpine sh -c 'du -sh /c; find /c -name "*.nemo"'
else
  cat <<MSG
Source volume '$SOURCE' not found.

Nothing to copy from, so the models will be downloaded on the first diarization instead.
That needs internet ONCE. To do it now rather than on a live request, start the stack
with a network and send any audio file through /transcribe-and-generate-mom; after that
the volume is populated and the box can go air-gapped.
MSG
fi
