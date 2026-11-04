"""
把首帧图上传到图床，刷新清单里的公网链接
=========================================
litterbox 的链接只有 72 小时有效，云端每次跑之前都要重新传一遍拿新链接。

用法：python scripts/upload_frames.py
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(BASE, "manifest.json")
FRAMES = os.path.join(BASE, "frames")
LITTER = "https://litterbox.catbox.moe/resources/internals/api.php"


def upload(path, tries=4):
    cmd = ["curl", "-s", "-m", "120", "-F", "reqtype=fileupload",
           "-F", "time=72h", "-F", f"fileToUpload=@{path}", LITTER]
    for k in range(tries):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
            u = (r.stdout or "").strip()
            if u.startswith("http"):
                return u
        except Exception:
            pass
        time.sleep(6 + k * 6)
    return None


def main():
    with open(MANIFEST, encoding="utf-8") as f:
        cfg = json.load(f)
    ok = 0
    for s in cfg["shots"]:
        p = os.path.join(FRAMES, f"{s['idx']}.png")
        if not os.path.isfile(p):
            print(f"{s['idx']:6} 缺图，跳过")
            continue
        url = upload(p)
        if url:
            s["first_frame"] = url
            s["_checked_at"] = 0
            ok += 1
            print(f"{s['idx']:6} -> {url}")
        else:
            print(f"{s['idx']:6} 上传失败，沿用旧链接")
        time.sleep(2)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"链接刷新完成 {ok}/{len(cfg['shots'])}")


if __name__ == "__main__":
    main()
