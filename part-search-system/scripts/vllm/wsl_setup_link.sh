#!/usr/bin/env bash
# 创建无空格软链接, 供 Windows 后端看门狗调用 (路径含空格时 wsl 引号传递不稳)
set -e
SRC="/mnt/d/Trae Workshop/Vanea Part Search System/part-search-system/scripts/vllm"
ln -sfn "$SRC" /root/vllm_scripts
echo "== symlink =="
ls -ld /root/vllm_scripts
echo "== contents =="
ls -la /root/vllm_scripts/
