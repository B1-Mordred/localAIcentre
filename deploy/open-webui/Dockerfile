FROM ghcr.io/open-webui/open-webui:v0.10.2@sha256:9fcea9c6e32ab60b0498f3986c6cdf651ddbe61db48d2213a3d28048ddd673d4

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY b1-open-webui-entrypoint.sh /usr/local/bin/b1-open-webui-entrypoint.sh
RUN chmod 0555 /usr/local/bin/b1-open-webui-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/b1-open-webui-entrypoint.sh"]
CMD ["bash", "start.sh"]
