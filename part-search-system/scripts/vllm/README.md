# vLLM / Ollama 双引擎大模型服务 — 操作文档

本系统同时支持 **Ollama**（Windows 侧，单请求串行、稳定兜底）与 **vLLM**（WSL2 侧，多用户并发、高吞吐）两套推理引擎，由后端引擎管理器（`backend/llm_engine.py`）统一调度，支持后台一键无缝切换、故障自动转移、看门狗自动恢复、显存动态适配。

---

## 1. 架构总览

```
┌─────────────────────────── Windows 后端 (Flask, py3.14) ───────────────────────────┐
│                                                                                     │
│   EngineManager (单例)                                                              │
│   ├── OllamaEngine  ──HTTP──▶  Ollama 服务 (Windows, localhost:11434, qwen3:8b)     │
│   ├── VllmEngine    ──HTTP──▶  vLLM 服务   (WSL2,    localhost:8000, qwen3-8b AWQ)  │
│   ├── 看门狗 (15s 间隔, 连续失败 3 次触发故障转移/自动重启)                          │
│   └── 无缝切换 (Condition 排队 + 在途请求排空, 请求不丢失)                          │
│                                                                                     │
│   前端后台页: 引擎切换按钮 / GPU 实时监控条 / 异常横幅 / 恢复进度                    │
└─────────────────────────────────────────────────────────────────────────────────────┘
                      ▲ WSL2 localhost 转发 (Windows 可直连 vLLM:8000)
                      │
┌─────────────────────┴────────────────── WSL2 (Ubuntu, conda env: vllm) ────────────┐
│   vllm serve Qwen3-8B-Instruct-AWQ  (OpenAI 兼容 API, /v1/chat/completions)        │
│   管理脚本: /root/vllm_scripts/vllm_service.sh  (start|stop|restart|status|logs)    │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

- **Ollama 在 Windows 侧**：服务常驻，模型按需加载、`keep_alive` 到期或显式卸载后释放显存。
- **vLLM 在 WSL2 侧**：启动即常驻显存，支持连续批处理（continuous batching），多用户并发吞吐远高于 Ollama。
- **网络**：Windows 侧经 `localhost:8000` 可直连 WSL2 内 vLLM（WSL2 localhost 转发）；WSL2 内访问不到 Windows 侧 Ollama（NAT + Ollama 仅监听 127.0.0.1）。

---

## 2. 傻瓜式日常操作

### 2.1 启动 / 停止 / 重启 vLLM（在 WSL2 内）

```bash
# 脚本在 WSL 内的固定路径 (软链接, 无空格): /root/vllm_scripts/vllm_service.sh
sudo bash /root/vllm_scripts/vllm_service.sh start     # 启动 (含健康等待, 首次约 1~2 分钟)
sudo bash /root/vllm_scripts/vllm_service.sh status    # 查看运行状态 / PID / 端口
sudo bash /root/vllm_scripts/vllm_service.sh restart   # 重启
sudo bash /root/vllm_scripts/vllm_service.sh stop      # 停止
sudo bash /root/vllm_scripts/vllm_service.sh logs      # 跟踪日志
```

脚本源文件在 Windows 仓库：`scripts/vllm/vllm_service.sh`（WSL 内通过软链接 `/root/vllm_scripts` 调用，避免 Windows 路径空格问题）。

### 2.2 后台页面一键切换引擎

浏览器打开系统后台管理页：

- 页面顶部有 **Ollama / vLLM 切换按钮**与引擎状态圆点（绿=健康，红=异常）。
- 点击切换 → 后端等待在途请求排空后无缝切换，**服务不中断、请求不丢失**，顶部弹出提示。
- **GPU 资源监控条**：实时显示显卡型号、显存占用（已用/总量、百分比）、GPU 利用率、温度，4 秒轮询。
- **大模型调用统计**：各引擎请求总数 / 失败数 / 在途数。

### 2.3 切换时显存如何自动处理（c6）

8GB 这类小显存卡上，vLLM（~6.6G）与 Ollama 模型（~5G）**无法同时驻留**。系统自动处理：

- **切到 vLLM**：流量切走后，后端在后台向 Ollama 发 `keep_alive=0` 卸载全部驻留模型，显存归还给 vLLM；Ollama 服务本身保持在线（故障转移时模型按需重新加载，首次请求多几秒）。
- **切到 Ollama / vLLM 故障转移**：vLLM 停止后显存释放，Ollama 加载模型承接流量。
- **看门狗自动重启 vLLM 前**：后端（Windows 侧，能可靠访问 Ollama）先卸载 Ollama 模型，并轮询等待空闲显存回升到总显存 80% 以上，再发起 vLLM 重启——避免 "No available memory for the cache blocks" 显存竞争失败。

> 说明：vLLM 运行时 Ollama 服务仍在线但**不加载模型、不占显存**（脱离占用），随时可作为故障兜底。

---

## 3. 显存动态适配（不同显卡 / 不同负载）

`vllm_service.sh` 启动时用 `nvidia-smi --query-gpu=memory.total,memory.free` 探测显卡，**自动计算启动参数**，无需手工调：

| 参数 | 动态规则 |
|------|----------|
| `--gpu-memory-utilization` | `(空闲显存 MiB − 1024MiB 安全余量) / 总显存`，夹在 `[0.45, 0.90]`，并**向下取整到 0.01**（保证 `util×总显存 ≤ 空闲−余量`，避免踩线失败） |
| `--max-model-len` | 总显存 ≥20GB→16384；≥10GB→12288；小卡（如 8GB）→**3072** |
| `--max-num-seqs` | ≥20GB→16；≥10GB→8；小卡→**4**（并发序列数） |
| `--enforce-eager` | 小卡默认开启（关闭 CUDA Graph，省显存、降启动峰值） |

**8GB RTX 3070（WSL2）实测最终参数**：`util=0.81~0.82`、`max-model-len=3072`、`max-num-seqs=4`、`enforce-eager`，占用约 **6.9G / 8G**。6 路并发全部成功（峰值在途 4），平均 3.9s。

可用环境变量覆盖自动值（一般不需要）：

```bash
export VLLM_GPU_MEM_UTIL=0.85
export VLLM_MAX_MODEL_LEN=4096
export VLLM_MAX_NUM_SEQS=8
export VLLM_ENFORCE_EAGER=0   # 大显存卡可关 eager 用 CUDA Graph 提速
```

> 为什么 8GB 卡上下文是 3072 而不是 4096：保守 util 下框架+权重占完，KV cache 仅剩 ~0.46G；4096 上下文需 ~0.56G KV，vLLM 估算上限 ~3344，故取 3072 留余量。大显存卡不受此限。

---

## 4. 故障转移与自动恢复（用户侧体验）

- **vLLM 调用异常**：前端顶部出现横幅，提示"vLLM 异常，已退回规则/Ollama 兜底模式"，用户请求不报错、自动由 Ollama 或规则模式承接。
- **正在恢复 vLLM**：横幅实时显示恢复进度（"vLLM 服务异常，正在自动重启并恢复…"）。
- **看门狗逻辑**（后端 `llm_engine.py`）：
  1. 每 15s 健康检查；某引擎连续失败 3 次 → 若它正承载流量则**自动故障转移**到备用引擎；
  2. vLLM 故障则先**卸载 Ollama 释放显存**（见 2.3），再经 WSL 调 `vllm_service.sh restart` 自动重启（冷却约 4 个检查周期防频繁重启）；
  3. vLLM 恢复健康且它是首选引擎 → 自动**切回 vLLM**，横幅消失。
- **Agent 访问过长**：前端对长时间无响应的请求给出"后台服务器繁忙/卡住"提示，后端看门狗负责重启与并发资源重分配。

---

## 5. 首次环境部署（已在本机完成，供换机/重装参考）

脚本均在 `scripts/vllm/`：

1. **WSL2 + conda 环境**：`setup_env.sh` 安装 vLLM、torch、CUDA 依赖，创建 conda env `vllm`。
2. **下载模型**：`download_model.sh` 从 ModelScope 下载 `Qwen3-8B-Instruct-AWQ`（轻量化 8B 量化模型，约 5.7G 权重），校验完整性。
3. **建立脚本软链接**（Windows 路径含空格，WSL 内用无空格软链）：`wsl_setup_link.sh`。
4. 启动：`vllm_service.sh start`。
5. Windows 后端配置见 `backend/config.py`：`VLLM_URL=http://localhost:8000/v1`、`VLLM_HEALTH_URL=http://localhost:8000/health`、`VLLM_MODEL=qwen3-8b`、`VLLM_SERVICE_SCRIPT=/root/vllm_scripts/vllm_service.sh`。

### 5.1 CUDA 工具链版本对齐（重要排障经验）

torch 2.13+cu130 只带 CUDA runtime 不带 `nvcc`；vLLM/flashinfer 首次运行 JIT 编译自定义 kernel 需要完整 nvcc 工具链。pip 的 `nvidia-cuda-nvcc / nvidia-cuda-nvvm / nvidia-cuda-crt` 必须**全部同版本**（本机对齐为 **13.0.88**）：

- `nvcc` / `ptxas` / `nvvm(cicc)` / `crt` 版本不一致会报 `Unsupported .version 9.3; current version is '9.0'`（PTX ISA 不匹配：CUDA 13.0→PTX 9.0，13.3→PTX 9.3）。
- 对齐命令：`pip install nvidia-nvvm==13.0.88 nvidia-cuda-crt==13.0.88`，并清除 `~/.cache/flashinfer`、`~/.cache/torch_extensions` 缓存。

### 5.2 cu13 pip 元包库布局补齐

pip 的 cu13 元包采用新布局：库在 `site-packages/nvidia/cu13/lib/`（**没有 `lib64/`**），且缺 `libcudart.so` 开发符号链接与 `stubs/libcuda.so`。flashinfer/torch 的链接命令写死 `-L$CUDA_HOME/lib64 -lcudart -lcuda`，会报 `cannot find -lcudart/-lcuda` + `collect2: ld returned 1`。

`vllm_service.sh` 的 `setup_cuda_home` 已**幂等自动补齐**：`lib64 → lib` 软链、`libcudart.so → libcudart.so.13`、`lib/stubs/libcuda.so → /usr/lib/wsl/lib/libcuda.so.1`（WSL 驱动）。换机后无需手工处理，脚本每次启动自检补齐。

---

## 6. Qwen3 非思考模式（避免思考块污染结构化输出）

Qwen3 默认输出思考块，会污染 NL2SQL / JSON 等结构化结果。系统三重保证关闭思考：

1. **服务端 chat template**（`scripts/vllm/qwen3_nonthinking.jinja`）：对最后一条 user 消息末尾追加 `/no_think`（Qwen3 官方硬关闭约定）。启动参数 `--chat-template qwen3_nonthinking.jinja`。
2. **后端双保险**：`VllmEngine.chat` 请求体带 `chat_template_kwargs: {'enable_thinking': False}`。
3. **残留剥离**：后端 `_strip_think()` 正则剥离开关生效后可能残留的空思考外壳。

---

## 7. 常见问题排查

| 现象 | 原因 / 处理 |
|------|-------------|
| vLLM 启动报 `Free memory ... less than desired utilization` | 空闲显存不足（Ollama 模型驻留/其他占用）。脚本会自动降 util；仍失败则确认 Ollama 模型已卸载（后端会自动处理） |
| vLLM 启动报 `No available memory for the cache blocks` | KV cache 显存不足。小卡已用 `max-model-len=3072`；确认启动前 Ollama 模型已卸载 |
| JIT 编译报 `Unsupported .version 9.x` | CUDA 工具链版本不齐，见 5.1，对齐 nvcc/nvvm/crt 同版本并清缓存 |
| 链接报 `cannot find -lcudart/-lcuda` | cu13 库布局问题，脚本已自动补齐（5.2）；检查 `setup_cuda_home` 日志 |
| 模型回答里夹带思考块 | `/no_think` 未生效，检查 `--chat-template` 路径与后端 `enable_thinking=False` |
| Windows 连不上 vLLM | 确认 WSL2 内 `vllm_service.sh status` 为运行中；Windows 侧 `curl.exe http://localhost:8000/health` 应返回 200 |
| 切换后 Ollama 仍占显存 | 后端卸载是后台异步进行，稍候数秒；看门狗重启 vLLM 前会同步等待显存归还到 80% |

日志位置：WSL 内 vLLM 日志 `/root/vllm_logs/vllm.log`；后端引擎日志见 Flask 控制台（前缀 `[LLMEngine]`）。
