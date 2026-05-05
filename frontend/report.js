/* ═══════════════════════════════════════════════════════════════════════════
   AI SQL Analyst — Enterprise Report Viewer (New Tab)
   Renders filter bar, KPIs, charts (Chart.js), data tables, insights.
   Features: streaming reveal, interactive charts, compact layout.
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    // ── Extract report ID from URL ────────────────────────────────────────
    const params = new URLSearchParams(window.location.search);
    const reportId = params.get("id");

    if (!reportId) {
        document.getElementById("reportContent").innerHTML =
            '<div class="report-loading"><div class="report-loading-text">No report ID provided.</div></div>';
        return;
    }

    // ── Retrieve data from sessionStorage ─────────────────────────────────
    const raw = sessionStorage.getItem(reportId);
    if (!raw) {
        document.getElementById("reportContent").innerHTML =
            '<div class="report-loading"><div class="report-loading-text">Report data not found. Please generate the report again from the chat.</div></div>';
        return;
    }

    const reportData = JSON.parse(raw);
    const question = sessionStorage.getItem(reportId + "_question") || "Report";
    const theme = sessionStorage.getItem(reportId + "_theme") || "light";

    // Apply the theme from the parent page
    document.documentElement.setAttribute("data-theme", theme);

    let currentReport = reportData.report;
    const applicableFilters = reportData.applicable_filters || {};

    function _persistReport() {
        try {
            const saved = JSON.parse(sessionStorage.getItem(reportId) || "{}");
            saved.report = currentReport;
            sessionStorage.setItem(reportId, JSON.stringify(saved));
        } catch (_) {}
        // Broadcast to main chat tab so @ mentions update
        try {
            const bc = new BroadcastChannel('report_sync');
            bc.postMessage({ type: 'report_updated', reportId, report: currentReport });
            bc.close();
        } catch (_) {}
    }

    // ── Per-Chart Filter Store ────────────────────────────────────────────
    // Stores chart instances + original data for client-side filtering.
    // Reset on every renderReport() call.
    let chartStore = [];  // [{instance, originalData, spec, idx}]

    // ── Color Palettes ────────────────────────────────────────────────────
    const PALETTES = {
        blues:   ["#3b82f6","#2563eb","#1d4ed8","#60a5fa","#93c5fd","#1e40af"],
        greens:  ["#10b981","#059669","#047857","#34d399","#6ee7b7","#065f46"],
        purples: ["#8b5cf6","#7c3aed","#6d28d9","#a78bfa","#c4b5fd","#5b21b6"],
        oranges: ["#f59e0b","#d97706","#b45309","#fbbf24","#fcd34d","#92400e"],
        mixed:   ["#10b981","#3b82f6","#8b5cf6","#f59e0b","#f43f5e","#06b6d4","#6366f1","#ec4899","#14b8a6","#a855f7","#eab308","#ef4444","#22c55e","#0ea5e9","#d946ef"],
        gradient:["#6366f1","#8b5cf6","#a855f7","#c084fc","#d8b4fe","#7c3aed"],
    };
    const DEFAULT_COLORS = PALETTES.mixed;
    const ALPHA = "33";

    function getColors(scheme, count) {
        const pal = PALETTES[scheme] || DEFAULT_COLORS;
        const result = [];
        for (let i = 0; i < count; i++) result.push(pal[i % pal.length]);
        return result;
    }

    function getChartDefaults() {
        const isDark = document.documentElement.getAttribute("data-theme") === "dark";
        return {
            gridColor: isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.05)",
            textColor: isDark ? "#94a3b8" : "#475569",
            bgColor: isDark ? "#161b22" : "#ffffff",
        };
    }

    // Force crisp rendering on Retina / high-DPI screens
    if (typeof Chart !== "undefined") {
        Chart.defaults.devicePixelRatio = window.devicePixelRatio || 2;
    }

    // ── Streaming Render ──────────────────────────────────────────────────
    function streamReveal(container) {
        const sections = container.querySelectorAll(".stream-section");
        sections.forEach((el, i) => {
            setTimeout(() => {
                el.classList.add("visible");
            }, 150 + i * 200);
        });
    }

    // ── Render Full Report ────────────────────────────────────────────────
    function renderReport(report, { skipAnimation = false } = {}) {
        // Destroy existing Chart.js instances before clearing DOM
        chartStore.forEach(entry => {
            if (entry && entry.instance) { try { entry.instance.destroy(); } catch (_) {} }
        });

        if (!report) {
            document.getElementById("reportContent").innerHTML =
                '<div class="report-loading"><div class="report-loading-text">Report generation failed. Try again.</div></div>';
            return;
        }

        const content = document.getElementById("reportContent");
        const title = report.title || "Analytics Report";
        const summary = report.summary || "";

        document.title = title + " — AI SQL Analyst";
        document.getElementById("reportTitle").textContent = title;

        let html = "";

        // ── Dynamic Filter Bar (only shows applicable filters) ─────────
        const hasAnyFilter = Object.keys(applicableFilters).length > 0;
        if (hasAnyFilter) {
            html += `<div class="report-filter-bar stream-section" id="filterBar">`;

            if (applicableFilters.date_range) {
                html += `<div class="filter-group">
                    <span class="filter-label">Date Range</span>
                    <input type="date" class="filter-input" id="filterDateFrom" />
                    <span style="font-size:0.65rem;color:var(--text-muted)">to</span>
                    <input type="date" class="filter-input" id="filterDateTo" />
                </div>
                <div class="filter-divider"></div>`;
            }
            if (applicableFilters.category) {
                html += `<div class="filter-group">
                    <span class="filter-label">Category</span>
                    <select class="filter-select" id="filterCategory"><option value="">All</option></select>
                </div>`;
            }
            if (applicableFilters.status) {
                html += `<div class="filter-group">
                    <span class="filter-label">Status</span>
                    <select class="filter-select" id="filterStatus"><option value="">All</option></select>
                </div>`;
            }
            if (applicableFilters.customer) {
                html += `<div class="filter-group">
                    <span class="filter-label">Customer</span>
                    <select class="filter-select" id="filterCustomer"><option value="">All</option></select>
                </div>`;
            }
            if (applicableFilters.product) {
                html += `<div class="filter-group">
                    <span class="filter-label">Product</span>
                    <select class="filter-select" id="filterProduct"><option value="">All</option></select>
                </div>`;
            }

            html += `<button class="filter-apply-btn" id="filterApplyBtn">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:12px;height:12px;margin-right:4px"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                    Apply Filters
                </button>
                <button class="filter-clear-btn" id="filterClearBtn">Clear Filters</button>
            </div>`;
        }



        // ── Summary ───────────────────────────────────────────────────────
        if (summary) {
            html += `<div class="report-summary stream-section">
                <div class="report-summary-text">${escapeHtml(summary)}</div>
            </div>`;
        }

        // ── KPIs ──────────────────────────────────────────────────────────
        const rawKpis = report.kpis || [];
        // Show all KPIs the backend sends — backend guarantees exactly 6.
        // Error KPIs render as "Error" card; zero values render as "0".
        // Only drop KPIs that are completely missing a value AND have no error.
        const kpis = rawKpis.filter(k => {
            if (k._placeholder) return true; // always show empty slots
            if (k.error) return true;  // always show error KPIs (renders "Error" card)
            const v = k.value;
            if (v === null || v === undefined || v === "") return false;
            const s = String(v).trim().toLowerCase();
            if (s === "nan") return false;  // garbage value — drop
            return true;
        });
        if (kpis.length > 0) {
            html += `<div class="report-section-label label-kpi stream-section">
                <div class="report-section-label-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
                </div>
                <span class="report-section-label-text">Key Performance Indicators</span>
            </div>
            <div class="kpi-grid stream-section">`;

            kpis.forEach((kpi, kpiIdx) => {
                if (kpi._placeholder) {
                    html += `<div class="kpi-card placeholder-card" data-ph-idx="${kpiIdx}" data-ph-type="kpi">`
                        + `<div class="ph-inner"><button class="ph-plus-btn" title="Add KPI here">+</button>`
                        + `<span class="ph-label">Add KPI here</span></div>`
                        + `<div class="ph-chat" style="display:none">`
                        + `<textarea class="ph-input" rows="2" placeholder="Describe the KPI you want (e.g. \"average order value\")…"></textarea>`
                        + `<div class="ph-actions"><button class="ph-send-btn">Add</button><button class="ph-cancel-btn">Cancel</button></div>`
                        + `</div></div>`;
                    return;
                }
                const val = formatKPIValue(kpi.value, kpi.format);
                const hasExplanation = kpi.explanation && (kpi.explanation.what || kpi.explanation.how);
                html += `<div class="kpi-card">
                    <div class="kpi-header">
                        <div class="kpi-label">${escapeHtml(kpi.label || kpi.id || "Metric")}</div>
                        ${hasExplanation ? `<button class="kpi-eye-btn" data-explain='${escapeAttr(JSON.stringify(kpi.explanation))}' data-title="${escapeAttr(kpi.label || kpi.id || "Metric")}">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                        </button>` : ""}
                    </div>
                    <div class="kpi-value">${kpi.error ? '<span style="font-size:0.75rem;color:var(--accent-rose)">Error</span>' : val}</div>
                    ${kpi.error ? `<div style="font-size:0.62rem;color:var(--text-muted);margin-top:0.15rem;line-height:1.3">${escapeHtml(kpi.error)}</div>` : ""}
                </div>`;
            });
            html += `</div>`;
        }

        // ── Charts (pre-filter: skip empty, errored, or all-zero charts) ──
        const rawCharts = report.charts || [];
        const charts = rawCharts.filter(c => {
            if (c._placeholder) return true; // always keep empty slots
            if (c.error) return false;
            if (!c.data || c.data.length === 0) return false;
            // Check if all numeric values are zero
            const keys = Object.keys(c.data[0]);
            if (keys.length < 2) return false; // need label + value
            const valueKeys = keys.slice(1);
            const allZero = c.data.every(row => valueKeys.every(k => {
                const v = Number(row[k]);
                return isNaN(v) || v === 0;
            }));
            if (allZero) return false;
            return true;
        });
        if (charts.length > 0) {
            html += `<div class="report-section-label label-charts stream-section">
                <div class="report-section-label-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                </div>
                <span class="report-section-label-text">Visual Analytics</span>
            </div>
            <div class="charts-grid stream-section">`;

            // Pre-compute which charts are naturally wide (line/area/stackedBar)
            const naturallyWide = charts.map(c =>
                !c._placeholder && ["line", "area", "stackedbar"].includes((c.type || "bar").toLowerCase())
            );

            // Simulate 2-col grid placement to find orphaned half-width charts.
            // A half-width chart is orphaned when it is alone in its grid row
            // (because the next chart is wide, or it is the last chart).
            // Promote orphaned half-width charts to full-width to eliminate gaps.
            const shouldBeWide = [...naturallyWide];
            let col = 0; // current column cursor (0 = left, 1 = right)
            for (let i = 0; i < charts.length; i++) {
                if (shouldBeWide[i]) {
                    col = 0; // wide chart consumes full row, next starts at col 0
                } else {
                    if (col === 0) {
                        // This chart is in the left column.
                        // Look ahead: if the next chart is wide (or there is no next),
                        // this chart would sit alone — promote it to full-width.
                        const nextWide = (i + 1 >= charts.length) || shouldBeWide[i + 1];
                        if (nextWide) {
                            shouldBeWide[i] = true;
                            col = 0; // still starts fresh after a full-width
                        } else {
                            col = 1; // this takes col 0, next takes col 1
                        }
                    } else {
                        col = 0; // paired with previous, next row starts at col 0
                    }
                }
            }

            charts.forEach((chart, idx) => {
                const isWide = shouldBeWide[idx];
                if (chart._placeholder) {
                    html += `<div class="chart-card placeholder-card${isWide ? " chart-full-width" : ""}" data-ph-idx="${idx}" data-ph-type="chart" data-chart-idx="${idx}">`
                        + `<div class="ph-inner"><button class="ph-plus-btn" title="Add chart here">+</button>`
                        + `<span class="ph-label">Add chart here</span></div>`
                        + `<div class="ph-chat" style="display:none">`
                        + `<textarea class="ph-input" rows="2" placeholder="Describe the chart you want\u2026"></textarea>`
                        + `<div class="ph-actions"><button class="ph-send-btn">Add</button><button class="ph-cancel-btn">Cancel</button></div>`
                        + `</div></div>`;
                    return;
                }
                const hasExplanation = chart.explanation && (chart.explanation.what || chart.explanation.how);
                html += `<div class="chart-card${isWide ? " chart-full-width" : ""}" data-chart-idx="${idx}">
                    <div class="chart-header">
                        <span class="chart-title">${escapeHtml(chart.title || "Chart " + (idx + 1))}</span>
                        <div class="chart-active-filters" id="chartActiveFilters_${idx}"></div>
                        <div style="display:flex;align-items:center;gap:0.25rem;margin-left:auto">
                            ${hasExplanation ? `<button class="kpi-eye-btn" data-explain='${escapeAttr(JSON.stringify(chart.explanation))}' data-title="${escapeAttr(chart.title || "Chart")}">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                            </button>` : ""}
                            <div class="chart-dl-wrapper" data-chart-dl-idx="${idx}">
                                <button class="chart-dl-btn" title="Download this chart">
                                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                                </button>
                                <div class="chart-dl-dropdown">
                                    <button class="chart-dl-option" data-dl-type="png" data-dl-idx="${idx}"> Download PNG</button>
                                    <button class="chart-dl-option" data-dl-type="pdf" data-dl-idx="${idx}"> Download PDF</button>
                                </div>
                            </div>
                            <button class="chart-filter-toggle" id="chartFilterBtn_${idx}" title="Filter this chart">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>
                                <span class="chart-filter-badge" id="chartFilterBadge_${idx}"></span>
                            </button>
                        </div>
                    </div>
                    <div class="chart-filter-panel" id="chartFilterPanel_${idx}"></div>
                    <div class="chart-body">
                        <canvas id="chart_${idx}"></canvas>
                    </div>
                </div>`;
            });
            html += `</div>`;
        }

        // ── Data Table ────────────────────────────────────────────────────
        const table = report.table;
        if (table && table.data && table.data.length > 0) {
            html += `<div class="report-section-label label-table stream-section">
                <div class="report-section-label-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                </div>
                <span class="report-section-label-text">Data Preview</span>
            </div>
            <div class="report-table-section stream-section">
                <div class="report-table-header">
                    <span class="report-table-title">${escapeHtml(table.title || "Detail Data")}</span>
                    <span class="row-badge">${table.data.length} row${table.data.length !== 1 ? "s" : ""}</span>
                    ${table.explanation ? `<button class="kpi-eye-btn" data-explain='${escapeAttr(JSON.stringify(table.explanation))}' data-title="${escapeAttr(table.title || "Data Table")}">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                    </button>` : ""}
                </div>
                <div class="report-table-body">${buildTable(table.data)}</div>
            </div>`;
        }

        // ── Insights — Rich Cards ─────────────────────────────────────────
        const insights = report.insights || [];
        if (insights.length > 0) {
            html += `<div class="report-section-label label-insights stream-section">
                <div class="report-section-label-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a7 7 0 017 7c0 2.38-1.19 4.47-3 5.74V17a1 1 0 01-1 1H9a1 1 0 01-1-1v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 017-7z"/><line x1="9" y1="21" x2="15" y2="21"/></svg>
                </div>
                <span class="report-section-label-text">AI-Generated Insights</span>
            </div>
            <div class="report-insights stream-section">`;

            insights.forEach(ins => {
                // Support both string and object insights
                if (typeof ins === "string") {
                    html += `<div class="insight-card type-neutral">
                        <div class="insight-body">${escapeHtml(ins)}</div>
                    </div>`;
                } else {
                    const type = ins.type || "neutral";
                    html += `<div class="insight-card type-${type}">
                        <div class="insight-type-badge badge-${type}">${type}</div>
                        ${ins.title ? `<div class="insight-title">${escapeHtml(ins.title)}</div>` : ""}
                        <div class="insight-body">${escapeHtml(ins.body || ins.title || "")}</div>
                    </div>`;
                }
            });
            html += `</div>`;
        }

        content.innerHTML = html;

        // ── Reset chart store ──────────────────────────────────────────────
        chartStore = [];

        // ── Render Charts ─────────────────────────────────────────────────
        charts.forEach((chart, idx) => {
            if (!chart.data || chart.data.length === 0) return;
            const canvas = document.getElementById(`chart_${idx}`);
            if (!canvas) return;
            try {
                const instance = renderChart(canvas, chart);
                // Store for per-chart filtering
                chartStore[idx] = {
                    instance: instance,
                    originalData: JSON.parse(JSON.stringify(chart.data)),
                    spec: chart,
                    idx: idx,
                };
                // Build the filter panel for this chart
                buildChartFilterPanel(idx, chart);
            } catch (err) {
                console.error(`Chart ${idx} render failed:`, err, chart);
                // Hide the chart card entirely instead of showing an error
                const card = canvas.closest(".chart-card");
                if (card) card.style.display = "none";
            }
        });

        // ── Wire per-chart filter toggle buttons ──────────────────────────
        charts.forEach((chart, idx) => {
            const toggleBtn = document.getElementById(`chartFilterBtn_${idx}`);
            const panel = document.getElementById(`chartFilterPanel_${idx}`);
            if (toggleBtn && panel) {
                toggleBtn.addEventListener("click", (e) => {
                    e.stopPropagation();
                    const isOpen = panel.classList.contains("open");
                    // Close all other panels first
                    document.querySelectorAll(".chart-filter-panel.open").forEach(p => {
                        if (p !== panel) p.classList.remove("open");
                    });
                    document.querySelectorAll(".chart-filter-toggle.active").forEach(b => {
                        if (b !== toggleBtn) b.classList.remove("active");
                    });
                    panel.classList.toggle("open", !isOpen);
                    toggleBtn.classList.toggle("active", !isOpen);
                });
            }
        });

        // ── Wire eye buttons ──────────────────────────────────────────────
        content.querySelectorAll(".kpi-eye-btn").forEach(btn => {
            btn.addEventListener("click", () => {
                const explanation = JSON.parse(btn.dataset.explain);
                const title = btn.dataset.title || "Explanation";
                showExplanationModal(title, explanation);
            });
        });

        // ── Wire placeholder + buttons ─────────────────────────────────
        _wirePlaceholders(content);

        // ── Wire per-chart download buttons ───────────────────────────
        _wireChartDownloads(content);

        // ── Streaming reveal (skip for instant edit-mode re-renders) ─────
        if (skipAnimation) {
            content.querySelectorAll(".stream-section").forEach(el => el.classList.add("visible"));
        } else {
            streamReveal(content);
        }

        // ── Wire filter apply button ──────────────────────────────────────
        const applyBtn = document.getElementById("filterApplyBtn");
        if (applyBtn) {
            applyBtn.addEventListener("click", handleFilterApply);
        }

        // ── Wire clear filters button ─────────────────────────────────────
        const clearBtn = document.getElementById("filterClearBtn");
        if (clearBtn) {
            clearBtn.addEventListener("click", handleClearFilters);
        }

        // ── Re-populate filter dropdowns from cache after every re-render ──
        // renderReport rebuilds the DOM so selects are fresh — refill them.
        _applyFilterOptionsCache();
    }

    // ── Load Filter Options ───────────────────────────────────────────────
    // Cache the API response so re-renders can repopulate without re-fetching.
    let _filterOptionsCache = null;

    function _applyFilterOptionsCache() {
        if (!_filterOptionsCache) return;
        const d = _filterOptionsCache;
        populateSelect("filterCategory", d.categories || []);
        populateSelect("filterCustomer", d.customers || []);
        populateSelect("filterProduct", d.products || []);
        populateSelect("filterStatus", d.statuses || []);
        if (d.date_range) {
            const fromEl = document.getElementById("filterDateFrom");
            const toEl   = document.getElementById("filterDateTo");
            if (fromEl && !fromEl.value && d.date_range.min_date)
                fromEl.value = d.date_range.min_date.split("T")[0];
            if (toEl && !toEl.value && d.date_range.max_date)
                toEl.value = d.date_range.max_date.split("T")[0];
        }
    }

    async function loadFilterOptions() {
        try {
            const res = await fetch("/report/filters");
            if (!res.ok) return;
            _filterOptionsCache = await res.json();
            _applyFilterOptionsCache();
        } catch (e) {
            console.warn("Failed to load filter options:", e);
        }
    }

    function populateSelect(id, options) {
        const el = document.getElementById(id);
        if (!el) return;
        // Clear all options after the first "All" placeholder to prevent duplicates
        while (el.options.length > 1) el.remove(1);
        options.forEach(opt => {
            const o = document.createElement("option");
            o.value = opt;
            o.textContent = opt;
            el.appendChild(o);
        });
    }

    // ── Handle Filter Apply ───────────────────────────────────────────────
    // Stores the original (unfiltered) report so we can re-apply filters
    // from a clean base each time, avoiding stacking of filter conditions.
    let originalReport = currentReport ? JSON.parse(JSON.stringify(currentReport)) : null;

    async function handleFilterApply() {
        const btn = document.getElementById("filterApplyBtn");
        btn.textContent = "Loading...";
        btn.disabled = true;

        const filters = {
            date_from: document.getElementById("filterDateFrom")?.value || null,
            date_to: document.getElementById("filterDateTo")?.value || null,
            category: document.getElementById("filterCategory")?.value || null,
            customer: document.getElementById("filterCustomer")?.value || null,
            status: document.getElementById("filterStatus")?.value || null,
            product: document.getElementById("filterProduct")?.value || null,
        };

        // Check if any filter is actually set (non-empty)
        const hasActiveFilters = Object.values(filters).some(v => v && v.trim() !== "");

        try {
            let data;

            if (hasActiveFilters && originalReport) { 
                // ── Use server-side SQL injection (fast, no LLM) ──────────
                const res = await fetch("/report/apply-filters", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        report: originalReport,
                        provider: sessionStorage.getItem(reportId + "_provider") || "groq",
                        ...filters,
                    }),
                });

                if (res.ok) {
                    data = await res.json();
                }
            } else if (!hasActiveFilters) {
                // All filters cleared — restore the original report
                data = { report: JSON.parse(JSON.stringify(originalReport)) };
            } else {
                // Fallback: LLM regeneration (only if no original report)
                const res = await fetch("/report", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        question: question,
                        provider: sessionStorage.getItem(reportId + "_provider") || "groq",
                        ...filters,
                    }),
                });

                if (res.ok) {
                    data = await res.json();
                }
            }

            if (data && data.report) {
                currentReport = data.report;
                // Destroy existing charts
                try {
                    Object.values(Chart.instances).forEach(inst => inst.destroy());
                } catch (_) {}

                // Save current filter values before re-render wipes them
                const savedFilters = { ...filters };

                renderReport(currentReport);

                // Restore filter values after re-render
                await loadFilterOptions();
                if (savedFilters.date_from) {
                    const el = document.getElementById("filterDateFrom");
                    if (el) el.value = savedFilters.date_from;
                }
                if (savedFilters.date_to) {
                    const el = document.getElementById("filterDateTo");
                    if (el) el.value = savedFilters.date_to;
                }
                if (savedFilters.category) {
                    const el = document.getElementById("filterCategory");
                    if (el) el.value = savedFilters.category;
                }
                if (savedFilters.status) {
                    const el = document.getElementById("filterStatus");
                    if (el) el.value = savedFilters.status;
                }
                if (savedFilters.customer) {
                    const el = document.getElementById("filterCustomer");
                    if (el) el.value = savedFilters.customer;
                }
                if (savedFilters.product) {
                    const el = document.getElementById("filterProduct");
                    if (el) el.value = savedFilters.product;
                }
            }
        } catch (e) {
            console.error("Filter apply failed:", e);
            // Show error to user
            const content = document.getElementById("reportContent");
            if (content) {
                const errDiv = document.createElement("div");
                errDiv.className = "chart-error";
                errDiv.style.cssText = "margin:1rem;padding:0.75rem;border-radius:0.5rem;";
                errDiv.textContent = "Filter application failed: " + (e.message || "Unknown error");
                content.prepend(errDiv);
                setTimeout(() => errDiv.remove(), 5000);
            }
        }

        btn.textContent = "Apply Filters";
        btn.disabled = false;
    }

    // ── Handle Clear Filters ──────────────────────────────────────────────
    function handleClearFilters() {
        // Reset all filter selects to "All"
        ["filterCategory", "filterStatus", "filterCustomer", "filterProduct"].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.selectedIndex = 0;
        });

        // Reset date inputs
        const fromEl = document.getElementById("filterDateFrom");
        const toEl = document.getElementById("filterDateTo");
        if (fromEl) fromEl.value = "";
        if (toEl) toEl.value = "";

        // Restore original report
        if (originalReport) {
            currentReport = JSON.parse(JSON.stringify(originalReport));
            try {
                Object.values(Chart.instances).forEach(inst => inst.destroy());
            } catch (_) {}
            renderReport(currentReport);
        }

        // Re-load date range defaults
        loadFilterOptions();
    }

    // ── Chart Rendering ───────────────────────────────────────────────────
    function renderChart(canvas, chartSpec) {
        const data = chartSpec.data;
        if (!data || data.length === 0) return;

        const keys = Object.keys(data[0]);
        if (keys.length === 0) return;

        const labelKey = keys[0];
        const valueKeys = keys.length > 1 ? keys.slice(1) : [keys[0]];
        const labels = data.map(row => String(row[labelKey]));
        const defaults = getChartDefaults();
        const colors = getColors(chartSpec.color_scheme, Math.max(data.length, valueKeys.length));

        let chartType = (chartSpec.type || "bar").toLowerCase();
        let isHorizontal = false, isStacked = false, isArea = false;

        // Normalize chart type variants
        if (chartType === "horizontalbar") { chartType = "bar"; isHorizontal = true; }
        else if (chartType === "stackedbar") { chartType = "bar"; isStacked = true; }
        else if (chartType === "area") { chartType = "line"; isArea = true; }
        else if (chartType === "polararea") { chartType = "doughnut"; }
        else if (chartType === "radar") { chartType = "bar"; }
        // Default to bar for any unrecognized type
        else if (!["bar", "line", "pie", "doughnut", "scatter"].includes(chartType)) {
            chartType = "bar";
        }

        const datasets = valueKeys.map((key, i) => {
            const values = data.map(row => {
                const v = row[key];
                return v === null || v === undefined ? 0 : Number(v) || 0;
            });

            const color = colors[i % colors.length];
            const cfg = {
                label: formatColumnName(key),
                data: values,
                backgroundColor: color + ALPHA,
                borderColor: color,
                borderWidth: 2,
            };

            if (chartType === "line") {
                cfg.tension = 0.45;
                // Dynamic point size: hide dots when too many points
                const ptRadius = data.length > 50 ? 0 : data.length > 20 ? 2 : 4;
                const ptHover = data.length > 50 ? 5 : 7;
                cfg.pointRadius = ptRadius;
                cfg.pointHoverRadius = ptHover;
                cfg.pointBackgroundColor = color;
                cfg.pointBorderColor = "#fff";
                cfg.pointBorderWidth = ptRadius > 0 ? 2 : 0;
                cfg.borderWidth = data.length > 50 ? 2 : 2.5;
                if (isArea) {
                    cfg.fill = "origin";
                    cfg.backgroundColor = (ctx) => {
                        if (!ctx.chart.chartArea) return color + "18";
                        const { top, bottom } = ctx.chart.chartArea;
                        const gradient = ctx.chart.ctx.createLinearGradient(0, top, 0, bottom);
                        gradient.addColorStop(0, color + "55");
                        gradient.addColorStop(1, color + "04");
                        return gradient;
                    };
                } else {
                    cfg.backgroundColor = color + "18";
                }
            }

            if (chartType === "bar") {
                // Glass-transparent bar: very light fill, crisp colored border
                cfg.borderRadius = isHorizontal ? 4 : 6;
                cfg.borderSkipped = false;
                cfg.borderWidth = 2;
                cfg.borderColor = color;
                cfg.backgroundColor = color + (isStacked ? "44" : "22");
                cfg.hoverBackgroundColor = color + "55";
                cfg.hoverBorderColor = color;
                cfg.hoverBorderWidth = 2.5;
            }

            if (isPieType(chartType)) {
                // Semi-transparent slices with a clean white/dark separator
                cfg.backgroundColor = colors.slice(0, values.length).map(c => c + "bb");
                cfg.borderColor = defaults.bgColor;
                cfg.borderWidth = 2;
                cfg.hoverBackgroundColor = colors.slice(0, values.length).map(c => c + "ee");
                cfg.hoverOffset = 10;
                cfg.hoverBorderWidth = 0;
            }

            if (chartType === "radar") {
                cfg.fill = true;
                cfg.backgroundColor = color + "33";
                cfg.pointBackgroundColor = color;
                cfg.pointBorderColor = "#fff";
                cfg.pointBorderWidth = 2;
            }

            if (chartType === "polararea") {
                chartType = "polarArea";
                cfg.backgroundColor = colors.slice(0, values.length).map(c => c + "88");
                cfg.borderColor = colors.slice(0, values.length);
                cfg.borderWidth = 2;
            }

            if (chartType === "scatter") {
                cfg.data = data.map(row => ({ x: Number(row[labelKey]) || 0, y: Number(row[key]) || 0 }));
                cfg.pointRadius = 5;
                cfg.pointHoverRadius = 8;
                cfg.pointBackgroundColor = color + "cc";
                cfg.pointBorderColor = color;
                cfg.pointBorderWidth = 2;
            }

            return cfg;
        });

        // Plugin: paint an opaque canvas background so backdrop-filter blur
        // on the chart-card never bleeds through transparent chart elements.
        const _canvasBgPlugin = {
            id: "canvasBackground",
            beforeDraw(chart) {
                const ctx = chart.ctx;
                ctx.save();
                ctx.globalCompositeOperation = "destination-over";
                ctx.fillStyle = defaults.bgColor === "#ffffff"
                    ? "rgba(255,255,255,0.94)"
                    : "rgba(22,27,34,0.94)";
                ctx.fillRect(0, 0, chart.width, chart.height);
                ctx.restore();
            },
        };

        const config = {
            type: chartType,
            data: { labels: chartType === "scatter" ? undefined : labels, datasets },
            plugins: [_canvasBgPlugin],
            options: {
                responsive: true,
                maintainAspectRatio: false,
                devicePixelRatio: Math.ceil(window.devicePixelRatio || 2),
                indexAxis: isHorizontal ? "y" : "x",
                interaction: {
                    intersect: (chartType === "line" || chartType === "area") ? false : true,
                    mode: (chartType === "line" || chartType === "area") ? "index" : "nearest",
                },
                plugins: {
                    legend: {
                        display: valueKeys.length > 1 || isPieType(chartType),
                        position: isPieType(chartType) ? "right" : "top",
                        labels: {
                            color: defaults.textColor,
                            font: { family: "'Inter', sans-serif", size: 11, weight: 500 },
                            padding: 12, usePointStyle: true, pointStyleWidth: 8,
                            boxWidth: 8, boxHeight: 8,
                        },
                    },
                    tooltip: {
                        enabled: true,
                        backgroundColor: defaults.bgColor === "#ffffff" ? "rgba(15,23,42,0.96)" : "rgba(255,255,255,0.97)",
                        titleColor: defaults.bgColor === "#ffffff" ? "#f1f5f9" : "#0f172a",
                        bodyColor: defaults.bgColor === "#ffffff" ? "#e2e8f0" : "#334155",
                        borderColor: defaults.bgColor === "#ffffff" ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)",
                        borderWidth: 1,
                        padding: { top: 10, bottom: 10, left: 14, right: 14 },
                        cornerRadius: 10,
                        caretSize: 6,
                        caretPadding: 8,
                        titleFont: { family: "'Inter', sans-serif", size: 12, weight: 700 },
                        bodyFont: { family: "'Inter', sans-serif", size: 11, weight: 500 },
                        titleMarginBottom: 8,
                        bodySpacing: 6,
                        boxPadding: 6,
                        displayColors: true,
                        usePointStyle: true,
                        callbacks: {
                            title: function(items) {
                                if (!items.length) return "";
                                const rawLabel = items[0].label || "";
                                // Show full label (un-truncated) in tooltip title
                                return rawLabel;
                            },
                            label: function (ctx) {
                                let val;
                                if (isPieType(chartType)) {
                                    val = ctx.parsed;
                                } else if (isHorizontal) {
                                    val = ctx.parsed.x;
                                } else {
                                    val = ctx.parsed.y;
                                }
                                const dsLabel = ctx.dataset.label || valueKeys[ctx.datasetIndex] || "Value";
                                const colName = valueKeys[ctx.datasetIndex] || dsLabel;
                                const isCurr = isCurrencyColumn(colName);
                                let formatted;
                                if (typeof val === "number") {
                                    if (isCurr) {
                                        if (Math.abs(val) >= 10000000) {
                                            formatted = "₹" + (val / 10000000).toFixed(2) + " Cr";
                                        } else if (Math.abs(val) >= 100000) {
                                            formatted = "₹" + (val / 100000).toFixed(2) + " L";
                                        } else if (Math.abs(val) >= 1000) {
                                            formatted = "₹" + val.toLocaleString("en-IN", { maximumFractionDigits: 0 });
                                        } else {
                                            formatted = val.toLocaleString("en-IN", { maximumFractionDigits: 2 });
                                        }
                                    } else {
                                        // Non-currency (count, qty, rate…) — plain number, no ₹
                                        if (Math.abs(val) >= 1000000) {
                                            formatted = (val / 1000000).toFixed(2) + "M";
                                        } else if (Math.abs(val) >= 1000) {
                                            formatted = val.toLocaleString("en-IN", { maximumFractionDigits: 0 });
                                        } else {
                                            formatted = val.toLocaleString("en-IN", { maximumFractionDigits: 2 });
                                        }
                                    }
                                } else {
                                    formatted = String(val);
                                }
                                return ` ${dsLabel}: ${formatted}`;
                            },
                            afterLabel: function(ctx) {
                                // For pie/doughnut: show percentage of total
                                if (isPieType(chartType)) {
                                    const dataset = ctx.dataset;
                                    const total = dataset.data.reduce((sum, v) => sum + (Number(v) || 0), 0);
                                    const val = Number(ctx.parsed) || 0;
                                    if (total > 0) {
                                        const pct = ((val / total) * 100).toFixed(1);
                                        return `  Share: ${pct}%`;
                                    }
                                }
                                return "";
                            },
                        },
                    },
                },
                scales: {},
                animation: { duration: 800, easing: "easeOutQuart" },
                onHover: (event, elements) => {
                    event.native.target.style.cursor = elements.length ? "pointer" : "default";
                },
            },
        };

        if (!isPieType(chartType) && chartType !== "radar" && chartType !== "polarArea") {
            // If every value column is a non-currency column (count, qty, etc.)
            // use plain number format on the axis (no ₹ prefix)
            const allNonCurrency = valueKeys.every(k => !isCurrencyColumn(k));
            const numFmtCallback = val => {
                if (typeof val !== "number") return val;
                if (allNonCurrency) {
                    if (Math.abs(val) >= 1000000) return (val / 1000000).toFixed(1) + "M";
                    if (Math.abs(val) >= 1000)    return (val / 1000).toFixed(1) + "K";
                    if (Number.isInteger(val)) return val.toLocaleString("en-IN");
                    return val.toLocaleString("en-IN", { maximumFractionDigits: 1 });
                }
                if (Math.abs(val) >= 10000000) return "₹" + (val / 10000000).toFixed(1) + "Cr";
                if (Math.abs(val) >= 100000)   return "₹" + (val / 100000).toFixed(1) + "L";
                if (Math.abs(val) >= 1000)     return "₹" + (val / 1000).toFixed(1) + "K";
                if (Number.isInteger(val)) return val.toLocaleString("en-IN");
                return val.toLocaleString("en-IN", { maximumFractionDigits: 1 });
            };
            // Smart label truncation — shows full text in tooltip
            const labelFmtCallback = function(val) {
                const label = this.getLabelForValue(val);
                if (typeof label === "string" && label.length > 18) {
                    return label.substring(0, 16) + "…";
                }
                return label;
            };
            // For horizontal bar: x=values (needs number fmt), y=labels (plain text)
            // For all others:     x=labels (plain text),     y=values (needs number fmt)
            const valueAxisKey  = isHorizontal ? "x" : "y";
            const labelAxisKey  = isHorizontal ? "y" : "x";
            // Auto-detect if labels are long and increase rotation
            const avgLabelLen = labels.reduce((s, l) => s + l.length, 0) / Math.max(labels.length, 1);
            const smartRotation = isHorizontal ? 0 : (avgLabelLen > 12 ? 45 : (labels.length > 8 ? 35 : 0));
            config.options.scales = {
                [valueAxisKey]: {
                    grid: { color: defaults.gridColor, drawBorder: false },
                    ticks: {
                        color: defaults.textColor,
                        font: { family: "'Inter'", size: 11, weight: 500 },
                        callback: numFmtCallback,
                        maxTicksLimit: 8,
                        padding: 4,
                    },
                    title: (isHorizontal ? chartSpec.x_label : chartSpec.y_label) ? {
                        display: true,
                        text: isHorizontal ? chartSpec.x_label : chartSpec.y_label,
                        color: defaults.textColor,
                        font: { family: "'Inter'", size: 11, weight: 700 },
                        padding: { top: 8 },
                    } : undefined,
                    stacked: isStacked,
                    beginAtZero: true,
                },
                [labelAxisKey]: {
                    grid: { color: isHorizontal ? "transparent" : defaults.gridColor, drawBorder: false },
                    ticks: {
                        color: defaults.textColor,
                        font: { family: "'Inter'", size: isHorizontal ? 11 : 10, weight: 500 },
                        maxRotation: smartRotation,
                        minRotation: smartRotation > 0 ? smartRotation - 10 : 0,
                        autoSkip: true,
                        autoSkipPadding: 8,
                        maxTicksLimit: isHorizontal ? 20 : 14,
                        callback: isHorizontal ? undefined : labelFmtCallback,
                    },
                    title: (isHorizontal ? chartSpec.y_label : chartSpec.x_label) ? {
                        display: true,
                        text: isHorizontal ? chartSpec.y_label : chartSpec.x_label,
                        color: defaults.textColor,
                        font: { family: "'Inter'", size: 11, weight: 700 },
                        padding: { top: 8 },
                    } : undefined,
                    stacked: isStacked,
                },
            };
        }

        if (chartType === "radar") {
            config.options.scales = {
                r: {
                    angleLines: { color: defaults.gridColor },
                    grid: { color: defaults.gridColor },
                    ticks: { color: defaults.textColor, font: { size: 8 }, backdropColor: "transparent" },
                    pointLabels: { color: defaults.textColor, font: { family: "'Inter'", size: 9, weight: 500 } },
                },
            };
        }

        // Sync height with CSS: wide cards = 310px, regular = 270px
        const isWideCard = canvas.closest(".chart-full-width") !== null;
        canvas.parentElement.style.height = isWideCard ? "310px" : "270px";
        return new Chart(canvas, config);
    }

    function isPieType(type) { return ["pie", "doughnut"].includes(type); }
    function formatColumnName(name) { return name.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()); }

    /**
     * Returns true if the column is likely a monetary/currency value (revenue, amount, price…).
     * Returns false for count/quantity/rate columns that should NOT be prefixed with ₹.
     */
    function isCurrencyColumn(colName) {
        const n = (colName || "").toLowerCase().replace(/_/g, " ");
        const countKeywords = /\b(count|qty|quantity|units|orders|items|pieces|num |number |frequency|volume|rate|ratio|pct|percent)\b/;
        if (countKeywords.test(n)) return false;
        const currencyKeywords = /\b(revenue|amount|total|price|value|cost|sales|spend|spending|earning|profit|margin|invoice)\b/;
        if (currencyKeywords.test(n)) return true;
        return true; // default: assume currency for unknown columns
    }

    // ══════════════════════════════════════════════════════════════════════
    //  PER-CHART FILTER SYSTEM
    // ══════════════════════════════════════════════════════════════════════

    /**
     * Build a smart filter panel for a chart based on its data columns.
     * Analyzes the data to decide which filters make sense:
     * - Label column: dropdown with unique values (if ≤50 unique) or search input
     * - Top N selector: pill buttons for quick selection
     * - Sort order: dropdown for bar/pie
     *
     * Filters are DYNAMIC per chart type — only relevant controls appear.
     * UI: floating card with header, sectioned body, and footer.
     */
    function buildChartFilterPanel(idx, chartSpec) {
        const panel = document.getElementById(`chartFilterPanel_${idx}`);
        if (!panel || !chartSpec.data || chartSpec.data.length === 0) return;

        const data = chartSpec.data;
        const keys = Object.keys(data[0]);
        if (keys.length < 2) return;

        const labelKey = keys[0];
        const valueKeys = keys.slice(1);
        const chartType = (chartSpec.type || "bar").toLowerCase();
        const totalItems = data.length;
        const uniqueLabels = [...new Set(data.map(r => String(r[labelKey])))];
        const chartTitle = chartSpec.title || "Chart";

        // ── Which filters to show per chart type ────────────────────────
        const isPie = isPieType(chartType);
        const isLine = chartType === "line" || chartType === "area";
        const isBar = chartType === "bar" || chartType === "horizontalbar" || chartType === "stackedbar";
        const showLabel    = uniqueLabels.length > 1;
        const showTopN     = totalItems > 3 && !isLine;
        const showSort     = isBar || isPie;
        const showSeries   = valueKeys.length > 1;
        const showGroupOthers = showTopN && totalItems > 5;

        // ── Header ──────────────────────────────────────────────────────
        let html = `<div class="cf-header">
            <div class="cf-header-title">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>
                Chart Filters
                <span class="cf-header-subtitle">\u2014 ${escapeHtml(chartTitle.length > 28 ? chartTitle.substring(0, 26) + '\u2026' : chartTitle)}</span>
            </div>
            <button class="cf-close-btn" id="cfClose_${idx}" title="Close filters">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
        </div>`;

        // ── Body ────────────────────────────────────────────────────────
        html += '<div class="cf-body">';

        // Section: Label / Category
        if (showLabel && uniqueLabels.length <= 50) {
            html += `<div class="cf-section">
                <div class="cf-section-label">${escapeHtml(formatColumnName(labelKey))}</div>
                <select class="cf-select" id="cfLabel_${idx}" multiple size="1">
                    <option value="__all__" selected>All ${escapeHtml(formatColumnName(labelKey))}s</option>
                    ${uniqueLabels.map(l => `<option value="${escapeAttr(l)}">${escapeHtml(l.length > 35 ? l.substring(0, 33) + '\u2026' : l)}</option>`).join('')}
                </select>
            </div>`;
        } else if (showLabel && uniqueLabels.length > 50) {
            html += `<div class="cf-section">
                <div class="cf-section-label">${escapeHtml(formatColumnName(labelKey))}</div>
                <input type="text" class="cf-search-input" id="cfSearch_${idx}" placeholder="Search ${formatColumnName(labelKey).toLowerCase()}\u2026" />
            </div>`;
        }

        // Section: Sort By (dropdown)
        if (showSort) {
            html += `<div class="cf-section">
                <div class="cf-section-label">Sort By</div>
                <select class="cf-select" id="cfSort_${idx}">
                    <option value="default">Default Order</option>
                    <option value="desc">\u2193 Highest First</option>
                    <option value="asc">\u2191 Lowest First</option>
                    <option value="alpha">A \u2192 Z</option>
                    <option value="alpha_desc">Z \u2192 A</option>
                </select>
            </div>`;
        }

        // Section: Series selector
        if (showSeries) {
            html += `<div class="cf-section">
                <div class="cf-section-label">Series</div>
                <select class="cf-select" id="cfValueCol_${idx}">
                    <option value="__all__">All Series</option>
                    ${valueKeys.map(k => `<option value="${escapeAttr(k)}">${escapeHtml(formatColumnName(k))}</option>`).join('')}
                </select>
            </div>`;
        }

        // Section: Show Top (pill buttons)
        if (showTopN) {
            const topOptions = [5, 10, 20].filter(n => n < totalItems);
            html += `<div class="cf-section">
                <div class="cf-section-label">Show Top</div>
                <div class="cf-pills">
                    <button class="cf-pill active" data-topn="0" data-idx="${idx}">All</button>
                    ${topOptions.map(n => `<button class="cf-pill" data-topn="${n}" data-idx="${idx}">Top ${n}</button>`).join('')}
                </div>
            </div>`;

            // Group Others toggle (inside Top N section)
            if (showGroupOthers) {
                html += `<div class="cf-section">
                    <div class="cf-toggle-row">
                        <label class="cf-switch">
                            <input type="checkbox" id="cfGroupOthers_${idx}" />
                            <span class="cf-switch-track"></span>
                        </label>
                        <label class="cf-switch-label" for="cfGroupOthers_${idx}">Group remaining as "Others"</label>
                    </div>
                </div>`;
            }
        }

        html += '</div>';

        // ── Footer ──────────────────────────────────────────────────────
        html += `<div class="cf-footer">
            <button class="cf-apply-btn" id="cfApply_${idx}">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
                Apply Filters
            </button>
            <button class="cf-reset-btn" id="cfReset_${idx}">Reset</button>
        </div>`;

        panel.innerHTML = html;

        // ── Wire events ─────────────────────────────────────────────────
        document.getElementById(`cfApply_${idx}`)?.addEventListener("click", () => applyChartFilter(idx));
        document.getElementById(`cfReset_${idx}`)?.addEventListener("click", () => resetChartFilter(idx));
        document.getElementById(`cfClose_${idx}`)?.addEventListener("click", () => {
            panel.classList.remove("open");
            const toggleBtn = panel.closest(".chart-card")?.querySelector(".chart-filter-toggle");
            if (toggleBtn) toggleBtn.classList.remove("active");
        });

        // Wire pill buttons (Top N)
        panel.querySelectorAll(".cf-pill").forEach(pill => {
            pill.addEventListener("click", () => {
                panel.querySelectorAll(".cf-pill").forEach(p => p.classList.remove("active"));
                pill.classList.add("active");
            });
        });

        // Wire label dropdown: deselect "All" when specific items are picked
        const labelSelect = document.getElementById(`cfLabel_${idx}`);
        if (labelSelect) {
            labelSelect.addEventListener("change", () => {
                const selected = Array.from(labelSelect.selectedOptions).map(o => o.value);
                if (selected.includes("__all__") && selected.length > 1) {
                    labelSelect.querySelector('option[value="__all__"]').selected = false;
                }
                if (selected.filter(v => v !== "__all__").length === 0) {
                    labelSelect.querySelector('option[value="__all__"]').selected = true;
                }
            });
        }
    }

    /** Format a number to a short display string for placeholders */
    function _shortNum(val) {
        if (Math.abs(val) >= 10000000) return (val / 10000000).toFixed(1) + "Cr";
        if (Math.abs(val) >= 100000)   return (val / 100000).toFixed(1) + "L";
        if (Math.abs(val) >= 1000)     return (val / 1000).toFixed(1) + "K";
        return String(Math.round(val * 100) / 100);
    }

    function applyChartFilter(idx) {
        const store = chartStore[idx];
        if (!store || !store.instance || !store.originalData) return;

        const data = JSON.parse(JSON.stringify(store.originalData));
        const keys = Object.keys(data[0]);
        const labelKey = keys[0];
        const valueKeys = keys.slice(1);
        const primaryValueKey = valueKeys[0];
        let filtered = [...data];
        let activeFilterDescriptions = [];

        // ── 1. Label filter (dropdown or search) ────────────────────────
        const labelSelect = document.getElementById(`cfLabel_${idx}`);
        const searchInput = document.getElementById(`cfSearch_${idx}`);

        if (labelSelect) {
            const selected = Array.from(labelSelect.selectedOptions).map(o => o.value);
            if (!selected.includes("__all__") && selected.length > 0) {
                filtered = filtered.filter(row => selected.includes(String(row[labelKey])));
                activeFilterDescriptions.push(`${selected.length} selected`);
            }
        }
        if (searchInput && searchInput.value.trim()) {
            const term = searchInput.value.trim().toLowerCase();
            filtered = filtered.filter(row => String(row[labelKey]).toLowerCase().includes(term));
            activeFilterDescriptions.push(`"${searchInput.value.trim()}"`);
        }

        // ── 2. Sort ─────────────────────────────────────────────────────
        const sortSelect = document.getElementById(`cfSort_${idx}`);
        if (sortSelect && sortSelect.value !== "default") {
            const sortVal = sortSelect.value;
            if (sortVal === "desc") {
                filtered.sort((a, b) => (Number(b[primaryValueKey]) || 0) - (Number(a[primaryValueKey]) || 0));
                activeFilterDescriptions.push("High→Low");
            } else if (sortVal === "asc") {
                filtered.sort((a, b) => (Number(a[primaryValueKey]) || 0) - (Number(b[primaryValueKey]) || 0));
                activeFilterDescriptions.push("Low→High");
            } else if (sortVal === "alpha") {
                filtered.sort((a, b) => String(a[labelKey]).localeCompare(String(b[labelKey])));
                activeFilterDescriptions.push("A→Z");
            } else if (sortVal === "alpha_desc") {
                filtered.sort((a, b) => String(b[labelKey]).localeCompare(String(a[labelKey])));
                activeFilterDescriptions.push("Z→A");
            }
        }

        // ── 3. Top N + Group Others ─────────────────────────────────────
        const panel = document.getElementById(`chartFilterPanel_${idx}`);
        const activePill = panel?.querySelector(".cf-pill.active");
        const topNVal = activePill ? parseInt(activePill.dataset.topn || "0") : 0;
        const groupOthersToggle = document.getElementById(`cfGroupOthers_${idx}`);
        if (topNVal > 0) {
            const n = topNVal;
            if (!sortSelect || sortSelect.value === "default") {
                filtered.sort((a, b) => (Number(b[primaryValueKey]) || 0) - (Number(a[primaryValueKey]) || 0));
            }
            if (groupOthersToggle && groupOthersToggle.checked && filtered.length > n) {
                const top = filtered.slice(0, n);
                const rest = filtered.slice(n);
                const othersRow = { [labelKey]: `Others (${rest.length})` };
                valueKeys.forEach(k => {
                    othersRow[k] = rest.reduce((s, r) => s + (Number(r[k]) || 0), 0);
                });
                filtered = [...top, othersRow];
                activeFilterDescriptions.push(`Top ${n} + Others`);
            } else {
                filtered = filtered.slice(0, n);
                activeFilterDescriptions.push(`Top ${n}`);
            }
        }

        // ── 6. Value column filter (hide series) ────────────────────────
        const valueColSelect = document.getElementById(`cfValueCol_${idx}`);
        let activeValueKeys = valueKeys;
        if (valueColSelect && valueColSelect.value !== "__all__") {
            activeValueKeys = [valueColSelect.value];
            activeFilterDescriptions.push(formatColumnName(valueColSelect.value));
        }

        // ── Update chart instance ───────────────────────────────────────
        const chart = store.instance;
        const chartType = chart.config.type;
        const newLabels = filtered.map(row => String(row[labelKey]));

        if (chartType !== "scatter") {
            chart.data.labels = newLabels;
        }

        const colors = getColors(store.spec.color_scheme, Math.max(filtered.length, activeValueKeys.length));
        activeValueKeys.forEach((key, dIdx) => {
            if (!chart.data.datasets[dIdx]) return;
            const values = filtered.map(row => {
                const v = row[key];
                return v === null || v === undefined ? 0 : Number(v) || 0;
            });
            chart.data.datasets[dIdx].data = values;

            if (isPieType(chartType)) {
                chart.data.datasets[dIdx].backgroundColor = colors.slice(0, values.length).map(c => c + "bb");
                chart.data.datasets[dIdx].hoverBackgroundColor = colors.slice(0, values.length).map(c => c + "ee");
            }
        });

        if (valueColSelect && valueColSelect.value !== "__all__") {
            chart.data.datasets.forEach((ds, i) => {
                ds.hidden = (valueKeys[i] !== valueColSelect.value);
            });
        } else {
            chart.data.datasets.forEach(ds => { ds.hidden = false; });
        }

        chart.update("active");

        // ── Update badge + chips ────────────────────────────────────────
        const badge = document.getElementById(`chartFilterBadge_${idx}`);
        const toggle = document.getElementById(`chartFilterBtn_${idx}`);
        const chipsEl = document.getElementById(`chartActiveFilters_${idx}`);

        if (activeFilterDescriptions.length > 0) {
            if (badge) badge.textContent = activeFilterDescriptions.length;
            if (toggle) toggle.classList.add("has-filters");
            if (chipsEl) {
                chipsEl.innerHTML = activeFilterDescriptions.map(desc =>
                    `<span class="chart-active-chip">${escapeHtml(desc)}</span>`
                ).join('');
            }
        } else {
            if (toggle) toggle.classList.remove("has-filters");
            if (chipsEl) chipsEl.innerHTML = '';
        }
    }

    /**
     * Reset all filters for a specific chart back to original data.
     */
    function resetChartFilter(idx) {
        const store = chartStore[idx];
        if (!store || !store.instance || !store.originalData) return;

        // Reset all filter controls
        const labelSelect = document.getElementById(`cfLabel_${idx}`);
        if (labelSelect) {
            Array.from(labelSelect.options).forEach(o => { o.selected = o.value === "__all__"; });
        }
        const searchInput = document.getElementById(`cfSearch_${idx}`);
        if (searchInput) searchInput.value = "";
        // Reset Top N pills to "All"
        const filterPanel = document.getElementById(`chartFilterPanel_${idx}`);
        if (filterPanel) {
            filterPanel.querySelectorAll(".cf-pill").forEach(p => p.classList.remove("active"));
            const allPill = filterPanel.querySelector('.cf-pill[data-topn="0"]');
            if (allPill) allPill.classList.add("active");
        }
        const sortSelect = document.getElementById(`cfSort_${idx}`);
        if (sortSelect) sortSelect.value = "default";
        const valueColSelect = document.getElementById(`cfValueCol_${idx}`);
        if (valueColSelect) valueColSelect.value = "__all__";
        const groupOthersToggle = document.getElementById(`cfGroupOthers_${idx}`);
        if (groupOthersToggle) groupOthersToggle.checked = false;

        // Restore original data to chart
        const data = store.originalData;
        const keys = Object.keys(data[0]);
        const labelKey = keys[0];
        const valueKeys = keys.slice(1);
        const chart = store.instance;
        const chartType = chart.config.type;
        const labels = data.map(row => String(row[labelKey]));
        const colors = getColors(store.spec.color_scheme, Math.max(data.length, valueKeys.length));

        if (chartType !== "scatter") {
            chart.data.labels = labels;
        }

        valueKeys.forEach((key, dIdx) => {
            if (!chart.data.datasets[dIdx]) return;
            const values = data.map(row => {
                const v = row[key];
                return v === null || v === undefined ? 0 : Number(v) || 0;
            });
            chart.data.datasets[dIdx].data = values;
            chart.data.datasets[dIdx].hidden = false;

            if (isPieType(chartType)) {
                chart.data.datasets[dIdx].backgroundColor = colors.slice(0, values.length).map(c => c + "bb");
                chart.data.datasets[dIdx].hoverBackgroundColor = colors.slice(0, values.length).map(c => c + "ee");
            }
        });

        chart.update("active");

        // Clear badge + chips
        const badge = document.getElementById(`chartFilterBadge_${idx}`);
        const toggle = document.getElementById(`chartFilterBtn_${idx}`);
        const chipsEl = document.getElementById(`chartActiveFilters_${idx}`);
        if (toggle) toggle.classList.remove("has-filters");
        if (chipsEl) chipsEl.innerHTML = '';
    }

    // ── KPI Value Formatting ──────────────────────────────────────────────
    function formatKPIValue(value, format) {
        if (value === null || value === undefined || value === "N/A") return "N/A";
        const num = Number(value);
        if (isNaN(num)) return String(value);

        if (format === "percent") return num.toFixed(1) + "%";

        if (format === "currency") {
            if (Math.abs(num) >= 10000000) return "₹" + (num / 10000000).toFixed(2) + " Cr";
            if (Math.abs(num) >= 100000) return "₹" + (num / 100000).toFixed(2) + " L";
            return "₹" + num.toLocaleString("en-IN", { maximumFractionDigits: 0 });
        }

        if (Math.abs(num) >= 10000000) return (num / 10000000).toFixed(2) + " Cr";
        if (Math.abs(num) >= 100000) return (num / 100000).toFixed(2) + " L";
        if (Number.isInteger(num)) return num.toLocaleString("en-IN");
        return num.toLocaleString("en-IN", { maximumFractionDigits: 2 });
    }

    // ── Table Builder ─────────────────────────────────────────────────────
    function buildTable(rows) {
        if (!rows || !rows.length) return '<p style="font-size:0.72rem;color:var(--text-muted);padding:0.5rem">No data.</p>';
        const cols = Object.keys(rows[0]);
        const display = rows.slice(0, 200);
        let html = "<table><thead><tr>";
        cols.forEach(c => { html += `<th>${escapeHtml(formatColumnName(c))}</th>`; });
        html += "</tr></thead><tbody>";
        display.forEach(row => {
            html += "<tr>";
            cols.forEach(c => {
                const v = row[c];
                let dv = v === null || v === undefined ? "—" : String(v);
                const nv = Number(v);
                if (!isNaN(nv) && String(v) === String(nv) && Math.abs(nv) >= 1000) {
                    dv = nv.toLocaleString("en-IN", { maximumFractionDigits: 2 });
                }
                html += `<td>${escapeHtml(dv)}</td>`;
            });
            html += "</tr>";
        });
        html += "</tbody></table>";
        return html;
    }

    // ── Explanation Modal ─────────────────────────────────────────────────
    const explainOverlay = document.getElementById("explainModalOverlay");
    const explainTitle = document.getElementById("explainModalTitle");
    const explainBody = document.getElementById("explainModalBody");
    const explainClose = document.getElementById("explainModalClose");

    function showExplanationModal(title, explanation) {
        explainTitle.textContent = title;
        // Build a flowing paragraph from all explanation fields
        const parts = [];
        if (explanation.what) parts.push(explanation.what);
        if (explanation.how) parts.push(explanation.how);
        if (explanation.why) parts.push(explanation.why);
        if (explanation.insight) parts.push(explanation.insight);
        const fullText = parts.join(". ").replace(/\.\.\s*/g, ". ").replace(/\s+/g, " ").trim();
        explainBody.innerHTML = `<div style="font-size:0.82rem;line-height:1.75;color:var(--text-secondary);padding:0.25rem 0">${escapeHtml(fullText)}</div>`;
        explainOverlay.classList.remove("hidden");
    }

    explainClose.addEventListener("click", () => explainOverlay.classList.add("hidden"));
    explainOverlay.addEventListener("click", e => { if (e.target === explainOverlay) explainOverlay.classList.add("hidden"); });

    // ── Thought Process Modal ─────────────────────────────────────────────
    const thoughtOverlay = document.getElementById("thoughtModalOverlay");
    const thoughtBody = document.getElementById("thoughtModalBody");
    const thoughtClose = document.getElementById("thoughtModalClose");
    const thoughtBtn = document.getElementById("reportThoughtBtn");

    thoughtBtn.addEventListener("click", () => {
        const meta = currentReport && currentReport.meta;
        const steps = (meta && meta.thought_process) || [];

        if (steps.length === 0) {
            thoughtBody.innerHTML = '<p style="color:var(--text-muted);font-size:0.82rem;">No reasoning data available.</p>';
        } else {
            let html = '<ul class="thought-steps">';
            steps.forEach((step, i) => {
                html += `<li class="thought-step"><span class="thought-step-num">${i + 1}</span><span class="thought-step-text">${escapeHtml(step)}</span></li>`;
            });
            html += "</ul>";
            thoughtBody.innerHTML = html;
        }
        thoughtOverlay.classList.remove("hidden");
    });

    thoughtClose.addEventListener("click", () => thoughtOverlay.classList.add("hidden"));
    thoughtOverlay.addEventListener("click", e => { if (e.target === thoughtOverlay) thoughtOverlay.classList.add("hidden"); });

    // ── Utilities ─────────────────────────────────────────────────────────
    function escapeHtml(str) {
        const d = document.createElement("div");
        d.appendChild(document.createTextNode(String(str)));
        return d.innerHTML;
    }
    function escapeAttr(str) {
        return str.replace(/&/g, "&amp;").replace(/'/g, "&#39;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // ── Per-Chart Download Wiring ──────────────────────────────────────
    function _wireChartDownloads(container) {
        // Toggle dropdown on button click
        container.querySelectorAll(".chart-dl-btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const wrapper = btn.closest(".chart-dl-wrapper");
                const dropdown = wrapper.querySelector(".chart-dl-dropdown");
                // Close any other open dropdowns
                document.querySelectorAll(".chart-dl-dropdown.open").forEach(d => {
                    if (d !== dropdown) d.classList.remove("open");
                });
                dropdown.classList.toggle("open");
            });
        });

        // Handle download option clicks
        container.querySelectorAll(".chart-dl-option").forEach(opt => {
            opt.addEventListener("click", (e) => {
                e.stopPropagation();
                const dlType = opt.dataset.dlType;
                const dlIdx = parseInt(opt.dataset.dlIdx);
                opt.closest(".chart-dl-dropdown").classList.remove("open");

                const entry = chartStore[dlIdx];
                if (!entry || !entry.instance) {
                    alert("Chart not available for download.");
                    return;
                }

                const chartTitle = (currentReport.charts[dlIdx]?.title || `Chart_${dlIdx + 1}`)
                    .replace(/[^a-zA-Z0-9_\- ]/g, "").replace(/\s+/g, "_");

                if (dlType === "png") {
                    // Download as PNG
                    const imgData = entry.instance.toBase64Image("image/png", 1.0);
                    const link = document.createElement("a");
                    link.download = `${chartTitle}.png`;
                    link.href = imgData;
                    link.click();
                } else if (dlType === "pdf") {
                    // Download as single-page PDF
                    try {
                        const imgData = entry.instance.toBase64Image("image/png", 1.0);
                        const { jsPDF } = window.jspdf;
                        const img = new Image();
                        img.onload = () => {
                            const ratio = img.width / img.height;
                            const pdfWidth = 210;
                            const margin = 12;
                            const usable = pdfWidth - margin * 2;
                            const imgH = usable / ratio;
                            const pdfHeight = imgH + margin * 2 + 20; // extra for title
                            const pdf = new jsPDF("l", "mm", [pdfWidth, Math.max(pdfHeight, 150)]);
                            // Title
                            pdf.setFontSize(14);
                            pdf.text(currentReport.charts[dlIdx]?.title || "Chart", margin, margin + 5);
                            // Chart image
                            pdf.addImage(imgData, "PNG", margin, margin + 12, usable, imgH);
                            pdf.save(`${chartTitle}.pdf`);
                        };
                        img.src = imgData;
                    } catch (err) {
                        console.error("Chart PDF failed:", err);
                        alert("Failed to generate chart PDF.");
                    }
                }
            });
        });

        // Close dropdowns when clicking outside
        document.addEventListener("click", () => {
            document.querySelectorAll(".chart-dl-dropdown.open").forEach(d => d.classList.remove("open"));
        });
    }

    // ── PDF Download (pure jsPDF — no html2canvas) ─────────────────────────
    const pdfBtn = document.getElementById("downloadPdfBtn");
    pdfBtn.addEventListener("click", async () => {
        pdfBtn.disabled = true;
        const origText = pdfBtn.innerHTML;
        pdfBtn.innerHTML = '<div class="dl-spinner"></div> Generating...';

        try {
            const { jsPDF } = window.jspdf;
            const pdf = new jsPDF("p", "mm", "a4");
            const W = 210, H = 297, M = 14;           // page width, height, margin
            const usableW = W - M * 2;
            let y = M;

            // Helper: add new page if not enough room
            function ensureSpace(needed) {
                if (y + needed > H - M) {
                    pdf.addPage();
                    y = M;
                }
            }

            // Helper: wrap text and return lines
            function wrapText(text, maxWidth, fontSize) {
                pdf.setFontSize(fontSize);
                return pdf.splitTextToSize(text, maxWidth);
            }

            // ── Title ────────────────────────────────────────────
            pdf.setFont("helvetica", "bold");
            pdf.setFontSize(18);
            pdf.setTextColor(30, 30, 60);
            const titleText = currentReport.title || "Analytics Report";
            pdf.text(titleText, M, y + 6);
            y += 12;

            // Thin accent line under title
            pdf.setDrawColor(99, 102, 241);
            pdf.setLineWidth(0.8);
            pdf.line(M, y, M + 60, y);
            y += 6;

            // ── Date ─────────────────────────────────────────────
            pdf.setFont("helvetica", "normal");
            pdf.setFontSize(8);
            pdf.setTextColor(120, 120, 140);
            pdf.text("Generated: " + new Date().toLocaleString(), M, y);
            y += 8;

            // ── Summary ──────────────────────────────────────────
            if (currentReport.summary) {
                ensureSpace(30);
                // Light blue background box
                pdf.setFillColor(240, 244, 255);
                const summaryLines = wrapText(currentReport.summary, usableW - 8, 9);
                const summaryH = summaryLines.length * 4.5 + 8;
                pdf.roundedRect(M, y, usableW, summaryH, 3, 3, "F");
                // Left accent bar
                pdf.setFillColor(99, 102, 241);
                pdf.rect(M, y, 1.5, summaryH, "F");
                // Text
                pdf.setFont("helvetica", "normal");
                pdf.setFontSize(9);
                pdf.setTextColor(50, 50, 70);
                pdf.text(summaryLines, M + 6, y + 6);
                y += summaryH + 6;
            }

            // ── KPIs ─────────────────────────────────────────────
            const kpis = currentReport.kpis || [];
            if (kpis.length > 0) {
                ensureSpace(30);
                // Section label
                pdf.setFont("helvetica", "bold");
                pdf.setFontSize(10);
                pdf.setTextColor(16, 185, 129);
                pdf.text("KEY PERFORMANCE INDICATORS", M, y + 4);
                y += 8;

                // KPI cards in a grid (3 per row)
                const cols = 3;
                const cardW = (usableW - (cols - 1) * 4) / cols;
                const cardH = 20;
                const accentColors = [
                    [16, 185, 129], [59, 130, 246], [139, 92, 246],
                    [245, 158, 11], [244, 63, 94], [6, 182, 212]
                ];

                for (let i = 0; i < kpis.length; i++) {
                    const col = i % cols;
                    const row = Math.floor(i / cols);
                    if (col === 0 && i > 0) {
                        y += cardH + 4;
                        ensureSpace(cardH + 4);
                    }
                    const x = M + col * (cardW + 4);
                    const cy = (col === 0 && i > 0) ? y : y;

                    // Card background
                    pdf.setFillColor(248, 249, 250);
                    pdf.roundedRect(x, cy, cardW, cardH, 2, 2, "F");

                    // Top accent bar
                    const ac = accentColors[i % accentColors.length];
                    pdf.setFillColor(ac[0], ac[1], ac[2]);
                    pdf.rect(x, cy, cardW, 1.5, "F");

                    // Label
                    pdf.setFont("helvetica", "normal");
                    pdf.setFontSize(6.5);
                    pdf.setTextColor(100, 100, 120);
                    const label = (kpis[i].label || "").substring(0, 30);
                    pdf.text(label.toUpperCase(), x + 3, cy + 6);

                    // Value
                    pdf.setFont("helvetica", "bold");
                    pdf.setFontSize(13);
                    pdf.setTextColor(30, 30, 50);
                    const val = String(kpis[i].value || "—").substring(0, 20);
                    pdf.text(val, x + 3, cy + 14);
                }

                y += cardH + 8;
            }

            // ── Charts ───────────────────────────────────────────
            const charts = currentReport.charts || [];
            if (charts.length > 0) {
                ensureSpace(20);
                pdf.setFont("helvetica", "bold");
                pdf.setFontSize(10);
                pdf.setTextColor(99, 102, 241);
                pdf.text("CHARTS & VISUALIZATIONS", M, y + 4);
                y += 10;

                for (let i = 0; i < charts.length; i++) {
                    const chartEntry = chartStore[i];
                    if (!chartEntry || !chartEntry.instance) continue;

                    const chartImg = chartEntry.instance.toBase64Image("image/png", 1.0);
                    const chartTitle = charts[i].title || ("Chart " + (i + 1));

                    // Calculate image dimensions (fit to usableW, max height 100mm)
                    const canvas = chartEntry.instance.canvas;
                    const aspect = canvas.width / canvas.height;
                    let imgW = usableW;
                    let imgH = imgW / aspect;
                    if (imgH > 100) { imgH = 100; imgW = imgH * aspect; }

                    const cardH = imgH + 14;
                    ensureSpace(cardH + 4);

                    // Card background
                    pdf.setFillColor(255, 255, 255);
                    pdf.setDrawColor(220, 220, 230);
                    pdf.roundedRect(M, y, usableW, cardH, 2, 2, "FD");

                    // Chart title
                    pdf.setFont("helvetica", "bold");
                    pdf.setFontSize(9);
                    pdf.setTextColor(30, 30, 60);
                    pdf.text(chartTitle, M + 4, y + 7);

                    // Chart image
                    const imgX = M + (usableW - imgW) / 2;
                    pdf.addImage(chartImg, "PNG", imgX, y + 11, imgW, imgH);

                    y += cardH + 5;
                }
            }

            // ── Insights ─────────────────────────────────────────
            const insights = currentReport.insights || [];
            if (insights.length > 0) {
                ensureSpace(20);
                pdf.setFont("helvetica", "bold");
                pdf.setFontSize(10);
                pdf.setTextColor(245, 158, 11);
                pdf.text("KEY INSIGHTS", M, y + 4);
                y += 8;

                const insightColors = {
                    positive: [16, 185, 129],
                    negative: [244, 63, 94],
                    warning: [244, 63, 94],
                    neutral: [59, 130, 246],
                    opportunity: [245, 158, 11]
                };

                insights.forEach(ins => {
                    const title = ins.title || "";
                    const body = ins.body || ins.text || "";
                    const type = (ins.type || "neutral").toLowerCase();
                    const color = insightColors[type] || insightColors.neutral;

                    const bodyLines = wrapText(body, usableW - 12, 8);
                    const cardH = 8 + bodyLines.length * 3.8 + 4;
                    ensureSpace(cardH + 3);

                    // Card
                    pdf.setFillColor(252, 252, 253);
                    pdf.roundedRect(M, y, usableW, cardH, 2, 2, "F");
                    // Left accent
                    pdf.setFillColor(color[0], color[1], color[2]);
                    pdf.rect(M, y, 1.5, cardH, "F");

                    // Title
                    pdf.setFont("helvetica", "bold");
                    pdf.setFontSize(8.5);
                    pdf.setTextColor(30, 30, 60);
                    pdf.text(title, M + 5, y + 5.5);

                    // Body
                    pdf.setFont("helvetica", "normal");
                    pdf.setFontSize(8);
                    pdf.setTextColor(70, 70, 90);
                    pdf.text(bodyLines, M + 5, y + 10);

                    y += cardH + 3;
                });
            }

            // ── Save ─────────────────────────────────────────────
            const fileName = (currentReport.title || "Analytics_Report")
                .replace(/[^a-zA-Z0-9_\- ]/g, "").replace(/\s+/g, "_");
            pdf.save(fileName + ".pdf");

        } catch (err) {
            console.error("PDF generation failed:", err);
            alert("PDF generation failed: " + err.message);
        }

        pdfBtn.disabled = false;
        pdfBtn.innerHTML = origText;
    });

    // ── Excel Download ───────────────────────────────────────────────────
    const excelBtn = document.getElementById("downloadExcelBtn");
    excelBtn.addEventListener("click", () => {
        excelBtn.disabled = true;
        const origText = excelBtn.innerHTML;
        excelBtn.innerHTML = '<div class="dl-spinner"></div> Exporting...';

        try {
            const wb = XLSX.utils.book_new();

            // Sheet 1: KPIs
            if (currentReport.kpis && currentReport.kpis.length > 0) {
                const kpiData = currentReport.kpis.map(kpi => ({
                    "KPI": kpi.label || kpi.title || "—",
                    "Value": kpi.value != null ? kpi.value : "N/A",
                    "SQL": kpi.sql || "",
                }));
                const ws = XLSX.utils.json_to_sheet(kpiData);
                ws["!cols"] = [{ wch: 30 }, { wch: 20 }, { wch: 60 }];
                XLSX.utils.book_append_sheet(wb, ws, "KPIs");
            }

            // Sheets 2-N: Each chart's data
            if (currentReport.charts && currentReport.charts.length > 0) {
                currentReport.charts.forEach((chart, i) => {
                    if (!chart.data || chart.data.length === 0) return;
                    const sheetName = (chart.title || `Chart ${i + 1}`)
                        .replace(/[\\\/\?\*\[\]:]/g, "")
                        .substring(0, 31); // Excel sheet name limit
                    const ws = XLSX.utils.json_to_sheet(chart.data);
                    XLSX.utils.book_append_sheet(wb, ws, sheetName);
                });
            }

            // Final sheet: Detail Table
            if (currentReport.table && currentReport.table.data && currentReport.table.data.length > 0) {
                const ws = XLSX.utils.json_to_sheet(currentReport.table.data);
                XLSX.utils.book_append_sheet(wb, ws, "Detail Table");
            }

            const title = (currentReport.title || "Analytics_Report")
                .replace(/[^a-zA-Z0-9_\- ]/g, "")
                .replace(/\s+/g, "_");
            XLSX.writeFile(wb, `${title}.xlsx`);

        } catch (err) {
            console.error("Excel export failed:", err);
            alert("Excel export failed. Please try again.");
        }

        excelBtn.disabled = false;
        excelBtn.innerHTML = origText;
    });

    // ── Initialize ────────────────────────────────────────────────────────
    renderReport(currentReport);
    loadFilterOptions();

    // ══════════════════════════════════════════════════════════════════════
    //  EDIT MODE  —  Drag & Drop reorder + Delete components
    // ══════════════════════════════════════════════════════════════════════
    let editModeActive = false;
    let _dragType = null;   // "kpi" | "chart"
    let _dragIdx  = -1;

    const editBtn      = document.getElementById("editModeBtn");
    const editAddGroup = document.getElementById("editAddGroup");
    const addKpiBtn    = document.getElementById("addKpiBtn");
    const addChartBtn  = document.getElementById("addChartBtn");

    editBtn.addEventListener("click", toggleEditMode);

    function toggleEditMode() {
        editModeActive = !editModeActive;
        editBtn.classList.toggle("active", editModeActive);
        editBtn.querySelector(".edit-mode-label").textContent = editModeActive ? "Done" : "Edit";
        editAddGroup.classList.toggle("visible", editModeActive);
        const content = document.getElementById("reportContent");
        content.classList.toggle("edit-mode", editModeActive);
        if (editModeActive) _attachEditControls();
        else _detachEditControls();
    }

    addKpiBtn.addEventListener("click", () => {
        if (!editModeActive) return;
        currentReport.kpis = currentReport.kpis || [];
        currentReport.kpis.push({ _placeholder: true });
        _persistReport();
        renderReport(currentReport, { skipAnimation: true });
        setTimeout(() => {
            _attachEditControls();
            // Auto-open the chat on the new placeholder
            const cards = document.querySelectorAll(".kpi-card.placeholder-card");
            const last = cards[cards.length - 1];
            if (last) {
                const inner = last.querySelector(".ph-inner");
                const chat  = last.querySelector(".ph-chat");
                const input = last.querySelector(".ph-input");
                if (inner && chat && input) {
                    inner.style.display = "none";
                    chat.style.display  = "flex";
                    input.focus();
                }
            }
        }, 60);
    });

    addChartBtn.addEventListener("click", () => {
        if (!editModeActive) return;
        currentReport.charts = currentReport.charts || [];
        currentReport.charts.push({ _placeholder: true });
        _persistReport();
        renderReport(currentReport, { skipAnimation: true });
        setTimeout(() => {
            _attachEditControls();
            // Auto-open the chat on the new placeholder
            const cards = document.querySelectorAll(".chart-card.placeholder-card");
            const last = cards[cards.length - 1];
            if (last) {
                const inner = last.querySelector(".ph-inner");
                const chat  = last.querySelector(".ph-chat");
                const input = last.querySelector(".ph-input");
                if (inner && chat && input) {
                    inner.style.display = "none";
                    chat.style.display  = "flex";
                    input.focus();
                }
            }
        }, 60);
    });

    function _attachEditControls() {
        document.querySelectorAll(".kpi-card").forEach((card, i) => {
            card.dataset.editIdx  = i;
            card.dataset.editType = "kpi";
            if (card.classList.contains("placeholder-card")) {
                card.style.position = "relative";
                _addPlaceholderDeleteBtn(card, i, "kpi");
                card.addEventListener("dragover",  _onDragOver);
                card.addEventListener("dragleave", _onDragLeave);
                card.addEventListener("drop",      _onDrop);
                return;
            }
            card.style.position = "relative";
            card.setAttribute("draggable", "true");
            _addOverlay(card, i, "kpi");
            card.addEventListener("dragstart",  _onDragStart);
            card.addEventListener("dragover",   _onDragOver);
            card.addEventListener("dragleave",  _onDragLeave);
            card.addEventListener("drop",       _onDrop);
            card.addEventListener("dragend",    _onDragEnd);
        });
        document.querySelectorAll(".chart-card").forEach((card, i) => {
            card.dataset.editIdx  = i;
            card.dataset.editType = "chart";
            if (card.classList.contains("placeholder-card")) {
                card.style.position = "relative";
                _addPlaceholderDeleteBtn(card, i, "chart");
                card.addEventListener("dragover",  _onDragOver);
                card.addEventListener("dragleave", _onDragLeave);
                card.addEventListener("drop",      _onDrop);
                return;
            }
            card.style.position = "relative";
            card.setAttribute("draggable", "true");
            _addOverlay(card, i, "chart");
            card.addEventListener("dragstart",  _onDragStart);
            card.addEventListener("dragover",   _onDragOver);
            card.addEventListener("dragleave",  _onDragLeave);
            card.addEventListener("drop",       _onDrop);
            card.addEventListener("dragend",    _onDragEnd);
        });
    }

    function _addOverlay(card, idx, type) {
        const ov = document.createElement("div");
        ov.className = "edit-card-overlay";
        ov.innerHTML =
            `<span class="drag-handle-icon" title="Drag to reorder">` +
            `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">` +
            `<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>` +
            `</svg></span>` +
            `<button class="delete-card-btn" title="Delete">&#x2715;</button>`;
        card.appendChild(ov);
        ov.querySelector(".delete-card-btn").addEventListener("click", e => {
            e.stopPropagation();
            _deleteComponent(type, idx);
        });
    }

    function _addPlaceholderDeleteBtn(card, idx, type) {
        const ov = document.createElement("div");
        ov.className = "edit-card-overlay";
        ov.innerHTML = `<button class="delete-card-btn" title="Remove empty slot">&#x2715;</button>`;
        card.appendChild(ov);
        ov.querySelector(".delete-card-btn").addEventListener("click", e => {
            e.stopPropagation();
            _removePlaceholder(type, idx);
        });
    }

    function _detachEditControls() {
        document.querySelectorAll(".edit-card-overlay").forEach(el => el.remove());
        document.querySelectorAll(".kpi-card, .chart-card").forEach(c => {
            c.removeAttribute("draggable");
            c.classList.remove("dragging", "drag-over");
        });
    }

    function _removePlaceholder(type, idx) {
        if (type === "kpi"   && currentReport.kpis)
            currentReport.kpis.splice(idx, 1);
        if (type === "chart" && currentReport.charts)
            currentReport.charts.splice(idx, 1);
        _persistReport();
        renderReport(currentReport, { skipAnimation: true });
        if (editModeActive) setTimeout(_attachEditControls, 60);
    }

    function _deleteComponent(type, idx) {
        if (type === "kpi"   && currentReport.kpis   && idx < currentReport.kpis.length)
            currentReport.kpis[idx] = { _placeholder: true };
        if (type === "chart" && currentReport.charts && idx < currentReport.charts.length)
            currentReport.charts[idx] = { _placeholder: true };
        _persistReport();
        renderReport(currentReport, { skipAnimation: true });
        if (editModeActive) setTimeout(_attachEditControls, 60);
    }

    function _onDragStart(e) {
        _dragType = this.dataset.editType;
        _dragIdx  = parseInt(this.dataset.editIdx);
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", _dragIdx);
        setTimeout(() => this.classList.add("dragging"), 0);
    }
    function _onDragOver(e) {
        e.preventDefault();
        if (this.dataset.editType !== _dragType) return;
        e.dataTransfer.dropEffect = "move";
        this.classList.add("drag-over");
    }
    function _onDragLeave() { this.classList.remove("drag-over"); }
    function _onDrop(e) {
        e.preventDefault(); e.stopPropagation();
        this.classList.remove("drag-over");
        if (this.dataset.editType !== _dragType) return;
        const dst = parseInt(this.dataset.editIdx);
        if (dst === _dragIdx) return;
        const arr = _dragType === "kpi" ? currentReport.kpis : currentReport.charts;
        if (this.classList.contains("placeholder-card")) {
            // Fill the placeholder slot with the dragged item
            const [item] = arr.splice(_dragIdx, 1);
            const adjustedDst = _dragIdx < dst ? dst - 1 : dst;
            arr[adjustedDst] = item;
        } else {
            const [item] = arr.splice(_dragIdx, 1);
            arr.splice(dst, 0, item);
        }
        _persistReport();
        renderReport(currentReport, { skipAnimation: true });
        if (editModeActive) setTimeout(_attachEditControls, 60);
    }
    function _onDragEnd() {
        document.querySelectorAll(".kpi-card,.chart-card").forEach(c => c.classList.remove("dragging","drag-over"));
        _dragIdx = -1; _dragType = null;
    }

    // ══════════════════════════════════════════════════════════════════════
    //  PLACEHOLDER SLOT WIRING
    // ══════════════════════════════════════════════════════════════════════
    function _wirePlaceholders(container) {
        container.querySelectorAll(".placeholder-card").forEach(card => {
            const phIdx  = parseInt(card.dataset.phIdx);
            const phType = card.dataset.phType;
            const inner    = card.querySelector(".ph-inner");
            const chat     = card.querySelector(".ph-chat");
            const input    = card.querySelector(".ph-input");
            const sendBtn  = card.querySelector(".ph-send-btn");
            const cancelBtn = card.querySelector(".ph-cancel-btn");
            const plusBtn  = card.querySelector(".ph-plus-btn");

            plusBtn.addEventListener("click", () => {
                inner.style.display = "none";
                chat.style.display  = "flex";
                input.focus();
            });
            cancelBtn.addEventListener("click", () => {
                inner.style.display = "";
                chat.style.display  = "none";
                input.value = "";
            });

            async function _doAdd() {
                const desc = input.value.trim();
                if (!desc) return;
                const origLabel = sendBtn.textContent;
                sendBtn.disabled = true;
                sendBtn.textContent = "Adding…";

                const clean = JSON.parse(JSON.stringify(currentReport));
                clean.kpis   = (clean.kpis   || []).filter(k => !k._placeholder);
                clean.charts = (clean.charts || []).filter(c => !c._placeholder);
                const total  = phType === "kpi" ? clean.kpis.length : clean.charts.length;
                const mod    = `Add a new ${phType} at index ${phIdx} (0-based, currently ${total} ${phType}s exist): ${desc}`;

                try {
                    const res = await fetch("/report/modify", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ report_json: JSON.stringify(clean), modification: mod, provider: "groq" })
                    });
                    const data = await res.json();
                    if (data.error || !data.report) {
                        sendBtn.disabled = false;
                        sendBtn.textContent = origLabel;
                        alert(data.error || "Failed — please try again.");
                        return;
                    }
                    currentReport = data.report;
                    _persistReport();
                    renderReport(currentReport, { skipAnimation: true });
                    if (editModeActive) setTimeout(_attachEditControls, 60);
                } catch (err) {
                    sendBtn.disabled = false;
                    sendBtn.textContent = origLabel;
                    alert("Network error: " + String(err));
                }
            }

            sendBtn.addEventListener("click", _doAdd);
            input.addEventListener("keydown", e => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); _doAdd(); }
                if (e.key === "Escape") cancelBtn.click();
            });
        });
    }

})();
