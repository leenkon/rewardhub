# RewardHub

RewardHub（中文名“带娃神器”）是一套面向家庭的本地积分管理工具。管理员可以创建管理账号和孩子账号，设置加分、扣分和兑换奖励；孩子可以自行注册账号、申请加分、兑换奖励或兑换现金，孩子提交的申请由管理员审核后生效。

系统提供家庭监控总控、账户积分表、折合金额、审核中心、积分记录、任务悬赏与验收、冒险等级与成就、积分分析和账号变更日志，并支持成人与孩子头像、本地图标与自定义图片。账号、积分和操作记录保存在本地数据目录中。

当前版本：`0.8.5`

## 更新记录

完整变更列表见 `CHANGELOG.md`。

## 孩子账号自助注册

在登录页点击“注册孩子账号”，填写登录账号、孩子名称、密码并选择头像，提交后即可进入孩子账号。孩子注册不会创建管理员账号；孩子提交的加分、奖励兑换和现金兑换申请，仍需管理员审核。

## Docker Compose 部署

把仓库里的 `docker-compose.yml` 复制到部署目录（镜像、端口 `9696`、`./points_data:/data` 数据卷已写好），如需自动创建管理员可在同目录建 `.env`：

```dotenv
ADMIN_USERNAME=your-admin
ADMIN_PASSWORD=change-this-password
```

启动与更新：

```bash
docker compose pull
docker compose up -d
```

停止：`docker compose down`。打开：`http://设备IP:9696`。

`points_data` 是数据目录。更新或重建容器时不要删除它，否则账号和积分数据会被清空。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `9696` | 监听端口 |
| `DATA_DIR` | 程序目录下的 `data`（镜像内为 `/data`） | 数据目录 |
| `DATABASE_PATH` | `<DATA_DIR>/points.db` | 数据库文件位置，仅用于把库放到数据目录之外 |
| `SECRET_KEY` | 内置占位值 | 会话签名密钥，公网或多实例部署时必须设置为自定义值 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 空 | 写入后按「安装凭据」一节处理，会覆盖现有管理员 |
| `ADMIN_USERNAME_B64` / `ADMIN_PASSWORD_B64` | 空 | 上一项的 Base64 形式，优先读取；适配会改写 `$`、`#`、引号的 NAS 环境文件 |

## 首次创建管理员

首次启动且数据目录中没有管理员账号时，打开页面即可按引导创建管理员用户名和密码。

## 安装凭据的行为

数据目录里没有 `DATA_DIR/.admin-credentials.applied` 标记（或标记与这组凭据不符）时，只要设置了 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，启动就会按这组凭据改名、重置现有管理员密码；目录里已有同名账号时只重置它的密码。这一步不会删除任何积分与记录，但会改动登录凭据。

想保住原有账号，要么别设这组变量，要么把 `DATA_DIR`（含标记文件）整体备份并原样恢复。

## 本地运行

```bash
pip install -r requirements.txt
python app.py            # 默认 http://127.0.0.1:9696，可用 PORT / DATA_DIR 覆盖
python -m unittest discover -s tests -q
```
