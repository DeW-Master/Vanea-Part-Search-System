# ============================================
# van.ea 车辆零件智能查询系统 - Docker 镜像
# 版本: v1.3.0
# ============================================
FROM python:3.9-slim

# ---------- 镜像元信息 ----------
LABEL maintainer="R&D Team"
LABEL version="1.3.0"
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
    pip install --no-cache-dir -r ./backend/requirements.txt

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
ENV ADMIN_PASSWORD=admin2026
ENV OLLAMA_URL=http://host.docker.internal:11434
ENV OLLAMA_MODEL=qwen2.5:7b
ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=5000
ENV SECRET_KEY=parts-search-secret-key-2026

# ---------- 数据卷 ----------
VOLUME ["/app/data"]

# ---------- 健康检查 ----------
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD curl -f http://localhost:5000/api/health || exit 1

# ---------- 启动命令 ----------
CMD ["python", "backend/app.py"]
