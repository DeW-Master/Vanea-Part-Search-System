# ============================================
# van.ea 车辆零件智能查询系统 - Docker 镜像
# 版本: build20260817 (Phase 3)
# ============================================
FROM python:3.9-slim

# ---------- 镜像元信息 ----------
LABEL maintainer="R&D Team"
LABEL version="build20260817"
LABEL description="van.ea Vehicle Parts Intelligent Query System"
LABEL app.name="van.ea Part Search"
LABEL app.codename="van.ea"

# ---------- 基础配置 ----------
WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装系统依赖（curl 用于 HEALTHCHECK）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ---------- Python 依赖（利用 Docker layer cache 优化） ----------
# 先复制 requirements.txt，只要依赖不变就复用缓存层
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r ./backend/requirements.txt && \
    # gunicorn 仅用于容器内生产运行 (Windows 原生运行不受影响)
    pip install --no-cache-dir "gunicorn>=21.0,<23.0"

# ---------- 应用代码 ----------
# 保持与本地一致的目录结构：backend/ 和 frontend/
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# ---------- 创建非 root 用户（安全考虑） ----------
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

# 创建数据目录并设置权限
RUN mkdir -p /app/data/uploads && \
    chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# ---------- 端口 ----------
EXPOSE 5000

# ---------- 环境变量默认值 ----------
# 注意: 敏感配置 (ADMIN_PASSWORD / SECRET_KEY) 不在此写死默认值, 必须由
# docker-compose 的 env_file / environment 或 docker run -e 注入;
# 未注入时应用使用 config.py 中的默认值并打印安全告警。
ENV OLLAMA_URL=http://host.docker.internal:11434
ENV OLLAMA_MODEL=qwen2.5:7b
ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=5000

# ---------- 数据卷 ----------
VOLUME ["/app/data"]

# ---------- 健康检查 ----------
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD curl -f http://localhost:5000/api/health || exit 1

# ---------- 启动命令 (gunicorn 多进程生产服务器) ----------
# 多副本/滚动更新由 docker-compose --scale 或 Swarm 编排; 单副本内多 worker 提升并发。
CMD ["gunicorn", "--chdir", "/app/backend", "-w", "2", "-k", "gthread", "--threads", "8", "--timeout", "120", "-b", "0.0.0.0:5000", "app:app"]
