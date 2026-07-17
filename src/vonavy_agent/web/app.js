"use strict";

const state = {version:null,mapping:null,profile:null,spec:null,gate:null,runs:[],proposal:null};
const $ = id => document.getElementById(id);
const notice = (message,error=false) => {const n=$("notice");n.textContent=message;n.className=error?"notice error":"notice"};
const show = (id,message,className="result") => {const n=$(id);n.replaceChildren(document.createTextNode(message));n.className=className};
const api = async (path,options={}) => {
  const response=await fetch(path,options);
  const body=await response.json().catch(()=>({error:{code:"invalid_response",message:"Server returned invalid JSON"}}));
  if(!response.ok) throw new Error(`${body.error?.code||response.status}: ${body.error?.message||response.statusText}`);
  return body;
};
const jsonOptions = value => ({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
const pollJob = async jobId => {
  for(;;){
    const job=await api(`/api/jobs/${jobId}`);
    if(["succeeded","failed","cancelled"].includes(job.state)) return job;
    await new Promise(resolve=>setTimeout(resolve,700));
  }
};
const table = (headers,rows) => {
  const node=document.createElement("table"),head=document.createElement("tr");
  for(const value of headers){const cell=document.createElement("th");cell.textContent=value;head.append(cell)}node.append(head);
  for(const row of rows){const tr=document.createElement("tr");for(const value of row){const td=document.createElement("td");if(value instanceof Node)td.append(value);else td.textContent=value??"";tr.append(td)}node.append(tr)}
  return node;
};
const resultNode = id => {const node=$(id);node.replaceChildren();node.className="result";return node};
const addDays = (iso,count) => {const value=new Date(`${iso}T00:00:00Z`);value.setUTCDate(value.getUTCDate()+count);return value.toISOString().slice(0,10)};

$("upload-form").addEventListener("submit",async event=>{
  event.preventDefault();notice("Copying and hashing dataset…");
  try{
    const data=new FormData();data.append("dataset_name",$("dataset-name").value);data.append("file",$("dataset-file").files[0]);
    state.version=await api("/api/datasets/upload",{method:"POST",body:data});
    show("dataset-result",`Version ${state.version.version_number} / ${state.version.row_count} rows / SHA-256 ${state.version.materialized_sha256}`);
    notice("Immutable dataset version created. Define the mapping.");
  }catch(error){notice(error.message,true)}
});

$("mapping-form").addEventListener("submit",async event=>{
  event.preventDefault();if(!state.version){notice("Ingest a dataset first.",true);return}
  notice("Validating mapping and profiling in the worker…");
  try{
    const availabilityKind=$("target-availability").value;
    const targetAvailability={kind:availabilityKind};
    if(availabilityKind==="column")targetAvailability.column=$("target-availability-column").value;
    const mapping={
      timestamp_column:$("timestamp-column").value,
      entity_column:$("entity-column").value||null,
      target_column:$("target-column").value,
      frequency:"D",
      target_availability:targetAvailability,
      features:JSON.parse($("features-json").value)
    };
    state.mapping=await api(`/api/dataset-versions/${state.version.id}/mappings`,jsonOptions(mapping));
    const queued=await api("/api/profiles",jsonOptions({dataset_version_id:state.version.id,mapping_id:state.mapping.id}));
    const job=await pollJob(queued.id);if(job.state!=="succeeded")throw new Error(job.error?.message||`Profile ${job.state}`);
    state.profile=await api(`/api/profiles/${job.result.profile_id}`);
    const p=state.profile.profile,node=resultNode("profile-result");
    node.append(table(["Rows","Entities","Dates","Duplicates","Gap days"],[[
      String(p.rows),String(p.entities),`${p.date_start} — ${p.date_end}`,String(p.duplicate_key_rows),String(p.gap_days)
    ]]));
    const trainStart=p.date_start,trainEnd=addDays(p.date_end,-42),calStart=addDays(p.date_end,-41),calEnd=addDays(p.date_end,-21),testStart=addDays(p.date_end,-20),testEnd=p.date_end;
    $("train-start").value=trainStart;$("train-end").value=trainEnd;$("cal-start").value=calStart;$("cal-end").value=calEnd;$("test-start").value=testStart;$("test-end").value=testEnd;
    $("cal-origin").value=calStart;$("test-origin").value=testStart;$("evaluation-as-of").value=testEnd;
    notice("Profile complete. Review the proposed temporal boundaries.");
  }catch(error){notice(error.message,true)}
});

$("spec-form").addEventListener("submit",async event=>{
  event.preventDefault();if(!state.profile){notice("Profile the mapped dataset first.",true);return}
  notice("Creating canonical spec and running the mandatory gate…");
  try{
    const models=[];
    if($("model-seasonal").checked)models.push({kind:"seasonal_naive",period_days:7});
    if($("model-moving").checked)models.push({kind:"moving_average",window_days:28});
    if($("model-ridge").checked)models.push({kind:"ridge_direct",alpha:1,lag_days:[1,7,14,28],rolling_days:[7,28]});
    const spec={
      schema_version:"1.0",dataset_version_id:state.version.id,mapping_id:state.mapping.id,profile_id:state.profile.id,frequency:"D",
      train:{start:$("train-start").value,end:$("train-end").value},
      calibration:{start:$("cal-start").value,end:$("cal-end").value},
      test:{start:$("test-start").value,end:$("test-end").value},
      origins:[{date:$("cal-origin").value,role:"calibration"},{date:$("test-origin").value,role:"test"}],
      horizon_days:Number($("horizon").value),training_window_days:Number($("training-window").value),
      entity_column:state.mapping.mapping.entity_column,target_column:state.mapping.mapping.target_column,
      features:state.mapping.mapping.features,
      feature_allow_list:state.mapping.mapping.features.filter(x=>x.role!=="excluded").map(x=>x.name),
      models,seeds:[42],
      metrics:["wape","mae","rmse","bias","coverage","runtime"],
      evaluation_as_of:`${$("evaluation-as-of").value}T23:59:59Z`,minimum_coverage:Number($("minimum-coverage").value),
      resources:{max_rows:500000,max_entities:5000,max_origins:50,max_models:3,wall_seconds:900,memory_mb:4096,threads:1}
    };
    state.spec=await api("/api/specs",jsonOptions(spec));
    const queued=await api(`/api/specs/${state.spec.id}/gate`,{method:"POST"}),job=await pollJob(queued.id);
    if(job.state!=="succeeded")throw new Error(job.error?.message||`Gate ${job.state}`);
    state.gate=await api(`/api/gates/${job.result.gate_result_id}`);
    const report=state.gate.report,node=resultNode("gate-result");node.classList.add(report.status==="passed"?"status-pass":"status-block");
    const title=document.createElement("strong");title.textContent=`Gate ${report.status.toUpperCase()} / coverage ${Number(report.evidence.coverage).toFixed(3)}`;node.append(title);
    for(const item of [...report.reasons,...report.warnings]){const p=document.createElement("p");p.textContent=`${item.code}: ${item.message}${item.count?` (${item.count})`:""}`;node.append(p)}
    $("run-button").disabled=report.status!=="passed";$("planner-button").disabled=false;
    notice(report.status==="passed"?"Gate passed. Review and explicitly confirm before enqueueing.":"Gate blocked. Correct data or boundaries; nothing was executed.",report.status!=="passed");
  }catch(error){notice(error.message,true)}
});

$("run-confirm").addEventListener("change",()=>{$("run-button").disabled=!(state.gate?.report.status==="passed"&&$("run-confirm").checked)});
$("run-button").addEventListener("click",async()=>{
  if(!$("run-confirm").checked||!state.gate)return;notice("Experiment queued; monitoring the separate worker…");
  try{
    const created=await api("/api/runs",jsonOptions({spec_id:state.spec.id,gate_result_id:state.gate.id,confirmation_token:state.gate.report.confirmation_token}));
    show("run-result",`Run ${created.run_id} / ${created.job.state}`);
    const job=await pollJob(created.job.id);
    if(job.state!=="succeeded")throw new Error(job.error?.message||`Run ${job.state}`);
    const run=await api(`/api/runs/${created.run_id}`);
    show("run-result",`Run ${run.id} succeeded in ${run.summary.runtime_seconds.toFixed(3)} seconds; ${run.summary.prediction_rows} prediction rows.`);
    notice("Experiment succeeded. Compare common-row evidence or export it.");await refreshRuns();
  }catch(error){notice(error.message,true)}
});

$("planner-button").addEventListener("click",async()=>{
  if(!state.spec)return;notice("Planner is inspecting profile and eligible calibration evidence…");
  try{
    state.proposal=await api(`/api/planner/proposals/${state.spec.id}`,{method:"POST"});
    const node=resultNode("planner-result"),items=state.proposal.proposal.proposals;
    if(!items.length){node.textContent="No bounded next experiment is currently justified.";return}
    for(const item of items){const card=document.createElement("p"),strong=document.createElement("strong");strong.textContent=`${item.rank}. ${item.kind}`;card.append(strong,document.createTextNode(` — ${item.reason} Cost: ${item.estimated_cost}. Confirmation required.`));node.append(card)}
    notice("Deterministic proposals are ready for review; none were enqueued.");
  }catch(error){notice(error.message,true)}
});

const refreshRuns=async()=>{
  const response=await api("/api/runs");state.runs=response.runs;
  const node=resultNode("runs-result"),rows=state.runs.map(run=>{
    const box=document.createElement("input");box.type="checkbox";box.className="run-select";box.value=run.id;box.disabled=run.job.state!=="succeeded";
    return [box,run.id,run.job.state,run.summary?run.summary.runtime_seconds.toFixed(3):"—",run.manifest_hash||"—"];
  });node.append(table(["Select","Run","State","Seconds","Manifest SHA-256"],rows));
};
$("refresh-runs").addEventListener("click",()=>refreshRuns().catch(error=>notice(error.message,true)));
const selectedRuns=()=>Array.from(document.querySelectorAll(".run-select:checked")).map(node=>node.value);
$("compare-button").addEventListener("click",async()=>{
  const ids=selectedRuns();if(!ids.length){notice("Select at least one successful run.",true);return}
  try{
    const comparison=await api("/api/comparisons",jsonOptions({run_ids:ids})),node=resultNode("compare-result");
    node.append(table(["Run","Role","Model","Metric","Value","Rows","Coverage"],comparison.metrics.map(item=>[
      item.run_id,item.role,item.model,item.metric,item.value===null?`Unsupported: ${item.unsupported_reason}`:String(item.value),String(item.row_count),String(item.coverage)
    ])));notice("Comparison uses common availability-eligible rows.");
  }catch(error){notice(error.message,true)}
});
$("export-button").addEventListener("click",async()=>{
  const ids=selectedRuns();if(!ids.length){notice("Select at least one successful run.",true);return}
  notice("Building self-contained static export…");
  try{
    const queued=await api("/api/exports",jsonOptions({run_ids:ids})),job=await pollJob(queued.job.id);
    if(job.state!=="succeeded")throw new Error(job.error?.message||`Export ${job.state}`);
    const node=resultNode("export-result"),link=document.createElement("a");link.href=`/api/exports/${queued.export_id}/download`;link.textContent=`Download experiment-agent-report-${queued.export_id}.zip`;node.append(link);
    notice("Static HTML + JSON export is ready.");
  }catch(error){notice(error.message,true)}
});

refreshRuns().catch(error=>notice(error.message,true));
