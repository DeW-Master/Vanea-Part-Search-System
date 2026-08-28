#!/usr/bin/env bash
# ============================================================
# Vanea Part Search - ModelScope 模型下载 + 完整性校验
# 在 WSL2 (Ubuntu) 内执行:  bash scripts/vllm/download_model.sh
# 模型: Qwen3-8B-AWQ (INT4 量化, ~7GB, 适配 8GB 显存)
# ============================================================
set -e

source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda activate vllm

# 模型存放于 WSL 原生文件系统 (Linux IO 远快于 /mnt/d, vLLM mmap 加载必需)
MODEL_ID="${VLLM_MODEL_ID:-Qwen/Qwen3-8B-AWQ}"
MODEL_ROOT="${VLLM_MODEL_ROOT:-$HOME/models}"

echo "[download] 模型: $MODEL_ID"
echo "[download] 目标: $MODEL_ROOT"

python - "$MODEL_ID" "$MODEL_ROOT" <<'PYEOF'
import sys, os
from modelscope import snapshot_download

model_id, model_root = sys.argv[1], sys.argv[2]
os.makedirs(model_root, exist_ok=True)

path = snapshot_download(
    model_id,
    cache_dir=model_root,
    revision='master',
)
print(f"[download] 模型路径: {path}")

# ---- 完整性校验: vLLM 加载必需文件清单 ----
required = ["config.json", "tokenizer.json", "tokenizer_config.json"]
weights = [".safetensors"]
missing = [f for f in required if not os.path.exists(os.path.join(path, f))]
if missing:
    print(f"[verify] 缺少必需文件: {missing}")
    sys.exit(1)

st_files = [f for f in os.listdir(path) if f.endswith(".safetensors")]
if not st_files:
    print("[verify] 未找到 *.safetensors 权重文件!")
    sys.exit(1)

total_gb = sum(os.path.getsize(os.path.join(path, f))
               for f in os.listdir(path)) / 1024**3
print(f"[verify] 权重文件 {len(st_files)} 个, 模型总大小 {total_gb:.2f} GB")

# 输出模型实际路径, 供服务脚本读取
with open(os.path.expanduser("~/models/vllm_model_path.txt"), "w") as f:
    f.write(path)
print(f"[verify] OK - 模型完整性校验通过")
print(f"[verify] 路径已记录: ~/models/vllm_model_path.txt")
PYEOF

echo ""
echo "[download] ============================================"
echo "[download] 模型下载完成! 下一步执行:"
echo "[download]   bash scripts/vllm/start_vllm.sh"
echo "[download] ============================================"
