# Tiny helper image with `tc` for network fault injection (delay, loss).
FROM alpine:3.21
RUN apk add --no-cache iproute2-tc
ENTRYPOINT ["tc"]
