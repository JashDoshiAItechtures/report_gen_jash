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
