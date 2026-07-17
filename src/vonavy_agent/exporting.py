from __future__ import annotations

import json
import os
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_json, file_hash
from vonavy_agent.persistence import (
    ExperimentSpecRow,
    GateResultRow,
    Run,
)
from vonavy_agent.settings import Settings


def create_static_export(
    engine: Engine,
    settings: Settings,
    export_id: str,
    run_ids: list[str],
    before_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    with Session(engine) as session:
        for run_id in sorted(set(run_ids)):
            run = session.get(Run, run_id)
            if run is None or not run.summary_json or not run.artifact_relative_path:
                raise AgentError(
                    "run_not_exportable", f"Run {run_id} is not a successful published run"
                )
            spec = session.get_one(ExperimentSpecRow, run.spec_id)
            gate = session.get_one(GateResultRow, run.gate_result_id)
            artifact = settings.managed_root / run.artifact_relative_path
            reports.append(
                {
                    "run_id": run.id,
                    "summary": json.loads(run.summary_json),
                    "spec": json.loads(spec.canonical_json),
                    "gate": json.loads(gate.canonical_json),
                    "manifest": json.loads(
                        (artifact / "manifest.json").read_text(encoding="utf-8")
                    ),
                }
            )
    report = {
        "schema_version": "1.0",
        "title": "Experiment Agent evidence report",
        "runs": reports,
        "limitations": [
            "Calibration evidence is for development selection; test evidence is audit-only.",
            "Coverage is based on rows common to every compared model.",
            "Unlabelled anomaly outputs, when imported, are exceedance/alert rates, not false-alarm rates.",
            "Chronos is not executed by this built-in vertical slice.",
        ],
    }
    export_dir = settings.managed_root / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=f"{export_id}-", dir=export_dir))
    try:
        report_json = canonical_json(report)
        (work_dir / "report.json").write_text(report_json, encoding="utf-8")
        embedded = report_json.replace("</", "<\\/")
        document = f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Experiment Agent evidence report</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#eee;color:#111;font:15px Arial,sans-serif}}
main{{max-width:1280px;margin:32px auto;background:#fff;border:3px solid #000;padding:32px}}
h1,h2,h3{{margin-top:0}}.brand{{font-weight:800;letter-spacing:.08em;border-bottom:3px solid #000;padding-bottom:16px}}
.card{{border:2px solid #000;padding:18px;margin:18px 0}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #000;padding:8px;text-align:left}}.muted{{color:#555}}code{{word-break:break-all}}
</style></head>
<body><main><div class="brand">NOTINO / Interview Assignment</div>
<h1>Experiment Agent evidence report</h1><p class="muted">Self-contained static export</p>
<div id="report"></div></main>
<script id="data" type="application/json">{embedded}</script>
<script>
const d=JSON.parse(document.getElementById("data").textContent),root=document.getElementById("report");
const el=(tag,text,cls)=>{{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n}};
for(const run of d.runs){{const c=el("section",undefined,"card");c.append(el("h2","Run "+run.run_id));
c.append(el("p","Status: passed gate; runtime "+run.summary.runtime_seconds.toFixed(3)+" seconds."));
for(const role of ["calibration","test"]){{c.append(el("h3",role==="test"?"Test / audit evidence":"Calibration / selection evidence"));
const rows=run.summary.metrics.filter(x=>x.role===role&&x.origin===null&&x.horizon===null);
const t=el("table"),head=el("tr");for(const x of ["Model","Metric","Value","Rows","Coverage"])head.append(el("th",x));t.append(head);
for(const r of rows){{const tr=el("tr");for(const x of [r.model,r.metric,r.value===null?"Unsupported: "+r.unsupported_reason:String(r.value),String(r.row_count),String(r.coverage)])tr.append(el("td",x));t.append(tr)}}c.append(t)}}
c.append(el("h3","Provenance"));c.append(el("code",JSON.stringify(run.manifest.source)));root.append(c)}}
const limits=el("section",undefined,"card");limits.append(el("h2","Limitations"));const ul=el("ul");for(const x of d.limitations)ul.append(el("li",x));limits.append(ul);root.append(limits);
</script></body></html>"""
        (work_dir / "index.html").write_text(document, encoding="utf-8")
        manifest = {
            "schema_version": "1.0",
            "export_id": export_id,
            "run_ids": sorted(set(run_ids)),
            "outputs": {
                name: {
                    "sha256": file_hash(work_dir / name),
                    "bytes": (work_dir / name).stat().st_size,
                }
                for name in ("index.html", "report.json")
            },
        }
        (work_dir / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
        zip_temp = export_dir / f".{export_id}.tmp"
        with zipfile.ZipFile(zip_temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ("index.html", "report.json", "manifest.json"):
                archive.write(work_dir / name, arcname=name)
        final = export_dir / f"experiment-agent-report-{export_id}.zip"
        if before_publish is not None:
            before_publish()
        os.replace(zip_temp, final)
        return {
            "export_id": export_id,
            "relative_path": str(Path("exports") / final.name),
            "sha256": file_hash(final),
            "bytes": final.stat().st_size,
            "manifest": manifest,
        }
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)
