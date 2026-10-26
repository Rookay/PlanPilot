/* PlanPilot 前端 · session
   会话列表、切换与删除
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

async function loadSessions() {
  try {
    state.sessions = await api("/agent/sessions?limit=50");
  } catch (error) {
    // 列表挂了不该连累聊天本身，静默降级成空列表。
    state.sessions = [];
  }
  state.sessionsLoaded = true;
  renderSessionList();
}

function renderSessionList() {
  const container = $("sessionList");
  if (!container) {
    return;
  }
  container.textContent = "";

  const counter = $("sessionCount");
  if (counter) {
    // 空字符串让 :empty 生效，把徽章整个收起来，而不是留个"0"。
    counter.textContent = state.sessions.length ? String(state.sessions.length) : "";
  }

  if (!state.sessions.length) {
    const empty = document.createElement("span");
    empty.className = "session-empty";
    empty.textContent = "还没有历史会话";
    container.appendChild(empty);
    return;
  }

  state.sessions.forEach((item) => {
    const chip = document.createElement("div");
    const isActive = item.session_id === state.chatSessionId;
    chip.className = isActive ? "session-chip active" : "session-chip";
    chip.setAttribute("role", "button");
    chip.tabIndex = 0;
    chip.title = item.preview || item.title;
    chip.addEventListener("click", () => switchSession(item.session_id));
    chip.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        switchSession(item.session_id);
      }
    });

    const title = document.createElement("span");
    title.className = "session-chip-title";
    title.textContent = item.title || item.session_id;

    const meta = document.createElement("span");
    meta.className = "session-chip-meta";
    meta.textContent = `${item.message_count} 条 · ${formatSessionTime(item.last_message_at)}`;

    const remove = document.createElement("span");
    remove.className = "session-chip-delete";
    remove.textContent = "×";
    remove.title = "删除这个会话";
    remove.addEventListener("click", (event) => {
      // 不拦住就会冒泡到 chip，变成"删完顺手切过去"。
      event.stopPropagation();
      deleteSession(item.session_id);
    });

    chip.append(title, meta, remove);
    container.appendChild(chip);
  });

  // 会话多了以后当前会话可能已被滚出可视区，切完看不到"我现在在哪"。
  // 只动列表自己的 scrollLeft：scrollIntoView 会连带外层页面一起滚，
  // 而这个函数每次发完消息都会跑，页面会莫名其妙地上下跳。
  const active = container.querySelector(".session-chip.active");
  if (active) {
    // 用视口坐标算，不用 offsetLeft：offsetLeft 相对的是最近的定位祖先，
    // 未必是列表本身，减出来的数会带上无关偏移。
    const list = container.getBoundingClientRect();
    const chip = active.getBoundingClientRect();
    if (chip.left < list.left) {
      container.scrollLeft -= list.left - chip.left + 4;
    } else if (chip.right > list.right) {
      container.scrollLeft += chip.right - list.right + 4;
    }
  }
}

async function switchSession(sessionId) {
  if (sessionId === state.chatSessionId) {
    return;
  }
  state.chatSessionId = sessionId;
  localStorage.setItem("planpilot_session_id", sessionId);
  state.chatMessages = [];
  state.chatLoaded = false;
  state.chatPendingConfirmation = null;
  state.agentRuns = [];
  $("agentTrace")?.classList.add("hidden");
  renderSessionList();

  try {
    await Promise.all([loadChatHistory(), loadAgentRuns()]);
  } catch (error) {
    handleError(error);
  }
}

async function deleteSession(sessionId) {
  const target = state.sessions.find((item) => item.session_id === sessionId);
  const label = target ? target.title : sessionId;
  if (
    !confirm(
      `确定删除会话「${label}」吗？消息、运行记录和摘要会一起删掉，无法恢复。`
    )
  ) {
    return;
  }

  try {
    await api(`/agent/sessions/${sessionId}`, { method: "DELETE" });
    if (sessionId === state.chatSessionId) {
      startNewSession();
    }
    await loadSessions();
  } catch (error) {
    alert(`删除失败：${error.message}`);
  }
}

function startNewSession() {
  const newId =
    window.crypto && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, "")
      : `session_${Date.now()}`;
  state.chatSessionId = newId;
  localStorage.setItem("planpilot_session_id", newId);
  state.chatMessages = [];
  // 新会话必然是空的，没必要为它多打一次接口。
  state.chatLoaded = true;
  state.chatPendingConfirmation = null;
  state.agentRuns = [];
  $("agentTrace")?.classList.add("hidden");
  renderChatMessages();
  renderSessionList();
}


/* =========================
   Agent 评测
========================= */
