/* PlanPilot 前端 · itinerary
   行程时间线、地图与通勤预留
   ---------------------------------------------------------
   由 app.js 拆分而来。按业务域归档，加载顺序见 index.html；
   所有函数仍是全局声明（页面有 107 处内联 onclick/onchange 依赖它们）。
   ========================================================= */

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
