# 更新记录

## 2026-10-30（下午）

### 新增

- 界面粘贴令牌：`POST /api/token/paste` + 首页「Token 剩余」卡片里的「粘贴令牌」入口。粘贴 `access_token` / `refresh_token` 后，**先用这对令牌真连一次云端（列一次根目录），通过才落盘**，成功即生效、无需重启容器；验不过不改动现有凭证。快速开始第 3 步的 SSH 写文件降级为备选方案。
- 截图进仓库：三张实拍图放到 `docs/screenshots/`，README 改用仓库内路径，补了中文 alt 与图注。

### 修复

- README 的功能列表原先写着「界面直接浏览云端、单文件或整目录取回」，但界面入口已经移除。改为如实描述：取回走 `/api/restore` 接口，演练走 `tools/drill.py`。

### 说明

- 新增「安全边界」小节：明确 **dir 模式明文上传**，敏感数据请走 push 模式先加密；Web 端单账号 Basic Auth，只建议内网或套 HTTPS 反代。

## 2026-10-30

### 新增

- 孤儿扫描：dir 任务新增“孤儿扫描”按钮，后台扫描云端有、本地没有同名文件（大小写不敏感）的条目；孤儿文件逐条列出，孤儿目录整棵统计文件数与字节数；扫描只列清单、不删除云端文件；清单超 2000 条会截断提示。接口：POST /api/tasks/{tid}/orphans、GET /api/orphans/{jid}。
- 恢复演练进面板：管理台新增演练卡片，展示上次演练时间、抽样通过/失败数、超 45 天未演练提示与失败样本；演练报告由 tools/drill.py 跑完后自动上报（POST /api/drill/report），存于 DATA/state/drill.json，接口 GET /api/drill。
- 演练哈希抽查：drill.py 样本体积比对通过后，再算下载文件的 SHA256 并与云端 sha256 比对；不一致判失败，云端无哈希时不推翻体积结论；上报失败只记 warning，不影响演练退出码。

### 修复

- 演练卡片的「过期」判断从未生效：`/api/drill` 用 `datetime.strptime(at, "%F %T")` 解析时间，而 `%F` 只在 `strftime` 里有效，`strptime` 会抛 `ValueError` 被 `except` 吞掉，导致 `age_days` 恒为 `None`、`stale` 恒为 `False`。改为 `"%Y-%m-%d %H:%M:%S"`。
- 过期阈值统一为 45 天（原先面板 35 天、`check.sh` 45 天，口径不一致；演练每月 3 号跑，35 天会在下次演练前就误报过期）。

### 说明

- 孤儿扫描的排除规则与同步一致（DEFAULT_EXCLUDE_DIRS/FILES + 任务 excludes），云端根目录用只读方式解析，不会顺手建目录。
- 合并部署后，演练卡片在下次 drill 跑完上报前显示“暂无报告”，属正常现象。
