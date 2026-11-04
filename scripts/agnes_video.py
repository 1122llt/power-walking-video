#!/usr/bin/env python3
"""Agnes Video 2.5 Flash 生视频命令行工具（零依赖，只用 Python 自带库）。

接口是异步两段式：
  1) POST /v1/videos  建任务，拿到 video_id
  2) GET/agnesapi?video_id=&model_name=  轮询直到 completed，取顶层 url

用法示例：
  # 文生视频
  python agnes_video.py --prompt "银色跑车驶过雨夜霓虹街道" --seconds 5 --aspect 16:9 --out ./out/a.mp4

  # 首尾帧控制（本地图片会自动起临时图床转成公网URL）
  python agnes_video.py --mode keyframe --prompt "角色转身走向窗边，镜头缓慢推近" \\
    --first-frame ./a.png --last-frame ./b.png --out ./out/b.mp4

  # 图片参考（--image 可传多张，最多5张）
  python agnes_video.py --mode reference --prompt "参考<Image 1>的人物设定，在花田中奔跑" \\
    --image ./char.png --out ./out/c.mp4
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL = "agnes-video-2.5-flash"
DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
SIZE = "720P"  # Flash 固定 720P，其他值直接 400
ASPECTS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
ASPECT_PIXELS = {
    "21:9": "1680x720",
    "16:9": "1280x704",
    "4:3": "960x720",
    "1:1": "720x720",
    "3:4": "720x960",
    "9:16": "720x1280",
}
MODES = ("text", "keyframe", "reference")
KEY_FILE = Path.home() / ".agnes_key"
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 520, 522, 524}


def die(msg, code=1):
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(code)


def sanitize_key(raw):
    """清掉复制 Key 时常带进来的杂质：首尾空白/引号/尖括号/中文引号/换行。"""
    k = raw.strip().strip("<>\"'“”‘’")
    return "".join(k.split())


def resolve_key(explicit):
    key = explicit or os.environ.get("AGNES_API_KEY") or os.environ.get("AGONES_API_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    key = sanitize_key(key or "")
    if not key:
        die(
            "没找到 API Key。三种办法任选一种：\n"
            "  1) 直接带参数：--key ag-xxxxxxxx\n"
            "  2) 设环境变量：setx AGNES_API_KEY \"ag-xxxxxxxx\"（永久生效，需重开终端）\n"
            "  3) 存到文件：python agnes_video.py --save-key ag-xxxxxxxx"
        )
    return key


def resolve_base_url(explicit):
    """优先级：命令行 > AGNES_BASE_URL 环境变量 > ~/.agnes_base_url > 官方默认。"""
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("AGNES_BASE_URL")
    if env:
        return env.rstrip("/")
    saved = Path.home() / ".agnes_base_url"
    if saved.exists():
        val = saved.read_text(encoding="utf-8").strip()
        if val:
            return val.rstrip("/")
    return DEFAULT_BASE_URL


# ---------- 临时图床：把本地图片临时暴露成公网可访问的 URL ----------

class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def start_temp_host(directory):
    """在本机临时起一个静态文件服务，返回 (端口, server)。

    注意：Agnes 服务端必须能访问到这个地址。本机回环地址对远端不可见，
    因此脚本会先尝试从公网探测本机可达地址；探测不到时如实报错。
    """
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    srv = ThreadingHTTPServer(("0.0.0.0", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port, srv


def detect_public_host():
    """探测本机在公网上的可达主机名/IP，取不到返回 None。"""
    for probe in (
        "https://api.ipify.org?format=text",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ):
        try:
            req = urllib.request.Request(probe, headers={"User-Agent": "agnes-video-skill"})
            with urllib.request.urlopen(req, timeout=8) as r:
                host = r.read().decode("utf-8").strip()
            if re.match(r"^[\w.:\-]+$", host):
                return host
        except Exception:
            continue
    return None


def to_public_url(path_or_url, host_hint=None):
    """http(s) 开头直接返回；本地路径则起临时图床并返回可公网访问的 URL。"""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url

    p = Path(path_or_url)
    if not p.exists():
        die(f"找不到文件：{path_or_url}")

    host = host_hint or detect_public_host()
    if not host:
        die(
            "这个模式需要公网可访问的图片地址，但没能探测到本机的公网地址。\n"
            "两个办法：\n"
            f"  1) 手动把图片传到任何图床，然后把公网 URL 传给 --image\n"
            f"  2) 当前文件在：{p.resolve()}\n"
            "     可用任意静态服务暴露它，例如在该目录执行：\n"
            "     python -m http.server 8000\n"
            "再把 http://<你的公网IP>:8000/" + p.name + " 作为参数传入"
        )

    # 每次调用单起一个 server 会泄漏线程，这里做一次性复用
    if not hasattr(to_public_url, "_srv"):
        port, srv = start_temp_host(p.parent.resolve())
        to_public_url._srv = (srv, port, p.parent.resolve())
    _, port, _ = to_public_url._srv
    return f"http://{host}:{port}/{p.name}"


# ---------- HTTP ----------

def request_json(url, key, method="GET", payload=None, timeout=60, attempts=3,
                 backoff_cap=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last = None
    for i in range(max(1, attempts)):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:600]
            code = e.code
            if code == 401:
                die(f"401 鉴权失败，Key 无效。原文：{detail}", code=2)
            if code == 404:
                die(f"404 找不到资源。原文：{detail}", code=3)
            if code == 400:
                die(f"400 参数错误。原文：{detail}", code=4)
            if code not in RETRY_STATUS:
                die(f"{code} 请求失败。原文：{detail}", code=5)
            last = f"HTTP {code}: {detail}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if i < attempts - 1:
            w = min(backoff_cap, 2.0 * (2**i))
            print(f"  第 {i + 1} 次失败（{last[:70]}），{w:.0f}s 后重试…", file=sys.stderr)
            time.sleep(w)
    die(f"重试 {attempts} 次仍失败。最后错误：{last}", code=5)


def build_payload(args, host_hint):
    if args.seconds < 4 or args.seconds > 12:
        die(f"seconds 只能是 4–12，你填的是：{args.seconds}")
    if args.aspect not in ASPECTS:
        die(f"aspect 只能是 {', '.join(ASPECTS)}，你填的是：{args.aspect}")

    mode = args.mode
    if mode == "auto":
        if args.first_frame or args.last_frame:
            mode = "keyframe"
        elif args.image or args.audio:
            mode = "reference"
        else:
            mode = "text"
    if mode not in MODES:
        die(f"mode 只能是 {', '.join(MODES)}")

    # Flash 专属限制：参考图最多 5 张、参考音频最多 3 条、videos 不支持
    if len(args.image) > 5:
        die("参考图最多 5 张，Flash 模型硬限制（超了服务端返回 400）")
    if len(args.audio) > 3:
        die("参考音频最多 3 条，Flash 模型硬限制（超了服务端返回 400）")
    if args.video:
        die("Flash 模型不支持参考视频输入（videos 字段会返回 400）")

    # 显式声明 text 模式时，先做互斥校验再碰文件，避免报出与意图无关的文件错误
    if args.mode == "text" and (args.first_frame or args.last_frame
                                or args.image or args.audio):
        die("text 模式不接受任何参考媒体，请改用 --mode keyframe 或 --mode reference")

    p = {
        "model": MODEL,
        "prompt": args.prompt,
        "mode": mode,
        "size": SIZE,
        "seconds": str(args.seconds),
        "aspect_ratio": args.aspect,
    }
    if args.seed is not None:
        p["seed"] = args.seed

    if mode == "keyframe":
        if not (args.first_frame or args.last_frame):
            die("keyframe 模式至少要给 --first-frame 或 --last-frame 其中一个")
        if args.first_frame:
            p["first_frame"] = to_public_url(args.first_frame, host_hint)
        if args.last_frame:
            p["last_frame"] = to_public_url(args.last_frame, host_hint)
    elif mode == "reference":
        if not (args.image or args.audio):
            die("reference 模式至少要给 --image 或 --audio 其中一个")
        if args.image:
            p["images"] = [to_public_url(i, host_hint) for i in args.image]
        if args.audio:
            p["audios"] = list(args.audio)  # 音频必须自带公网 URL
    else:
        if args.first_frame or args.last_frame or args.image or args.audio:
            die("text 模式不接受任何参考媒体，请改用 --mode keyframe 或 --mode reference")
    return p, mode


def poll(video_id, key, base_url, max_wait, interval):
    """轮询任务直到 completed / failed。以顶层 status 和 url 为准。"""
    origin = base_url.split("/v1")[0].rstrip("/")
    query = urllib.parse.urlencode({"video_id": video_id, "model_name": MODEL})
    url = f"{origin}/agnesapi?{query}"
    deadline = time.time() + max_wait
    last_pct = -1
    while time.time() < deadline:
        r = request_json(url, key, timeout=60, attempts=3)
        st = (r.get("status") or "").lower()
        pct = r.get("progress")
        if isinstance(pct, int) and pct != last_pct:
            last_pct = pct
            print(f"  状态 {st} 进度 {pct}%", file=sys.stderr)
        if st == "completed":
            return r
        if st == "failed":
            err = r.get("error")
            die(f"任务失败：{json.dumps(err, ensure_ascii=False) if err else r}", code=6)
        time.sleep(interval)
    die(f"等待超过 {max_wait}s 仍未完成，可稍后用 --video-id {video_id} 继续查", code=7)


def download(url, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "agnes-video-skill"})
    with urllib.request.urlopen(req, timeout=300, context=ssl.create_default_context()) as r:
        out.write_bytes(r.read())
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="Agnes Video 2.5 Flash 生视频")
    ap.add_argument("--prompt", help="画面描述文本")
    ap.add_argument("--mode", default="auto",
                    choices=("auto",) + MODES, help="生成模式，默认自动判断")
    ap.add_argument("--seconds", type=int, default=5, help="时长 4–12 秒，默认 5")
    ap.add_argument("--aspect", default="16:9", help=f"画面比例 {ASPECTS}，默认 16:9")
    ap.add_argument("--seed", type=int, help="随机种子，填同一数字可复现同一结果")
    ap.add_argument("--first-frame", help="首帧图片（本地路径或公网URL）")
    ap.add_argument("--last-frame", help="尾帧图片（本地路径或公网URL）")
    ap.add_argument("--image", action="append", default=[], help="参考图，可重复，最多 5 张")
    ap.add_argument("--audio", action="append", default=[], help="参考音频公网URL，最多 3 条")
    ap.add_argument("--video", action="append", default=[], help="不支持，Flash 会报 400")
    ap.add_argument("--out", help="输出 mp4 路径，默认 ./agnes-out/video-xxx.mp4")
    ap.add_argument("--url-only", action="store_true", help="只打印视频链接，不下载")
    ap.add_argument("--video-id", help="已有 video_id 时只查询结果，不重新建任务")
    ap.add_argument("--key", help="API Key")
    ap.add_argument("--base-url", help=f"API 基址，默认 {DEFAULT_BASE_URL}")
    ap.add_argument("--save-key", help="把 Key 存到本地文件")
    ap.add_argument("--save-base-url", help="把基址存到本地文件")
    ap.add_argument("--max-wait", type=int, default=900, help="最长等待秒数，默认 900")
    ap.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数，默认 2")
    ap.add_argument("--attempts", type=int, default=8,
                    help="建任务失败重试次数，默认 8（队列满/限流时靠它自动扛过去）")
    args = ap.parse_args()

    if args.save_key:
        clean = sanitize_key(args.save_key)
        if not clean:
            die("--save-key 给的值是空的")
        KEY_FILE.write_text(clean, encoding="utf-8")
        try:
            KEY_FILE.chmod(0o600)
        except OSError:
            pass
        print(f"已保存到 {KEY_FILE}（长度 {len(clean)}）")
    if args.save_base_url:
        Path.home().joinpath(".agnes_base_url").write_text(args.save_base_url.strip(), encoding="utf-8")
        print(f"基址已保存：{args.save_base_url.strip()}")
    if args.save_key or args.save_base_url:
        return

    if not args.prompt and not args.video_id:
        die("必须提供 --prompt 画面描述")

    key = resolve_key(args.key)
    base_url = resolve_base_url(args.base_url)

    # ---- 只查询已有任务 ----
    if args.video_id:
        print(f"查询任务 {args.video_id} …", file=sys.stderr)
        r = poll(args.video_id, key, base_url, args.max_wait, args.interval)
        print(json.dumps({"status": "ok", "url": r.get("url"),
                          "seconds": r.get("seconds"), "size": r.get("size")},
                         ensure_ascii=False))
        return

    # ---- 建任务 ----
    host_hint = detect_public_host() if (args.image or args.first_frame or args.last_frame) else None
    payload, mode = build_payload(args, host_hint)

    print(f"[{mode} 模式] 模型={MODEL} {payload['seconds']}s "
          f"{payload['aspect_ratio']}({ASPECT_PIXELS[payload['aspect_ratio']]}) …", file=sys.stderr)

    created = request_json(f"{base_url}/videos", key, method="POST",
                           payload=payload, timeout=120,
                           attempts=max(args.attempts, 8), backoff_cap=90)
    video_id = created.get("video_id")
    if not video_id:
        die(f"建任务返回里没有 video_id：{json.dumps(created, ensure_ascii=False)[:400]}", code=8)
    print(f"任务已创建 video_id={video_id}，开始等待…", file=sys.stderr)

    result = poll(video_id, key, base_url, args.max_wait, args.interval)
    url = result.get("url")
    if not url:
        die(f"任务完成但没有 url 字段：{json.dumps(result, ensure_ascii=False)[:400]}", code=6)

    if args.url_only:
        print(url)
        return

    out = args.out or str(Path.cwd() / "agnes-out" / f"agnes-{uuid.uuid4().hex[:8]}.mp4")
    saved = download(url, out)
    print(json.dumps({
        "status": "ok",
        "video_id": video_id,
        "file": str(saved.resolve()),
        "bytes": saved.stat().st_size,
        "url": url,
        "seconds": result.get("seconds"),
        "size": result.get("size"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()