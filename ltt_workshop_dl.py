#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
LTT 工坊下载器 (v0.7.1)
================================================
给 Left 4 Dead 2（appid 550）下载工坊物品 / 合集。
本项目的代码与文档由 AI（DeepSeek Harness）协助生成，作者负责需求、真机测试与验收。
以 MIT 许可公开。

用途：L4D2（appid 550）工坊物品 / 合集 → 下载到本工具所在目录。
原理：Steam 公开 API `GetPublishedFileDetails` 返回 `file_url` 直链，直接 HTTP 下载
      （默认 8 条并行 + Range 分段；支持断点续传、分段重试；合集可勾选要下哪些）。
      不需要开游戏 / 不需要 Steam 登录 / 不走 steamcmd。

用法：
  双击 ltt_workshop_dl.bat
  python ltt_workshop_dl.py --cli "<链接或ID>" [--threads=8] [--proxy=http://127.0.0.1:7897] [--all] [--list]

输出：
  <工具目录>\<清洗后的标题>_<ID>.vpk / 同名 .jpg
  <工具目录>\run.log        运行日志（超过 2 MB 自动轮转）
  <工具目录>\settings.ini   代理开关/地址/线程数
  *.part / *.part.json      未完成的下载与进度（下次自动续传）
"""

import os
import sys
import re
import json
import ssl
import time
import threading
import urllib.parse
import urllib.request

APPID = 550
VERSION = "0.7.1"
API_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
API_COLLECTION = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
UA = f"LTT-WorkshopDL/{VERSION} (+L4D2)"

def tool_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

OUT_DIR = tool_dir()
SETTINGS = os.path.join(OUT_DIR, "settings.ini")
LOG_FILE = os.path.join(OUT_DIR, "run.log")

PROXY = None
THREADS = 8
RETRIES = 3            # 每个分段最多重试次数
LOG_SINK = None
PROGRESS_SINK = None
OPEN = None
CANCEL = threading.Event()

# ---------------------------------------------------------------- 日志（控制台 + 窗口 + 文件）
def _rotate_log():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
            old = LOG_FILE + ".1"
            if os.path.exists(old):
                os.remove(old)
            os.replace(LOG_FILE, old)
    except Exception:
        pass

def log(msg=""):
    print(msg, flush=True)
    if LOG_SINK:
        try:
            LOG_SINK(msg)
        except Exception:
            pass
    try:
        _rotate_log()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

# ---------------------------------------------------------------- 设置
def load_settings():
    global PROXY, THREADS
    try:
        txt = open(SETTINGS, encoding="utf-8").read()
    except Exception:
        return
    m = re.search(r"^proxy_enabled\s*=\s*([01])", txt, re.M)
    p = re.search(r"^proxy_url\s*=\s*(\S*)", txt, re.M)
    t = re.search(r"^threads\s*=\s*(\d+)", txt, re.M)
    if m and m.group(1) == "1" and p and p.group(1):
        PROXY = p.group(1)
    if t:
        try:
            THREADS = max(1, min(32, int(t.group(1))))
        except ValueError:
            pass

def save_settings():
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            f.write("[general]\n")
            f.write(f"proxy_enabled={'1' if PROXY else '0'}\n")
            f.write(f"proxy_url={PROXY or ''}\n")
            f.write(f"threads={THREADS}\n")
    except Exception:
        pass

# ---------------------------------------------------------------- 格式化
def fmt_bytes(n):
    if n is None:
        return "?"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024.0
    return f"{v:.1f} GB"

def fmt_eta(sec):
    if sec is None or sec < 0 or sec > 86400 * 3:
        return "--"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"

def build_bar(frac, written, total, speed, width=26):
    frac = max(0.0, min(1.0, frac if frac is not None else 0.0))
    filled = int(round(width * frac))
    bar = "#" * filled + "-" * (width - filled)
    if total:
        eta = fmt_eta((total - written) / speed) if speed > 1 else "--"
        head = f"{frac*100:3.0f}% [{bar}] {fmt_bytes(written)}/{fmt_bytes(total)}"
    else:
        eta = "--"
        head = f" ??% [{bar}] {fmt_bytes(written)}"
    spd = f"{speed/1048576:.2f} MB/s" if speed > 0 else "-- MB/s"
    return f"{head}  {spd}  剩 {eta}"

def progress(frac, written, total, speed):
    line = build_bar(frac, written, total, speed)
    try:
        cols = os.get_terminal_size().columns
    except Exception:
        cols = 100
    try:
        print("\r" + line[:max(40, cols - 1)].ljust(max(40, cols - 1)), end="", flush=True)
    except Exception:
        print("\r" + line, end="", flush=True)
    if PROGRESS_SINK:
        try:
            PROGRESS_SINK(line)
        except Exception:
            pass

def progress_end():
    try:
        print(flush=True)
    except Exception:
        pass

# ---------------------------------------------------------------- HTTP
def _opener():
    ph = {"http": PROXY, "https": PROXY} if PROXY else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(ph),
                                       urllib.request.HTTPSHandler(context=ssl.create_default_context()))

def http_post(url, fields, timeout=30):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with OPEN.open(req, timeout=timeout) as r:
        return r.status, r.read()

def probe_range(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    try:
        with OPEN.open(req, timeout=timeout) as r:
            if r.status == 206:
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    try:
                        return int(cr.rsplit("/", 1)[1]), True
                    except ValueError:
                        return None, True
                return None, True
            cl = r.headers.get("Content-Length")
            return (int(cl) if cl and cl.isdigit() else None), False
    except Exception:
        return None, False

def download_single(url, dest, timeout=1800, want_progress=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    written = 0
    last_rep = -1
    win_t, win_b = time.time(), 0
    speed = 0.0
    with OPEN.open(req, timeout=timeout) as r:
        cl = r.headers.get("Content-Length")
        total = int(cl) if cl and cl.isdigit() else None
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if want_progress:
                    now = time.time()
                    if now - win_t >= 0.25 or (total and written == total):
                        inst = (written - win_b) / max(1e-6, now - win_t)
                        speed = inst if speed <= 0 else speed * 0.6 + inst * 0.4
                        win_t, win_b = now, written
                        progress(written / total if total else None, written, total, speed)
                        last_rep = written
        if want_progress:
            if last_rep != written:
                progress(1.0 if (total and written == total) else None, written, total, speed)
            progress_end()
        return r.status, written

class Cancelled(Exception):
    pass

def _seg_worker(url, dest, start, end, already, state, lock, persist, idx, timeout):
    """下载一个分段，从 start+already 继续；失败重试 RETRIES 次。返回 (已下字节, 错误)。"""
    pos = start + already
    attempt = 0
    while pos <= end:
        if CANCEL.is_set():
            return pos - start, "cancelled"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Range": f"bytes={pos}-{end}"})
            with OPEN.open(req, timeout=timeout) as r:
                if r.status != 206:
                    raise RuntimeError(f"HTTP {r.status}")
                with open(dest, "r+b") as f:
                    f.seek(pos)
                    while pos <= end:
                        if CANCEL.is_set():
                            return pos - start, "cancelled"
                        b = r.read(256 * 1024)
                        if not b:
                            break
                        b = b[:end - pos + 1]
                        f.write(b)
                        pos += len(b)
                        with lock:
                            state["written"] += len(b)
                            state["segs"][idx][2] = pos - start
            if pos > end:
                break
            raise RuntimeError("连接提前结束")
        except Exception as e:
            attempt += 1
            with lock:
                state["last_err"] = f"分段{idx+1} 第{attempt}次: {type(e).__name__}: {e}"
            if attempt >= RETRIES:
                return pos - start, f"{type(e).__name__}: {e}"
            time.sleep(min(5, 1.5 * attempt))
            persist()
    return pos - start, None

def download_resumable(url, dest, want_progress=False):
    """并行 + 断点续传 + 分段重试。dest 是 .part 文件，配套 .part.json 记进度。"""
    size, ranges = probe_range(url)
    if not ranges or not size:
        return download_single(url, dest, want_progress=want_progress)

    meta_path = dest + ".json"
    n = min(THREADS, 16)
    seg = size // n
    segs = [(i * seg, (size - 1 if i == n - 1 else i * seg + seg - 1)) for i in range(n)]

    state = None
    if os.path.exists(meta_path) and os.path.exists(dest) and os.path.getsize(dest) == size:
        try:
            st = json.load(open(meta_path, encoding="utf-8"))
            if st.get("size") == size and st.get("url") == url and len(st.get("segs", [])) == n:
                state = st
        except Exception:
            state = None
    if state is None:
        with open(dest, "wb") as f:
            f.truncate(size)
        state = {"url": url, "size": size, "segs": [[s, e, 0] for s, e in segs]}
        json.dump(state, open(meta_path, "w", encoding="utf-8"))
    elif any(s[2] for s in state["segs"]):
        done = sum(s[2] for s in state["segs"])
        log(f"  [续传] 接着上次的进度：已完成 {fmt_bytes(done)} / {fmt_bytes(size)}")

    lock = threading.Lock()
    state["written"] = sum(s[2] for s in state["segs"])
    state["last_err"] = None
    last_save = [time.time()]

    def persist():
        now = time.time()
        if now - last_save[0] < 1.5:
            return
        last_save[0] = now
        try:
            with lock:
                snap = json.dumps(state)
            tmp = meta_path + ".tmp"
            open(tmp, "w", encoding="utf-8").write(snap)
            os.replace(tmp, meta_path)
        except Exception:
            pass

    todo = [(i, s, e, d) for i, (s, e, d) in enumerate(state["segs"]) if d < (e - s + 1)]
    ths = []
    results = []
    rlock = threading.Lock()

    def runner(i, s, e, d):
        got, err = _seg_worker(url, dest, s, e, d, state, lock, persist, i, 1800)
        with rlock:
            results.append((i, got, err))

    t0 = time.time()
    win_t, win_b = t0, state["written"]
    speed = 0.0
    for (i, s, e, d) in todo:
        t = threading.Thread(target=runner, args=(i, s, e, d), daemon=True)
        t.start()
        ths.append(t)

    while any(t.is_alive() for t in ths):
        time.sleep(0.25)
        with lock:
            written = state["written"]
        if want_progress:
            now = time.time()
            inst = (written - win_b) / max(1e-6, now - win_t)
            speed = inst if speed <= 0 else speed * 0.6 + inst * 0.4
            win_t, win_b = now, written
            progress(written / size if size else None, written, size, speed)
        persist()
    for t in ths:
        t.join(timeout=5)
    persist()

    with lock:
        written = state["written"]
    if want_progress:
        progress(1.0 if written == size else None, written, size, speed)
        progress_end()

    errs = [e for (_i, _g, e) in results if e]
    if CANCEL.is_set() or any(e == "cancelled" for e in errs):
        raise Cancelled("用户停止")
    if errs:
        raise RuntimeError(errs[0])
    return 206, written

def http_get_to_file(url, dest, want_progress=False):
    if THREADS > 1:
        return download_resumable(url, dest, want_progress=want_progress)
    return download_single(url, dest, want_progress=want_progress)

# ---------------------------------------------------------------- 解析 / 命名
def parse_item_id(text):
    text = (text or "").strip()
    if not text:
        return None, "输入为空"
    if re.fullmatch(r"\d{6,20}", text):
        return text, None
    q = urllib.parse.urlparse(text)
    qs = urllib.parse.parse_qs(q.query)
    if "id" in qs and qs["id"] and qs["id"][0].isdigit():
        return qs["id"][0], None
    m = re.search(r"[?&]id=(\d{6,20})", text)
    if m:
        return m.group(1), None
    m = re.search(r"\b(\d{9,20})\b", text)
    if m:
        return m.group(1), None
    return None, "没能在里面找到物品 ID（应形如 …/filedetails/?id=1234567890）"

def parse_many(text):
    """把多行/逗号/空格分隔的输入拆成 ID 列表（去重、保序）。"""
    parts = re.split(r"[\s,;，；]+", (text or "").strip())
    out, seen = [], set()
    for p in parts:
        if not p:
            continue
        i, _err = parse_item_id(p)
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out

ILLEGAL_CHARS = set('\\/:*?"<>|')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

def sanitize_title(title, item_id, max_len=70):
    t = (title or "").strip()
    t = "".join("_" if c in ILLEGAL_CHARS else c for c in t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(". ").strip()
    if not t:
        return str(item_id)
    if len(t) > max_len:
        t = t[:max_len].rstrip(". ")
    if t.upper() in RESERVED_NAMES:
        t = "_" + t
    return f"{t}_{item_id}"

class DownloadError(Exception):
    def __init__(self, msg, fid=None, detail=None, extra=None):
        super().__init__(msg)
        self.msg = msg
        self.fid = fid
        self.detail = detail or {}
        self.extra = extra or {}

def resolve_source(detail):
    fid = detail.get("publishedfileid", "?")
    try:
        expected = int(detail.get("file_size"))
    except (TypeError, ValueError):
        expected = None
    furl = (detail.get("file_url") or "").strip()
    if not furl:
        raise DownloadError(
            "Steam 没有给这个物品直链（file_url 为空）=> 本工具下不了。\n"
            "    【这种物品只能用 steamcmd 取，L4D2 支持匿名，不用登录】\n"
            f"      1) steamcmd +login anonymous +workshop_download_item 550 {fid} +quit\n"
            f"      2) 下载完在 steamapps/workshop/content/550/{fid}/ 下，文件名是 *.bin\n"
            "      3) 把那个 .bin 改名成 .vpk，丢进 left4dead2/addons/ 即可\n"
            "    （Windows 上 steamcmd 的引导程序可能因缺少 TLS 支持而自更新失败；\n"
            "      这种情况建议在 Linux 服务器上跑上面这条命令）",
            fid=fid, detail=detail)
    return furl, expected, "steam_api_file_url"

def is_l4d2_item(detail):
    return (detail.get("filename") or "").strip().lower().endswith(".vpk")

# ---------------------------------------------------------------- API
def fetch_detail(item_id):
    status, body = http_post(API_DETAILS, {"itemcount": "1", "publishedfileids[0]": item_id})
    if status != 200:
        raise DownloadError(f"查询 API 失败：HTTP {status}", fid=item_id, extra={"http": status})
    arr = json.loads(body.decode("utf-8", "replace")).get("response", {}).get("publishedfiledetails", [])
    if not arr:
        raise DownloadError("API 没有返回这个物品（数组为空）", fid=item_id)
    return arr[0]

def fetch_details_batch(ids, on_progress=None):
    out = {}
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        fields = {"itemcount": str(len(chunk))}
        for k, x in enumerate(chunk):
            fields[f"publishedfileids[{k}]"] = x
        status, body = http_post(API_DETAILS, fields)
        if status != 200:
            raise DownloadError(f"批量查询 API 失败：HTTP {status}", extra={"http": status})
        for f in json.loads(body.decode("utf-8", "replace")).get("response", {}).get("publishedfiledetails", []):
            out[f["publishedfileid"]] = f
        if on_progress:
            try:
                on_progress(min(i + 20, len(ids)), len(ids))
            except Exception:
                pass
    return out

def fetch_collection_children(item_id):
    status, body = http_post(API_COLLECTION, {"collectioncount": "1", "publishedfileids[0]": item_id})
    if status != 200:
        raise DownloadError(f"查询合集 API 失败：HTTP {status}", fid=item_id, extra={"http": status})
    arr = json.loads(body.decode("utf-8", "replace")).get("response", {}).get("collectiondetails", [])
    if not arr:
        return []
    out = []
    for c in (arr[0].get("children") or []):
        pid = c.get("publishedfileid")
        if pid:
            try:
                ft = int(c.get("filetype") or 0)
            except (TypeError, ValueError):
                ft = 0
            out.append((str(pid), ft))
    return out

# ---------------------------------------------------------------- 准备（不下载）
def prepare(item_text, on_progress=None):
    """返回 plan：{'kind':'single'|'collection', ...}；不下载。"""
    log("=" * 64)
    item_id, err = parse_item_id(item_text)
    if err:
        raise DownloadError(f"输入无法解析：{err}")
    log(f"物品 ID：{item_id}")

    detail = fetch_detail(item_id)
    result = detail.get("result")
    if result != 1:
        hints = {9: "物品不存在（可能已删除）", 0: "API 未返回结果"}
        raise DownloadError(
            f"Steam 拒绝了这次查询：API result={result}（{hints.get(result, '不可访问/已删除/私密')}）\n"
            f"    ID={item_id}  => 这种情况本工具无能为力", fid=item_id, detail=detail)

    if is_l4d2_item(detail):
        return {"kind": "single", "id": item_id, "detail": detail}

    log(f"它不是单个 .vpk 物品（filename={detail.get('filename')}），按【合集】解析…")
    log(f"标题：{detail.get('title') or '(无标题)'}")
    children = fetch_collection_children(item_id)
    if not children:
        raise DownloadError(
            "这既不是可下载的 L4D2 物品，也解析不出合集的子物品。\n"
            "    常见原因：该物品只挂了预览图/已被作者清空。", fid=item_id, detail=detail)

    ids = [cid for cid, ft in children if ft == 0]
    nested = len(children) - len(ids)
    if not ids:
        raise DownloadError("合集里没有可下载的物品（可能全是嵌套合集）。", fid=item_id, detail=detail)

    details = fetch_details_batch(ids, on_progress)
    items = []
    total = 0
    for cid in ids:
        d = details.get(cid) or {}
        try:
            sz = int(d.get("file_size") or 0)
        except (TypeError, ValueError):
            sz = 0
        total += sz
        items.append({"id": cid, "detail": d, "title": d.get("title") or "(抓不到标题)",
                      "size": sz, "ok": (d.get("result") == 1 and is_l4d2_item(d))})
    log(f"合集共 {len(children)} 项：可下载 {len(ids)} 个" + (f"，跳过嵌套合集 {nested} 个" if nested else ""))
    log(f"合计大小：{fmt_bytes(total)}")
    return {"kind": "collection", "id": item_id, "title": detail.get("title") or "",
            "items": items, "total": total}

# ---------------------------------------------------------------- 执行（下载）
def download_one(item_id, detail):
    title = detail.get("title") or ""
    base = sanitize_title(title, item_id)
    out_vpk = os.path.join(OUT_DIR, base + ".vpk")
    log(f"  标题：{title or '(无标题)'}")
    log(f"  API result={detail.get('result')}  filename={detail.get('filename')}  file_size={detail.get('file_size')}")

    url, expected, source = resolve_source(detail)

    if expected is not None and os.path.exists(out_vpk) and os.path.getsize(out_vpk) == expected:
        log("  [OK] 已存在且大小正确，跳过下载")
        log(f"  标题 -> 文件名：{os.path.basename(out_vpk)}")
        return out_vpk, expected

    legacy = os.path.join(OUT_DIR, f"{item_id}.vpk")
    if expected is not None and os.path.exists(legacy) and os.path.getsize(legacy) == expected:
        os.replace(legacy, out_vpk)
        old_jpg = os.path.join(OUT_DIR, f"{item_id}.jpg")
        if os.path.exists(old_jpg):
            try:
                os.replace(old_jpg, os.path.join(OUT_DIR, base + ".jpg"))
            except Exception:
                pass
        log(f"  [OK] 复用旧文件并改名：{os.path.basename(legacy)} -> {os.path.basename(out_vpk)}")
        log(f"  标题 -> 文件名：{os.path.basename(out_vpk)}")
        return out_vpk, expected

    part = out_vpk + ".part"
    log(f"  下载源：{source}（{'代理 ' + PROXY if PROXY else '直连'}，{THREADS} 并行，重试 {RETRIES} 次）")
    log(f"  目标：{out_vpk}")

    try:
        status, written = http_get_to_file(url, part, want_progress=True)
    except Cancelled:
        log(f"  [停止] 已保存进度，下次自动续传（{os.path.basename(part)}）")
        raise
    except Exception as e:
        raise DownloadError(f"下载过程出错：{type(e).__name__}: {e}", fid=item_id, detail=detail)

    if status not in (200, 206):
        raise DownloadError(f"直链返回 HTTP {status}=> 拿到的不是文件", fid=item_id,
                            detail=detail, extra={"http": status})

    if expected is not None and written != expected:
        for p in (part, part + ".json"):
            if os.path.exists(p):
                os.remove(p)
        raise DownloadError(
            "下载后大小与 API 不一致，已删除半成品（不留坏文件）。\n"
            f"    API file_size={expected}  实际={written}  HTTP={status}\n"
            "    可能：作者刚更新过（API 缓存滞后）、传输被截断、CDN 回了错误页、磁盘满。",
            fid=item_id, detail=detail,
            extra={"http": status, "expected": expected, "got": written})

    os.replace(part, out_vpk)
    for p in (part + ".json",):
        if os.path.exists(p):
            os.remove(p)
    log(f"  [OK] 已保存：{out_vpk}（{fmt_bytes(written)}）")

    prev = (detail.get("preview_url") or "").strip()
    if prev and not CANCEL.is_set():
        jpg = os.path.join(OUT_DIR, base + ".jpg")
        try:
            st, n = download_single(prev, jpg + ".part", timeout=120)
            if st == 200 and n > 0:
                os.replace(jpg + ".part", jpg)
            elif os.path.exists(jpg + ".part"):
                os.remove(jpg + ".part")
        except Exception:
            if os.path.exists(jpg + ".part"):
                os.remove(jpg + ".part")

    log(f"  标题 -> 文件名：{os.path.basename(out_vpk)}")
    return out_vpk, written

def execute(plan, selected=None):
    if plan["kind"] == "single":
        path, size = download_one(plan["id"], plan["detail"])
        log("=" * 64)
        return True, f"完成：{path}（{fmt_bytes(size)}）"

    items = plan["items"]
    if selected is not None:
        items = [x for x in items if x["id"] in selected]
    log(f"开始下载 {len(items)} 个物品（合计 {fmt_bytes(sum(x['size'] for x in items))}）")
    log("")

    ok = 0
    fails = []
    total_bytes = 0
    for i, it in enumerate(items, 1):
        if CANCEL.is_set():
            fails.append((it["id"], "已停止"))
            log("  [停止] 用户中断，后续物品未下载")
            break
        log(f"--- [{i}/{len(items)}] {it['id']}  {it['title']} ---")
        try:
            if not it["ok"]:
                raise DownloadError(f"API result={it['detail'].get('result')} 或不是 .vpk 物品", fid=it["id"])
            _p, n = download_one(it["id"], it["detail"])
            total_bytes += n
            ok += 1
        except Cancelled:
            fails.append((it["id"], "已停止"))
            log("  [停止] 用户中断")
            break
        except DownloadError as e:
            fails.append((it["id"], e.msg))
            log(f"  [失败] {it['id']}：{e.msg.splitlines()[0]}")
        except Exception as e:
            fails.append((it["id"], f"{type(e).__name__}: {e}"))
            log(f"  [失败] {it['id']}：{type(e).__name__}: {e}")

    log("")
    log("=" * 64)
    log(f"结果：成功 {ok} / 失败 {len(fails)} / 共 {len(items)}（合计 {fmt_bytes(total_bytes)}）")
    for cid, m in fails:
        log(f"  失败 {cid}：{m.splitlines()[0]}")
    if CANCEL.is_set():
        return False, f"已停止：完成 {ok}/{len(items)}"
    if fails:
        return False, f"部分成功：{ok}/{len(items)}（{len(fails)} 个失败，详见上方）"
    return True, f"完成：{ok}/{len(items)}，合计 {fmt_bytes(total_bytes)}"

# ---------------------------------------------------------------- CLI
def cli(argv):
    global OPEN, PROXY, THREADS
    args = []
    only_list = False
    take_all = False
    for a in argv:
        if a.startswith("--proxy="):
            PROXY = a.split("=", 1)[1] or None
        elif a.startswith("--threads="):
            try:
                THREADS = max(1, min(32, int(a.split("=", 1)[1])))
            except ValueError:
                pass
        elif a == "--no-proxy":
            PROXY = None
        elif a == "--list":
            only_list = True
        elif a == "--all":
            take_all = True
        else:
            args.append(a)
    OPEN = _opener()
    if not args:
        log(f'用法：python ltt_workshop_dl.py --cli "<链接或ID>" [--threads=8] [--proxy=URL] [--all] [--list]  (v{VERSION})')
        return 2
    tokens = parse_many(" ".join(args))
    if not tokens:
        log("没解析出任何物品 ID")
        return 2
    log(f"共 {len(tokens)} 个物品：{', '.join(tokens)}")
    any_fail = False
    for k, tok in enumerate(tokens, 1):
        log("")
        log(f"########## [{k}/{len(tokens)}] {tok} ##########")
        try:
            plan = prepare(tok)
        except DownloadError as e:
            log(f"[失败] {e.msg}")
            any_fail = True
            continue
        if plan["kind"] == "collection":
            log("")
            for i, it in enumerate(plan["items"], 1):
                log(f'  [{i:>2}] {"OK " if it["ok"] else "SKIP"} {fmt_bytes(it["size"]):>10}  {it["title"]}')
            log("")
            if only_list:
                continue
            if not take_all:
                log("（CLI 默认下载合集全部物品；只想列清单请用 --list）")
        ok, msg = execute(plan)
        log("")
        log(("成功：" if ok else "失败：") + msg)
        if not ok:
            any_fail = True
    return 1 if any_fail else 0

# ---------------------------------------------------------------- GUI
def gui():
    global OPEN, LOG_SINK, PROGRESS_SINK, PROXY, THREADS
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk

    load_settings()
    root = tk.Tk()
    root.title(f"LTT 工坊下载器 · L4D2 · v{VERSION}")
    root.geometry("820x620")
    root.minsize(700, 520)

    tk.Label(root, text="粘贴工坊链接或物品 ID（可一次粘多条：一行一个 / 逗号分隔），回车开始：", anchor="w").pack(fill="x", padx=10, pady=(10, 2))

    var = tk.StringVar()
    try:
        clip = root.clipboard_get().strip()
        if clip and ("steamcommunity" in clip or re.fullmatch(r"\d{6,20}", clip)):
            var.set(clip)
    except Exception:
        pass

    entry = tk.Entry(root, textvariable=var, font=("Consolas", 11))
    entry.pack(fill="x", padx=10)
    entry.focus_set()

    opt = tk.Frame(root)
    opt.pack(fill="x", padx=10, pady=(6, 0))

    use_proxy = tk.BooleanVar(value=bool(PROXY))
    proxy_url = tk.StringVar(value=PROXY or "")
    thr = tk.StringVar(value=str(THREADS))

    tk.Checkbutton(opt, text="走代理", variable=use_proxy).pack(side="left")
    tk.Entry(opt, textvariable=proxy_url, width=24, font=("Consolas", 9)).pack(side="left", padx=(2, 4))
    tk.Label(opt, text="(如 http://127.0.0.1:7897)", fg="#999").pack(side="left", padx=(0, 10))
    tk.Label(opt, text="并行").pack(side="left")
    tk.Spinbox(opt, from_=1, to=16, width=4, textvariable=thr).pack(side="left", padx=(2, 10))
    tk.Label(opt, text=f"(重试 {RETRIES} 次，支持断点续传)", fg="#999").pack(side="left")

    btns = tk.Frame(root)
    btns.pack(fill="x", padx=10, pady=8)

    txt = scrolledtext.ScrolledText(root, height=17, font=("Consolas", 9), wrap="word")
    txt.pack(fill="both", expand=True, padx=10)

    prog = tk.StringVar(value="")
    tk.Label(root, textvariable=prog, anchor="w", font=("Consolas", 9), fg="#0a6").pack(fill="x", padx=10, pady=(6, 0))
    tk.Label(root, text=f"输出目录：{OUT_DIR}   |   日志：run.log", anchor="w", fg="#666").pack(fill="x", padx=10, pady=(0, 8))

    def append(line):
        txt.insert("end", line + "\n")
        txt.see("end")

    LOG_SINK = lambda line: root.after(0, append, line)
    PROGRESS_SINK = lambda line: root.after(0, lambda: prog.set(line))

    busy = {"v": False}

    def set_busy(v):
        busy["v"] = v
        btn.config(state="disabled" if v else "normal")
        stop_btn.config(state="normal" if v else "disabled")

    def worker(plan, selected):
        try:
            ok, msg = execute(plan, selected)
        except Cancelled:
            ok, msg = False, "已停止（进度已保存，下次续传）"
        except Exception as e:
            ok, msg = False, f"未预期错误：{type(e).__name__}: {e}"
        root.after(0, lambda: (set_busy(False), prog.set(""), append(""),
                               append(("[OK] " if ok else "[X] ") + msg)))

    def multi_worker(jobs, errs):
        results = []
        for plan, sel in jobs:
            if CANCEL.is_set():
                results.append((plan["id"], False, "已停止"))
                break
            try:
                ok, msg = execute(plan, sel)
            except Cancelled:
                ok, msg = False, "已停止"
            except Exception as e:
                ok, msg = False, f"未预期错误：{type(e).__name__}: {e}"
            results.append((plan["id"], ok, msg))
            if not ok and CANCEL.is_set():
                break
        total_ok = sum(1 for _i, ok, _m in results if ok)
        failed = [r for r in results if not r[1]]
        def done():
            set_busy(False)
            prog.set("")
            append("")
            append("=" * 20 + " 汇总 " + "=" * 20)
            append(f"成功 {total_ok} / {len(jobs)} 个任务" + (f"；失败 {len(failed)}" if failed else ""))
            for i, ok, m in results:
                append(("  [OK] " if ok else "  [X] ") + f"{i}  {m.splitlines()[0]}")
            if errs:
                append(f"（另有 {len(errs)} 个输入在查询阶段就失败了，见上方）")
        root.after(0, done)

    def start(_evt=None):
        global OPEN, PROXY, THREADS
        if busy["v"]:
            return
        text = var.get().strip()
        if not text:
            messagebox.showinfo("提示", "先粘贴工坊链接或物品 ID")
            return
        if use_proxy.get() and not proxy_url.get().strip():
            messagebox.showinfo("提示", "勾了「走代理」但没填地址。\n请填代理地址（例如 http://127.0.0.1:7897），或取消勾选直接下载。")
            return
        PROXY = proxy_url.get().strip() if use_proxy.get() else None
        try:
            THREADS = max(1, min(32, int(thr.get())))
        except ValueError:
            THREADS = 8
        save_settings()
        OPEN = _opener()
        CANCEL.clear()
        txt.delete("1.0", "end")
        prog.set("")
        set_busy(True)

        tokens = parse_many(text)
        if not tokens:
            messagebox.showinfo("提示", "没解析出任何物品 ID")
            set_busy(False)
            return
        append(f"共 {len(tokens)} 个物品：" + ", ".join(tokens))

        def prep_worker():
            plans, errs = [], []
            for k, tok in enumerate(tokens):
                idx = k + 1
                root.after(0, lambda idx=idx: prog.set(f"正在查询 {idx}/{len(tokens)} …"))
                try:
                    plan = prepare(tok, lambda d, t: root.after(0, lambda: prog.set(f"查询中 {d}/{t} …")))
                    plans.append(plan)
                except DownloadError as e:
                    errs.append((tok, e.msg))
                    root.after(0, lambda tok=tok, m=e.msg: append(f"[失败] {tok}：{m}"))
                except Exception as e:
                    errs.append((tok, f"{type(e).__name__}: {e}"))
                    root.after(0, lambda tok=tok, e=e: append(f"[失败] {tok}：{type(e).__name__}: {e}"))
            root.after(0, lambda: on_prepared(plans, errs))

        def on_prepared(plans, errs):
            prog.set("")
            jobs = []
            for plan in plans:
                sel = None
                if plan["kind"] == "collection":
                    sel = ask_pick(root, plan)
                    if not sel:
                        append(f"跳过合集 {plan['id']}（没有勾选任何物品）")
                        continue
                jobs.append((plan, sel))
            if not jobs:
                append("没有可下载的物品")
                set_busy(False)
                return
            threading.Thread(target=multi_worker, args=(jobs, errs), daemon=True).start()

        threading.Thread(target=prep_worker, daemon=True).start()

    def do_stop():
        if busy["v"]:
            CANCEL.set()
            append("正在停止…（当前分段会在几秒内收尾，进度已保存）")

    btn = tk.Button(btns, text="下载", width=12, command=start)
    btn.pack(side="left")
    stop_btn = tk.Button(btns, text="停止", width=10, state="disabled", command=do_stop)
    stop_btn.pack(side="left", padx=6)
    tk.Button(btns, text="清空日志", width=10, command=lambda: txt.delete("1.0", "end")).pack(side="left", padx=6)
    tk.Button(btns, text="退出", width=8, command=root.destroy).pack(side="right")
    tk.Label(btns, text=f"v{VERSION}（控制台有同一份日志）", fg="#888").pack(side="left", padx=8)

    entry.bind("<Return>", start)
    append(f"输出目录：{OUT_DIR}")
    append("支持：单个物品 与 合集（可勾选）；不需要开游戏 / 不需要登录 / 不走 steamcmd。")
    append(f"当前：{'代理 ' + PROXY if PROXY else '直连'}，{THREADS} 并行，分段重试 {RETRIES} 次，支持断点续传")
    append("")

    root.mainloop()
    return 0

def ask_pick(root, plan):
    """合集勾选窗口：Treeview + 搜索 + 已选统计。返回选中 ID 列表；取消返回 None。"""
    import tkinter as tk
    from tkinter import ttk

    win = tk.Toplevel(root)
    win.title("选择要下载的物品")
    win.geometry("860x560")
    win.transient(root)
    win.grab_set()

    tk.Label(win, text=f"合集：{plan['title']}", anchor="w", font=("", 10, "bold")).pack(fill="x", padx=10, pady=(10, 0))

    top = tk.Frame(win)
    top.pack(fill="x", padx=10, pady=(6, 4))
    tk.Label(top, text="搜索：").pack(side="left")
    q = tk.StringVar()
    entq = tk.Entry(top, textvariable=q, width=32)
    entq.pack(side="left")
    entq.focus_set()
    stats = tk.StringVar(value="")
    tk.Label(top, textvariable=stats, fg="#0a6").pack(side="right")

    body = tk.Frame(win)
    body.pack(fill="both", expand=True, padx=10)

    cols = ("sel", "size", "title")
    tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="none")
    tree.heading("sel", text="选")
    tree.column("sel", width=42, anchor="center", stretch=False)
    tree.heading("size", text="大小")
    tree.column("size", width=92, anchor="e", stretch=False)
    tree.heading("title", text="标题")
    tree.column("title", width=660, anchor="w")
    sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")

    by_id = {i["id"]: i for i in plan["items"]}
    chosen = set(i["id"] for i in plan["items"] if i["ok"])   # 默认全选可下载项

    def visible():
        k = q.get().strip().lower()
        return [i for i in plan["items"] if (not k) or (k in i["title"].lower())]

    def upd_stats():
        sel = [by_id[i] for i in chosen if i in by_id]
        stats.set(f"已选 {len(sel)} / {len(plan['items'])} 项，合计 {fmt_bytes(sum(x['size'] for x in sel))}")

    def render():
        tree.delete(*tree.get_children())
        for it in visible():
            mark = "☑" if it["id"] in chosen else "☐"
            if not it["ok"]:
                mark = "✗"
            tree.insert("", "end", iid=it["id"], values=(mark, fmt_bytes(it["size"]), it["title"]))
        upd_stats()

    def toggle(iid):
        it = by_id.get(iid)
        if not it:
            return
        if not it["ok"]:
            return
        if iid in chosen:
            chosen.discard(iid)
        else:
            chosen.add(iid)
        tree.item(iid, values=(("☑" if iid in chosen else "☐"), fmt_bytes(it["size"]), it["title"]))
        upd_stats()

    def on_click(evt):
        iid = tree.identify_row(evt.y)
        if iid:
            toggle(iid)

    tree.bind("<Button-1>", on_click)
    tree.bind("<space>", lambda e: (tree.focus(), toggle(tree.focus())))
    q.trace_add("write", lambda *a: render())

    result = {"v": None}

    def sel_all(flag):
        for it in visible():
            if not it["ok"]:
                continue
            if flag:
                chosen.add(it["id"])
            else:
                chosen.discard(it["id"])
        render()

    def ok_btn():
        result["v"] = sorted(chosen)
        win.destroy()

    def cancel_btn():
        result["v"] = None
        win.destroy()

    bar = tk.Frame(win)
    bar.pack(fill="x", padx=10, pady=10)
    tk.Button(bar, text="全选(当前筛选)", width=16, command=lambda: sel_all(True)).pack(side="left")
    tk.Button(bar, text="全不选(当前筛选)", width=16, command=lambda: sel_all(False)).pack(side="left", padx=6)
    tk.Button(bar, text="取消", width=8, command=cancel_btn).pack(side="right")
    tk.Button(bar, text="开始下载", width=12, command=ok_btn).pack(side="right", padx=6)
    tk.Label(bar, text="点行即可勾选/取消；✗ 表示该物品没有直链，下不了", fg="#888").pack(side="left", padx=10)

    render()
    root.wait_window(win)
    return result["v"]

# ---------------------------------------------------------------- 入口
def main(argv):
    global OPEN
    if "--cli" in argv:
        return cli([a for a in argv if a != "--cli"])
    try:
        return gui()
    except Exception as e:
        load_settings()
        OPEN = _opener()
        log(f"[提示] 小窗启动失败（{type(e).__name__}: {e}），改用控制台模式。")
        if len(argv) > 1:
            try:
                plan = prepare(" ".join(argv[1:]))
                ok, msg = execute(plan)
            except DownloadError as ex:
                log(f"[失败] {ex.msg}")
                return 1
            log(("成功：" if ok else "失败：") + msg)
            return 0 if ok else 1
        return 1

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main(sys.argv))
