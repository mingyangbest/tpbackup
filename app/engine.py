#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联通云盘直传引擎

从既有的 camera_to_wopan_direct.py / album_to_wopan_direct.py /
immich_to_wopan_direct.py 提炼而来。**协议实现一行未改**，只做了三件事：

  1. 把写死的 SRC / CLOUD_CHAIN / 日志路径改成参数
  2. 把 main() 的流程抽成 sync()，可被 Web 应用反复调用
  3. 日志与进度改为回调，供前端实时展示

原脚本的这些设计原样保留（都是踩过坑换来的）：
  · 强制 IPv4（该域名 AAAA 记录会挂死）
  · 显式禁用代理（避免继承系统代理走旁路由）
  · 分片上传用 curl 发 multipart（比 urllib 手拼稳定）
  · 文件名大小写不敏感建索引（联通云盘特性）
  · 频控 9999 退避重试，只有凭证错误才刷 token
  · 跑到一半 token 到期的补传逻辑
"""
import base64
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ── 强制 IPv4：该域名 AAAA 记录走不通，会挂死 ─────────────────
_orig_gai = socket.getaddrinfo


def _gai_v4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_gai(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _gai_v4

# 显式不使用任何代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ── 协议常量 ────────────────────────────────────────────────
# 以下常量逆向自联通云盘官方客户端（与 AList/OpenList 的 wopan 驱动同源），
# 属非公开接口，仅供学习研究。厂商若调整协议或提出异议，删除本节即可——
# 项目的 Web 管理台、调度、日志等其余部分不依赖它。
IV = b"wNSOYIB1k1DjY5lA"
SECRET = b"XFmi9GS2hzk98jGX"
BASE = "https://panservice.mail.wo.cn"
UPLOAD_URL = "https://tjupload.pan.wo.cn/openapi/client/upload2C"
CLIENT_ID = "1001000021"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36 Edg/114.0.1823.37")
PART = 8 * 1024 * 1024

# 恢复出来的文件归属：默认给 NAS 主用户（fnOS 的 1000:1000）
RESTORE_UID = int(os.environ.get("TP_FILE_UID", "1000"))
RESTORE_GID = int(os.environ.get("TP_FILE_GID", "1000"))

TOKEN_TTL = 604799              # AppRefreshToken 稳定返回 7 天
TOKEN_RENEW_AHEAD = 2 * 86400   # 剩余不足 2 天就提前换
CRED_CODES = {"8005", "1001"}   # 只有这两个才值得刷 token

DEFAULT_EXCLUDE_DIRS = {".@#local", "#recycle", "@eaDir", "$RECYCLE.BIN", "System Volume Information"}
DEFAULT_EXCLUDE_FILES = {"dockports-icon.png", ".DS_Store", "Thumbs.db"}

# 上传后做内容哈希复核的文件大小上限：超过它只比体积，避免大文件双倍 I/O
VERIFY_SHA256_MAX = 64 * 1048576


class CredentialError(RuntimeError):
    pass


def err_code(e):
    s = str(e)
    for c in CRED_CODES:
        if c in s:
            return c
    return None


def is_cred(e):
    return isinstance(e, CredentialError) or err_code(e) is not None


# ── 加解密 ──────────────────────────────────────────────────
def pkcs7(b, bs=16):
    n = bs - len(b) % bs
    return b + bytes([n] * n)


def aes_enc(data, key):
    c = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return c.update(pkcs7(data)) + c.finalize()


def aes_dec(data, key):
    c = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
    out = c.update(data) + c.finalize()
    return out[:-out[-1]] if out else out


# ── 客户端 ──────────────────────────────────────────────────
class Wopan:
    """联通云盘客户端。逻辑与既有脚本完全一致。"""

    def __init__(self, at, rt, on_token=None):
        self.at = at
        self.rt = rt
        self.key = at[:16].encode()
        self.family_id = None
        self._on_token = on_token     # token 刷新后的回调（用于落盘）

    def _post(self, url, payload, timeout=120):
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accesstoken", self.at)
        req.add_header("Origin", "https://pan.wo.cn")
        req.add_header("Referer", "https://pan.wo.cn/")
        req.add_header("User-Agent", UA)
        with _OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def call(self, channel, method, param, other=None, timeout=120, retries=2):
        """统一出口。凭证类错误立刻抛；频控/网络类退避重试，绝不误刷 token。"""
        last = None
        for i in range(retries + 1):
            try:
                return self._call_once(channel, method, param, other, timeout)
            except CredentialError:
                raise
            except Exception as e:
                last = e
                if i >= retries:
                    break
                time.sleep(3 * (2 ** i))
        raise last

    def _call_once(self, channel, method, param, other, timeout):
        res_time = int(time.time() * 1000)
        req_seq = random.randint(1, 9000) + 100000
        sign = hashlib.md5(("%s%d%d%s%s" % (
            method, res_time, req_seq, channel, "")).encode()).hexdigest()
        ek = SECRET if channel == "api-user" else self.key
        body = dict(other or {"secret": True})
        if param is not None:
            body["param"] = base64.b64encode(
                aes_enc(json.dumps(param, ensure_ascii=False).encode(), ek)).decode()
        payload = {"header": {"key": method, "resTime": res_time, "reqSeq": req_seq,
                              "channel": channel, "sign": sign, "version": ""},
                   "body": body}
        r = self._post(BASE + "/" + channel + "/dispatcher", payload, timeout)
        if r.get("STATUS") != "200":
            code = str(r.get("STATUS"))
            msg = "STATUS=%s MSG=%s" % (code, r.get("MSG"))
            raise CredentialError(msg) if code in ("401", "403") else RuntimeError(msg)
        rsp = r.get("RSP") or {}
        if rsp.get("RSP_CODE") != "0000":
            code = str(rsp.get("RSP_CODE"))
            msg = "RSP_CODE=%s DESC=%s" % (code, rsp.get("RSP_DESC"))
            raise CredentialError(msg) if code in CRED_CODES else RuntimeError(msg)
        data = rsp.get("DATA")
        if isinstance(data, str) and data:
            try:
                data = aes_dec(base64.b64decode(data), ek).decode()
            except Exception:
                pass
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass
        return data

    def refresh(self):
        r = self.call("api-user", "AppRefreshToken",
                      {"refreshToken": self.rt, "clientSecret": SECRET.decode()},
                      other={"clientId": CLIENT_ID, "secret": True})
        r = r if isinstance(r, dict) else {}
        if not r.get("access_token"):
            raise RuntimeError("刷新 token 失败: %r" % (r,))
        self.at = r["access_token"]
        self.rt = r.get("refresh_token") or self.rt
        self.key = self.at[:16].encode()
        if self._on_token:
            self._on_token(self.at, self.rt)
        return self

    def ensure_family(self):
        if self.family_id:
            return self.family_id
        d = self.call("wohome", "FamilyUserCurrentEncode", {"clientId": CLIENT_ID})
        self.family_id = str((d or {}).get("defaultHomeId") or "")
        return self.family_id

    def ls(self, dirid):
        out, page = [], 0
        while True:
            d = self.call("wohome", "QueryAllFiles", {
                "spaceType": "0", "parentDirectoryId": str(dirid),
                "pageNum": page, "pageSize": 100, "sortRule": 0, "clientId": CLIENT_ID})
            fs = (d or {}).get("files") or []
            out.extend(fs)
            if len(fs) < 100:
                break
            page += 1
        return out

    def mkdir(self, parent, name, retries=5):
        """建目录。服务端对短时间大量建目录会返回 9999（频控），
        重试前先查一次，避免重复创建。"""
        last = None
        for i in range(retries):
            try:
                d = self.call("wohome", "CreateDirectory", {
                    "spaceType": "0", "familyId": self.ensure_family(),
                    "parentDirectoryId": str(parent),
                    "directoryName": name, "clientId": CLIENT_ID})
                time.sleep(0.15)
                return (d or {}).get("id")
            except Exception as e:
                last = e
                time.sleep(1.5 * (i + 1))
                try:
                    for f in self.ls(parent):
                        if f.get("name") == name and f.get("type") == 0:
                            return f.get("id")
                except Exception:
                    pass
        raise last

    def upload(self, path, dirid, name=None, on_part=None):
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        part = min(PART, size) if size else 1
        total = size // PART or 1
        ftype = "5"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in ("jpg", "jpeg", "png", "gif", "bmp", "webp", "heic"):
            ftype = "1"
        elif ext in ("mp4", "mov", "mkv", "avi", "flv", "wmv"):
            ftype = "2"
        info = base64.b64encode(aes_enc(json.dumps({
            "spaceType": "0", "directoryId": str(dirid),
            "batchNo": time.strftime("%Y%m%d%H%M%S"), "fileName": name,
            "fileSize": size, "fileType": ftype,
        }, ensure_ascii=False, separators=(",", ":")).encode(), self.key)).decode()
        uid = "%d_%s" % (int(time.time() * 1000), "".join(
            random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
            for _ in range(6)))
        fid, done = "", 0
        last_err = None
        with open(path, "rb") as f:
            for idx in range(1, total + 1):
                cur = part if idx != total else size - done
                blob = f.read(cur)
                fields = {"uniqueId": uid, "accessToken": self.at, "fileName": name,
                          "psToken": "undefined", "fileSize": str(size), "totalPart": str(total),
                          "channel": "wocloud", "directoryId": str(dirid), "fileInfo": info,
                          "partSize": str(cur), "partIndex": str(idx)}
                for attempt in range(3):
                    try:
                        r = self._upload_part(fields, name, blob)
                        if r.get("code") != "0000":
                            raise RuntimeError("code=%s msg=%s" % (r.get("code"), r.get("msg")))
                        if (r.get("data") or {}).get("fid"):
                            fid = r["data"]["fid"]
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        time.sleep(2 * (attempt + 1))
                if last_err:
                    raise last_err
                done += cur
                if on_part:
                    on_part(idx, total)
        return fid

    def _upload_part(self, fields, filename, blob):
        """用 curl 发 multipart：实测比 urllib 手拼稳定。

        临时文件固定名 blob.bin，multipart 的 filename 传净化后的名字
        （真实文件名同时也在 fields.fileName 里）。不能直接拼原始文件名：
        curl 的 -F 值里出现 ``;`` / ``@`` / ``<`` / 引号会干扰它解析子参数，
        中文等普通字符不受影响，原样保留。
        """
        d = tempfile.mkdtemp(prefix="wpart_")
        try:
            p = os.path.join(d, "blob.bin")
            with open(p, "wb") as f:
                f.write(blob)
            safe_name = filename
            for ch in ';@"<>\'\r\n':
                safe_name = safe_name.replace(ch, "_")
            args = ["curl", "-s", "-m", "900", "-X", "POST", UPLOAD_URL,
                    "-H", "Origin: https://pan.wo.cn", "-H", "Referer: https://pan.wo.cn/",
                    "-A", UA, "--ipv4",
                    "-F", "file=@%s;filename=%s" % (p, safe_name)]
            for k, v in fields.items():
                args += ["-F", "%s=%s" % (k, v)]
            r = subprocess.run(args, capture_output=True, text=True)
            if not r.stdout.strip():
                raise RuntimeError("curl 空响应: %s" % (r.stderr.strip()[:200]))
            return json.loads(r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ── 目录链与扫描 ────────────────────────────────────────────
def ensure_chain(w, names, log=lambda m: None):
    cid = "0"
    for n in names:
        found = None
        for f in w.ls(cid):
            if f.get("type") == 0 and f.get("name") == n:
                found = f.get("id")
                break
        if not found:
            found = w.mkdir(cid, n)
            log("新建云端目录: %s" % n)
        cid = found
    return cid


# ── 云端删除与保留策略 ──────────────────────────────────────
# 方法签名取自 OpenList 的 wopan 驱动（drivers/wopan/driver.go）与 SDK
# （wopan-sdk-go/api-fs.go）：DeleteFile 需要 dirList / fileList 两个数组，
# 不是官方文档里的单个 fileId；参数名写错服务端只会回「请求参数错误」。
def delete_entries(w, file_ids=None, dir_ids=None):
    if not file_ids and not dir_ids:
        return True
    w.call("wohome", "DeleteFile", {
        "spaceType": "0", "vipLevel": "0", "clientId": CLIENT_ID,
        "dirList": list(dir_ids or []), "fileList": list(file_ids or []),
    })
    return True


def prune_cloud(w, cid, pattern, keep, log=lambda m: None, protect=()):
    """只保留文件名匹配 pattern 的最新 keep 份，其余删除。

    只处理「文件」，绝不动目录；名称按字典序倒排（时间戳命名天然按时间有序）。
    protect 内的名字永不删除（例如 README.txt 不匹配 pattern，这里再兜一层）。
    """
    import fnmatch
    if keep <= 0 or not pattern:
        return {"deleted": 0, "kept": 0}
    try:
        entries = [f for f in w.ls(cid) if f.get("type") == 1]
    except Exception as e:
        log("保留策略：列目录失败 %s" % e)
        return {"deleted": 0, "kept": 0, "error": str(e)[:120]}

    matched = [f for f in entries
               if fnmatch.fnmatch(f.get("name") or "", pattern)
               and (f.get("name") or "") not in protect]
    matched.sort(key=lambda x: (x.get("name") or ""), reverse=True)
    drop = matched[keep:]
    # 只删文件，空列表保护：dirList 永远传空
    ids = [f.get("id") for f in drop if f.get("id")]
    if ids:
        try:
            delete_entries(w, file_ids=ids)
            log("保留策略：保留最新 %d 份，清理 %d 份旧包" % (keep, len(ids)))
        except Exception as e:
            log("保留策略：删除失败（不影响本次备份）%s" % e)
            return {"deleted": 0, "kept": len(matched), "error": str(e)[:120]}
    return {"deleted": len(ids), "kept": len(matched)}


# ── 读取云端文件（供巡检脚本验证"能不能下回来"）──────────────
def find_file(w, chain, name):
    """按目录链查一个文件，返回文件条目 dict；找不到返回 None。"""
    cid = "0"
    for n in chain:
        nxt = None
        for f in w.ls(cid):
            if f.get("type") == 0 and f.get("name") == n:
                nxt = f.get("id")
                break
        if not nxt:
            return None
        cid = nxt
    for f in w.ls(cid):
        if f.get("type") == 1 and (f.get("name") or "") == name:
            return f
    return None


def download_file(w, item, out_path, chunk=262144):
    """取下载地址并把文件落到本地。返回字节数。

    fidList 必须传条目里的 fid（形如 VpP01_...），传 id（32 位 uuid）
    服务端会回 RSP_CODE=9999 系统异常——这一点是实测出来的。
    """
    d = w.call("wohome", "GetDownloadUrlV2",
               {"type": "1", "fidList": [item.get("fid")], "clientId": CLIENT_ID})
    lst = (d or {}).get("list") or []
    url = lst[0].get("downloadUrl") if lst else ""
    if not url:
        raise RuntimeError("未取到下载地址")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    n = 0
    with _OPENER.open(req, timeout=900) as r, open(out_path, "wb") as f:
        while True:
            b = r.read(chunk)
            if not b:
                break
            f.write(b)
            n += len(b)
    return n


def find_dir(w, chain):
    """按名称链找目录，只读不建。找不到返回 None。
    用途：恢复/浏览时不能像同步那样顺手 mkdir，否则会把拼错的路径建成空目录。"""
    cur = "0"
    for name in chain or []:
        hit = None
        for f in w.ls(cur):
            if f.get("name") == name and f.get("type") == 0:
                hit = str(f.get("id"))
                break
        if not hit:
            return None
        cur = hit
    return cur


def walk_usage(w, dirid, max_files=300000):
    """递归统计目录下文件数与总字节。云端不给目录大小，只能遍历。
    返回 (files, bytes)；max_files 是保险丝，防止在大目录里无限翻页。"""
    files, total = 0, 0
    stack = [str(dirid)]
    while stack:
        d = stack.pop()
        for f in w.ls(d):
            if f.get("type") == 0:
                stack.append(str(f.get("id")))
                continue
            files += 1
            try:
                total += int(f.get("size") or 0)
            except Exception:
                pass
            if files >= max_files:
                return files, total
    return files, total


def _fix_perm(path, is_dir=False):
    """恢复出来的文件归 NAS 用户所有，并给正常权限。

    2026-10-28 实测：部分 NAS（fnOS 的用户主目录）带默认 ACL，容器里以 root
    新建的目录/文件权限是 000（连 chmod 755 都会被 ACL mask 削成 705），
    不修的话恢复出来的东西用户在文件管理器里打不开。
    """
    try:
        os.chmod(path, 0o755 if is_dir else 0o644)
    except Exception:
        pass
    try:
        os.chown(path, RESTORE_UID, RESTORE_GID)
    except Exception:
        pass


def download_tree(w, dirid, dest, log=lambda m: None, max_files=5000):
    """把云端目录递归取回本地。返回 (files, bytes, skipped)。
    恢复功能和「恢复演练」共用这一条路径，保证演练验的就是真恢复。"""
    files = nbytes = skipped = 0
    stack = [(str(dirid), "")]
    while stack:
        d, rel = stack.pop()
        for f in w.ls(d):
            name = f.get("name") or ""
            if not name or name in (".", ".."):
                continue
            target_rel = os.path.join(rel, name) if rel else name
            target = os.path.join(dest, target_rel)
            if f.get("type") == 0:
                os.makedirs(target, exist_ok=True)
                _fix_perm(target, True)
                stack.append((str(f.get("id")), target_rel))
                continue
            if files >= max_files:
                skipped += 1
                continue
            os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
            try:
                n = download_file(w, f, target)
                _fix_perm(target, False)
                files += 1
                nbytes += n
                if files % 50 == 0:
                    log("已取回 %d 个文件 / %.1f MB" % (files, nbytes / 1048576.0))
            except Exception as e:
                skipped += 1
                log("取回失败 %s: %s" % (target_rel, str(e)[:120]))
    return files, nbytes, skipped


def size_of(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def sha256_of(p, chunk=1 << 20):
    """本地文件的内容哈希。云端 QueryAllFiles 会返回它自己的 sha256，
    两者相等即可证明「字节完全一致」，比只比体积强得多。"""
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(chunk), b""):
                h.update(b)
        return h.hexdigest()
    except OSError:
        return ""


def match_exclude(name, is_dir, excludes):
    """支持 glob 与目录名精确匹配。"""
    import fnmatch
    for pat in excludes or []:
        if fnmatch.fnmatch(name, pat):
            return True
        if is_dir and name == pat:
            return True
    return False


def collect(w, src, cid, jobs, stats, exclude_dirs, exclude_files, rel="", log=lambda m: None, seen=None):
    # seen 记录已进入过的目录 (st_dev, st_ino)：目录里若有一个指向祖先的软链接，
    # 裸递归会无限展开直到把进程打挂——这是文件扫描器的经典死法，必须防。
    if seen is None:
        seen = set()
    try:
        st = os.stat(src)
        key = (st.st_dev, st.st_ino)
        if key in seen:
            log("跳过循环链接: %s" % (rel or src))
            stats["skip"] += 1
            return
        seen.add(key)
    except OSError as e:
        log("无法访问 %s: %s" % (src, e))
        stats["err"] += 1
        return
    try:
        cloud = {}
        for f in w.ls(cid):
            # 联通云盘文件名大小写不敏感：统一小写建索引，
            # 否则 FullSizeRender.heic 与 FULLSIZERENDER.HEIC 会互相覆盖
            cloud[(f.get("name") or "").lower()] = f
    except Exception as e:
        log("列云端失败 %s: %s" % (cid, e))
        raise
    try:
        entries = sorted(os.listdir(src))
    except Exception as e:
        log("列本地失败 %s: %s" % (src, e))
        return
    for name in entries:
        p = os.path.join(src, name)
        try:
            if os.path.isdir(p):
                if name in exclude_dirs or match_exclude(name, True, exclude_dirs):
                    stats["skip"] += 1
                    continue
                c = cloud.get(name.lower())
                if c and c.get("type") == 0:
                    sid = c.get("id")
                else:
                    sid = w.mkdir(cid, name)
                collect(w, p, sid, jobs, stats, exclude_dirs, exclude_files,
                        rel + "/" + name, log, seen)
            else:
                if name in exclude_files or match_exclude(name, False, exclude_files):
                    stats["skip"] += 1
                    continue
                sz = size_of(p)
                if sz <= 0:
                    stats["skip"] += 1
                    continue
                c = cloud.get(name.lower())
                if c and c.get("size") == sz:
                    stats["exist"] += 1
                    continue
                jobs.append((p, cid, name, sz, rel + "/" + name))
                stats["todo"] += 1
        except Exception as e:
            log("处理出错 %s: %s" % (p, e))
            stats["err"] += 1


# ── 一次完整同步 ────────────────────────────────────────────
def sync(store, src, cloud_chain, excludes_dirs=None, excludes_files=None,
         dry_run=False, limit=0, workers=4, log=lambda m: None,
         progress=None, should_stop=None, verify=True):
    """执行一次同步。

    store  : TokenStore 实例，负责 token 的读写与续期
    progress: 可选回调 fn(dict)，用于向 Web 推送实时状态
    should_stop: 可选回调，返回 True 时中止（用于 Web 上点"停止"）
    verify : 上传后做内容复核（体积全比 + ≤VERIFY_SHA256_MAX 的文件比 SHA256）。
             真实上传路径默认开启；dry-run 不经过上传，与本参数无关。
    返回 dict(stats)
    """
    result = {"ok": False, "reason": "", "todo": 0, "ok_n": 0, "fail": 0,
              "exist": 0, "skip": 0, "bytes": 0, "elapsed": 0, "detail": ""}

    if not os.path.isdir(src):
        result["reason"] = "src-missing"
        log("SKIP 源目录不存在: %s" % src)
        return result

    at, rt = store.load()
    if not at:
        result["reason"] = "no-token"
        log("没有可用 token")
        return result

    w = Wopan(at, rt, on_token=store.save)
    store.maybe_renew(w, log)

    # 连通性自检：凭证类错误才恢复凭证；频控/网络类已在 call() 里退避重试
    connected = False
    for attempt in (1, 2, 3):
        try:
            w.ensure_family()
            connected = True
            break
        except Exception as e:
            if is_cred(e) and attempt < 3:
                log("凭证失效（%s），恢复凭证后重试（第 %d 次）" % (err_code(e) or e, attempt))
                if not store.recover(w, prefer_db=(attempt == 1)):
                    result["reason"] = "token-refresh"
                    result["detail"] = str(e)[:200]
                    return result
                continue
            log("云端连接检查失败: %s" % e)
            result["reason"] = "cloud-unreachable"
            result["detail"] = str(e)[:200]
            return result
    if not connected:
        result["reason"] = "token-invalid"
        return result

    t0 = time.time()
    log("=== 开始同步 dry_run=%s workers=%d ===" % (dry_run, workers))
    try:
        root_id = ensure_chain(w, cloud_chain, log)
    except Exception as e:
        log("建根路径失败: %s" % e)
        result["reason"] = "cloud-path"
        result["detail"] = str(e)[:200]
        return result
    log("云端根目录 id=%s" % root_id)

    exd = set(DEFAULT_EXCLUDE_DIRS) | set(excludes_dirs or [])
    exf = set(DEFAULT_EXCLUDE_FILES) | set(excludes_files or [])

    stats = {"todo": 0, "exist": 0, "skip": 0, "err": 0}
    jobs = []
    try:
        collect(w, src, root_id, jobs, stats, exd, exf, "", log)
    except Exception as e:
        log("遍历失败: %s" % e)
        result["reason"] = "walk"
        result["detail"] = str(e)[:200]
        return result

    total_bytes = sum(j[3] for j in jobs)
    result.update(todo=len(jobs), exist=stats["exist"], skip=stats["skip"])
    log("扫描完成：待传 %d 个 / %.2f GB，已存在 %d，跳过 %d，错误 %d" % (
        len(jobs), total_bytes / 1073741824, stats["exist"], stats["skip"], stats["err"]))

    if dry_run:
        for j in jobs[:20]:
            log("  待传 %s (%.1f MB)" % (j[4], j[3] / 1048576))
        result.update(ok=True, reason="dry-run", elapsed=time.time() - t0)
        return result

    if limit:
        jobs = jobs[:limit]

    done_n, done_b, fail = 0, 0, 0
    err_msgs, failed, succeeded = [], [], []
    lock = threading.Lock()

    def work(j):
        nonlocal done_n, done_b, fail
        p, cid, name, sz, relp = j
        if should_stop and should_stop():
            return False
        t = time.time()
        try:
            w.upload(p, cid, name)
            el = time.time() - t
            with lock:
                done_n += 1
                done_b += sz
                succeeded.append(j)
                if progress:
                    progress({"phase": "upload", "done": done_n, "total": len(jobs),
                              "bytes": done_b, "name": relp,
                              "kbs": (sz / 1024 / el) if el else 0})
            return True
        except Exception as e:
            with lock:
                fail += 1
                err_msgs.append("%s: %s" % (relp, e))
                failed.append((j, str(e)))
            log("失败 %s: %s" % (relp, e))
            return False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, jobs))

    # 跑到一半 token 到期：失败项含凭证类错误就刷一次再补传（只补一次，防触发风控）
    if failed and any(is_cred(x[1]) for x in failed):
        retry_jobs = [x[0] for x in failed]
        log("失败项含凭证类错误，刷新 token 后补传 %d 个" % len(retry_jobs))
        try:
            w.refresh()
        except Exception as e:
            log("刷新 token 失败，放弃补传: %s" % e)
        else:
            with lock:
                failed.clear(); err_msgs.clear(); fail = 0
            with ThreadPoolExecutor(max_workers=min(2, len(retry_jobs))) as ex:
                list(ex.map(work, retry_jobs))

    el = time.time() - t0
    avg = (done_b / 1024 / el) if el else 0
    log("=== 结束：成功 %d，失败 %d，共 %.2f GB，耗时 %.0fs，平均 %.0f KB/s ===" % (
        done_n, fail, done_b / 1073741824, el, avg))
    if err_msgs:
        log("失败样例: " + " | ".join(err_msgs[:5]))

    # ── 上传后复核：把「云盘说收到了」升级成「内容确认一致」────────────
    # 按目录分组复查（只对传过文件的目录各补一次列表）：
    #   体积全比；云端返回了 sha256 且文件 ≤64MB 的再比内容哈希。
    # 大文件只比体积是刻意的取舍——全量哈希会让 1GB 批次的 I/O 翻倍。
    verify_bad, hashed = [], 0
    if verify and succeeded:
        by_dir = {}
        for j in succeeded:
            by_dir.setdefault(j[1], []).append(j)
        for cid2, jlist in by_dir.items():
            try:
                cloud = {(f.get("name") or "").lower(): f
                         for f in w.ls(cid2) if f.get("type") == 1}
            except Exception as e:
                log("复核时列目录失败（不影响已完成上传）: %s" % e)
                continue
            for (p2, _c, name2, sz2, relp2) in jlist:
                c = cloud.get(name2.lower())
                if not c:
                    verify_bad.append("%s 云端未找到" % relp2)
                    continue
                if str(c.get("size")) != str(sz2):
                    verify_bad.append("%s 体积不符 本地=%s 云端=%s" % (relp2, sz2, c.get("size")))
                    continue
                rh = (c.get("sha256") or "").lower()
                if rh and sz2 <= VERIFY_SHA256_MAX:
                    if sha256_of(p2) == rh:
                        hashed += 1
                    else:
                        verify_bad.append("%s 内容哈希不符（传输损坏）" % relp2)
        if verify_bad:
            log("❌ 复核未通过 %d 项：%s" % (len(verify_bad), "；".join(verify_bad[:5])))
        else:
            log("✅ 复核通过：%d 个文件%s" % (done_n,
                ("，SHA256 一致 %d 个" % hashed) if hashed else ""))

    all_errs = err_msgs + verify_bad
    result.update(ok=(not all_errs), ok_n=done_n, fail=fail + len(verify_bad),
                  bytes=done_b, elapsed=el,
                  reason="" if not all_errs else ("upload" if fail else "verify"),
                  detail=(all_errs[0][:200] if all_errs else ""))
    return result


# ── 推送上传：把宿主机打好包的文件送上去 ────────────────────
def _connect(store, log):
    """建立可用连接。与 sync() 的前置检查完全一致：提前续期 + 连通自检 +
    凭证类错误才恢复凭证。返回 (w, result)；result 非空表示失败。"""
    res = {"ok": False, "reason": "", "todo": 0, "ok_n": 0, "fail": 0,
           "exist": 0, "skip": 0, "bytes": 0, "elapsed": 0, "detail": "", "files": []}
    at, rt = store.load()
    if not at:
        res["reason"] = "no-token"
        log("没有可用 token")
        return None, res

    w = Wopan(at, rt, on_token=store.save)
    store.maybe_renew(w, log)

    for attempt in (1, 2, 3):
        try:
            w.ensure_family()
            return w, res
        except Exception as e:
            if is_cred(e) and attempt < 3:
                log("凭证失效（%s），恢复凭证后重试（第 %d 次）" % (err_code(e) or e, attempt))
                if store.recover(w, prefer_db=(attempt == 1)):
                    continue
                res["reason"] = "token-refresh"
                res["detail"] = str(e)[:200]
                return None, res
            log("云端连接检查失败: %s" % e)
            res["reason"] = "cloud-unreachable"
            res["detail"] = str(e)[:200]
            return None, res
    res["reason"] = "token-invalid"
    return None, res


def push(store, files, cloud_chain, keep=0, pattern="", dry_run=False,
         log=lambda m: None, verify=True, protect=()):
    """把一批本地文件推送到云端目录（同名覆盖）。

    files : [(本地绝对路径, 云端文件名或 None), ...]
    比 sync() 少一个"扫描目录"的过程——宿主机脚本已经把包装好了，
    这里只负责传输、校验、保留策略。凭证与重试逻辑与 sync() 共用。
    """
    result = {"ok": False, "reason": "", "todo": 0, "ok_n": 0, "fail": 0,
              "exist": 0, "skip": 0, "bytes": 0, "elapsed": 0,
              "detail": "", "files": [], "pruned": 0}

    # 先做本地校验，避免把明显的问题拖到网络层
    items = []
    for it in files or []:
        p, name = (it if isinstance(it, (list, tuple)) else (it.get("path"), it.get("name")))
        if not p or not os.path.isfile(p):
            result["reason"] = "src-missing"
            result["detail"] = "文件不存在: %s" % p
            log("SKIP 文件不存在: %s" % p)
            return result
        sz = size_of(p)
        if sz <= 0:
            result["reason"] = "src-empty"
            result["detail"] = "文件为空: %s" % p
            log("SKIP 文件为空: %s" % p)
            return result
        items.append((p, name or os.path.basename(p), sz))

    if not items:
        result["reason"] = "no-files"
        log("没有待推送文件")
        return result

    result["todo"] = len(items)
    for p, n, sz in items:
        log("待推送 %s (%.2f MB)" % (n, sz / 1048576))

    if dry_run:
        result.update(ok=True, reason="dry-run")
        log("dry-run：跳过实际上传")
        return result

    w, res = _connect(store, log)
    if not w:
        result.update(reason=res["reason"], detail=res["detail"])
        return result

    t0 = time.time()
    try:
        root_id = ensure_chain(w, cloud_chain, log)
    except Exception as e:
        log("建根路径失败: %s" % e)
        result.update(reason="cloud-path", detail=str(e)[:200])
        return result
    log("云端根目录 id=%s" % root_id)

    ok_n, done_b, errs, uploaded = 0, 0, [], []
    for p, n, sz in items:
        t = time.time()
        try:
            w.upload(p, root_id, n)
            el = time.time() - t
            ok_n += 1
            done_b += sz
            uploaded.append((n, sz))
            log("✅ 上传完成 %s（%.2f MB，%.1fs，%.0f KB/s）" % (
                n, sz / 1048576, el, (sz / 1024 / el) if el else 0))
        except Exception as e:
            errs.append("%s: %s" % (n, e))
            log("❌ 上传失败 %s: %s" % (n, e))

    # 复核：云端体积必须一致；云端若返回 sha256，再比内容哈希（更强）
    if verify and uploaded:
        try:
            cloud = {}
            for f in w.ls(root_id):
                if f.get("type") == 1:
                    cloud[(f.get("name") or "")] = f
            bad = []
            for n, sz in uploaded:
                c = cloud.get(n)
                if not c:
                    bad.append("%s 云端未找到" % n)
                elif str(c.get("size")) != str(sz):
                    bad.append("%s 体积不符 本地=%s 云端=%s" % (n, sz, c.get("size")))
            if bad:
                errs.extend(bad)
                log("❌ 复核未通过：" + "；".join(bad))
            else:
                # 体积都对了，再做一次内容哈希比对（仅对有 sha256 且体积较小的文件）
                hashed = 0
                for p, n, sz in items:
                    c = cloud.get(n) or {}
                    remote_hash = (c.get("sha256") or "").lower()
                    if not remote_hash:
                        continue
                    if sha256_of(p) != remote_hash:
                        bad.append("%s 内容哈希不符（传输损坏）" % n)
                    else:
                        hashed += 1
                if bad:
                    errs.extend(bad)
                    log("❌ 内容校验未通过：" + "；".join(bad))
                else:
                    log("✅ 体积复核通过（%d 个文件）%s" % (
                        len(uploaded),
                        "，SHA256 一致 %d 个" % hashed if hashed else ""))
        except Exception as e:
            log("复核时列目录失败（不影响已完成的上传）: %s" % e)

    pruned = 0
    if keep and pattern and not errs:
        r = prune_cloud(w, root_id, pattern, keep, log, protect=protect)
        pruned = r.get("deleted", 0)

    el = time.time() - t0
    result.update(ok=(not errs), ok_n=ok_n, fail=len(errs), bytes=done_b,
                  elapsed=el, reason="" if not errs else "upload",
                  detail=(errs[0][:200] if errs else ""), pruned=pruned,
                  files=[n for n, _ in uploaded])
    log("=== 结束：成功 %d / 失败 %d / 共 %.2f MB / 耗时 %.0fs ===" % (
        ok_n, len(errs), done_b / 1048576, el))
    return result


def quota(store, log=lambda m: None):
    """查询云盘容量（QueryCloudUsageInfo）。失败抛异常，由调用方兜底。

    返回字段：total/used/free（字节）、pct、vip、expire、cat 分类占用。
    协议里 totalSize/usedSize 是 KB，byteTotalSize/byteUsedSize 是字节，
    优先用字节字段，缺失时用 KB 换算。
    """
    w, err = _connect(store, log)
    if not w:                      # 注意：_connect 成功时也会返回那个 dict，判据只能是 w
        raise RuntimeError((err or {}).get("detail") or (err or {}).get("reason") or "connect-failed")
    d = w.call("wohome", "QueryCloudUsageInfo",
               {"phoneNum": "", "clientId": CLIENT_ID}, retries=1)
    ui = (d or {}).get("usageInfo") or {}

    def _i(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0

    def kb2b(v):
        return _i(v) * 1024

    total = _i(ui.get("byteTotalSize")) or kb2b(ui.get("totalSize"))
    used = _i(ui.get("byteUsedSize")) or kb2b(ui.get("usedSize"))
    return {
        "ok": True,
        "total": total,
        "used": used,
        "free": max(total - used, 0),
        "pct": round(used * 100.0 / total, 1) if total else 0.0,
        "vip": str((d or {}).get("vipLevel") or ""),
        "expire": str((d or {}).get("expireTime") or ""),
        "cat": {
            "video": kb2b(ui.get("videoSize")),
            "image": kb2b(ui.get("imageSize")),
            "other": kb2b(ui.get("otherSize")),
            "audio": kb2b(ui.get("audioSize")),
            "text": kb2b(ui.get("textSize")),
        },
    }
