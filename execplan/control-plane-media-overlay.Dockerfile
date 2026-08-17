FROM b1-ai-hub/control-plane:deps-582b4c3d

LABEL org.b1.dependency-image-id="sha256:582b4c3d2d5e2a9da29a7e3a0984b22cd699af4f7ea12e6f25e7de1c4c4f99c4" \
      org.b1.live-source-image-id="sha256:324519b9a044c2628965f60ce5b4cb40edc2ed5ee3ef52f0bbb4b61ffe8bf7d8" \
      org.b1.change="scheduler-managed P40 media adapter and interactive-priority guard" \
      org.b1.built-at="2026-08-09"

USER root
RUN rm -rf /app/app
COPY --chown=b1:b1 app /app/app
USER b1
