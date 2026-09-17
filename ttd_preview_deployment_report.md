# Model Preview Deployment Report

## Basic

- repo_path: `/opt/pymss-studio` on `ttd-edge`
- project_name: `pymss-studio`
- preview_mode: `reuse-native-ui`
- preview_runtime_strategy: `docker-preview`
- preview_validation_level: `real-inference-ok`
- recommended_entry: `pymss serve 1_HP-UVR --webui`
- ui_strategy: `native-webui`
- next_skills: none

## Environment Strategy

- uv_strategy: not used at runtime; Docker isolates Python and CUDA dependencies
- local_env_path: none
- package_manager_source: pip editable install inside Docker image, with official `pymss-webui` built by npm
- python_version_strategy: PyTorch CUDA 12.8 runtime image with Python from the base image
- gpu_binary_conflict_risk: high
- global_pollution_risk: low
- fallback_strategy: stay Docker

## Runtime

- launch_command: `cd /opt/pymss-studio && docker compose -f compose.preview.yaml up -d --build`
- required_env_files: none
- required_env_vars: `NVIDIA_VISIBLE_DEVICES=2`, `PYMSS_MODEL_DIR=/models`
- model_download_strategy: default startup model `1_HP-UVR`; missing catalog files download to `/TTD-Data/pymss/models`
- model_weight_path: `/TTD-Data/pymss/models`
- weight_reuse_status: project-local reusable cache on target host
- output_path_strategy: `/TTD-Data/pymss/outputs`
- cache_path_strategy: `/TTD-Data/pymss/models`

## UI

- existing_ui_entry: `pymss serve --webui`, static assets from `https://github.com/pymss-project/pymss-webui`
- gradio_wrapper_needed: no
- wrapper_target: none
- localization_status: WebUI primary navigation and operating screens localized to Chinese
- catalog_curation_status: Catalog upgraded to a Chinese business selection desk with `场景选型` and `高级筛选`; advisory metadata covers all 352 catalog models, including 53/53 `bs_mega_53stem_*` entries, and merges real-time local/download status from the API
- audio_output_strategy: WebUI response format labels are `分轨` and `zip`; both modes encode every selected stem as DAW-compatible `48000Hz PCM_24 WAV`

## Risks

- confirmed_risks: first model load may download external model files if `/TTD-Data/pymss/models` is empty
- unknowns: long songs and larger RoFormer models may require more GPU memory than the default VR smoke model
- user_decisions_needed: none

## Attempt Log

- attempt_log: built Docker preview around official `pymss` server and official `pymss-webui` static frontend; constrained `typing-extensions==4.14.0` to avoid a conda package uninstall failure in the PyTorch base image.
- attempt_log: deployed on `ttd-edge` port `17867`, bound Docker DeviceRequests to physical GPU `2`, downloaded and loaded `1_HP-UVR.pth` into `/TTD-Data/pymss/models`.
- attempt_log: verified `/health`, official WebUI GET `/ui/`, container CUDA visibility, and a real `/v1/audio/separations` request that returned `0001-instrumental.wav`, `0002-vocals.wav`, and `manifest.json`.
- attempt_log: localized PYMSS WebUI, added curated model notes from the CSV attachments and the follow-up `模型名称,分类,精华备注` supplement, and redeployed the rebuilt static frontend.
- attempt_log: downloaded all supplement concrete models and all 53 `bs_mega_53stem_*_mvsep.ckpt` series models. `model_bandit_plus_dnr_sdr_11.47.ckpt` and `aufr33-jarredou_DrumSep_model_mdx23c_ep_141_sdr_10.8059.ckpt` required manual `.yaml` completion because remote YAML sizes differed from the source index; Catalog now reports both complete.
- attempt_log: post-change validation passed: `/health` ok, container healthy, 23 supplement concrete models complete, 53/53 `bs_mega_53stem_*` complete, and deployed UI JS contains the Chinese recommended-filter and supplement note strings.
- attempt_log: implemented full WebUI model-selection upgrade from `model_catalog.json`: generated `model-advisory-index.ts`, added scenario recommendations, advanced Chinese filters, full details metadata, Loaded/Separation advisory notes, and JSON-mode per-stem preview from `pcm_f32le`.
- attempt_log: changed Docker preview pip install to use cached/resumable downloads and fixed binary dependency versions for `scikit-learn`, `scipy`, `numba`, and `llvmlite` after PyPI network retries caused resolver backtracking into source builds.
- attempt_log: final validation passed on `ttd-edge`: container healthy, `/health` ok, Catalog API returns 352 models, deployed UI JS contains `场景选型`, `高级筛选`, `pcm_f32le`, `临时生成 WAV`, and `巨无霸模型`; container CUDA reports `NVIDIA_VISIBLE_DEVICES=2`, `cuda=True`, `device_count=1`.
- attempt_log: updated DAW output path after `pcm_f32le` import incompatibility: WebUI now defaults to ZIP + WAV only, server resamples final WAV stems to 48000Hz and forces PCM_24 encoding.
- attempt_log: real separation smoke passed with loaded `1_HP-UVR.pth`: ZIP response returned `0001-instrumental.wav` and `0002-vocals.wav`; manifest and WAV headers both report 48000Hz, stereo, 24-bit PCM (`sample_width_bytes=3`).
- attempt_log: updated split-track JSON mode: `response_format=json&output_audio_format=wav` now returns each stem as base64-encoded `48000Hz PCM_24 WAV`; WebUI preview uses that WAV directly, and per-stem download saves `.wav`. Validated JSON and ZIP modes sequentially because the inference queue size is 1.
- attempt_log: added the merged `分离工作台` main view as the default landing screen. It nests flattened scenario/catalog model picking, selected-model confirmation, download/load actions, stem selection, audio upload, direct separation, and per-stem preview/download in one window. Workbench typography and select controls were increased by one size for easier model selection.
- current_verdict: preview-ready at `http://ttd-edge:17867/ui/`
