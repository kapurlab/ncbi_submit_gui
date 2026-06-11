import { useState, useEffect, useRef } from "react";
import "./App.css";

const APP_VERSION = "0.1.0";

function fileIcon(name) {
  if (name.endsWith(".json")) return "📁";
  if (name.endsWith(".tsv") || name.endsWith(".xlsx")) return "📊";
  if (name.endsWith(".pdf")) return "📄";
  if (name.endsWith(".xml")) return "🧾";
  if (name.endsWith(".png")) return "🖼";
  if (name.endsWith(".fasta") || name.endsWith(".fa") || name.endsWith(".fna")) return "🧬";
  if (name.endsWith(".txt")) return "📝";
  return "📁";
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function App() {
  const [projects, setProjects] = useState([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [activeProject, setActiveProject] = useState("");
  const [expanded, setExpanded] = useState({});

  // Inputs per project: { [name]: { fastq, fasta, metadata } } each {files,count,total_bytes}
  const [inputs, setInputs] = useState({});
  const [addPath, setAddPath] = useState({});       // { "<proj>:<kind>": path }
  const [addStatus, setAddStatus] = useState({});
  const uploadRef = useRef({ project: "", kind: "fastq" });
  const uploadInputRef = useRef(null);

  // Config + run options
  const [cfg, setCfg] = useState({});
  const [settingsDraft, setSettingsDraft] = useState({});
  const [presets, setPresets] = useState([]);
  const [organism, setOrganism] = useState("generic");
  const [archive, setArchive] = useState("both");
  const [mode, setMode] = useState("prep");
  const [target, setTarget] = useState("test");
  const [dryRun, setDryRun] = useState(true);
  const [noNcbiCheck, setNoNcbiCheck] = useState(false);
  const [selectedMeta, setSelectedMeta] = useState("");

  // Jobs / results
  const [running, setRunning] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState("idle");
  const [logLines, setLogLines] = useState([]);
  const [currentStep, setCurrentStep] = useState("");
  const [activeRun, setActiveRun] = useState(null);    // {project, run_id}
  const [runResults, setRunResults] = useState(null);  // {files:[]}

  const [showSettings, setShowSettings] = useState(false);
  const [showProjects, setShowProjects] = useState(true);
  const [showRun, setShowRun] = useState(true);
  const [showResults, setShowResults] = useState(true);
  const [showLogs, setShowLogs] = useState(true);
  const [folderBrowser, setFolderBrowser] = useState({ open: false, path: "", parent: null, entries: [], loading: false, error: "" });

  const logRef = useRef(null);
  const esRef = useRef(null);

  useEffect(() => {
    fetch("./api/config").then((r) => r.json()).then((c) => {
      setCfg(c); setSettingsDraft(c);
      if (c.organism_preset) setOrganism(c.organism_preset);
      if (c.submit_target) setTarget(c.submit_target);
    }).catch(() => {});
    fetch("./api/organism-presets").then((r) => r.json()).then((p) => {
      if (Array.isArray(p)) setPresets(p);
    }).catch(() => {});
    loadProjects();
  }, []);

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) el.scrollTop = el.scrollHeight;
  }, [logLines]);

  function loadProjects() {
    setProjectsLoading(true);
    fetch("./api/projects").then((r) => r.json()).then((data) => {
      setProjects(data); setProjectsLoading(false);
      if (data.length && (!activeProject || !data.find((p) => p.name === activeProject))) {
        setActiveProject(data[0].name);
        loadInputs(data[0].name);
      }
    }).catch(() => setProjectsLoading(false));
  }

  function loadInputs(name) {
    ["fastq", "fasta", "metadata"].forEach((kind) => {
      const url = kind === "metadata"
        ? `./api/projects/${encodeURIComponent(name)}/metadata`
        : `./api/projects/${encodeURIComponent(name)}/inputs?kind=${kind}`;
      fetch(url).then((r) => r.json())
        .then((d) => setInputs((m) => ({ ...m, [name]: { ...(m[name] || {}), [kind]: d } })))
        .catch(() => {});
    });
  }

  async function createProject() {
    const name = newProjectName.trim();
    if (!name || creatingProject) return;
    setCreatingProject(true);
    try {
      const res = await fetch("./api/projects", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); window.alert(`Could not create project: ${d.detail || res.status}`); return; }
      const created = await res.json().catch(() => ({}));
      setNewProjectName(""); loadProjects();
      if (created.name) { setExpanded((e) => ({ ...e, [created.name]: true })); setActiveProject(created.name); loadInputs(created.name); }
    } finally { setCreatingProject(false); }
  }

  function toggleProject(name) {
    const isOpen = expanded[name];
    setExpanded((e) => ({ ...e, [name]: !isOpen }));
    setActiveProject(name);
    if (!isOpen) loadInputs(name);
  }

  const setStat = (key, msg) => setAddStatus((m) => ({ ...m, [key]: msg }));

  async function linkLocal(name, kind) {
    const key = `${name}:${kind}`;
    const path = (addPath[key] || "").trim();
    if (!path) return;
    setStat(key, "Linking…");
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/link-local`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, kind }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(key, `Failed: ${d.detail || res.status}`); return; }
      setStat(key, `Linked ${d.linked} file(s).`);
      setAddPath((m) => ({ ...m, [key]: "" }));
      loadInputs(name); loadProjects();
    } catch (e) { setStat(key, `Failed: ${e.message}`); }
  }

  function pickFiles(name, kind) {
    uploadRef.current = { project: name, kind };
    const accept = kind === "fasta" ? ".fasta,.fa,.fna" : kind === "metadata" ? ".xlsx,.xls" : ".fastq.gz,application/gzip";
    if (uploadInputRef.current) { uploadInputRef.current.accept = accept; uploadInputRef.current.click(); }
  }

  async function uploadFiles(name, kind, fileList) {
    const files = Array.from(fileList || []);
    if (!name || !files.length) return;
    const key = `${name}:${kind}`;
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    setStat(key, `Uploading ${files.length} file(s)…`);
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/upload?kind=${kind}`, { method: "POST", body: fd });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(key, `Upload failed: ${d.detail || res.status}`); return; }
      setStat(key, `Uploaded ${d.uploaded} file(s).`);
      loadInputs(name); loadProjects();
    } catch (e) { setStat(key, `Upload failed: ${e.message}`); }
  }

  async function deleteInput(name, kind, filename) {
    if (!window.confirm(`Remove ${filename}?`)) return;
    await fetch(`./api/projects/${encodeURIComponent(name)}/inputs/${encodeURIComponent(filename)}?kind=${kind}`, { method: "DELETE" }).catch(() => {});
    loadInputs(name); loadProjects();
  }

  function runPipeline() {
    if (running || !activeProject) return;
    const metaFiles = inputs[activeProject]?.metadata?.files || [];
    const meta = selectedMeta || (metaFiles[0]?.name || "");
    if (!meta) { window.alert("Upload an NCBI metadata workbook (.xlsx) first."); return; }
    setShowLogs(true); setRunning(true); setJobStatus("running"); setLogLines([]); setCurrentStep("");
    const endpoint = mode === "submit" ? "submit" : "prep";
    fetch(`./api/run/${endpoint}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project: activeProject, metadata: meta, organism, archive, mode,
        target, dry_run: dryRun, no_ncbi_check: noNcbiCheck,
      }),
    })
      .then((r) => (r.ok ? r.json() : r.json().then((e) => { throw new Error(e.detail || "Run failed"); })))
      .then(({ job_id, run_id }) => {
        setJobId(job_id); setActiveRun({ project: activeProject, run_id });
        streamLog(job_id, activeProject, run_id);
      })
      .catch((err) => { setLogLines((p) => [...p, `ERROR: ${err.message}`]); setRunning(false); setJobStatus("failed"); });
  }

  function streamLog(id, project, runId) {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    const es = new EventSource(`./api/jobs/${id}/log`);
    esRef.current = es;
    es.onmessage = (evt) => {
      const data = evt.data;
      if (data === "[DONE]") {
        es.close(); setRunning(false);
        fetch(`./api/jobs/${id}`).then((r) => r.json()).then((job) => {
          setJobStatus(job.status); setCurrentStep("");
          loadRunResults(project, runId); loadProjects();
        }).catch(() => {});
      } else {
        setLogLines((p) => [...p, data]);
        if (/^###/.test(data) || /completed/i.test(data)) setCurrentStep(data.replace(/^#+\s*/, "").trim());
      }
    };
    es.onerror = () => { es.close(); setRunning(false); setJobStatus("failed"); };
  }

  function loadRunResults(project, runId) {
    setShowResults(true);
    setRunResults({ loading: true });
    fetch(`./api/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(runId)}/results`)
      .then((r) => r.json())
      .then((d) => setRunResults({ loading: false, project, ...d }))
      .catch(() => setRunResults({ loading: false, present: false, files: [] }));
  }

  function browseDirs(path) {
    setFolderBrowser((s) => ({ ...s, loading: true, error: "" }));
    fetch(`./api/browse-dirs?path=${encodeURIComponent(path || "")}`)
      .then((r) => (r.ok ? r.json() : r.json().then((e) => { throw new Error(e.detail || "Cannot open folder"); })))
      .then((d) => setFolderBrowser((s) => ({ ...s, path: d.path, parent: d.parent, entries: d.entries, loading: false })))
      .catch((err) => setFolderBrowser((s) => ({ ...s, loading: false, error: err.message })));
  }

  function saveSettings() {
    const body = {
      projects_root: settingsDraft.projects_root, organism_preset: settingsDraft.organism_preset,
      submit_target: settingsDraft.submit_target, ncbi_email: settingsDraft.ncbi_email,
      ncbi_ftp_host: settingsDraft.ncbi_ftp_host, ncbi_ftp_user: settingsDraft.ncbi_ftp_user,
      ncbi_organization: settingsDraft.ncbi_organization, ncbi_contact_first: settingsDraft.ncbi_contact_first,
      ncbi_contact_last: settingsDraft.ncbi_contact_last,
    };
    // Only send secrets when the user typed a new value (the form starts blank).
    if (settingsDraft._ncbi_api_key) body.ncbi_api_key = settingsDraft._ncbi_api_key;
    if (settingsDraft._ncbi_ftp_pass) body.ncbi_ftp_pass = settingsDraft._ncbi_ftp_pass;
    fetch("./api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then((r) => r.json()).then(() => fetch("./api/config").then((r) => r.json()).then((c) => { setCfg(c); setSettingsDraft(c); }))
      .catch(() => {});
  }

  const logLineClass = (line) => {
    if (line.startsWith("$ ")) return "log-line cmd";
    if (line.startsWith("###")) return "log-line cmd";
    if (/^ERROR/i.test(line)) return "log-line error";
    if (line === "[DONE]") return "log-line done";
    return "log-line";
  };
  const statusText = { idle: "idle", running: "running", succeeded: "succeeded", failed: "failed" }[jobStatus];
  const ai = inputs[activeProject] || {};
  const metaFiles = ai.metadata?.files || [];

  return (
    <div className="app">
      <input ref={uploadInputRef} type="file" multiple style={{ display: "none" }}
        onChange={(e) => { const f = Array.from(e.target.files); e.target.value = ""; uploadFiles(uploadRef.current.project, uploadRef.current.kind, f); }} />

      <header className="app-header">
        <div className="app-brand">
          <img className="app-logo" src="./ncbi_icon.svg" alt="NCBI submission upload icon" />
          <div>
            <h1>NCBI Submit <span className="version-tag">v{APP_VERSION}</span></h1>
            <p>Prepare &amp; submit SRA (FASTQ) and GenBank (FASTA) deposits from an Excel metadata sheet — validated, deduplicated, and report-backed.</p>
          </div>
        </div>
        <div className="status-pill"><span className="dot" data-state={jobStatus} /><span>{statusText}</span></div>
      </header>

      <main className="layout">
        <section className="status-strip">
          <div className="status-item"><span className="status-label">Project</span><span className="status-value">{activeProject || "—"}</span></div>
          <div className="status-item"><span className="status-label">Organism</span><span className="status-value">{organism}</span></div>
          <div className="status-item"><span className="status-label">Archive</span><span className="status-value cap">{archive}</span></div>
          <div className="status-item"><span className="status-label">Target</span>
            <span className="status-value cap">{mode === "submit" ? `${target}${dryRun ? " (dry-run)" : ""}` : "prep only"}</span></div>
          <div className="status-item"><span className="status-label">Job</span>
            <span className="status-value cap">{jobStatus === "running" ? <><span className="pulse-dot" />running</> : statusText}</span></div>
        </section>

        {/* ── Settings ── */}
        <div className="row-header">
          <h2>Settings &amp; NCBI credentials</h2>
          <button className="ghost" onClick={() => { if (!showSettings) fetch("./api/config").then((r) => r.json()).then(setSettingsDraft).catch(() => {}); setShowSettings(!showSettings); }}>{showSettings ? "Hide" : "Show"}</button>
        </div>
        {showSettings && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              <div className="form-section">
                <label className="form-label">Default organism preset</label>
                <select value={settingsDraft.organism_preset || "generic"} onChange={(e) => setSettingsDraft((d) => ({ ...d, organism_preset: e.target.value }))}>
                  {presets.map((p) => <option key={p.name} value={p.name}>{p.display_name}</option>)}
                </select>
              </div>
              <div className="form-section">
                <label className="form-label">NCBI contact email (E-utilities)</label>
                <input value={settingsDraft.ncbi_email || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, ncbi_email: e.target.value }))} placeholder="you@institution.gov" />
              </div>
              <div className="form-section">
                <label className="form-label">NCBI API key {cfg.ncbi_api_key_set && <span className="muted">(configured — leave blank to keep)</span>}</label>
                <input type="password" value={settingsDraft._ncbi_api_key || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, _ncbi_api_key: e.target.value }))} placeholder={cfg.ncbi_api_key_set ? "••••••••" : "(optional — raises eutils rate limit)"} />
              </div>
              <div className="form-section">
                <label className="form-label">Submission FTP host</label>
                <input value={settingsDraft.ncbi_ftp_host || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, ncbi_ftp_host: e.target.value }))} placeholder="ftp-private.ncbi.nlm.nih.gov" />
              </div>
              <div className="row" style={{ margin: 0 }}>
                <div className="form-section" style={{ flex: 1 }}>
                  <label className="form-label">FTP user</label>
                  <input value={settingsDraft.ncbi_ftp_user || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, ncbi_ftp_user: e.target.value }))} />
                </div>
                <div className="form-section" style={{ flex: 1 }}>
                  <label className="form-label">FTP password {cfg.ncbi_ftp_pass_set && <span className="muted">(set)</span>}</label>
                  <input type="password" value={settingsDraft._ncbi_ftp_pass || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, _ncbi_ftp_pass: e.target.value }))} placeholder={cfg.ncbi_ftp_pass_set ? "••••••••" : ""} />
                </div>
              </div>
              <div className="row" style={{ margin: 0 }}>
                <div className="form-section" style={{ flex: 1 }}>
                  <label className="form-label">Organization</label>
                  <input value={settingsDraft.ncbi_organization || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, ncbi_organization: e.target.value }))} />
                </div>
                <div className="form-section" style={{ flex: 1 }}>
                  <label className="form-label">Contact first name</label>
                  <input value={settingsDraft.ncbi_contact_first || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, ncbi_contact_first: e.target.value }))} />
                </div>
                <div className="form-section" style={{ flex: 1 }}>
                  <label className="form-label">Contact last name</label>
                  <input value={settingsDraft.ncbi_contact_last || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, ncbi_contact_last: e.target.value }))} />
                </div>
              </div>
              <div className="form-section">
                <label className="form-label">Default submission target</label>
                <select value={settingsDraft.submit_target || "test"} onChange={(e) => setSettingsDraft((d) => ({ ...d, submit_target: e.target.value }))}>
                  <option value="test">Test server (safe default)</option>
                  <option value="prod">Production</option>
                </select>
              </div>
              <div className="form-section">
                <label className="form-label">Personal projects root</label>
                <div style={{ display: "flex", gap: 6 }}>
                  <input style={{ flex: 1 }} value={settingsDraft.projects_root || ""} onChange={(e) => setSettingsDraft((d) => ({ ...d, projects_root: e.target.value }))} />
                  <button type="button" className="ghost" onClick={() => { setFolderBrowser({ open: true, path: "", parent: null, entries: [], loading: true, error: "" }); browseDirs(settingsDraft.projects_root || ""); }}>Browse…</button>
                </div>
              </div>
              <div className="note" style={{ marginBottom: 8 }}>
                Credentials are stored only in your private <code>~/.config/ncbi_submit_gui/config.json</code> (or env vars) — never in the repo. The submission target defaults to NCBI's <strong>test</strong> area.
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end" }}><button onClick={saveSettings}>Save</button></div>
            </section>
          </div>
        )}

        {/* ── Projects & Inputs ── */}
        <div className="row-header">
          <h2>Projects &amp; Inputs</h2>
          <button className="ghost" onClick={() => setShowProjects(!showProjects)}>{showProjects ? "Hide" : "Show"}</button>
        </div>
        {showProjects && (
          <div className="row-grid row-grid-split">
            <section className="panel">
              <div className="panel-header"><h2>Projects</h2><button className="ghost action" onClick={loadProjects}>↻ Refresh</button></div>
              <div className="row">
                <input placeholder="New project name (e.g. IAV_2026_batch)" value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value.replace(/\s+/g, "_"))}
                  onKeyDown={(e) => { if (e.key === "Enter") createProject(); }} disabled={creatingProject} />
                <button onClick={createProject} disabled={creatingProject || !newProjectName.trim()}>{creatingProject ? "Creating…" : "Create"}</button>
              </div>
              <div className="form-hint" style={{ marginTop: -4, marginBottom: 8 }}>Shared with the sibling GUIs. FASTQ → SRA, FASTA → GenBank, plus one Excel metadata sheet.</div>
              <div className="list project-list">
                {projectsLoading && <div className="loading-text">Loading projects…</div>}
                {!projectsLoading && projects.length === 0 && <div className="note">No projects found.</div>}
                {projects.map((proj) => (
                  <div key={proj.name} className={`list-item ${activeProject === proj.name ? "active" : ""}`}>
                    <div className="item-top" onClick={() => toggleProject(proj.name)}>
                      <span className="expand-icon">{expanded[proj.name] ? "▾" : "▸"}</span>
                      <div className="list-title" title={proj.name}>{proj.name}</div>
                      <span className={`scope-badge scope-${proj.scope}`}>{proj.scope}</span>
                    </div>
                    <div className="list-meta">
                      {proj.fastq_count} FASTQ · {proj.fasta_count} FASTA
                      {proj.submit_runs?.length > 0 && ` · ${proj.submit_runs.length} run${proj.submit_runs.length > 1 ? "s" : ""}`}
                    </div>
                    {expanded[proj.name] && proj.submit_runs?.length > 0 && (
                      <div className="sample-list">
                        {proj.submit_runs.map((rid) => (
                          <div key={rid} className="sample-item">
                            <div className="sample-name-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                              <div className="sample-name" style={{ flex: 1, cursor: "pointer" }} onClick={() => { setActiveProject(proj.name); loadRunResults(proj.name, rid); }} title="Show this run's results">{rid}</div>
                              <button className="ghost" style={{ fontSize: 11 }} onClick={() => { setActiveProject(proj.name); loadRunResults(proj.name, rid); }}>View ↓</button>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>

            <section className="panel">
              <div className="panel-header">
                <h2>Inputs</h2>
                {projects.length > 0 && (
                  <select value={activeProject} onChange={(e) => { setActiveProject(e.target.value); loadInputs(e.target.value); }} style={{ width: "auto", maxWidth: "60%", padding: "6px 10px" }}>
                    {projects.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
                  </select>
                )}
              </div>
              {!activeProject ? <div className="empty-msg">Create or pick a project, then add FASTQ, FASTA, and a metadata sheet.</div> : (
                <div className="input-columns">
                  {[
                    { kind: "fastq", title: "FASTQ (SRA)", hint: ".fastq.gz — paired reads", path: "/srv/kapurlab/… folder or .fastq.gz" },
                    { kind: "fasta", title: "FASTA (GenBank)", hint: ".fasta / .fa / .fna assemblies", path: "/srv/kapurlab/… folder or .fasta" },
                    { kind: "metadata", title: "Metadata (Excel)", hint: ".xlsx NCBI metadata sheet", path: "/srv/kapurlab/… .xlsx" },
                  ].map(({ kind, title, hint, path }) => {
                    const key = `${activeProject}:${kind}`;
                    const data = (inputs[activeProject] || {})[kind] || { files: [] };
                    return (
                      <div className="input-column" key={kind}>
                        <h3>{title}</h3>
                        <div className="row" style={{ margin: 0 }}>
                          <input placeholder={path} value={addPath[key] || ""} onChange={(e) => setAddPath((m) => ({ ...m, [key]: e.target.value }))}
                            onKeyDown={(e) => { if (e.key === "Enter") linkLocal(activeProject, kind); }} />
                          <button className="ghost action" onClick={() => linkLocal(activeProject, kind)} disabled={!(addPath[key] || "").trim()}>Link</button>
                        </div>
                        <div className="dropzone" onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); uploadFiles(activeProject, kind, e.dataTransfer.files); }} style={{ marginTop: 8 }}>
                          <button type="button" onClick={() => pickFiles(activeProject, kind)}>Choose Files</button>
                          <span className="drop-hint">{hint}</span>
                        </div>
                        {addStatus[key] && <div className="note" style={{ marginBottom: 0 }}>{addStatus[key]}</div>}
                        {data.files?.length > 0 && (
                          <div className="input-files" style={{ marginTop: 8 }}>
                            {data.files.map((f) => (
                              <div key={f.name} className="input-file-row">
                                <span className="file-name" title={f.name} style={{ flex: 1 }}>{f.name}</span>
                                <span className="file-size">{fmtSize(f.size)}</span>
                                <button className="ghost" style={{ fontSize: 11, padding: "2px 7px" }} onClick={() => deleteInput(activeProject, kind, f.name)} title="Remove">✕</button>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          </div>
        )}

        {/* ── Run ── */}
        <div className="row-header">
          <h2>Prepare &amp; Submit</h2>
          <button className="ghost" onClick={() => setShowRun(!showRun)}>{showRun ? "Hide" : "Show"}</button>
        </div>
        {showRun && (
          <div className="row-grid row-grid-split">
            <section className="panel">
              <h2>Configure</h2>
              <div className="form-section">
                <label className="form-label">Metadata workbook</label>
                <select value={selectedMeta} onChange={(e) => setSelectedMeta(e.target.value)} disabled={running}>
                  <option value="">{metaFiles.length ? "(first uploaded)" : "— upload one in Inputs —"}</option>
                  {metaFiles.map((f) => <option key={f.name} value={f.name}>{f.name}</option>)}
                </select>
              </div>
              <div className="form-section">
                <label className="form-label">Organism preset</label>
                <select value={organism} onChange={(e) => setOrganism(e.target.value)} disabled={running}>
                  {presets.map((p) => <option key={p.name} value={p.name}>{p.display_name}</option>)}
                </select>
              </div>
              <div className="form-section">
                <label className="form-label">Archive</label>
                <select value={archive} onChange={(e) => setArchive(e.target.value)} disabled={running}>
                  <option value="both">SRA + GenBank</option>
                  <option value="sra">SRA only (FASTQ)</option>
                  <option value="genbank">GenBank only (FASTA)</option>
                </select>
              </div>
              <div className="form-section">
                <label className="checkbox-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input type="checkbox" checked={mode === "submit"} onChange={(e) => setMode(e.target.checked ? "submit" : "prep")} disabled={running} />
                  <span>Submit programmatically (build submission.xml &amp; FTP). Unchecked = prepare files only.</span>
                </label>
                {mode === "submit" && (
                  <div style={{ marginTop: 8, paddingLeft: 22 }}>
                    <label className="form-label">Target</label>
                    <select value={target} onChange={(e) => setTarget(e.target.value)} disabled={running}>
                      <option value="test">Test server (safe)</option>
                      <option value="prod">Production (real submission)</option>
                    </select>
                    <label className="checkbox-label" style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
                      <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} disabled={running} />
                      <span>Dry run (build &amp; validate submission.xml; upload nothing)</span>
                    </label>
                    {target === "prod" && !dryRun && <div className="alert-banner" style={{ marginTop: 8 }}><strong>⚠ Production submission.</strong> This uploads to NCBI for real.</div>}
                  </div>
                )}
              </div>
              <div className="form-section">
                <label className="checkbox-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input type="checkbox" checked={noNcbiCheck} onChange={(e) => setNoNcbiCheck(e.target.checked)} disabled={running} />
                  <span>Skip NCBI existence check (faster; no dedup against records already in NCBI)</span>
                </label>
              </div>
              <button className="run-btn" onClick={runPipeline} disabled={running || !activeProject}>
                {running ? "Running…" : mode === "submit" ? `▶ Build & ${dryRun ? "validate" : "submit"} (${archive})` : `▶ Prepare files (${archive})`}
              </button>
            </section>

            <section className="panel">
              <div className="panel-header"><h2>Current run</h2>{jobId && <span className="muted" style={{ fontSize: 12 }}>job {jobId.slice(0, 8)}</span>}</div>
              {activeRun ? (
                <div className="selection-box">
                  <div className="sel-title">{jobStatus === "running" ? "Running" : jobStatus === "succeeded" ? "Done" : jobStatus}</div>
                  <div><span className="sel-name">{activeRun.run_id}</span></div>
                  <div style={{ marginTop: 2 }}><span className="muted">Project:</span> <strong>{activeRun.project}</strong></div>
                  {currentStep && <div className="muted" style={{ marginTop: 4 }}>{currentStep}</div>}
                  <div className="note" style={{ marginTop: 8 }}>Outputs appear in the Results section below when finished.</div>
                </div>
              ) : <div className="empty-msg">No active run. Configure and click the run button.</div>}
            </section>
          </div>
        )}

        {/* ── Results ── */}
        <div className="row-header">
          <h2>Results</h2>
          <button className="ghost" onClick={() => setShowResults(!showResults)}>{showResults ? "Hide" : "Show"}</button>
        </div>
        {showResults && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              {!runResults ? <div className="empty-msg">Run a preparation/submission, or click a past run in the Projects tree.</div>
                : runResults.loading ? <div className="loading-text">Loading results…</div>
                : !runResults.present || (runResults.files || []).length === 0 ? <div className="empty-msg">No result files yet.</div> : (
                  <>
                    <div className="panel-header"><h2>{activeRun?.run_id || runResults.run_id}</h2>
                      <button className="ghost action" onClick={() => loadRunResults(runResults.project, runResults.run_id)}>↻ Refresh</button></div>
                    <div className="results-list">
                      {runResults.files.map((f) => {
                        const base = `./api/projects/${encodeURIComponent(runResults.project)}/file?path=${encodeURIComponent(f.path)}`;
                        return (
                          <div key={f.name} className="results-item">
                            <span className="result-icon">{fileIcon(f.name)}</span>
                            <a className="result-name result-link" href={`${base}&inline=${f.openable ? 1 : 0}`} target={f.openable ? "_blank" : undefined} rel="noopener noreferrer" title={f.name}>{f.label || f.name}</a>
                            <span className="result-size">{fmtSize(f.size)}</span>
                            <a className="result-download" href={`${base}&inline=0`} title={`Download ${f.name}`}>⬇</a>
                          </div>
                        );
                      })}
                    </div>
                  </>
                )}
            </section>
          </div>
        )}

        {/* ── Log ── */}
        <div className="row-header">
          <h2>Pipeline Log</h2>
          <button className="ghost" onClick={() => setShowLogs(!showLogs)}>{showLogs ? "Hide" : "Show"}</button>
        </div>
        {showLogs && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              <div className="log-meta">
                <span className="dot" data-state={jobStatus} />
                <span style={{ fontWeight: 600 }}>{{ idle: "Idle", running: "Running", succeeded: "Done", failed: "Failed" }[jobStatus]}</span>
                {jobStatus === "running" && currentStep && <span className="log-step" title={currentStep}>— {currentStep}</span>}
              </div>
              <div className="log" ref={logRef}>
                {logLines.length === 0 ? <span className="log-placeholder">{jobStatus === "idle" ? "Configure a run and click the run button." : "Waiting for output…"}</span>
                  : logLines.map((line, i) => <div key={i} className={logLineClass(line)}>{line}</div>)}
              </div>
            </section>
          </div>
        )}
      </main>

      {folderBrowser.open && (
        <div onClick={() => setFolderBrowser((s) => ({ ...s, open: false }))} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}>
          <div onClick={(e) => e.stopPropagation()} style={{ background: "var(--panel, #fff)", borderRadius: 10, width: "min(640px, 92vw)", maxHeight: "80vh", display: "flex", flexDirection: "column", boxShadow: "0 10px 40px rgba(0,0,0,0.3)" }}>
            <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border, #ddd)", fontWeight: 700 }}>Select a projects root</div>
            <div style={{ padding: "10px 16px", display: "flex", gap: 6, alignItems: "center" }}>
              <button type="button" className="ghost" disabled={!folderBrowser.parent || folderBrowser.loading} onClick={() => browseDirs(folderBrowser.parent)}>↑ Up</button>
              <input style={{ flex: 1 }} value={folderBrowser.path} onChange={(e) => setFolderBrowser((s) => ({ ...s, path: e.target.value }))} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); browseDirs(folderBrowser.path); } }} />
              <button type="button" className="ghost" onClick={() => browseDirs(folderBrowser.path)}>Go</button>
            </div>
            <div style={{ flex: 1, overflow: "auto", padding: "0 16px", minHeight: 160 }}>
              {folderBrowser.loading ? <div className="note" style={{ padding: 12 }}>Loading…</div>
                : folderBrowser.error ? <div className="note" style={{ padding: 12, color: "var(--danger, #c00)" }}>{folderBrowser.error}</div>
                : folderBrowser.entries.length === 0 ? <div className="note" style={{ padding: 12 }}>No sub-folders here.</div>
                : folderBrowser.entries.map((e) => (
                  <div key={e.path} onClick={() => browseDirs(e.path)} style={{ padding: "7px 8px", cursor: "pointer", borderRadius: 6, display: "flex", gap: 8, alignItems: "center" }}
                    onMouseEnter={(ev) => (ev.currentTarget.style.background = "var(--panel-2, #f0f0f0)")} onMouseLeave={(ev) => (ev.currentTarget.style.background = "transparent")}>
                    <span>📁</span><span>{e.name}</span>
                  </div>
                ))}
            </div>
            <div style={{ padding: "12px 16px", borderTop: "1px solid var(--border, #ddd)", display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button type="button" className="ghost" onClick={() => setFolderBrowser((s) => ({ ...s, open: false }))}>Cancel</button>
              <button type="button" onClick={() => { setSettingsDraft((d) => ({ ...d, projects_root: folderBrowser.path })); setFolderBrowser((s) => ({ ...s, open: false })); }} disabled={folderBrowser.loading || !folderBrowser.path}>Select this folder</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
