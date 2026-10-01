# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   ├── app/release/          总览发布：闸门、影子验收、构建包、事务编排
│   ├── tests/                发布闸门与影子验收规则测试（标准库 unittest）
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 总览发布闸门与影子验收

道桥养护态势总览（`/api/overview`）只能经发布编排器上线，入口页面为「总览发布」。

- **四道闸门单向推进**：依赖检查 → 样例回放 → 差异裁定 → 切换流量
  （`POST /api/release/releases/{批次}/gates/{闸门}`）。跳级发布、倒序回滚、重复推进已
  完成闸门一律 `409` 拒绝；倒序回滚另有 `/rollback/{闸门}` 入口也只返回拒绝。
- **失败即保留旧版本**：任何闸门失败都不会把总览写成零；候选总览为空时构建包直接拒绝。
  切换阶段失败时，构建包与结构迁移在同一事务边界内整批回退（迁移 down + 仓库快照还原）。
- **发布前影子验收**：`POST /api/release/shadow-preview` 在隔离环境重放巡查、病害、桥隧
  事件，再与现网采样逐项对照。时区、设备时钟、业务日基准冲突一律以辖区运营约定为准
  （`app/release/clocks.py`：Asia/Shanghai、业务日切点 06:00、设备钟容忍 5 分钟）；
  旧样例维持此前批次，现网旧口径只用于对照暴露差异；约定未覆盖的冲突阻断裁定。
- **结论三处回写**：发布台账（append-only，历史保留当时批次）、路段待办（路段清单写
  「发布批次」列）、告警清单。
- **重复发布幂等**：版本签名只由总览内容、迁移结构与修订说明决定，相同签名重复发布返回
  既有批次。
- **基础设施门控**：队列、地图服务、缓存任一不可达时，依赖检查失败、切换流量前复查也
  拒绝转换流量（`POST /api/release/infra/{queue|map|cache}` 可演练）。
- **编排锁与断点续跑**：同一时刻只有一个发布者；回放逐项落检查点，连接断开后从
  `/resume` 查看未完成项并续跑，已灌入样例不重复灌入。

运行规则测试（无需安装 FastAPI，仅标准库）：

```bash
cd backend
python3 -m unittest discover -s tests
```
