#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署入口的一致性检查。

install.sh 里内嵌了一份 docker-compose.yml，好处是「一条命令装完」不依赖
别的文件，坏处是同一个文件有了两个副本。这两份一旦漂移，新手照 README
装出来的东西和他以为的不一样，而且很难看出来——所以用测试钉住。

另外查 .env.example：里面出现的每个键都必须真有人读，否则等于给新手
一个改了没反应的旋钮。
"""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HEREDOC_START = "cat > docker-compose.yml <<'YAML'\n"
HEREDOC_END = "\nYAML\n"


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def embedded_compose(install_sh):
    i = install_sh.index(HEREDOC_START) + len(HEREDOC_START)
    j = install_sh.index(HEREDOC_END, i)
    return install_sh[i:j] + "\n"


def test_install_sh_embedded_compose_matches_repo():
    """install.sh 内嵌的 compose 必须和仓库里的逐字一致。"""
    assert embedded_compose(read("install.sh")) == read("docker-compose.yml")


def test_env_example_keys_are_used_by_compose():
    """`.env.example` 里的每个键都要在 compose 里被读到。"""
    compose = read("docker-compose.yml")
    keys = re.findall(r"^([A-Z][A-Z0-9_]*)=", read(".env.example"), re.M)
    assert keys, "`.env.example` 里一个键都没解析到，检查格式"
    unused = [k for k in keys if "${" + k not in compose and "${" + k + ":" not in compose]
    assert not unused, f"这些键在 .env.example 里有、compose 里没人读：{unused}"


def test_compose_defaults_are_beginner_safe():
    """开箱默认值：白名单恰好覆盖挂载点，密码与端口都有默认值。"""
    compose = read("docker-compose.yml")
    # 挂载点固定 /data，白名单默认 /data——两者不一致的话新手建任务会看不到目录
    assert "- ${BACKUP_DIR:-./library}:/data" in compose
    assert "TP_ALLOWED_ROOTS: ${TP_ALLOWED_ROOTS:-/data}" in compose
    assert "${TP_PORT:-8088}:8088" in compose
    assert "${TP_PASS:-tpbackup}" in compose
    # 数据目录必须落在 compose 同级的 ./data，删容器不丢配置
    assert "- ./data:/app/data" in compose


def test_install_sh_syntax():
    """至少保证脚本能被 bash 解析（不执行）。"""
    r = subprocess.run(["bash", "-n", os.path.join(ROOT, "install.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
