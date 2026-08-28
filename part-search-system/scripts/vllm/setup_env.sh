#!/usr/bin/env bash
# ============================================================
# Vanea Part Search - vLLM 运行环境一键配置脚本
# 在 WSL2 (Ubuntu) 内执行:  bash scripts/vllm/setup_env.sh
# 作用: 创建 Python 3.11 环境 + 安装 vLLM / ModelScope / OpenAI SDK
# ============================================================
set -e

CONDA_DIR="$HOME/miniconda3"
ENV_NAME="vllm"

# 1) Miniconda (已安装则跳过)
if [ ! -d "$CONDA_DIR" ]; then
    echo "[setup] 下载 Miniconda ..."
    cd /tmp
    wget -q https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-py311_24.9.2-0-Linux-x86_64.sh -O miniconda.sh
    bash miniconda.sh -b -p "$CONDA_DIR"
    "$CONDA_DIR/bin/conda" init bash >/dev/null 2>&1 || true
    echo "[setup] Miniconda 安装完成"
else
    echo "[setup] Miniconda 已存在, 跳过"
fi

source "$CONDA_DIR/etc/profile.d/conda.sh"

# 2) vllm 环境 (Python 3.11 - vLLM 官方支持 3.10~3.12)
if ! conda env list | grep -q "/$ENV_NAME$"; then
    echo "[setup] 创建 conda 环境: $ENV_NAME (Python 3.11)"
    conda create -n "$ENV_NAME" python=3.11 -y
else
    echo "[setup] conda 环境 $ENV_NAME 已存在, 跳过"
fi
conda activate "$ENV_NAME"

# 3) pip 清华镜像
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null

# 4) 安装核心依赖
#    vllm>=0.8.5 为 Qwen3 官方要求; modelscope 用于国内下载模型
echo "[setup] 安装 vLLM (体积较大, 请耐心等待) ..."
pip install "vllm>=0.8.5" modelscope openai

# 5) GPU 可用性验证
echo "[setup] 验证 GPU / CUDA ..."
python - <<'PYEOF'
import torch
print(f"[setup] PyTorch {torch.__version__}, CUDA available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"[setup] GPU: {torch.cuda.get_device_name(0)}")
    print(f"[setup] 显存: {torch.cuda.get_device_properties(0).total_memory/1024**3:.1f} GB")
import vllm
print(f"[setup] vLLM {vllm.__version__}")
PYEOF

echo ""
echo "[setup] ============================================"
echo "[setup] 环境配置完成! 下一步执行:"
echo "[setup]   bash scripts/vllm/download_model.sh"
echo "[setup] ============================================"
