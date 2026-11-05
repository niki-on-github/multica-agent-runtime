# Test app for the smoke test: a page served by busybox httpd on :8080 (busybox-extras since Alpine 3.x).
FROM docker.io/library/alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
RUN apk add --no-cache busybox-extras
COPY index.html /www/index.html
EXPOSE 8080
CMD ["busybox-extras", "httpd", "-f", "-p", "8080", "-h", "/www"]
