FROM quay.io/go-skynet/local-ai-backends:sha-0a8a7fb-gpu-nvidia-cuda-12-llama-cpp@sha256:01f3d24a07507d6ec5caeab3052599c1ec5a3ae45552cf17d81a845b3123ed04 AS localai_llama_cpp_backend

FROM localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973

ARG B1_LOCALAI_UPSTREAM_VERSION=v4.7.1-gpu-nvidia-cuda-12
ARG B1_LOCALAI_UPSTREAM_COMMIT=b224c96db6f4b87306a33a808650bfce63b12588
ARG B1_LOCALAI_LLAMA_CPP_BACKEND_IMAGE=quay.io/go-skynet/local-ai-backends:sha-0a8a7fb-gpu-nvidia-cuda-12-llama-cpp@sha256:01f3d24a07507d6ec5caeab3052599c1ec5a3ae45552cf17d81a845b3123ed04
ARG B1_LOCALAI_UID=999
ARG B1_LOCALAI_GID=999

LABEL org.opencontainers.image.title="B1 AI Hub LocalAI runtime wrapper" \
      org.opencontainers.image.description="Pinned LocalAI CUDA runtime with B1 scheduler lifecycle hooks" \
      org.opencontainers.image.source="https://github.com/B1-Mordred/localAIcentre" \
      b1.localai.upstream.version="${B1_LOCALAI_UPSTREAM_VERSION}" \
      b1.localai.upstream.commit="${B1_LOCALAI_UPSTREAM_COMMIT}" \
      b1.localai.llama-cpp-backend.image="${B1_LOCALAI_LLAMA_CPP_BACKEND_IMAGE}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    B1_LOCALAI_PUBLIC_HOST=0.0.0.0 \
    B1_LOCALAI_PUBLIC_PORT=8080 \
    B1_LOCALAI_UPSTREAM_ADDRESS=127.0.0.1:18080 \
    B1_LOCALAI_UPSTREAM_URL=http://127.0.0.1:18080 \
    B1_LOCALAI_UPSTREAM_VERSION="${B1_LOCALAI_UPSTREAM_VERSION}" \
    B1_LOCALAI_UPSTREAM_COMMIT="${B1_LOCALAI_UPSTREAM_COMMIT}" \
    B1_LOCALAI_UPSTREAM_IMAGE="localai/localai:v4.7.1-gpu-nvidia-cuda-12@sha256:b55bba84712cb1893cd59faf9ebb55fc4fd15a36df698c30a51a8ba62720b973" \
    B1_LOCALAI_LLAMA_CPP_BACKEND_IMAGE="${B1_LOCALAI_LLAMA_CPP_BACKEND_IMAGE}" \
    LOCALAI_BACKENDS_SYSTEM_PATH=/opt/b1/localai/backends

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=localai_llama_cpp_backend /lib /opt/b1/localai/backends/llama-cpp/lib
COPY --from=localai_llama_cpp_backend /llama-cpp-fallback /opt/b1/localai/backends/llama-cpp/llama-cpp-fallback
COPY --from=localai_llama_cpp_backend /llama-cpp-grpc /opt/b1/localai/backends/llama-cpp/llama-cpp-grpc
COPY --from=localai_llama_cpp_backend /llama-cpp-rpc-server /opt/b1/localai/backends/llama-cpp/llama-cpp-rpc-server
COPY --from=localai_llama_cpp_backend /run.sh /opt/b1/localai/backends/llama-cpp/run.sh

COPY b1-localai-entrypoint.sh /usr/local/bin/b1-localai-entrypoint.sh
COPY b1_localai_config.py /usr/local/bin/b1_localai_config.py
COPY b1_localai_proxy.py /usr/local/bin/b1_localai_proxy.py

RUN ln -s llama-cpp /opt/b1/localai/backends/cuda12-llama-cpp \
    && mkdir -p /srv/b1-ai-hub/models /srv/b1-ai-hub/localai/backends /srv/b1-ai-hub/localai/configuration /srv/b1-ai-hub/localai/data /srv/b1-ai-hub/cache \
    && if ! getent group "${B1_LOCALAI_GID}" >/dev/null; then groupadd --system --gid "${B1_LOCALAI_GID}" b1; fi \
    && if ! getent passwd "${B1_LOCALAI_UID}" >/dev/null; then useradd --system --uid "${B1_LOCALAI_UID}" --gid "${B1_LOCALAI_GID}" --home-dir /srv/b1-ai-hub/localai --shell /usr/sbin/nologin b1; fi \
    && chown -R "${B1_LOCALAI_UID}:${B1_LOCALAI_GID}" /srv/b1-ai-hub \
    && chmod 0755 /usr/local/bin/b1-localai-entrypoint.sh /usr/local/bin/b1_localai_config.py /usr/local/bin/b1_localai_proxy.py

USER ${B1_LOCALAI_UID}:${B1_LOCALAI_GID}
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --retries=5 --start-period=60m \
    CMD curl -fsS http://127.0.0.1:8080/readyz || exit 1

ENTRYPOINT ["tini", "--", "b1-localai-entrypoint.sh"]
