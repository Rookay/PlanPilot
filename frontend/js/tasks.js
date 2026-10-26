/* PlanPilot 前端 · tasks
   任务列表、筛选、任务弹窗与依赖
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

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
