#!/usr/bin/env bash
# ============================================================
# Vanea Part Search - vLLM 服务管理脚本 (启动/停止/状态/重启)
# 在 WSL2 (Ubuntu) 内执行:
#   bash scripts/vllm/vllm_service.sh start|stop|restart|status|logs
#
# 特性:
#   - 自动加载 Qwen3-8B-AWQ (8GB 显存参数已调优)
#   - 默认关闭 thinking 模式 (NL2SQL 场景需要稳定直接输出)
#   - PID 文件 + 健康检查 + 自动重启循环 (由 watchdog 调用)
# ============================================================
set -u

source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda activate vllm

# ---------- CUDA 工具包 (nvcc) 自动探测 ----------
# vLLM V1 引擎 profile 阶段 cutedsl/JIT warmup 会编译自定义 CUDA kernel, 需要 nvcc;
# torch 只带 CUDA runtime 不带编译器。pip 安装的 nvidia-cuda-nvcc / cuda-toolkit
# 采用 cu13 元包新布局: site-packages/nvidia/cu13/{bin/nvcc, include, lib, nvvm},
# 不在 PATH 上, vLLM 的 _find_cuda_home() 只查 PATH 和 /usr/local/cuda, 故需显式导出。
setup_cuda_home() {
    local sp cu_dir
    sp="$(python -c 'import site;print(site.getsitepackages()[0])' 2>/dev/null)"
    [ -z "$sp" ] && return 0
    for cu_dir in "$sp/nvidia/cu13" "$sp/nvidia/cuda_nvcc/.." ; do
        if [ -x "$cu_dir/bin/nvcc" ]; then
            export CUDA_HOME="$cu_dir"
            export PATH="$cu_dir/bin:$PATH"
            # nvcc 编译时需调用 ptxas/fatbin 等工具; Triton/torch 自带旧 ptxas
            # (如 triton/backends/nvidia/bin/ptxas, PTX ISA 9.0) 会在 PATH 上被先找到,
            # 导致 "ptxas fatal: Unsupported .version 9.3"。
            # CUDA_BIN_PATH 让 nvcc 直接用 cu13 的工具链 (PTX 9.3), 不看 PATH
            export CUDA_BIN_PATH="$cu_dir/bin"
            [ -d "$cu_dir/lib" ] && export LD_LIBRARY_PATH="$cu_dir/lib:${LD_LIBRARY_PATH:-}"

            # --- 修复 cu13 元包新布局导致的 JIT 链接失败 (collect2: ld returned 1) ---
            # flashinfer/torch 的 cpp_extension 链接命令按旧布局写死:
            #   -L$CUDA_HOME/lib64 -L$CUDA_HOME/lib64/stubs -lcudart -lcuda
            # 但 pip cu13 元包只有 lib/(且 libcudart 是 libcudart.so.13 无 .so 开发链接,
            # 也无 stubs/libcuda.so)。nvcc 编译只用到 include/nvvm 能过, 链接才报
            # "cannot find -lcudart / -lcuda"。这里幂等补齐:
            # 1) lib64 -> lib 软链; 2) lib/libcudart.so -> libcudart.so.13;
            # 3) lib/stubs/libcuda.so -> 系统驱动 libcuda (WSL: /usr/lib/wsl/lib)
            if [ -d "$cu_dir/lib" ] && [ ! -e "$cu_dir/lib64" ]; then
                ln -sfn lib "$cu_dir/lib64" 2>/dev/null
            fi
            if [ -e "$cu_dir/lib/libcudart.so.13" ] && [ ! -e "$cu_dir/lib/libcudart.so" ]; then
                ln -sfn libcudart.so.13 "$cu_dir/lib/libcudart.so" 2>/dev/null
            fi
            local drv_cuda
            drv_cuda="$(ldconfig -p 2>/dev/null | grep -m1 'libcuda.so.1' | awk '{print $NF}')"
            [ -z "$drv_cuda" ] && drv_cuda="$(ls /usr/lib/wsl/lib/libcuda.so.1 2>/dev/null | head -1)"
            mkdir -p "$cu_dir/lib/stubs" 2>/dev/null
            if [ -n "$drv_cuda" ] && [ ! -e "$cu_dir/lib/stubs/libcuda.so" ]; then
                ln -sfn "$drv_cuda" "$cu_dir/lib/stubs/libcuda.so" 2>/dev/null
                echo "[vllm] CUDA 链接库已补齐: lib64->lib, libcudart.so, stubs/libcuda.so -> $drv_cuda"
            fi

            echo "[vllm] CUDA_HOME=$cu_dir (nvcc: $("$cu_dir/bin/nvcc" --version 2>/dev/null | grep release | awk '{print $5,$6}'), ptxas: $("$cu_dir/bin/ptxas" --version 2>/dev/null | grep release | awk '{print $5}'))"
            return 0
        fi
    done
    echo "[vllm] 警告: 未在 pip 包中找到 nvcc (site-packages/nvidia/cu13), 若 JIT 编译报错请安装 cuda-toolkit"
}
setup_cuda_home

# ---------- 修复 Triton 自带旧 ptxas ----------
# triton (3.7.x) 包内自带 ptxas (backends/nvidia/bin/ptxas, CUDA 12.8, PTX ISA 9.0),
# 并在 CUDA 算子编译时把该目录前置到 PATH; nvcc 13.0 生成 PTX 9.3, 旧 ptxas 报错:
#   "ptxas fatal: Unsupported .version 9.3; current version is '9.0'"
# nvcc 只按 PATH 解析裸名 ptxas (CUDA_BIN_PATH 无效), 因此直接把 triton 的 ptxas
# 替换为 cu13 的 13.0 ptxas (原文件备份 .bak), 幂等; 同理处理 fatbinary。
fix_triton_ptxas() {
    [ -n "${CUDA_HOME:-}" ] || return 0
    local sp tri new
    sp="$(python -c 'import site;print(site.getsitepackages()[0])' 2>/dev/null)"
    for tri in "$sp/triton/backends/nvidia/bin" "$sp/tokenspeed_triton/backends/nvidia/bin"; do
        [ -d "$tri" ] || continue
        for tool in ptxas fatbinary nvlink; do
            new="$CUDA_HOME/bin/$tool"
            [ -x "$new" ] || continue
            if [ -e "$tri/$tool" ] && [ ! -L "$tri/$tool" ]; then
                # 已是备份或已链接则跳过; 否则备份后软链到 cu13 新版
                mv -f "$tri/$tool" "$tri/$tool.bak" 2>/dev/null
                ln -sf "$new" "$tri/$tool"
                echo "[vllm] 已替换 $tri/$tool -> cu13 ($tool 13.0)"
            elif [ -L "$tri/$tool" ]; then
                ln -sf "$new" "$tri/$tool"
            fi
        done
    done
}
fix_triton_ptxas

MODEL_PATH_FILE="$HOME/models/vllm_model_path.txt"
MODEL_PATH="${VLLM_MODEL_PATH:-$(cat "$MODEL_PATH_FILE" 2>/dev/null || echo '')}"
SERVED_NAME="${VLLM_SERVED_NAME:-qwen3-8b}"
HOST="${VLLM_HOST:-0.0.0.0}"
PORT="${VLLM_PORT:-8000}"
LOG_DIR="$HOME/vllm_logs"
LOG_FILE="$LOG_DIR/vllm.log"
PID_FILE="$HOME/vllm.pid"

mkdir -p "$LOG_DIR"

# WSL2: vLLM V1 引擎的 UVA buffer 依赖 pinned memory, 内核 >=4.19.121 时需显式开启
export VLLM_WSL2_ENABLE_PIN_MEMORY="${VLLM_WSL2_ENABLE_PIN_MEMORY:-1}"

# ---------- 显存动态适配 ----------
# vLLM 启动时要求 free 显存 >= gpu-memory-utilization * 总显存, 否则直接报错退出。
# 该参数在启动时一次性确定, 运行期不可改; 因此启动时按当前显卡总显存 (不同显卡)
# 与当前空闲显存 (Windows 桌面/其他应用/Ollama 占用不同) 自动计算,
# 预留 ~1024MiB 安全余量给系统抖动 (探测时刻与 vLLM 自检时刻间显存会有少量波动,
# 且 util 向下取整确保保守)。可用环境变量 VLLM_GPU_MEM_UTIL 强制覆盖。
query_gpu_mem_mib() {
    # 输出: "总显存MiB 空闲显存MiB"; 失败输出空串
    nvidia-smi --query-gpu=memory.total,memory.free --format=csv,noheader,nounits 2>/dev/null \
        | head -1 | awk -F',' '{printf "%d %d", $1+0, $2+0}'
}

detect_gpu_mem_util() {
    local total free util pct
    read -r total free <<< "$(query_gpu_mem_mib)"
    if [ -z "$total" ] || [ "$total" -le 0 ] 2>/dev/null; then
        echo "[vllm] 警告: 无法查询 nvidia-smi 显存信息, 使用默认 util=0.82"
        echo "0.82"
        return
    fi
    # util = (空闲显存 - 1024MiB 安全余量) / 总显存, 夹在 [0.45, 0.90]
    # 用 int(u*100)/100 向下取整到 0.01, 保证 util*总显存 <= 空闲显存-余量, 避免踩线失败
    util=$(awk -v t="$total" -v f="$free" 'BEGIN{
        u = (f - 1024) / t;
        if (u < 0.45) u = 0.45;
        if (u > 0.90) u = 0.90;
        u = int(u * 100) / 100;
        printf "%.2f", u;
    }')
    pct=$(awk -v f="$free" -v t="$total" 'BEGIN{printf "%d", f*100/t}')
    echo "[vllm] GPU 显存: 总 ${total}MiB, 当前空闲 ${free}MiB (${pct}%), 动态计算 gpu-memory-utilization=${util}"
    if [ "$free" -lt $((total * 45 / 100)) ] 2>/dev/null; then
        echo "[vllm] 警告: 空闲显存不足总显存的 45%, vLLM 可能因显存不足启动失败!"
        echo "[vllm]       建议: 关闭 Windows 侧其他占显存程序, 或停止 Ollama (ollama stop <model>)"
    fi
    echo "$util"
}

# ---------- 启动前卸载 Ollama 模型, 释放显存给 vLLM ----------
# Ollama 服务本身不占显存, 模型加载 (runner) 才占; keep_alive 到期或显式卸载后
# runner 退出, 显存归还。vLLM 与 Ollama 模型在 8GB 卡上无法同时驻留 (~6.6G + ~5G),
# 因此 vLLM 启动前先把 Ollama 已加载的模型卸载; Ollama 服务保持在线, 故障转移时
# 模型会按需重新加载 (首次请求多几秒加载时间)。
unload_ollama() {
    local base=""
    for u in "http://127.0.0.1:11434" "http://localhost:11434"; do
        if curl -sf -m 2 -o /dev/null "$u/api/tags"; then base="$u"; break; fi
    done
    # WSL2 NAT 网络下再尝试 Windows 宿主地址 (Ollama 默认仅监听 127.0.0.1, 多数不可达, 尽力而为)
    if [ -z "$base" ]; then
        local host_ip; host_ip=$(ip route show default 2>/dev/null | awk '/default/{print $3; exit}')
        [ -n "$host_ip" ] && curl -sf -m 2 -o /dev/null "http://$host_ip:11434/api/tags" \
            && base="http://$host_ip:11434"
    fi
    if [ -z "$base" ]; then
        echo "[vllm] 未检测到 Ollama 服务, 跳过模型卸载"
        return
    fi
    local loaded models m
    loaded=$(curl -sf -m 3 "$base/api/ps" 2>/dev/null)
    models=$(echo "$loaded" | grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:"\([^"]*\)"/\1/')
    if [ -z "$models" ]; then
        echo "[vllm] Ollama 无驻留模型 (显存未占用), 无需卸载"
        return
    fi
    for m in $models; do
        # keep_alive=0 立即卸载模型, runner 退出后释放显存
        curl -sf -m 10 -X POST "$base/api/generate" \
            -H 'Content-Type: application/json' \
            -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null 2>&1 \
            && echo "[vllm] 已卸载 Ollama 模型释放显存: $m" \
            || echo "[vllm] 卸载 Ollama 模型失败 (忽略): $m"
    done
    sleep 3  # 等待 runner 进程退出、显存回收
}

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

health_check() {
    # /health 端点: 模型加载完成返回 200
    curl -sf -o /dev/null "http://127.0.0.1:$PORT/health" && return 0
    return 1
}

do_start() {
    if is_running; then
        echo "[vllm] 已在运行, PID=$(cat "$PID_FILE")"
        return 0
    fi
    if [ -z "$MODEL_PATH" ] || [ ! -d "$MODEL_PATH" ]; then
        echo "[vllm] 错误: 模型路径不存在 ($MODEL_PATH)"
        echo "[vllm] 请先执行: bash scripts/vllm/download_model.sh"
        return 1
    fi
    echo "[vllm] 启动服务: $SERVED_NAME @ $HOST:$PORT"
    echo "[vllm] 模型: $MODEL_PATH"

    # 1) 先卸载 Ollama 已驻留模型, 释放显存 (8GB 卡 vLLM/Ollama 模型无法共存)
    unload_ollama

    # 2) 按当前显卡总显存/空闲显存动态计算启动参数 (不同显卡/不同负载自适应;
    #    环境变量 VLLM_GPU_MEM_UTIL / VLLM_MAX_MODEL_LEN / VLLM_MAX_NUM_SEQS 可强制覆盖)
    read -r GPU_TOTAL GPU_FREE <<< "$(query_gpu_mem_mib)"
    if [ -n "${VLLM_GPU_MEM_UTIL:-}" ]; then
        GPU_UTIL="$VLLM_GPU_MEM_UTIL"
        echo "[vllm] 使用环境变量覆盖 gpu-memory-utilization=$GPU_UTIL"
    else
        detect_out="$(detect_gpu_mem_util)"
        echo "${detect_out%$'\n'*}"   # 打印探测/警告信息行 (最后一行为 util 数值)
        GPU_UTIL="$(printf '%s' "$detect_out" | tail -1)"
    fi

    # 上下文长度 / 并发 / eager 按总显存分档: <=10GB 小卡保守, 10-20GB 中档, >20GB 宽松
    # 注意 8GB 小卡: AWQ 权重 ~5.7G + CUDA context/激活 ~1G 后, 保守 util(0.81) 下
    # KV cache 仅剩 ~0.46G; 4096 上下文需 ~0.56G KV 会启动失败 (vLLM 估算上限 ~3344),
    # 故小卡用 3072 (NL2SQL 查询场景足够且留有余量); 10GB+ 卡 KV 充裕可用更长上下文
    local max_len max_seqs eager_args
    if [ -z "${VLLM_MAX_MODEL_LEN:-}" ]; then
        if [ "${GPU_TOTAL:-0}" -ge 20480 ] 2>/dev/null; then max_len=16384
        elif [ "${GPU_TOTAL:-0}" -ge 10240 ] 2>/dev/null; then max_len=12288
        else max_len=3072; fi
    else max_len="$VLLM_MAX_MODEL_LEN"; fi
    if [ -z "${VLLM_MAX_NUM_SEQS:-}" ]; then
        if [ "${GPU_TOTAL:-0}" -ge 20480 ] 2>/dev/null; then max_seqs=16
        elif [ "${GPU_TOTAL:-0}" -ge 10240 ] 2>/dev/null; then max_seqs=8
        else max_seqs=4; fi
    else max_seqs="$VLLM_MAX_NUM_SEQS"; fi
    # 小卡 (含 WSL2 共享 8GB) 默认 eager 跳过 CUDA Graph 预分配省显存;
    # 大卡可设 VLLM_ENFORCE_EAGER=0 开启图优化提升吞吐
    eager_args=(--enforce-eager)
    if [ "${VLLM_ENFORCE_EAGER:-1}" = "0" ]; then eager_args=(); fi
    echo "[vllm] 启动参数: util=$GPU_UTIL max-model-len=$max_len max-num-seqs=$max_seqs eager=${#eager_args[@]}"

    # 3) 自适应启动: 8GB 小卡上 KV cache 余量随空闲显存波动, 固定 max-model-len 可能
    #    因 "0.42 GiB KV needed > 0.35 GiB available (estimated maximum model length 2528)"
    #    失败。失败分两类, 分别处理:
    #    a) 上下文确实过长 (est 合理且低于当前长度): 按 est 降一档 (64 对齐再留 64 余量);
    #    b) 启动瞬间 Windows 侧显存瞬时占用 (桌面/浏览器/Ollama runner 未退), vLLM 自检时
    #       空闲比探测时更低, est 异常低 (如 192/784, 连 1024 都放不下): 等失败进程显存
    #       回收后重新探测 util (空闲恢复后 util 上调, KV 预算增加), 上下文恢复初始值重试。
    #    最多重试 3 次。
    local attempt max_attempts=3 init_max_len
    init_max_len="$max_len"
    attempt=1
    while [ "$attempt" -le "$max_attempts" ]; do
        echo "[vllm] 启动尝试 #$attempt: util=$GPU_UTIL max-model-len=$max_len ..."
        nohup vllm serve \
            --model "$MODEL_PATH" \
            --served-model-name "$SERVED_NAME" \
            --host "$HOST" --port "$PORT" \
            --quantization awq --dtype float16 \
            --max-model-len "$max_len" \
            --gpu-memory-utilization "$GPU_UTIL" \
            --max-num-seqs "$max_seqs" \
            "${eager_args[@]}" \
            --enable-prefix-caching --trust-remote-code \
            --chat-template "$(dirname "$0")/qwen3_nonthinking.jinja" \
            > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "[vllm] PID=$(cat "$PID_FILE"), 日志: $LOG_FILE"

        # 等待模型加载 (最长 180 秒)
        local ok=0 died=0
        echo -n "[vllm] 等待模型加载"
        for i in $(seq 1 90); do
            if ! is_running; then died=1; break; fi
            if health_check; then ok=1; break; fi
            echo -n "."
            sleep 2
        done
        echo ""

        if [ "$ok" = 1 ]; then
            echo "[vllm] 服务就绪! http://127.0.0.1:$PORT (max-model-len=$max_len)"
            return 0
        fi

        # 未就绪: 若进程仍在 (超时) 先停止, 便于重试释放显存
        if [ "$died" != 1 ] && is_running; then
            kill "$(cat "$PID_FILE")" 2>/dev/null
            sleep 3
            kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"

        # 解析 KV cache 不足错误中的 vLLM 估算最大上下文
        local est newlen
        est=$(grep -oE 'estimated maximum model length is [0-9]+' "$LOG_FILE" \
              | grep -oE '[0-9]+' | tail -1)
        if [ -z "$est" ]; then
            # 非 KV cache 错误 (工具链/显存自检/其他), 不重试直接报错
            echo "[vllm] 启动失败 (非 KV 缓存问题), 最后 20 行日志:"
            tail -20 "$LOG_FILE"
            return 1
        fi

        # 等待失败进程显存回收: 失败时 vLLM 占用可能尚未完全释放, 轮询至空闲
        # 不再增长 (最多 ~24s), 再重新探测, 保证拿到的是回收后的真实空闲
        echo -n "[vllm] 等待显存回收"
        local prev_free=-1 now_free total_now
        for i in $(seq 1 12); do
            read -r total_now now_free <<< "$(query_gpu_mem_mib)"
            echo -n ".(${now_free}MiB)"
            [ "$now_free" = "$prev_free" ] && break
            prev_free="$now_free"
            sleep 2
        done
        echo ""

        # 重新探测 util (未用环境变量强制覆盖时): util 上调说明失败是启动瞬间瞬时
        # 显存压力 (场景 b), 上下文恢复初始长度重试; util 不变才按 est 降档 (场景 a)
        local old_util="$GPU_UTIL"
        if [ -z "${VLLM_GPU_MEM_UTIL:-}" ]; then
            detect_out="$(detect_gpu_mem_util)"
            echo "${detect_out%$'\n'*}"
            GPU_UTIL="$(printf '%s' "$detect_out" | tail -1)"
        fi

        if awk -v o="$old_util" -v n="$GPU_UTIL" 'BEGIN{exit !(n > o)}'; then
            echo "[vllm] gpu-memory-utilization 上调 ($old_util -> $GPU_UTIL), 判定为启动瞬间瞬时显存压力, 上下文恢复 $init_max_len 重试"
            max_len="$init_max_len"
        else
            # 估算值向下 64 对齐再减一档 (64 token) 留安全余量, 下限 1024
            newlen=$(( (est / 64) * 64 - 64 ))
            [ "$newlen" -lt 1024 ] && newlen=1024
            if [ "$newlen" -ge "$max_len" ]; then
                echo "[vllm] KV 缓存不足 (est=$est), 但 util 未回升 ($old_util) 且上下文已无法再降低, 放弃"
                tail -20 "$LOG_FILE"
                return 1
            fi
            echo "[vllm] KV 缓存不足 (vLLM 估算最大上下文 $est, util=$GPU_UTIL 无回升), 降低 max-model-len: $max_len -> $newlen, 3 秒后重试..."
            max_len="$newlen"
        fi
        attempt=$((attempt + 1))
        sleep 3
    done

    echo "[vllm] 自适应重试 $max_attempts 次后仍失败, 最后 20 行日志:"
    tail -20 "$LOG_FILE"
    return 1
}

do_stop() {
    if is_running; then
        local pid; pid=$(cat "$PID_FILE")
        echo "[vllm] 停止服务 PID=$pid ..."
        kill "$pid" 2>/dev/null
        for i in $(seq 1 15); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
        echo "[vllm] 已停止"
    else
        echo "[vllm] 未在运行"
        rm -f "$PID_FILE"
    fi
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)
        if is_running && health_check; then
            echo "[vllm] 运行中且健康, PID=$(cat "$PID_FILE"), port=$PORT"
            exit 0
        elif is_running; then
            echo "[vllm] 进程存在但未通过健康检查 (模型加载中或异常)"
            exit 2
        else
            echo "[vllm] 未运行"
            exit 1
        fi ;;
    logs)    tail -f "$LOG_FILE" ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs}"
        exit 1 ;;
esac
