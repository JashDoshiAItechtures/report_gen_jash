/* ═══════════════════════════════════════════════════════════════════════════
   AI SQL Analyst — Frontend Logic
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    // ── DOM refs ──────────────────────────────────────────────────────────
    const questionInput   = document.getElementById("questionInput");
    const submitBtn       = document.getElementById("submitBtn");
    const loadingIndicator= document.getElementById("loadingIndicator");
    const resultsSection  = document.getElementById("resultsSection");
    const errorSection    = document.getElementById("errorSection");

    const sqlOutput       = document.getElementById("sqlOutput");
    const tableWrapper    = document.getElementById("tableWrapper");
    const rowCount        = document.getElementById("rowCount");
    const answerOutput    = document.getElementById("answerOutput");
    const insightsOutput  = document.getElementById("insightsOutput");
    const errorOutput     = document.getElementById("errorOutput");
    const copySqlBtn      = document.getElementById("copySqlBtn");

    const modelSwitcher   = document.getElementById("modelSwitcher");

    // ── Sidebar DOM refs ──────────────────────────────────────────────────
    const sidebar         = document.getElementById("sidebar");
    const sidebarList     = document.getElementById("sidebarList");
    const sidebarToggle   = document.getElementById("sidebarToggle");
    const newChatBtn      = document.getElementById("newChatBtn");

    // ── Modal DOM refs ────────────────────────────────────────────────────
    const historyModal    = document.getElementById("historyModal");
    const modalBody       = document.getElementById("modalBody");
    const modalTitle      = document.getElementById("modalTitle");
    const modalClose      = document.getElementById("modalClose");

    let selectedProvider  = "groq";
    let loadingStepTimer  = null;

    // Persistent conversation id per browser (for multi-turn memory)
    let conversationId = window.localStorage.getItem("sqlbot_conversation_id");
    if (!conversationId) {
        if (window.crypto && window.crypto.randomUUID) {
            conversationId = window.crypto.randomUUID();
        } else {
            conversationId = "conv-" + Date.now().toString(36);
        }
        window.localStorage.setItem("sqlbot_conversation_id", conversationId);
    }

    // ── Sidebar toggle ───────────────────────────────────────────────────
    let sidebarOpen = true;

    function setSidebar(open) {
        sidebarOpen = open;
        if (open) {
            sidebar.classList.remove("collapsed");
        } else {
            sidebar.classList.add("collapsed");
        }
    }

    sidebarToggle.addEventListener("click", () => setSidebar(!sidebarOpen));

    // ── New chat ─────────────────────────────────────────────────────────
    newChatBtn.addEventListener("click", () => {
        // Generate a brand-new conversation id and clear the UI
        if (window.crypto && window.crypto.randomUUID) {
            conversationId = window.crypto.randomUUID();
        } else {
            conversationId = "conv-" + Date.now().toString(36);
        }
        window.localStorage.setItem("sqlbot_conversation_id", conversationId);
        hideResults();
        hideError();
        questionInput.value = "";
        renderSidebar([]);
        loadSidebarHistory();
    });

    // ── Sidebar history ───────────────────────────────────────────────────
    async function loadSidebarHistory() {
        try {
            const res = await fetch(`/history?conversation_id=${encodeURIComponent(conversationId)}`);
            if (!res.ok) return;
            const turns = await res.json();
            renderSidebar(turns);
        } catch (_) { /* non-critical */ }
    }

    function renderSidebar(turns) {
        if (!turns || turns.length === 0) {
            sidebarList.innerHTML = '<p class="sidebar-empty">No history yet.</p>';
            return;
        }
        sidebarList.innerHTML = "";
        // Most-recent first
        [...turns].reverse().forEach((turn, idx) => {
            const item = document.createElement("button");
            item.className = "sidebar-item";
            item.dataset.idx = idx;

            const date = new Date(turn.created_at);
            const timeStr = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            const dateStr = date.toLocaleDateString([], { month: "short", day: "numeric" });

            item.innerHTML = `
                <div class="sidebar-item-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>
                    </svg>
                </div>
                <div class="sidebar-item-content">
                    <div class="sidebar-item-question">${escapeHtml(turn.question)}</div>
                    <div class="sidebar-item-meta">${dateStr} · ${timeStr}</div>
                </div>
                <button class="sidebar-delete-btn" title="Delete" data-id="${turn.id}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="3 6 5 6 21 6"/>
                        <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>
                        <path d="M10 11v6M14 11v6"/>
                        <path d="M9 6V4h6v2"/>
                    </svg>
                </button>
            `;
            item.querySelector(".sidebar-item-icon, .sidebar-item-content")
            item.addEventListener("click", (e) => {
                if (e.target.closest(".sidebar-delete-btn")) return;
                openHistoryModal(turn);
            });
            item.querySelector(".sidebar-delete-btn").addEventListener("click", async (e) => {
                e.stopPropagation();
                await deleteTurn(turn.id);
            });
            sidebarList.appendChild(item);
        });
    }

    // ── Delete turn ──────────────────────────────────────────────────────
    async function deleteTurn(turnId) {
        try {
            await fetch(`/history/${turnId}`, { method: "DELETE" });
            loadSidebarHistory();
        } catch (_) { /* non-critical */ }
    }

    // ── History modal ─────────────────────────────────────────────────────
    function buildModalTable(rows) {
        if (!rows || rows.length === 0) return '<p class="modal-no-data">No data returned.</p>';
        const cols = Object.keys(rows[0]);
        let html = '<div class="modal-table-wrapper"><table class="modal-table"><thead><tr>';
        cols.forEach(c => { html += `<th>${escapeHtml(c)}</th>`; });
        html += "</tr></thead><tbody>";
        rows.forEach(row => {
            html += "<tr>";
            cols.forEach(c => {
                const val = row[c];
                html += `<td>${escapeHtml(val === null || val === undefined ? "NULL" : String(val))}</td>`;
            });
            html += "</tr>";
        });
        html += "</tbody></table></div>";
        if (rows.length === 200) {
            html += '<p class="modal-no-data" style="margin-top:0.5rem;">Showing first 200 rows.</p>';
        }
        return html;
    }

    function openHistoryModal(turn) {
        modalTitle.textContent = turn.question.length > 60
            ? turn.question.slice(0, 60) + "…"
            : turn.question;

        const date = new Date(turn.created_at);
        const timeStr = date.toLocaleString();

        const rowCount = turn.query_result ? turn.query_result.length : 0;
        const rowLabel = rowCount === 1 ? "1 row" : `${rowCount} rows`;

        modalBody.innerHTML = `
            <div class="modal-section">
                <span class="modal-section-label">Question</span>
                <div class="modal-section-content">${escapeHtml(turn.question)}</div>
            </div>
            ${turn.sql_query ? `
            <div class="modal-section">
                <span class="modal-section-label">Generated SQL</span>
                <div class="modal-section-content modal-sql">${escapeHtml(turn.sql_query)}</div>
            </div>` : ""}
            <div class="modal-section">
                <span class="modal-section-label">Query Result${turn.query_result ? ` · ${rowLabel}` : ""}</span>
                ${buildModalTable(turn.query_result)}
            </div>
            <div class="modal-section">
                <span class="modal-section-label">AI Explanation</span>
                <div class="modal-section-content">${escapeHtml(turn.answer)}</div>
            </div>
            <div class="modal-time">${timeStr}</div>
        `;

        historyModal.classList.remove("hidden");
        document.body.style.overflow = "hidden";
    }

    function closeModal() {
        historyModal.classList.add("hidden");
        document.body.style.overflow = "";
    }

    modalClose.addEventListener("click", closeModal);
    historyModal.addEventListener("click", (e) => {
        if (e.target === historyModal) closeModal();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeModal();
    });

    // Load sidebar on startup
    loadSidebarHistory();

    // ── Model Switcher ───────────────────────────────────────────────────
    modelSwitcher.addEventListener("click", (e) => {
        const btn = e.target.closest(".switcher-btn");
        if (!btn) return;
        modelSwitcher.querySelectorAll(".switcher-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        selectedProvider = btn.dataset.provider;
    });

    // ── Submit ────────────────────────────────────────────────────────────
    submitBtn.addEventListener("click", handleSubmit);
    questionInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSubmit();
        }
    });

    async function handleSubmit() {
        const question = questionInput.value.trim();
        if (!question) return;

        showLoading();
        hideResults();
        hideError();

        try {
            const res = await fetch("/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    question,
                    provider: selectedProvider,
                    conversation_id: conversationId,
                }),
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                throw new Error(err.detail || `HTTP ${res.status}`);
            }

            const data = await res.json();
            renderResults(data);
            loadSidebarHistory();   // refresh sidebar after each answer
        } catch (err) {
            showError(err.message || "Something went wrong. Please try again.");
        } finally {
            hideLoading();
        }
    }

    // ── Render Results ───────────────────────────────────────────────────
    function renderResults(data) {
        // SQL
        sqlOutput.textContent = data.sql || "(no SQL generated)";

        // Data table
        if (data.data && data.data.length > 0) {
            rowCount.textContent = `${data.data.length} row${data.data.length !== 1 ? "s" : ""}`;
            tableWrapper.innerHTML = buildTable(data.data);
        } else {
            rowCount.textContent = "0 rows";
            tableWrapper.innerHTML = '<p style="padding:1rem;color:var(--text-muted);">No data returned.</p>';
        }

        // Answer
        answerOutput.textContent = data.answer || "";

        // Insights
        insightsOutput.textContent = data.insights || "";

        resultsSection.classList.remove("hidden");
    }

    function buildTable(rows) {
        if (!rows.length) return "";
        const cols = Object.keys(rows[0]);
        // Limit display to 200 rows
        const displayRows = rows.slice(0, 200);
        let html = "<table><thead><tr>";
        cols.forEach(c => { html += `<th>${escapeHtml(c)}</th>`; });
        html += "</tr></thead><tbody>";
        displayRows.forEach(row => {
            html += "<tr>";
            cols.forEach(c => {
                const val = row[c];
                html += `<td>${escapeHtml(val === null ? "NULL" : String(val))}</td>`;
            });
            html += "</tr>";
        });
        html += "</tbody></table>";
        if (rows.length > 200) {
            html += `<p style="padding:0.75rem 1rem;color:var(--text-muted);font-size:0.8rem;">Showing 200 of ${rows.length} rows</p>`;
        }
        return html;
    }

    // ── Copy SQL ─────────────────────────────────────────────────────────
    copySqlBtn.addEventListener("click", () => {
        const sql = sqlOutput.textContent;
        navigator.clipboard.writeText(sql).then(() => {
            copySqlBtn.style.color = "var(--accent-emerald)";
            setTimeout(() => { copySqlBtn.style.color = ""; }, 1200);
        });
    });

    // ── Loading animation ────────────────────────────────────────────────
    function showLoading() {
        loadingIndicator.classList.remove("hidden");
        submitBtn.disabled = true;
        animateLoadingSteps();
    }

    function hideLoading() {
        loadingIndicator.classList.add("hidden");
        submitBtn.disabled = false;
        if (loadingStepTimer) clearInterval(loadingStepTimer);
    }

    function animateLoadingSteps() {
        const steps = loadingIndicator.querySelectorAll(".step");
        let idx = 0;
        steps.forEach(s => s.classList.remove("active"));
        if (steps.length) steps[0].classList.add("active");

        loadingStepTimer = setInterval(() => {
            steps.forEach(s => s.classList.remove("active"));
            idx = (idx + 1) % steps.length;
            steps[idx].classList.add("active");
        }, 2000);
    }

    // ── Visibility helpers ───────────────────────────────────────────────
    function hideResults() { resultsSection.classList.add("hidden"); }
    function hideError()   { errorSection.classList.add("hidden"); }

    function showError(msg) {
        errorOutput.textContent = msg;
        errorSection.classList.remove("hidden");
    }

    // ── Escape HTML ──────────────────────────────────────────────────────
    function escapeHtml(str) {
        const div = document.createElement("div");
        div.appendChild(document.createTextNode(str));
        return div.innerHTML;
    }
})();
