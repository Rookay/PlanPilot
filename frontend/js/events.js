/* PlanPilot 前端 · events
   固定事件、重复规则与行程组
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

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
