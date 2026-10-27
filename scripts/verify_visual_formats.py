"""Opt-in live checks for the other two supported text visualization formats."""
import json
import time
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
path=ROOT/"data"/"live-verification.json"
report=json.loads(path.read_text(encoding="utf-8"))
sid=report["reading_space_id"]
source=next(x for x in report["checks"] if x["name"]=="upload_and_read")
prompts={
    "mermaid":"请使用 mermaid 格式的 flowchart 为本节的三种深度优先遍历画一张概念关系图，分别列出根、左、右的访问顺序。只画关系图，不演示过程。",
    "svg":"请使用 svg 格式的静态图（format=svg），画资料中 A(B(D,E),C) 这棵二叉树，明确标注根节点、左子树和右子树。使用基本 SVG 图形和文字，不要步骤动画，也不要 Mermaid。"
}
with httpx.Client(base_url="http://127.0.0.1:8018",trust_env=False,timeout=30) as client:
    for fmt,prompt in prompts.items():
        response=client.post(f"/api/spaces/{sid}/visualize",json={"material_id":source["material_id"],"page":source["page"],"message":prompt})
        response.raise_for_status();jid=response.json()["job_id"]
        for _ in range(240):
            state=client.get(f"/api/spaces/{sid}/jobs/{jid}").json()
            if state["status"]=="failed":raise RuntimeError(state["error"])
            if state["status"]=="completed":break
            time.sleep(1)
        else:raise RuntimeError("Visualization timed out")
        artifact=client.get(f"/api/spaces/{sid}/artifacts/{state['result']['artifact_id']}").json()
        assert artifact["format"]==fmt,artifact["format"]
        report[fmt+"_artifact_id"]=artifact["id"]
        report["checks"].append({"name":fmt+"_live_generation","ok":True,"artifact_id":artifact["id"]})
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(fmt+": PASS",flush=True)
