/* PlanPilot 前端 · chat
   对话、Markdown、轨迹与请求生命周期
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

function renderChatMessages() {
  const container = $("chatMessages");
  if (!container) {
    return;
  }

  if (state.chatMessages.length === 0 && !state.chatLoading) {
    container.innerHTML = `
      <div class="chat-empty">还没有对话，和助手打个招呼吧</div>
    `;
  } else {
    const bubbles = state.chatMessages
      .map((message) => {
        const isUser = message.role === "user";
        const roleClass = isUser ? "chat-user" : "chat-assistant";
        const label = isUser ? "我" : "助手";

        const content = isUser
          ? escapeHtml(message.content)
          : renderMarkdown(message.content);
        return `
          <div class="chat-bubble ${roleClass}">
            <div class="chat-bubble-label">${escapeHtml(label)}</div>
            <div class="chat-bubble-content ${isUser ? "" : "markdown-content"}">${content}</div>
          </div>
        `;
      })
      .join("");

    const loading = state.chatLoading
      ? `
        <div class="chat-bubble chat-assistant">
          <div class="chat-bubble-label">助手</div>
          <div class="chat-bubble-content chat-typing">
            <span>正在思考…</span>
            <button class="chat-stop" type="button" onclick="stopChatMessage()">停止</button>
          </div>
        </div>
      `
      : "";

    container.innerHTML = bubbles + loading;
    scrollChatToBottom();
  }

  const bar = $("chatConfirmBar");
  if (bar) {
    if (state.chatPendingConfirmation) {
      $("chatConfirmSummary").textContent =
        "待确认：" + state.chatPendingConfirmation.summary;
      renderChatConfirmationPreview(state.chatPendingConfirmation.preview);
      bar.classList.remove("hidden");
    } else {
      if ($("chatConfirmPreview")) {
        $("chatConfirmPreview").innerHTML = "";
      }
      bar.classList.add("hidden");
    }
  }
}

function renderChatConfirmationPreview(preview) {
  const container = $("chatConfirmPreview");
  const events = preview?.events;
  if (!container || !Array.isArray(events) || !events.length) {
    if (container) {
      container.innerHTML = "";
    }
    return;
  }
  const eventRows = events.map((event) => {
    const start = parseDate(event.start_time);
    const end = parseDate(event.end_time);
    const time = start && end
      ? `${formatDate(start)} ${formatTime(start)}–${formatTime(end)}`
      : "";
    const route = event.event_type === "travel" && event.origin_location
      ? `${travelModeName(event.travel_mode)} · ${event.origin_location} → ${event.location || "目的地"}`
      : event.location || "";
    return `
      <div class="chat-confirm-item">
        <span>${escapeHtml(time)}</span>
        <strong>${escapeHtml(event.title || "行程")}</strong>
        <small>${escapeHtml(route)}</small>
      </div>
    `;
  }).join("");
  const deletedCount = Array.isArray(preview.deleted_event_ids)
    ? preview.deleted_event_ids.length
    : 0;
  container.innerHTML = `
    ${preview.trip_title ? `<div class="chat-confirm-trip-title"><strong>${escapeHtml(preview.trip_title)}</strong>${preview.shift_minutes ? `<span>整体${preview.shift_minutes > 0 ? "顺延" : "提前"} ${Math.abs(preview.shift_minutes)} 分钟</span>` : ""}</div>` : ""}
    ${eventRows}
    ${deletedCount ? `<div class="chat-confirm-delete">将删除 ${deletedCount} 段行程</div>` : ""}
  `;
}

function renderMarkdown(value) {
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  const output = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (
      index + 1 < lines.length &&
      line.includes("|") &&
      isMarkdownTableSeparator(lines[index + 1])
    ) {
      const headers = markdownTableCells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(markdownTableCells(lines[index]));
        index += 1;
      }
      output.push(`
        <div class="markdown-table-wrap"><table>
          <thead><tr>${headers.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("")}</tr></thead>
          <tbody>${rows
            .map(
              (row) =>
                `<tr>${headers
                  .map((_, cellIndex) => `<td>${renderInlineMarkdown(row[cellIndex] || "")}</td>`)
                  .join("")}</tr>`,
            )
            .join("")}</tbody>
        </table></div>
      `);
      continue;
    }

    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) {
      const items = [];
      while (index < lines.length) {
        const match = lines[index].match(/^\s*[-*+]\s+(.+)$/);
        if (!match) break;
        items.push(`<li>${renderInlineMarkdown(match[1])}</li>`);
        index += 1;
      }
      output.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) {
      const items = [];
      while (index < lines.length) {
        const match = lines[index].match(/^\s*\d+[.)]\s+(.+)$/);
        if (!match) break;
        items.push(`<li>${renderInlineMarkdown(match[1])}</li>`);
        index += 1;
      }
      output.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 6);
      output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    const paragraph = [line.trim()];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^\s*[-*+]\s+/.test(lines[index]) &&
      !/^\s*\d+[.)]\s+/.test(lines[index]) &&
      !/^#{1,4}\s+/.test(lines[index]) &&
      !(
        index + 1 < lines.length &&
        lines[index].includes("|") &&
        isMarkdownTableSeparator(lines[index + 1])
      )
    ) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    output.push(`<p>${paragraph.map(renderInlineMarkdown).join("<br>")}</p>`);
  }
  return output.join("");
}

function renderInlineMarkdown(value) {
  const codeTokens = [];
  let escaped = escapeHtml(value).replace(/`([^`]+)`/g, (_, code) => {
    const token = `@@PLANPILOT_CODE_${codeTokens.length}@@`;
    codeTokens.push(`<code>${code}</code>`);
    return token;
  });
  escaped = escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  codeTokens.forEach((html, tokenIndex) => {
    escaped = escaped.replace(`@@PLANPILOT_CODE_${tokenIndex}@@`, html);
  });
  return escaped;
}

function markdownTableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isMarkdownTableSeparator(line) {
  const cells = markdownTableCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

async function loadChatHistory() {
  if (state.chatLoaded) {
    renderChatMessages();
    return;
  }
  const messages = await api(`/agent/sessions/${state.chatSessionId}/messages`);
  state.chatMessages = messages.map((item) => ({
    role: item.role,
    content: item.content,
  }));
  state.chatLoaded = true;
  renderChatMessages();
}

async function loadAgentStatus() {
  const status = await api("/agent/status");
  const badge = $("agentStatus");
  const reconnectButton = $("agentReconnectButton");
  if (!badge) {
    return;
  }
  const disconnected = status.mode === "local" || status.fallback_active || Boolean(status.last_error);
  const errorLabel = llmErrorLabel(status.last_error_type);
  badge.textContent = disconnected
    ? status.retry_after_seconds > 0
      ? `${errorLabel || "连接异常"} · ${status.retry_after_seconds} 秒后自动重试`
      : `${errorLabel || "连接异常"} · 可重新连接`
    : `大模型 · ${status.model || "已连接"}`;
  badge.title = status.last_error || "大模型连接正常";
  badge.className = `status-badge ${disconnected ? "status-local" : "status-online"}`;
  reconnectButton?.classList.toggle("hidden", !disconnected);
}

async function reconnectAgent() {
  const button = $("agentReconnectButton");
  const badge = $("agentStatus");
  if (button?.disabled) {
    return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = "连接中";
  }
  if (badge) {
    badge.textContent = "正在重新连接";
  }
  try {
    const result = await api("/agent/reconnect", { method: "POST" });
    await loadAgentStatus();
    if (!result.connected) {
      alert(`重连失败：${result.error}`);
    }
  } catch (error) {
    alert(`重连失败：${error.message}`);
    await loadAgentStatus().catch(handleError);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "重新连接";
    }
  }
}

function llmErrorLabel(kind) {
  switch (kind) {
    case "auth":
      return "鉴权失败";
    case "rate_limit":
      return "限流中";
    case "transient":
      return "网络异常";
    default:
      return "";
  }
}

function renderAgentTrace(data) {
  const trace = $("agentTrace");
  const content = $("agentTraceContent");
  const meta = $("agentTraceMeta");
  const calls = data.tool_calls || [];
  const workflow = data.steps || data.workflow_steps || [];
  if (!trace || !content || (!calls.length && !workflow.length)) {
    trace?.classList.add("hidden");
    return;
  }
  const traceItems = workflow.length ? workflow : calls;
  const statusName = {
    completed: "已完成",
    waiting_confirmation: "待确认",
    warning: "有提醒",
    failed: "失败",
  }[data.status || data.run_status] || "已完成";
  if (meta) {
    const duration = data.duration_ms === undefined ? "" : `${data.duration_ms} ms`;
    const toolCount = data.tool_count ?? traceItems.filter((item) => item.kind === "tool").length;
    meta.innerHTML = `
      <span>${escapeHtml(statusName)}</span>
      <span>${toolCount} 次工具调用</span>
      ${duration ? `<span>${escapeHtml(duration)}</span>` : ""}
      ${data.provider ? `<span>${escapeHtml(data.provider)}</span>` : ""}
      ${data.model_usage?.total_tokens ? `<span>${escapeHtml(`${data.model_usage.total_tokens} tokens`)}</span>` : ""}
      ${data.estimated_cost ? `<span>${escapeHtml(`估算成本 ${Number(data.estimated_cost).toFixed(6)}`)}</span>` : ""}
    `;
  }
  const rows = traceItems.map((call) => `
    <div class="trace-row">
      <span class="trace-state ${traceStateClass(call)}">${escapeHtml(call.stage || "执行")}</span>
      <strong>${escapeHtml(call.label || call.name || "")}</strong>
      <span>${escapeHtml(call.summary || "")}${call.duration_ms ? ` · ${call.duration_ms} ms` : ""}</span>
    </div>
  `);
  content.innerHTML = rows.join("");
  trace.classList.remove("hidden");
}

function traceStateClass(item) {
  const status = item.status || (item.success ? "completed" : "failed");
  if (status === "failed") return "failed";
  if (status === "warning") return "warning";
  if (status === "waiting") return "waiting";
  return "ok";
}

async function loadAgentRuns() {
  state.agentRuns = await api(`/agent/sessions/${state.chatSessionId}/runs?limit=20`);
  const select = $("agentRunSelect");
  if (select) {
    select.innerHTML = state.agentRuns.map((run, index) => {
      const created = parseDate(run.created_at);
      const label = `${index === 0 ? "最近 · " : ""}${created ? `${formatDate(created)} ${formatTime(created)}` : "执行记录"}`;
      return `<option value="${escapeHtml(run.id)}">${escapeHtml(label)}</option>`;
    }).join("");
  }
  if (state.agentRuns.length) {
    renderAgentTrace(state.agentRuns[0]);
  } else {
    $("agentTrace")?.classList.add("hidden");
  }
}

function selectAgentRun(runId) {
  const run = state.agentRuns.find((item) => item.id === runId);
  if (run) {
    renderAgentTrace(run);
  }
}

function scrollChatToBottom() {
  const container = $("chatMessages");
  if (container) {
    container.scrollTop = container.scrollHeight;
  }
}

function autoResizeChatInput() {
  const input = $("chatInput");
  if (!input) {
    return;
  }
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
}

//: 客户端兜底超时。必须晚于服务端单次上限（默认 90 秒）再加一次模型调用，
//: 否则服务端还在收尾、界面已经判失败，用户会看到"失败"但数据其实已写进去。
const CHAT_CLIENT_TIMEOUT_MS = 150000;

function newRequestId() {
  if (window.crypto && crypto.randomUUID) {
    return crypto.randomUUID().replace(/-/g, "").slice(0, 32);
  }
  return `r${Date.now()}${Math.random().toString(16).slice(2, 12)}`.slice(0, 32);
}

function stopChatMessage() {
  if (state.chatAbort) {
    state.chatAbort.abort();
  }
}

async function sendChatMessage() {
  const input = $("chatInput");
  const content = input.value.trim();
  if (!content || state.chatLoading) {
    return;
  }

  input.value = "";
  autoResizeChatInput();

  state.chatPendingConfirmation = null;
  state.chatMessages.push({ role: "user", content });
  state.chatLoading = true;

  // 同一条内容重发（上次超时或被中断）时复用 requestId：服务端若已经跑完，
  // 直接把那次的结果还回来；若还在跑，会回 409 而不是再执行一遍。
  const pending = state.chatPendingRequest;
  const requestId =
    pending && pending.content === content ? pending.requestId : newRequestId();
  state.chatPendingRequest = { requestId, content };
  state.chatAbort = new AbortController();

  renderChatMessages();

  try {
    const data = await api("/agent/chat", {
      method: "POST",
      body: JSON.stringify({
        message: content,
        session_id: state.chatSessionId,
        request_id: requestId,
      }),
      signal: state.chatAbort.signal,
      timeoutMs: CHAT_CLIENT_TIMEOUT_MS,
    });

    // 这次请求已经有确定结果，不必再用同一个 id 重发。
    state.chatPendingRequest = null;

    state.chatSessionId = data.session_id;
    localStorage.setItem("planpilot_session_id", data.session_id);
    state.chatMessages.push({ role: "assistant", content: data.reply });
    renderAgentTrace(data);

    if (data.confirmation && data.confirmation.required) {
      state.chatPendingConfirmation = {
        action_id: data.confirmation.action_id,
        summary: data.confirmation.summary,
        preview: data.confirmation.preview,
      };
    }
    if ((data.applied_changes || []).length || (data.proposed_schedules || []).length) {
      await loadAllData();
    }
    await loadAgentStatus();
    await loadAgentRuns().catch(handleError);
    // 消息数和标题都变了，列表要跟着更新（新会话第一次说话后才会出现在列表里）。
    await loadSessions().catch(handleError);
  } catch (error) {
    let content_;
    if (error.status === 409) {
      content_ =
        "上一条请求还在处理中，等它返回就行，不用重复发送（同一次请求不会被重复执行）。";
    } else if (error.timeout) {
      content_ =
        `请求超过 ${Math.round(CHAT_CLIENT_TIMEOUT_MS / 1000)} 秒仍未返回，界面已停止等待。` +
        "服务端可能还在跑——重发同样的话会复用同一次请求，不会重复创建任务。";
    } else if (error.aborted) {
      content_ =
        "已停止这次请求。服务端可能仍在执行，结果会写进会话记录，刷新可见。";
    } else {
      content_ = `发送失败：${error.message}`;
    }
    state.chatMessages.push({ role: "assistant", content: content_ });
  } finally {
    state.chatLoading = false;
    state.chatAbort = null;
    renderChatMessages();
  }
}

async function confirmChatDecision(decision) {
  if (!state.chatPendingConfirmation || state.chatLoading) {
    return;
  }

  const pending = state.chatPendingConfirmation;
  state.chatPendingConfirmation = null;
  state.chatLoading = true;
  renderChatMessages();

  try {
    const data = await api("/agent/chat", {
      method: "POST",
      body: JSON.stringify({
        message: "",
        session_id: state.chatSessionId,
        confirm_action_id: pending.action_id,
        decision,
        // 确认路径同样需要幂等：这是唯一会真的写数据的入口，
        // 双击"确认"不该产生两遍执行。
        request_id: newRequestId(),
      }),
      timeoutMs: CHAT_CLIENT_TIMEOUT_MS,
    });

    state.chatSessionId = data.session_id;
    state.chatMessages.push({ role: "assistant", content: data.reply });
    renderAgentTrace(data);
    if ((data.applied_changes || []).length || (data.proposed_schedules || []).length) {
      await loadAllData();
    }
    await loadAgentRuns().catch(handleError);
  } catch (error) {
    state.chatMessages.push({
      role: "assistant",
      content: `操作失败：${error.message}`,
    });
  } finally {
    state.chatLoading = false;
    renderChatMessages();
  }
}

async function clearChat() {
  if (!confirm("确定清空对话吗？")) {
    return;
  }
  try {
    await api(`/agent/sessions/${state.chatSessionId}/messages`, { method: "DELETE" });
    state.chatMessages = [];
    state.chatPendingConfirmation = null;
    state.agentRuns = [];
    $("agentTrace")?.classList.add("hidden");
    renderChatMessages();
    await loadSessions().catch(handleError);
  } catch (error) {
    alert(`清空失败：${error.message}`);
  }
}


/* =========================
   历史会话
========================= */

// 前端原本只认 localStorage 里那一个 session_id，既没有列表也没有切换入口，
// 换 origin（localhost 与 127.0.0.1 算两个）、清缓存或换浏览器就会生成新 id，
// 看起来像"历史记录丢了"。数据一直都在库里，缺的只是这个入口。
