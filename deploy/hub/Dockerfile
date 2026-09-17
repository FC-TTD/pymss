# syntax=docker/dockerfile:1
ARG BASE_IMAGE
ARG HUB_IMAGE
FROM ${HUB_IMAGE} AS runtime_package
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG HUB_IMAGE
ARG SOURCE_REV
WORKDIR /app
RUN python -c 'import re,sys; assert all(re.fullmatch(r".+@sha256:[a-f0-9]{64}", v) for v in sys.argv[1:])' "${BASE_IMAGE}" "${HUB_IMAGE}"
COPY --from=runtime_package /opt/hub-runtime-wheel /tmp/hub-runtime-wheel
RUN python -m pip install --no-deps --no-cache-dir /tmp/hub-runtime-wheel/*.whl
# Native source and byte-identical administrator WebUI stay in the pinned base.
COPY hub_runtime /app/hub_runtime
ENV GRADIO_ANALYTICS_ENABLED=False TTD_WEBHOOK_URL=""
LABEL org.opencontainers.image.source="https://github.com/FC-TTD/pymss" org.opencontainers.image.revision="${SOURCE_REV}"
RUN python -m hub_runtime describe > /app/hub_runtime/contract.json
HEALTHCHECK --interval=30s --timeout=3s --start-period=120s --retries=3 CMD curl -fsS http://127.0.0.1:8000/health >/dev/null || exit 1
CMD ["python", "-m", "hub_runtime"]
