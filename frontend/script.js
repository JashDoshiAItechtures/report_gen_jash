/* ═══════════════════════════════════════════════════════════════════════════
   AI SQL Analyst — Chat Interface
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
    "use strict";

    // ── DOM refs ───────────────────────────────────────────────────────────
    const questionInput  = document.getElementById("questionInput");
    const submitBtn      = document.getElementById("submitBtn");
    const chatThread     = document.getElementById("chatThread");
    const welcomeState   = document.getElementById("welcomeState");
    const sidebar        = document.getElementById("sidebar");
    const sidebarList    = document.getElementById("sidebarList");
    const sidebarToggle  = document.getElementById("sidebarToggle");
    const newChatBtn     = document.getElementById("newChatBtn");
    const modelSwitcher  = document.getElementById("modelSwitcher");
    const modeSwitcher    = document.getElementById("modeSwitcher");
    const welcomeChips   = document.getElementById("welcomeChips");
    const welcomeChipsReport = document.getElementById("welcomeChipsReport");
    const topbarTitle    = document.getElementById("topbarTitle");

    let selectedProvider = "groq";
    let isLoading        = false;
    let currentMode      = "chat"; // "chat" | "report"

    // ── Report modification state ─────────────────────────────────────
    let latestReportData = null;   // Latest report JSON for modifications
    let latestReportId   = null;   // sessionStorage key for the report
    let reportWindow     = null;   // Reference to the report tab

    // ── Modification Detection ───────────────────────────────────────
    function isModificationCommand(text) {
        const q = text.toLowerCase().trim();

        // Keyword-based detection
        const modKeywords = [
            "change", "replace", "swap", "switch", "modify", "update", "edit",
            "add a kpi", "add kpi", "add a chart", "add chart", "add one more",
            "remove the", "remove kpi", "remove chart", "delete the", "delete kpi",
            "rename", "make it", "convert to", "convert the", "turn into", "turn the",
            "to a bar", "to bar", "to a pie", "to pie", "to a line", "to line",
            "to a line graph", "to line graph", "to a bar chart", "to a bar graph",
            "to doughnut", "to a doughnut", "to area", "to an area", "to horizontal",
            "to a stacked", "to stacked", "to a scatter",
            "instead of", "more kpi", "another kpi", "another chart",
            "change color", "change the color", "update the title",
            "make the", "set the", "show it as", "display as", "show as",
        ];
        if (modKeywords.some(kw => q.includes(kw))) return true;

        // Regex-based detection — catches 'change the X chart to Y' style commands
        const modPatterns = [
            /\bchange\b.+\b(chart|graph|kpi|metric|plot|title|color|legend)\b/i,
            /\b(convert|turn|switch|transform)\b.+\b(chart|graph|kpi|plot)\b/i,
            /\b(pie|bar|line|doughnut|area|horizontal|stacked)\b.+\b(chart|graph)\b.+\b(to|into|as)\b/i,
            /\bto\s+a?\s*(bar|line|pie|doughnut|area|horizontalbar|stackedbar)\b/i,
            /\b(remove|delete|hide|drop)\b.+\b(chart|kpi|metric|graph|insight)\b/i,
            /\badd\b.+\b(chart|kpi|metric|graph|insight)\b/i,
            /\b(rename|relabel|retitle)\b/i,
        ];
        return modPatterns.some(p => p.test(q));
    }

    // ── Strip executed data from report before sending to LLM ────────
    function stripReportData(report) {
        const clean = JSON.parse(JSON.stringify(report)); // deep clone
        // Strip KPI values (LLM only needs SQL + structure)
        if (clean.kpis) {
            clean.kpis.forEach(kpi => {
                delete kpi.value;
                delete kpi.error;
            });
        }
        // Strip chart data (LLM only needs SQL + type + config)
        if (clean.charts) {
            clean.charts.forEach(chart => {
                delete chart.data;
                delete chart.error;
            });
        }
        // Strip table data
        if (clean.table) {
            delete clean.table.data;
            delete clean.table.error;
        }
        return clean;
    }

    // ── Theme ──────────────────────────────────────────────────────────────
    const themeSwitcher = document.getElementById("themeSwitcher");

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        localStorage.setItem("sqlbot_theme", theme);
        themeSwitcher.querySelectorAll(".switcher-btn").forEach(b => {
            b.classList.toggle("active", b.dataset.theme === theme);
        });
    }

    // Apply saved or system preference on load
    const savedTheme = localStorage.getItem("sqlbot_theme") ||
        (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    applyTheme(savedTheme);

    themeSwitcher.addEventListener("click", e => {
        const btn = e.target.closest(".switcher-btn");
        if (btn) applyTheme(btn.dataset.theme);
    });

    // ── Conversation management ────────────────────────────────────────────
    // Each conversation: { id, title, created_at, mode }
    function getAllConversations() {
        try { return JSON.parse(localStorage.getItem("sqlbot_conversations") || "[]"); }
        catch { return []; }
    }
    function getConversations() {
        // Return only conversations for the current mode
        return getAllConversations().filter(c => (c.mode || "chat") === currentMode);
    }
    function saveConversations(list) {
        localStorage.setItem("sqlbot_conversations", JSON.stringify(list));
    }

    // ── Per-mode conversation IDs (chat and report are fully separate) ────
    function convStorageKey(mode) {
        return "sqlbot_conversation_id_" + (mode || currentMode);
    }

    let currentConvId = localStorage.getItem(convStorageKey("chat")) || newConvId();

    function newConvId() {
        return (window.crypto && window.crypto.randomUUID)
            ? window.crypto.randomUUID()
            : "conv-" + Date.now().toString(36);
    }

    function setCurrentConv(id) {
        currentConvId = id;
        localStorage.setItem(convStorageKey(currentMode), id);
    }

    function addConversationToList(id, title) {
        const list = getAllConversations();
        if (!list.find(c => c.id === id)) {
            list.unshift({ id, title, created_at: new Date().toISOString(), mode: currentMode });
            saveConversations(list);
        }
        renderSidebarList();
    }

    function updateConversationTitle(id, title) {
        const list = getAllConversations();
        const conv = list.find(c => c.id === id);
        if (conv && conv.title !== title) {
            conv.title = title;
            saveConversations(list);
            renderSidebarList();
        }
    }

    // ── Sidebar ────────────────────────────────────────────────────────────
    let sidebarOpen = true;

    function setSidebar(open) {
        sidebarOpen = open;
        sidebar.classList.toggle("collapsed", !open);
    }

    sidebarToggle.addEventListener("click", () => setSidebar(!sidebarOpen));

    function renderSidebarList() {
        const list = getConversations();
        if (!list.length) {
            sidebarList.innerHTML = '<p class="sidebar-empty">No conversations yet.</p>';
            return;
        }
        sidebarList.innerHTML = "";
        list.forEach(conv => {
            const item = document.createElement("button");
            item.className = "sidebar-item" + (conv.id === currentConvId ? " active" : "");
            item.innerHTML = `
                <div class="sidebar-item-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>
                    </svg>
                </div>
                <div class="sidebar-item-content">
                    <div class="sidebar-item-question">${escapeHtml(conv.title || "New chat")}</div>
                    <div class="sidebar-item-meta">${formatDate(conv.created_at)}</div>
                </div>
                <button class="sidebar-delete-btn" title="Delete conversation" data-id="${conv.id}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="3 6 5 6 21 6"/>
                        <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/>
                        <path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/>
                    </svg>
                </button>
            `;
            item.addEventListener("click", e => {
                if (e.target.closest(".sidebar-delete-btn")) return;
                loadConversation(conv.id, conv.title);
            });
            item.querySelector(".sidebar-delete-btn").addEventListener("click", async e => {
                e.stopPropagation();
                await deleteConversation(conv.id);
            });
            sidebarList.appendChild(item);
        });
    }

    async function deleteConversation(id) {
        // Delete all turns for this conversation from DB
        try {
            const res = await fetch(`/history?conversation_id=${encodeURIComponent(id)}`);
            if (res.ok) {
                const turns = await res.json();
                for (const t of turns) {
                    await fetch(`/history/${t.id}`, { method: "DELETE" });
                }
            }
        } catch (_) {}

        // Remove from localStorage
        const list = getAllConversations().filter(c => c.id !== id);
        saveConversations(list);

        // If we deleted the active one, start a new chat
        if (id === currentConvId) {
            startNewChat();
        } else {
            renderSidebarList();
        }
    }

    async function loadConversation(id, title) {
        setCurrentConv(id);
        topbarTitle.textContent = title || "Chat";
        clearChatThread();

        try {
            const res = await fetch(`/history?conversation_id=${encodeURIComponent(id)}`);
            if (!res.ok) return;
            const turns = await res.json();
            if (turns.length > 0) {
                hideWelcome();
                turns.forEach(t => appendTurn(t.question, t.answer, t.sql_query, t.query_result));
            } else {
                showWelcome();
            }
        } catch (_) { showWelcome(); }

        renderSidebarList();
        scrollToBottom();
    }

    // ── New Chat ───────────────────────────────────────────────────────────
    newChatBtn.addEventListener("click", startNewChat);

    function startNewChat() {
        const id = newConvId();
        setCurrentConv(id);
        topbarTitle.textContent = "New Chat";
        clearChatThread();
        showWelcome();
        questionInput.value = "";
        questionInput.style.height = "";
        renderSidebarList();
        // Clear report modification state
        latestReportData = null;
        latestReportId = null;
    }

    // ── Welcome chips ──────────────────────────────────────────────────────
    document.querySelectorAll(".chip").forEach(chip => {
        chip.addEventListener("click", () => {
            // Auto-switch mode based on chip type
            const isReport = chip.classList.contains("chip-report");
            if (isReport && currentMode !== "report") {
                currentMode = "report";
                if (modeSwitcher) {
                    modeSwitcher.querySelectorAll(".switcher-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === "report"));
                }
                questionInput.placeholder = "Describe the report you want...";
                // Ensure per-mode conversation is set
                if (!localStorage.getItem(convStorageKey("report"))) {
                    setCurrentConv(newConvId());
                }
            } else if (!isReport && currentMode !== "chat") {
                currentMode = "chat";
                if (modeSwitcher) {
                    modeSwitcher.querySelectorAll(".switcher-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === "chat"));
                }
                questionInput.placeholder = "Ask a question about your data...";
                if (!localStorage.getItem(convStorageKey("chat"))) {
                    setCurrentConv(newConvId());
                }
            }
            questionInput.value = chip.dataset.q;
            questionInput.dispatchEvent(new Event("input"));
            handleSubmit();
        });
    });

    // ── Model switcher ─────────────────────────────────────────────────────
    modelSwitcher.addEventListener("click", e => {
        const btn = e.target.closest(".switcher-btn");
        if (!btn) return;
        modelSwitcher.querySelectorAll(".switcher-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        selectedProvider = btn.dataset.provider;
    });

    // ── Mode switcher ──────────────────────────────────────────────────────

    if (modeSwitcher) {
        modeSwitcher.addEventListener("click", e => {
            const btn = e.target.closest(".switcher-btn");
            if (!btn) return;
            const newMode = btn.dataset.mode;
            if (newMode === currentMode) return; // no change

            modeSwitcher.querySelectorAll(".switcher-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            // ── Save current mode's conversation ID before switching ──
            localStorage.setItem(convStorageKey(currentMode), currentConvId);

            currentMode = newMode;

            // ── Restore the previous conversation for the new mode ────
            const savedId = localStorage.getItem(convStorageKey(newMode));
            if (savedId) {
                // Find the conversation to get its title
                const convList = getAllConversations();
                const conv = convList.find(c => c.id === savedId);
                loadConversation(savedId, conv ? conv.title : "Chat");
            } else {
                // First time entering this mode — start fresh
                startNewChat();
            }

            // Toggle welcome chips & placeholder
            if (currentMode === "report") {
                if (welcomeChips) welcomeChips.classList.add("hidden");
                if (welcomeChipsReport) welcomeChipsReport.classList.remove("hidden");
                questionInput.placeholder = "Describe the report you want (e.g., sales performance analysis, top products by revenue...)";
            } else {
                if (welcomeChips) welcomeChips.classList.remove("hidden");
                if (welcomeChipsReport) welcomeChipsReport.classList.add("hidden");
                questionInput.placeholder = "Ask a question about your data...";
            }
        });
    }

    // ── Auto-resize textarea ───────────────────────────────────────────────
    questionInput.addEventListener("input", () => {
        questionInput.style.height = "auto";
        questionInput.style.height = Math.min(questionInput.scrollHeight, 160) + "px";
    });

    // ── Submit ─────────────────────────────────────────────────────────────
    submitBtn.addEventListener("click", handleSubmit);
    questionInput.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            handleSubmit();
        }
    });

    async function handleSubmit() {
        const question = questionInput.value.trim();
        if (!question || isLoading) return;

        isLoading = true;
        submitBtn.disabled = true;

        // Hide welcome, show user message immediately
        hideWelcome();
        appendUserMessage(question);
        questionInput.value = "";
        questionInput.style.height = "";
        scrollToBottom();

        // ── Report Mode: generate or modify ──
        if (currentMode === "report") {
            const typingEl = appendTypingIndicator();
            scrollToBottom();

            // Allow mode switching while report generates
            isLoading = false;
            submitBtn.disabled = false;

            // Capture the conversation context so mode-switching doesn't break it
            const capturedConvId = currentConvId;

            // ── Smart detection: is this a MODIFICATION or a NEW report? ──
            const isModification = latestReportData !== null && isModificationCommand(question);

            let fetchPromise;

            if (isModification) {
                // ── Modify the existing report ──
                // Strip executed data to keep payload small for the LLM
                const cleanReport = stripReportData(latestReportData);
                fetchPromise = fetch("/report/modify", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        report_json: JSON.stringify(cleanReport),
                        modification: question,
                        provider: selectedProvider,
                    }),
                });
            } else {
                // ── Generate a new report ──
                // Clear any previous report state
                latestReportData = null;
                latestReportId = null;
                reportWindow = null;
                fetchPromise = fetch("/report", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ question, provider: selectedProvider }),
                });
            }

            fetchPromise
            .then(res => {
                if (!res.ok) throw new Error(isModification ? "Report modification failed" : "Report generation failed");
                return res.json();
            })
            .then(data => {
                if (data.error) {
                    throw new Error(data.error);
                }

                // Store the report data for future modifications
                latestReportData = data.report;

                // Use same report ID if modifying (replaces in sessionStorage)
                const reportId = isModification && latestReportId ? latestReportId : "rpt_" + Date.now();
                latestReportId = reportId;

                localStorage.setItem(reportId, JSON.stringify(data));
                // Preserve the original report question — don't overwrite with the modification command
                if (!isModification) {
                    localStorage.setItem(reportId + "_question", question);
                }
                localStorage.setItem(reportId + "_provider", selectedProvider);
                localStorage.setItem(reportId + "_theme", document.documentElement.getAttribute("data-theme") || "light");

                // Only update UI if still on the same conversation
                if (currentConvId === capturedConvId) {
                    typingEl.remove();
                    if (isModification) {
                        appendAssistantMessage("✅ Report updated successfully! The report tab has been refreshed.");
                    } else {
                        appendReportSuccessMessage(question, reportId);
                    }
                    scrollToBottom();
                } else {
                    typingEl.remove();
                }

                // Open new tab or refresh existing one
                if (isModification && reportWindow && !reportWindow.closed) {
                    reportWindow.location.href = `/report-view?id=${reportId}&t=${Date.now()}`;
                    reportWindow.focus();
                } else {
                    reportWindow = window.open(`/report-view?id=${reportId}`, "_blank");
                }
            })
            .catch(err => {
                if (currentConvId === capturedConvId) {
                    typingEl.remove();
                    appendErrorMessage(err.message || "Report operation failed.");
                    scrollToBottom();
                } else {
                    typingEl.remove();
                }
            });

            // Update sidebar
            const convs = getConversations();
            if (!convs.find(c => c.id === capturedConvId)) {
                addConversationToList(capturedConvId, question);
                topbarTitle.textContent = question.length > 40 ? question.slice(0, 40) + "..." : question;
            }
            return;
        }

        // ── Chat Mode: normal /chat flow ──
        const typingEl = appendTypingIndicator();
        scrollToBottom();

        try {
            const res = await fetch("/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    question,
                    provider: selectedProvider,
                    conversation_id: currentConvId,
                }),
            });

            typingEl.remove();

            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                appendErrorMessage(err.detail || `HTTP ${res.status}`);
            } else {
                const data = await res.json();

                // Check if this is a report intent
                if (data.mode === "report") {
                    appendReportMessage(question, data);
                } else {
                    appendAIMessage(data);
                }

                // Update sidebar: first question becomes the conversation title
                const convs = getConversations();
                if (!convs.find(c => c.id === currentConvId)) {
                    addConversationToList(currentConvId, question);
                    topbarTitle.textContent = question.length > 40 ? question.slice(0, 40) + "..." : question;
                }
            }
        } catch (err) {
            typingEl.remove();
            appendErrorMessage(err.message || "Something went wrong. Please try again.");
        }

        isLoading = false;
        submitBtn.disabled = false;
        scrollToBottom();
    }

    // ── Report message (shows button to open report in new tab) ───────────
    function appendReportMessage(question, chatData) {
        const el = document.createElement("div");
        el.className = "msg msg-ai";

        const reportId = "rpt_" + Date.now();

        el.innerHTML = `
            <div class="ai-avatar">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                    <path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
                </svg>
            </div>
            <div class="ai-body">
                <div class="ai-answer">${escapeHtml(chatData.answer)}</div>
                <div class="report-trigger-card">
                    <div class="report-trigger-icon">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                            <line x1="3" y1="9" x2="21" y2="9"/>
                            <line x1="9" y1="21" x2="9" y2="9"/>
                        </svg>
                    </div>
                    <div class="report-trigger-info">
                        <div class="report-trigger-title">Analytics Report Detected</div>
                        <div class="report-trigger-desc">I can generate a comprehensive dashboard with KPIs, charts, data tables, and insights for your query.</div>
                    </div>
                    <button class="report-trigger-btn" data-report-id="${reportId}" data-question="${escapeHtml(question)}">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                            <line x1="3" y1="9" x2="21" y2="9"/>
                            <line x1="9" y1="21" x2="9" y2="9"/>
                        </svg>
                        Generate Report
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="report-trigger-arrow">
                            <polyline points="9 18 15 12 9 6"/>
                        </svg>
                    </button>
                </div>
            </div>`;

        // Wire up the report button
        const btn = el.querySelector(".report-trigger-btn");
        btn.addEventListener("click", () => {
            generateAndOpenReport(question, reportId, btn);
        });

        chatThread.appendChild(el);
    }

    // ── Report success message (used by report mode) ──────────────────────
    function appendReportSuccessMessage(question, reportId) {
        const el = document.createElement("div");
        el.className = "msg msg-ai";
        el.innerHTML = `
            <div class="ai-avatar">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                    <path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
                </svg>
            </div>
            <div class="ai-body">
                <div class="ai-answer">Your analytics report has been generated and opened in a new tab.</div>
                <div class="report-trigger-card">
                    <div class="report-trigger-icon">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                            <line x1="3" y1="9" x2="21" y2="9"/>
                            <line x1="9" y1="21" x2="9" y2="9"/>
                        </svg>
                    </div>
                    <div class="report-trigger-info">
                        <div class="report-trigger-title">Report Ready</div>
                        <div class="report-trigger-desc">${escapeHtml(question)}</div>
                    </div>
                    <button class="report-trigger-btn report-trigger-btn-success" onclick="window.open('/report-view?id=${reportId}', '_blank')">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                        Open Report
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="report-trigger-arrow"><polyline points="9 18 15 12 9 6"/></svg>
                    </button>
                </div>
            </div>`;
        chatThread.appendChild(el);
    }

    async function generateAndOpenReport(question, reportId, btn) {
        // Disable button and show loading
        btn.disabled = true;
        btn.innerHTML = `
            <div class="report-btn-spinner"></div>
            Generating Report...
        `;

        try {
            const res = await fetch("/report", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    question,
                    provider: selectedProvider,
                }),
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                btn.disabled = false;
                btn.innerHTML = `
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                    Retry Report
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="report-trigger-arrow"><polyline points="9 18 15 12 9 6"/></svg>
                `;
                appendErrorMessage("Report generation failed: " + (err.detail || err.error || "Unknown error"));
                return;
            }

            const reportData = await res.json();

            // Store report data in localStorage (shared across tabs)
            localStorage.setItem(reportId, JSON.stringify(reportData));
            localStorage.setItem(reportId + "_question", question);
            localStorage.setItem(reportId + "_provider", selectedProvider);
            localStorage.setItem(reportId + "_theme", document.documentElement.getAttribute("data-theme") || "light");

            // Update button to "Open Report"
            btn.disabled = false;
            btn.classList.add("report-trigger-btn-success");
            btn.innerHTML = `
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                Open Report
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="report-trigger-arrow"><polyline points="9 18 15 12 9 6"/></svg>
            `;

            // Re-wire to only open the report (not regenerate)
            btn.onclick = () => {
                window.open(`/report-view?id=${reportId}`, "_blank");
            };

            // Auto-open the report
            window.open(`/report-view?id=${reportId}`, "_blank");

        } catch (err) {
            btn.disabled = false;
            btn.innerHTML = `
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                Retry Report
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="report-trigger-arrow"><polyline points="9 18 15 12 9 6"/></svg>
            `;
            appendErrorMessage("Report generation failed: " + err.message);
        }

        scrollToBottom();
    }

    // ── Chat rendering helpers ─────────────────────────────────────────────

    function appendUserMessage(text) {
        const el = document.createElement("div");
        el.className = "msg msg-user";
        el.innerHTML = `<div class="msg-bubble">${escapeHtml(text)}</div>`;
        chatThread.appendChild(el);
    }

    function appendTypingIndicator() {
        const el = document.createElement("div");
        el.className = "msg msg-ai";
        el.innerHTML = `
            <div class="ai-avatar">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                    <path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
                </svg>
            </div>
            <div class="typing-indicator">
                <span></span><span></span><span></span>
            </div>`;
        chatThread.appendChild(el);
        return el;
    }

    function appendAIMessage(data) {
        const el = document.createElement("div");
        el.className = "msg msg-ai";

        const hasData   = data.data && data.data.length > 0;
        const hasSql    = !!data.sql;
        const hasAnswer = !!data.answer;
        const rowLabel  = hasData ? `${data.data.length} row${data.data.length !== 1 ? "s" : ""}` : "0 rows";

        el.innerHTML = `
            <div class="ai-avatar">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                    <path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
                </svg>
            </div>
            <div class="ai-body">
                ${hasAnswer ? `<div class="ai-answer">${escapeHtml(data.answer)}</div>` : ""}
                ${hasSql ? `
                <div class="ai-section">
                    <button class="section-toggle" data-target="sql-${Date.now()}">
                        <span class="section-toggle-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                        </span>
                        SQL Query
                        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="section-body" id="sql-${Date.now()}">
                        <pre class="sql-code"><code>${escapeHtml(data.sql)}</code></pre>
                    </div>
                </div>` : ""}
                ${hasData ? `
                <div class="ai-section">
                    <button class="section-toggle" data-target="tbl-${Date.now()}">
                        <span class="section-toggle-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        </span>
                        Results <span class="row-badge">${rowLabel}</span>
                        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="section-body" id="tbl-${Date.now()}">
                        <div class="table-wrapper">${buildTable(data.data)}</div>
                    </div>
                </div>` : ""}
                ${data.insights ? `
                <div class="ai-section">
                    <button class="section-toggle" data-target="ins-${Date.now()}">
                        <span class="section-toggle-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a7 7 0 017 7c0 2.38-1.19 4.47-3 5.74V17a1 1 0 01-1 1H9a1 1 0 01-1-1v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 017-7z"/><line x1="9" y1="21" x2="15" y2="21"/></svg>
                        </span>
                        Insights
                        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="section-body" id="ins-${Date.now()}">
                        <div class="insights-text">${escapeHtml(data.insights)}</div>
                    </div>
                </div>` : ""}
            </div>`;

        // Wire up section toggles
        // SQL and Results are open by default; Insights is collapsed
        el.querySelectorAll(".section-toggle").forEach(btn => {
            const targetId = btn.dataset.target;
            const body = el.querySelector(`#${targetId}`);
            if (!body) return;

            const isInsights = targetId.startsWith("ins-");
            if (isInsights) {
                body.classList.add("collapsed");
            } else {
                btn.classList.add("open"); // chevron rotated = open
            }

            btn.addEventListener("click", () => {
                body.classList.toggle("collapsed");
                btn.classList.toggle("open");
            });
        });

        chatThread.appendChild(el);
    }

    function appendErrorMessage(msg) {
        const el = document.createElement("div");
        el.className = "msg msg-ai";
        el.innerHTML = `
            <div class="ai-avatar ai-avatar-error">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>
                </svg>
            </div>
            <div class="ai-body">
                <div class="ai-error">${escapeHtml(msg)}</div>
            </div>`;
        chatThread.appendChild(el);
    }

    function appendAssistantMessage(msg) {
        const el = document.createElement("div");
        el.className = "msg msg-ai";
        el.innerHTML = `
            <div class="ai-avatar">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                    <path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
                </svg>
            </div>
            <div class="ai-body">
                <div class="answer-text">${escapeHtml(msg)}</div>
            </div>`;
        chatThread.appendChild(el);
    }

    function appendTurn(question, answer, sql, queryResult) {
        appendUserMessage(question);
        appendAIMessage({
            answer,
            sql: sql || "",
            data: queryResult || [],
            insights: "",
        });
    }

    // ── Table builder ──────────────────────────────────────────────────────
    function buildTable(rows) {
        if (!rows || !rows.length) return '<p class="no-data">No data returned.</p>';
        const cols = Object.keys(rows[0]);
        const display = rows.slice(0, 200);
        let html = "<table><thead><tr>";
        cols.forEach(c => { html += `<th>${escapeHtml(c)}</th>`; });
        html += "</tr></thead><tbody>";
        display.forEach(row => {
            html += "<tr>";
            cols.forEach(c => {
                const v = row[c];
                html += `<td>${escapeHtml(v === null || v === undefined ? "NULL" : String(v))}</td>`;
            });
            html += "</tr>";
        });
        html += "</tbody></table>";
        if (rows.length > 200) {
            html += `<p class="no-data">Showing 200 of ${rows.length} rows</p>`;
        }
        return html;
    }

    // ── Helpers ────────────────────────────────────────────────────────────
    function showWelcome()  { welcomeState.classList.remove("hidden"); }
    function hideWelcome()  { welcomeState.classList.add("hidden"); }
    function clearChatThread() {
        // Remove all msg elements, keep welcome state
        chatThread.querySelectorAll(".msg").forEach(e => e.remove());
    }
    function scrollToBottom() {
        chatThread.scrollTop = chatThread.scrollHeight;
    }
    function formatDate(iso) {
        if (!iso) return "";
        const d = new Date(iso);
        const now = new Date();
        if (d.toDateString() === now.toDateString()) {
            return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        }
        return d.toLocaleDateString([], { month: "short", day: "numeric" });
    }
    function escapeHtml(str) {
        const d = document.createElement("div");
        d.appendChild(document.createTextNode(String(str)));
        return d.innerHTML;
    }

    // ── Startup: load current conversation ────────────────────────────────
    (async function init() {
        renderSidebarList();
        const convs = getConversations();
        const existing = convs.find(c => c.id === currentConvId);

        if (existing) {
            topbarTitle.textContent = existing.title || "Chat";
            try {
                const res = await fetch(`/history?conversation_id=${encodeURIComponent(currentConvId)}`);
                if (res.ok) {
                    const turns = await res.json();
                    if (turns.length > 0) {
                        hideWelcome();
                        turns.forEach(t => appendTurn(t.question, t.answer, t.sql_query, t.query_result));
                        scrollToBottom();
                    }
                }
            } catch (_) {}
        } else {
            showWelcome();
        }
    })();

})();
