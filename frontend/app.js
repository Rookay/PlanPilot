/* =========================================================
   日程规划助手 —— 前端逻辑
   ========================================================= */

// API 地址：优先使用页面里通过 window.API_URL 指定的地址；
// 否则自动指向「当前访问的主机 + 8000 端口」，这样换主机/换 IP 访问时不用改代码。
// 例：http://192.168.1.50:5173 打开页面 -> http://192.168.1.50:8000/api
const API_URL =
  window.API_URL ||
  (() => {
    const { protocol, hostname } = window.location;

    // 用 file:// 直接双击打开页面时没有 hostname，退回本机地址。
    if (protocol === "file:" || !hostname) {
      return "http://127.0.0.1:8000/api";
    }

    return `${protocol}//${hostname}:8000/api`;
  })();

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

// 日程表格的显示范围与每小时的高度（像素）。
const TIMETABLE_START_HOUR = 6;
const TIMETABLE_END_HOUR = 24;
const HOUR_HEIGHT = 48;

const state = {
  tasks: [],
  events: [],
  schedules: [],
  weekStart: null,
  editingTaskId: null,
  chatMessages: [],
  chatLoading: false,
  chatSessionId: null,
  chatPendingConfirmation: null,
};


/* =========================
   通用工具
========================= */

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function startOfDay(date) {
  const result = new Date(date);
  result.setHours(0, 0, 0, 0);
  return result;
}

function startOfWeek(date) {
  // 以周一作为一周的第一天。
  const result = startOfDay(date);
  const offset = (result.getDay() + 6) % 7;
  result.setDate(result.getDate() - offset);
  return result;
}

function addDays(date, amount) {
  const result = new Date(date);
  result.setDate(result.getDate() + amount);
  return result;
}

function isSameDay(a, b) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function parseDate(value) {
  if (!value) {
    return null;
  }

  let text = String(value);

  // 后端返回的是不带时区的 UTC 时间（例如 2026-09-12T10:00:00），
  // 补上 Z 才会被浏览器按 UTC 解析，否则会当成本地时间导致显示错位。
  if (!/([zZ]|[+-]\d{2}:?\d{2})$/.test(text)) {
    text += "Z";
  }

  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(date) {
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

function formatTime(date) {
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

function formatDateTime(value) {
  const date = parseDate(value);
  if (!date) {
    return "";
  }
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day} ${formatTime(date)}`;
}

function toDateTimeLocalValue(value) {
  const date = parseDate(value);
  if (!date) {
    return "";
  }
  const pad = (number) => String(number).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

function getPriorityName(priority) {
  const map = { low: "低", medium: "中", high: "高" };
  return map[priority] || priority;
}

function getStatusName(status) {
  const map = {
    pending: "待完成",
    in_progress: "进行中",
    completed: "已完成",
    cancelled: "已取消",
  };
  return map[status] || status;
}


/* =========================
   网络请求
========================= */

async function api(path, options = {}) {
  const config = { ...options };

  // 只有带请求体时才发送 Content-Type，避免 GET/DELETE 触发多余的 CORS 预检。
  if (config.body !== undefined && !config.headers) {
    config.headers = { "Content-Type": "application/json" };
  }

  const response = await fetch(`${API_URL}${path}`, config);

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data && data.detail) {
        detail =
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail);
      }
    } catch (error) {
      /* 忽略解析失败，使用默认错误信息 */
    }
    throw new Error(detail);
  }

  if (response.status === 204) {
    return null;
  }
  return response.json();
}


/* =========================
   页面切换
========================= */

function showPage(pageName) {
  document
    .querySelectorAll(".page")
    .forEach((page) => page.classList.add("hidden"));

  const target = $(pageName);
  if (target) {
    target.classList.remove("hidden");
  }

  if (pageName === "tasks" || pageName === "dashboard") {
    loadTasks().catch(handleError);
  } else if (pageName === "calendar" || pageName === "planning") {
    loadAllData()
      .then(() => {
        if (pageName === "planning") {
          renderSchedule(null);
        }
      })
      .catch(handleError);
  } else if (pageName === "chat") {
    renderChatMessages();
  }
}


/* =========================
   数据加载
========================= */

async function loadTasks() {
  try {
    state.tasks = await api("/tasks");
    renderTasks();
    renderDashboardTasks();
    updateDashboard();
  } catch (error) {
    const container = $("taskList");
    if (container) {
      container.innerHTML = `
        <div class="empty error">加载任务失败：${escapeHtml(error.message)}</div>
      `;
    }
    throw error;
  }
}

async function loadAllData() {
  const [tasks, events, schedules] = await Promise.all([
    api("/tasks"),
    api("/events"),
    api("/schedules"),
  ]);

  state.tasks = tasks;
  state.events = events;
  state.schedules = schedules;

  renderTasks();
  renderDashboardTasks();
  updateDashboard();
  renderTimetables();
}

async function refreshSchedules() {
  state.schedules = await api("/schedules");
  renderTimetables();
}


/* =========================
   任务
========================= */

function renderTasks() {
  const container = $("taskList");
  if (!container) {
    return;
  }

  if (state.tasks.length === 0) {
    container.innerHTML = `
      <div class="empty">
        暂时没有任务，点击「+ 新建任务」开始吧
      </div>
    `;
    return;
  }

  container.innerHTML = state.tasks.map(renderTaskItem).join("");
}

function renderTaskItem(task) {
  const done = task.status === "completed";

  const meta = [
    `时长 ${task.estimated_duration} 分钟`,
    `优先级：${getPriorityName(task.priority)}`,
    `状态：${getStatusName(task.status)}`,
  ];

  const startText = formatDateTime(task.start_time);
  if (startText) {
    meta.unshift(`开始：${startText}`);
  }
  if (task.category) {
    meta.push(`分类：${task.category}`);
  }

  const description = task.description
    ? `<div class="task-desc">${escapeHtml(task.description)}</div>`
    : "";

  return `
    <div class="task ${done ? "task-done" : ""}">
      <div class="task-main">
        <div class="task-title">${escapeHtml(task.title)}</div>
        <div class="task-info">${escapeHtml(meta.join(" · "))}</div>
        ${description}
      </div>

      <div class="task-actions">
        <button class="ghost-button" onclick="toggleTask(${task.id})">
          ${done ? "恢复" : "完成"}
        </button>
        <button class="ghost-button" onclick="openTaskModal(${task.id})">
          编辑
        </button>
        <button class="delete-button" onclick="deleteTask(${task.id})">
          删除
        </button>
      </div>
    </div>
  `;
}

function renderDashboardTasks() {
  const container = $("dashboardTaskList");
  if (!container) {
    return;
  }

  if (state.tasks.length === 0) {
    container.innerHTML = `
      <div class="empty">还没有任务，点击「+ 新建任务」开始吧</div>
    `;
    return;
  }

  const recent = state.tasks.slice(0, 5);

  container.innerHTML = recent
    .map((task) => {
      const startText = formatDateTime(task.start_time);
      return `
        <div class="mini-task">
          <span class="mini-task-title">${escapeHtml(task.title)}</span>
          <span class="mini-task-meta">
            ${startText ? escapeHtml(startText) : "未设置时间"} ·
            ${escapeHtml(getStatusName(task.status))}
          </span>
        </div>
      `;
    })
    .join("");
}

async function submitTask() {
  const title = $("taskTitle").value.trim();
  if (!title) {
    alert("请输入任务名称");
    return;
  }

  const duration = Number($("taskDuration").value);
  if (!duration || duration < 1) {
    alert("持续时间必须是大于 0 的数字");
    return;
  }

  const startValue = $("taskStartTime").value;

  const data = {
    title,
    description: $("taskDescription").value.trim() || null,
    estimated_duration: duration,
    priority: $("taskPriority").value,
    start_time: startValue ? new Date(startValue).toISOString() : null,
  };

  try {
    if (state.editingTaskId) {
      await api(`/tasks/${state.editingTaskId}`, {
        method: "PUT",
        body: JSON.stringify(data),
      });
    } else {
      data.status = "pending";
      await api("/tasks", {
        method: "POST",
        body: JSON.stringify(data),
      });
    }

    closeTaskModal();
    await loadTasks();
  } catch (error) {
    alert(`保存失败：${error.message}`);
  }
}

async function deleteTask(id) {
  if (!confirm("确定删除这个任务吗？相关日程也会一并删除。")) {
    return;
  }

  try {
    await api(`/tasks/${id}`, { method: "DELETE" });
    await loadTasks();
  } catch (error) {
    alert(`删除失败：${error.message}`);
  }
}

async function toggleTask(id) {
  const task = state.tasks.find((item) => item.id === id);
  if (!task) {
    return;
  }

  const nextStatus = task.status === "completed" ? "pending" : "completed";

  try {
    await api(`/tasks/${id}`, {
      method: "PUT",
      body: JSON.stringify({ status: nextStatus }),
    });
    await loadTasks();
  } catch (error) {
    alert(`更新失败：${error.message}`);
  }
}


/* =========================
   仪表盘统计
========================= */

function updateDashboard() {
  const countOf = (status) =>
    state.tasks.filter((task) => task.status === status).length;

  setText("pendingCount", countOf("pending"));
  setText("inProgressCount", countOf("in_progress"));
  setText("completedCount", countOf("completed"));
  setText("cancelledCount", countOf("cancelled"));
}

function setText(id, value) {
  const element = $(id);
  if (element) {
    element.textContent = value;
  }
}


/* =========================
   弹窗
========================= */

function openTaskModal(id = null) {
  state.editingTaskId = id;

  if (id) {
    const task = state.tasks.find((item) => item.id === id);
    if (task) {
      $("modalTitle").textContent = "编辑任务";
      $("taskSubmitButton").textContent = "保存修改";
      $("taskTitle").value = task.title || "";
      $("taskDescription").value = task.description || "";
      $("taskStartTime").value = toDateTimeLocalValue(task.start_time);
      $("taskDuration").value = task.estimated_duration;
      $("taskPriority").value = task.priority;
    }
  } else {
    $("modalTitle").textContent = "新建任务";
    $("taskSubmitButton").textContent = "创建任务";
    clearTaskForm();
  }

  $("taskModal").classList.remove("hidden");
}

function closeTaskModal() {
  $("taskModal").classList.add("hidden");
  state.editingTaskId = null;
}

function clearTaskForm() {
  $("taskTitle").value = "";
  $("taskDescription").value = "";
  $("taskStartTime").value = "";
  $("taskDuration").value = 60;
  $("taskPriority").value = "medium";
}


/* =========================
   日程规划
========================= */

async function generateSchedule() {
  const container = $("scheduleResult");
  container.innerHTML = `<div class="empty">正在规划…</div>`;

  try {
    const result = await api("/schedules/generate", {
      method: "POST",
      body: JSON.stringify({ days: 7 }),
    });

    await loadAllData();
    renderSchedule(result);
  } catch (error) {
    container.innerHTML = `
      <div class="empty error">
        规划失败：${escapeHtml(error.message)}
      </div>
    `;
  }
}

function renderSchedule(result) {
  const container = $("scheduleResult");
  if (!container) {
    return;
  }

  const schedules =
    result && result.schedules ? result.schedules : state.schedules;
  const unscheduled =
    result && result.unscheduled ? result.unscheduled : [];

  const blocks = [];

  if (schedules.length === 0) {
    blocks.push(`<div class="empty">还没有日程，点击「开始规划」生成</div>`);
  } else {
    blocks.push(
      `<h3 class="section-title">日程清单</h3>` +
        `<div class="schedule-list">${schedules
          .map(renderScheduleItem)
          .join("")}</div>`
    );
  }

  if (unscheduled.length > 0) {
    blocks.push(`
      <div class="unscheduled">
        <h4>未能安排（${unscheduled.length}）</h4>
        <ul>
          ${unscheduled
            .map(
              (item) =>
                `<li>${escapeHtml(item.title)} —— ${escapeHtml(item.reason)}</li>`
            )
            .join("")}
        </ul>
      </div>
    `);
  }

  container.innerHTML = blocks.join("");
}

function renderScheduleItem(schedule) {
  const task = state.tasks.find((item) => item.id === schedule.task_id);
  const title = task ? task.title : `任务 #${schedule.task_id}`;

  const start = parseDate(schedule.start_time);
  const end = parseDate(schedule.end_time);

  const timeText = start
    ? `${start.getMonth() + 1}/${start.getDate()} ${formatTime(start)}` +
      (end ? ` - ${formatTime(end)}` : "")
    : "";

  const accepted = schedule.status === "accepted";

  return `
    <div class="schedule ${accepted ? "schedule-accepted" : ""}">
      <div class="schedule-time">${escapeHtml(timeText)}</div>
      <div class="schedule-title">${escapeHtml(title)}</div>
      <div class="schedule-actions">
        <button class="ghost-button" onclick="acceptSchedule(${schedule.id})">
          ${accepted ? "已确认" : "确认"}
        </button>
        <button class="delete-button" onclick="deleteSchedule(${schedule.id})">
          移除
        </button>
      </div>
    </div>
  `;
}

async function acceptSchedule(id) {
  try {
    await api(`/schedules/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "accepted" }),
    });
    await refreshSchedules();
    renderSchedule(null);
  } catch (error) {
    alert(`操作失败：${error.message}`);
  }
}

async function deleteSchedule(id) {
  try {
    await api(`/schedules/${id}`, { method: "DELETE" });
    await refreshSchedules();
    renderSchedule(null);
  } catch (error) {
    alert(`操作失败：${error.message}`);
  }
}

async function clearSchedules() {
  if (!confirm("确定清空所有「未确认」的日程吗？")) {
    return;
  }

  try {
    await api("/schedules", { method: "DELETE" });
    await refreshSchedules();
    renderSchedule(null);
  } catch (error) {
    alert(`清空失败：${error.message}`);
  }
}


/* =========================
   日程表格（小时 × 日期）
========================= */

function renderTimetables() {
  document.querySelectorAll(".timetable-host").forEach((host) => {
    renderTimetable(host);
  });
  updateWeekLabels();
}

function updateWeekLabels() {
  const weekStart = state.weekStart || startOfWeek(new Date());
  const text =
    `${weekStart.getFullYear()}年 ${formatDate(weekStart)} - ` +
    `${formatDate(addDays(weekStart, 6))}`;

  document.querySelectorAll(".week-label").forEach((label) => {
    label.textContent = text;
  });
}

function renderTimetable(host) {
  const weekStart = state.weekStart || startOfWeek(new Date());
  const today = startOfDay(new Date());

  const days = [];
  for (let index = 0; index < 7; index += 1) {
    days.push(addDays(weekStart, index));
  }

  const hours = [];
  for (let hour = TIMETABLE_START_HOUR; hour < TIMETABLE_END_HOUR; hour += 1) {
    hours.push(hour);
  }

  const timeColumn = hours
    .map((hour) => `<div class="tt-hour-label">${String(hour).padStart(2, "0")}:00</div>`)
    .join("");

  const head = days
    .map((day, index) => {
      const isToday = isSameDay(day, today);
      return `
        <div class="tt-head-day ${isToday ? "tt-today" : ""}">
          <div>${WEEKDAYS[index]}</div>
          <div class="tt-head-date">${day.getMonth() + 1}/${day.getDate()}</div>
        </div>
      `;
    })
    .join("");

  const columns = days
    .map((day) => {
      const dayEnd = addDays(day, 1);
      const hourCells = hours.map(() => `<div class="tt-hour"></div>`).join("");
      const items = collectDayItems(day, dayEnd)
        .map(renderTimetableItem)
        .join("");

      return `
        <div class="tt-col ${isSameDay(day, today) ? "tt-today" : ""}">
          ${hourCells}
          ${items}
        </div>
      `;
    })
    .join("");

  host.innerHTML = `
    <div class="tt-head">
      <div class="tt-corner"></div>
      ${head}
    </div>
    <div class="tt-body">
      <div class="tt-times">${timeColumn}</div>
      <div class="tt-cols">${columns}</div>
    </div>
  `;
}

function collectDayItems(day, dayEnd) {
  const items = [];

  const dayStartMs = day.getTime();
  const windowStartMs = dayStartMs + TIMETABLE_START_HOUR * 3600000;
  const windowEndMs = dayStartMs + TIMETABLE_END_HOUR * 3600000;

  const push = (start, end, className, label) => {
    if (!start || !end) {
      return;
    }

    const startMs = Math.max(start.getTime(), windowStartMs);
    const endMs = Math.min(end.getTime(), windowEndMs);
    if (endMs <= startMs) {
      return;
    }

    const top = ((startMs - windowStartMs) / 3600000) * HOUR_HEIGHT;
    const height = Math.max(((endMs - startMs) / 3600000) * HOUR_HEIGHT, 20);

    items.push({
      top,
      height,
      className,
      label,
      timeText: formatTime(start),
    });
  };

  state.events.forEach((event) => {
    const start = parseDate(event.start_time);
    const end = parseDate(event.end_time);
    if (start && end && end > day && start < dayEnd) {
      push(start, end, "tt-event", event.title);
    }
  });

  const scheduledTaskIds = new Set(
    state.schedules.map((schedule) => schedule.task_id)
  );

  state.schedules.forEach((schedule) => {
    const start = parseDate(schedule.start_time);
    const end = parseDate(schedule.end_time);
    if (start && end && end > day && start < dayEnd) {
      const task = state.tasks.find((item) => item.id === schedule.task_id);
      const title = task ? task.title : `任务 #${schedule.task_id}`;
      const className =
        schedule.status === "accepted"
          ? "tt-schedule tt-accepted"
          : "tt-schedule";
      push(start, end, className, title);
    }
  });

  state.tasks.forEach((task) => {
    const start = parseDate(task.start_time);
    if (!start || !isSameDay(start, day)) {
      return;
    }
    if (scheduledTaskIds.has(task.id)) {
      return;
    }

    const end = new Date(
      start.getTime() + (task.estimated_duration || 60) * 60000
    );
    push(start, end, "tt-task", task.title);
  });

  return items;
}

function renderTimetableItem(item) {
  return `
    <div
      class="tt-item ${item.className}"
      style="top:${item.top}px;height:${item.height}px"
      title="${escapeHtml(item.label)}">
      <div class="tt-item-title">${escapeHtml(item.label)}</div>
      <div class="tt-item-time">${escapeHtml(item.timeText)}</div>
    </div>
  `;
}

function previousWeek() {
  state.weekStart = addDays(state.weekStart || startOfWeek(new Date()), -7);
  renderTimetables();
}

function nextWeek() {
  state.weekStart = addDays(state.weekStart || startOfWeek(new Date()), 7);
  renderTimetables();
}

function goToday() {
  state.weekStart = startOfWeek(new Date());
  renderTimetables();
}


/* =========================
   对话助手（接入 Agent）
========================= */

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

        return `
          <div class="chat-bubble ${roleClass}">
            <div class="chat-bubble-label">${escapeHtml(label)}</div>
            <div class="chat-bubble-content">${escapeHtml(message.content)}</div>
          </div>
        `;
      })
      .join("");

    const loading = state.chatLoading
      ? `
        <div class="chat-bubble chat-assistant">
          <div class="chat-bubble-label">助手</div>
          <div class="chat-bubble-content chat-typing">正在思考…</div>
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
      bar.classList.remove("hidden");
    } else {
      bar.classList.add("hidden");
    }
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
  renderChatMessages();

  try {
    const data = await api("/agent/chat", {
      method: "POST",
      body: JSON.stringify({
        message: content,
        session_id: state.chatSessionId,
      }),
    });

    state.chatSessionId = data.session_id;
    state.chatMessages.push({ role: "assistant", content: data.reply });

    if (data.confirmation && data.confirmation.required) {
      state.chatPendingConfirmation = {
        action_id: data.confirmation.action_id,
        summary: data.confirmation.summary,
      };
    }
  } catch (error) {
    state.chatMessages.push({
      role: "assistant",
      content: `发送失败：${error.message}`,
    });
  } finally {
    state.chatLoading = false;
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
      }),
    });

    state.chatSessionId = data.session_id;
    state.chatMessages.push({ role: "assistant", content: data.reply });
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

function clearChat() {
  if (!confirm("确定清空对话吗？")) {
    return;
  }
  state.chatMessages = [];
  state.chatSessionId = null;
  state.chatPendingConfirmation = null;
  renderChatMessages();
}


/* =========================
   错误处理
========================= */

function handleError(error) {
  console.error(error);
}


/* =========================
   初始化
========================= */

function init() {
  state.weekStart = startOfWeek(new Date());
  loadTasks().catch(handleError);

  const chatInput = $("chatInput");
  if (chatInput) {
    chatInput.addEventListener("input", autoResizeChatInput);
    chatInput.addEventListener("keydown", (event) => {
      // Enter 发送，Shift+Enter 换行。
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendChatMessage();
      }
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
