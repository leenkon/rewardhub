# AGENTS.md

本文件是给在本仓库工作的编码代理的说明。交流用中文；一轮只做一个明确目标：审查 → 精简 → 实施 → 验证。

## 代码库信息

### 项目概览与主要语言

- RewardHub（中文名「带娃神器」）：家庭积分管理工具，Flask 单文件后端 + 无构建步骤的原生 JS 单页前端。
- 唯一运行时依赖是 `Flask==3.1.1`（`requirements.txt`）；数据库用标准库 `sqlite3`，无 ORM、无前端框架、无打包器。
- 主代码集中在四个文件：`app.py`（约 3820 行，路由 + 表结构 + 结算逻辑）、`static/app.js`（约 2065 行，全部前端逻辑）、`templates/index.html`（SPA 外壳，约 310 行）、`tests/test_app.py`（单测套件，约 2510 行）。
- 本机解释器是 Python 3.14，CI 用 3.12（`.github/workflows/publish-image.yml`），改动必须在 3.12 上可运行。

### 架构与核心文件

- 后端：40 个 `@app.get/post/put/delete/route` 装饰的路由全部在 `app.py`，按域排布——auth/setup、accounts、`/api/state`、transactions（录入与补录）、items（项目库、绑定、停用、排序）、tasks（悬赏）、announcements、requests（审核）、analytics（含 `/api/targets/export` 目标表工作簿）、settlements（期次与结算）、system。
- 数据层：裸 SQL。`connection()`（`app.py:214`）是每请求的上下文管理器，负责 `row_factory = sqlite3.Row` 和 `PRAGMA foreign_keys = ON`；建表与升级全在 `init_db()`（`app.py:486`）里——`CREATE TABLE IF NOT EXISTS` 加 `migrate_*()` / `ensure_*()` 幂等补列补索引，模块末尾在 import 时执行 `init_db()`。
- 结算：`AUTO_SETTLEMENT_SOURCES`（`app.py:186`）列出 9 个结算 `source`，分为「目标窗口族」（`weekly_bonus` / `weekly_penalty` / `target_streak` / `over_cap_penalty`，有 `weekly_settlements` 水位表）与「连续扣分族」（`deduct_{day,week,month,quarter,year}_streak`，无水位、靠 `records_settlement_once` 部分唯一索引兜底）。哪些记录参与计数由 `EARN_COMPLETION_FILTER` / `DEDUCT_COMPLETION_FILTER`（`app.py:166`）决定；只有「天」档随页面实时结算，其余档位靠首页「手动结算」。
- 认证与会话：`password_hash()` 用 PBKDF2-HMAC-SHA256 240k 迭代；`require_admin` / `current_user()` / `active_child_id()` 构成角色门控，孩子只能看到自己与被绑定的项目。没设 `SECRET_KEY` 时 `generated_secret_key()` 首次启动随机生成一份、存成 `DATA_DIR/secret_key`——源码里不留固定密钥，否则任何人都能签出管理员 cookie（升级后所有人要重新登录一次）。
- 前端：单个 `appState` 全局对象 + 逐面板 `renderX()` 重绘；`api()`（`static/app.js:188`）封装 fetch 并把 `payload.error` 抛成 `Error`；所有拼进 `innerHTML` 的值必须过 `escapeHtml()`。
- 版本与缓存：`APP_VERSION`（`app.py:55`）是版本号单一来源，`templates/index.html` 把它作为所有静态资源的 `?v=` 缓存串——不 bump 版本号，客户端会继续用旧 JS/CSS。

### 关键配置文件

- 环境变量：`DATA_DIR`、`DATABASE_PATH`、`PORT`、`TZ`、`SECRET_KEY`、`ADMIN_USERNAME(_B64)`、`ADMIN_PASSWORD(_B64)`。读取一律写成 `os.getenv("X") or 默认值`（`app.py:31-53`），因为 compose 用 `"${VAR:-}"` 转发时未设置的变量在容器里是**空串**而非缺失，带第二参数的 `getenv` 不会回退。新增变量要同时改 `docker-compose.yml` 的 `environment:` 和 README 的环境变量表，三处缺一处就静默失效。
- `docker-compose.yml` 是交付物本身（用户复制到部署目录再用），默认端口 `9696`、数据卷 `./points_data:/data`；`PORT` 同时喂给端口映射和容器环境，`TZ` 默认 `Asia/Shanghai`——基础镜像没有 zone 数据库（`Dockerfile` 里那层 apt 装 tzdata 补上），不传 `TZ` 容器的「今天」会按 UTC 早 8 小时翻页。
- `Dockerfile` 里 `COPY app.py` / `COPY templates` / `COPY static` 三条不能合并：多源 COPY 会把目录内容平铺进目标，`static/app.js` 会变成 `/app/app.js`，前端路径全废。
- `.dockerignore` 排除 `tests/`、`data/`、`.git/`。`static/illustrations/ATTRIBUTIONS.md` 是 CC-BY 署名的固化要求，必须留在 `static/` 里。
- `static/chart.js` 是 vendored 的 Chart.js 4.5.1 压缩包，不要手工编辑。
- `requirements-build.txt`（PyInstaller）只在打二进制时用，不进镜像。

### 开发与测试命令

```bash
pip install -r requirements.txt          # 运行时依赖
python app.py                            # 本地起服务，默认 http://127.0.0.1:9696
python -m unittest discover -s tests -q  # 单测，当前 84 条，必须全绿
python -m py_compile app.py              # 语法检查
git diff --check                         # 空白检查（make check = 上面三条 + 单测）
make release VERSION=x.y.z               # check → bump-version → changelog → check
make next-version                        # 只算下一版本号，勿手改版本标记
make binary / make binary-package        # PyInstaller 打包，见 BINARY_DEPLOYMENT.md
```

- 本机 PATH 里没有 `make`（只有 `git` 和 `python`），`Makefile` 的目标主要在 CI 里跑；本地按其等价命令执行即可。
- 没有 pytest 配置，别引入新测试框架；`make next-version` 输出非法时 CI 会直接失败。
- 版本标记只有三处（`app.py`、README「当前版本」、`tests/test_app.py` 里的断言），由 `scripts/release.py bump-version` 统一写，不要手改、不要在别处复制版本解析逻辑。

### 代码风格与约定

- Python：文件头 `from __future__ import annotations`，函数带类型标注。可复用的 SQL 片段和文案提成模块级常量字典（`ITEM_TABLES`、`CAP_PERIOD_LABELS`、`SETTLEMENT_SOURCE_PREDICATE`），避免同一谓词在两处各写一遍。注释只写非显然的「为什么」（例如为什么索引 WHERE 必须和 upsert 冲突目标文本一致），不写代码在做什么。
- 报错：API 统一 `return api_error("中文提示", 状态码)`（`app.py:2608`）；前端直接把 `error` 字段弹给用户，所以提示要可执行、是中文、不泄露堆栈。Python 自己的英文原文（`int()` 那种）也算泄露，数字字段一律走 `positive_int` / `non_negative_int` / `signed_int`，它们共用 `int_field()` 的整数与 2^63 上限校验。
- 文件导出：页面已经渲染好的东西（目标表）由前端把措辞与列结构随请求交给后端，后端只用 `xlsx_workbook()` 写工作簿——标准库 `zipfile` 手写最小 OOXML，不为此新增依赖，也不要用 Python 复写一遍周期与文案口径；下载文件名以服务端 `Content-Disposition` 为准。
- 用户可见文案：标签 2-3 字，说明不复述标题，按钮去掉冗余动词，但保留功能术语。界面已于 2026-10-12 定稿冻结——不主动提改版，改动只限用户点名的控件。全新库的加/扣/兑换项目表必须为空，不要重新引入默认项目或示例数据。
- 前端：函数命名沿用 `render*` / `handle*` / `api`；字符串用双引号；列表重排用 ⠿ 手柄 + pointer 事件；下拉只列当前真实可用的候选项。
- 测试：`unittest`。`setUpModule()` 先在临时目录设好 `DATA_DIR` / `DATABASE_PATH` 才 `import app`——任何要碰库的用例都必须照这个顺序，否则 import 期的 `init_db()` 会写到真实库。规则类改动要同时补结算与角标断言，并把测试条数变化写进 CHANGELOG。
- Commit / CHANGELOG：中文 conventional commits（`feat:` / `fix:` / `refactor:` / `perf(scope):` / `test:` / `chore(release):`），一次提交一个明确目标。CHANGELOG 每条带类型前缀，写清「行为差异 + 为什么」以及受影响的用户可见措辞，不是罗列改了哪些文件。

### 版本控制与远程仓库

- Git 仓库根是 `RewardHub/`（工作区根 `D:\My_Projects\taskManager` 只是容器目录）。主干 `main`，远程 `origin = https://github.com/leenkon/rewardhub.git`。
- push 到 `main`（含空推送）或手动 `workflow_dispatch` 都会触发 `.github/workflows/publish-image.yml`：算下一版本 → bump 版本标记 → 写 CHANGELOG → 自动提交 `chore(release): vX [skip ci]` → 打 tag → 建 Release → 推 GHCR（`ghcr.io/leenkon/rewardhub`，每版 `X.Y.Z`、`vX.Y.Z`、`sha-<commit>` 三个别名加 `latest`）→ 构建 linux/windows 二进制。**也就是说「只是提交并推送」等于发版**；纯文档或脚本清理在推送前必须先问一句：① 带 `[skip ci]` 不发版，② 照常推顺带发版，③ 先不推攒到下次功能改动。
- 除 CI 工作流外没有 lint / formatter 配置（无 `pyproject.toml`、`setup.cfg`、`.editorconfig`、ESLint、Prettier），也不要顺手引入。
- 数据安全（最高优先级）：`data/points.db` 是用户日常在用的真实家庭库，9696 实例跑的就是它；`.gitignore` 已排除 `data/`、`points_data/`、`.env`，提交前确认这些没被 `git add -A` 带进来。复验只能用只读连接 `sqlite3.connect("file:...?mode=ro", uri=True)`；任何试跑都在临时副本上做，探针服务要独占端口（先 `netstat -ano | grep :<port>` 确认空闲）并显式设 `DATA_DIR`。写库必须由用户显式点名落点，且先复制一份 `points.bak-<用途>-<时间戳>.db` 再用单个 `BEGIN IMMEDIATE` 事务落笔。只想读改某个库字段时**不要 `import app`**。
- 改 `templates/index.html` 必须重启进程才生效（Jinja 在启动时缓存模板），而 `static/` 每次读盘——不重启会出现「旧 DOM + 新 JS」的假 bug。原终端的 9696 实例由用户自己重启，不要代劳。
- 本机 shell 限制（不是「无法验证」的借口）：PATH 里没有 `node` / `gh` / `docker` CLI，只有 `git` 和 `python`。JS 语法校验用 node-repl MCP 的 `node:vm`；Python 输出中文要 `PYTHONIOENCODING=utf-8`（控制台默认 GBK）；含中文的 curl 请求体要先落成 UTF-8 文件再 `--data-binary @file`；线上版本与镜像状态用 `api.github.com` / `ghcr.io` 匿名 REST 取证，镜像 tag 请求要带全四种 `Accept`，用逗号拼成单个 header。
