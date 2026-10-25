/**
 * FAA Approach Plate 4-Color Tactical Enhancer
 * Desktop Client Logic (desktop_ui/app.js)
 */

let currentPlatePath = null;
let currentChart = null;
let currentChartsList = [];
let currentPlateData = null;
let zoomLevel = 1.0;
let currentViewMode = "enhanced";
let searchDebounceTimer = null;
let dynonFormat = "png";
let dynonMode = "enhanced";
let dynonNavList = [];
let isPipelinePaused = false;
let isPipelineRunning = false;

// Wait for PyWebView Native Bridge
window.addEventListener("pywebviewready", async () => {
  try {
    const initData = await window.pywebview.api.init_app();
    onAppInitialized(initData);
  } catch (err) {
    console.error("Failed to initialize desktop API:", err);
  }
});

function onAppInitialized(data) {
  // Update cycle badge
  const cycleBadge = document.getElementById("cycle-badge");
  if (data.cycle) {
    cycleBadge.textContent = `Cycle ${data.cycle}`;
    cycleBadge.title = `Valid: ${data.from_edate} to ${data.to_edate}`;
  }

  // Set default directory paths in Dynon tab
  const faaInput = document.getElementById("path-faa-dir");
  const outInput = document.getElementById("path-out-dir");
  const navInput = document.getElementById("path-nav-dir");
  if (faaInput) { faaInput.value = data.faa_dir || ""; faaInput.title = data.faa_dir || ""; }
  if (outInput) { outInput.value = data.output_dir || ""; outInput.title = data.output_dir || ""; }
  if (navInput) { navInput.value = data.dynon_nav_dir || ""; navInput.title = data.dynon_nav_dir || ""; }

  // Populate FAA Editions dropdown
  const cycleSelect = document.getElementById("dynon-cycle-select");
  cycleSelect.innerHTML = "";
  if (data.available_editions && data.available_editions.length > 0) {
    data.available_editions.forEach((ed, idx) => {
      const opt = document.createElement("option");
      opt.value = ed.edition;
      opt.textContent = ed.label;
      if (idx === 0) opt.selected = true;
      cycleSelect.appendChild(opt);
    });
  }
  const customOpt = document.createElement("option");
  customOpt.value = "custom";
  customOpt.textContent = "Custom Edition Code...";
  cycleSelect.appendChild(customOpt);

  // Populate Dynon Nav (.duc) Databases dropdown
  dynonNavList = data.dynon_databases || [];
  const navSelect = document.getElementById("dynon-nav-select");
  navSelect.innerHTML = "";
  if (dynonNavList.length > 0) {
    dynonNavList.forEach((db, idx) => {
      const opt = document.createElement("option");
      opt.value = db.url;
      opt.textContent = db.label;
      if (idx === 0) opt.selected = true;
      navSelect.appendChild(opt);
    });
  }

  // Set default airport & charts if available
  if (data.default_airport && data.default_charts) {
    document.getElementById("airport-search-input").value = data.default_airport.ident;
    populatePlateSelect(data.default_charts);
  }
}

// ==========================================================================
// Tabs & Views
// ==========================================================================
function switchTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach(btn => btn.classList.remove("active"));
  document.querySelectorAll(".app-view").forEach(view => view.classList.remove("active"));

  if (tabName === "viewer") {
    document.getElementById("tab-btn-viewer").classList.add("active");
    document.getElementById("view-viewer").classList.add("active");
  } else {
    document.getElementById("tab-btn-dynon").classList.add("active");
    document.getElementById("view-dynon").classList.add("active");
  }
}

// ==========================================================================
// Airport Search & Autocomplete
// ==========================================================================
const searchInput = document.getElementById("airport-search-input");
const resultsDropdown = document.getElementById("airport-results");

searchInput.addEventListener("input", (e) => {
  clearTimeout(searchDebounceTimer);
  const q = e.target.value.trim();
  if (q.length < 2) {
    resultsDropdown.classList.add("hidden");
    return;
  }

  searchDebounceTimer = setTimeout(async () => {
    try {
      const matches = await window.pywebview.api.search_airports(q);
      renderSearchResults(matches);
    } catch (err) {
      console.error("Airport search failed:", err);
    }
  }, 200);
});

function renderSearchResults(airports) {
  resultsDropdown.innerHTML = "";
  if (!airports || airports.length === 0) {
    resultsDropdown.classList.add("hidden");
    return;
  }

  airports.forEach(a => {
    const item = document.createElement("div");
    item.className = "ac-item";
    item.innerHTML = `
      <span class="ac-item-ident">${a.ident}</span>
      <span class="ac-item-name">${a.city}, ${a.state} (${a.iac_count} IAPs)</span>
    `;
    item.onclick = () => selectAirport(a);
    resultsDropdown.appendChild(item);
  });

  resultsDropdown.classList.remove("hidden");
}

document.addEventListener("click", (e) => {
  if (!searchInput.contains(e.target) && !resultsDropdown.contains(e.target)) {
    resultsDropdown.classList.add("hidden");
  }
});

async function selectAirport(airport) {
  searchInput.value = airport.ident;
  resultsDropdown.classList.add("hidden");

  try {
    const charts = await window.pywebview.api.get_airport_charts(airport.id);
    populatePlateSelect(charts);
  } catch (err) {
    console.error("Failed to load charts:", err);
  }
}

function populatePlateSelect(charts) {
  const plateSelect = document.getElementById("plate-select");
  plateSelect.innerHTML = "";
  currentChartsList = charts || [];

  if (!charts || charts.length === 0) {
    const opt = document.createElement("option");
    opt.textContent = "No approach plates found";
    plateSelect.appendChild(opt);
    currentPlatePath = null;
    currentChart = null;
    return;
  }

  // Separate approach plates from standard documents
  const approachCharts = charts.filter(c => c.is_approach || c.chart_code === "IAP" || c.chart_code === "IAC");
  const otherCharts = charts.filter(c => !(c.is_approach || c.chart_code === "IAP" || c.chart_code === "IAC"));

  if (approachCharts.length > 0) {
    const grp = document.createElement("optgroup");
    grp.label = "Approach Plates (Enhanced 4-Color Briefing)";
    approachCharts.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c.pdf_path;
      opt.textContent = c.label;
      grp.appendChild(opt);
    });
    plateSelect.appendChild(grp);
  }

  if (otherCharts.length > 0) {
    const grp = document.createElement("optgroup");
    grp.label = "Standard Documents (Diagrams, Minimums, SIDs, STARs)";
    otherCharts.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c.pdf_path;
      opt.textContent = c.label;
      grp.appendChild(opt);
    });
    plateSelect.appendChild(grp);
  }

  // Default selection to first approach plate if present, else first chart
  const defaultChart = approachCharts.length > 0 ? approachCharts[0] : charts[0];
  plateSelect.value = defaultChart.pdf_path;
  currentPlatePath = defaultChart.pdf_path;
  currentChart = defaultChart;

  plateSelect.onchange = () => {
    currentPlatePath = plateSelect.value;
    currentChart = currentChartsList.find(c => c.pdf_path === currentPlatePath) || null;
    renderCurrentPlate();
  };

  renderCurrentPlate();
}

// ==========================================================================
// Tactical Plate Rendering
// ==========================================================================
function getEnabledColors() {
  const colors = [];
  if (document.getElementById("color-cyan").checked) colors.push("CYAN");
  if (document.getElementById("color-magenta").checked) colors.push("MAGENTA");
  if (document.getElementById("color-amber").checked) colors.push("AMBER");
  if (document.getElementById("color-red").checked) colors.push("RED");
  return colors;
}

function onLayerToggle() {
  if (currentViewMode === "original") return;
  renderCurrentPlate();
}

function updateLayerControlState() {
  const colorToggles = document.getElementById("color-toggles");
  const typeBadge = document.getElementById("plate-type-badge");

  if (!currentPlateData) return;

  const isApproach = currentPlateData.is_approach !== false;

  if (currentViewMode === "original") {
    if (colorToggles) colorToggles.classList.add("disabled-controls");
    if (typeBadge) {
      typeBadge.textContent = "Official FAA Chart (Base)";
      typeBadge.classList.remove("hidden");
    }
  } else if (!isApproach) {
    if (colorToggles) colorToggles.classList.add("disabled-controls");
    if (typeBadge) {
      typeBadge.textContent = "Standard Document (No overlay)";
      typeBadge.classList.remove("hidden");
    }
  } else {
    if (colorToggles) colorToggles.classList.remove("disabled-controls");
    if (typeBadge) typeBadge.classList.add("hidden");
  }
}

async function renderCurrentPlate(forcedMode) {
  if (!currentPlatePath) return;

  const mode = forcedMode || currentViewMode;

  const overlay = document.getElementById("loading-overlay");
  const loadingText = overlay ? overlay.querySelector(".loading-text") : null;
  if (loadingText) {
    loadingText.textContent = (mode === "original")
      ? "Loading Official Chart..."
      : "Enhancing Approach Plate...";
  }
  if (overlay) overlay.classList.remove("hidden");

  const colors = getEnabledColors();
  const chartCode = currentChart ? currentChart.chart_code : null;

  try {
    const result = await window.pywebview.api.render_plate(
      currentPlatePath,
      colors,
      ["A", "B"],
      ["LPV", "LNAV/VNAV"],
      1.0,
      chartCode,
      mode
    );

    if (result.success) {
      currentPlateData = result;
      updateImageView();
      updateLayerControlState();

      // Update 5 A's Briefing drawer
      const briefingContent = document.getElementById("briefing-content");
      if (result.briefing) {
        briefingContent.innerHTML = formatMarkdown(result.briefing);
      }
    } else {
      console.error("Error rendering plate:", result.error);
    }
  } catch (err) {
    console.error("Render API call failed:", err);
  } finally {
    if (overlay) overlay.classList.add("hidden");
  }
}

function updateImageView() {
  if (!currentPlateData) return;

  const singleContainer = document.getElementById("single-canvas-container");
  const splitContainer = document.getElementById("split-canvas-container");
  const mainImg = document.getElementById("main-plate-img");
  const origImg = document.getElementById("split-orig-img");
  const enhImg = document.getElementById("split-enh-img");

  if (currentViewMode === "split") {
    singleContainer.classList.add("hidden");
    singleContainer.style.display = "none";
    splitContainer.classList.remove("hidden");
    splitContainer.style.display = "flex";
    if (currentPlateData.original_image) {
      origImg.src = currentPlateData.original_image;
    }
    if (currentPlateData.enhanced_image) {
      enhImg.src = currentPlateData.enhanced_image;
    }
  } else {
    splitContainer.classList.add("hidden");
    splitContainer.style.display = "none";
    singleContainer.classList.remove("hidden");
    singleContainer.style.display = "flex";
    const src = (currentViewMode === "original")
      ? currentPlateData.original_image
      : (currentPlateData.enhanced_image || currentPlateData.original_image);
    if (src) {
      mainImg.src = src;
    }
  }
  applyZoom();
}

async function setViewMode(mode) {
  currentViewMode = mode;
  document.querySelectorAll(".segmented-control .seg-btn").forEach(btn => {
    btn.classList.toggle("active", btn.id === `view-mode-${mode}`);
  });

  // If switching to enhanced or split and enhanced_image is not yet loaded, load it on demand
  if ((mode === "enhanced" || mode === "split") && (!currentPlateData || !currentPlateData.enhanced_image)) {
    await renderCurrentPlate(mode);
    return;
  }

  updateImageView();
  updateLayerControlState();
}

// Zoom & Pan
function zoomPlate(delta) {
  zoomLevel = Math.max(0.5, Math.min(3.0, zoomLevel + delta));
  applyZoom();
}

function resetPlateZoom() {
  zoomLevel = 1.0;
  applyZoom();
}

function applyZoom() {
  const single = document.getElementById("single-canvas-container");
  const split = document.getElementById("split-canvas-container");
  if (single) single.style.transform = `scale(${zoomLevel})`;
  if (split) split.style.transform = `scale(${zoomLevel})`;
}

// 5 A's Briefing Drawer Toggle
function toggleBriefingDrawer() {
  const drawer = document.getElementById("briefing-drawer");
  const arrow = document.getElementById("drawer-arrow");
  drawer.classList.toggle("collapsed");
  arrow.textContent = drawer.classList.contains("collapsed") ? "▲" : "▼";
}

function formatMarkdown(md) {
  return md
    .replace(/^### (.*$)/gim, '<h4 style="color:#00d7f5; margin: 8px 0 4px;">$1</h4>')
    .replace(/^## (.*$)/gim, '<h3 style="color:#f1f5f9; margin: 10px 0 6px;">$1</h3>')
    .replace(/^\* (.*$)/gim, '• $1<br>')
    .replace(/\*\*(.*?)\*\*/gim, '<strong>$1</strong>')
    .replace(/\n/gim, '<br>');
}

// Export Current Plate
async function exportCurrentPlate(format) {
  if (!currentPlatePath) return;
  const colors = getEnabledColors();
  const chartCode = currentChart ? currentChart.chart_code : null;
  try {
    const res = await window.pywebview.api.save_plate_file(
      currentPlatePath,
      format,
      colors,
      ["A", "B"],
      ["LPV", "LNAV/VNAV"],
      1.0,
      chartCode
    );
    if (res.success) {
      const btn = document.getElementById(format === "png" ? "btn-save-png" : "btn-save-pdf");
      if (btn) {
        const origText = btn.innerHTML;
        btn.innerHTML = "✅ Saved";
        setTimeout(() => { btn.innerHTML = origText; }, 2000);
      }
    }
  } catch (err) {
    console.error("Export failed:", err);
  }
}

// ==========================================================================
// VIEW 2: Dynon Export & Updates
// ==========================================================================
function onCycleSelectChange() {
  const select = document.getElementById("dynon-cycle-select");
  const customGroup = document.getElementById("custom-edition-group");
  if (select.value === "custom") {
    customGroup.style.display = "block";
  } else {
    customGroup.style.display = "none";
  }
}

function setDynonFormat(fmt) {
  dynonFormat = fmt;
  document.getElementById("fmt-png").classList.toggle("active", fmt === "png");
  document.getElementById("fmt-pdf").classList.toggle("active", fmt === "pdf");
}

function setDynonMode(mode) {
  dynonMode = mode;
  document.getElementById("mode-enhanced").classList.toggle("active", mode === "enhanced");
  document.getElementById("mode-standard").classList.toggle("active", mode === "standard");
}

async function browseFolder(type) {
  let inputId = "path-out-dir";
  if (type === "faa") inputId = "path-faa-dir";
  else if (type === "nav") inputId = "path-nav-dir";
  const input = document.getElementById(inputId);
  try {
    const res = await window.pywebview.api.select_directory(input.value);
    if (!res.cancelled && res.path) {
      input.value = res.path;
      input.title = res.path;
    }
  } catch (err) {
    console.error("Browse folder failed:", err);
  }
}

async function downloadDynonNavOnly() {
  const navSelect = document.getElementById("dynon-nav-select");
  const navUrl = navSelect.value;
  if (!navUrl) {
    return;
  }
  const matched = dynonNavList.find(db => db.url === navUrl);
  const filename = matched ? matched.filename : navUrl.split("/").pop();
  const navDir = document.getElementById("path-nav-dir").value;

  const progressCard = document.getElementById("progress-card");
  progressCard.classList.remove("hidden");
  document.getElementById("prog-status").textContent = `Downloading ${filename}...`;
  document.getElementById("prog-fill").style.width = "0%";

  try {
    await window.pywebview.api.download_dynon_nav_database(navUrl, navDir, filename);
  } catch (err) {
    console.error("Nav download failed:", err);
  }
}

function setPipelineActionButtonsEnabled(enabled) {
  const btnAll = document.getElementById("btn-run-all");
  const btnDl = document.getElementById("btn-run-dl");
  const btnGen = document.getElementById("btn-run-gen");
  if (btnAll) btnAll.disabled = !enabled;
  if (btnDl) btnDl.disabled = !enabled;
  if (btnGen) btnGen.disabled = !enabled;
}

async function togglePausePipeline() {
  const pauseBtn = document.getElementById("btn-pipeline-pause");
  if (!isPipelinePaused) {
    try {
      const res = await window.pywebview.api.pause_pipeline();
      if (res.success) {
        isPipelinePaused = true;
        if (pauseBtn) pauseBtn.textContent = "▶️ Resume";
        document.getElementById("prog-status").textContent = "⏸️ [PAUSED] Pipeline paused by user";
      }
    } catch (err) {
      console.error("Pause failed:", err);
    }
  } else {
    try {
      const res = await window.pywebview.api.resume_pipeline();
      if (res.success) {
        isPipelinePaused = false;
        if (pauseBtn) pauseBtn.textContent = "⏸️ Pause";
        document.getElementById("prog-status").textContent = "▶️ Resuming pipeline...";
      }
    } catch (err) {
      console.error("Resume failed:", err);
    }
  }
}

async function stopPipeline() {
  const stopBtn = document.getElementById("btn-pipeline-stop");
  if (stopBtn) stopBtn.disabled = true;
  document.getElementById("prog-status").textContent = "🛑 [STOPPING] Cancelling pipeline...";
  try {
    await window.pywebview.api.stop_pipeline();
  } catch (err) {
    console.error("Stop failed:", err);
  }
}

async function triggerPipeline(action) {
  const cycleSelect = document.getElementById("dynon-cycle-select");
  let edition = cycleSelect.value;
  if (edition === "custom") {
    edition = document.getElementById("custom-edition-input").value.trim() || "261029";
  }

  const faaDir = document.getElementById("path-faa-dir").value;
  const outDir = document.getElementById("path-out-dir").value;

  const includeNav = document.getElementById("chk-include-nav").checked;
  const navSelect = document.getElementById("dynon-nav-select");
  const navUrl = navSelect.value;
  const matchedNav = dynonNavList.find(db => db.url === navUrl);
  const navFilename = matchedNav ? matchedNav.filename : (navUrl ? navUrl.split("/").pop() : "");
  const navDir = document.getElementById("path-nav-dir").value;

  const progressCard = document.getElementById("progress-card");
  progressCard.classList.remove("hidden");
  document.getElementById("prog-status").textContent = "Starting pipeline...";
  document.getElementById("prog-status").style.color = "#00d7f5";
  document.getElementById("prog-fill").style.width = "0%";

  isPipelineRunning = true;
  isPipelinePaused = false;
  setPipelineActionButtonsEnabled(false);

  const pauseBtn = document.getElementById("btn-pipeline-pause");
  const stopBtn = document.getElementById("btn-pipeline-stop");
  if (pauseBtn) {
    pauseBtn.textContent = "⏸️ Pause";
    pauseBtn.disabled = false;
  }
  if (stopBtn) {
    stopBtn.disabled = false;
  }

  try {
    await window.pywebview.api.run_pipeline(
      action,
      edition,
      faaDir,
      outDir,
      dynonFormat,
      dynonMode,
      200,
      0, // 0 = auto-detect optimal M5 Pro cores
      true,
      includeNav,
      navUrl,
      navDir,
      navFilename
    );
  } catch (err) {
    console.error("Pipeline trigger error:", err);
    setPipelineActionButtonsEnabled(true);
    isPipelineRunning = false;
  }
}

// Real-Time Callback from Python to JS
window.__onPipelineProgress = function(p) {
  const statusEl = document.getElementById("prog-status");
  const fillEl = document.getElementById("prog-fill");
  const etaEl = document.getElementById("prog-eta");
  const mStage = document.getElementById("m-stage");
  const mSpeed = document.getElementById("m-speed");
  const mVol = document.getElementById("m-vol");
  const pauseBtn = document.getElementById("btn-pipeline-pause");
  const stopBtn = document.getElementById("btn-pipeline-stop");

  if (p.is_stopped || p.stage === "stopped") {
    isPipelineRunning = false;
    isPipelinePaused = false;
    setPipelineActionButtonsEnabled(true);
    statusEl.textContent = p.status || "🛑 Pipeline stopped by user";
    statusEl.style.color = "#f53232";
    etaEl.textContent = "Stopped";
    if (pauseBtn) pauseBtn.disabled = true;
    if (stopBtn) stopBtn.disabled = true;
    return;
  }

  if (p.stage === "error") {
    isPipelineRunning = false;
    isPipelinePaused = false;
    setPipelineActionButtonsEnabled(true);
    statusEl.textContent = `Error: ${p.error}`;
    statusEl.style.color = "#f53232";
    if (pauseBtn) pauseBtn.disabled = true;
    if (stopBtn) stopBtn.disabled = true;
    return;
  }

  if (p.stage === "complete") {
    isPipelineRunning = false;
    isPipelinePaused = false;
    setPipelineActionButtonsEnabled(true);
    fillEl.style.width = "100%";
    statusEl.textContent = p.status || "Completed Successfully!";
    statusEl.style.color = "#10b981";
    etaEl.textContent = "Done";
    if (pauseBtn) pauseBtn.disabled = true;
    if (stopBtn) stopBtn.disabled = true;
    if (p.summary && p.summary.saved_file) {
      statusEl.textContent = `Saved: ${p.summary.saved_file.split("/").pop()}`;
    } else if (p.summary && p.summary.output_dir) {
      statusEl.textContent = `Completed! Saved to: ${p.summary.output_dir}`;
    }
    return;
  }

  statusEl.textContent = p.status || "";
  statusEl.style.color = "#00d7f5";
  const pct = Math.round((p.progress || 0) * 100);
  fillEl.style.width = `${pct}%`;

  if (p.eta) etaEl.textContent = `ETA: ${p.eta}`;
  if (p.stage) mStage.textContent = p.stage.toUpperCase();
  if (p.speed) mSpeed.textContent = p.speed;
  if (p.volume) mVol.textContent = p.volume;
};
