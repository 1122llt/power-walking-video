"""
后台守护：每 8 分钟探一次，通了就开始批量生成
=============================================
用法（后台跑）：
  python 后台守护生成.py            # 守护 28 条，最多守 12 小时
  python 后台守护生成.py --only 1-1 6-4   # 只守指定几条
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "素材AI")
LOG = os.path.join(BASE, "生成日志.txt")
AGNES = os.path.expanduser("~/.workbuddy/skills/agnes-video/scripts/agnes_video.py")
PY = sys.executable


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fmt(sec):
    return max(4, min(12, int(round(sec))))


def url_alive(url):
    """图床链接状态：True=可用, False=明确失效(404/410), None=不确定(网络/限流)
    注意：图床对频繁 HEAD/GET 请求会限流，模糊错误一律当作"不确定"，不做重传，
    否则会陷入「误判过期 → 重传 → 更限流」的恶性循环。
    """
    if not url or not url.startswith("http"):
        return False
    try:
        r = subprocess.run(["curl", "-s", "-m", "20", "-o", "/dev/null",
                            "-w", "%{http_code}", url],
                           capture_output=True, text=True, timeout=40)
        code = (r.stdout or "").strip()
        if code == "200":
            return True
        if code in ("404", "410"):
            return False
        return None
    except Exception:
        return None


def reupload(local_path):
    """首帧图重新上传到图床，返回新 URL"""
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "120", "-F", "reqtype=fileupload",
             "-F", "time=72h", "-F", f"fileToUpload=@{local_path}",
             "https://litterbox.catbox.moe/resources/internals/api.php"],
            capture_output=True, text=True, timeout=150)
        url = (r.stdout or "").strip()
        return url if url.startswith("http") else None
    except Exception:
        return None


def try_one(idx, prompt, seconds, aspect, first_frame=None):
    out = os.path.join(OUT_DIR, f"{idx}.mp4")
    cmd = [PY, AGNES, "--prompt", prompt, "--seconds", str(fmt(seconds)),
           "--aspect", aspect, "--out", out, "--max-wait", "1500"]
    if first_frame:
        # 首帧模式：给一张卡通首帧，让 Agnes 照着动
        cmd = [PY, AGNES, "--mode", "keyframe", "--first-frame", first_frame,
               "--prompt", prompt, "--seconds", str(fmt(seconds)),
               "--aspect", aspect, "--out", out, "--max-wait", "1500"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1650)
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 1024:
            return True, f"{os.path.getsize(out)//1024}KB"
        lines = (r.stderr or r.stdout or "").strip().splitlines()
        msg = lines[-1] if lines else "unknown"
        # 网络层故障（代理隧道断开等）单独标记：这种要整轮停下来等，不能硬打
        if any(k in msg for k in ("URLError", "Tunnel connection", "Connection refused",
                                  "Connection reset", "timed out", "Temporary failure")):
            return False, "NET:" + msg
        return False, msg
    except subprocess.TimeoutExpired:
        return False, "timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="只守指定镜号")
    ap.add_argument("--probe", type=int, default=8, help="探测间隔分钟，默认8")
    ap.add_argument("--hours", type=float, default=12, help="最多守护几小时")
    ap.add_argument("--cfg", default="生成分镜视频.json", help="分镜清单文件名，默认生成分镜视频.json")
    ap.add_argument("--force", action="store_true", help="不跳过已存在文件（补生成长版时用）")
    ap.add_argument("--out", default="素材AI", help="输出子目录名，默认 素材AI")
    ap.add_argument("--workers", type=int, default=3, help="并发条数，默认 3")
    args = ap.parse_args()

    global OUT_DIR
    OUT_DIR = os.path.join(BASE, args.out)

    with open(os.path.join(BASE, args.cfg), encoding="utf-8") as f:
        cfg = json.load(f)

    shots = {s["idx"]: s for s in cfg["shots"]}
    aspect = cfg["meta"]["aspect"]
    targets = args.only if args.only else list(shots.keys())
    targets = [t for t in targets if t in shots]
    # 优先级：开场与结尾的镜头先做（观众最先/最后看到的），其余按镜头时长从短到长
    # （实测 4-8 秒的镜头更容易排上队，10/12 秒反复 503）
    PRIO = ["1-1", "1-3a", "1-3b", "1-3c", "2-1", "9-3", "9-2a", "9-2b"]
    targets.sort(key=lambda t: (0, PRIO.index(t)) if t in PRIO
                 else (1, shots[t].get("seconds", 6), t))

    os.makedirs(OUT_DIR, exist_ok=True)

    log("=" * 56)
    log(f"后台守护启动 | 目标 {len(targets)} 条 | 探测间隔 {args.probe} 分钟")
    log(f"最多守护 {args.hours} 小时 | 日志：{LOG}")
    log("=" * 56)

    deadline = time.time() + args.hours * 3600
    done, failed = [], []

    # 完成记录：output/ 每次 runner 重新 checkout 会被清空，
    # 靠这个文件记住哪些镜头已经出过，避免重复排队
    done_file = os.path.join(BASE, "done.json")
    done_set = set()
    if os.path.isfile(done_file):
        try:
            with open(done_file, encoding="utf-8") as f:
                done_set = set(json.load(f))
        except Exception:
            done_set = set()

    while time.time() < deadline:
        pending = [t for t in targets if t not in done and t not in done_set]

        # 已存在文件也算完成（--force 时不算，因为要覆盖重生成）
        if not args.force:
            for t in list(pending):
                p = os.path.join(OUT_DIR, f"{t}.mp4")
                if os.path.isfile(p) and os.path.getsize(p) > 1024:
                    done.append(t)
                    done_set.add(t)
                    pending.remove(t)
                    log(f"{t} 已存在，计入完成")

        if not pending:
            log(f"🎉 全部完成！{len(done)} 条")
            break

        log(f"--- 探测轮次：剩余 {len(pending)} 条 ---")

        # 并发跑：成功的条目要 9-12 分钟，串行太慢；3 路并发轮询快 3 倍
        round_fail = []
        lock = threading.Lock()

        def run_one(idx):
            log(f"  试 {idx} …")
            sh = shots[idx]
            ff = sh.get("first_frame")
            # 链接检查（并发下加锁，避免同时重传）
            if ff and ff.startswith("http"):
                now = time.time()
                if now - sh.get("_checked_at", 0) > 3600:
                    with lock:
                        st = url_alive(ff)
                        sh["_checked_at"] = now
                        if st is False:
                            local = sh.get("first_frame_local")
                            new = reupload(local) if local else None
                            if new:
                                sh["first_frame"] = new
                                sh["_checked_at"] = 0
                                ff = new
                                with open(os.path.join(BASE, args.cfg), "w", encoding="utf-8") as f:
                                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                                log(f"  ↻ {idx} 首帧链接确已失效，重新上传 -> {new}")
                            else:
                                log(f"  ! {idx} 首帧重上传失败")
            ok, info = try_one(idx, sh["prompt"], sh["seconds"], aspect, ff)
            if ok:
                log(f"  ✅ {idx} 成功 {info}")
            elif isinstance(info, str) and info.startswith("NET:"):
                log(f"  ⚠ {idx} 网络异常，暂停本轮")
            else:
                log(f"  ❌ {idx} {info[:70]}")
            return ok, info

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_one, t): t for t in pending}
            net_err = 0
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    ok, info = fut.result()
                except Exception as e:
                    ok, info = False, str(e)
                if ok:
                    done.append(idx)
                    done_set.add(idx)
                else:
                    if isinstance(info, str) and info.startswith("NET:"):
                        net_err += 1
                    round_fail.append(idx)

        failed = round_fail

        # 存完成记录，供下一轮（output/ 会被清空）参考
        try:
            with open(done_file, "w", encoding="utf-8") as f:
                json.dump(sorted(done_set), f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f"  ! 写完成记录失败 {str(e)[:60]}")

        # 网络整体不通：立刻停手，等下一轮，别持续轰接口
        if net_err >= max(1, args.workers):
            log(f"⚠ 网络/代理异常（{net_err} 条同时报网络错），本轮中止，等 {args.probe} 分钟后重试")
            if time.time() > deadline:
                break
            time.sleep(args.probe * 60)
            continue

        if not failed:
            log(f"🎉 本轮全通！成功 {len(done)}/{len(targets)}")
            break

        if time.time() > deadline:
            break

        wait = args.probe * 60
        log(f"队列仍满，等待 {args.probe} 分钟后继续…")
        time.sleep(wait)

    log("=" * 56)
    log(f"守护结束。成功 {len(done)}/{len(targets)}")
    if failed:
        log(f"未完成：{', '.join(failed)}")
    log(f"文件位置：{OUT_DIR}")
    log("=" * 56)


if __name__ == "__main__":
    main()
