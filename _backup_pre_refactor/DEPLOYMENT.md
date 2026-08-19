# van.ea 车辆零件智能查询系统 - 部署文档

> **构建版本**: build20260817  
> **更新日期**: 2026-08-17  
> **项目代号**: van.ea

---

## 目录

1. [项目简介](#1-项目简介)
2. [快速启动（3步）](#2-快速启动3步)
3. [Docker 部署](#3-docker-部署)
4. [Windows 原生部署](#4-windows-原生部署)
5. [环境变量说明](#5-环境变量说明)
6. [目录结构说明](#6-目录结构说明)
7. [运维与监控](#7-运维与监控)
8. [常见问题 FAQ](#8-常见问题-faq)
9. [版本信息](#9-版本信息)

---

## 1. 项目简介

van.ea 车辆零件智能查询系统是一个基于 Flask + SQLite 的零件数据管理与智能查询平台。支持多 Excel 文件动态合并、零件号搜索、阶段 Delta 对比、AI 智能体问答等功能。

### 核心功能

- **动态数据库管理**: 多 Excel 文件上传、表头智能映射、统一查询
- **零件搜索**: 按零件号、任意字段、复杂条件搜索
- **阶段 Delta**: pre-TO / TO1 / TO2 三阶段数据对比，差异高亮
- **AI 智能体**: 本地 Ollama / 云端 API 双后端，支持自然语言查询
- **并发控制**: 默认上限 3 人并发搜索，SSE 实时通知
- **系统监控**: CPU / 内存 / GPU / 磁盘 / 请求速率实时图表
- **管理后台**: 文件管理、列名配置、数据编辑、并发数设置

### 技术栈

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.9 + Flask |
| 数据库 | SQLite 3 / PostgreSQL 14+ |
| 缓存 | Redis 6+ (可选) |
| 前端 | 原生 HTML / CSS / JavaScript |
| AI 推理 | Ollama (本地) / OpenAI 兼容 API (云端) |
| 部署 | Docker (Compose/Swarm) / Windows |

---

## 2. 快速启动（3步）

### 方式 A：Docker 部署（推荐）

```bash
# 第 1 步：配置环境变量
cp .env.example .env
# 编辑 .env，修改 ADMIN_PASSWORD 和 SECRET_KEY

# 第 2 步：构建并启动
docker compose up -d --build

# 第 3 步：访问服务
# 查询页面:  http://localhost:5000
# 管理后台:  http://localhost:5000/admin
# 监控页面:  http://localhost:5000/monitoring
```

### 方式 B：Windows 原生部署

```bat
:: 第 1 步：确保已安装 Python 3.9
python --version

:: 第 2 步：双击运行启动脚本
start.bat

:: 第 3 步：浏览器访问
:: 查询页面:  http://localhost:5000
:: 管理后台:  http://localhost:5000/admin
```

---

## 3. Docker 部署

### 3.1 系统要求

- Docker 20.10+
- Docker Compose v2+
- 至少 2GB 可用内存
- 数据目录至少 500MB 可用空间

### 3.2 部署步骤

#### 1. 准备配置文件

```bash
cd part-search-system
cp .env.example .env
```

编辑 `.env` 文件，至少修改以下配置：

```env
ADMIN_PASSWORD=你的强密码
SECRET_KEY=随机字符串（可用 openssl rand -hex 32 生成）
```

#### 2. 配置 Ollama 连接（可选，用于 AI 智能体）

如果 Ollama 运行在**宿主机**上：

```env
OLLAMA_URL=http://host.docker.internal:11434
```

> Linux 用户还需在 `docker-compose.yml` 中取消注释 `extra_hosts` 配置。

如果 Ollama 也运行在 Docker 中且在同一网络：

```env
OLLAMA_URL=http://ollama:11434
```

#### 3. 启动服务

```bash
# 构建并启动（后台运行）
docker compose up -d --build

# 查看启动日志
docker compose logs -f

# 检查健康状态
docker compose ps
```

#### 4. 验证部署

访问健康检查接口：

```bash
curl http://localhost:5000/api/health
```

预期返回：

```json
{
  "status": "ok",
  "version": "build20260817",
  "timestamp": "2026-08-17T12:00:00",
  "services": {
    "app": "ok",
    "database": "ok",
    "redis": "unavailable"
  },
  "database": {
    "total_records": 0,
    "total_parts": 0
  }
}
```

### 3.3 常用运维命令

```bash
# 启动服务
docker compose up -d

# 停止服务
docker compose down

# 重启服务
docker compose restart

# 查看日志
docker compose logs -f
docker compose logs -f --tail=100

# 重新构建镜像
docker compose build --no-cache
docker compose up -d

# 进入容器
docker compose exec part-search bash

# 查看资源占用
docker stats
```

### 3.4 数据持久化

所有数据存储在 `./data` 目录，通过 Docker volume 挂载：

```
data/
├── parts.db          # 主数据库（SQLite）
├── parts_data.db     # 旧版数据库（兼容用）
├── cloud_config.json # 云端 AI 配置
├── version.txt       # 版本信息
└── uploads/          # 上传文件临时目录
```

> **备份建议**: 定期备份 `data/parts.db` 文件。

### 3.5 升级步骤

```bash
# 1. 备份数据库
cp data/parts.db data/parts.db.backup

# 2. 拉取最新代码
git pull  # 或手动替换代码文件

# 3. 重新构建并启动
docker compose up -d --build

# 4. 验证服务健康
docker compose ps
curl http://localhost:5000/api/health
```

---

## 4. Windows 原生部署

### 4.1 系统要求

- Windows 10 / Windows 11
- Python 3.9.x（推荐 3.9.13）
- 至少 4GB 可用内存
- 本地运行 AI 模型需 NVIDIA GPU（推荐 8GB+ 显存）

### 4.2 部署步骤

#### 1. 安装 Python

下载并安装 Python 3.9：  
https://www.python.org/downloads/release/python-3913/

> 安装时勾选 "Add Python to PATH"

验证安装：

```bat
python --version
pip --version
```

#### 2. 获取项目代码

将 `part-search-system` 文件夹放置到目标位置，例如：  
`C:\apps\part-search-system`

#### 3. 安装依赖

```bat
cd C:\apps\part-search-system
pip install flask flask-cors openpyxl
```

或者使用 requirements.txt：

```bat
pip install -r requirements.txt
```

#### 4. 配置环境变量（可选）

创建 `.env` 文件或直接修改 `config.py` 中的默认值。

#### 5. 启动服务

双击 `start.bat` 启动，或在命令行中运行：

```bat
python app.py
```

#### 6. 配置开机自启（可选）

方法一：使用任务计划程序

1. 按 `Win + R`，输入 `taskschd.msc`
2. 创建基本任务 → 触发器选 "计算机启动时"
3. 操作选 "启动程序" → 程序选 `start.bat`
4. 勾选 "不管用户是否登录都要运行"

方法二：使用 NSSM 注册为 Windows 服务

```bat
nssm install PartSearch "C:\apps\part-search-system\start.bat"
nssm start PartSearch
```

### 4.3 配置 Ollama（本地 AI）

#### 1. 安装 Ollama

下载地址：https://ollama.com/download

#### 2. 下载模型

```bat
ollama pull qwen2.5:7b
```

#### 3. 优化启动（可选）

运行 `start_optimized.bat` 以 GPU 优化参数启动 Ollama：

- Flash Attention 加速
- KV Cache 8 位量化
- 模型保活 10 分钟

---

## 5. 环境变量说明

所有配置项均可通过环境变量覆盖，优先级：环境变量 > 默认值。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `ADMIN_PASSWORD` | `admin2026` | 管理员后台密码，**生产环境务必修改** |
| `SECRET_KEY` | `parts-search-secret-key-2026` | Flask Session 加密密钥，**生产环境务必修改** |
| `FLASK_HOST` | `0.0.0.0` | Flask 监听地址 |
| `FLASK_PORT` | `5000` | Flask 监听端口 |
| `PARTS_DB_PATH` | `data/parts.db` | SQLite 数据库文件路径 |
| `PARTS_UPLOAD_DIR` | `data/uploads` | 上传文件临时目录 |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama 服务地址 |
| `OLLAMA_MODEL` | `qwen2.5:7b` | 默认 Ollama 模型名称 |
| `ALLOWED_EXTENSIONS` | `.xlsx, .xlsm` | 允许上传的文件类型（代码内配置） |

### 生成安全密钥

```bash
# Linux / Mac
openssl rand -hex 32

# Windows PowerShell
-join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) })
```

---

## 6. 目录结构说明

```
part-search-system/
│
├── 核心代码
│   ├── app.py              # Flask 主应用（路由、API、中间件）
│   ├── database.py         # 数据库操作层（SQLite CRUD、Delta 计算）
│   ├── agent.py            # AI 智能体模块（Ollama / 云端 API）
│   ├── config.py           # 配置文件（版本号、环境变量、Delta 字段配置）
│   └── requirements.txt    # Python 依赖列表
│
├── 部署配置
│   ├── Dockerfile          # Docker 镜像构建配置
│   ├── docker-compose.yml  # Docker Compose 编排配置
│   ├── .dockerignore       # Docker 构建忽略文件
│   ├── .env.example        # 环境变量示例（复制为 .env 使用）
│   ├── .gitignore          # Git 忽略文件
│   └── DEPLOYMENT.md       # 本文档
│
├── 启动脚本（Windows）
│   ├── start.bat           # 一键启动脚本（安装依赖 + 启动服务）
│   └── start_optimized.bat # Ollama GPU 优化启动脚本
│
├── 静态文件
│   └── static/
│       ├── index.html      # 主查询页面
│       ├── admin.html      # 管理后台页面
│       ├── monitoring.html # 系统监控页面
│       ├── delta.js        # Delta 功能前端逻辑
│       └── logo.png        # 品牌 Logo
│
├── 数据目录（持久化）
│   └── data/
│       ├── parts.db        # 主数据库（SQLite）
│       ├── parts_data.db   # 旧版数据库（兼容用）
│       ├── parts_data.xlsm # 初始数据文件
│       ├── cloud_config.json # 云端 AI 配置
│       ├── version.txt     # 版本信息文件
│       └── uploads/        # 上传文件临时目录
│
├── 其他
│   ├── operation-guide.html # 操作指南（独立 HTML）
│   ├── parts_database.db   # 遗留数据库文件
│   └── __pycache__/        # Python 字节码缓存（自动生成）
│
└── 自动创建目录（运行时）
    ├── backup/             # 备份目录
    └── logs/               # 日志目录
```

---

## 7. 运维与监控

### 7.1 健康检查

系统提供健康检查 API，可用于 Docker 健康检查和外部监控：

```
GET /api/health
```

**响应字段说明**：

| 字段 | 说明 |
|------|------|
| `status` | 整体状态：`ok` / `degraded` / `error` |
| `version` | 系统版本号 |
| `timestamp` | 检查时间（ISO 格式） |
| `services.app` | 应用服务状态 |
| `services.database` | 数据库服务状态 |
| `services.redis` | Redis 状态（当前未使用，为 unavailable） |
| `database.total_records` | 总记录数 |
| `database.total_parts` | 唯一零件号数量 |
| `database.total_files` | 已上传文件数 |
| `database.total_columns` | 统一列数 |

### 7.2 系统监控页面

访问 `/monitoring` 查看实时监控图表：

- CPU 使用率
- 内存使用率 / 总量
- GPU 使用率 / 显存（如有 NVIDIA GPU）
- 磁盘使用率
- 请求速率（每分钟请求数）
- 活跃搜索数
- 总请求数

### 7.3 日志管理

**Docker 部署**：日志由 Docker 管理，已配置日志轮转（单文件 10MB，最多 3 个文件）。

```bash
# 查看日志
docker compose logs -f

# 清理日志
docker compose down
docker system prune -f
```

**Windows 部署**：输出到控制台，可重定向到文件。

### 7.4 数据备份

#### 手动备份

```bash
# Docker 部署
docker compose exec part-search cp /app/data/parts.db /app/data/parts.db.backup

# 或直接复制宿主机目录
cp data/parts.db data/backup/parts.db_$(date +%Y%m%d).bak
```

#### 自动备份建议

在服务器上设置 cron job 或 Windows 任务计划，每日备份数据库文件。

### 7.5 安全建议

1. **修改默认密码**: 首次部署务必修改 `ADMIN_PASSWORD`
2. **修改密钥**: 生产环境修改 `SECRET_KEY` 为随机字符串
3. **HTTPS**: 生产环境建议配置 Nginx 反向代理 + SSL
4. **防火墙**: 限制管理后台 `/admin` 的访问来源 IP
5. **非 root 用户**: Docker 镜像已配置非 root 用户运行
6. **定期更新**: 关注依赖包安全更新

---

## 8. 常见问题 FAQ

### Q1: Docker 启动后无法访问页面？

**A**: 检查以下几点：
1. 容器是否正常运行：`docker compose ps`
2. 端口是否被占用：`netstat -ano | findstr 5000`
3. 查看错误日志：`docker compose logs`
4. 健康检查是否通过：访问 `http://localhost:5000/api/health`

### Q2: AI 智能体不可用？

**A**: 检查以下几点：
1. Ollama 服务是否启动：`ollama ps`
2. Docker 能否访问宿主机 Ollama（检查 `OLLAMA_URL` 配置）
3. 模型是否已下载：`ollama list`
4. 查看 agent 状态：`GET /api/agent/status`

### Q3: Linux Docker 无法连接宿主机 Ollama？

**A**: 在 `docker-compose.yml` 中添加：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

然后重启：`docker compose up -d`

### Q4: 上传 Excel 文件失败？

**A**: 检查以下几点：
1. 文件格式是否为 `.xlsx` 或 `.xlsm`
2. 文件大小是否过大（默认无限制，但建议 < 50MB）
3. `data/uploads/` 目录是否有写入权限
4. 查看浏览器控制台和服务端日志的具体错误

### Q5: 数据库被锁（database is locked）？

**A**: SQLite 在高并发写入时可能出现锁等待。
1. 系统已启用 WAL 模式，读取不阻塞写入
2. 减少同时进行的导入操作
3. 确保所有数据库连接正确关闭
4. 如频繁出现，考虑迁移到 PostgreSQL

### Q6: 如何修改默认并发搜索数？

**A**: 有两种方式：
1. 登录管理后台，在设置中调整
2. 调用 API：`POST /api/admin/concurrent/max`（需认证）

### Q7: Docker 镜像体积太大？

**A**: 当前镜像约 200MB 左右。如需进一步优化：
1. 已使用 `python:3.9-slim` 基础镜像
2. 已启用 `--no-cache-dir` 减少 pip 缓存
3. 已清理 apt 缓存
4. 可考虑多阶段构建（但项目较小，收益有限）

### Q8: 如何迁移数据到新服务器？

**A**: 
1. 停止旧服务
2. 复制 `data/` 目录到新服务器
3. 在新服务器启动服务
4. 验证数据完整性

### Q9: 版本升级后数据会丢失吗？

**A**: 不会。数据存储在 `data/` 目录中，升级代码不会影响数据。
但建议升级前备份 `parts.db` 文件。

### Q10: Windows 下中文显示乱码？

**A**: 启动脚本已设置 `chcp 65001`（UTF-8 代码页）。
如仍有问题，检查系统区域设置是否支持 UTF-8。

---

## 9. 版本信息

### 当前构建：build20260817 (2026-08-17)

**build20260817 更新内容**：

- **分布式架构**: 支持多副本部署，集成 Redis Session 共享
- **负载均衡**: Ollama 多节点自动轮询与健康检查
- **数据库抽象**: 完美支持 SQLite 与 PostgreSQL 切换
- **监控集成**: 新增 Prometheus 监控指标端点
- **UI 优化**: 移除页面可见版本号，统一 build 命名规范
- **Bug 修复**: 解决并发竞态、SQL 转义等已知问题

### 构建号规则

采用 `build + yyyymmdd` 格式，用于内部追溯与日志定位。页面上不再主动显示版本号以保持界面简洁。

---

## 附录

### A. API 速查

| 接口 | 方法 | 说明 | 认证 |
|------|------|------|------|
| `/api/health` | GET | 健康检查 | 否 |
| `/api/version` | GET | 版本信息 | 否 |
| `/api/stats` | GET | 数据库统计 | 否 |
| `/api/search` | GET | 零件号搜索 | 否 |
| `/api/search_field` | GET | 按字段搜索 | 否 |
| `/api/search_complex` | POST | 复杂条件搜索 | 否 |
| `/api/all_part_numbers` | GET | 所有零件号列表 | 否 |
| `/api/columns` | GET | 所有列信息 | 否 |
| `/api/delta` | GET | 阶段 Delta 查询 | 否 |
| `/api/delta_detail` | GET | Delta 详情下钻 | 否 |
| `/api/compare` | POST | 记录对比 | 否 |
| `/api/delta/dashboard` | GET | Delta 仪表盘 | 否 |
| `/api/agent/status` | GET | 智能体状态 | 否 |
| `/api/agent/query` | POST | 智能体问答 | 否 |
| `/api/agent/suggestions` | GET | 建议问题 | 否 |
| `/api/concurrent/status` | GET | 并发状态 | 否 |
| `/api/concurrent/acquire` | POST | 获取并发槽位 | 否 |
| `/api/concurrent/release` | POST | 释放并发槽位 | 否 |
| `/api/concurrent/heartbeat` | POST | 心跳保活 | 否 |
| `/api/concurrent/stream` | GET | SSE 并发状态流 | 否 |
| `/api/monitoring/current` | GET | 当前监控指标 | 否 |
| `/api/monitoring/history` | GET | 历史监控指标 | 否 |
| `/api/auth/login` | POST | 管理员登录 | - |
| `/api/auth/logout` | POST | 管理员登出 | 是 |
| `/api/auth/status` | GET | 登录状态 | 是 |
| `/api/admin/files` | GET | 文件列表 | 是 |
| `/api/admin/files/<id>` | DELETE | 删除文件 | 是 |
| `/api/admin/analyze` | POST | 分析 Excel | 是 |
| `/api/admin/import` | POST | 导入 Excel 数据 | 是 |
| `/api/admin/columns/<id>` | PUT | 更新列名 | 是 |
| `/api/admin/stats_enhanced` | GET | 增强版统计 | 是 |
| `/api/admin/concurrent/max` | POST | 设置最大并发数 | 是 |

### B. 联系与支持

如有问题，请联系 R&D 团队。
