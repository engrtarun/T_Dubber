# edge-server: the Hugging Face Space image.
#
# WHY THIS IS A SEPARATE, TINY IMAGE
# ----------------------------------
# The repo's main Dockerfile builds CUDA, PyTorch, vLLM and a Rust toolchain. The
# Space needs none of that: it has no GPU, runs no model, and serves files. So
# this image is built from the same source with a stage that compiles a single
# dependency-free Go binary against a static base.
#
# The result is a few tens of megabytes instead of tens of gigabytes, which means
# a Space cold start is seconds rather than minutes -- and a cold start is the
# single most important property here, because the entire caching benefit is lost
# if the Space takes five minutes to answer the first request.
#
# WHY CGO IS OFF
# --------------
# A static binary needs no libc in the image at all, so the runtime stage can be
# a minimal base with nothing but the one executable. That is the smallest
# attack surface a public endpoint can have: there is no shell, no package
# manager, no interpreter, and nothing to exec but the server itself.
#
# WHY SCRATCH-ISH RATHER THAN distroless
# -------------------------------------
# distroless would also work and would add a CA bundle and a passwd file. The
# service makes no TLS connections (it is the TLS termination point) and runs as
# whatever uid the platform assigns, so neither is needed. The tradeoff is
# documented rather than silently made: if outbound HTTPS is ever added here,
# switch to the distroless base in the comment below.

# ---------------------------------------------------------------------------
# Stage 1: build
# ---------------------------------------------------------------------------
FROM golang:1.25-bookworm AS build

WORKDIR /src

# Dependencies first, as their own layer. edge/ imports nothing outside the
# standard library, so there is no go.sum to fetch -- but copying the module
# files ahead of the sources keeps this stage correct if that ever changes,
# because a new import then invalidates only the COPY, not the toolchain layer.
COPY go.mod ./
# Deliberately no COPY go.sum ./ : this package has no module dependencies, and
# a `go mod download` here would fail on the missing go.sum rather than
# succeeding with nothing to do. The build below runs with -mod=mod and the
# package set is asserted by `go vet` in CI.

COPY edge/ ./edge/
COPY cmd/edge-server/ ./cmd/edge-server/

# CGO_ENABLED=0 produces a binary with no dynamic loader. Without it the binary
# works on glibc and not on musl, and the Space base is neither, so the failure
# would appear only at runtime as "no such file or directory" for a 9 MB
# executable that exists.
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
        -trimpath \
        -ldflags="-s -w" \
        -o /out/edge-server \
        ./cmd/edge-server

# The artifact tree ships empty. A Space's persistent volume is mounted at
# runtime and is what actually holds the multi-gigabyte artefacts; baking them
# into the image would multiply the image size by the payload and make every
# push a full upload. Publishing is a CI step that writes into the volume, not a
# build step.
RUN mkdir -p /out/artefacts

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
# If outbound HTTPS is added to this service, change this to:
#   FROM gcr.io/distroless/static-debian12:nonroot
# which supplies a CA bundle and a non-root user. See the note above.
FROM debian:bookworm-slim AS runtime

# A TLS trust store is included even though the server makes no outbound TLS
# calls: it costs ~2 MB and it means a future HTTPS feature does not silently
# fail on every certificate. curl is intentionally NOT installed -- the image
# serves bytes and nothing else, and a shell in a public endpoint is a liability.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /out/edge-server /usr/local/bin/edge-server

# The artefact root is a volume. Declaring it here means HF mounts its persistent
# storage over exactly this path, and `docker run` without a volume still works
# (the server creates the directory if absent).
VOLUME ["/data"]

ENV EDGE_ROOT=/data \
    EDGE_ADDR=:7860 \
    EDGE_LOG_FORMAT=json \
    EDGE_BUILD_MANIFEST=true \
    PORT=7860

# HF Spaces routes traffic to 7860 and considers the container unhealthy if the
# port does not answer, so the readiness check is the real /healthz endpoint
# rather than a TCP connect: a listener that is up but wedged on a multi-
# gigabyte manifest build should be reported as not ready.
# The probe is the same binary with -self-check, so the image needs no curl and no
# shell for a health check. start-period covers the initial manifest build, which
# hashes the whole artefact tree and on a multi-gigabyte volume is not instant.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/usr/local/bin/edge-server", "-self-check"]

EXPOSE 7860

# exec form, so the binary is PID 1 and receives SIGTERM directly. Without this
# a shell wrapper would swallow the signal and the Space's graceful drain in
# ListenAndServe would never run.
ENTRYPOINT ["/usr/local/bin/edge-server"]