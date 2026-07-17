from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from vonavy_agent.domain import JobState
from vonavy_agent.errors import AgentError
from vonavy_agent.hashing import canonical_json, file_hash
from vonavy_agent.managed_files import fsync_tree, verified_managed_file
from vonavy_agent.persistence import (
    ExperimentSpecRow,
    GateResultRow,
    Job,
    Run,
)
from vonavy_agent.settings import Settings


@dataclass(frozen=True)
class StagedExport:
    result: dict[str, Any]
    staging_path: Path
    final_path: Path


def safe_embedded_json(value: str) -> str:
    return value.replace("<", "\\u003c")


def stage_static_export(
    engine: Engine,
    settings: Settings,
    export_id: str,
    run_ids: list[str],
    stage_dir: Path,
) -> StagedExport:
    reports: list[dict[str, Any]] = []
    with Session(engine) as session:
        for run_id in sorted(set(run_ids)):
            run = session.get(Run, run_id)
            if (
                run is None
                or not run.summary_json
                or not run.artifact_relative_path
                or not run.manifest_hash
            ):
                raise AgentError(
                    "run_not_exportable",
                    f"Run {run_id} is not a successful published run",
                )
            if session.get_one(Job, run.job_id).state != JobState.SUCCEEDED.value:
                raise AgentError(
                    "run_not_exportable",
                    f"Run {run_id} is not a successful published run",
                )
            spec = session.get_one(ExperimentSpecRow, run.spec_id)
            gate = session.get_one(GateResultRow, run.gate_result_id)
            manifest_relative = Path(run.artifact_relative_path) / "manifest.json"
            with verified_managed_file(
                settings, manifest_relative, run.manifest_hash
            ) as manifest_handle:
                manifest = json.load(manifest_handle)
            reports.append(
                {
                    "run_id": run.id,
                    "summary": json.loads(run.summary_json),
                    "spec": json.loads(spec.canonical_json),
                    "gate": json.loads(gate.canonical_json),
                    "manifest": manifest,
                }
            )
    report = {
        "schema_version": "1.0",
        "title": "Experiment Agent evidence report",
        "runs": reports,
        "limitations": [
            "Calibration evidence is for development selection; test evidence is audit-only.",
            "Coverage is based on rows common to every compared model and the explicit product-availability scoring policy.",
            "Unlabelled anomaly outputs, when imported, are exceedance/alert rates, not false-alarm rates.",
            "Chronos is not executed by this built-in vertical slice.",
        ],
    }
    shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True)
    report_json = canonical_json(report)
    (stage_dir / "report.json").write_text(report_json, encoding="utf-8")
    embedded = safe_embedded_json(report_json)
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
    (stage_dir / "index.html").write_text(document, encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "export_id": export_id,
        "run_ids": sorted(set(run_ids)),
        "outputs": {
            name: {
                "sha256": file_hash(stage_dir / name),
                "bytes": (stage_dir / name).stat().st_size,
            }
            for name in ("index.html", "report.json")
        },
    }
    (stage_dir / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    staging_zip = stage_dir / "report.zip"
    with zipfile.ZipFile(staging_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("index.html", "report.json", "manifest.json"):
            archive.write(stage_dir / name, arcname=name)
    fsync_tree(stage_dir)
    final_path = settings.managed_root / "exports" / f"experiment-agent-report-{export_id}.zip"
    result = {
        "export_id": export_id,
        "relative_path": str(Path("exports") / final_path.name),
        "staging_relative_path": str(staging_zip.relative_to(settings.managed_root)),
        "sha256": file_hash(staging_zip),
        "bytes": staging_zip.stat().st_size,
        "manifest": manifest,
    }
    return StagedExport(result=result, staging_path=staging_zip, final_path=final_path)


def create_static_export(
    engine: Engine,
    settings: Settings,
    export_id: str,
    run_ids: list[str],
    before_publish: Callable[[], None] | None = None,
) -> dict[str, Any]:
    stage_dir = Path(
        tempfile.mkdtemp(
            prefix=f"direct-export-{export_id}-",
            dir=settings.managed_root / "jobs" / "tmp",
        )
    )
    staged = stage_static_export(engine, settings, export_id, run_ids, stage_dir)
    if before_publish is not None:
        before_publish()
    staged.final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged.staging_path, staged.final_path)
    result = dict(staged.result)
    result.pop("staging_relative_path", None)
    shutil.rmtree(stage_dir, ignore_errors=True)
    return result
