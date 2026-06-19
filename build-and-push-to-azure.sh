# Need to be logged in to ACR when running this.
#
# Usage: ./build-and-push-to-azure.sh <registry> [image:tag]
#   registry - ACR registry name (e.g., ivritaicontainers)
#   image:tag - Image name and tag (default: transcriptor-worker:latest)
#
# NOTE: ACR Tasks' default builder does NOT enable BuildKit, so the Dockerfile
# avoids BuildKit-only features (`# syntax=` directive and `RUN --mount=...`
# cache mounts). It builds fine on the classic builder.

REGISTRY="${1:?Usage: $0 <registry> [image:tag]}"
IMAGE="${2:-transcriptor-worker:latest}"

az acr build \
  --registry "$REGISTRY" \
  --platform linux/amd64 \
  --image "$IMAGE" .