/* PlanPilot 前端 · calendar
   周视图、拖拽缩放与日程详情
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

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
