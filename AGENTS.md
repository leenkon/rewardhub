# AGENTS.md

本文件是给在本仓库工作的编码代理的说明。交流用中文；一轮只做一个明确目标：审查 → 精简 → 实施 → 验证。

## 代码库信息

### 项目概览与主要语言

- RewardHub（中文名「带娃神器」）：家庭积分管理工具，Flask 单文件后端 + 无构建步骤的原生 JS 单页前端。
- 唯一运行时依赖是 `Flask==3.1.1`（`requirements.txt`）；数据库用标准库 `sqlite3`，无 ORM、无前端框架、无打包器。
- 主代码集中在四个文件：`app.py`（约 4120 行，路由 + 表结构 + 结算逻辑）、`static/app.js`（约 2190 行，全部前端逻辑）、`templates/index.html`（SPA 外壳，约 311 行）、`tests/test_app.py`（单测套件，约 2970 行）。
- 本机解释器是 Python 3.14，CI 用 3.12（`.github/workflows/publish-image.yml`），改动必须在 3.12 上可运行。

### 架构与核心文件

- 后端：40 个 `@app.get/post/put/delete/route` 装饰的路由全部在 `app.py`，按域排布——auth/setup、accounts、`/api/state`、transactions（录入与补录）、items（项目库、绑定、`/api/item-rules/<child>` 每孩子规则覆盖）、tasks（悬赏）、announcements、requests（审核）、analytics（含 `/api/targets/export` 目标表工作簿）、settlements（期次与结算）、system。
- 数据层：裸 SQL。`connection()`（`app.py:221`）是每请求的上下文管理器，负责 `row_factory = sqlite3.Row` 和 `PRAGMA foreign_keys = ON`；建表与升级全在 `init_db()`（`app.py:493`）里——`CREATE TABLE IF NOT EXISTS` 加 `migrate_*()` / `ensure_*()` 幂等补列补索引，模块末尾在 import 时执行 `init_db()`。
- 结算：`AUTO_SETTLEMENT_SOURCES`（`app.py:186`）列出 10 个结算 `source`，分为「目标窗口族」（`weekly_bonus` / `weekly_penalty` / `target_streak` / `target_streak_penalty` / `over_cap_penalty`）与「连续扣分族」（`deduct_{day,week,month,quarter,year}_streak`）。水位只护住 3 个窗口 source：`weekly_bonus` / `weekly_penalty` / `over_cap_penalty` 发放前先往 `weekly_settlements` 抢一行，抢不到就跳过；7 个连续族不写水位，去重只剩 `records_settlement_once` 部分唯一索引——记录一删就等于允许下一次结算原样补发。所以 `undo_transaction` 在 DELETE 之前用 `STREAK_SOURCES`（`app.py:193`）拒绝这 7 类单独撤销（409「连续奖惩记录不能单独撤销，请用首页的重新核算调整」），前端 `NO_UNDO_SOURCES`（`static/app.js:1007`）同步不给撤销入口，调整只能走首页重新核算；撤销入口只挂在加/扣分页的项目行上：`undoTargetFor()`（`static/app.js:1014`）按页顶选中的日期取该项目当天最新一笔，优先当天的加减分记录、其次当天落账的达标/未达标发放，所以积分记录页只展示、不再提供撤销；`state_payload` 的 records 因此带上 `item_kind` / `item_id`。先校验后写库这条顺序不能反，`connection()` 正常返回就 commit，把拒绝写在删除之后等于把删除也提交了。加分项目的「连续达标额外加」与「连续未达标扣」是两条独立链，各用自己的期数与分值，共用同一套目标窗口——负分值写法的旧数据由 `ensure_item_schema()` 一次性搬进 `streak_penalty*`。哪些记录参与计数由 `EARN_COMPLETION_FILTER` / `DEDUCT_COMPLETION_FILTER`（`app.py:155`、`app.py:158`）决定；`AUTO_SETTLE_UNITS`（`app.py:146`）只有「天」，所以页面加载只自动结天档，其余档位靠首页「手动结算」——而每一笔落账（加/扣分、补录、审批、撤销）已在同一个请求里把自己那一期重结过，手动结算因此只补最后一个日志之后才闭合的窗口和遗留数据（正常情况下 `entries` 为 0）。`#settle-unit` 的「全部周期」档跨档位补结所有未结算窗口（`unit=all` 不接受期次，重算仍要逐档选）。项目没有开关列：删除项目或把目标改成 0 就退出结算集合，此前未结算的期次不再补发，要先手动结算。起算锚点是项目的 `rule_set_at`（设定规则那天），`full_window_start()`（`app.py:1639`）把它对齐到**下一个完整期次**：规则定在半途的那一期既不奖励也不罚分，目标窗口与连续奖惩链都走这一条。回看长度同样只有 `SETTLE_LOOKBACK_PERIODS`（`app.py:143`）一份，`target_lookback_steps()`（`app.py:1385`）算结算地板、`streak_start()`（`app.py:1943`）算连续链起点、`settlement_periods()`（`app.py:1416`）算手动结算下拉的期次，三处共用；旧实现目标窗口按「84 天」折算而下拉按期数列举，月/季档下拉比引擎宽，选中够不到的期次只静默跳过什么都不发——要改回看范围只动这一份常数，别再造第二套。**落账就重算自己那一期**：`regrade_after_log()`（`app.py:1740`）先用 `resettle_item_windows()`（`app.py:1701`）把**这一个项目**从记录日期起的水位和发放记录删掉，再当场把删出来的期次重结；范围只限这一条项目，整档重扫会顺手罚分家长没要求补的其他项目。撤销一条发放记录（`source` 属于 `AUTO_SETTLEMENT_SOURCES`）不触发重算（`app.py:3762`），否则撤销会连锁改判别的期次。往前只到 `item_window_floor()`（`app.py:1645`）：回看长度和 `rule_set_at` 对齐的完整期次划到哪，重扫就到哪为止，更早的期次永远不动；今天才设的规则不会回头补发上周的奖，要改判历史只能勾首页的「重新核算」（force，逐档选周期），它走 `regrade_windows()`（`app.py:1750`）。`settled_floor()`（`app.py:1654`）是结算写入时的地板——从这条项目最新的那枚标记（发放记录最大日期，目标窗口族再算上水位 `week_start`）往后推一期；因为尾巴先删后结，重算那一期的地板跟着退回，它只拦得住更早期次的补发。连续链照旧从整个回看区间开始数，只有地板之后的窗口拿钱，所以迟到几天的记录照样进得去下一个未结算窗口。删除项目也不删 `weekly_settlements`，历史水位跟着记录一起留着。**新增一个 source 必须同时重建 `records_settlement_once`**：索引谓词和 upsert 的冲突目标都由这份列表拼出，库里留旧谓词的索引会让已发布版本的结算写入直接报 `ON CONFLICT clause does not match ...`。
- 认证与会话：`password_hash()` 用 PBKDF2-HMAC-SHA256 240k 迭代；`require_admin` / `current_user()` / `active_child_id()` 构成角色门控，孩子只能看到自己与被绑定的项目。没设 `SECRET_KEY` 时 `generated_secret_key()` 首次启动随机生成一份、存成 `DATA_DIR/secret_key`——源码里不留固定密钥，否则任何人都能签出管理员 cookie（升级后所有人要重新登录一次）。
- 前端：单个 `appState` 全局对象 + 逐面板 `renderX()` 重绘；`api()`（`static/app.js:194`）封装 fetch 并把 `payload.error` 抛成 `Error`；所有拼进 `innerHTML` 的值必须过 `escapeHtml()`；周窗口一律从 `mondayOf()`（`static/app.js:82`）起算，别再各自 `getDay() || 7`。分析页「项目完成情况」表的奖惩分单元格只给合计，逐笔明细由 `item_period_stats()`（`app.py:2163`）挂在对应期次的 cell 上（`detail`），`renderItemPeriods()`（`static/app.js:1125`）在表下 `#period-payouts` 按项目 × 期次逐行列出，跟着加分/扣分页签一起切——数据跟着行走，就别再为明细单开一个接口。这张表**一行是一个孩子 × 一个项目**：`item_period_stats()` 按 `account_ids` 逐孩子展开、各用那个孩子自己的规则，所以同一个项目两个孩子就两行；「孩子」列紧跟项目名，明细行第二格也标孩子名，前端只把 `account_id` 映射到 `/api/state` 的 `children`。加减分列表每行还带一个 `.note-input`「原因」，`handleOperation()`（`static/app.js:1741`）优先取它、没有才退回页顶备注，值最后由后端 `noted_title()` 拼进记录标题。计分规则表单只在 `openEditor()`（`static/app.js:1451`）一处赋值、`readRuleForm()`（`static/app.js:1420`）一处读回，别再在别处重复这十几行。规则是**每孩子一层**：项目本身（名称、积分、图标、绑定）全局共用，只有规则可以属于某个孩子——`item_rule_overrides` 的一行 = 孩子 × 项目的完整规则快照加自己的 `rule_set_at` 锚点，没有这行就沿用项目库规则，所以空表时行为与旧版一字不差。读只有 `effective_item()`（`app.py:1203`）一个口子（提交上限、目标与连续结算、期次统计、`/api/state` 都走它），写只有 `update_item_rule()`（`app.py:3905`），手工保存和从别的孩子抄两种写法都过同一个 `item_rule_payload()`（`app.py:975`）校验。前端 `effectiveItem()`（`static/app.js:1631`）按 `appState.bindingChildId` 做同一层合并，项目列表、目标表、弹窗都不必判断规则来自哪。项目库标题栏「抄自」下拉只列当前选中孩子以外的孩子（`renderRuleSync()`，`static/app.js:546`，一个都没有就整行隐藏），行内「抄」把来源孩子在该项目上的生效规则写给当前孩子；兑换库没有规则列，所以奖励页签既没这一行也没「抄」按钮。弹窗「范围」选「只给当前孩子」时名称/积分/图标禁用（`applyRuleScope()`，`static/app.js:1442`），保存走 `/api/item-rules/<child>`，「恢复共用」删掉那行快照。三条在途/竞态约束别回退：委托点击监听先置灰被点的按钮、`finally` 恢复（双击不会重复提交）；`loadAnalytics()` 靠自增的 `analyticsRequestId` 丢弃过期响应；`appState.settlePeriodUnit` 标记期次下拉属于哪个单位，与 `#settle-unit` 对不上时 `syncSettleRun()` 禁用结算。
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
python -m unittest discover -s tests -q  # 单测，当前 94 条，必须全绿
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
- 报错：API 统一 `return api_error("中文提示", 状态码)`（`app.py:2821`）；前端直接把 `error` 字段弹给用户，所以提示要可执行、是中文、不泄露堆栈。Python 自己的英文原文（`int()` 那种）也算泄露，数字字段一律走 `positive_int` / `non_negative_int`，它们共用 `int_field()` 的整数与 2^63 上限校验；奖惩分值都填正数、方向由字段本身决定，不要再靠正负号表示方向（`signed_int` 已随这条规则删掉，旧负值数据在 `ensure_item_schema()` 里搬到罚则列）。
- 文件导出：页面已经渲染好的东西（目标表）由前端把措辞与列结构随请求交给后端，后端只用 `xlsx_workbook()` 写工作簿——标准库 `zipfile` 手写最小 OOXML，不为此新增依赖，也不要用 Python 复写一遍周期与文案口径；下载文件名以服务端 `Content-Disposition` 为准。列宽与行高一律按内容算（`xlsx_auto_width()` / `xlsx_line_count()`，中日韩字符按两格估宽）——内容长度只有前端知道，写死宽度会让项目名折进固定行高被裁掉；WPS 不会像 Excel 那样自己撑高换行的行，`ht` + `customHeight` 必须显式写。
- 用户可见文案：标签 2-3 字，说明不复述标题，按钮去掉冗余动词，但保留功能术语。界面已于 2026-10-12 定稿冻结——不主动提改版，改动只限用户点名的控件。全新库的加/扣/兑换项目表必须为空，不要重新引入默认项目或示例数据。
- 前端：函数命名沿用 `render*` / `handle*` / `api`；字符串用双引号；列表重排用 ⠿ 手柄 + pointer 事件；下拉只列当前真实可用的候选项。
- 测试：`unittest`。`setUpModule()` 先在临时目录设好 `DATA_DIR` / `DATABASE_PATH` 才 `import app`——任何要碰库的用例都必须照这个顺序，否则 import 期的 `init_db()` 会写到真实库。规则类改动要同时补结算与角标断言，并把测试条数变化写进 CHANGELOG。
- Commit / CHANGELOG：中文 conventional commits（`feat:` / `fix:` / `refactor:` / `perf(scope):` / `test:` / `chore(release):`），一次提交一个明确目标。CHANGELOG 每条带类型前缀，写清「行为差异 + 为什么」以及受影响的用户可见措辞，不是罗列改了哪些文件。

### 版本控制与远程仓库

- Git 仓库根是 `RewardHub/`（工作区根 `D:\My_Projects\taskManager` 只是容器目录）。主干 `main`，远程 `origin = https://github.com/leenkon/rewardhub.git`。
- push 到 `main`（含空推送）或手动 `workflow_dispatch` 都会触发 `.github/workflows/publish-image.yml`：算下一版本 → bump 版本标记 → 写 CHANGELOG → 自动提交 `chore(release): vX [skip ci]` → 打 tag → 建 Release → 推 GHCR（`ghcr.io/leenkon/rewardhub`，每版 `X.Y.Z`、`vX.Y.Z`、`sha-<commit>` 三个别名加 `latest`）→ 构建 linux/windows 二进制。**也就是说「只是提交并推送」等于发版**；纯文档或脚本清理在推送前必须先问一句：① 带 `[skip ci]` 不发版，② 照常推顺带发版，③ 先不推攒到下次功能改动。
- 除 CI 工作流外没有 lint / formatter 配置（无 `pyproject.toml`、`setup.cfg`、`.editorconfig`、ESLint、Prettier），也不要顺手引入。
- 数据安全（最高优先级）：`data/points.db` 是用户日常在用的真实家庭库，9696 实例跑的就是它；`.gitignore` 已排除 `data/`、`points_data/`、`.env`，提交前确认这些没被 `git add -A` 带进来。复验只能用只读连接 `sqlite3.connect("file:...?mode=ro", uri=True)`；任何试跑都在临时副本上做，探针服务要独占端口（先 `netstat -ano | grep :<port>` 确认空闲）并显式设 `DATA_DIR`。写库必须由用户显式点名落点，且先复制一份 `points.bak-<用途>-<时间戳>.db` 再用单个 `BEGIN IMMEDIATE` 事务落笔。只想读改某个库字段时**不要 `import app`**；连「数一下某个模块级常量」也不要——在 `RewardHub/` 目录里 import 时 `DATA_DIR` 默认就指向 `data/`，`init_db()` 会顺手把真实库迁移成新 schema（2026-10-20 就这样给线上库加了两列并重建了结算索引）。取常量用 `grep` / `git show HEAD:app.py`，别 import。
- 改 `templates/index.html` 必须重启进程才生效（Jinja 在启动时缓存模板），而 `static/` 每次读盘——不重启会出现「旧 DOM + 新 JS」的假 bug。原终端的 9696 实例由用户自己重启，不要代劳。
- 本机 shell 限制（不是「无法验证」的借口）：PATH 里没有 `node` / `gh` / `docker` CLI，只有 `git` 和 `python`。JS 语法校验用 node-repl MCP 的 `node:vm`；Python 输出中文要 `PYTHONIOENCODING=utf-8`（控制台默认 GBK）；含中文的 curl 请求体要先落成 UTF-8 文件再 `--data-binary @file`；线上版本与镜像状态用 `api.github.com` / `ghcr.io` 匿名 REST 取证，镜像 tag 请求要带全四种 `Accept`，用逗号拼成单个 header。
