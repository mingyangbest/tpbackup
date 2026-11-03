#!/usr/bin/env bash
# tpbackup 一键安装。
#
#   curl -fsSL https://raw.githubusercontent.com/mingyangbest/tpbackup/main/install.sh | bash
#
# 也可以先下载再运行，效果一样：
#   bash install.sh
#
# 脚本只做四件事：建一个 tpbackup/ 目录、写好 compose 与 .env、
# 拉镜像起容器、打印访问地址和密码。重复运行不会覆盖已有的 .env 和数据。
#
# 想指定位置或端口，用环境变量：
#   TPBACKUP_DIR=/vol1/docker/tpbackup TPBACKUP_PORT=9000 bash install.sh

set -euo pipefail

IMAGE_REF="${TPBACKUP_IMAGE:-ghcr.io/mingyangbest/tpbackup:latest}"
PORT="${TPBACKUP_PORT:-8088}"
USER_NAME="${TPBACKUP_USER:-admin}"
NAME="${TPBACKUP_NAME:-tpbackup}"
PASS="${TPBACKUP_PASS:-}"
BACKUP_DIR="${TPBACKUP_BACKUP_DIR:-}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\n安装中止：%s\n' "$*" >&2; exit 1; }

# ── 1. 环境检查 ────────────────────────────────────────────
step "检查 Docker"
command -v docker >/dev/null 2>&1 || die "没找到 docker。先装 Docker（群晖/飞牛在应用中心装，Linux 用官方脚本）。"

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "Docker 装了，但缺少 compose 插件。Linux 上装 docker-compose-plugin，或升级 Docker 到 20.10 以上。"
fi
say "docker 与 compose 都在。"

docker info >/dev/null 2>&1 || die "连不上 Docker 服务。群晖/飞牛在控制面板里启动 Docker；Linux 上确认当前用户有权限（sudo usermod -aG docker \$USER 后重新登录）。"

# ── 2. 安装目录 ────────────────────────────────────────────
if [ -n "${TPBACKUP_DIR:-}" ]; then
  dir="$TPBACKUP_DIR"
elif [ -w "$PWD" ]; then
  dir="$PWD/tpbackup"
else
  dir="$HOME/tpbackup"
fi
mkdir -p "$dir" || die "建不了目录 $dir"
cd "$dir"

EXISTING=0
if [ -f .env ]; then
  EXISTING=1
  old_port=$(sed -n 's/^TP_PORT=//p' .env | head -1)
  if [ -n "$old_port" ]; then PORT="$old_port"; fi
  old_dir=$(sed -n 's/^BACKUP_DIR=//p' .env | head -1)
  if [ -n "$old_dir" ]; then BACKUP_DIR="$old_dir"; fi
  old_name=$(sed -n 's/^TP_NAME=//p' .env | head -1)
  if [ -n "$old_name" ]; then NAME="$old_name"; fi
  # 密码也读回来，只为最后打印对得上；.env 不重写
  old_pass=$(sed -n 's/^TP_PASS=//p' .env | head -1)
  if [ -n "$old_pass" ] && [ -z "$PASS" ]; then PASS="$old_pass"; fi
fi

# 默认密码随机生成 16 位十六进制；重复安装时不重新生成（.env 整个保留）
if [ -z "$PASS" ]; then
  PASS=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
fi

if [ -z "$BACKUP_DIR" ]; then
  BACKUP_DIR="$dir/library"
fi
mkdir -p "$BACKUP_DIR" || die "建不了备份目录 $BACKUP_DIR"
BACKUP_DIR=$(cd "$BACKUP_DIR" && pwd)

# ── 3. 端口 ────────────────────────────────────────────────
# 能连上就说明有人在监听（子 shell 退出时 fd 自动关）
port_taken() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

if [ "$EXISTING" = "0" ] && port_taken "$PORT"; then
  say "端口 $PORT 已被占用，往后找一个空的。"
  found=""
  for p in $(seq $((PORT + 1)) $((PORT + 20))); do
    if ! port_taken "$p"; then found="$p"; break; fi
  done
  [ -n "$found" ] || die "从 $PORT 往后 20 个端口都占着，用 TPBACKUP_PORT=xxxx 指定一个。"
  PORT="$found"
  say "改用 $PORT。"
fi

# ── 4. 写 compose 与 .env ──────────────────────────────────
step "写配置文件到 $dir"

# 这段必须和仓库里的 docker-compose.yml 逐字一致，
# tests/test_install_compose.py 会对比两者，改一处要同步另一处。
cat > docker-compose.yml <<'YAML'
# tpbackup —— 开箱即用的 compose 文件。
#
#   docker compose up -d
#
# 就这两步：拉预构建镜像、起容器。不需要 clone 代码，不需要 build。
# 想改端口 / 密码 / 备份哪个目录，在同目录放一个 .env（照 .env.example 抄一份），
# 下面的 ${...} 会从那里读；不写 .env 就全用括号里的默认值。
services:
  tpbackup:
    image: ${IMAGE_REF:-ghcr.io/mingyangbest/tpbackup:latest}
    container_name: ${TP_NAME:-tpbackup}
    restart: unless-stopped
    ports:
      - "${TP_PORT:-8088}:8088"
    environment:
      TZ: ${TZ:-Asia/Shanghai}
      # Web 与 API 的登录凭据。默认密码只为让你先看到界面，务必改掉
      TP_USER: ${TP_USER:-admin}
      TP_PASS: ${TP_PASS:-tpbackup}
      TP_DATA: /app/data
      # 界面里能浏览、能建任务的范围。默认 /data，就是下面挂进来的那个目录
      TP_ALLOWED_ROOTS: ${TP_ALLOWED_ROOTS:-/data}
    volumes:
      # 左边换成你要备份的目录，右边固定是容器里的 /data，别改
      - ${BACKUP_DIR:-./library}:/data
      # 任务配置、云盘令牌、运行日志、数据库
      - ./data:/app/data
    mem_limit: 512m
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
YAML

if [ "$EXISTING" = "0" ]; then
  cat > .env <<ENV
# 由 install.sh 生成。改完执行 docker compose up -d 生效。
TP_USER=$USER_NAME
TP_PASS=$PASS
BACKUP_DIR=$BACKUP_DIR
TP_ALLOWED_ROOTS=/data
TP_PORT=$PORT
TP_NAME=$NAME
IMAGE_REF=$IMAGE_REF
ENV
else
  say "已有 .env，保留其中的密码与目录，不覆盖。"
  if grep -q '^IMAGE_REF=' .env; then
    sed -i.bak "s|^IMAGE_REF=.*|IMAGE_REF=$IMAGE_REF|" .env && rm -f .env.bak
  else
    printf 'IMAGE_REF=%s\n' "$IMAGE_REF" >> .env
  fi
fi

mkdir -p data

# ── 5. 起容器 ──────────────────────────────────────────────
step "拉镜像 $IMAGE_REF"
if [ "${TPBACKUP_SKIP_PULL:-0}" = "1" ]; then
  say "跳过拉取（TPBACKUP_SKIP_PULL=1），直接用本地镜像。"
elif ! $COMPOSE pull; then
  # 离线、或用的是自己 build 的镜像时，本地有就能继续
  if docker image inspect "$IMAGE_REF" >/dev/null 2>&1; then
    say "拉取失败，但本地已经有 $IMAGE_REF，用它继续。"
  else
    say ""
    say "镜像拉不下来。常见原因："
    say "  · 网络/DNS 不通，或 NAS 没配镜像加速"
    say "  · 提示 denied / unauthorized：镜像包还没公开"
    say ""
    say "改用源码构建也能装（多花几分钟编译）："
    say "  git clone https://github.com/mingyangbest/tpbackup.git"
    say "  cd tpbackup && docker compose -f docker-compose.build.yml up -d --build"
    exit 1
  fi
fi

step "启动容器"
$COMPOSE up -d

step "等待服务就绪"
ok=""
for _ in $(seq 1 45); do
  st=$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo none)
  if [ "$st" = "healthy" ]; then ok=1; break; fi
  if [ "$st" = "unhealthy" ]; then break; fi
  sleep 2
done

# ── 6. 收尾 ────────────────────────────────────────────────
# 取默认路由出口的地址。NAS 上 hostname -I 的第一个往往是网桥或虚拟机地址
# （实测 R1 上第一项是 192.168.124.112，真地址在后面），照着打印的地址打不开。
ip=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
if [ -z "$ip" ]; then
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
if [ -z "$ip" ]; then ip="<这台机器的IP>"; fi

say ""
if [ -n "$ok" ]; then
  say "装好了。"
else
  say "容器起来了，但健康检查还没通过。看日志：docker logs $NAME"
fi
say ""
say "  打开      http://$ip:$PORT"
say "  账号      $USER_NAME"
say "  密码      $PASS"
say "  备份目录  $BACKUP_DIR  （容器里是 /data）"
say "  安装位置  $dir"
say ""
say "接下来三步："
say "  1. 浏览器打开上面的地址，用账号密码登录"
say "  2. 右上角「Token 剩余」卡片 → 粘贴令牌。令牌在 https://alistgo.com/tool/wopan/token.html 取，"
say "     一定要把 refresh_token 一起拿回来，只填 access_token 的话 7 天就断"
say "  3. 新建备份任务 → 先点「试运行」看待传清单 → 再点「立即备份」"
say ""
say "改密码 / 换备份目录：编辑 $dir/.env，再 cd $dir && docker compose up -d"
