# syntax=docker/dockerfile:1
ARG BASE_IMAGE
ARG HUB_IMAGE
FROM ${HUB_IMAGE} AS runtime_package
FROM ${BASE_IMAGE} AS native_assets
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG HUB_IMAGE
ARG SOURCE_REV
WORKDIR /app
RUN python -c 'import re,sys; assert all(re.fullmatch(r".+@sha256:[a-f0-9]{64}", v) for v in sys.argv[1:])' "${BASE_IMAGE}" "${HUB_IMAGE}"
# Keep the inherited site-packages untouched; the SDK artifact is already unpacked.
# Refuse a base with existing target files instead of merging SDK versions.
RUN test ! -e /opt/ttd-hub-runtime
COPY --from=runtime_package /opt/ttd-hub-runtime /opt/ttd-hub-runtime
ENV PYTHONPATH=/opt/ttd-hub-runtime
RUN python -c 'import importlib.metadata as m,ttd_model_runtime; from ttd_model_runtime.protocol import validate_provider_metadata; assert m.version("ttd-model-runtime") == "0.3.0"; assert ttd_model_runtime.__file__.startswith("/opt/ttd-hub-runtime/")'
# Install the new core before copying frequently changing application source.
# Keep the pinned base's CUDA stack and administrator WebUI.
COPY pyproject.toml /tmp/pymss-upgrade/pyproject.toml
RUN python -c 'import pathlib,re; text=pathlib.Path("/tmp/pymss-upgrade/pyproject.toml").read_text(); print(re.search(r"\"(pymss-core==[^\"]+)\"", text).group(1))' > /tmp/pymss-upgrade/core.txt
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --no-deps -r /tmp/pymss-upgrade/core.txt
# Keep the native tree completely untouched. BuildKit may expose parts of the
# native application as an overlay that cannot be renamed or deleted; only copy
# the administrator UI assets into an independent fork directory.
WORKDIR /opt/pymss-fork
COPY --link pyproject.toml README.md LICENSE ./
COPY --link pymss ./pymss
COPY --from=native_assets /app/pymss/server/webui_static/ ./pymss/server/webui_static/
COPY --link hub_runtime ./hub_runtime
COPY --link hub_batch ./hub_batch
# Publish matching distribution metadata for plugin requires_pymss checks.
# Install into an independent path without resolving or replacing the pinned
# Torch/CUDA/SDK dependencies inherited from the native image.
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --no-deps --target /opt/pymss-package .
ENV PYTHONPATH=/opt/pymss-package:/opt/ttd-hub-runtime
RUN python -c 'import importlib.metadata as m,pathlib,tomllib,pymss; expected=tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"]; assert m.version("pymss") == expected; assert pymss.__file__.startswith("/opt/pymss-fork/"); assert m.version("pymss-core") == "0.1.14"'
ENV GRADIO_ANALYTICS_ENABLED=False TTD_WEBHOOK_URL=""
LABEL org.opencontainers.image.source="https://github.com/FC-TTD/pymss" org.opencontainers.image.revision="${SOURCE_REV}"
RUN python -m hub_runtime describe > /opt/pymss-fork/hub_runtime/contract.json
HEALTHCHECK --interval=30s --timeout=3s --start-period=120s --retries=3 CMD curl -fsS http://127.0.0.1:8000/health >/dev/null || exit 1
CMD ["python", "-m", "hub_runtime"]
