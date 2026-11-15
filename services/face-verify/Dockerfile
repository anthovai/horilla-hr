# OrvaHR face verification (Rust / axum / tract). Models: models/MODELS.md.
FROM rust:1-slim-bookworm AS build
WORKDIR /src

# Dependency layer: build against stub sources so crates are cached until
# Cargo.toml / Cargo.lock change.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && touch src/lib.rs \
    && cargo build --release --locked \
    && rm -rf src target/release/deps/face_verify* target/release/deps/libface_verify* \
       target/release/face-verify target/release/.fingerprint/orvahr-face-verify*

COPY src ./src
# The copied sources can carry older mtimes than the stub build above, and
# cargo would then reuse the stub: an image that starts and exits 0. Touching
# them forces the real build, and the check below fails the build if the stub
# ever ships again.
RUN touch src/main.rs src/lib.rs \
    && cargo build --release --locked --bin face-verify \
    && grep -q "face-verify listening" target/release/face-verify

# Models: the large upstream ONNX files are fetched and checked against pinned
# SHA-256 sums (a local copy that already matches is reused); the converted
# liveness models come from the build context.
FROM debian:bookworm-slim AS models
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /models
COPY models/ ./
RUN sh ./fetch.sh && rm -f fetch.sh *.part && ls -l

# Runtime: debian-slim ships bash, which the HEALTHCHECK uses (/dev/tcp), so
# no curl/wget is installed and the Rust code needs no health subcommand.
FROM debian:bookworm-slim
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin facesvc
COPY --from=build /src/target/release/face-verify /usr/local/bin/face-verify
COPY --from=models /models /opt/face-verify/models
USER 10001
ENV FACE_MODELS_DIR=/opt/face-verify/models
# Listen on all interfaces inside the container; compose does not publish it.
# FACE_SERVICE_TOKEN must be supplied at run time (the process refuses to start
# without one).
ENV FACE_BIND=0.0.0.0:8092
EXPOSE 8092
# /healthz needs the token like every other route; it is read from the
# container's own environment.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8092 && printf 'GET /healthz HTTP/1.0\\r\\nX-Service-Token: %s\\r\\n\\r\\n' \"$FACE_SERVICE_TOKEN\" >&3 && head -n1 <&3 | grep -q ' 200'"]
ENTRYPOINT ["face-verify"]
