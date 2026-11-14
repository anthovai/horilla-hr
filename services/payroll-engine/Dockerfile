# OrvaHR payroll engine (Rust / axum). Rules are bundled at compile time.
FROM rust:1-slim-bookworm AS build
WORKDIR /src

# Dependency layer: build against stub sources so crates are cached until
# Cargo.toml / Cargo.lock change.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && touch src/lib.rs \
    && cargo build --release --locked \
    && rm -rf src target/release/deps/payroll_engine* target/release/deps/libpayroll_engine* \
       target/release/payroll-engine target/release/.fingerprint/orvahr-payroll-engine*

COPY src ./src
COPY rules ./rules
RUN cargo build --release --locked --bin payroll-engine

# Runtime: debian-slim ships bash, which the HEALTHCHECK uses (/dev/tcp), so
# no curl/wget is installed and the Rust code needs no health subcommand.
FROM debian:bookworm-slim
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin engine
COPY --from=build /src/target/release/payroll-engine /usr/local/bin/payroll-engine
USER 10001
# Listen on all interfaces inside the container; compose does not publish it.
ENV BIND=0.0.0.0:8090
EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8090 && printf 'GET /healthz HTTP/1.0\\r\\n\\r\\n' >&3 && head -n1 <&3 | grep -q ' 200'"]
ENTRYPOINT ["payroll-engine"]
