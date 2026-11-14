# OrvaHR payroll exports service (Go, stdlib only). Static binary, non-root.
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/exports .

FROM alpine:3.22
RUN adduser -S -D -H -u 10001 exports
COPY --from=build /out/exports /usr/local/bin/exports
USER 10001
# Listen on all interfaces inside the container; compose does not publish it.
ENV EXPORTS_BIND=0.0.0.0:8091
EXPOSE 8091
# busybox wget ships with alpine; no curl needed and no health flag in the binary.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8091/healthz"]
ENTRYPOINT ["exports"]
