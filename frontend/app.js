/* =========================================================
   日程规划助手 —— 前端逻辑
   ========================================================= */

// API 地址：优先使用页面里通过 window.API_URL 指定的地址；
// 否则自动指向「当前访问的主机 + 8000 端口」，这样换主机/换 IP 访问时不用改代码。
// 例：http://192.168.1.50:5173 打开页面 -> http://192.168.1.50:8000/api
const API_URL =
  window.API_URL ||
  (() => {
    const { protocol, hostname, origin, port } = window.location;

    // 用 file:// 直接双击打开页面时没有 hostname，退回本机地址。
    if (protocol === "file:" || !hostname) {
      return "http://127.0.0.1:8000/api";
    }

    return port === "8000" ? `${origin}/api` : `${protocol}//${hostname}:8000/api`;
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
  calendarStart: null,
  calendarRangeMode: "upcoming",
  editingTaskId: null,
  editingEventId: null,
  chatMessages: [],
  chatLoading: false,
  chatSessionId: null,
  chatPendingConfirmation: null,
  sessions: [],
  sessionsLoaded: false,
  preferences: null,
  locations: [],
  modalLocationCandidates: { task: [], event: [], eventOrigin: [] },
  editingLocationId: null,
  itinerary: null,
  itineraryMap: null,
  itineraryMarkers: new Map(),
  itineraryRoutes: new Map(),
  itineraryInfoWindow: null,
  calendarDetailMap: null,
  calendarDetailEdit: null,
  calendarDetailDelete: null,
  planVersions: [],
  calendarDrag: null,
  suppressCalendarClickUntil: 0,
  mapClientConfig: null,
  mapLoadPromise: null,
  lastPlanBatchId: null,
  chatLoaded: false,
  agentRuns: [],
  evaluationRun: null,
  evaluationsLoaded: false,
  planningScope: "week",
  pendingReplan: null,
  planAlternatives: null,
  planAlternativesRequest: null,
};

// 前端只认 localStorage 里存的那一个 session_id，没有会话列表也没有切换入口。
// 换 origin（localhost 和 127.0.0.1 算两个）、清缓存、换浏览器都会拿到新 id，
// 看起来像"历史没了"——其实数据都在库里，只是没有入口找回去。
// 带 ?session=xxx 可跳过 localStorage 直接定位到某个已知会话。
const urlSession = (
  new URLSearchParams(window.location.search).get("session") || ""
).trim();
const hasUrlSession = /^[A-Za-z0-9_-]{1,64}$/.test(urlSession);

state.chatSessionId =
  (hasUrlSession ? urlSession : "") ||
  localStorage.getItem("planpilot_session_id") ||
  (window.crypto && crypto.randomUUID
    ? crypto.randomUUID().replace(/-/g, "")
    : `session_${Date.now()}`);
localStorage.setItem("planpilot_session_id", state.chatSessionId);


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

  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : new Date(value.getTime());
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

// 会话条里只放得下一行小字，完整时间（2026-10-26 21:10）连年份都挤进去反而
// 最难扫。近两天用"今天 / 昨天"，今年省掉年份，跨年才写全。
function formatSessionTime(value) {
  const date = parseDate(value);
  if (!date) {
    return "";
  }
  const now = new Date();
  const sameDay = (a, b) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();

  if (sameDay(date, now)) {
    return `今天 ${formatTime(date)}`;
  }

  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (sameDay(date, yesterday)) {
    return `昨天 ${formatTime(date)}`;
  }

  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  if (date.getFullYear() === now.getFullYear()) {
    return `${month}-${day}`;
  }
  return `${date.getFullYear()}-${month}-${day}`;
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

function updateFutureInputMinimums() {
  const minimum = toDateTimeLocalValue(new Date(Date.now() + 60 * 1000));
  for (const id of [
    "planStart",
    "taskStartTime",
    "taskEarliestStart",
    "taskDeadline",
    "eventStartTime",
    "eventEndTime",
  ]) {
    const input = $(id);
    if (input) {
      input.min = minimum;
    }
  }
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

function eventDurationText(event) {
  const start = parseDate(event.start_time);
  const end = parseDate(event.end_time);
  if (!start || !end) {
    return "";
  }
  return `${formatDateTime(event.start_time)} - ${formatTime(end)}`;
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

  document.querySelectorAll(".sidebar nav button").forEach((button) => {
    const active = button.dataset.page === pageName;
    button.classList.toggle("active", active);
    if (active) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
    }
  });

  if (pageName === "tasks" || pageName === "dashboard") {
    loadTasks().catch(handleError);
  } else if (pageName === "events") {
    loadEvents().catch(handleError);
  } else if (pageName === "calendar" || pageName === "planning") {
    loadAllData()
      .then(() => {
        if (pageName === "planning") {
          renderSchedule(null);
          loadPlanVersions().catch(handleError);
        }
      })
      .catch(handleError);
  } else if (pageName === "itinerary") {
    loadItinerary().catch(handleError);
  } else if (pageName === "chat") {
    Promise.all([
      loadChatHistory(),
      loadAgentStatus(),
      loadAgentRuns(),
      loadSessions(),
    ]).catch(handleError);
  } else if (pageName === "evaluations") {
    loadEvaluation().catch(handleError);
  } else if (pageName === "settings") {
    Promise.all([loadPreferences(), loadLocations()]).catch(handleError);
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

async function loadEvents() {
  try {
    state.events = await api("/events");
    renderEvents();
  } catch (error) {
    const container = $("eventList");
    if (container) {
      container.innerHTML = `
        <div class="empty error">加载固定日程失败：${escapeHtml(error.message)}</div>
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
  renderEvents();
  renderDashboardTasks();
  updateDashboard();
  renderDashboardSchedules();
  renderTimetables();
  renderPlanningOverview();
}

async function refreshSchedules() {
  state.schedules = await api("/schedules");
  renderTimetables();
  renderPlanningOverview();
}


/* =========================
   任务
========================= */

function renderTasks() {
  const container = $("taskList");
  if (!container) {
    return;
  }

  const tasks = getFilteredTasks();
  if (tasks.length === 0) {
    container.innerHTML = `
      <div class="empty">
        没有符合当前条件的任务
      </div>
    `;
    return;
  }

  container.innerHTML = tasks.map(renderTaskItem).join("");
}

function getFilteredTasks() {
  const search = ($("taskSearch")?.value || "").trim().toLowerCase();
  const status = $("taskStatusFilter")?.value || "";
  const priority = $("taskPriorityFilter")?.value || "";
  const sort = $("taskSort")?.value || "created_desc";

  const tasks = state.tasks.filter((task) => {
    const matchesSearch =
      !search ||
      `${task.title || ""} ${task.description || ""} ${task.category || ""}`
        .toLowerCase()
        .includes(search);
    return (
      matchesSearch &&
      (!status || task.status === status) &&
      (!priority || task.priority === priority)
    );
  });

  const valueOfDeadline = (task) => parseDate(task.deadline)?.getTime() ?? Infinity;
  const comparators = {
    deadline_asc: (a, b) => valueOfDeadline(a) - valueOfDeadline(b),
    duration_asc: (a, b) => a.estimated_duration - b.estimated_duration,
    title_asc: (a, b) => a.title.localeCompare(b.title, "zh-CN"),
  };
  if (comparators[sort]) {
    tasks.sort(comparators[sort]);
  }
  return tasks;
}

function applyTaskFilters() {
  renderTasks();
}

function renderTaskItem(task) {
  const done = task.status === "completed";

  const meta = [
    `时长 ${task.estimated_duration} 分钟`,
    `优先级：${getPriorityName(task.priority)}`,
  ];

  const startText = formatDateTime(task.start_time);
  if (startText) {
    meta.unshift(`开始：${startText}`);
  }
  const earliestText = formatDateTime(task.earliest_start);
  if (earliestText) {
    meta.push(`最早：${earliestText}`);
  }
  const deadlineText = formatDateTime(task.deadline);
  if (deadlineText) {
    meta.push(`截止：${deadlineText}`);
  }
  if (task.preferred_time) {
    meta.push(`偏好：${task.preferred_time}`);
  }
  if (task.category) {
    meta.push(`分类：${task.category}`);
  }
  if (task.splittable) {
    meta.push(`可拆分：${task.min_chunk_minutes}–${task.max_chunk_minutes} 分钟`);
  }
  if (task.dependency_ids?.length) {
    const dependencyNames = task.dependency_ids
      .map((id) => state.tasks.find((item) => item.id === id)?.title || `#${id}`)
      .join("、");
    meta.push(`前置：${dependencyNames}`);
  }
  const taskLocation = locationById(task.location_id);
  if (taskLocation || task.location) {
    meta.push(`地点：${taskLocation?.label || task.location}`);
  }

  const description = task.description
    ? `<div class="task-desc">${escapeHtml(task.description)}</div>`
    : "";

  return `
    <div class="task ${done ? "task-done" : ""} priority-${task.priority}">
      <div class="task-main">
        <div class="task-heading">
          <div class="task-title">${escapeHtml(task.title)}</div>
          <span class="state-pill state-${task.status}">${escapeHtml(getStatusName(task.status))}</span>
        </div>
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
          <span class="mini-task-title"><i class="priority-dot priority-${task.priority}"></i>${escapeHtml(task.title)}</span>
          <span class="mini-task-meta">
            ${startText ? escapeHtml(startText) : "未设置时间"} ·
            ${escapeHtml(getStatusName(task.status))}
          </span>
        </div>
      `;
    })
    .join("");
}

function renderDashboardSchedules() {
  const container = $("dashboardScheduleList");
  if (!container) {
    return;
  }
  const now = new Date();
  const upcoming = state.schedules
    .filter(
      (item) =>
        ["proposed", "accepted"].includes(item.status) &&
        parseDate(item.end_time) > now
    )
    .slice(0, 4);
  if (!upcoming.length) {
    container.innerHTML = `<div class="empty">还没有即将开始的日程</div>`;
    return;
  }
  container.innerHTML = upcoming.map(renderScheduleItem).join("");
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
  const minChunk = Number($("taskMinChunk").value) || 30;
  const maxChunk = Number($("taskMaxChunk").value) || 120;
  if (minChunk > maxChunk) {
    alert("最短分段不能大于最长分段");
    return;
  }

  const startValue = $("taskStartTime").value;
  const earliestValue = $("taskEarliestStart").value;
  const deadlineValue = $("taskDeadline").value;

  const location = modalLocationPayload("task");
  if (!location) {
    return;
  }

  const data = {
    title,
    description: $("taskDescription").value.trim() || null,
    estimated_duration: duration,
    splittable: $("taskSplittable").checked,
    min_chunk_minutes: minChunk,
    max_chunk_minutes: maxChunk,
    dependency_ids: Array.from(
      document.querySelectorAll("#taskDependencyOptions input:checked"),
    ).map((input) => Number(input.value)),
    priority: $("taskPriority").value,
    start_time: startValue ? new Date(startValue).toISOString() : null,
    earliest_start: earliestValue ? new Date(earliestValue).toISOString() : null,
    deadline: deadlineValue ? new Date(deadlineValue).toISOString() : null,
    preferred_time: $("taskPreferredTime").value.trim() || null,
    category: $("taskCategory").value.trim() || null,
    ...location,
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

  const pending = countOf("pending");
  const inProgress = countOf("in_progress");
  const completed = countOf("completed");
  const cancelled = countOf("cancelled");
  const activeTotal = pending + inProgress + completed;
  const completionRate = activeTotal ? Math.round((completed / activeTotal) * 100) : 0;

  setText("pendingCount", pending);
  setText("inProgressCount", inProgress);
  setText("completedCount", completed);
  setText("cancelledCount", cancelled);
  setText("dashboardCompletionRate", `完成率 ${completionRate}%`);

  const now = new Date();
  setText(
    "dashboardDate",
    new Intl.DateTimeFormat("zh-CN", {
      month: "long",
      day: "numeric",
      weekday: "long",
    }).format(now),
  );
  const hour = now.getHours();
  const greeting = hour < 6 ? "夜深了，留一点时间给休息" :
    hour < 12 ? "上午好，先处理今天最重要的事情" :
    hour < 18 ? "下午好，继续稳稳推进今天的计划" :
    "晚上好，整理一下今天的进度";
  setText("dashboardGreeting", greeting);
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
  updateFutureInputMinimums();
  state.editingTaskId = id;

  if (id) {
    const task = state.tasks.find((item) => item.id === id);
    if (task) {
      $("modalTitle").textContent = "编辑任务";
      $("taskSubmitButton").textContent = "保存修改";
      $("taskTitle").value = task.title || "";
      $("taskDescription").value = task.description || "";
      $("taskStartTime").value = toDateTimeLocalValue(task.start_time);
      $("taskEarliestStart").value = toDateTimeLocalValue(task.earliest_start);
      $("taskDeadline").value = toDateTimeLocalValue(task.deadline);
      $("taskDuration").value = task.estimated_duration;
      $("taskSplittable").checked = Boolean(task.splittable);
      $("taskMinChunk").value = task.min_chunk_minutes || 30;
      $("taskMaxChunk").value = task.max_chunk_minutes || 120;
      populateTaskDependencyOptions(task.dependency_ids || [], task.id);
      toggleTaskSplitFields();
      $("taskPriority").value = task.priority;
      $("taskPreferredTime").value = task.preferred_time || "";
      $("taskCategory").value = task.category || "";
      $("taskLocationId").value = task.location_id || "";
      setModalLocationFields("task", task);
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
  $("taskEarliestStart").value = "";
  $("taskDeadline").value = "";
  $("taskDuration").value = state.preferences?.default_task_duration_min || 60;
  $("taskSplittable").checked = false;
  $("taskMinChunk").value = 30;
  $("taskMaxChunk").value = 120;
  populateTaskDependencyOptions();
  toggleTaskSplitFields();
  $("taskPriority").value = "medium";
  $("taskPreferredTime").value = "";
  $("taskCategory").value = "";
  clearModalLocation("task");
}

function toggleTaskSplitFields() {
  $("taskSplitFields")?.classList.toggle("hidden", !$("taskSplittable")?.checked);
}

function populateTaskDependencyOptions(selectedIds = [], currentTaskId = null) {
  const container = $("taskDependencyOptions");
  const noneOption = $("taskDependencyNone");
  if (!container || !noneOption) {
    return;
  }
  const selected = new Set(selectedIds.map(Number));
  const candidates = state.tasks.filter(
    (task) =>
      task.id !== currentTaskId &&
      task.status !== "cancelled" &&
      (task.status !== "completed" || selected.has(task.id)),
  );
  noneOption.checked = selected.size === 0;
  container.innerHTML = candidates
    .map(
      (task) => `
        <label class="task-dependency-option">
          <input type="checkbox" value="${task.id}" ${selected.has(task.id) ? "checked" : ""} onchange="syncTaskDependencyNone()">
          <span>${escapeHtml(task.title)}</span>
          <small>${task.status === "completed" ? "已完成" : `${escapeHtml(getPriorityName(task.priority))}优先级`}</small>
        </label>
      `,
    )
    .join("");
  if (!candidates.length) {
    container.innerHTML = '<div class="task-dependency-empty">暂无其他待完成任务</div>';
  }
}

function clearTaskDependencies() {
  if (!$("taskDependencyNone")?.checked) {
    syncTaskDependencyNone();
    return;
  }
  document
    .querySelectorAll("#taskDependencyOptions input:checked")
    .forEach((input) => { input.checked = false; });
}

function syncTaskDependencyNone() {
  const hasSelected = Boolean(
    document.querySelector("#taskDependencyOptions input:checked"),
  );
  if ($("taskDependencyNone")) {
    $("taskDependencyNone").checked = !hasSelected;
  }
}


/* =========================
   固定日程
========================= */

function renderEvents() {
  const container = $("eventList");
  if (!container) {
    return;
  }

  if (state.events.length === 0) {
    container.innerHTML = `
      <div class="empty">还没有固定日程，添加会议、预约或车次吧</div>
    `;
    return;
  }

  container.innerHTML = state.events.map(renderEventItem).join("");
}

function renderEventItem(event) {
  const meta = [eventDurationText(event)];
  if (event.trip_id) {
    meta.push(`日程组：${event.trip_title || "未命名日程组"}`);
  }
  const savedLocation = locationById(event.location_id);
  const savedOrigin = locationById(event.origin_location_id);
  if (event.event_type === "travel") {
    meta.push("类型：赶路");
    meta.push(`方式：${travelModeName(event.travel_mode)}`);
    const origin = savedOrigin?.label || event.origin_location || "继承上一地点";
    const destination = savedLocation?.label || event.location || "未指定目的地";
    meta.push(`路线：${origin} → ${destination}`);
  } else if (savedLocation || event.location) {
    meta.push(`地点：${savedLocation?.label || event.location}`);
  }

  const description = event.description
    ? `<div class="task-desc">${escapeHtml(event.description)}</div>`
    : "";

  return `
    <div class="event ${event.event_type === "travel" ? "event-travel" : "event-fixed"}">
      <div class="task-main">
        <div class="task-title"><span class="kind-badge fixed-kind">固定</span>${escapeHtml(event.title)}</div>
        <div class="task-info">${escapeHtml(meta.filter(Boolean).join(" · "))}</div>
        ${description}
      </div>

      <div class="task-actions">
        <button class="ghost-button" onclick="openEventModal(${event.id})">
          编辑
        </button>
        <button class="delete-button" onclick="deleteEvent(${event.id})">
          删除
        </button>
      </div>
    </div>
  `;
}

function openEventModal(id = null) {
  updateFutureInputMinimums();
  state.editingEventId = id;

  if (id) {
    const event = state.events.find((item) => item.id === id);
    if (event) {
      $("eventModalTitle").textContent = "编辑固定日程";
      $("eventSubmitButton").textContent = "保存修改";
      $("eventTitle").value = event.title || "";
      $("eventDescription").value = event.description || "";
      $("eventStartTime").value = toDateTimeLocalValue(event.start_time);
      $("eventEndTime").value = toDateTimeLocalValue(event.end_time);
      $("eventType").value = event.event_type || "activity";
      $("eventTravelMode").value = event.travel_mode || "transit";
      setModalLocationFields("event", event);
      setModalLocationFields("eventOrigin", event);
      toggleEventLocationMode();
      $("eventRecurrence").value = "";
      $("eventRecurrence").disabled = true;
      toggleRecurrenceFields();
    }
  } else {
    $("eventModalTitle").textContent = "新建固定日程";
    $("eventSubmitButton").textContent = "创建固定日程";
    clearEventForm();
    $("eventRecurrence").disabled = false;
  }

  $("eventModal").classList.remove("hidden");
}

function closeEventModal() {
  $("eventModal").classList.add("hidden");
  state.editingEventId = null;
}

function clearEventForm() {
  $("eventTitle").value = "";
  $("eventDescription").value = "";
  $("eventStartTime").value = "";
  $("eventEndTime").value = "";
  $("eventType").value = "activity";
  $("eventTravelMode").value = "transit";
  clearModalLocation("event");
  clearModalLocation("eventOrigin");
  toggleEventLocationMode();
  $("eventRecurrence").value = "";
  $("eventOccurrences").value = 4;
  toggleRecurrenceFields();
}

function toggleRecurrenceFields() {
  const repeated = Boolean($("eventRecurrence")?.value);
  $("eventOccurrencesLabel")?.classList.toggle("hidden", !repeated);
}

function toggleEventLocationMode() {
  const isTravel = $("eventType")?.value === "travel";
  $("eventOriginLocationEditor")?.classList.toggle("hidden", !isTravel);
  $("eventTravelModeLabel")?.classList.toggle("hidden", !isTravel);
  if ($("eventDestinationHeading")) {
    $("eventDestinationHeading").textContent = isTravel ? "目的地" : "地点";
  }
}

async function submitEvent() {
  const title = $("eventTitle").value.trim();
  const startValue = $("eventStartTime").value;
  const endValue = $("eventEndTime").value;

  if (!title) {
    alert("请输入行程名称");
    return;
  }
  if (!startValue || !endValue) {
    alert("请选择开始和结束时间");
    return;
  }
  if (new Date(endValue) <= new Date(startValue)) {
    alert("结束时间必须晚于开始时间");
    return;
  }

  const location = modalLocationPayload("event");
  if (!location) {
    return;
  }
  const isTravel = $("eventType").value === "travel";
  const originLocation = isTravel
    ? modalLocationPayload("eventOrigin")
    : emptyModalLocationPayload("origin_location");
  if (!originLocation) {
    return;
  }

  const data = {
    title,
    description: $("eventDescription").value.trim() || null,
    start_time: new Date(startValue).toISOString(),
    end_time: new Date(endValue).toISOString(),
    event_type: $("eventType").value,
    travel_mode: isTravel ? $("eventTravelMode").value : null,
    ...location,
    ...originLocation,
  };

  const recurrence = $("eventRecurrence").value;
  if (!state.editingEventId && recurrence) {
    data.recurrence = recurrence;
    data.occurrences = Number($("eventOccurrences").value) || 4;
  }

  try {
    if (state.editingEventId) {
      await api(`/events/${state.editingEventId}`, {
        method: "PUT",
        body: JSON.stringify(data),
      });
    } else {
      await api(recurrence ? "/events/series" : "/events", {
        method: "POST",
        body: JSON.stringify(data),
      });
    }

    closeEventModal();
    await loadAllData();
  } catch (error) {
    alert(`保存失败：${error.message}`);
  }
}

async function deleteEvent(id) {
  const event = state.events.find((item) => item.id === Number(id));
  if (event?.trip_id) {
    openCalendarItemDetails("event", id);
    return;
  }
  if (!confirm(`确定删除固定日程「${event?.title || `#${id}`}」吗？`)) {
    return;
  }

  try {
    await api(`/events/${id}`, { method: "DELETE" });
    await loadAllData();
  } catch (error) {
    alert(`删除失败：${error.message}`);
  }
}


/* =========================
   按日地点路线
========================= */

function localDateInputValue(date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function setItineraryToday() {
  $("itineraryDate").value = localDateInputValue();
  loadItinerary().catch(handleError);
}

async function loadItinerary() {
  const dateInput = $("itineraryDate");
  if (!dateInput) {
    return;
  }
  if (!dateInput.value) {
    dateInput.value = localDateInputValue();
  }
  const mode = $("itineraryMode").value;
  const originValue = $("itineraryOrigin")?.value || "auto";
  const destinationValue = $("itineraryDestination")?.value || "last";
  const carAvailable = $("itineraryCar").checked;
  const parkingMinutes = Math.max(0, Math.min(60, Number($("itineraryParking").value) || 0));
  const query = new URLSearchParams({
    date: dateInput.value,
    mode,
    include_return: String(destinationValue === "origin"),
    car_available: String(carAvailable),
    parking_minutes: String(parkingMinutes),
    origin_strategy: originValue === "auto" ? "automatic" : "default",
  });
  if (originValue.startsWith("location:")) {
    query.set("origin_location_id", originValue.slice("location:".length));
  }
  if (destinationValue.startsWith("location:")) {
    query.set("destination_location_id", destinationValue.slice("location:".length));
  }
  $("itineraryTimeline").innerHTML = '<div class="itinerary-empty">正在整理行程...</div>';
  try {
    state.itinerary = await api(`/itineraries/day?${query.toString()}`);
    renderItineraryTimeline();
    await renderItineraryMap();
  } catch (error) {
    $("itineraryTimeline").innerHTML =
      `<div class="itinerary-empty error">加载失败：${escapeHtml(error.message)}</div>`;
    showMapFallback("暂时无法加载地点路线");
  }
}

function renderItineraryTimeline() {
  const itinerary = state.itinerary;
  const container = $("itineraryTimeline");
  const warningBox = $("itineraryWarnings");
  if (!itinerary || !container) {
    return;
  }

  if (itinerary.warnings.length) {
    warningBox.innerHTML = itinerary.warnings
      .map((warning) => `<div>${escapeHtml(warning)}</div>`)
      .join("");
    warningBox.classList.remove("hidden");
  } else {
    warningBox.classList.add("hidden");
  }

  if (!itinerary.items.length) {
    container.innerHTML = '<div class="itinerary-empty">这一天还没有固定日程或任务安排</div>';
    return;
  }

  const legsByDestination = new Map(
    itinerary.legs.map((leg) => [leg.destination_id, leg]),
  );
  const rows = [];
  const firstLocatedItem = itinerary.items.find((item) => item.location);
  const originIsFirstStop = itinerary.origin_strategy === "automatic"
    && sameItineraryCoordinate(itinerary.origin, firstLocatedItem?.location);
  if (itinerary.origin && !originIsFirstStop) {
    rows.push(`
      <div class="itinerary-origin">
        <span class="itinerary-pin origin-pin">起</span>
        <div><strong>${escapeHtml(itinerary.origin.label)}</strong><small>${itinerary.origin_strategy === "automatic" ? "按首段行程判断" : "选择的出发点"}</small></div>
      </div>
    `);
  }

  itinerary.items.forEach((item, index) => {
    const previousItem = index > 0 ? itinerary.items[index - 1] : null;
    if (item.trip_id && item.trip_id !== previousItem?.trip_id) {
      rows.push(`
        <div class="itinerary-group-label">
          <strong>${escapeHtml(item.trip_title || "未命名行程")}</strong>
          <span>${escapeHtml(`${itinerary.items.filter((candidate) => candidate.trip_id === item.trip_id).length} 段`)}</span>
        </div>
      `);
    }
    const leg = legsByDestination.get(item.id);
    if (leg) {
      const routeText = leg.duration_minutes === null
        ? "路线待计算"
        : leg.duration_minutes === 0
          ? "同一地点，无需移动"
          : `${travelModeName(leg.display_mode || leg.mode)} ${leg.duration_minutes} 分钟${leg.parking_minutes ? `（含停车 ${leg.parking_minutes} 分钟）` : ""}${leg.distance_m === null ? "" : ` · ${formatDistance(leg.distance_m)}`}`;
      const departText = leg.suggested_departure
        ? `建议 ${formatTime(parseDate(leg.suggested_departure))} 出发`
        : "";
      const carText = leg.car_location_after && itinerary.car_available
        ? `车辆位置：${leg.car_location_after}`
        : "";
      rows.push(`
        <div class="itinerary-leg ${leg.warning ? "leg-warning" : ""}">
          <span>${escapeHtml(routeText)}</span>
          ${departText ? `<small>${escapeHtml(departText)}</small>` : ""}
          ${leg.recommendation_reason ? `<small>${escapeHtml(leg.recommendation_reason)}</small>` : ""}
          ${carText ? `<small>${escapeHtml(carText)}</small>` : ""}
          ${leg.warning ? `<strong>${escapeHtml(leg.warning)}</strong>` : ""}
        </div>
      `);
    }

    const locationText = item.event_type === "travel"
      ? `${item.origin_location?.label || "上一地点"} → ${item.location?.label || "未指定目的地"}`
      : item.location?.label || "未绑定地点";
    const statusText = item.kind === "event"
      ? item.event_type === "travel" ? "赶路" : "固定日程"
      : item.status === "accepted" ? "已确认任务" : "待确认任务";
    const itemInteraction = item.location
      ? `role="button" tabindex="0" onclick="focusItineraryItem('${item.id}')" onkeydown="handleItineraryItemKey(event, '${item.id}')"`
      : "";
    rows.push(`
      <div class="itinerary-item ${item.location ? "has-location" : "without-location"}" ${itemInteraction}>
        <span class="itinerary-pin">${index + 1}</span>
        <div class="itinerary-item-copy">
          <div class="itinerary-item-time">${formatTime(parseDate(item.start_time))}–${formatTime(parseDate(item.end_time))}</div>
          <strong>${escapeHtml(item.title)}</strong>
          <span>${escapeHtml(locationText)} · ${escapeHtml(statusText)}</span>
        </div>
      </div>
    `);
  });
  const endpointLeg = itinerary.legs.find((leg) =>
    ["origin-return", "trip-end"].includes(leg.destination_id),
  );
  if (endpointLeg && itinerary.destination) {
    const routeText = endpointLeg.duration_minutes === null
      ? "终点路线待计算"
      : `${travelModeName(endpointLeg.display_mode || endpointLeg.mode)} ${endpointLeg.duration_minutes} 分钟${endpointLeg.distance_m === null ? "" : ` · ${formatDistance(endpointLeg.distance_m)}`}`;
    rows.push(`
      <div class="itinerary-leg return-leg">
        <span>${escapeHtml(routeText)}</span>
        ${endpointLeg.estimated_arrival ? `<small>预计 ${formatTime(parseDate(endpointLeg.estimated_arrival))} 到达</small>` : ""}
      </div>
      <div class="itinerary-origin return-origin">
        <span class="itinerary-pin origin-pin">终</span>
        <div><strong>${escapeHtml(itinerary.destination.label)}</strong><small>${endpointLeg.destination_id === "origin-return" ? "返回出发点" : "选择的终点"}</small></div>
      </div>
    `);
  }
  container.innerHTML = rows.join("");
}

function travelModeName(mode) {
  return {
    smart: "智能推荐",
    transit: "公交 / 地铁",
    taxi: "打车",
    driving: "自驾",
    walking: "步行",
  }[mode] || mode;
}

function handleItineraryItemKey(event, itemId) {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    focusItineraryItem(itemId);
  }
}

function focusItineraryItem(itemId) {
  const route = state.itineraryRoutes.get(itemId);
  if (route) {
    route.focus();
    return;
  }
  const entry = state.itineraryMarkers.get(itemId);
  if (entry) {
    entry.open();
  }
}

function formatDistance(distance) {
  return distance >= 1000 ? `${(distance / 1000).toFixed(1)} 公里` : `${distance} 米`;
}

function sameItineraryCoordinate(first, second) {
  if (!first?.coordinate || !second?.coordinate) {
    return false;
  }
  return Number(first.coordinate.longitude).toFixed(6) === Number(second.coordinate.longitude).toFixed(6)
    && Number(first.coordinate.latitude).toFixed(6) === Number(second.coordinate.latitude).toFixed(6);
}

async function loadMapClient() {
  if (window.AMap) {
    return window.AMap;
  }
  if (state.mapLoadPromise) {
    return state.mapLoadPromise;
  }
  state.mapLoadPromise = (async () => {
    state.mapClientConfig = await api("/maps/client-config");
    if (!state.mapClientConfig.configured) {
      throw new Error(state.mapClientConfig.message);
    }
    window._AMapSecurityConfig = {
      securityJsCode: state.mapClientConfig.js_security_code,
    };
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(state.mapClientConfig.js_api_key)}`;
      script.async = true;
      script.onload = resolve;
      script.onerror = () => reject(new Error("地图脚本加载失败"));
      document.head.appendChild(script);
    });
    return window.AMap;
  })();
  try {
    return await state.mapLoadPromise;
  } catch (error) {
    state.mapLoadPromise = null;
    throw error;
  }
}

async function renderItineraryMap() {
  const itinerary = state.itinerary;
  const mapElement = $("itineraryMap");
  if (!itinerary || !mapElement) {
    return;
  }
  state.itineraryMarkers.clear();
  state.itineraryRoutes.clear();
  if (state.itineraryInfoWindow) {
    state.itineraryInfoWindow.close();
    state.itineraryInfoWindow = null;
  }
  const locatedItems = itinerary.items.filter((item) => item.location);
  if (!locatedItems.length && !itinerary.origin) {
    showMapFallback("当天事项还没有绑定精确地点");
    return;
  }

  let AMap;
  try {
    AMap = await loadMapClient();
  } catch (error) {
    showMapFallback(error.message);
    return;
  }
  $("mapFallback").classList.add("hidden");
  mapElement.classList.remove("hidden");

  if (!state.itineraryMap) {
    state.itineraryMap = new AMap.Map("itineraryMap", {
      zoom: 12,
      viewMode: "2D",
      mapStyle: "amap://styles/whitesmoke",
    });
  } else {
    state.itineraryMap.clearMap();
  }

  const overlays = [];
  const markerGroups = new Map();
  const openMarkerInfo = (entries, position) => {
    if (state.itineraryInfoWindow) {
      state.itineraryInfoWindow.close();
    }
    const content = entries
      .map((entry) => `
        <div class="map-info-entry">
          <strong>${escapeHtml(entry.title)}</strong>
          <span>${escapeHtml(entry.location.address || entry.location.label)}</span>
        </div>
      `)
      .join("");
    const info = new AMap.InfoWindow({
      content: `<div class="map-info">${content}</div>`,
      offset: new AMap.Pixel(0, -30),
    });
    state.itineraryInfoWindow = info;
    state.itineraryMap.setCenter(position);
    info.open(state.itineraryMap, position);
  };
  const addMarker = (itemId, location, label, title) => {
    const position = [location.coordinate.longitude, location.coordinate.latitude];
    const coordinateKey = `${Number(position[0]).toFixed(6)},${Number(position[1]).toFixed(6)}`;
    let group = markerGroups.get(coordinateKey);
    if (!group) {
      const marker = new AMap.Marker({
        position,
        anchor: "bottom-center",
        title,
        content: `<div class="map-number-marker"><span>${escapeHtml(label)}</span></div>`,
      });
      group = { marker, position, entries: [] };
      markerGroups.set(coordinateKey, group);
      marker.on("click", () => openMarkerInfo(group.entries, group.position));
      overlays.push(marker);
    }
    const markerEntry = { itemId, title, location };
    group.entries.push(markerEntry);
    const entry = {
      marker: group.marker,
      open: () => openMarkerInfo([markerEntry], group.position),
    };
    state.itineraryMarkers.set(itemId, entry);
    return entry;
  };

  if (itinerary.origin) {
    addMarker("origin", itinerary.origin, "起", itinerary.origin.label);
  }
  locatedItems.forEach((item) => {
    const timelineIndex = itinerary.items.findIndex(
      (candidate) => candidate.id === item.id,
    );
    if (item.event_type === "travel" && item.origin_location) {
      addMarker(
        `${item.id}:origin`,
        item.origin_location,
        "出",
        `${item.title}：出发`,
      );
    }
    addMarker(
      item.id,
      item.location,
      timelineIndex + 1,
      item.title,
    );
  });

  const endpointLeg = itinerary.legs.find((leg) =>
    ["origin-return", "trip-end"].includes(leg.destination_id),
  );
  if (endpointLeg && itinerary.destination) {
    addMarker(
      endpointLeg.destination_id,
      itinerary.destination,
      "终",
      itinerary.destination.label,
    );
  }

  itinerary.legs.forEach((leg) => {
    let polyline = null;
    if (leg.path && leg.path.length >= 2) {
      const modeColors = { walking: "#16a34a", transit: "#0f766e", driving: "#2563eb" };
      polyline = new AMap.Polyline({
        path: leg.path.map((point) => [point.longitude, point.latitude]),
        strokeColor: leg.warning ? "#c2410c" : leg.route_available ? (modeColors[leg.mode] || "#0f766e") : "#94a3b8",
        strokeWeight: 5,
        strokeOpacity: 0.9,
        strokeStyle: leg.route_available ? "solid" : "dashed",
        lineJoin: "round",
      });
      overlays.push(polyline);
    }
    const travelItem = itinerary.items.find(
      (item) => item.id === leg.destination_id && item.event_type === "travel",
    );
    if (travelItem) {
      const originEntry = state.itineraryMarkers.get(`${travelItem.id}:origin`);
      const destinationEntry = state.itineraryMarkers.get(travelItem.id);
      state.itineraryRoutes.set(travelItem.id, {
        focus: () => {
          originEntry?.open();
          const routeOverlays = [...new Set([
            polyline,
            originEntry?.marker,
            destinationEntry?.marker,
          ].filter(Boolean))];
          if (routeOverlays.length > 1) {
            state.itineraryMap.setFitView(routeOverlays, false, [80, 80, 80, 80]);
          } else if (originEntry?.marker) {
            state.itineraryMap.setZoomAndCenter(15, originEntry.marker.getPosition());
          }
        },
      });
    }
  });
  state.itineraryMap.add(overlays);
  state.itineraryMap.setFitView(overlays, false, [60, 60, 60, 60]);
}

function showMapFallback(message) {
  const fallback = $("mapFallback");
  $("itineraryMap")?.classList.add("hidden");
  fallback.textContent = message;
  fallback.classList.remove("hidden");
  state.itineraryMarkers.clear();
  state.itineraryRoutes.clear();
}


/* =========================
   日程规划
========================= */

function setPlanningScope(scope) {
  state.planningScope = scope;
  const now = new Date();
  if (scope === "upcoming") {
    $("planStart").value = toDateTimeLocalValue(startOfDay(now));
    $("planDays").value = 7;
  } else if (scope === "week") {
    syncPlanningWeekRange();
  }
  for (const name of ["upcoming", "week"]) {
    const button = $(`planScope${name[0].toUpperCase()}${name.slice(1)}`);
    button?.classList.toggle("active", name === scope);
  }
  closePlanComparison();
  closePlanAlternatives();
  renderPlanningOverview();
  renderTimetables();
}

function syncPlanningWeekRange() {
  const now = new Date();
  const weekStart = state.weekStart || startOfWeek(now);
  const weekEnd = addDays(weekStart, 7);
  const rangeStart = weekStart <= now && now < weekEnd ? now : weekStart;
  $("planStart").value = toDateTimeLocalValue(rangeStart);
  $("planDays").value = Math.max(
    1,
    Math.ceil((weekEnd - rangeStart) / (24 * 60 * 60 * 1000)),
  );
}

function setPlanningMode(mode) {
  const labels = {
    auto: "系统会根据任务数量、日期跨度、地点与约束自动选择",
    quick: "使用现有规则引擎，适合少量任务和简单约束",
    optimized: "使用 CP-SAT 全局求解，适合一周计划和复杂约束",
  };
  const input = $("planMode");
  if (input) {
    input.value = mode;
  }
  for (const name of ["auto", "quick", "optimized"]) {
    const button = $(`planMode${name[0].toUpperCase()}${name.slice(1)}`);
    button?.classList.toggle("active", name === mode);
  }
  if ($("planModeHint")) {
    $("planModeHint").textContent = labels[mode] || labels.auto;
  }
  closePlanAlternatives();
}

function markPlanningScopeCustom() {
  // 用户手动改了 planStart / planDays 输入框；范围不再是任何预设。
  // planningRange() 在 scope 不是 upcoming/week 时回退到自定义的 start + days。
  if (state.planningScope !== "custom") {
    state.planningScope = "custom";
    for (const name of ["upcoming", "week"]) {
      const button = $(`planScope${name[0].toUpperCase()}${name.slice(1)}`);
      button?.classList.toggle("active", false);
    }
  }
  closePlanAlternatives();
  renderPlanningOverview();
}

function planningRange() {
  const rawStart = $("planStart")?.value;
  const start = rawStart ? new Date(rawStart) : new Date();
  const days = Math.max(1, Number($("planDays")?.value) || 1);
  const end = state.planningScope === "upcoming"
    ? addDays(startOfDay(new Date()), 7)
    : state.planningScope === "week"
      ? addDays(startOfWeek(start), 7)
      : addDays(start, days);
  return { start, end, days };
}

function renderPlanningOverview() {
  const container = $("planningOverview");
  if (!container) {
    return;
  }
  const { start, end } = planningRange();
  const schedules = state.schedules.filter((item) => {
    const itemStart = parseDate(item.start_time);
    return itemStart && itemStart >= start && itemStart < end
      && ["proposed", "accepted"].includes(item.status);
  });
  const arrangedTaskIds = new Set(schedules.map((item) => item.task_id));
  const pendingTasks = state.tasks.filter(
    (task) => ["pending", "in_progress"].includes(task.status)
      && !arrangedTaskIds.has(task.id),
  ).length;
  const minutes = schedules.reduce((total, item) => {
    const itemStart = parseDate(item.start_time);
    const itemEnd = parseDate(item.end_time);
    return total + (itemStart && itemEnd ? Math.max(0, (itemEnd - itemStart) / 60000) : 0);
  }, 0);
  const locked = schedules.filter((item) => item.is_locked).length;
  container.innerHTML = `
    <span><strong>${schedules.length}</strong> 个时间块</span>
    <span><strong>${(minutes / 60).toFixed(1)}</strong> 小时</span>
    <span><strong>${pendingTasks}</strong> 个待安排任务</span>
    <span><strong>${locked}</strong> 个已锁定</span>
  `;
}

async function generateSchedule() {
  const container = $("scheduleResult");
  container.innerHTML = `<div class="empty">正在规划…</div>`;

  try {
    const payload = collectPlanPayload();

    const result = await api("/schedules/generate", {
      method: "POST",
      body: JSON.stringify(payload),
    });

    state.lastPlanBatchId = result.batch_id || null;
    await loadAllData();
    renderSchedule(result);
    renderPlanAnalysis(result.analysis, "规划完成");
    closePlanComparison();
  } catch (error) {
    container.innerHTML = `
      <div class="empty error">
        规划失败：${escapeHtml(error.message)}
      </div>
    `;
  }
}

function collectPlanPayload() {
  const startValue = $("planStart").value;
  const payload = {
    days: Number($("planDays").value) || 7,
    planning_mode: $("planMode")?.value || "auto",
  };
  const gapValue = $("planGap").value;
  const strategy = $("planStrategy").value;
  if (gapValue !== "") {
    payload.gap_minutes = Number(gapValue);
  }
  if (strategy) {
    payload.strategy = strategy;
  }
  if (startValue) {
    payload.start = new Date(startValue).toISOString();
  }
  return payload;
}

async function previewSchedule() {
  try {
    const result = await api("/schedules/preview", {
      method: "POST",
      body: JSON.stringify(collectPlanPayload()),
    });
    renderPlanAnalysis(result.analysis, "预览结果");
    renderPlanComparison(result.comparison, "规划方案对比");
  } catch (error) {
    renderPlanAnalysis({ score: 0, issues: [error.message] }, "预览失败");
  }
}

async function loadPlanAlternatives() {
  const panel = $("planAlternatives");
  const grid = $("planAlternativesGrid");
  panel.classList.remove("hidden");
  grid.innerHTML = '<div class="empty">正在生成三套可行方案...</div>';
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const payload = collectPlanPayload();
    const result = await api("/schedules/alternatives", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.planAlternatives = result;
    state.planAlternativesRequest = payload;
    renderPlanAlternatives(result);
  } catch (error) {
    state.planAlternatives = null;
    state.planAlternativesRequest = null;
    grid.innerHTML = `<div class="empty error">方案生成失败：${escapeHtml(error.message)}</div>`;
  }
}

function renderPlanAlternatives(result) {
  const alternatives = result.alternatives || [];
  $("planAlternativesGrid").innerHTML = alternatives.map((alternative) => {
    const metrics = alternative.metrics || {};
    const schedules = alternative.schedules || [];
    const timeline = schedules.slice(0, 6).map((item) => {
      const task = state.tasks.find((candidate) => candidate.id === item.task_id);
      const taskMeta = task
        ? [
            `优先级：${getPriorityName(task.priority)}`,
            task.deadline ? `截止：${formatDateTime(task.deadline)}` : "无截止时间",
          ].join(" · ")
        : "";
      return `<li><span>${escapeHtml(planComparisonTime(item))}</span><div><strong>${escapeHtml(task?.title || `任务 #${item.task_id}`)}</strong><small>${escapeHtml(taskMeta)}</small></div></li>`;
    }).join("");
    const remaining = Math.max(0, schedules.length - 6);
    const strengths = (alternative.strengths || []).slice(0, 3);
    const tradeoffs = (alternative.tradeoffs || []).slice(0, 2);
    const unscheduled = (alternative.unscheduled || []).map(
      (item) => `<li><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.reason)}</span></li>`,
    ).join("");
    return `
      <article class="plan-alternative ${alternative.recommended ? "recommended" : ""}">
        <div class="plan-alternative-head">
          <div><h4>${escapeHtml(alternative.title)}</h4><p>${escapeHtml(alternative.summary)}</p></div>
          ${alternative.recommended ? '<span class="recommended-badge">推荐</span>' : ""}
        </div>
        <div class="plan-alternative-outcomes">${(alternative.outcome_labels || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
        <dl class="plan-alternative-policy">
          <div><dt>优先规则</dt><dd>${escapeHtml(alternative.selection_policy || "")}</dd></div>
          <div><dt>安排节奏</dt><dd>${escapeHtml(alternative.pace || "")}</dd></div>
        </dl>
        <div class="plan-alternative-metrics">
          <div><strong>${escapeHtml(alternative.analysis?.score ?? 0)}</strong><span>计划评分</span></div>
          <div><strong>${escapeHtml(metrics.scheduled_tasks || 0)}</strong><span>已安排任务</span></div>
          <div><strong>${escapeHtml(formatMinutesCompact(metrics.max_daily_minutes || 0))}</strong><span>单日最高</span></div>
          <div><strong>${escapeHtml(metrics.days_used || 0)}</strong><span>占用天数</span></div>
          <div><strong>${escapeHtml(formatMinutesCompact(metrics.idle_minutes || 0))}</strong><span>任务间空档</span></div>
          <div><strong>${escapeHtml(metrics.changed_blocks || 0)}</strong><span>变更时间块</span></div>
        </div>
        ${alternative.recommendation_reason ? `<p class="plan-alternative-reason"><strong>推荐依据</strong>${escapeHtml(alternative.recommendation_reason)}</p>` : ""}
        <div class="plan-alternative-explanation">
          <div><strong>优势</strong><ul>${strengths.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
          <div><strong>取舍</strong><ul>${tradeoffs.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
        </div>
        <ol class="plan-alternative-timeline">${timeline}</ol>
        ${remaining ? `<p class="plan-alternative-more">另有 ${remaining} 个时间块</p>` : ""}
        ${unscheduled ? `<div class="plan-alternative-unscheduled"><strong>未安排</strong><ul>${unscheduled}</ul></div>` : ""}
        <div class="plan-alternative-actions">
          <button class="ghost-button" type="button" onclick="showAlternativeComparison('${alternative.profile}')">查看变更</button>
          <button class="primary-button" type="button" onclick="applyPlanAlternative('${alternative.profile}')">应用此方案</button>
        </div>
      </article>
    `;
  }).join("");
}

function formatMinutesCompact(minutes) {
  if (minutes < 60) {
    return `${minutes}分`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}时${rest}分` : `${hours}小时`;
}

function showAlternativeComparison(profile) {
  const alternative = state.planAlternatives?.alternatives?.find(
    (item) => item.profile === profile,
  );
  if (alternative) {
    renderPlanComparison(alternative.comparison, `${alternative.title} · 变更详情`);
  }
}

async function applyPlanAlternative(profile) {
  const alternative = state.planAlternatives?.alternatives?.find(
    (item) => item.profile === profile,
  );
  if (!alternative || !confirm(`确定应用「${alternative.title}」吗？当前范围内未锁定的待确认安排将被替换。`)) {
    return;
  }
  try {
    const result = await api(`/schedules/alternatives/${profile}/apply`, {
      method: "POST",
      body: JSON.stringify(state.planAlternativesRequest || collectPlanPayload()),
    });
    state.lastPlanBatchId = result.batch_id || null;
    await loadAllData();
    renderSchedule(result);
    renderPlanAnalysis(result.analysis, `已应用：${alternative.title}`);
    closePlanAlternatives();
    closePlanComparison();
    loadPlanVersions().catch(handleError);
  } catch (error) {
    alert(`应用方案失败：${error.message}`);
  }
}

function closePlanAlternatives() {
  $("planAlternatives")?.classList.add("hidden");
  state.planAlternatives = null;
  state.planAlternativesRequest = null;
}

function renderPlanComparison(comparison, title, allowApply = false) {
  const panel = $("planComparison");
  if (!panel || !comparison) {
    return;
  }
  const counts = comparison.counts || {};
  if (!allowApply) {
    state.pendingReplan = null;
  }
  $("planComparisonTitle").textContent = title;
  $("planComparisonSummary").innerHTML = [
    ["保留", counts.preserved || 0, "preserved"],
    ["移动", counts.moved || 0, "moved"],
    ["新增", counts.added || 0, "added"],
    ["移除", counts.removed || 0, "removed"],
    ["不变", counts.unchanged || 0, "unchanged"],
  ].map(([label, value, type]) => `
    <div class="comparison-metric ${type}"><span>${label}</span><strong>${value}</strong></div>
  `).join("");

  const changed = (comparison.changes || []).filter((item) => item.type !== "unchanged");
  $("planComparisonRows").innerHTML = changed.length
    ? changed.map(renderPlanComparisonRow).join("")
    : '<div class="empty">当前方案无需调整</div>';
  $("planComparisonActions").classList.toggle("hidden", !allowApply);
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderPlanComparisonRow(change) {
  const task = state.tasks.find((item) => item.id === change.task_id);
  const title = task?.title || `任务 #${change.task_id}`;
  const labels = { added: "新增", removed: "移除", moved: "调整" };
  const before = planComparisonTime(change.before);
  const after = planComparisonTime(change.after);
  const timeMarkup = change.type === "moved"
    ? `<span>${escapeHtml(before)}</span><b>→</b><span>${escapeHtml(after)}</span>`
    : `<span>${escapeHtml(after || before)}</span>`;
  return `
    <div class="plan-comparison-row ${change.type}">
      <span class="comparison-type">${labels[change.type] || change.type}</span>
      <div class="comparison-task"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(change.reason || "")}</small></div>
      <div class="comparison-time">${timeMarkup}</div>
    </div>
  `;
}

function planComparisonTime(item) {
  if (!item) {
    return "";
  }
  const start = parseDate(item.start_time);
  const end = parseDate(item.end_time);
  if (!start) {
    return "";
  }
  return `${start.getMonth() + 1}/${start.getDate()} ${formatTime(start)}${end ? `–${formatTime(end)}` : ""}`;
}

function closePlanComparison() {
  $("planComparison")?.classList.add("hidden");
  state.pendingReplan = null;
  $("planComparisonActions")?.classList.add("hidden");
}

function renderPlanAnalysis(analysis, title = "计划分析") {
  const container = $("planAnalysis");
  if (!container || !analysis) {
    return;
  }
  const hours = ((analysis.scheduled_minutes || 0) / 60).toFixed(1);
  const issues = analysis.issues || [];
  const modeLabel = analysis.selected_mode === "optimized" ? "深度优化" : "快速安排";
  const modeNote = analysis.selection_reason
    ? `<div class="analysis-engine"><strong>${modeLabel}</strong><span>${escapeHtml(analysis.selection_reason)}</span></div>`
    : "";
  const fallbackNote = analysis.fallback_reason
    ? `<p class="analysis-fallback">深度优化暂未启用：${escapeHtml(analysis.fallback_reason)}</p>`
    : "";
  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="score-ring"><strong>${escapeHtml(analysis.score ?? 0)}</strong><span>分</span></div>
    <div class="analysis-copy">
      <h3>${escapeHtml(title)}</h3>
      <p>已安排 ${escapeHtml(analysis.scheduled_count || 0)} 项，共 ${escapeHtml(hours)} 小时；未安排 ${escapeHtml(analysis.unscheduled_count || 0)} 项。</p>
      ${modeNote}
      ${fallbackNote}
      ${issues.length ? `<ul>${issues.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="analysis-good">当前计划没有明显风险。</p>`}
    </div>
  `;
}

function renderSchedule(result) {
  const container = $("scheduleResult");
  if (!container) {
    return;
  }

  const schedules =
    result && result.schedules
      ? result.schedules
      : state.schedules.filter((item) => ["proposed", "accepted"].includes(item.status));
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
  const proposed = schedule.status === "proposed";
  const actionable = Number.isInteger(schedule.id);
  const reason = schedule.reason
    ? `<div class="schedule-reason">${escapeHtml(schedule.reason)}</div>`
    : "";

  return `
    <div class="schedule ${accepted ? "schedule-accepted" : ""} ${schedule.is_locked ? "schedule-locked" : ""}">
      <div class="schedule-time">${escapeHtml(timeText)}</div>
      <div class="schedule-title"><span class="kind-badge flexible-kind">可调整</span>${escapeHtml(title)}${reason}</div>
      ${actionable ? `<div class="schedule-actions">
        ${proposed ? `<button class="ghost-button" onclick="acceptSchedule(${schedule.id})">确认</button>` : `<span class="schedule-status">${accepted ? "已确认" : escapeHtml(schedule.status)}</span>`}
        <button class="ghost-button" onclick="toggleScheduleLock(${schedule.id})">${schedule.is_locked ? "解锁" : "锁定"}</button>
        <button class="delete-button" onclick="deleteSchedule(${schedule.id})">
          移除
        </button>
      </div>` : ""}
    </div>
  `;
}

async function acceptAllSchedules() {
  const query = state.lastPlanBatchId
    ? `?batch_id=${encodeURIComponent(state.lastPlanBatchId)}`
    : "";
  try {
    const result = await api(`/schedules/accept-all${query}`, { method: "POST" });
    await refreshSchedules();
    renderSchedule(null);
    renderDashboardSchedules();
    alert(`已确认 ${result.accepted} 条任务安排`);
  } catch (error) {
    alert(`确认失败：${error.message}`);
  }
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
  const schedule = state.schedules.find((item) => item.id === Number(id));
  const siblings = schedule
    ? state.schedules.filter(
        (item) => item.task_id === schedule.task_id && ["accepted", "proposed"].includes(item.status),
      )
    : [];
  if (siblings.length > 1) {
    openCalendarItemDetails("schedule", id);
    return;
  }
  const task = schedule ? state.tasks.find((item) => item.id === schedule.task_id) : null;
  if (!confirm(`确定移除任务安排「${task?.title || `#${id}`}」吗？`)) {
    return;
  }
  try {
    await api(`/schedules/${id}`, { method: "DELETE" });
    await refreshSchedules();
    renderSchedule(null);
  } catch (error) {
    alert(`操作失败：${error.message}`);
  }
}

async function clearSchedules() {
  if (!confirm("确定清空所有待确认的任务安排吗？")) {
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

async function toggleScheduleLock(id) {
  const schedule = state.schedules.find((item) => item.id === id);
  if (!schedule) {
    return;
  }
  try {
    await api(`/schedules/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ is_locked: !schedule.is_locked }),
    });
    await loadAllData();
    renderSchedule(null);
    loadPlanVersions().catch(handleError);
  } catch (error) {
    alert(`操作失败：${error.message}`);
  }
}

async function previewLocalReplan() {
  const { start, end } = planningRange();
  const payload = {
    start: start.toISOString(),
    end: end.toISOString(),
    include_unscheduled: true,
    planning_mode: $("planMode")?.value || "auto",
  };
  const gapValue = $("planGap").value;
  const strategy = $("planStrategy").value;
  if (gapValue !== "") {
    payload.gap_minutes = Number(gapValue);
  }
  if (strategy) {
    payload.strategy = strategy;
  }
  try {
    const preview = await api("/schedules/replan-local/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.pendingReplan = payload;
    renderPlanAnalysis(preview.analysis, "局部重排预览");
    renderPlanComparison(preview.comparison, "局部重排前后对比", true);
  } catch (error) {
    alert(`局部重排预览失败：${error.message}`);
  }
}

async function applyPendingReplan() {
  if (!state.pendingReplan) {
    return;
  }
  const payload = state.pendingReplan;
  try {
    await api("/schedules/replan-local", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await loadAllData();
    renderSchedule(null);
    await loadPlanVersions();
    closePlanComparison();
  } catch (error) {
    alert(`局部重排失败：${error.message}`);
  }
}

async function undoLatestPlan() {
  if (!confirm("确定撤销最近一次日程修改吗？")) {
    return;
  }
  try {
    await api("/schedules/versions/undo-latest", { method: "POST" });
    await loadAllData();
    renderSchedule(null);
    await loadPlanVersions();
  } catch (error) {
    alert(`撤销失败：${error.message}`);
  }
}

async function loadPlanVersions() {
  const container = $("planVersionList");
  if (!container) {
    return;
  }
  state.planVersions = await api("/schedules/versions?limit=12");
  if (!state.planVersions.length) {
    container.innerHTML = '<div class="empty">还没有计划版本</div>';
    return;
  }
  container.innerHTML = state.planVersions.map((version) => {
    const createdAt = parseDate(version.created_at);
    const added = version.changes.filter((item) => item.type === "added").length;
    const removed = version.changes.filter((item) => item.type === "removed").length;
    const updated = version.changes.filter((item) => item.type === "updated").length;
    return `
      <div class="plan-version-row ${version.status === "undone" ? "is-undone" : ""}">
        <div>
          <strong>${escapeHtml(version.summary)}</strong>
          <span>${createdAt ? escapeHtml(formatDateTime(createdAt.toISOString())) : ""}</span>
        </div>
        <span>新增 ${added} · 移除 ${removed} · 修改 ${updated}${version.status === "undone" ? " · 已撤销" : ""}</span>
      </div>
    `;
  }).join("");
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
  const weekText =
    `${weekStart.getFullYear()}年 ${formatDate(weekStart)} - ` +
    `${formatDate(addDays(weekStart, 6))}`;
  const calendarStart = state.calendarStart || startOfDay(new Date());
  const calendarText =
    `${calendarStart.getFullYear()}年 ${formatDate(calendarStart)} - ` +
    `${formatDate(addDays(calendarStart, 6))}`;
  const planningStart = planningRangeStart();
  const planningText =
    `${planningStart.getFullYear()}年 ${formatDate(planningStart)} - ` +
    `${formatDate(addDays(planningStart, 6))}`;

  $("planning")?.querySelectorAll(".week-label").forEach((label) => {
    label.textContent = state.planningScope === "week" ? weekText : planningText;
  });
  $("calendar")?.querySelectorAll(".week-label").forEach((label) => {
    label.textContent = calendarText;
  });
}

function planningRangeStart() {
  if (state.planningScope === "upcoming") {
    return startOfDay(new Date());
  }
  if (state.planningScope === "week") {
    return state.weekStart || startOfWeek(new Date());
  }
  const raw = $("planStart")?.value;
  return raw ? startOfDay(new Date(raw)) : startOfDay(new Date());
}

function renderTimetable(host) {
  const rangeStart = host.id === "calendarGrid"
    ? state.calendarStart || startOfDay(new Date())
    : planningRangeStart();
  const today = startOfDay(new Date());

  const days = [];
  for (let index = 0; index < 7; index += 1) {
    days.push(addDays(rangeStart, index));
  }

  const hours = [];
  for (let hour = TIMETABLE_START_HOUR; hour < TIMETABLE_END_HOUR; hour += 1) {
    hours.push(hour);
  }

  const timeColumn = hours
    .map((hour) => `<div class="tt-hour-label">${String(hour).padStart(2, "0")}:00</div>`)
    .join("");

  const head = days
    .map((day) => {
      const isToday = isSameDay(day, today);
      const weekday = WEEKDAYS[(day.getDay() + 6) % 7];
      return `
        <div class="tt-head-day ${isToday ? "tt-today" : ""}">
          <div>${weekday}</div>
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
        <div class="tt-col ${isSameDay(day, today) ? "tt-today" : ""}"
             ondragover="allowTimetableDrop(event)"
             ondrop="dropTimetableSchedule(event, '${localDateInputValue(day)}')">
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

  const push = (start, end, className, label, detailKind, detailId, options = {}) => {
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
      detailKind,
      detailId,
      ...options,
    });
  };

  state.events.forEach((event) => {
    const start = parseDate(event.start_time);
    const end = parseDate(event.end_time);
    if (start && end && end > day && start < dayEnd) {
      push(start, end, "tt-event", event.title, "event", event.id);
    }
  });

  const scheduledTaskIds = new Set(
    state.schedules
      .filter((schedule) => ["proposed", "accepted"].includes(schedule.status))
      .map((schedule) => schedule.task_id)
  );

  state.schedules
    .filter((schedule) => ["proposed", "accepted"].includes(schedule.status))
    .forEach((schedule) => {
    const start = parseDate(schedule.start_time);
    const end = parseDate(schedule.end_time);
    if (start && end && end > day && start < dayEnd) {
      const task = state.tasks.find((item) => item.id === schedule.task_id);
      const title = task ? task.title : `任务 #${schedule.task_id}`;
      const className =
        schedule.status === "accepted"
          ? "tt-schedule tt-accepted"
          : "tt-schedule";
      push(start, end, className, title, "schedule", schedule.id, {
        locked: Boolean(schedule.is_locked),
      });
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
    push(start, end, "tt-task", task.title, "task", task.id);
  });

  return items;
}

function renderTimetableItem(item) {
  const draggable = item.detailKind === "schedule" && !item.locked;
  return `
    <div
      class="tt-item ${item.className} ${item.locked ? "tt-locked" : ""}"
      style="top:${item.top}px;height:${item.height}px"
      title="${escapeHtml(item.label)}"
      role="button"
      tabindex="0"
      draggable="${draggable}"
      ${draggable ? `ondragstart="startTimetableDrag(event, ${item.detailId})" ondragend="finishTimetableDrag(event)"` : ""}
      onclick="handleCalendarItemClick('${item.detailKind}', ${item.detailId})"
      onkeydown="handleCalendarItemKey(event, '${item.detailKind}', ${item.detailId})">
      <div class="tt-item-title">${escapeHtml(item.label)}</div>
      <div class="tt-item-time">${escapeHtml(item.timeText)}</div>
      ${draggable ? `<span class="tt-resize-handle" draggable="false" title="调整时长" ondragstart="event.preventDefault()" onpointerdown="startScheduleResize(event, ${item.detailId})"></span>` : ""}
    </div>
  `;
}

function handleCalendarItemClick(kind, id) {
  if (Date.now() < state.suppressCalendarClickUntil) {
    return;
  }
  openCalendarItemDetails(kind, id);
}

function handleCalendarItemKey(event, kind, id) {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    openCalendarItemDetails(kind, id);
  }
}

function startTimetableDrag(event, scheduleId) {
  const schedule = state.schedules.find((item) => item.id === scheduleId);
  if (!schedule || schedule.is_locked) {
    event.preventDefault();
    return;
  }
  state.calendarDrag = { type: "move", scheduleId };
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", String(scheduleId));
  event.currentTarget.classList.add("is-dragging");
}

function finishTimetableDrag(event) {
  event.currentTarget.classList.remove("is-dragging");
  state.suppressCalendarClickUntil = Date.now() + 500;
}

function allowTimetableDrop(event) {
  if (state.calendarDrag?.type === "move") {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }
}

function dropTimetableSchedule(event, dayValue) {
  event.preventDefault();
  const scheduleId = Number(event.dataTransfer.getData("text/plain"));
  const schedule = state.schedules.find((item) => item.id === scheduleId);
  if (!schedule || schedule.is_locked) {
    return;
  }
  const rect = event.currentTarget.getBoundingClientRect();
  const durationMinutes = Math.max(
    15,
    Math.round((parseDate(schedule.end_time) - parseDate(schedule.start_time)) / 60000),
  );
  const visibleMinutes = (event.clientY - rect.top) / HOUR_HEIGHT * 60;
  const roundedMinutes = Math.round(visibleMinutes / 15) * 15;
  const minuteOfDay = Math.max(
    TIMETABLE_START_HOUR * 60,
    Math.min(TIMETABLE_END_HOUR * 60 - durationMinutes, TIMETABLE_START_HOUR * 60 + roundedMinutes),
  );
  const nextStart = new Date(`${dayValue}T00:00:00`);
  nextStart.setMinutes(minuteOfDay);
  const nextEnd = new Date(nextStart.getTime() + durationMinutes * 60000);
  state.calendarDrag = null;
  state.suppressCalendarClickUntil = Date.now() + 500;
  moveScheduleFromCalendar(scheduleId, nextStart, nextEnd);
}

function startScheduleResize(event, scheduleId) {
  event.preventDefault();
  event.stopPropagation();
  const schedule = state.schedules.find((item) => item.id === scheduleId);
  if (!schedule || schedule.is_locked) {
    return;
  }
  const item = event.currentTarget.closest(".tt-item");
  state.calendarDrag = {
    type: "resize",
    scheduleId,
    startY: event.clientY,
    originalHeight: item.getBoundingClientRect().height,
    element: item,
  };
  state.suppressCalendarClickUntil = Date.now() + 800;
}

function handleScheduleResizeMove(event) {
  const drag = state.calendarDrag;
  if (!drag || drag.type !== "resize") {
    return;
  }
  const delta = event.clientY - drag.startY;
  const nextHeight = Math.max(20, drag.originalHeight + delta);
  drag.element.style.height = `${nextHeight}px`;
}

function finishScheduleResize(event) {
  const drag = state.calendarDrag;
  if (!drag || drag.type !== "resize") {
    return;
  }
  const schedule = state.schedules.find((item) => item.id === drag.scheduleId);
  const deltaMinutes = Math.round(
    ((event.clientY - drag.startY) / HOUR_HEIGHT * 60) / 15,
  ) * 15;
  state.calendarDrag = null;
  state.suppressCalendarClickUntil = Date.now() + 500;
  if (!schedule || deltaMinutes === 0) {
    renderTimetables();
    return;
  }
  const start = parseDate(schedule.start_time);
  const originalEnd = parseDate(schedule.end_time);
  const end = new Date(Math.max(start.getTime() + 15 * 60000, originalEnd.getTime() + deltaMinutes * 60000));
  moveScheduleFromCalendar(schedule.id, start, end);
}

async function moveScheduleFromCalendar(scheduleId, start, end) {
  const payload = {
    start_time: start.toISOString(),
    end_time: end.toISOString(),
  };
  try {
    const preview = await api(`/schedules/${scheduleId}/move-preview`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (!preview.valid) {
      alert(`无法调整：${preview.conflicts.join("；")}`);
      renderTimetables();
      return;
    }
    if (preview.warnings.length && !confirm(`${preview.warnings.join("；")}。仍要保存吗？`)) {
      renderTimetables();
      return;
    }
    await api(`/schedules/${scheduleId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    await loadAllData();
    if (!$('planning')?.classList.contains('hidden')) {
      renderSchedule(null);
      loadPlanVersions().catch(handleError);
    }
  } catch (error) {
    alert(`调整失败：${error.message}`);
    renderTimetables();
  }
}

function resolveRecordLocation(record, prefix = "location") {
  if (!record) {
    return null;
  }
  const saved = locationById(record[`${prefix}_id`]);
  if (saved) {
    return {
      label: saved.label,
      address: saved.address || saved.city || null,
      coordinate: {
        longitude: saved.longitude,
        latitude: saved.latitude,
      },
    };
  }
  const label = record[prefix] || null;
  const city = record[`${prefix}_city`] || null;
  const longitude = record[`${prefix}_longitude`];
  const latitude = record[`${prefix}_latitude`];
  const hasCoordinate = longitude !== null && longitude !== undefined && longitude !== "" &&
    latitude !== null && latitude !== undefined && latitude !== "";
  if (!label && !city && !hasCoordinate) {
    return null;
  }
  return {
    label: label || city || "地点",
    address: city,
    coordinate: hasCoordinate
      ? { longitude: Number(longitude), latitude: Number(latitude) }
      : null,
  };
}

function calendarDateText(date) {
  if (!date) {
    return "未设置";
  }
  const weekday = WEEKDAYS[(date.getDay() + 6) % 7];
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${weekday}`;
}

function calendarTimeRange(start, end) {
  if (!start || !end) {
    return "未设置";
  }
  const endText = isSameDay(start, end)
    ? formatTime(end)
    : `${end.getMonth() + 1}/${end.getDate()} ${formatTime(end)}`;
  return `${formatTime(start)}–${endText}`;
}

function calendarDurationText(start, end) {
  if (!start || !end) {
    return "未设置";
  }
  const minutes = Math.max(0, Math.round((end - start) / 60000));
  if (minutes < 60) {
    return `${minutes} 分钟`;
  }
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours} 小时 ${remainder} 分钟` : `${hours} 小时`;
}

function buildCalendarItemDetail(kind, id) {
  if (kind === "event") {
    const event = state.events.find((item) => item.id === Number(id));
    if (!event) {
      return null;
    }
    const start = parseDate(event.start_time);
    const end = parseDate(event.end_time);
    const destination = resolveRecordLocation(event);
    const origin = resolveRecordLocation(event, "origin_location");
    const isTravel = event.event_type === "travel";
    const groupEvents = event.trip_id
      ? state.events
          .filter((item) => item.trip_id === event.trip_id)
          .sort((first, second) => (first.trip_order || 0) - (second.trip_order || 0))
      : [];
    const locationText = isTravel
      ? `${origin?.label || "继承上一地点"} → ${destination?.label || "未指定目的地"}`
      : destination?.label || "";
    return {
      title: event.title,
      badge: isTravel ? "赶路" : "固定日程",
      badgeClass: "fixed",
      description: event.description,
      details: [
        ["日期", calendarDateText(start)],
        ["时间", calendarTimeRange(start, end)],
        ["时长", calendarDurationText(start, end)],
        ...(isTravel ? [["交通方式", travelModeName(event.travel_mode)]] : []),
        ...(event.trip_id ? [
          ["日程组", event.trip_title || "未命名日程组"],
          ["组内位置", `第 ${event.trip_order || groupEvents.indexOf(event) + 1} 段，共 ${groupEvents.length} 段`],
        ] : []),
      ],
      locationText,
      locations: isTravel ? [origin, destination].filter(Boolean) : [destination].filter(Boolean),
      edit: () => openEventModal(event.id),
      deleteOne: () => deleteCalendarEvent(event, false),
      deleteGroup: groupEvents.length > 1 ? () => deleteCalendarEvent(event, true) : null,
      deleteOneLabel: "删除此段",
      deleteGroupLabel: `删除整组（${groupEvents.length} 段）`,
    };
  }

  if (kind === "schedule") {
    const schedule = state.schedules.find((item) => item.id === Number(id));
    if (!schedule) {
      return null;
    }
    const task = state.tasks.find((item) => item.id === schedule.task_id);
    const start = parseDate(schedule.start_time);
    const end = parseDate(schedule.end_time);
    const location = resolveRecordLocation(task);
    const taskSchedules = state.schedules
      .filter(
        (item) => item.task_id === schedule.task_id && ["accepted", "proposed"].includes(item.status),
      )
      .sort((first, second) => parseDate(first.start_time) - parseDate(second.start_time));
    return {
      title: task?.title || `任务 #${schedule.task_id}`,
      badge: schedule.status === "accepted" ? "已确认安排" : "待确认安排",
      badgeClass: schedule.status === "accepted" ? "accepted" : "proposed",
      description: task?.description || schedule.reason,
      details: [
        ["日期", calendarDateText(start)],
        ["时间", calendarTimeRange(start, end)],
        ["时长", calendarDurationText(start, end)],
        ["优先级", task ? getPriorityName(task.priority) : "未设置"],
        ["锁定", schedule.is_locked ? "已锁定" : "未锁定"],
        ["分段", taskSchedules.length > 1
          ? `第 ${taskSchedules.indexOf(schedule) + 1} 段，共 ${taskSchedules.length} 段`
          : "单段"],
      ],
      locationText: location?.label || "",
      locations: [location].filter(Boolean),
      edit: task ? () => openTaskModal(task.id) : null,
      deleteOne: () => deleteCalendarSchedule(schedule, false),
      deleteGroup: taskSchedules.length > 1
        ? () => deleteCalendarSchedule(schedule, true)
        : null,
      deleteOneLabel: "移除此时间块",
      deleteGroupLabel: `移除全部安排（${taskSchedules.length} 段）`,
    };
  }

  const task = state.tasks.find((item) => item.id === Number(id));
  if (!task) {
    return null;
  }
  const start = parseDate(task.start_time);
  const end = start
    ? new Date(start.getTime() + (task.estimated_duration || 60) * 60000)
    : null;
  const location = resolveRecordLocation(task);
  return {
    title: task.title,
    badge: "待安排任务",
    badgeClass: "task",
    description: task.description,
    details: [
      ["日期", calendarDateText(start)],
      ["时间", calendarTimeRange(start, end)],
      ["时长", calendarDurationText(start, end)],
      ["优先级", getPriorityName(task.priority)],
      ["状态", getStatusName(task.status)],
    ],
    locationText: location?.label || "",
    locations: [location].filter(Boolean),
    edit: () => openTaskModal(task.id),
  };
}

function openCalendarItemDetails(kind, id) {
  const detail = buildCalendarItemDetail(kind, id);
  if (!detail) {
    return;
  }
  $("calendarDetailTitle").textContent = detail.title;
  const badge = $("calendarDetailBadge");
  badge.textContent = detail.badge;
  badge.className = `calendar-detail-badge ${detail.badgeClass}`;
  $("calendarDetailMeta").innerHTML = detail.details
    .map(([label, value]) => `
      <div class="calendar-detail-meta-item">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
      </div>
    `)
    .join("");

  const descriptionSection = $("calendarDetailDescriptionSection");
  descriptionSection.classList.toggle("hidden", !detail.description);
  $("calendarDetailDescription").textContent = detail.description || "";

  const locationSection = $("calendarDetailLocationSection");
  locationSection.classList.toggle("hidden", !detail.locations.length);
  $("calendarDetailLocationText").textContent = detail.locationText || "";

  state.calendarDetailEdit = detail.edit;
  state.calendarDetailDelete = {
    one: detail.deleteOne || null,
    group: detail.deleteGroup || null,
  };
  $("calendarDetailEditButton").classList.toggle("hidden", !detail.edit);
  const deleteOneButton = $("calendarDetailDeleteOneButton");
  const deleteGroupButton = $("calendarDetailDeleteGroupButton");
  deleteOneButton.classList.toggle("hidden", !detail.deleteOne);
  deleteGroupButton.classList.toggle("hidden", !detail.deleteGroup);
  deleteOneButton.textContent = detail.deleteOneLabel || "删除此项";
  deleteGroupButton.textContent = detail.deleteGroupLabel || "删除整组";
  $("calendarDetailModal").classList.remove("hidden");
  if (detail.locations.length) {
    showCalendarDetailMapFallback("地图加载中...");
    renderCalendarDetailMap(detail.locations).catch((error) => {
      showCalendarDetailMapFallback(error.message || "地图暂时无法显示");
    });
  }
  $("calendarDetailModal").querySelector(".modal-close-button")?.focus();
}

function closeCalendarItemDetails() {
  $("calendarDetailModal").classList.add("hidden");
  state.calendarDetailEdit = null;
  state.calendarDetailDelete = null;
}

function handleCalendarDetailBackdrop(event) {
  if (event.target === $("calendarDetailModal")) {
    closeCalendarItemDetails();
  }
}

function editCalendarDetailItem() {
  const edit = state.calendarDetailEdit;
  closeCalendarItemDetails();
  edit?.();
}

async function runCalendarDetailDelete(scope) {
  const action = state.calendarDetailDelete?.[scope];
  if (action) {
    await action();
  }
}

async function deleteCalendarEvent(event, wholeGroup) {
  const group = event.trip_id
    ? state.events
        .filter((item) => item.trip_id === event.trip_id)
        .sort((first, second) => (first.trip_order || 0) - (second.trip_order || 0))
    : [event];
  const message = wholeGroup
    ? `确定删除日程组「${event.trip_title || "未命名日程组"}」的全部 ${group.length} 段吗？\n\n${group.map((item, index) => `${index + 1}. ${item.title}`).join("\n")}`
    : `确定只删除「${event.title}」吗？${event.trip_id ? "\n删除单段可能使连续路线不完整。" : ""}`;
  if (!confirm(message)) {
    return;
  }
  try {
    await api(
      wholeGroup ? `/events/trips/${encodeURIComponent(event.trip_id)}` : `/events/${event.id}`,
      { method: "DELETE" },
    );
    closeCalendarItemDetails();
    await loadAllData();
  } catch (error) {
    alert(`删除失败：${error.message}`);
  }
}

async function deleteCalendarSchedule(schedule, wholeTask) {
  const task = state.tasks.find((item) => item.id === schedule.task_id);
  const schedules = state.schedules
    .filter(
      (item) => item.task_id === schedule.task_id && ["accepted", "proposed"].includes(item.status),
    )
    .sort((first, second) => parseDate(first.start_time) - parseDate(second.start_time));
  const message = wholeTask
    ? `确定移除任务「${task?.title || `#${schedule.task_id}`}」的全部 ${schedules.length} 个时间块吗？\n任务本身会保留。\n\n${schedules.map((item, index) => `${index + 1}. ${formatDateTime(item.start_time)}`).join("\n")}`
    : `确定只移除「${task?.title || `#${schedule.task_id}`}」的这个时间块吗？`;
  if (!confirm(message)) {
    return;
  }
  try {
    await api(
      wholeTask ? `/schedules/task/${schedule.task_id}` : `/schedules/${schedule.id}`,
      { method: "DELETE" },
    );
    closeCalendarItemDetails();
    await loadAllData();
    renderSchedule(null);
    loadPlanVersions().catch(handleError);
  } catch (error) {
    alert(`移除失败：${error.message}`);
  }
}

function showCalendarDetailMapFallback(message) {
  $("calendarDetailMap").classList.add("hidden");
  const fallback = $("calendarDetailMapFallback");
  fallback.textContent = message;
  fallback.classList.remove("hidden");
}

async function renderCalendarDetailMap(locations) {
  const preciseLocations = locations.filter((location) => location.coordinate);
  if (!preciseLocations.length) {
    showCalendarDetailMapFallback("该地点还没有精确坐标");
    return;
  }
  const AMap = await loadMapClient();
  if ($("calendarDetailModal").classList.contains("hidden")) {
    return;
  }
  const mapElement = $("calendarDetailMap");
  mapElement.classList.remove("hidden");
  $("calendarDetailMapFallback").classList.add("hidden");

  if (!state.calendarDetailMap) {
    state.calendarDetailMap = new AMap.Map("calendarDetailMap", {
      zoom: 15,
      viewMode: "2D",
      mapStyle: "amap://styles/whitesmoke",
    });
  } else {
    state.calendarDetailMap.clearMap();
    state.calendarDetailMap.resize();
  }

  const uniqueLocations = [...new Map(
    preciseLocations.map((location) => [
      `${Number(location.coordinate.longitude).toFixed(6)},${Number(location.coordinate.latitude).toFixed(6)}`,
      location,
    ]),
  ).values()];
  const hasMultipleLocations = uniqueLocations.length > 1;
  const markers = uniqueLocations.map((location, index) => {
    const markerRole = hasMultipleLocations
      ? (index === 0 ? "起" : index === uniqueLocations.length - 1 ? "终" : index + 1)
      : "";
    const markerClass = hasMultipleLocations && index === 0 ? "is-origin" : "is-primary";
    return new AMap.Marker({
      position: [location.coordinate.longitude, location.coordinate.latitude],
      title: location.label,
      anchor: "bottom-center",
      zIndex: 120 + index,
      content: `
        <div class="calendar-map-marker ${markerClass}">
          <div class="calendar-map-marker-label">
            ${markerRole ? `<span>${escapeHtml(markerRole)}</span>` : ""}
            <strong>${escapeHtml(location.label || "日程地点")}</strong>
          </div>
          <i aria-hidden="true"></i>
        </div>
      `,
    });
  });
  state.calendarDetailMap.add(markers);
  if (markers.length === 1) {
    state.calendarDetailMap.setZoomAndCenter(15, markers[0].getPosition());
  } else {
    state.calendarDetailMap.setFitView(markers, false, [44, 44, 44, 44]);
  }
}

function previousWeek() {
  state.weekStart = addDays(state.weekStart || startOfWeek(new Date()), -7);
  if (!$("planning")?.classList.contains("hidden") && state.planningScope === "week") {
    syncPlanningWeekRange();
    renderPlanningOverview();
  }
  renderTimetables();
}

function nextWeek() {
  state.weekStart = addDays(state.weekStart || startOfWeek(new Date()), 7);
  if (!$("planning")?.classList.contains("hidden") && state.planningScope === "week") {
    syncPlanningWeekRange();
    renderPlanningOverview();
  }
  renderTimetables();
}

function goToday() {
  state.weekStart = startOfWeek(new Date());
  if (!$("planning")?.classList.contains("hidden") && state.planningScope === "week") {
    syncPlanningWeekRange();
    renderPlanningOverview();
  }
  renderTimetables();
}

function setCalendarRangeMode(mode) {
  state.calendarRangeMode = mode === "week" ? "week" : "upcoming";
  state.calendarStart = state.calendarRangeMode === "week"
    ? startOfWeek(new Date())
    : startOfDay(new Date());
  $("calendarModeUpcoming")?.classList.toggle("active", state.calendarRangeMode === "upcoming");
  $("calendarModeWeek")?.classList.toggle("active", state.calendarRangeMode === "week");
  renderTimetables();
}

function previousCalendarRange() {
  const fallback = state.calendarRangeMode === "week"
    ? startOfWeek(new Date())
    : startOfDay(new Date());
  state.calendarStart = addDays(state.calendarStart || fallback, -7);
  renderTimetables();
}

function nextCalendarRange() {
  const fallback = state.calendarRangeMode === "week"
    ? startOfWeek(new Date())
    : startOfDay(new Date());
  state.calendarStart = addDays(state.calendarStart || fallback, 7);
  renderTimetables();
}

function goCalendarToday() {
  state.calendarStart = state.calendarRangeMode === "week"
    ? startOfWeek(new Date())
    : startOfDay(new Date());
  renderTimetables();
}


/* =========================
   规划偏好
========================= */

async function loadPreferences() {
  const preferences = await api("/preferences");
  state.preferences = preferences;
  $("prefTimezone").value = preferences.timezone;
  $("prefLocation").value = preferences.default_location;
  $("prefWorkStart").value = preferences.workday_start;
  $("prefWorkEnd").value = preferences.workday_end;
  $("prefLunchStart").value = preferences.lunch_start || "";
  $("prefLunchEnd").value = preferences.lunch_end || "";
  $("prefDuration").value = preferences.default_task_duration_min;
  $("prefGap").value = preferences.default_gap_minutes;
  $("prefStrategy").value = preferences.planning_strategy;
  $("prefWeekends").checked = preferences.schedule_weekends;

  if ($("planGap") && $("planGap").value === "0") {
    $("planGap").value = preferences.default_gap_minutes;
  }
}

async function savePreferences() {
  const feedback = $("settingsFeedback");
  const payload = {
    timezone: $("prefTimezone").value.trim(),
    default_location: $("prefLocation").value.trim(),
    workday_start: $("prefWorkStart").value,
    workday_end: $("prefWorkEnd").value,
    lunch_start: $("prefLunchStart").value || null,
    lunch_end: $("prefLunchEnd").value || null,
    default_task_duration_min: Number($("prefDuration").value),
    default_gap_minutes: Number($("prefGap").value),
    planning_strategy: $("prefStrategy").value,
    schedule_weekends: $("prefWeekends").checked,
  };
  try {
    state.preferences = await api("/preferences", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    $("planGap").value = state.preferences.default_gap_minutes;
    feedback.textContent = "设置已保存，下一次规划会自动使用。";
    feedback.className = "feedback success";
  } catch (error) {
    feedback.textContent = `保存失败：${error.message}`;
    feedback.className = "feedback error";
  }
}


/* =========================
   常用地点
========================= */

async function loadLocations() {
  state.locations = await api("/locations");
  renderLocations();
  populateLocationOptions();
  renderTasks();
  renderEvents();
}

function locationById(locationId) {
  return state.locations.find((item) => item.id === Number(locationId)) || null;
}

function modalLocationIds(kind) {
  const prefix = kind === "task"
    ? "task"
    : kind === "eventOrigin"
      ? "eventOrigin"
      : "event";
  return {
    saved: `${prefix}LocationId`,
    name: `${prefix}Location`,
    city: `${prefix}LocationCity`,
    longitude: `${prefix}LocationLongitude`,
    latitude: `${prefix}LocationLatitude`,
    search: `${prefix}LocationSearch`,
    candidates: `${prefix}LocationCandidates`,
  };
}

function setModalLocationFields(kind, value = {}) {
  const ids = modalLocationIds(kind);
  const fieldPrefix = kind === "eventOrigin" ? "origin_location" : "location";
  $(ids.saved).value = value[`${fieldPrefix}_id`] || "";
  $(ids.name).value = value[fieldPrefix] || "";
  $(ids.city).value = value[`${fieldPrefix}_city`] || "";
  $(ids.longitude).value = value[`${fieldPrefix}_longitude`] ?? "";
  $(ids.latitude).value = value[`${fieldPrefix}_latitude`] ?? "";
  $(ids.search).value = "";
  $(ids.candidates).innerHTML = "";
  $(ids.candidates).classList.add("hidden");
  state.modalLocationCandidates[kind] = [];
}

function clearModalLocation(kind) {
  const ids = modalLocationIds(kind);
  $(ids.saved).value = "";
  setModalLocationFields(kind);
}

function selectSavedLocationForForm(kind) {
  const ids = modalLocationIds(kind);
  const location = locationById($(ids.saved).value);
  if (!location) {
    return;
  }
  $(ids.name).value = location.label || location.address || "";
  $(ids.city).value = location.city || "";
  $(ids.longitude).value = location.longitude;
  $(ids.latitude).value = location.latitude;
}

function markCustomLocation(kind) {
  $(modalLocationIds(kind).saved).value = "";
}

function handleLocationSearchKey(event, kind) {
  if (event.key === "Enter") {
    event.preventDefault();
    searchModalLocation(kind);
  }
}

async function searchModalLocation(kind) {
  const ids = modalLocationIds(kind);
  const query = $(ids.search).value.trim() || $(ids.name).value.trim();
  const city = $(ids.city).value.trim() || state.preferences?.default_location || "";
  if (!query) {
    alert("请输入要搜索的地点");
    return;
  }

  const container = $(ids.candidates);
  container.classList.remove("hidden");
  container.innerHTML = '<div class="location-candidate-status">正在搜索...</div>';
  try {
    const result = await api(
      `/maps/places?q=${encodeURIComponent(query)}&city=${encodeURIComponent(city)}&limit=6`,
    );
    state.modalLocationCandidates[kind] = result.places || [];
    renderModalLocationCandidates(kind);
  } catch (error) {
    state.modalLocationCandidates[kind] = [];
    container.innerHTML = `<div class="location-candidate-status error-text">${escapeHtml(error.message)}</div>`;
  }
}

function renderModalLocationCandidates(kind) {
  const ids = modalLocationIds(kind);
  const places = state.modalLocationCandidates[kind] || [];
  const container = $(ids.candidates);
  if (!places.length) {
    container.innerHTML = '<div class="location-candidate-status">没有找到匹配地点</div>';
    return;
  }
  container.innerHTML = places
    .map((place, index) => {
      const detail = [place.address, place.district, place.city].filter(Boolean).join(" · ");
      return `
        <button class="location-candidate" type="button" onclick="selectModalLocationCandidate('${kind}', ${index})">
          <strong>${escapeHtml(place.name)}</strong>
          <span>${escapeHtml(detail || "精确地点")}</span>
        </button>
      `;
    })
    .join("");
}

function selectModalLocationCandidate(kind, index) {
  const place = state.modalLocationCandidates[kind]?.[index];
  if (!place) {
    return;
  }
  const ids = modalLocationIds(kind);
  $(ids.saved).value = "";
  $(ids.name).value = place.name || place.address || "";
  $(ids.city).value = place.city || "";
  $(ids.longitude).value = place.coordinate?.longitude ?? "";
  $(ids.latitude).value = place.coordinate?.latitude ?? "";
  $(ids.search).value = place.name || "";
  $(ids.candidates).classList.add("hidden");
}

function modalLocationPayload(kind) {
  const ids = modalLocationIds(kind);
  const locationId = Number($(ids.saved).value) || null;
  const name = $(ids.name).value.trim() || null;
  const city = $(ids.city).value.trim() || null;
  const longitudeText = $(ids.longitude).value.trim();
  const latitudeText = $(ids.latitude).value.trim();
  if (Boolean(longitudeText) !== Boolean(latitudeText)) {
    alert("地点经度和纬度需要同时填写");
    return null;
  }
  const prefix = kind === "eventOrigin" ? "origin_location" : "location";
  return {
    [`${prefix}_id`]: locationId,
    [prefix]: locationId ? null : name,
    [`${prefix}_city`]: locationId ? null : city,
    [`${prefix}_longitude`]: locationId || !longitudeText ? null : Number(longitudeText),
    [`${prefix}_latitude`]: locationId || !latitudeText ? null : Number(latitudeText),
  };
}

function emptyModalLocationPayload(prefix) {
  return {
    [`${prefix}_id`]: null,
    [prefix]: null,
    [`${prefix}_city`]: null,
    [`${prefix}_longitude`]: null,
    [`${prefix}_latitude`]: null,
  };
}

function populateLocationOptions() {
  for (const selectId of ["taskLocationId", "eventLocationId", "eventOriginLocationId"]) {
    const select = $(selectId);
    if (!select) {
      continue;
    }
    const current = select.value;
    const emptyLabel = selectId === "taskLocationId"
      ? "不指定地点"
      : selectId === "eventOriginLocationId"
        ? "自动继承上一地点"
        : "不绑定精确地点";
    select.innerHTML = [
      `<option value="">${emptyLabel}</option>`,
      ...state.locations.map(
        (location) =>
          `<option value="${location.id}">${escapeHtml(location.label)}${location.is_default ? "（默认）" : ""}</option>`,
      ),
    ].join("");
    select.value = current;
  }

  const originSelect = $("itineraryOrigin");
  if (originSelect) {
    const current = originSelect.value || "auto";
    originSelect.innerHTML = [
      '<option value="auto">按当天行程判断</option>',
      ...state.locations.map(
        (location) => `<option value="location:${location.id}">${escapeHtml(location.label)}${location.is_default ? "（默认）" : ""}</option>`,
      ),
    ].join("");
    originSelect.value = Array.from(originSelect.options).some((option) => option.value === current)
      ? current
      : "auto";
  }

  const destinationSelect = $("itineraryDestination");
  if (destinationSelect) {
    const current = destinationSelect.value || "last";
    destinationSelect.innerHTML = [
      '<option value="last">停在最后一站</option>',
      '<option value="origin">返回出发点</option>',
      ...state.locations.map(
        (location) => `<option value="location:${location.id}">${escapeHtml(location.label)}${location.is_default ? "（默认）" : ""}</option>`,
      ),
    ].join("");
    destinationSelect.value = Array.from(destinationSelect.options).some((option) => option.value === current)
      ? current
      : "last";
  }
}

function renderLocations() {
  const container = $("locationList");
  if (!container) {
    return;
  }
  if (state.locations.length === 0) {
    container.innerHTML = '<div class="location-empty">暂无常用地点</div>';
    return;
  }

  container.innerHTML = state.locations
    .map((location) => {
      const address = location.address || location.city || "未填写地址";
      const coordinates = `${Number(location.longitude).toFixed(6)}, ${Number(location.latitude).toFixed(6)}`;
      return `
        <div class="location-row">
          <div class="location-copy">
            <div class="location-title">
              <strong>${escapeHtml(location.label)}</strong>
              ${location.is_default ? '<span class="location-default">默认</span>' : ""}
            </div>
            <div class="location-address">${escapeHtml(address)}</div>
            <div class="location-coordinates">${escapeHtml(coordinates)}</div>
          </div>
          <div class="location-actions">
            ${location.is_default ? "" : `<button class="ghost-button" type="button" onclick="setDefaultLocation(${location.id})">设为默认</button>`}
            <button class="ghost-button" type="button" onclick="editLocation(${location.id})">编辑</button>
            <button class="ghost-button danger-button" type="button" onclick="deleteLocation(${location.id})">删除</button>
          </div>
        </div>
      `;
    })
    .join("");
}

function editLocation(locationId) {
  const location = state.locations.find((item) => item.id === locationId);
  if (!location) {
    return;
  }
  state.editingLocationId = locationId;
  $("locationLabel").value = location.label;
  $("locationCity").value = location.city || "";
  $("locationAddress").value = location.address || "";
  $("locationLongitude").value = location.longitude;
  $("locationLatitude").value = location.latitude;
  $("locationDefault").checked = location.is_default;
  $("locationSaveButton").textContent = "保存地点";
  $("locationCancelButton").classList.remove("hidden");
  $("locationLabel").focus();
}

function resetLocationForm() {
  state.editingLocationId = null;
  $("locationLabel").value = "";
  $("locationCity").value = state.preferences?.default_location || "";
  $("locationAddress").value = "";
  $("locationLongitude").value = "";
  $("locationLatitude").value = "";
  $("locationDefault").checked = state.locations.length === 0;
  $("locationSaveButton").textContent = "添加地点";
  $("locationCancelButton").classList.add("hidden");
}

function showLocationFeedback(message, type = "success") {
  const feedback = $("locationFeedback");
  feedback.textContent = message;
  feedback.className = `feedback ${type}`;
}

async function saveLocation() {
  const longitude = Number($("locationLongitude").value);
  const latitude = Number($("locationLatitude").value);
  const payload = {
    label: $("locationLabel").value.trim(),
    city: $("locationCity").value.trim() || null,
    address: $("locationAddress").value.trim() || null,
    longitude,
    latitude,
    is_default: $("locationDefault").checked,
  };

  if (!payload.label) {
    showLocationFeedback("请填写地点名称。", "error");
    return;
  }
  if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) {
    showLocationFeedback("请填写有效的经纬度。", "error");
    return;
  }

  const editingId = state.editingLocationId;
  try {
    await api(editingId ? `/locations/${editingId}` : "/locations", {
      method: editingId ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    await loadLocations();
    resetLocationForm();
    showLocationFeedback(editingId ? "地点已更新。" : "地点已添加。");
  } catch (error) {
    showLocationFeedback(`保存失败：${error.message}`, "error");
  }
}

async function setDefaultLocation(locationId) {
  try {
    await api(`/locations/${locationId}`, {
      method: "PUT",
      body: JSON.stringify({ is_default: true }),
    });
    await loadLocations();
    showLocationFeedback("默认出发点已更新。");
  } catch (error) {
    showLocationFeedback(`设置失败：${error.message}`, "error");
  }
}

async function deleteLocation(locationId) {
  const location = state.locations.find((item) => item.id === locationId);
  if (!location || !confirm(`确定删除地点“${location.label}”吗？`)) {
    return;
  }
  try {
    await api(`/locations/${locationId}`, { method: "DELETE" });
    await loadLocations();
    if (state.editingLocationId === locationId) {
      resetLocationForm();
    }
    showLocationFeedback("地点已删除。");
  } catch (error) {
    showLocationFeedback(`删除失败：${error.message}`, "error");
  }
}

function useBrowserLocation() {
  if (!navigator.geolocation) {
    showLocationFeedback("当前浏览器不支持位置获取。", "error");
    return;
  }
  showLocationFeedback("正在获取当前位置...");
  navigator.geolocation.getCurrentPosition(
    (position) => {
      $("locationLongitude").value = position.coords.longitude.toFixed(6);
      $("locationLatitude").value = position.coords.latitude.toFixed(6);
      showLocationFeedback("已获取当前位置坐标。");
    },
    (error) => {
      const messages = {
        1: "位置权限未授予。",
        2: "暂时无法获取当前位置。",
        3: "获取当前位置超时。",
      };
      showLocationFeedback(messages[error.code] || "获取当前位置失败。", "error");
    },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
  );
}

async function resolveLocationAddress() {
  const address = $("locationAddress").value.trim();
  if (!address) {
    showLocationFeedback("请先填写详细地址。", "error");
    return;
  }
  try {
    const result = await api("/maps/geocode", {
      method: "POST",
      body: JSON.stringify({
        address,
        city: $("locationCity").value.trim() || null,
      }),
    });
    $("locationLongitude").value = result.place.coordinate.longitude;
    $("locationLatitude").value = result.place.coordinate.latitude;
    if (result.place.address) {
      $("locationAddress").value = result.place.address;
    }
    if (result.place.city) {
      $("locationCity").value = result.place.city;
    }
    showLocationFeedback("地址已解析为坐标。");
  } catch (error) {
    showLocationFeedback(`解析失败：${error.message}`, "error");
  }
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

function llmErrorLabel(kind) {
  const labels = {
    auth: "鉴权失败",
    rate_limit: "请求受限",
    transient: "网络异常",
  };
  return labels[kind] || "";
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

async function loadEvaluation() {
  const latest = await api("/evaluations/latest");
  state.evaluationsLoaded = true;
  state.evaluationRun = latest;
  renderEvaluation(latest);
}

async function runEvaluation() {
  const button = $("evaluationRunButton");
  if (button?.disabled) return;
  if (button) {
    button.disabled = true;
    button.textContent = "评测中";
  }
  try {
    const result = await api("/evaluations/run", { method: "POST" });
    state.evaluationRun = result;
    renderEvaluation(result);
  } catch (error) {
    alert(`评测失败：${error.message}`);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "运行评测";
    }
  }
}

function renderEvaluation(run) {
  const empty = $("evaluationEmpty");
  const results = $("evaluationResults");
  if (!run) {
    empty?.classList.remove("hidden");
    results?.classList.add("hidden");
    return;
  }
  empty?.classList.add("hidden");
  results?.classList.remove("hidden");
  $("evaluationScore").textContent = Number(run.score || 0).toFixed(1);
  const metrics = run.metrics || {};
  $("evaluationMetrics").innerHTML = [
    ["工具选择", `${metrics.tool_selection_accuracy ?? 0}%`],
    ["工具成功", `${metrics.tool_success_rate ?? 0}%`],
    ["约束通过", `${metrics.constraint_pass_rate ?? 0}%`],
    ["确认安全", `${metrics.confirmation_safety_rate ?? 0}%`],
    ["平均耗时", `${metrics.average_latency_ms ?? 0} ms`],
    ["模型 Tokens", `${metrics.total_model_tokens ?? 0}`],
    ["估算成本", `${Number(metrics.estimated_model_cost ?? 0).toFixed(6)}`],
  ].map(([label, value]) => `
    <div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>
  `).join("");
  $("evaluationScenarioRows").innerHTML = (run.results || []).map((item) => `
    <tr>
      <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml((item.actual_tools || []).join(" → "))}</small></td>
      <td><span class="evaluation-result ${item.passed ? "pass" : "fail"}">${item.passed ? "通过" : "失败"}</span></td>
      <td>${evaluationCheck(item.selection_passed)}</td>
      <td>${evaluationCheck(item.constraints_passed)}</td>
      <td>${evaluationCheck(item.confirmation_passed)}</td>
      <td>${escapeHtml(`${item.duration_ms} ms`)}</td>
    </tr>
  `).join("");
}

function evaluationCheck(value) {
  return `<span class="evaluation-check ${value ? "pass" : "fail"}">${value ? "通过" : "未通过"}</span>`;
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
  state.calendarStart = startOfDay(new Date());
  updateFutureInputMinimums();
  setPlanningScope("week");
  if ($("itineraryDate")) {
    $("itineraryDate").value = localDateInputValue();
  }
  Promise.all([loadAllData(), loadPreferences(), loadLocations()])
    .then(() => resetLocationForm())
    .catch(handleError);

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
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("calendarDetailModal").classList.contains("hidden")) {
      closeCalendarItemDetails();
    }
  });
  document.addEventListener("pointermove", handleScheduleResizeMove);
  document.addEventListener("pointerup", finishScheduleResize);
  window.setInterval(() => loadAgentStatus().catch(handleError), 5000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
