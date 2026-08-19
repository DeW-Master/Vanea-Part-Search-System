# van.ea 车辆零件智能查询系统

> F-Brain 智能问答 · Delta 阶段对比 · BOM 零件搜索

**技术栈**: Flask + SQLite/PostgreSQL + Redis + Ollama

---

## 快速开始

### Docker 方式（推荐）
```bash
docker-compose up -d --build
# 访问 http://localhost
# 管理员密码: admin2026
```

### Windows 原生方式
双击根目录的 `启动系统.bat`，访问 http://localhost:5000

---

## 目录结构

```
part-search-system/
├── 启动系统.bat             ⭐ 主入口
├── docker-compose.yml      Docker 编排
├── Dockerfile              Docker 构建
├── .env.example            环境变量示例
├── DEPLOYMENT.md           部署文档
├── PROJECT_CONTEXT.md      本文件
│
├── backend/                Python 后端
│   ├── app.py              Flask 主应用
│   ├── config.py           配置文件
│   ├── database.py         数据库层 + Delta 计算
│   ├── agent.py            AI 智能体
│   └── requirements.txt    Python 依赖
│
├── frontend/               前端静态文件
│   ├── index.html          主页面（所有Tab）
│   ├── admin.html          管理后台
│   ├── monitoring.html     监控页面
│   ├── delta.js            Delta 功能 JS
│   └── logo.png
│
├── scripts/                运维脚本
│   ├── windows/            6个 Windows bat 脚本
│   └── deploy/             部署辅助脚本
│
├── nginx/                  Nginx 配置
│   └── nginx.conf
│
└── data/                   运行时数据（持久化）
    ├── parts.db            主数据库
    ├── parts_data.xlsm     原始数据
    ├── cloud_config.json   云端配置
    └── uploads/            上传文件
```

---

## 核心功能

| 模块 | 说明 | 状态 |
|------|------|------|
| 🏠 Homepage Dashboard | Delta 总览：5 KPI + 双漏斗图 + 双饼图 + 趋势图 | ✅ |
| 🔍 零件搜索 | PN模糊搜索 / 字段搜索 / 复杂条件 / 导出 | ✅ |
| 📊 阶段 Delta | pre-TO/TO1/TO2 三阶段 PN+ZGS 组合对比 | ✅ |
| 🤖 F-Brain 智能体 | 规则引擎 + Ollama + 云端 API 三层架构 | ✅ |
| ⚙️ 管理后台 | 数据导入 / 列配置 / 云端配置 / 缓存管理 | ✅ |
| 📈 监控页面 | 并发用户 / 查询统计 / 系统状态 | ✅ |

**多语言**: 中文 / English / Deutsch 三语切换

---

## Delta KPI 逻辑（核心）

> Homepage 和阶段 Delta 页面使用**完全相同**的计算逻辑，都通过 `_build_stage_pn_map()` + `_compute_delta_pairs()` 实现。

| KPI | 定义 | 计算逻辑 |
|-----|------|----------|
| 新增PN | 新阶段 BOM 中新增的零件号 | `match_type == 'new_part'` |
| ZGS升级 | 同 PN 下 ZGS 版本升级 | `match_type == 'zgs_upgraded'` |
| EC新增 | EC 号从无到有 | `!from_ec && to_ec` |
| KEM释放 | 基于 EC 新增的 KEM 工程变更通知 | **EC新增子集**中 `!from_kem && to_kem` |
| SOMA新增 | SOMA 状态从无到有 | `from_soma != 'ja' && to_soma == 'ja'` |

### 阶段划分
通过 `Baulos_aggr` 字段判断：
- **pre-TO**: 不含 PRO1 也不含 PRO2
- **TO1**: 包含 PRO1
- **TO2**: 包含 PRO2

---

## 架构总览

```
                    Nginx :80
          (静态加速 / gzip / 安全头)
                       │
                       ▼
              Flask App :5000
          ┌──────────────┴──────────────┐
          │                             │
    Homepage/Dashboard            Search/Delta
          │                             │
          └──────────────┬──────────────┘
                         │
              Database Manager (SQLite)
                         │
              ┌──────────┴──────────┐
              │   Redis Cache       │  ← 可选，自动降级
              │  (查询/Delta缓存)   │
              └─────────────────────┘
                         │
              Ollama AI :11434 (可选)
```

---

## API 接口速查

| 分类 | 接口 | 说明 |
|------|------|------|
| **页面** | `GET /` | 首页 Dashboard |
| | `GET /admin` | 管理后台 |
| | `GET /monitoring` | 监控页面 |
| **搜索** | `GET /api/search` | PN 搜索 |
| | `POST /api/search_complex` | 复杂条件搜索 |
| | `GET /api/search_field` | 字段搜索 |
| **Delta** | `GET /api/delta/dashboard` | Dashboard 数据 |
| | `GET /api/delta` | Delta 列表（分页） |
| **AI** | `POST /api/agent/query` | 问答（SSE 流式） |
| | `GET /api/agent/status` | 智能体状态 |
| **管理** | `POST /api/admin/import` | 导入数据 |
| | `POST /api/admin/cache/clear` | 清空缓存 |
| **系统** | `GET /api/health` | 健康检查 |
| | `GET /api/version` | 版本信息 |

---

## 关键优化

- ✅ **多副本部署**: 支持 Docker Swarm / k8s 多实例部署，Redis 统一 Session
- ✅ **Ollama 负载均衡**: 多节点轮询 + 自动故障转移
- ✅ **全方位监控**: 集成 Prometheus 指标端点
- ✅ **数据库**: 双引擎支持（SQLite 默认 / PostgreSQL 可选）
- ✅ **Nginx 反向代理**: 静态文件加速 + 负载均衡配置
- ✅ **性能加速**: Redis 缓存层 + Delta 预计算后台线程
- ✅ **安全性**: 移除前端版本号显示，采用 build 日志追溯

---

## 配置项

所有配置均可通过环境变量覆盖，详见 `.env.example`。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_PASSWORD` | admin2026 | 管理员密码 |
| `FLASK_PORT` | 5000 | 监听端口 |
| `REDIS_URL` | redis://localhost:6379/0 | Redis 地址 |
| `CACHE_ENABLED` | true | 缓存开关 |
| `DELTA_REFRESH_INTERVAL` | 300 | Delta 刷新间隔（秒） |
| `OLLAMA_URL` | http://localhost:11434 | Ollama 地址 |

---

## 路线图 (Phase 4)

- [ ] 零件拓扑关系图谱可视化
- [ ] 零件图片库 AI 自动识别与匹配
- [ ] 基于历史变更数据的预测性 EC 预警
- [ ] 深度集成公司内部 Single Sign-On (SSO)

---

## 构建历史

构建版本记录统一维护在 `backend/config.py` 的 `VERSION_HISTORY` 中。
