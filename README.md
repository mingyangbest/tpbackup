# tpbackup

联通云盘（沃盘）备份管理台。把「扫描本地目录 → 比对云端 → 增量上传 → 校验 → 记日志」收进一个容器，网页管理，定时调度，上传后逐文件复核。

为 NAS 上的照片、媒体库、文档这类「只增不改」的目录设计，也支持先在宿主机打包快照、再交给它上传的推送模式。

<!-- 建议在这里放一张 Web 界面截图：发布前到 http://<NAS-IP>:8088 截一张主界面 -->

## 功能特性

- **增量同步**：按「文件名 + 体积」比对云端，只传新文件和变过的文件；文件名大小写不敏感比对（联通云盘特性，避免 iOS 导出的 `FullSizeRender.heic` 与 `FULLSIZERENDER.HEIC` 互相覆盖）
- **两级复核**：上传后先比体积，云端返回了 SHA256 且文件不超过 64MB 时再比内容哈希——「云盘说收到了」升级成「内容确认一致」
- **试运行**：只扫描比对列出待传清单，一个字节都不传。新任务先试运行再真跑
- **定时调度**：每个任务一条 cron 表达式，改完即生效，不用重启
- **推送模式**：密码库导出、虚拟机镜像这类需要先在宿主机打包/加密的任务，由宿主机脚本打包后调 API 上传，支持云端保留 N 份滚动清理
- **恢复与演练**：界面直接浏览云端、单文件或整目录取回；附带恢复演练脚本，每月随机抽样真下载回来校验
- **令牌自管理**：云盘令牌有效期 7 天，剩余不足 2 天自动续期；可选与 OpenList 共用同一凭证（本应用作为唯一刷新者）
- **心跳外报**：每次运行结束把结果推给 Uptime Kuma 的 push 监控项，失败时带上原因，告警策略交给 Kuma
- 单文件前端，零外部依赖，内联图标离线可用，明暗双主题

## 快速开始

前置条件：一台装了 Docker（含 compose 插件）的 NAS 或 Linux 主机，一个联通云盘账号。

### 1. 获取云盘令牌

容器自己不走登录流程，第一次的令牌需要在外部获取。用 AList 官方的令牌工具：

```
https://alistgo.com/tool/wopan/token.html
```

支持账号密码、短信验证码、APP 扫码三种方式。两个必须注意的点：

1. **一定要拿到 `refresh_token`**。只有 `access_token` 的话 7 天后凭证就断了，系统没法自动续期。APP 扫码有时不返回刷新令牌，工具会明确提示，遇到就换短信验证码方式。
2. 取完令牌后**别再去网页端登录联通云盘**，网页端会把工具这条登录踢下线。手机端 APP 不受影响。

### 2. 部署

```bash
git clone https://github.com/mingyangbest/tpbackup.git
cd tpbackup

# 准备 compose
cp docker-compose.example.yml docker-compose.yml
# 编辑 docker-compose.yml：改 TP_PASS，改 TP_ALLOWED_ROOTS 和 volumes 里的路径

sudo docker compose up -d --build
```

首次构建几分钟。看到 `healthy` 即启动完成：

```bash
sudo docker ps --filter name=tpbackup
```

### 3. 放置令牌

```bash
sudo mkdir -p data/state
sudo tee data/state/token.json >/dev/null <<'EOF'
{
  "access_token": "<你的 access_token>",
  "refresh_token": "<你的 refresh_token>",
  "updated": "2026-01-01 00:00:00"
}
EOF
sudo chmod 600 data/state/token.json
sudo docker compose restart
```

`updated` 只用于计算剩余天数，填当前时间即可，续期后系统自己更新。

确认生效：

```bash
curl -u admin:<密码> http://127.0.0.1:8088/api/token
# 返回里有 remain_days ≈ 7 且 has_refresh: true 即可
```

浏览器打开 `http://<NAS-IP>:8088`，登录后新建一个任务，**先点「试运行」**确认待传清单，再点「立即备份」。

### 4. 宿主机脚本凭据（可选）

如果要用推送模式或巡检脚本，还需要：

```bash
cp api.conf.example api.conf
# 编辑 api.conf：账号密码与 compose 里的 TP_USER / TP_PASS 保持一致
```

## 配置参考

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TP_USER` / `TP_PASS` | admin / tpbackup | Web 与 API 的登录凭据。**默认密码仅为首次体验，务必修改** |
| `TP_DATA` | /app/data | 配置、令牌、日志、SQLite 的存放位置 |
| `TP_ALLOWED_ROOTS` | /vol1,/vol00 | 目录浏览与任务源目录的允许范围，逗号分隔。这是安全边界，任务源目录不能超出它 |
| `TP_PROTECT_FILES` | 空 | 推送任务滚动清理时永不删除的云端文件名，逗号分隔（如 `README.txt`） |
| `TP_OPENLIST_DB` | 空 | OpenList 的 data.db 路径，填了则首次启动从库中导入令牌 |
| `TP_SYNC_OPENLIST` | 0 | 刷新令牌后是否回写 OpenList 库 |
| `TP_SEED` | 0 | 首次启动是否导入示例任务（先改 `app/main.py` 里 `seed_tasks()` 的路径再开） |
| `TP_FILE_UID` / `TP_FILE_GID` | 1000 / 1000 | 恢复出来的文件属主 |
| `TP_CONTAINER` | tpbackup | 冒烟脚本（tools/smoke_test.py）操作的容器名 |

安全提醒：本服务能读写挂载的文件、持有云盘凭证。Basic Auth 未加密、无速率限制，**只建议在内网使用**；需要公网访问请套反向代理 + HTTPS，并使用强密码。

## 使用

### 目录同步（dir）与推送（push）

| | dir | push |
|---|---|---|
| 谁打包 | 不需要 | 宿主机脚本 |
| 谁调度 | 本应用（任务的 cron 字段） | 宿主机 cron |
| 界面操作 | 立即备份 / 试运行 | 无（调度在宿主机） |
| 适用 | 照片、媒体库、文档这类目录 | 需要先打快照/加密的任务 |

dir 任务在界面上「新增备份任务」即可：名称、源目录、云端路径（不存在自动建）、cron（留空只手动）、排除项（每行一个，支持通配符）。保存即生效。

push 任务先在界面上建同名任务（或让脚本首次调用时自动创建），然后在宿主机：

```bash
# 上传，云端保留最近 14 份
python3 push.py --task "Vaultwarden 密码库" --cloud VaultwardenBackup \
    --keep 14 --pattern 'vaultwarden-*.tar.gz' -- /path/包.tar.gz

# 查云端文件 / 下载回来（巡检用）
python3 fetch.py --stat --cloud HomeNetBackup --name a.enc
python3 fetch.py --out /tmp/a.enc --cloud HomeNetBackup --name a.enc

# 查某任务最近一次真实运行状态（预览不算）
python3 taskstat.py --task immich
```

`--keep` 按文件名字典序保留最新 N 份并删除其余（时间戳命名天然有序）；`--dry-run` 只校验不传输。

### 恢复

界面进入任务详情可浏览云端目录，「取回」支持单文件或整目录，落到 `TP_ALLOWED_ROOTS` 内的指定位置。`tools/drill.py` 做月度演练：随机抽样真实下载回来校验——界面上的「成功」只证明传得上去，演练证明取得回来。

### 与 OpenList 共用令牌

两个组件都持有云盘凭证时，必须只有一个刷新者，否则会互相覆盖把好凭证换成旧的。推荐分工：**tpbackup 是唯一权威刷新者**（提前 2 天续期），OpenList 只做浏览。

compose 里设 `TP_OPENLIST_DB` 和 `TP_SYNC_OPENLIST=1`，宿主机再跑 `tools/openlist_sync.py`（每 10 分钟一次的 crontab）：令牌变化时写回 OpenList 库并重启它重载（OpenList 把存储配置读在内存里，改库不生效，重启是唯一可靠方式；只在真变化时重启，约 5~7 天一次）。脚本顶部路径可用环境变量覆盖。

顺带一提：OpenList 的 WoPan 存储里 `family_id` 必须留空，填了真实家庭 ID 反而列出空的家庭空间。

## 数据与备份

```
data/
├── tpbackup.db         # 任务定义 + 运行历史（SQLite）
├── state/token.json    # 云盘令牌（600 权限）
└── logs/<任务名>.log    # 每个任务的完整运行日志
```

`data/` 建议纳入你自己的备份范围——里面有任务配置和令牌，丢了要重新配置和授权。

## 故障排查

```bash
sudo docker logs -f tpbackup                      # 服务日志
sudo docker exec tpbackup ls /app/data/logs       # 任务日志
curl -u admin:<密码> http://127.0.0.1:8088/api/token   # 令牌状态
python3 tools/smoke_test.py                       # 7 项关键接口冒烟
```

| 现象 | 原因 |
|---|---|
| 报 `token-refresh` | refresh_token 失效：当初只填了 access_token，或网页端登录把凭证踢下线了。重新取令牌 |
| 报 `cloud-unreachable` | 网络不通或 DNS 异常 |
| 上传大量失败 | 并发过高触发频控，把任务的 workers 调到 2 |
| 试运行列表为空 | 云端已存在同名同大小文件，属正常 |
| 目录浏览看不到文件 | 源目录不在 `TP_ALLOWED_ROOTS` 内，或 volumes 挂错了 |
| 宿主机脚本报 401 | `api.conf` 里的密码和 compose 里的不一致 |
| 界面一直显示「运行中」 | 容器重启留下的僵尸记录，再重启一次会自动清扫（启动时标记为 interrupted） |

## 项目结构

```
tpbackup/
├── Dockerfile              python:3.12-slim + curl（分片上传用）
├── docker-compose.example.yml
├── requirements.txt        fastapi / uvicorn / apscheduler / cryptography
├── api.conf.example        宿主机脚本的 API 凭据模板
├── app/
│   ├── main.py             FastAPI 路由与鉴权
│   ├── engine.py           联通云盘协议实现（分片、加密、重试、复核）
│   ├── store.py            令牌落盘续期 + SQLite
│   ├── runner.py           任务执行与实时进度
│   ├── scheduler.py        cron 调度 + 每日维护
│   ├── notify.py           Uptime Kuma 心跳
│   └── static/index.html   前端（单文件）
├── push.py / fetch.py / taskstat.py   宿主机脚本
└── tools/                  巡检与演练
    ├── smoke_test.py       关键接口冒烟（含路径穿越回归用例）
    ├── verify_e2e.py       真实上传链路端到端测试
    ├── drill.py            恢复演练
    └── openlist_sync.py    令牌同步给 OpenList
```

引擎里有几处针对联通云盘特性的处理，改代码前先看 `app/engine.py` 头部注释：强制 IPv4（该域名 AAAA 记录会挂死）、显式禁用代理、分片上传用 curl 发 multipart（比 urllib 手拼稳定）、文件名大小写不敏感建索引、频控 9999 退避重试且只有凭证错误才刷 token。

## 开发

```bash
python -m pip install -r requirements-dev.txt
python -m pyflakes app/ tools/ *.py
python tools/smoke_test.py        # 需要服务已在运行
```

提交 PR 前 CI 会跑语法编译、pyflakes 和 Docker 构建。

## 免责声明

本项目通过逆向工程实现联通云盘的非公开上传接口，**仅供个人学习与研究使用**：

- 本项目与联通公司及其云盘产品无任何关联，亦非官方工具
- 直接调用非公开接口可能违反服务条款，存在账号被风控、限速甚至封禁的风险，请自行评估
- 请仅备份你拥有权利备份的内容，遵守所在地的法律法规
- 因使用本项目造成的任何直接或间接损失，作者不承担责任

如权利方认为本项目侵犯其合法权益，请提 issue 联系，会及时处理。

## 许可证

[MIT](LICENSE)
