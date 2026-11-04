#!/usr/bin/env bash
# 云端主脚本：跑生成 → 产出落在 output/
set -uo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8

mkdir -p output
echo "===== [$(date -u '+%F %T')] 开始 ====="
echo "首帧链接：GitHub raw 永久地址，无需重传"

# 首帧链接自检：raw 链接不通就自动改用图床上传
echo ">> 检查首帧链接…"
FIRST=$(python -c "import json;print(json.load(open('scripts/manifest.json',encoding='utf-8'))['shots'][0]['first_frame'])")
if curl -s -m 25 -o /dev/null -w "%{http_code}" "$FIRST" | grep -q "^200$"; then
  echo "首帧链接正常（GitHub raw 永久地址）"
else
  echo "!! raw 链接不通，自动改用图床上传…"
  python scripts/upload_frames.py
fi

# 跑生成守护（1.6 小时一轮，3 路并发，短镜头优先）
echo ">> 开始生成…"
python scripts/guardian.py --cfg manifest.json --out ../output --hours 0.4 --probe 12 --workers 3

cp -f scripts/生成日志.txt scripts/logs.txt 2>/dev/null || true
cp -f scripts/生成日志.txt scripts/logs.txt 2>/dev/null || true
echo "===== [$(date -u '+%F %T')] 结束，产出：$(ls -1 output/*.mp4 2>/dev/null | wc -l) 条 ====="
