FROM ghcr.io/open-webui/open-webui:v0.11.0@sha256:21a1ece1e32d1c79681c8d6b36a6f2f54abeb96d0f648965d793265dbef541f6

COPY b1-open-webui-entrypoint.sh /usr/local/bin/b1-open-webui-entrypoint.sh
COPY b1_cancel_bridge.py /app/backend/open_webui/b1_cancel_bridge.py
COPY patch_tasks.py /tmp/b1-patch-open-webui-tasks.py
RUN chmod 0555 /usr/local/bin/b1-open-webui-entrypoint.sh \
    && python /tmp/b1-patch-open-webui-tasks.py \
    && rm /tmp/b1-patch-open-webui-tasks.py \
    && chgrp -R 999 /app/backend/open_webui/static \
    && chmod -R g+rwX /app/backend/open_webui/static

ENTRYPOINT ["/usr/local/bin/b1-open-webui-entrypoint.sh"]
CMD ["bash", "start.sh"]
