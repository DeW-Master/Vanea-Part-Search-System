# van.ea 车辆零件智能查询系统

> F-Brain 智能问答 · Delta 阶段对比 · BOM 零件搜索

**技术栈**: Flask + SQLite/PostgreSQL + Redis + Ollama

<!-- BUILD_STATUS_START -->
<!-- 此区块由 scripts/update_readme.py 自动更新，请勿手动编辑。 -->

## 构建状态

- **应用版本**: `build20260817`
- **Git 分支**: `main`
- **最新提交**: [`8db9f4f`](https://github.com/DeW-Master/Vanea-Part-Search-System/commit/8db9f4f8bb53049d8e8f049f62ba1a653b9492ce)
- **提交说明**: chore: 项目文件整理 + 会话 idle 回收 + Ollama 负载测试脚本
- **提交时间**: 2026-08-21T10:59:26+08:00
- **工作区状态**: 干净（已全部提交）

> 本区块在每次 `git push` 前通过 pre-push 钩子按当前提交自动刷新。
<!-- BUILD_STATUS_END -->

---

## 快速开始

### Docker 方式（推荐）
```bash
cd part-search-system
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
启动后进入终端控制台：菜单按 `f` 手动查看服务状态、`r` 重启、`s` 停止、`x` 退出菜单（服务继续后台运行）。
本机 Redis 未启动时会随主服务自动拉起（`REDIS_AUTOSTART=0` 关闭）；Ollama 需手动启动 `ollama serve`。

---

## 目录结构

```
.
├── README.md                 本文件（GitHub 首页展示）
├── part-search-system/
│   ├── docker-compose.yml     Docker 编排
│   ├── Dockerfile             Docker 构建
│   ├── .env.example           环境变量示例
│   ├── PROJECT_CONTEXT.md     详细项目上下文
│   ├── backend/               Python 后端（Flask）
│   │   ├── app.py             Flask 主应用 + Redis 自启 + 终端控制台装配
│   │   ├── console_ui.py      终端控制台（横幅 / 菜单 / 服务状态探测）
│   │   ├── agent.py           AI 智能体（规则 + 双引擎 + 云端三层）
│   │   ├── llm_engine.py      LLM 双引擎管理（vLLM/Ollama 路由+故障转移+看门狗）
│   │   ├── ollama_lb.py       Ollama 多实例负载均衡
│   │   ├── database.py        数据库层 + Delta 计算
│   │   ├── metrics.py         Prometheus 指标
│   │   ├── leader_election.py 多副本领导者选举
│   │   ├── log_config.py      统一日志体系
│   │   ├── user_log.py        用户操作日志
│   │   ├── models/            领域模型 (Part)
│   │   └── tests/             单元/集成/负载测试
│   ├── frontend/              前端静态文件
│   ├── scripts/               运维脚本（vLLM WSL 部署 / README 自动更新）
│   ├── monitoring/            Prometheus + Grafana 配置
│   └── data/                  运行时数据（持久化）
└── part-search-system/scripts/update_readme.py  README 构建状态自动更新脚本
```

---

## 核心功能

| 模块 | 说明 | 状态 |
|------|------|------|
| 主页 Dashboard | Delta 总览：5 KPI + 双漏斗图 + 双饼图 + 趋势图 | 已完成 |
| 零件搜索 | PN 模糊搜索 / 字段搜索 / 复杂条件 / 导出 | 已完成 |
| 阶段 Delta | pre-TO/TO1/TO2 三阶段 PN+ZGS 组合对比 · 双区下钻 | 已完成 |
| F-Brain 智能体 | 规则引擎 + Ollama/vLLM 双引擎 + 云端 API，SSE 流式问答 | 已完成 |
| LLM 双引擎 | vLLM/Ollama 自动路由、故障转移、看门狗自动重启、在途监控 | 已完成 |
| 终端控制台 | 启动横幅 + 交互菜单，服务状态手动查看、重启/停止 | 已完成 |
| 管理后台 | 数据导入 / 列配置 / 云端配置 / 实时监控 | 已完成 |
| 监控页面 | 并发用户 / 查询统计 / 系统状态 | 已完成 |

**多语言**: 中文 / English / Deutsch 三语切换

---

## Delta KPI 逻辑（核心）

主页 Dashboard 和阶段 Delta 页面使用完全相同的计算逻辑（`_build_stage_pn_map()` + `_compute_delta_pairs()`）。

| KPI | 定义 | 计算逻辑 |
|-----|------|----------|
| 新增 PN | 新阶段 BOM 中新增的零件号 | `match_type == 'new_part'` |
| ZGS 升级 | 同 PN 下 ZGS 版本升级 | `match_type == 'zgs_upgraded'` |
| EC 新增 | EC 号从无到有 | `!from_ec && to_ec` |
| KEM 释放 | 基于 EC 新增的 KEM 工程变更通知 | EC 新增子集中 `!from_kem && to_kem` |
| SOMA 新增 | SOMA 状态从无到有 | `from_soma != 'ja' && to_soma == 'ja'` |

阶段通过 `Baulos_aggr` 字段判断：pre-TO（不含 PRO1/PRO2）、TO1（含 PRO1）、TO2（含 PRO2）。

---

## 架构总览

```
                    Nginx :80 (Docker 部署时)
          (静态加速 / gzip / 安全头)
                       |
                       v
              Flask App :5000  ── 终端控制台 (console_ui)
          +----------------------+
          |                      |
    Homepage/Dashboard     Search/Delta
          |                      |
          +-----------+----------+
                      |
           Database Manager (SQLite/PostgreSQL)
                      |
          +-----------+----------+
          |   Redis Cache        |  可选，自动降级；Windows 本机自动拉起
          +----------------------+
                      |
          LLM 双引擎管理器 (llm_engine)
           +----------+----------+
           |                     |
     Ollama :11434          vLLM :8000 (WSL2)
     (LB 多实例)            (看门狗守护/自动重启)
           |                     |
     规则引擎回退 (NL2SQL 失败或无 LLM 时)
```

---

## API 接口速查

| 分类 | 接口 | 说明 |
|------|------|------|
| 页面 | `GET /` | 首页 Dashboard |
| | `GET /admin` | 管理后台 |
| | `GET /monitoring` | 监控页面 |
| 搜索 | `GET /api/search` | PN 搜索 |
| | `POST /api/search_complex` | 复杂条件搜索 |
| | `GET /api/search_field` | 字段搜索 |
| Delta | `GET /api/delta/dashboard` | Dashboard 数据 |
| | `GET /api/delta` | Delta 列表（分页） |
| AI | `POST /api/agent/query` | 问答（SSE 流式） |
| | `GET /api/agent/status` | 智能体状态（模式/后端/引擎） |
| | `GET /api/agent/engine` | 双引擎状态（健康度/在途/切换历史） |
| 管理 | `POST /api/admin/import` | 导入数据 |
| | `POST /api/admin/cache/clear` | 清空缓存 |
| 系统 | `GET /api/health` | 健康检查 |
| | `GET /api/version` | 版本信息 |
| | `GET /metrics` | Prometheus 指标 |

---

## 配置项

所有配置均可通过环境变量覆盖，详见 `part-search-system/.env.example`。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_PASSWORD` | admin2026 | 管理员密码 |
| `FLASK_PORT` | 5000 | 监听端口 |
| `REDIS_URL` | redis://localhost:6379/0 | Redis 地址 |
| `REDIS_AUTOSTART` | 1 | Windows 本机 Redis 随主服务自动拉起 |
| `CACHE_ENABLED` | true | 缓存开关 |
| `DELTA_REFRESH_INTERVAL` | 300 | Delta 刷新间隔（秒） |
| `OLLAMA_URL` | http://localhost:11434 | Ollama 地址 |
| `LLM_ENGINE` | vllm | LLM 首选引擎 (vllm/ollama)，故障自动转移 |
| `VLLM_URL` | http://localhost:8000/v1 | vLLM OpenAI 兼容地址 (WSL2) |
| `LLM_WATCHDOG_INTERVAL` | 15 | 引擎健康检查间隔（秒） |

---

## 文档

- [PROJECT_CONTEXT.md](part-search-system/PROJECT_CONTEXT.md) — 完整项目上下文与开发说明
- `part-search-system/.env.example` — 全部环境变量说明
- `part-search-system/scripts/vllm/README.md` — vLLM WSL2 部署指南
- `part-search-system/frontend/operation-guide.html` — 操作指南

---

## 许可证

内部项目，版权所有。
