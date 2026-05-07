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
    const welcomeChips   = document.getElementById("welcomeChips");
    const topbarTitle    = document.getElementById("topbarTitle");

    let selectedProvider = "claude";
    let isLoading        = false;
    const currentMode    = "chat"; // Always chat — unified mode

    // ── Report state ──────────────────────────────────────────────────
    let latestReportData = null;   // Latest report JSON for modifications
    let latestReportId   = null;   // sessionStorage key for the report
    let reportWindow     = null;   // Reference to the report tab
    let lastChatQuestion = null;   // Track last question for "yes" report flow

    // ── Sync helper: persist report state so @ mentions survive refresh ──
    function _syncReportState(reportData, reportId) {
        latestReportData = reportData;
        latestReportId = reportId;
        if (reportId) {
            localStorage.setItem("sqlbot_latestReportId", reportId);
        }
        // Also persist to sessionStorage so _tryRestoreReportData works after refresh
        if (reportId && reportData) {
            try {
                const existing = sessionStorage.getItem(reportId);
                if (existing) {
                    const parsed = JSON.parse(existing);
                    parsed.report = reportData;
                    sessionStorage.setItem(reportId, JSON.stringify(parsed));
                } else {
                    sessionStorage.setItem(reportId, JSON.stringify({ report: reportData }));
                }
            } catch (_) {}
        }
        console.log('[SYNC] _syncReportState called. reportId=', reportId,
            'kpis=', (reportData?.kpis || []).map(k => k.label),
            'charts=', (reportData?.charts || []).map(c => c.title));
    }

    // ── Modification Detection ───────────────────────────────────────
    function isModificationCommand(text) {
        const q = text.toLowerCase().trim();

        // Keyword-based detection — single tokens or short phrases
        const modKeywords = [
            "change", "replace", "swap", "switch", "modify", "update", "edit",
            "add a kpi", "add kpi", "add a chart", "add chart", "add one more",
            "remove the", "remove kpi", "remove chart", "delete the", "delete kpi",
            "rename", "make it", "convert to", "convert the", "turn into", "turn the",
            "to a bar", "to bar", "to a pie", "to pie", "to a line", "to line",
            "to a line graph", "to line graph", "to a bar chart", "to a bar graph",
            "to doughnut", "to a doughnut", "to area", "to an area", "to horizontal",
            "to a stacked", "to stacked", "to a scatter", "to radar", "to polar",
            "instead of", "more kpi", "another kpi", "another chart",
            "change color", "change the color", "update the title",
            "make the", "set the", "show it as", "display as", "show as",
        ];
        if (modKeywords.some(kw => q.includes(kw))) return true;

        // Regex-based detection — catches 'change the X chart to Y' style commands
        const modPatterns = [
            /\bchange\b.+\b(chart|graph|kpi|metric|plot|title|color|legend)\b/i,
            /\b(convert|turn|switch|transform)\b.+\b(chart|graph|kpi|plot|to)\b/i,
            /\b(pie|bar|line|doughnut|area|horizontal|stacked)\b.+\b(chart|graph)\b.+\b(to|into|as)\b/i,
            /\bto\s+a?\s*(bar|line|pie|doughnut|area|horizontalbar|stackedbar|scatter|radar|polar)\b/i,
            /\b(remove|delete|hide|drop)\b.+\b(chart|kpi|metric|graph|insight)\b/i,
            /\badd\b.+\b(chart|kpi|metric|graph|insight)\b/i,
            /\b(rename|relabel|retitle)\b/i,
            // Name-based chart/kpi targeting — "the X chart to Y" or "X kpi to Y"
            /\bchart\s+to\s+/i,
            /\bkpi\s+to\s+/i,
            /\bgraph\s+to\s+/i,
            // "change X to bar" — change + anything + to + chart type
            /\bchange\b.+\bto\s+a?\s*(bar|line|pie|doughnut|area|horizontal|stacked|scatter)\b/i,
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
        // Unified mode — show all conversations
        return getAllConversations();
    }
    function saveConversations(list) {
        localStorage.setItem("sqlbot_conversations", JSON.stringify(list));
    }

    // ── Conversation ID storage key ────────────────────────────────────────
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

        // Reset report state — each conversation is independent
        latestReportData = null;
        latestReportId = null;
        lastChatQuestion = null;

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
        // Clear all report / chat state
        latestReportData = null;
        latestReportId = null;
        lastChatQuestion = null;
    }

    // ── Welcome chips ──────────────────────────────────────────────────────
    document.querySelectorAll(".chip").forEach(chip => {
        chip.addEventListener("click", () => {
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

    // ── Client-side intent detection ───────────────────────────────────────
    function isReportIntent(text) {
        const q = text.toLowerCase().trim();
        // Quick check: any query containing "report" or "dashboard" as a word
        if (/\breport\b/.test(q) || /\bdashboard\b/.test(q)) return true;
        // Fallback: explicit phrases
        const reportKeywords = [
            "generate analysis", "create analysis", "analytics overview",
            "comprehensive analysis", "detailed analysis", "full analysis",
            "performance analysis", "give me an analysis", "show me an analysis",
        ];
        return reportKeywords.some(kw => q.includes(kw));
    }

    function isAffirmativeResponse(text) {
        const q = text.toLowerCase().trim();
        const yesPatterns = [
            "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go ahead",
            "do it", "please", "yes please", "generate it", "yes generate",
            "make it", "absolutely", "of course", "definitely",
        ];
        // Must be short and match closely (to avoid false positives on long messages)
        return q.split(/\s+/).length <= 5 && yesPatterns.some(p => q === p || q.startsWith(p + " ") || q.startsWith(p + ","));
    }

    // ── Auto-resize textarea ───────────────────────────────────────────────
    questionInput.addEventListener("input", () => {
        questionInput.style.height = "auto";
        questionInput.style.height = Math.min(questionInput.scrollHeight, 160) + "px";
        _mHandleInput();
    });

    // ── Submit ─────────────────────────────────────────────────────────────
    submitBtn.addEventListener("click", handleSubmit);
    questionInput.addEventListener("keydown", e => {
        // @mention dropdown takes priority over submit
        if (_mDrop && _mDrop.classList.contains("visible")) {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                _mHlIdx = Math.min(_mHlIdx + 1, _mItems.length - 1);
                _mRefreshHl(); return;
            }
            if (e.key === "ArrowUp") {
                e.preventDefault();
                _mHlIdx = Math.max(_mHlIdx - 1, 0);
                _mRefreshHl(); return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
                if (_mItems[_mHlIdx]) { e.preventDefault(); _mInsert(_mHlIdx); }
                return;
            }
            if (e.key === "Escape") { _mHide(); return; }
        }
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

        // ── Route 1: Report modification command ──
        if (latestReportData !== null && isModificationCommand(question)) {
            lastChatQuestion = null; // Reset — modification is not a chat question
            const typingEl = appendTypingIndicator();
            scrollToBottom();

            try {
                const cleanReport = stripReportData(latestReportData);
                const res = await fetch("/report/modify", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        report_json: JSON.stringify(cleanReport),
                        modification: question,
                        provider: selectedProvider,
                    }),
                });
                typingEl.remove();
                if (!res.ok) throw new Error("Report modification failed");
                const data = await res.json();
                if (data.error) throw new Error(data.error);

                const reportId = latestReportId || ("rpt_" + Date.now());
                _syncReportState(data.report, reportId);

                sessionStorage.setItem(reportId, JSON.stringify(data));
                sessionStorage.setItem(reportId + "_provider", selectedProvider);
                sessionStorage.setItem(reportId + "_theme", document.documentElement.getAttribute("data-theme") || "light");

                appendAssistantMessage("✅ Report updated successfully! The report tab has been refreshed.");

                if (reportWindow && !reportWindow.closed) {
                    reportWindow.location.href = `/report-view?id=${reportId}&t=${Date.now()}`;
                    reportWindow.focus();
                } else {
                    reportWindow = window.open(`/report-view?id=${reportId}`, "_blank");
                }
            } catch (err) {
                typingEl.remove();
                appendErrorMessage(err.message || "Report modification failed.");
            }
            isLoading = false;
            submitBtn.disabled = false;
            scrollToBottom();
            return;
        }

        // ── Route 2: Affirmative response to report offer ──
        if (lastChatQuestion && isAffirmativeResponse(question)) {
            const reportQuestion = lastChatQuestion;
            lastChatQuestion = null; // consume it
            const typingEl = appendTypingIndicator();
            scrollToBottom();

            try {
                const res = await fetch("/report", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ question: reportQuestion, provider: selectedProvider }),
                });
                typingEl.remove();
                if (!res.ok) throw new Error("Report generation failed");
                const data = await res.json();
                if (data.error) throw new Error(data.error);

                const reportId = "rpt_" + Date.now();
                _syncReportState(data.report, reportId);

                sessionStorage.setItem(reportId, JSON.stringify(data));
                sessionStorage.setItem(reportId + "_question", reportQuestion);
                sessionStorage.setItem(reportId + "_provider", selectedProvider);
                sessionStorage.setItem(reportId + "_theme", document.documentElement.getAttribute("data-theme") || "light");

                appendReportSuccessMessage(reportQuestion, reportId);
                reportWindow = window.open(`/report-view?id=${reportId}`, "_blank");
            } catch (err) {
                typingEl.remove();
                appendErrorMessage(err.message || "Report generation failed.");
            }
            // Update sidebar for Route 2
            const convs2 = getConversations();
            if (!convs2.find(c => c.id === currentConvId)) {
                addConversationToList(currentConvId, reportQuestion);
                topbarTitle.textContent = reportQuestion.length > 40 ? reportQuestion.slice(0, 40) + "..." : reportQuestion;
            }

            isLoading = false;
            submitBtn.disabled = false;
            scrollToBottom();
            return;
        }

        // ── Route 3: Direct report intent ──
        if (isReportIntent(question)) {
            const typingEl = appendTypingIndicator();
            scrollToBottom();

            try {
                const res = await fetch("/report", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ question, provider: selectedProvider }),
                });
                typingEl.remove();
                if (!res.ok) throw new Error("Report generation failed");
                const data = await res.json();
                if (data.error) throw new Error(data.error);

                const reportId = "rpt_" + Date.now();
                _syncReportState(data.report, reportId);

                sessionStorage.setItem(reportId, JSON.stringify(data));
                sessionStorage.setItem(reportId + "_question", question);
                sessionStorage.setItem(reportId + "_provider", selectedProvider);
                sessionStorage.setItem(reportId + "_theme", document.documentElement.getAttribute("data-theme") || "light");

                appendReportSuccessMessage(question, reportId);
                reportWindow = window.open(`/report-view?id=${reportId}`, "_blank");
            } catch (err) {
                typingEl.remove();
                appendErrorMessage(err.message || "Report generation failed.");
            }

            // Update sidebar
            const convs = getConversations();
            if (!convs.find(c => c.id === currentConvId)) {
                addConversationToList(currentConvId, question);
                topbarTitle.textContent = question.length > 40 ? question.slice(0, 40) + "..." : question;
            }

            isLoading = false;
            submitBtn.disabled = false;
            scrollToBottom();
            return;
        }

        // ── Route 4: Normal chat flow (SSE streaming) ──
        lastChatQuestion = question; // Track for potential "yes" follow-up
        const streamingEl = appendStreamingStatus();
        scrollToBottom();

        try {
            const res = await fetch("/chat/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    question,
                    provider: selectedProvider,
                    conversation_id: currentConvId,
                }),
            });

            if (!res.ok) {
                streamingEl.remove();
                const err = await res.json().catch(() => ({ detail: res.statusText }));
                appendErrorMessage(err.detail || `HTTP ${res.status}`);
            } else {
                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";
                let finalData = null;

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split("\n\n");
                    buffer = lines.pop() || "";

                    for (const line of lines) {
                        if (!line.startsWith("data: ")) continue;
                        try {
                            const event = JSON.parse(line.slice(6));
                            if (event.stage === "complete") {
                                finalData = event.data;
                            } else {
                                // Update the streaming status text
                                updateStreamingStatus(streamingEl, event.text);
                            }
                        } catch (_) {}
                    }
                }

                streamingEl.remove();

                if (finalData) {
                    appendAIMessage(finalData);

                    if (finalData.report_eligible) {
                        appendReportOfferCard(question);
                    }

                    const convs = getConversations();
                    if (!convs.find(c => c.id === currentConvId)) {
                        addConversationToList(currentConvId, question);
                        topbarTitle.textContent = question.length > 40 ? question.slice(0, 40) + "..." : question;
                    }
                } else {
                    appendErrorMessage("No response received from server.");
                }
            }
        } catch (err) {
            streamingEl.remove();
            appendErrorMessage(err.message || "Something went wrong. Please try again.");
        }

        isLoading = false;
        submitBtn.disabled = false;
        scrollToBottom();
    }

    // ── Report offer card (shown after chat response) ─────────────────────
    function appendReportOfferCard(question) {
        const el = document.createElement("div");
        el.className = "msg msg-report-offer";
        const reportId = "rpt_" + Date.now();
        el.innerHTML = `
            <div class="report-trigger-card report-offer-inline">
                <div class="report-trigger-icon">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                        <line x1="3" y1="9" x2="21" y2="9"/>
                        <line x1="9" y1="21" x2="9" y2="9"/>
                    </svg>
                </div>
                <div class="report-trigger-info">
                    <div class="report-trigger-title">Want a detailed analytics report?</div>
                    <div class="report-trigger-desc">I can generate a comprehensive dashboard with KPIs, charts, data tables, and insights for this query.</div>
                </div>
                <button class="report-trigger-btn" data-report-id="${reportId}" data-question="${escapeHtml(question)}">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                        <line x1="3" y1="9" x2="21" y2="9"/>
                        <line x1="9" y1="21" x2="9" y2="9"/>
                    </svg>
                    Generate Report
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="report-trigger-arrow"><polyline points="9 18 15 12 9 6"/></svg>
                </button>
            </div>`;

        const btn = el.querySelector(".report-trigger-btn");
        btn.addEventListener("click", () => {
            generateAndOpenReport(question, reportId, btn);
        });

        chatThread.appendChild(el);
        scrollToBottom();
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

            // Store report data in sessionStorage (for report-view page)
            sessionStorage.setItem(reportId, JSON.stringify(reportData));
            sessionStorage.setItem(reportId + "_question", question);
            sessionStorage.setItem(reportId + "_provider", selectedProvider);
            sessionStorage.setItem(reportId + "_theme", document.documentElement.getAttribute("data-theme") || "light");

            // Store report state for future modifications (chat-side)
            _syncReportState(reportData.report, reportId);

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
            reportWindow = window.open(`/report-view?id=${reportId}`, "_blank");

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

    function appendStreamingStatus() {
        const el = document.createElement("div");
        el.className = "msg msg-ai";
        el.innerHTML = `
            <div class="ai-avatar">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                    <path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
                </svg>
            </div>
            <div class="streaming-status">
                <div class="streaming-spinner"></div>
                <span class="streaming-text">Analyzing your question...</span>
            </div>`;
        chatThread.appendChild(el);
        return el;
    }

    function updateStreamingStatus(el, text) {
        const textEl = el.querySelector(".streaming-text");
        if (textEl) {
            textEl.style.opacity = "0";
            setTimeout(() => {
                textEl.textContent = text;
                textEl.style.opacity = "1";
            }, 150);
        }
    }

    let _sectionIdCounter = 0; // Unique ID counter for section toggles

    function appendAIMessage(data) {
        const el = document.createElement("div");
        el.className = "msg msg-ai";

        const hasData   = data.data && data.data.length > 0;
        const hasSql    = !!data.sql;
        const hasAnswer = !!data.answer;
        const rowLabel  = hasData ? `${data.data.length} row${data.data.length !== 1 ? "s" : ""}` : "0 rows";

        // Use unique IDs per section (Date.now() can collide within the same template)
        const sid = ++_sectionIdCounter;
        const sqlId = `sql-${sid}`;
        const tblId = `tbl-${sid}`;
        const insId = `ins-${sid}`;

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
                    <button class="section-toggle" data-target="${sqlId}">
                        <span class="section-toggle-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                        </span>
                        SQL Query
                        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="section-body" id="${sqlId}">
                        <pre class="sql-code"><code>${escapeHtml(data.sql)}</code></pre>
                    </div>
                </div>` : ""}
                ${hasData ? `
                <div class="ai-section">
                    <button class="section-toggle" data-target="${tblId}">
                        <span class="section-toggle-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
                        </span>
                        Results <span class="row-badge">${rowLabel}</span>
                        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="section-body" id="${tblId}">
                        <div class="table-wrapper">${buildTable(data.data)}</div>
                    </div>
                </div>` : ""}
                ${data.insights ? `
                <div class="ai-section">
                    <button class="section-toggle" data-target="${insId}">
                        <span class="section-toggle-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a7 7 0 017 7c0 2.38-1.19 4.47-3 5.74V17a1 1 0 01-1 1H9a1 1 0 01-1-1v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 017-7z"/><line x1="9" y1="21" x2="15" y2="21"/></svg>
                        </span>
                        Insights
                        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="section-body" id="${insId}">
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

    // ── @Mention Autocomplete ──────────────────────────────────────────────
    const _mDrop = (() => {
        const el = document.createElement("div");
        el.className = "mention-dropdown";
        document.body.appendChild(el);
        return el;
    })();
    let _mAtPos = -1, _mQuery = "", _mItems = [], _mHlIdx = 0;

    function _mGetComponents(filter) {
        if (!latestReportData) {
            console.log('[MENTION] _mGetComponents: latestReportData is null/undefined');
            return [];
        }
        const f = (filter || "").toLowerCase();
        const items = [];
        (latestReportData.kpis || []).forEach(k => {
            if (k._placeholder) return;
            const label = k.label || k.id || "KPI";
            if (!f || label.toLowerCase().includes(f))
                items.push({ type: "kpi", label, token: `@[${label}]` });
        });
        (latestReportData.charts || []).forEach(c => {
            if (c._placeholder) return;
            const label = c.title || "Chart";
            if (!f || label.toLowerCase().includes(f))
                items.push({ type: "chart", label, token: `@[${label}]` });
        });
        console.log('[MENTION] _mGetComponents: filter=', f, 'items=', items.length,
            items.map(i => `${i.type}:${i.label}`));
        return items;
    }

    function _mHide() { _mDrop.classList.remove("visible"); _mItems = []; _mAtPos = -1; }

    function _mRefreshHl() {
        _mDrop.querySelectorAll(".mention-item").forEach(el =>
            el.classList.toggle("hl", parseInt(el.dataset.idx) === _mHlIdx)
        );
    }

    function _mInsert(idx) {
        const item = _mItems[idx];
        if (!item) return;
        const val = questionInput.value;
        const after = val.substring(_mAtPos + 1 + _mQuery.length);
        questionInput.value = val.substring(0, _mAtPos) + item.token + " " + after;
        const pos = (_mAtPos + item.token.length + 1);
        questionInput.setSelectionRange(pos, pos);
        questionInput.focus();
        _mHide();
    }

    function _mRender() {
        _mDrop.innerHTML = ""; _mHlIdx = 0;
        const kpis = _mItems.filter(m => m.type === "kpi");
        const charts = _mItems.filter(m => m.type === "chart");
        function makeSection(label, arr, offset) {
            if (!arr.length) return;
            const hdr = document.createElement("div");
            hdr.className = "mention-section-hdr"; hdr.textContent = label;
            _mDrop.appendChild(hdr);
            arr.forEach((item, i) => {
                const idx = offset + i;
                const el = document.createElement("div");
                el.className = "mention-item" + (idx === _mHlIdx ? " hl" : "");
                el.dataset.idx = idx;
                el.innerHTML = `<span class="mi-badge ${item.type}">${item.type === "kpi" ? "KPI" : "Chart"}</span>${escapeHtml(item.label)}`;
                el.addEventListener("click", () => _mInsert(idx));
                el.addEventListener("mouseover", () => { _mHlIdx = idx; _mRefreshHl(); });
                _mDrop.appendChild(el);
            });
        }
        makeSection("KPIs", kpis, 0);
        makeSection("Charts", charts, kpis.length);
        const inputBox = questionInput.closest(".input-box") || questionInput.parentElement;
        const r = inputBox.getBoundingClientRect();
        _mDrop.style.bottom = (window.innerHeight - r.top + 8) + "px";
        _mDrop.style.left   = r.left + "px";
        _mDrop.style.width  = r.width + "px";
        _mDrop.classList.add("visible");
    }

    function _mHandleInput() {
        const val = questionInput.value;
        const cursor = questionInput.selectionStart;
        const before = val.substring(0, cursor);
        const atIdx = before.lastIndexOf("@");
        if (atIdx === -1) { _mHide(); return; }
        const after = before.substring(atIdx + 1);
        // Allow spaces in query so multi-word chart/KPI names can be searched
        // Only hide if user typed a closing bracket (completed mention)
        if (after.includes("]")) { _mHide(); return; }
        // Lazy-load report data from sessionStorage if not in memory
        if (!latestReportData) { _tryRestoreReportData(); }
        if (!latestReportData) { _mHide(); return; }
        _mAtPos = atIdx; _mQuery = after;
        _mItems = _mGetComponents(after);
        if (_mItems.length === 0) { _mHide(); return; }
        _mRender();
    }

    questionInput.addEventListener("keyup", e => {
        // Re-check on any key that moves the cursor (non-modifier keys)
        if (!["Shift","Control","Alt","Meta","ArrowLeft","ArrowRight"].includes(e.key)) {
            _mHandleInput();
        }
    });

    document.addEventListener("click", e => {
        if (!_mDrop.contains(e.target) && e.target !== questionInput) _mHide();
    });

    // ── Restore report state from storage ──────────────────────────────────
    function _tryRestoreReportData() {
        if (latestReportData) return; // already loaded
        const savedId = latestReportId || localStorage.getItem("sqlbot_latestReportId");
        if (!savedId) return;
        try {
            const raw = sessionStorage.getItem(savedId);
            if (raw) {
                const parsed = JSON.parse(raw);
                if (parsed && parsed.report) {
                    latestReportData = parsed.report;
                    latestReportId = savedId;
                }
            }
        } catch (_) {}
    }

    // ── Cross-tab sync: listen for report changes from the report tab ───
    try {
        const _reportSyncChannel = new BroadcastChannel('report_sync');
        _reportSyncChannel.onmessage = (event) => {
            const msg = event.data;
            if (msg && msg.type === 'report_updated' && msg.report) {
                latestReportData = msg.report;
                if (msg.reportId) latestReportId = msg.reportId;
                console.log('[SYNC] Received report update from report tab.',
                    'kpis=', (msg.report.kpis || []).map(k => k.label),
                    'charts=', (msg.report.charts || []).map(c => c.title));
            }
        };
    } catch (_) {}
    // ── Startup: load current conversation ────────────────────────────────
    (async function init() {
        renderSidebarList();
        const convs = getConversations();
        const existing = convs.find(c => c.id === currentConvId);

        // Restore report state from previous session
        _tryRestoreReportData();

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
