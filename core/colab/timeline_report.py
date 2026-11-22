"""一次 Colab 作业在 VM 上各阶段的耗时和单元（job/timeline.jsonl，控制端取回后在作业目录 DATA/colab/<任务 id>/job/ 下）。
用法：python3 colab/timeline_report.py <作业目录> [每小时单元数，默认 L4 的 1.94]"""
import json
import sys
from pathlib import Path

run = Path(sys.argv[1])
rate = float(sys.argv[2]) if len(sys.argv) > 2 else 1.94
for line in (run / "job" / "timeline.jsonl").read_text(encoding="utf-8").splitlines():
    r = json.loads(line)
    if r.get("phase") == "end" and "sec" in r:
        extra = {k: r[k] for k in ("status", "best", "error") if r.get(k)}
        print(f"{r['sec']:8.1f}s  {r['sec'] / 3600 * rate:6.3f}u  {r['step']} {json.dumps(extra, ensure_ascii=False)}")
