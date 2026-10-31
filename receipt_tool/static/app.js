"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { batch: null, busy: false, polling: null, tab: "groups", groupId: null, toastTimer: null, previewCache: new Map(), uploadQueue: [], activeUploads: [], uploading: false, normalizing: 0, importChain: Promise.resolve(), uploadError: "", localId: 0 };
  let initialization;
  window.receiptWorkspaceReady = false;
  const kinds = { order: "订单截图", payment: "支付凭证", unknown: "待确认类型" };
  const storageKey = "receipt-studio:last-batch";
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const money = (value) => Number.isInteger(value) ? `¥${(value / 100).toFixed(2)}` : "金额待确认";
  const name = (record) => record?.name || record?.filename || "未命名截图";
  const records = () => state.batch?.records || [];
  const findRecord = (id) => records().find((record) => record.id === id);
  const processing = () => ["queued", "processing"].includes(state.batch?.status);
  const pendingCount = () => state.batch?.pending_count ?? records().filter((record) => record.ocr_status === "pending").length;
  const recognizedRecords = () => records().filter((record) => record.ocr_status !== "pending");
  const retryEligibleRecords = () => recognizedRecords().filter((record) => !record.manually_edited && (record.amount_cents == null || record.kind === "unknown" || record.ocr_error === true));
  const uploadsWaiting = () => state.uploading || state.normalizing > 0 || state.uploadQueue.length > 0;
  const imageURL = (record) => `/api/batches/${encodeURIComponent(state.batch.id)}/images/${encodeURIComponent(record.id)}`;
  const dateOnly = (date) => /^\d{4}-\d{2}-\d{2}/.test(date || "") ? date.slice(0, 10) : "";
  const groupTitle = (record) => [dateOnly(record?.date), record?.merchant].filter(Boolean).join(" · ") || name(record);

  function persist(id) {
    try { id ? localStorage.setItem(storageKey, id) : localStorage.removeItem(storageKey); } catch (_) { /* Storage may be disabled. */ }
  }

  async function responseError(response) {
    try {
      const body = await response.json();
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return body.detail.map((issue) => issue.msg || "输入信息有误").join("；");
    } catch (_) { /* Non-JSON response from an unavailable server. */ }
    return `操作未完成（${response.status}），请稍后重试。`;
  }

  async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw new Error("无法连接本机服务。请关闭并重新打开凭证归档后重试。"); }
    if (!response.ok) {
      const error = new Error(await responseError(response));
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  function jsonOptions(method, body) {
    return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }

  function notice(message, error = false) {
    $("notice").hidden = !message;
    $("notice").textContent = message || "";
    $("notice").classList.toggle("error", error);
  }

  function toast(message) {
    clearTimeout(state.toastTimer);
    $("toast").textContent = message;
    $("toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4000);
  }

  function setBusy(value) {
    state.busy = value;
    const blocked = value || processing() || uploadsWaiting();
    const uploadBlocked = value || processing();
    $("file-input").disabled = uploadBlocked;
    $("drop-zone").setAttribute("aria-disabled", String(uploadBlocked));
    ["clear-button", "export-format", "crop-toggle", "incomplete-toggle", "document-title"].forEach((id) => { $(id).disabled = blocked; });
    $("clear-button").disabled = value || processing() || state.uploading || state.normalizing > 0;
    ["rematch-button", "create-group-button", "export-button"].forEach((id) => { $(id).disabled = blocked || pendingCount() > 0; });
    $("retry-recognition-button").disabled = blocked || pendingCount() > 0 || retryEligibleRecords().length === 0;
    $("retry-recognition-button").title = `保留手动修改，仅重试未识别金额或类型、识别失败的截图（${retryEligibleRecords().length} 张）`;
    $("recognize-button").disabled = blocked || !state.batch || pendingCount() === 0;
    $("retry-upload-button").disabled = value || processing() || state.uploading;
    document.querySelectorAll(".record-form input, .record-form select, .record-form button, .group-controls button, #group-form input, #group-form select, #group-form button[type=submit]").forEach((element) => { element.disabled = blocked; });
    if (!state.batch) { $("export-button").disabled = true; if (!state.uploadQueue.length) $("clear-button").disabled = true; }
    if (!value && !processing() && state.uploadQueue.length && !state.uploading && !state.uploadError) queueMicrotask(drainUploads);
  }

  function updateBatch(batch, options = {}) {
    state.batch = batch;
    persist(batch.id);
    render(options);
  }

  function render(options = {}) {
    const batch = state.batch;
    renderCollection();
    if (!batch) {
      $("workspace").hidden = true;
      $("processing-panel").hidden = true;
      $("empty-guide").hidden = uploadsWaiting();
      $("step-upload").classList.add("active");
      $("step-review").classList.remove("active", "done");
      $("step-export").classList.remove("active");
      $("upload-title").textContent = "导入凭证";
      setBusy(state.busy);
      return;
    }
    const isProcessing = processing();
    $("processing-panel").hidden = !isProcessing;
    $("empty-guide").hidden = true;
    $("workspace").hidden = isProcessing || !recognizedRecords().length;
    $("upload-title").textContent = "继续添加凭证";
    $("step-upload").classList.toggle("active", pendingCount() > 0 && !isProcessing);
    $("step-upload").classList.toggle("done", pendingCount() === 0 || isProcessing);
    $("step-review").classList.toggle("active", isProcessing || recognizedRecords().length > 0);
    $("step-export").classList.remove("active");
    $("processing-title").textContent = batch.status === "queued" ? "截图已导入，准备识别…" : "正在识别与配对…";
    $("processing-counter").textContent = `${batch.progress || 0} / ${batch.total || 0}`;
    $("progress-bar").style.width = `${batch.total ? Math.round((batch.progress || 0) / batch.total * 100) : 0}%`;
    if (isProcessing) { setBusy(state.busy); return; }

    const groups = batch.groups || [];
    const review = groups.filter((group) => group.status !== "matched").length;
    const unmatched = (batch.unmatched_ids || []).filter((id) => findRecord(id)?.ocr_status !== "pending");
    $("summary-strip").innerHTML = [
      ["已导入截图", records().length, ""],
      ["已完成配对", groups.length - review, "good"],
      ["需要核对", review, review ? "warning" : ""],
      ["尚未配对", unmatched.length, unmatched.length ? "warning" : ""],
    ].map(([label, value, className]) => `<div class="summary-item ${className}"><span>${label}</span><strong>${value}</strong></div>`).join("");
    $("group-count").textContent = groups.length;
    $("record-count").textContent = records().length;
    const errors = [...(batch.errors || [])];
    if (batch.duplicates?.length) errors.push(`已跳过 ${batch.duplicates.length} 张内容完全相同的重复截图：${batch.duplicates.map((item) => item.name || item.filename || "未命名截图").join("、")}。`);
    $("batch-errors").hidden = !errors.length;
    $("batch-errors").textContent = errors.join("\n");
    renderGroups();
    if (!options.preserveRecords) renderRecords();
    switchTab(state.tab);
    updateExport();
    setBusy(state.busy);
  }

  function renderCollection() {
    const waiting = [...state.activeUploads, ...state.uploadQueue];
    const pending = pendingCount();
    const done = recognizedRecords().length;
    $("collection-panel").hidden = !state.batch && !waiting.length;
    $("collection-count").textContent = records().length + waiting.length;
    const duplicates = state.batch?.duplicates?.length || 0;
    $("collection-status").textContent = `${records().length} 张已保存${pending ? `，${pending} 张待识别` : ""}${done ? `，${done} 张已识别` : ""}${duplicates ? `；已自动去重 ${duplicates} 张，重复图片仅保留一份` : ""}${waiting.length ? `；${waiting.length} 张正在导入或排队` : ""}。`;
    $("collection-grid").innerHTML = records().map((record) => {
      const isPending = record.ocr_status === "pending";
      const label = isPending ? "已保存 · 待识别" : record.ocr_status === "error" ? "识别失败 · 可手动补全" : "已识别";
      return `<article class="collection-item ${isPending ? "pending" : ""}"><button type="button" class="collection-image" data-preview="${escape(record.id)}" aria-label="查看 ${escape(name(record))} 原图"><img src="${escape(imageURL(record))}" alt="${escape(name(record))}" loading="lazy"></button><p class="collection-item-name" title="${escape(name(record))}">${escape(name(record))}</p><span class="collection-item-status">${label}</span></article>`;
    }).join("") + waiting.map((item) => `<article class="collection-item waiting"><button type="button" class="collection-image" disabled><img src="${escape(item.url)}" alt="${escape(item.file.name || "粘贴截图")}"></button><p class="collection-item-name" title="${escape(item.file.name || "粘贴截图")}">${escape(item.file.name || "粘贴截图")}</p><span class="collection-item-status">${state.activeUploads.includes(item) ? "正在保存…" : state.uploadError ? "待重试导入" : "等待导入"}</span></article>`).join("");
    $("upload-queue-status").hidden = !waiting.length && !state.uploadError;
    $("upload-queue-status").classList.toggle("error", !!state.uploadError);
    $("upload-queue-status").textContent = state.uploadError || `正在按顺序保存截图，还有 ${waiting.length} 张处理中。可以继续添加，保存完成后即可识别。`;
    $("retry-upload-button").hidden = !state.uploadError;
    const hasRecognized = done > 0;
    $("recognize-button").querySelector("span").textContent = processing() ? "正在识别…" : hasRecognized && pending ? `识别新增截图（${pending}）` : pending ? `开始识别（${pending} 张）` : "开始识别";
    $("recognize-hint").textContent = processing() ? "正在识别当前截图，完成后可继续添加。" : waiting.length ? "截图保存完成后即可开始识别；可以继续添加。" : pending ? "可以继续添加截图；添加完成后点击「开始识别」。" : done ? "当前截图已识别。继续添加后，可单独识别新增截图。" : "可以继续添加截图；添加完成后点击「开始识别」。";
  }

  function switchTab(tab) {
    state.tab = tab;
    for (const key of ["groups", "records"]) {
      const active = key === tab;
      $(`tab-${key}`).classList.toggle("active", active);
      $(`tab-${key}`).setAttribute("aria-selected", String(active));
      $(`tab-${key}`).tabIndex = active ? 0 : -1;
      $(`panel-${key}`).hidden = !active;
    }
  }

  function renderGroups() {
    const batch = state.batch;
    const groups = batch.groups || [];
    const unmatched = (batch.unmatched_ids || []).map(findRecord).filter((record) => record && record.ocr_status !== "pending");
    $("unmatched-panel").hidden = !unmatched.length;
    $("unmatched-panel").innerHTML = unmatched.length ? `<div class="unmatched-heading"><div><h3>${unmatched.length} 张截图尚未归入订单</h3><p>点击截图检查识别信息，或使用「手动配对」关联。导出时可选择保留这些截图。</p></div></div><div class="unmatched-items">${unmatched.map((record) => `<button type="button" class="unmatched-item" data-show-record="${escape(record.id)}"><img src="${escape(imageURL(record))}" alt="" loading="lazy"><span>${escape(name(record))}</span></button>`).join("")}</div>` : "";
    $("groups-empty").hidden = !!groups.length;
    $("groups-grid").innerHTML = groups.map((group, index) => {
      const order = findRecord(group.order_id);
      const payments = (group.payment_ids || []).map(findRecord).filter(Boolean);
      const needsReview = group.status !== "matched";
      const shots = [order, ...payments].filter(Boolean);
      return `<article class="group-card ${needsReview ? "review" : ""}"><div class="group-heading"><h3>订单 ${String(index + 1).padStart(2, "0")}${payments.length > 1 ? ` · ${payments.length} 笔支付` : ""}</h3><span class="status-badge ${needsReview ? "warning" : ""}">${needsReview ? "需要核对" : "已配对"}</span></div><div class="group-preview"><div class="document-sheet">${shots.map((record, i) => `<div class="document-image"><img data-crop-id="${escape(record.id)}" src="${escape(imageURL(record))}" alt="${escape(`${i === 0 ? "订单" : "支付凭证"}：${name(record)}`)}" data-preview="${escape(record.id)}" tabindex="0" role="button"><span class="document-caption">${i === 0 ? "订单" : `支付${payments.length > 1 ? ` ${i}` : ""}`}</span></div>`).join("")}${!payments.length ? '<div class="missing-payment">尚未关联支付凭证</div>' : ""}</div></div><div class="group-details"><div class="group-amount">${escape(money(order?.amount_cents))}<small>${payments.length ? `支付合计 ${money(payments.reduce((sum, record) => sum + (record.amount_cents || 0), 0))}` : "待补充支付截图"}</small></div><div class="group-meta" title="${escape(groupTitle(order))}">${escape(groupTitle(order))}</div><p class="match-reason">${escape(group.reason || (needsReview ? "请检查订单与支付截图是否为同笔交易。" : "金额与交易信息相符。"))}</p><div class="group-controls"><button type="button" data-edit-group="${escape(group.id)}">${needsReview ? "核对 / 调整配对" : "调整配对"}</button><button type="button" data-remove-group="${escape(group.id)}">解除配对</button></div></div></article>`;
    }).join("");
    if ($("crop-toggle").checked) hydrateCrops();
  }

  async function cropPreview(record) {
    const crop = record.crop || [0, 0, 1, 1];
    if (crop.every((value, index) => value === [0, 0, 1, 1][index])) return imageURL(record);
    const key = `${state.batch.id}:${record.id}:${crop.join(",")}`;
    if (state.previewCache.has(key)) return state.previewCache.get(key);
    const promise = new Promise((resolve) => {
      const source = new Image();
      source.onload = () => {
        try {
          const width = source.naturalWidth * (crop[2] - crop[0]);
          const height = source.naturalHeight * (crop[3] - crop[1]);
          const scale = Math.min(1, 650 / width);
          const canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round(width * scale));
          canvas.height = Math.max(1, Math.round(height * scale));
          canvas.getContext("2d").drawImage(source, source.naturalWidth * crop[0], source.naturalHeight * crop[1], width, height, 0, 0, canvas.width, canvas.height);
          resolve(canvas.toDataURL("image/jpeg", .85));
        } catch (_) { resolve(imageURL(record)); }
      };
      source.onerror = () => resolve(imageURL(record));
      source.src = imageURL(record);
    });
    state.previewCache.set(key, promise);
    return promise;
  }

  function hydrateCrops() {
    document.querySelectorAll("img[data-crop-id]").forEach(async (image) => {
      const record = findRecord(image.dataset.cropId);
      if (!record) return;
      const source = await cropPreview(record);
      if (image.isConnected && $("crop-toggle").checked) image.src = source;
    });
  }

  function renderRecords() {
    const filter = $("record-filter").value;
    const subset = records().filter((record) => filter === "all" || (record.ocr_status !== "pending" && (filter === "unmatched" ? state.batch.unmatched_ids.includes(record.id) : record.kind === filter)));
    $("records-grid").innerHTML = subset.length ? subset.map((record) => {
      const index = records().findIndex((item) => item.id === record.id) + 1;
      const crop = record.crop || [0, 0, 1, 1];
      if (record.ocr_status === "pending") return `<article class="record-card" id="record-${escape(record.id)}"><div class="record-image-wrap"><button type="button" data-preview="${escape(record.id)}" aria-label="查看 ${escape(name(record))} 原图"><img src="${escape(imageURL(record))}" alt="${escape(name(record))}" loading="lazy"></button><span class="record-index">${String(index).padStart(2, "0")}</span><span class="record-kind-badge pending">待识别</span></div><div class="record-body"><h3 class="record-filename" title="${escape(name(record))}">${escape(name(record))}</h3><p class="pending-record-note">截图已保存在当前批次。添加完成后，点击上方「开始识别」。</p></div></article>`;
      return `<article class="record-card" id="record-${escape(record.id)}"><div class="record-image-wrap"><button type="button" data-preview="${escape(record.id)}" aria-label="查看 ${escape(name(record))} 原图"><img src="${escape(imageURL(record))}" alt="${escape(name(record))}" loading="lazy"></button><span class="record-index">${String(index).padStart(2, "0")}</span><span class="record-kind-badge">${escape(kinds[record.kind] || kinds.unknown)}</span></div><div class="record-body"><h3 class="record-filename" title="${escape(name(record))}">${escape(name(record))}</h3><form class="record-form" data-record-id="${escape(record.id)}"><div class="record-fields"><label>截图类型<select name="kind" aria-label="${escape(name(record))} 的截图类型">${Object.entries(kinds).map(([value, label]) => `<option value="${value}" ${value === record.kind ? "selected" : ""}>${label}</option>`).join("")}</select></label><label>实际金额（元）<input name="amount" type="text" inputmode="decimal" placeholder="待确认" value="${Number.isInteger(record.amount_cents) ? (record.amount_cents / 100).toFixed(2) : ""}" aria-label="${escape(name(record))} 的实际金额"></label><label>交易日期<input name="date" type="date" value="${escape(dateOnly(record.date))}" aria-label="${escape(name(record))} 的交易日期"></label><label>商户<input name="merchant" type="text" value="${escape(record.merchant)}" maxlength="100" placeholder="选填" aria-label="${escape(name(record))} 的商户"></label></div><details class="record-details"><summary>裁剪范围与识别文字</summary><p class="crop-help">边界按原图百分比设置。左 / 上默认 0，右 / 下默认 100；导出时可关闭裁剪。</p><div class="crop-fields">${["左边界 %", "上边界 %", "右边界 %", "下边界 %"].map((label, i) => `<label>${label}<input name="crop${i}" type="number" min="0" max="100" step="0.1" value="${Math.round(crop[i] * 1000) / 10}" aria-label="${escape(name(record))} 的${label}"></label>`).join("")}</div><pre class="ocr-text">${escape(record.text || "未识别到文字，可手动填写类型与金额。")}</pre></details>${record.warnings?.length ? `<p class="record-warning">${record.warnings.map(escape).join("<br>")}</p>` : ""}<div class="record-save-row"><span>${record.manually_edited ? "已手动修改" : `识别可信度 ${Math.round((record.confidence || 0) * 100)}%`}</span><button type="submit" class="button secondary">保存修改</button></div></form></div></article>`;
    }).join("") : '<div class="empty-state"><h3>当前分类没有截图</h3><p>选择「全部截图」查看本批次凭证。</p></div>';
    setBusy(state.busy);
  }

  function updateExport() {
    const batch = state.batch;
    if (!batch) return;
    const reviews = (batch.groups || []).filter((group) => group.status !== "matched").length;
    const unmatched = batch.unmatched_ids?.length || 0;
    const incomplete = reviews + unmatched > 0;
    const include = $("incomplete-toggle").checked;
    const format = $("export-format").value;
    const labels = { zip: "导出 Word + PDF", docx: "导出 Word", pdf: "导出 PDF" };
    $("export-button").querySelector("span").textContent = labels[format];
    if (pendingCount() || uploadsWaiting()) {
      $("export-hint").classList.add("warning");
      $("export-hint").textContent = uploadsWaiting() ? "请等待截图保存完成，再点击「开始识别」。" : `还有 ${pendingCount()} 张新增截图待识别，请先点击上方「开始识别」。`;
      return;
    }
    $("export-hint").classList.toggle("warning", incomplete);
    $("export-hint").textContent = incomplete
      ? include ? `待确认凭证也会保留：${reviews} 组待核对、${unmatched} 张未配对，未配对截图另附原图。` : `还有 ${reviews} 组待核对、${unmatched} 张未配对。请核对，或勾选同时导出。`
      : `${batch.groups?.length || 0} 组凭证已配对，可导出${$("crop-toggle").checked ? "裁剪后的" : "完整"}截图。`;
  }

  async function confirmAction(title, message, buttonText) {
    const dialog = $("confirm-dialog");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = message;
    dialog.querySelector('[value="confirm"]').textContent = buttonText;
    dialog.returnValue = "cancel";
    dialog.showModal();
    return new Promise((resolve) => { dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true }); });
  }

  const imageExtension = /\.(png|jpe?g|webp|tiff?|bmp)$/i;

  function imageSignature(bytes) {
    const starts = (signature) => signature.every((byte, index) => bytes[index] === byte);
    if (starts([137, 80, 78, 71, 13, 10, 26, 10])) return { mime: "image/png", extension: "png" };
    if (starts([255, 216, 255])) return { mime: "image/jpeg", extension: "jpg" };
    if (starts([82, 73, 70, 70]) && bytes[8] === 87 && bytes[9] === 69 && bytes[10] === 66 && bytes[11] === 80) return { mime: "image/webp", extension: "webp" };
    if (starts([66, 77])) return { mime: "image/bmp", extension: "bmp" };
    if (starts([73, 73, 42, 0]) || starts([77, 77, 0, 42])) return { mime: "image/tiff", extension: "tiff" };
    return null;
  }

  async function normalizeImageFile(file) {
    const signature = imageSignature(new Uint8Array(await file.slice(0, 32).arrayBuffer()));
    if (!signature && !String(file.type).startsWith("image/") && !imageExtension.test(file.name || "")) return null;
    if (!signature) return file; // Pillow validates claimed image files locally.
    let filename = String(file.name || "微信截图").replace(/^.*[\\/]/, "");
    if (!imageExtension.test(filename)) filename = `${filename.replace(/\.[^.]*$/, "") || "微信截图"}.${signature.extension}`;
    return new File([file], filename, { type: signature.mime, lastModified: file.lastModified || Date.now() });
  }

  function uploadFiles(list) {
    const incoming = Array.from(list || []);
    state.normalizing += 1;
    setBusy(state.busy);
    const pending = state.importChain.then(async () => {
      if (!await initialization) return "invalid";
      if (processing()) { notice("正在识别截图，请识别完成后继续添加。", true); return "processing"; }
      if (document.querySelector("dialog[open]")) { notice("请先关闭当前预览或确认窗口，再添加截图。"); return "dialog-open"; }
      if (incoming.length > 500) throw new Error("每批最多导入 500 张截图，请分批整理。");
      if (incoming.some((file) => file.size > 20 * 1024 * 1024)) throw new Error("存在超过 20 MB 的截图，请压缩后再导入。");
      if (incoming.reduce((sum, file) => sum + file.size, 0) > 512 * 1024 * 1024) throw new Error("本批图片总大小超过 512 MB，请分批导入。");
      const checked = await Promise.all(incoming.map(normalizeImageFile));
      const files = checked.filter(Boolean);
      if (!files.length) throw new Error("没有找到可导入的图片，请选择 PNG、JPG、WEBP、TIFF 或 BMP 文件。");
      if (files.length !== incoming.length) toast(`将导入 ${files.length} 张图片，已忽略非图片文件。`);
      // Processing or a dialog may have started while file headers were read.
      if (processing()) return "processing";
      if (document.querySelector("dialog[open]")) return "dialog-open";
      state.uploadQueue.push(...files.map((file) => ({ file, url: URL.createObjectURL(file), id: ++state.localId })));
      $("file-input").value = "";
      return "accepted";
    }).catch((error) => {
      notice(error.message || "无法读取拖入的图片，请复制图片后按 Ctrl + V，或先保存图片再添加。", true);
      return "invalid";
    }).finally(() => {
      state.normalizing -= 1;
      render();
      drainUploads();
    });
    state.importChain = pending.then(() => undefined);
    return pending;
  }

  function base64ImageFile(payload, index) {
    const data = String(payload?.data || "");
    if (!data || data.length > Math.ceil(20 * 1024 * 1024 / 3) * 4 + 4) throw new Error("图片为空或超过 20 MB，请重新添加。");
    const decoded = atob(data);
    const bytes = new Uint8Array(decoded.length);
    for (let offset = 0; offset < decoded.length; offset += 1) bytes[offset] = decoded.charCodeAt(offset);
    return new File([bytes], String(payload.name || `微信截图-${index + 1}`), { type: String(payload.mime || "") });
  }

  window.receiptNativeImport = async (payloads) => {
    try {
      if (!await initialization) return "invalid";
      if (processing()) return "processing";
      if (document.querySelector("dialog[open]")) return "dialog-open";
      if (!Array.isArray(payloads) || !payloads.length || payloads.length > 500) throw new Error("没有收到可导入的截图，请重新拖入图片。");
      return await uploadFiles(payloads.map(base64ImageFile));
    } catch (error) {
      notice(error.message || "无法读取微信图片，请复制图片后按 Ctrl + V，或先保存图片再添加。", true);
      return "invalid";
    }
  };

  function transferFiles(transfer) {
    const files = Array.from(transfer?.files || []);
    if (files.length) return files;
    return Array.from(transfer?.items || []).filter((item) => item.kind === "file")
      .map((item) => { try { return item.getAsFile(); } catch (_) { return null; } }).filter(Boolean);
  }

  function inlineImageFiles(html) {
    if (!html || !/data:image\//i.test(html)) return [];
    // Parse only embedded image data, so remote images/iframes/styles in the
    // original drag HTML never reach a document capable of requesting them.
    const sources = html.match(/data:image\/[a-z0-9.+-]+;base64,[a-z0-9+/=\s]+/ig) || [];
    const parsed = new DOMParser().parseFromString(sources.map((source) => `<img src="${source}">`).join(""), "text/html");
    return Array.from(parsed.querySelectorAll("img[src]")).flatMap((image, index) => {
      const source = image.getAttribute("src") || "";
      const match = /^data:(image\/[a-z0-9.+-]+);base64,([a-z0-9+/=\s]+)$/i.exec(source);
      return match ? [base64ImageFile({ name: `粘贴截图-${index + 1}`, mime: match[1], data: match[2].replace(/\s/g, "") }, index)] : [];
    });
  }

  async function handleDrop(event) {
    const transfer = event.dataTransfer;
    const files = transferFiles(transfer);
    const types = Array.from(transfer?.types || []);
    const hasImageTransfer = files.length || types.some((type) => type === "Files" || type === "text/uri-list" || type === "text/html" || type.startsWith("image/"));
    if (!hasImageTransfer) return;
    event.preventDefault();
    $("drop-zone").classList.remove("drag-over");
    if (event.target.closest?.("input, textarea, select, [contenteditable=true]")) return;
    try {
      if (files.length) { await uploadFiles(files); return; }
      // GTK reads native URI/path-only drags itself. Wait for its import bridge
      // instead of racing it with a false empty-files error in the WebView.
      if (window.receiptNativeDrop) return;
      const inline = inlineImageFiles(transfer?.getData?.("text/html") || "");
      if (inline.length) { await uploadFiles(inline); return; }
      notice("微信未提供图片文件，可复制图片后按 Ctrl + V 粘贴，或先保存图片再添加。");
    } catch (error) {
      notice(error.message || "无法读取拖入的图片，请复制后按 Ctrl + V，或先保存图片再添加。", true);
    }
  }

  async function drainUploads() {
    if (state.uploading || state.busy || processing() || state.uploadError || !state.uploadQueue.length) return;
    state.uploading = true;
    setBusy(state.busy);
    while (state.uploadQueue.length && !state.busy && !processing() && !state.uploadError) {
      const items = state.uploadQueue.splice(0, 500);
      state.activeUploads = items;
      renderCollection();
      const form = new FormData();
      items.forEach((item) => form.append("files", item.file, item.file.name || `粘贴截图-${item.id}.png`));
      try {
        const path = state.batch ? `/api/batches/${state.batch.id}/images` : "/api/batches";
        const batch = await api(path, { method: "POST", body: form });
        state.activeUploads = [];
        updateBatch(batch);
        items.forEach((item) => URL.revokeObjectURL(item.url));
        notice("");
      } catch (error) {
        state.uploadQueue.unshift(...items);
        state.activeUploads = [];
        state.uploadError = `${error.message} 截图仍保留在导入队列中，请点击「重试导入」。`;
        notice(state.uploadError, true);
      }
    }
    state.uploading = false;
    render();
  }

  async function recognizeBatch() {
    if (state.busy || uploadsWaiting() || processing() || !state.batch || pendingCount() === 0) return;
    setBusy(true); notice("");
    try {
      const batch = await api(`/api/batches/${state.batch.id}/recognize`, { method: "POST" });
      updateBatch(batch);
      state.tab = "groups";
      startPolling();
    } catch (error) { notice(error.message, true); }
    finally { setBusy(false); }
  }

  async function retryRecognition() {
    if (state.busy || uploadsWaiting() || processing() || !state.batch || pendingCount() > 0 || retryEligibleRecords().length === 0) return;
    setBusy(true); notice("");
    try {
      const batch = await api(`/api/batches/${state.batch.id}/retry-recognition`, { method: "POST" });
      updateBatch(batch);
      startPolling();
    } catch (error) { notice(error.message, true); }
    finally { setBusy(false); }
  }

  function startPolling() {
    clearTimeout(state.polling);
    if (!processing()) return;
    state.polling = setTimeout(async () => {
      try {
        const batch = await api(`/api/batches/${encodeURIComponent(state.batch.id)}`);
        notice("");
        updateBatch(batch);
        if (processing()) startPolling();
        else {
          toast(batch.status === "error" ? "识别已结束，请检查提示并手动补全。" : `已识别 ${batch.records.length} 张截图，请核对配对结果。`);
          $("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
        }
      } catch (error) {
        notice(`${error.message} 保留当前批次，正在重试连接。`, true);
        state.polling = setTimeout(startPolling, 3500);
      }
    }, 900);
  }

  async function saveRecord(form) {
    if (state.busy) return;
    const data = new FormData(form);
    const amount = String(data.get("amount")).trim();
    if (amount && (!/^\d+(?:\.\d{1,2})?$/.test(amount) || Number(amount) <= 0 || Number(amount) > 1000000)) {
      notice("实际金额请填写大于 0、最多两位小数的数字。", true);
      form.elements.amount.focus(); return;
    }
    const crop = [0, 1, 2, 3].map((i) => Number(data.get(`crop${i}`)) / 100);
    if (crop.some((value) => !Number.isFinite(value) || value < 0 || value > 1) || crop[2] - crop[0] < .02 || crop[3] - crop[1] < .02) {
      notice("裁剪边界应在 0–100% 之间，右边界需大于左边界，下边界需大于上边界，宽高至少为 2%。", true); return;
    }
    const record = findRecord(form.dataset.recordId);
    const payload = { kind: data.get("kind"), amount_cents: amount ? Math.round(Number(amount) * 100) : null, date: data.get("date") || null, merchant: String(data.get("merchant")).trim(), crop };
    // Send only changed values: a crop-only correction should preserve confirmed grouping.
    for (const key of Object.keys(payload)) {
      const original = key === "date" ? dateOnly(record.date) || null : key === "merchant" ? record.merchant || "" : key === "crop" ? record.crop || [0, 0, 1, 1] : record[key];
      if (JSON.stringify(original) === JSON.stringify(payload[key])) delete payload[key];
    }
    if (!Object.keys(payload).length) { toast("当前信息已经保存。 "); return; }
    setBusy(true); notice("");
    try {
      const batch = await api(`/api/batches/${state.batch.id}/records/${encodeURIComponent(record.id)}`, jsonOptions("PATCH", payload));
      updateBatch(batch);
      toast("修改已保存。金额或类型变更后，请重新配对或手动确认。");
    } catch (error) { notice(error.message, true); }
    finally { setBusy(false); }
  }

  function showPreview(id) {
    const record = findRecord(id);
    if (!record) return;
    $("preview-title").textContent = name(record);
    $("preview-image").src = imageURL(record);
    $("preview-image").alt = name(record);
    $("preview-dialog").showModal();
  }

  function showRecord(id) {
    $("record-filter").value = "all";
    renderRecords(); switchTab("records");
    requestAnimationFrame(() => {
      const card = $(`record-${id}`);
      if (!card) return;
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("highlight");
      setTimeout(() => card.classList.remove("highlight"), 2300);
      card.querySelector("select")?.focus({ preventScroll: true });
    });
  }

  function openGroup(id = null) {
    const orders = recognizedRecords().filter((record) => record.kind === "order");
    const payments = recognizedRecords().filter((record) => record.kind === "payment");
    if (!orders.length || !payments.length) {
      notice("手动配对需要至少一张订单截图和一张支付截图。请在「识别结果」中检查截图类型。", true);
      switchTab("records"); return;
    }
    state.groupId = id;
    const group = (state.batch.groups || []).find((item) => item.id === id);
    const assigned = new Map();
    for (const item of state.batch.groups || []) {
      for (const rid of [item.order_id, ...item.payment_ids]) assigned.set(rid, item.id);
    }
    $("group-dialog-title").textContent = id ? "核对并调整配对" : "关联一笔订单";
    $("group-order").innerHTML = orders.map((record) => `<option value="${escape(record.id)}" ${record.id === group?.order_id ? "selected" : ""}>${escape(money(record.amount_cents))} · ${escape(name(record))}${assigned.has(record.id) && assigned.get(record.id) !== id ? "（已有配对）" : ""}</option>`).join("");
    $("group-payments").innerHTML = payments.map((record) => `<label class="payment-option"><input type="checkbox" name="payment" value="${escape(record.id)}" ${group?.payment_ids.includes(record.id) ? "checked" : ""}><img src="${escape(imageURL(record))}" alt=""><span class="payment-info"><span>${escape(name(record))}</span><small>${escape([dateOnly(record.date), record.merchant, assigned.has(record.id) && assigned.get(record.id) !== id ? "已有配对，选择后移入当前组" : ""].filter(Boolean).join(" · "))}</small></span><strong>${escape(money(record.amount_cents))}</strong></label>`).join("");
    updatePaymentSum();
    $("group-dialog").showModal();
  }

  function updatePaymentSum() {
    const order = findRecord($("group-order").value);
    const selected = [...$("group-payments").querySelectorAll("input:checked")].map((input) => findRecord(input.value));
    const sum = selected.reduce((total, record) => total + (record?.amount_cents || 0), 0);
    $("payment-sum").textContent = `合计 ${money(sum)}`;
    const missingAmount = selected.some((record) => !record?.amount_cents) || !order?.amount_cents;
    const mismatch = !!selected.length && (missingAmount || order.amount_cents !== sum);
    $("group-warning").hidden = !mismatch;
    $("group-warning").textContent = missingAmount ? "有截图金额尚未确认。可以保存当前关联，补全金额后再确认。" : `支付合计与订单金额 ${money(order.amount_cents)} 不一致。保存后仍标记为需要核对。`;
  }

  async function saveGroup(event) {
    event.preventDefault();
    if (state.busy) return;
    const orderId = $("group-order").value;
    const paymentIds = [...$("group-payments").querySelectorAll("input:checked")].map((input) => input.value);
    if (!paymentIds.length) { $("group-warning").hidden = false; $("group-warning").textContent = "请至少选择一张对应的支付截图。"; return; }
    const order = findRecord(orderId);
    const total = paymentIds.reduce((sum, id) => sum + (findRecord(id)?.amount_cents || 0), 0);
    const complete = order?.amount_cents === total && paymentIds.every((id) => !!findRecord(id)?.amount_cents);
    const groups = (state.batch.groups || []).filter((group) => group.id !== state.groupId && group.order_id !== orderId).map((group) => {
      const next = { ...group, payment_ids: group.payment_ids.filter((id) => !paymentIds.includes(id)) };
      if (next.payment_ids.length !== group.payment_ids.length) { next.status = "review"; next.reason = "部分支付凭证已移至其他订单，请重新核对。"; }
      return next;
    });
    groups.push({ id: state.groupId || `manual-${Date.now().toString(36)}`, order_id: orderId, payment_ids: paymentIds, status: complete ? "matched" : "review", reason: complete ? "手动确认，扣款合计与订单一致。" : "金额待核对。", manual: true });
    setBusy(true); notice("");
    try {
      updateBatch(await api(`/api/batches/${state.batch.id}/groups`, jsonOptions("PUT", { groups })));
      $("group-dialog").close();
      toast(complete ? "配对已确认并保存。" : "关联已保存，金额仍需核对。");
    } catch (error) { notice(error.message, true); $("group-warning").textContent = error.message; $("group-warning").hidden = false; }
    finally { setBusy(false); }
  }

  async function removeGroup(id) {
    if (state.busy) return;
    const groups = state.batch.groups.filter((group) => group.id !== id);
    setBusy(true); notice("");
    try {
      updateBatch(await api(`/api/batches/${state.batch.id}/groups`, jsonOptions("PUT", { groups })));
      toast("已解除配对，截图保留在「尚未配对」中。");
    } catch (error) { notice(error.message, true); }
    finally { setBusy(false); }
  }

  async function rematch() {
    if (state.busy) return;
    const hasManual = state.batch.groups?.some((group) => group.id.startsWith("manual-") || (group.reason || "").includes("手动确认"));
    if (hasManual && !await confirmAction("重新自动配对？", "自动配对会根据当前识别信息重新关联所有截图，并替换已保存的手动配对。", "重新自动配对")) return;
    setBusy(true); notice("");
    try { updateBatch(await api(`/api/batches/${state.batch.id}/rematch`, { method: "POST" })); toast("已按当前识别信息重新配对。"); }
    catch (error) { notice(error.message, true); }
    finally { setBusy(false); }
  }

  async function exportBatch() {
    if (state.busy || !state.batch) return;
    if (pendingCount() || uploadsWaiting()) { notice("请等待截图保存完成，并点击「开始识别」处理新增截图后再导出。", true); return; }
    const options = { format: $("export-format").value, crop: $("crop-toggle").checked, title: $("document-title").value.trim(), allow_incomplete: $("incomplete-toggle").checked };
    const incomplete = state.batch.unmatched_ids?.length || state.batch.groups.some((group) => group.status !== "matched");
    if (incomplete && !options.allow_incomplete) { notice("还有未配对或待核对的凭证。请先核对，或勾选「同时导出待确认与未配对截图」。", true); $("incomplete-toggle").focus(); return; }
    setBusy(true); notice("");
    $("export-button").querySelector("span").textContent = "正在生成文档…";
    try {
      const response = await fetch(`/api/batches/${state.batch.id}/export`, jsonOptions("POST", options));
      if (!response.ok) throw new Error(await responseError(response));
      const blob = await response.blob();
      const header = response.headers.get("Content-Disposition") || "";
      const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header)?.[1];
      let filename = `凭证归档.${options.format}`;
      try { if (encoded) filename = decodeURIComponent(encoded); } catch (_) { /* Keep safe default filename. */ }
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(href), 60000);
      $("step-export").classList.add("active");
      toast(`${options.format === "zip" ? "Word 与 PDF 已打包" : "文档已生成"}，请在浏览器下载中查看。`);
    } catch (error) { notice(error.message || "文档导出失败，请检查本机服务并重试。", true); }
    finally { setBusy(false); updateExport(); }
  }

  async function clearBatch() {
    if (state.busy || state.uploading || state.normalizing > 0 || (!state.batch && !state.uploadQueue.length)) return;
    if (!await confirmAction("清空本批次？", "本批次保存的截图、识别结果和配对将从本机工作区删除。已下载的 Word 和 PDF 不受影响。", "确认清空")) return;
    setBusy(true);
    try {
      if (state.batch) await api(`/api/batches/${state.batch.id}`, { method: "DELETE" });
      state.uploadQueue.forEach((item) => URL.revokeObjectURL(item.url));
      state.uploadQueue = []; state.activeUploads = []; state.uploadError = "";
      state.batch = null; state.previewCache.clear(); persist(null); clearTimeout(state.polling);
      notice(""); render(); toast("本批次已清空，可以导入下一批截图。");
    } catch (error) { notice(error.message, true); }
    finally { setBusy(false); }
  }

  async function init(signal) {
    try {
      const health = await api("/api/health", { signal });
      $("health-status").textContent = health.ocr_ready ? "识别引擎已就绪" : "识别引擎未安装";
      $("health-status").classList.toggle("error", !health.ocr_ready);
      if (!health.ocr_ready) notice("识别引擎尚未安装。请按使用说明完成依赖安装；仍可导入截图并手动填写金额与类型。", true);
    } catch (error) { $("health-status").textContent = "服务连接失败"; $("health-status").classList.add("error"); notice(error.message, true); }
    let id;
    try { id = localStorage.getItem(storageKey); } catch (_) { /* Continue without persistence. */ }
    if (id && /^[a-f0-9]{32}$/.test(id)) {
      try { updateBatch(await api(`/api/batches/${id}`, { signal })); startPolling(); }
      catch (error) {
        if (error.status === 404) {
          persist(null);
          notice("上次批次已不存在，可以导入新截图。");
        } else {
          notice(`上次批次尚未恢复，请刷新工作区后重试添加截图。${error.message}`, true);
          return false;
        }
      }
    } else {
      try {
        const latest = await api("/api/batches/latest", { signal });
        if (latest && /^[a-f0-9]{32}$/.test(latest.id)) { updateBatch(latest); startPolling(); }
      } catch (_) { /* No active batch to resume. */ }
    }
    return true;
  }

  $("drop-zone").addEventListener("click", () => { if (!state.busy && !processing()) $("file-input").click(); });
  $("drop-zone").addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("drop-zone").click(); } });
  $("file-input").addEventListener("change", (event) => uploadFiles(event.target.files));
  ["dragenter", "dragover"].forEach((type) => $("drop-zone").addEventListener(type, (event) => { event.preventDefault(); if (!state.busy && !processing()) $("drop-zone").classList.add("drag-over"); }));
  $("drop-zone").addEventListener("dragleave", (event) => { if (!$("drop-zone").contains(event.relatedTarget)) $("drop-zone").classList.remove("drag-over"); });
  document.addEventListener("dragover", (event) => { if (Array.from(event.dataTransfer?.types || []).some((type) => type === "Files" || type === "text/uri-list" || type === "text/html" || type.startsWith("image/"))) event.preventDefault(); });
  document.addEventListener("drop", handleDrop);
  document.addEventListener("paste", (event) => {
    const files = transferFiles(event.clipboardData);
    if (files.length && !document.querySelector("dialog[open]")) { event.preventDefault(); uploadFiles(files); }
  });
  for (const tab of ["groups", "records"]) {
    $(`tab-${tab}`).addEventListener("click", () => switchTab(tab));
    $(`tab-${tab}`).addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? "groups" : event.key === "End" ? "records" : tab === "groups" ? "records" : "groups"; switchTab(next); $(`tab-${next}`).focus(); } });
  }
  $("record-filter").addEventListener("change", renderRecords);
  $("records-grid").addEventListener("submit", (event) => { if (event.target.matches(".record-form")) { event.preventDefault(); saveRecord(event.target); } });
  document.addEventListener("click", (event) => {
    const preview = event.target.closest("[data-preview]");
    const show = event.target.closest("[data-show-record]");
    const edit = event.target.closest("[data-edit-group]");
    const remove = event.target.closest("[data-remove-group]");
    if (preview) showPreview(preview.dataset.preview);
    if (show) showRecord(show.dataset.showRecord);
    if (edit) openGroup(edit.dataset.editGroup);
    if (remove) removeGroup(remove.dataset.removeGroup);
  });
  document.addEventListener("keydown", (event) => { if ((event.key === "Enter" || event.key === " ") && event.target.matches("img[data-preview]")) { event.preventDefault(); showPreview(event.target.dataset.preview); } });
  $("close-preview").addEventListener("click", () => $("preview-dialog").close());
  ["close-group", "cancel-group"].forEach((id) => $(id).addEventListener("click", () => $("group-dialog").close()));
  $("create-group-button").addEventListener("click", () => openGroup());
  $("group-payments").addEventListener("change", updatePaymentSum);
  $("group-order").addEventListener("change", updatePaymentSum);
  $("group-form").addEventListener("submit", saveGroup);
  $("rematch-button").addEventListener("click", rematch);
  $("recognize-button").addEventListener("click", recognizeBatch);
  $("retry-recognition-button").addEventListener("click", retryRecognition);
  $("retry-upload-button").addEventListener("click", () => { state.uploadError = ""; notice(""); drainUploads(); });
  $("clear-button").addEventListener("click", clearBatch);
  $("export-button").addEventListener("click", exportBatch);
  $("crop-toggle").addEventListener("change", () => { if (state.batch) renderGroups(); updateExport(); });
  ["incomplete-toggle", "export-format"].forEach((id) => $(id).addEventListener("change", updateExport));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target !== dialog) return; const bounds = dialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close(); }));
  const startupController = new AbortController();
  const startupTimeout = setTimeout(() => startupController.abort(), 15000);
  initialization = init(startupController.signal).then((ready) => {
    window.receiptWorkspaceReady = ready;
    return ready;
  }).catch((error) => {
    notice(error.message || "工作区尚未恢复，请刷新后重试添加截图。", true);
    return false;
  }).finally(() => clearTimeout(startupTimeout));
})();
