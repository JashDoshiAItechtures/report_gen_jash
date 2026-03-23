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
    const topbarTitle    = document.getElementById("topbarTitle");

    let selectedProvider = "groq";
    let isLoading        = false;

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
    // Each conversation: { id, title, created_at }
    function getConversations() {
        try { return JSON.parse(localStorage.getItem("sqlbot_conversations") || "[]"); }
        catch { return []; }
    }
    function saveConversations(list) {
        localStorage.setItem("sqlbot_conversations", JSON.stringify(list));
    }

    let currentConvId = localStorage.getItem("sqlbot_conversation_id") || newConvId();

    function newConvId() {
        return (window.crypto && window.crypto.randomUUID)
            ? window.crypto.randomUUID()
            : "conv-" + Date.now().toString(36);
    }

    function setCurrentConv(id) {
        currentConvId = id;
        localStorage.setItem("sqlbot_conversation_id", id);
    }

    function addConversationToList(id, title) {
        const list = getConversations();
        if (!list.find(c => c.id === id)) {
            list.unshift({ id, title, created_at: new Date().toISOString() });
            saveConversations(list);
        }
        renderSidebarList();
    }

    function updateConversationTitle(id, title) {
        const list = getConversations();
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
        const list = getConversations().filter(c => c.id !== id);
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

        // Show typing indicator
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
                appendAIMessage(data);

                // Update sidebar: first question becomes the conversation title
                const convs = getConversations();
                if (!convs.find(c => c.id === currentConvId)) {
                    addConversationToList(currentConvId, question);
                    topbarTitle.textContent = question.length > 40 ? question.slice(0, 40) + "…" : question;
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
