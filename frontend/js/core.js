/* PlanPilot 前端 · core
   常量、日期与格式化工具、api 请求、页面导航
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

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
  // 待重发的请求：同一条内容重发时复用它的 requestId，服务端才不会重复执行。
  chatPendingRequest: null,
  // 正在进行的对话请求，用于"停止"和服务端超时前的主动取消。
  chatAbort: null,
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
  const { timeoutMs, ...rest } = options;
  const config = { ...rest };

  // 只有带请求体时才发送 Content-Type，避免 GET/DELETE 触发多余的 CORS 预检。
  if (config.body !== undefined && !config.headers) {
    config.headers = { "Content-Type": "application/json" };
  }

  // 客户端兜底超时。服务端有单次请求时长上限，但那个上限只约束"模型循环"，
  // 不覆盖排队、传输、网关这些环节；没有客户端超时的话，界面会无限转圈。
  let timer = null;
  let timedOut = false;
  if (timeoutMs || config.signal) {
    const controller = new AbortController();
    const outer = config.signal;
    if (outer) {
      if (outer.aborted) {
        controller.abort();
      } else {
        // 外部取消（用户点"停止"）也要能传到 fetch 上。
        outer.addEventListener("abort", () => controller.abort(), { once: true });
      }
    }
    config.signal = controller.signal;
    if (timeoutMs) {
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
    }
  }

  let response;
  try {
    response = await fetch(`${API_URL}${path}`, config);
  } catch (error) {
    if (timedOut) {
      const timeout = new Error(`请求超过 ${Math.round(timeoutMs / 1000)} 秒未返回`);
      timeout.status = 0;
      timeout.timeout = true;
      throw timeout;
    }
    // AbortError 由调用方区分（用户主动取消），这里原样抛出并补上标记。
    error.status = 0;
    error.aborted = error.name === "AbortError";
    throw error;
  } finally {
    if (timer) {
      clearTimeout(timer);
    }
  }

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    let payload = null;
    try {
      const data = await response.json();
      payload = data;
      if (data && data.detail) {
        detail =
          typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail);
      }
    } catch (error) {
      /* 忽略解析失败，使用默认错误信息 */
    }
    const failure = new Error(detail);
    // 带上状态码：调用方要区分 409（同请求在跑）和其它失败。
    failure.status = response.status;
    failure.body = payload;
    throw failure;
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
