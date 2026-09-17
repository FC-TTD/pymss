# 手工 preview 成果接管

2026-09-17，用户明确授权接管另一管理员维护的PYMSS preview，并建立组织fork与hub分支。

## 归属与来源

- 后端：上游`pymss-project/pymss`，组织fork`FC-TTD/pymss`。
- WebUI：上游`pymss-project/pymss-webui`，组织fork`FC-TTD/pymss-webui`。
- 原部署：edge `/opt/pymss-studio`，Compose `pymss-studio-preview`，实际容器
  `19b4f49b4c88315024ea47db88c2a22a99a2ce6180bc446a3d014e3b95a9594a`。
- 原目录无Git元数据；后端121个源码/测试文件中116个与上游候选提交
  `9bdcad4aa883ac4fa2047f0a68488d239173be38`相同，不能声称这就是精确部署来源。
  现场快照作为实际运行权威已完整保留，首个接管提交`ba46145`。
- 106个运行容器内Python/catalog文件与接管后的源码逐个SHA256一致；
  [原文件hash](runtime-source-hashes.json)、[上游对比](source-comparison.json)。
- 原固定镜像image ID `sha256:c6631ca77499b7dcddf6af44945bfe4b66357a513e92613ba068364adc042e56`，
  已保存为`registry.ttd/pymss/native@sha256:a5bb212a097c35e9d99cb6eefd33323aaa82717530992976c282ef61cd9b672a`。

WebUI源码最接近原上游初始提交`394743695cd8a0f85001dfa3b30a0f16e342afd3`。
管理员定制已在独立UI fork/hub保存为`fcbd50b`；catalog检查脚本增加显式provider
源码路径后为`04504f6`。从接管UI源重新npm ci/build得到的三个主制品与线上字节相同：

| 产物 | SHA256 |
|---|---|
| index.html | 5c45360da0e7e5be982134b99b18e1bd8c9d72da5ac08e2ed826c92ef973d8f9 |
| index-Dl_TdLG6.js | 298d05b28d50da198a13d783b7331e6615cae35de36a27d8d2a7545ad0f8b853 |
| index-C2aaIIOh.css | 9d06226c61bbbb37cacf94fcb2d025784660ff98e7de5a0f48489067d8aeb88e |

管理员成果包括48kHz/PCM24 WAV、JSON各stem文件、模型checkpoint/config兼容、
中文场景选择、模型说明、批量下载与分轨预览。原Dockerfile/Compose/部署报告
保留为历史来源，正式纳管资产另在deploy/hub。

## 实际原生基线

PYMSS2.0.6、Torch2.7.1+cu128、FastAPI0.138.2、Pydantic2.11.10、NumPy2.2.6，
保留固定基础镜像。当前选中`BS-Roformer-Resurrection.ckpt`，非旧启动命令中的
`1_HP-UVR`。原max_queue_size1、max_audio600秒、max_request536870912字节、timeout0。
原`inference_lock`与load/download锁保留；不提高native并发。

20.143秒真实音频：JSON默认与ZIP均返回48kHz/24-bit双声道两轨；并发提交两次，
一次200、一次原429 server_overloaded。实际峰值2832MiB（当前模型/输入，不是
352模型总预算）。Python3.11 wave模块不认识FFmpeg产生的WAVE_FORMAT_EXTENSIBLE，
初次验证器失败，保留失败记录；改用ffprobe核验实际PCM24后新命名场景通过，
未把验证器错误算为模型失败或重放同一请求。

原模型权重/config与管理员手工补齐文件在`/TTD-Data/pymss/models`，
输出挂载`/TTD-Data/pymss/outputs`，不删除或重新生成覆盖。HTTP分离结果主要在内存
以JSON/base64或ZIP交付；浏览器生成下载文件，不假定服务端已有持久任务队列。

## 受管边界与交接

SDK loader只在GPU子进程返回NativeBackend；第一次真实推理/显式load才加载原
separator。CPU state保留模型选择和inference_params，换卡/驱逐后重建。
原路由/schema、鉴权、错误/编码器和静态UI保持。真实load/infer通过Runtime.task
settle，HTTP断连或超时不能提前放开原生锁、释放未完成GPU活动。

既有CPU Cover预处理容器使用`PYMSS_UPSTREAM=pymss-studio.:7860`。交接后
该overlay DNS别名转交CPU model-entry，并保留7860入口；业务仍经过受管准入。
外部原友好URL保持`http://pymss-studio`。不为兼容旧消费者重建裸GPU直连口，
也不修改Cover UI/音频逻辑。只在旧实例退出后添加别名，禁止旧新同时分流。

原生preview还有Caddy之外的Cover内部直连。Caddy临时503 guard只关闭友好URL新请求；
最终SIGTERM由tini交给Uvicorn，等待已接纳HTTP/native线程结束并正常exit0，
再确认旧GPU归属消失。不能把一次利用率为0当作已排空，更不能强杀当成成功。
节点仍仅worker，暂停的Turkey维护归属保持，不因新增服务重开它。

本地10项测试在原固定镜像、无网络/无GPU环境通过，包括真实ASGI、SDK task
取消settle、原429、跨驱逐选模保持、切模失败、鉴权、PCM24编码及真实SDK子进程
ndarray往返。实际GPU/API/UI/原域名与Cover验收待交接报告补充，不能以这些CPU
fixture替代上线验收。
