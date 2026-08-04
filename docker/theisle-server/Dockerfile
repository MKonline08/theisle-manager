FROM cm2network/steamcmd:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl tini lib32gcc-s1 lib32stdc++6 && \
    rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/theisle-entrypoint
RUN chmod 0755 /usr/local/bin/theisle-entrypoint && mkdir -p /server && chown -R steam:steam /server

VOLUME ["/server"]
WORKDIR /server
USER steam
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/theisle-entrypoint"]
