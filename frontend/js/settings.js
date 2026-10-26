/* PlanPilot 前端 · settings
   偏好设置与常用地点
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

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
