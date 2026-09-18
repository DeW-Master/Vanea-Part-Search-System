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
```bash
cd part-search-system/backend
python app.py
# 访问 http://localhost:5000
```
启动后进入终端控制台（console_ui）：`f` 手动刷新服务状态、`r` 重启、`s` 停止、`x` 退出菜单（服务继续后台运行）。
本机 Redis 未启动时随主服务自动拉起（`REDIS_AUTOSTART=0` 关闭，`REDIS_SERVER_PATH` 指定 exe 路径）；Ollama 需手动 `ollama serve`。

---

## 目录结构

```
part-search-system/
├── docker-compose.yml      Docker 编排
├── Dockerfile              Docker 构建
├── .env.example            环境变量示例
├── PROJECT_CONTEXT.md      本文件
│
├── backend/                Python 后端
│   ├── app.py              Flask 主应用 + Redis 自启 + 终端控制台装配
│   ├── console_ui.py       终端控制台（横幅 / 菜单 / 服务状态探测）
│   ├── config.py           配置文件 + 版本历史
│   ├── database.py         数据库层 + Delta 计算
│   ├── agent.py            AI 智能体（规则 + 双引擎 + 云端三层）
│   ├── llm_engine.py       LLM 双引擎管理（路由/故障转移/看门狗/在途监控）
│   ├── ollama_lb.py        Ollama 多实例负载均衡
│   ├── metrics.py          Prometheus 指标
│   ├── leader_election.py  多副本领导者选举
│   ├── log_config.py       统一日志体系
│   ├── user_log.py         用户操作日志
│   ├── models/             领域模型 (Part)
│   ├── tests/              单元/集成/负载测试
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
│   ├── vllm/               vLLM WSL2 部署（服务脚本/模型下载/环境安装）
│   ├── install-hooks.bat   git hooks 安装
│   ├── pre-push            pre-push 钩子（自动刷新 README 构建状态）
│   └── update_readme.py    README 构建状态更新脚本
│
├── monitoring/             Prometheus + Grafana 配置
│
└── data/                   运行时数据（持久化）
    ├── parts_data.xlsm     原始数据
    ├── parts.db            主数据库（SQLite）
    ├── cloud_config.json   云端配置（不入库）
    ├── llm_engine.json     LLM 首选引擎持久化
    └── uploads/            上传文件
```

---

## 核心功能

| 模块 | 说明 | 状态 |
|------|------|------|
| 🏠 Homepage Dashboard | Delta 总览：5 KPI + 双漏斗图 + 双饼图 + 趋势图 | ✅ |
| 🔍 零件搜索 | PN模糊搜索 / 字段搜索 / 复杂条件 / 导出 | ✅ |
| 📊 阶段 Delta | pre-TO/TO1/TO2 三阶段 PN+ZGS 组合对比 · 双区下钻 (BOM原始列 + ENIGMA参考) | ✅ |
| 🔬 Part.compare() | 零 hardcoding 逐字段对比，支持任意两阶段 BOM 数据对比 | ✅ |
| 🤖 F-Brain 智能体 | 规则引擎 + Ollama/vLLM 双引擎 + 云端 API 三层架构，SSE 流式问答 | ✅ |
| 🧠 LLM 双引擎 | vLLM/Ollama 自动路由、故障转移、看门狗自动重启、切换在途排空、在途监控 | ✅ |
| 🖥️ 终端控制台 | 启动横幅 + 交互菜单（f 刷新状态 / r 重启 / s 停止 / x 退菜单） | ✅ |
| 🚀 Redis 自启 | Windows 本机 Redis 未运行时随主服务自动后台拉起 | ✅ |
| ⚙️ 管理后台 | 数据导入 / 列配置 / 云端配置 / 缓存管理 / 实时监控 | ✅ |
| 📈 实时监控 | 在途调用 9 列 · 用户日志 · 系统状态 (admin 专属) | ✅ |

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

### 数据分层（ENIGMA vs BOM）
各阶段 BOM 数据保持纯粹，不被外部数据富化。ENIGMA 数据通过三层结构独立管理：

| 层级 | 载体 | 用途 |
|------|------|------|
| Part.data | BOM 原始列 | 阶段对比 / 下钻展示 |
| Part.enigma_record | ENIGMA 主表单条记录 | KPI 统计二级回退 / 参考展示 |
| Part.enigma_values | ENIGMA 多值索引 (ec/kem/fav/soma) | 去重统计 / 三级回退 |

`Part.compare()` 仅对比 `data` 层，保证零 hardcoding 且对比结果纯粹。
`Part.value()` 三级回退：`data → enigma_record → enigma_values`，保证 KPI 统计口径不变。

---

## 架构总览

```
                    Nginx :80 (Docker 部署时)
          (静态加速 / gzip / 安全头)
                       │
                       ▼
              Flask App :5000 ── 终端控制台 (console_ui)
          ┌──────────────┴──────────────┐
          │                             │
    Homepage/Dashboard            Search/Delta
          │                             │
          └──────────────┬──────────────┘
                         │
              Database Manager (SQLite/PostgreSQL)
                         │
              ┌──────────┴──────────┐
              │   Redis Cache       │  ← 可选，自动降级
              │  (查询/Delta缓存,   │     Windows 本机随主服务自启
              │   Session/Leader)   │
              └─────────────────────┘
                         │
              LLM 双引擎管理器 (llm_engine)
               ┌─────────┴─────────┐
               │                   │
        Ollama :11434        vLLM :8000 (WSL2)
        (LB 多实例轮询)       (看门狗守护/卡死自动重启)
               │                   │
        规则引擎回退 (NL2SQL 失败或无 LLM 时)
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
| | `GET /api/agent/status` | 智能体状态（模式/后端/引擎） |
| | `GET /api/agent/engine` | 双引擎状态（健康度/在途/切换历史） |
| **管理** | `POST /api/admin/import` | 导入数据 |
| | `POST /api/admin/cache/clear` | 清空缓存 |
| **系统** | `GET /api/health` | 健康检查 |
| | `GET /api/version` | 版本信息 |
| | `GET /metrics` | Prometheus 指标 |

---

## 关键优化

- ✅ **LLM 双引擎**: vLLM/Ollama 自动路由 + 故障转移 + 看门狗自动重启，切换在途排空，模式徽标返回实际承载引擎
- ✅ **终端控制台**: 服务状态按需手动查看（f），不做自动刷屏；r 重启 / s 停止 / x 退菜单后服务继续后台运行
- ✅ **Redis 自启**: Windows 下连不上本机 Redis 时自动后台拉起 redis-server，进程脱离控制台存活
- ✅ **SSE 流式问答**: F-Brain 思考/回答分段实时推送，前端逐字渲染
- ✅ **Part.compare() 零 hardcoding**: 纯字段逐字段对比，支持任意两阶段 / 任意 BOM 结构
- ✅ **数据分层架构**: BOM 原始数据与 ENIGMA 参考数据严格分离，下钻双区展示
- ✅ **多副本部署**: 支持 Docker Swarm / k8s 多实例部署，Redis 统一 Session + 领导者选举
- ✅ **Ollama 负载均衡**: 多节点轮询 + 自动故障转移
- ✅ **全方位监控**: 集成 Prometheus 指标端点 + admin 专属实时监控页
- ✅ **数据库**: 双引擎支持（SQLite 默认 / PostgreSQL 可选）
- ✅ **性能加速**: Redis 缓存层 + Delta 预计算后台线程
- ✅ **安全性**: 移除前端版本号显示，采用 build 日志追溯
- ✅ **用户审计**: JSONL 格式按 IP 分文件日志，支持查询历史与错误追溯

---

## 配置项

所有配置均可通过环境变量覆盖，详见 `.env.example`。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_PASSWORD` | admin2026 | 管理员密码 |
| `FLASK_PORT` | 5000 | 监听端口 |
| `REDIS_URL` | redis://localhost:6379/0 | Redis 地址 |
| `REDIS_AUTOSTART` | 1 | Windows 本机 Redis 随主服务自动拉起（0 关闭） |
| `REDIS_SERVER_PATH` | (PATH 查找) | redis-server.exe 完整路径 |
| `CACHE_ENABLED` | true | 缓存开关 |
| `DELTA_REFRESH_INTERVAL` | 300 | Delta 刷新间隔（秒） |
| `OLLAMA_URL` | http://localhost:11434 | Ollama 地址 |
| `LLM_ENGINE` | vllm | LLM 首选引擎（vllm/ollama），故障自动转移恢复自动切回 |
| `VLLM_URL` | http://localhost:8000/v1 | vLLM OpenAI 兼容地址（WSL2） |
| `VLLM_MODEL` | qwen3-8b | vLLM 模型名 |
| `LLM_WATCHDOG_INTERVAL` | 15 | 引擎健康检查间隔（秒） |
| `LLM_WATCHDOG_FAILURE_THRESHOLD` | 3 | 连续失败判定阈值 |
| `VLLM_RESTART_ENABLED` | 1 | vLLM 卡死经 WSL 自动重启 |

---

## 路线图 (Phase 4)

- [ ] 零件拓扑关系图谱可视化
- [ ] 零件图片库 AI 自动识别与匹配
- [ ] 基于历史变更数据的预测性 EC 预警
- [ ] 深度集成公司内部 Single Sign-On (SSO)

---

## 构建历史

构建版本记录统一维护在 `backend/config.py` 的 `VERSION_HISTORY` 中。
