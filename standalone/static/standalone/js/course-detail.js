function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function setBlockExpanded(toggle, target, expanded) {
  toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  target.hidden = !expanded;
  toggle.closest("[data-block-card]")?.classList.toggle("is-expanded", expanded);
}

function openBlockTarget(targetId, options = {}) {
  const shouldScroll = options.scroll !== false;
  const toggle = document.querySelector(`[data-block-toggle][data-block-target="${targetId}"]`);
  const target = targetId ? document.getElementById(targetId) : null;
  if (!toggle || !target) {
    return;
  }
  setBlockExpanded(toggle, target, true);
  if (shouldScroll) {
    target.scrollIntoView({ block: "start", behavior: "smooth" });
  }
}

function closeBlockActionMenus(root = document) {
  root.querySelectorAll("[data-block-menu-trigger]").forEach((trigger) => {
    trigger.setAttribute("aria-expanded", "false");
  });
  root.querySelectorAll("[data-block-menu-panel]").forEach((panel) => {
    panel.hidden = true;
  });
  root.querySelectorAll("[data-block-card].is-menu-open").forEach((card) => {
    card.classList.remove("is-menu-open");
  });
}

function ensureBlockActionMenuDocumentBindings(doc = document) {
  if (doc.documentElement.dataset.blockActionMenusBound === "true") {
    return;
  }

  doc.addEventListener("click", () => {
    closeBlockActionMenus(doc);
  });
  doc.documentElement.dataset.blockActionMenusBound = "true";
}

function getExpandedBlockIds(root = document) {
  return Array.from(root.querySelectorAll("[data-block-toggle][aria-expanded='true']"))
    .map((toggle) => toggle.dataset.blockTarget)
    .filter(Boolean);
}

function restoreExpandedBlocks(expandedIds, root = document) {
  expandedIds.forEach((targetId) => {
    const toggle = root.querySelector(`[data-block-toggle][data-block-target="${targetId}"]`);
    const target = targetId ? root.getElementById?.(targetId) || document.getElementById(targetId) : null;
    if (!toggle || !target) {
      return;
    }
    setBlockExpanded(toggle, target, true);
  });
}

function expandBlockPathFromHash(root = document, options = {}) {
  const shouldScroll = options.scroll !== false;
  const targetId = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : "";
  if (!targetId) {
    return;
  }

  let currentTarget = root.getElementById?.(targetId) || document.getElementById(targetId);
  if (!currentTarget) {
    return;
  }

  while (currentTarget) {
    const toggle = root.querySelector(`[data-block-toggle][data-block-target="${currentTarget.id}"]`);
    if (!toggle) {
      break;
    }
    setBlockExpanded(toggle, currentTarget, true);
    const parentContent = toggle.closest(".block-card-content");
    currentTarget = parentContent && parentContent.id ? parentContent : null;
  }

  const scrollTarget = root.getElementById?.(targetId) || document.getElementById(targetId);
  if (shouldScroll) {
    scrollTarget?.scrollIntoView({ block: "start", behavior: "auto" });
  }
}

let pendingAssetRefreshHandle = null;
let pendingAssetRefreshInFlight = false;
let courseDetailStatusSyncInFlight = false;
let courseDetailRefreshRequestSequence = 0;
let courseDetailLatestRefreshRequest = 0;
let settingsToastHideHandle = null;
let backgroundJobStatusPollHandle = null;
let backgroundJobStatusSyncInFlight = false;

const BACKGROUND_JOB_STATUS_CLASSES = ["is-ready", "is-completed", "is-paused", "is-uploaded", "is-analyzing", "is-creating", "is-queued", "is-running", "is-failed"];

function beginCourseDetailRefreshRequest() {
  courseDetailRefreshRequestSequence += 1;
  courseDetailLatestRefreshRequest = courseDetailRefreshRequestSequence;
  return courseDetailRefreshRequestSequence;
}

function isLatestCourseDetailRefreshRequest(requestId) {
  return requestId === courseDetailLatestRefreshRequest;
}

function hasPendingCourseDetailIndicators(root = document) {
  return Boolean(root.querySelector("[data-processing-asset='true'], [data-regenerating-block='true']"));
}

function showSettingsToast(message) {
  const toast = document.querySelector("[data-settings-toast]");
  if (!toast) {
    return;
  }
  toast.textContent = message || "Settings updated.";
  toast.hidden = false;
  window.requestAnimationFrame(() => {
    toast.classList.add("is-visible");
  });
  if (settingsToastHideHandle) {
    window.clearTimeout(settingsToastHideHandle);
  }
  settingsToastHideHandle = window.setTimeout(() => {
    toast.classList.remove("is-visible");
    window.setTimeout(() => {
      toast.hidden = true;
    }, 180);
  }, 1800);
}

function renderBackgroundJobStatus(panel, status) {
  if (!panel || !status) {
    return;
  }
  const label = panel.querySelector("[data-background-jobs-label]");
  const running = panel.querySelector("[data-background-jobs-running]");
  const queued = panel.querySelector("[data-background-jobs-queued]");
  const detail = panel.querySelector("[data-background-jobs-detail]");
  const error = panel.querySelector("[data-background-jobs-error]");

  if (label) {
    label.textContent = String(status.label || "");
    label.classList.remove(...BACKGROUND_JOB_STATUS_CLASSES);
    if (status.class_name) {
      label.classList.add(String(status.class_name));
    }
  }
  if (running) {
    const text = String(status.running_job_label || "");
    running.textContent = text;
    running.hidden = !text;
  }
  if (queued) {
    const count = Number(status.queued_count || 0);
    queued.textContent = count > 0 ? `${count} queued` : "";
    queued.hidden = count <= 0;
  }
  if (detail) {
    detail.textContent = String(status.detail || "");
  }
  if (error) {
    const text = String(status.error || "");
    error.textContent = text;
    error.hidden = !text;
  }
}

async function refreshBackgroundJobStatusNow() {
  if (backgroundJobStatusSyncInFlight || document.hidden) {
    return;
  }
  const panel = document.querySelector("[data-background-jobs-status]");
  const url = panel?.dataset.backgroundJobsStatusUrl;
  if (!panel || !url) {
    return;
  }

  backgroundJobStatusSyncInFlight = true;
  try {
    const response = await fetch(url, {
      headers: {
        Accept: "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      credentials: "same-origin",
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok || !payload.status) {
      throw new Error("Unable to refresh background job status.");
    }
    renderBackgroundJobStatus(panel, payload.status);
  } catch (_error) {
    // Allow the next poll to retry quietly.
  } finally {
    backgroundJobStatusSyncInFlight = false;
  }
}

function ensureBackgroundJobStatusPolling() {
  if (backgroundJobStatusPollHandle !== null) {
    return;
  }
  if (!document.querySelector("[data-background-jobs-status]")) {
    return;
  }
  backgroundJobStatusPollHandle = window.setInterval(() => {
    void refreshBackgroundJobStatusNow();
  }, 5000);
}

function setCourseConfigRowState(row, state, message = "") {
  if (!row) {
    return;
  }
  row.dataset.saveState = state;
  const status = row.querySelector("[data-course-config-status]");
  if (!status) {
    return;
  }
  if (!message) {
    status.hidden = true;
    status.textContent = "";
    return;
  }
  status.hidden = false;
  status.textContent = message;
}

function setBlockConfigRowState(row, state, message = "") {
  if (!row) {
    return;
  }
  row.dataset.saveState = state;
  const status = row.querySelector("[data-block-config-status]");
  if (!status) {
    return;
  }
  if (!message) {
    status.hidden = true;
    status.textContent = "";
    return;
  }
  status.hidden = false;
  status.textContent = message;
}

function syncDemoSettingsVisibility(root = document) {
  const demoToggle = root.querySelector("[data-course-config-field='demo_enabled'] input[type='checkbox']");
  const enabled = !!demoToggle?.checked;
  root.querySelectorAll("[data-demo-settings-enabled-only]").forEach((element) => {
    element.hidden = !enabled;
  });
  root.querySelectorAll("[data-demo-settings-disabled-copy]").forEach((element) => {
    element.hidden = enabled;
  });
}

function isTextLikeAutosaveInput(input) {
  if (!input) {
    return false;
  }
  if (input.tagName === "TEXTAREA") {
    return true;
  }
  if (input.tagName !== "INPUT") {
    return false;
  }
  const type = (input.type || "text").toLowerCase();
  return !["checkbox", "radio", "range", "color", "file", "hidden"].includes(type);
}

function shouldSyncSavedValueToInput(input, submittedValue) {
  if (!isTextLikeAutosaveInput(input)) {
    return true;
  }
  if (document.activeElement === input) {
    return false;
  }
  return input.value === submittedValue;
}

async function saveCourseConfigInput(input) {
  const row = input.closest("[data-course-config-row]");
  const fieldName = row?.dataset.courseConfigField;
  const url = row?.dataset.courseConfigUrl;
  if (!row || !fieldName || !url) {
    return;
  }

  if (row.dataset.saving === "true") {
    row.dataset.pendingSave = "true";
    return;
  }

  const body = new URLSearchParams();
  let submittedValue = "";
  if (input.type === "checkbox") {
    if (input.checked) {
      body.set(fieldName, "on");
    }
    submittedValue = input.checked ? "on" : "";
  } else {
    submittedValue = input.value;
    body.set(fieldName, submittedValue);
  }

  row.dataset.saving = "true";
  row.dataset.pendingSave = "false";
  setCourseConfigRowState(row, "saving", "Saving...");

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body: body.toString(),
      credentials: "same-origin",
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error((payload.errors || ["Unable to save this setting."]).join(" "));
    }
    if (input.type === "checkbox" && typeof payload.checked === "boolean") {
      input.checked = payload.checked;
    } else if (
      payload.raw_value !== undefined &&
      payload.raw_value !== null &&
      shouldSyncSavedValueToInput(input, submittedValue)
    ) {
      input.value = `${payload.raw_value}`;
    }
    setCourseConfigRowState(row, "saved", "Saved");
    showSettingsToast(payload.message || "Settings updated.");
    window.setTimeout(() => {
      if (row.dataset.saveState === "saved") {
        setCourseConfigRowState(row, "", "");
      }
    }, 1200);
  } catch (error) {
    setCourseConfigRowState(row, "error", error.message || "Unable to save this setting.");
  } finally {
    row.dataset.saving = "false";
    if (row.dataset.pendingSave === "true") {
      row.dataset.pendingSave = "false";
      void saveCourseConfigInput(input);
    }
  }
}

async function saveBlockConfigInput(input) {
  const row = input.closest("[data-block-config-row]");
  const fieldName = row?.dataset.blockConfigField;
  const url = row?.dataset.blockConfigUrl;
  if (!row || !fieldName || !url) {
    return;
  }

  if (row.dataset.saving === "true") {
    row.dataset.pendingSave = "true";
    return;
  }

  const body = new URLSearchParams();
  const submittedValue = input.value;
  body.set(fieldName, submittedValue);

  row.dataset.saving = "true";
  row.dataset.pendingSave = "false";
  setBlockConfigRowState(row, "saving", "Saving...");

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body: body.toString(),
      credentials: "same-origin",
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error((payload.errors || ["Unable to save this setting."]).join(" "));
    }
    if (shouldSyncSavedValueToInput(input, submittedValue)) {
      if (payload.raw_value === null || payload.raw_value === undefined || payload.raw_value === "") {
        input.value = "";
      } else {
        input.value = `${payload.raw_value}`;
      }
    }
    setBlockConfigRowState(row, "saved", "Saved");
    showSettingsToast(payload.message || "Block settings updated.");
    window.setTimeout(() => {
      if (row.dataset.saveState === "saved") {
        setBlockConfigRowState(row, "", "");
      }
    }, 1200);
  } catch (error) {
    setBlockConfigRowState(row, "error", error.message || "Unable to save this setting.");
  } finally {
    row.dataset.saving = "false";
    if (row.dataset.pendingSave === "true") {
      row.dataset.pendingSave = "false";
      void saveBlockConfigInput(input);
    }
  }
}

function schedulePendingAssetRefresh(root = document) {
  if (pendingAssetRefreshHandle) {
    window.clearTimeout(pendingAssetRefreshHandle);
    pendingAssetRefreshHandle = null;
  }

  if (!hasPendingCourseDetailIndicators(root)) {
    return;
  }

  pendingAssetRefreshHandle = window.setTimeout(async () => {
    if (pendingAssetRefreshInFlight) {
      schedulePendingAssetRefresh(document);
      return;
    }

    pendingAssetRefreshInFlight = true;
    const expandedIds = getExpandedBlockIds(document);
    const requestId = beginCourseDetailRefreshRequest();

    try {
      const response = await fetch(`${window.location.pathname}${window.location.search}`, {
        headers: {
          "X-Requested-With": "XMLHttpRequest",
        },
        credentials: "same-origin",
      });
      const responseText = await response.text();
      if (!response.ok) {
        throw new Error("Unable to refresh processing state.");
      }
      if (isLatestCourseDetailRefreshRequest(requestId)) {
        await refreshCourseDetailFromResponse(responseText, expandedIds);
      }
    } catch (_error) {
      // Retry on the next cycle while work is still pending.
    } finally {
      pendingAssetRefreshInFlight = false;
      schedulePendingAssetRefresh(document);
    }
  }, 2500);
}

async function syncPendingCourseDetailStateNow() {
  if (courseDetailStatusSyncInFlight || !hasPendingCourseDetailIndicators(document)) {
    return;
  }

  courseDetailStatusSyncInFlight = true;
  const expandedIds = getExpandedBlockIds(document);
  const requestId = beginCourseDetailRefreshRequest();

  try {
    const response = await fetch(`${window.location.pathname}${window.location.search}`, {
      headers: {
        "X-Requested-With": "XMLHttpRequest",
      },
      credentials: "same-origin",
    });
    const responseText = await response.text();
    if (!response.ok) {
      throw new Error("Unable to refresh current status.");
    }
    if (isLatestCourseDetailRefreshRequest(requestId)) {
      await refreshCourseDetailFromResponse(responseText, expandedIds);
    }
  } catch (_error) {
    // Allow the next poll or visibility/focus event to retry.
  } finally {
    courseDetailStatusSyncInFlight = false;
  }
}

async function refreshCourseDetailFromResponse(responseText, expandedIds) {
  const parser = new DOMParser();
  const nextDocument = parser.parseFromString(responseText, "text/html");
  const currentShell = document.querySelector(".app-shell");
  const nextShell = nextDocument.querySelector(".app-shell");
  const currentScrollY = window.scrollY;

  if (!currentShell || !nextShell) {
    window.location.reload();
    return;
  }

  currentShell.innerHTML = nextShell.innerHTML;
  initializeCourseDetail(currentShell);
  restoreExpandedBlocks(expandedIds, document);
  expandBlockPathFromHash(document, { scroll: false });
  window.scrollTo({ top: currentScrollY, behavior: "auto" });
}

async function submitAsyncRefreshForm(form) {
  const expandedIds = getExpandedBlockIds(document);
  const submitButton = form.querySelector("button[type='submit']");
  const confirmMessage = form.dataset.confirm;

  if (confirmMessage && !window.confirm(confirmMessage)) {
    return;
  }

  if (submitButton) {
    submitButton.disabled = true;
  }

  try {
    const requestId = beginCourseDetailRefreshRequest();
    const response = await fetch(form.action, {
      method: (form.method || "POST").toUpperCase(),
      headers: {
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body: new FormData(form),
      credentials: "same-origin",
    });

    const responseText = await response.text();
    if (!response.ok) {
      throw new Error("Unable to update this section.");
    }
    if (isLatestCourseDetailRefreshRequest(requestId)) {
      await refreshCourseDetailFromResponse(responseText, expandedIds);
    }
  } catch (error) {
    if (submitButton) {
      submitButton.disabled = false;
    }
    window.alert(error.message || "Unable to update this section.");
  }
}

function createEditor(element) {
  if (element.dataset.editing === "true") {
    return;
  }

  const originalValue = element.textContent.trim();
  const originalInlineValue = element.dataset.inlineValue || "";
  const placeholder = element.dataset.inlinePlaceholder || "";
  const isMultiline = element.dataset.inlineMultiline === "true";
  const label = element.dataset.inlineLabel || "Value";
  const inputType = element.dataset.inlineInputType || "text";

  element.dataset.editing = "true";
  element.dataset.originalValue = originalValue;
  element.dataset.originalInlineValue = originalInlineValue;
  element.classList.add("is-editing");

  const wrapper = document.createElement("div");
  wrapper.className = "inline-editor";

  const field = isMultiline ? document.createElement("textarea") : document.createElement("input");
  field.className = "inline-editor-field";
  field.value = element.dataset.inlineValue || (originalValue === placeholder ? "" : originalValue);
  field.setAttribute("aria-label", label);
  if (!isMultiline) {
    field.type = inputType;
  } else {
    field.rows = Math.max(3, Math.min(8, (field.value.match(/\n/g) || []).length + 2));
  }

  const actions = document.createElement("div");
  actions.className = "inline-editor-actions";

  const saveButton = document.createElement("button");
  saveButton.type = "button";
  saveButton.className = "button inline-editor-save";
  saveButton.textContent = "Save";

  const cancelButton = document.createElement("button");
  cancelButton.type = "button";
  cancelButton.className = "button secondary inline-editor-cancel";
  cancelButton.textContent = "Cancel";

  const errorMessage = document.createElement("p");
  errorMessage.className = "inline-editor-error";
  errorMessage.hidden = true;

  actions.append(saveButton, cancelButton);
  wrapper.append(field, actions, errorMessage);

  element.dataset.originalHtml = element.innerHTML;
  element.innerHTML = "";
  element.appendChild(wrapper);
  field.focus();
  if (typeof field.select === "function" && inputType !== "date") {
    field.select();
  }

  function restore(displayValue, rawValue = null) {
    element.textContent = displayValue || placeholder;
    if (rawValue !== null) {
      element.dataset.inlineValue = rawValue;
    }
    element.dataset.editing = "false";
    element.classList.remove("is-editing");
  }

  async function save() {
    const body = new URLSearchParams();
    body.set(element.dataset.inlineField || "value", field.value);

    saveButton.disabled = true;
    cancelButton.disabled = true;
    errorMessage.hidden = true;
    errorMessage.textContent = "";

    try {
      const response = await fetch(element.dataset.inlineUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-CSRFToken": getCsrfToken(),
          "X-Requested-With": "XMLHttpRequest",
        },
        body: body.toString(),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error((payload.errors || ["Unable to save this change."]).join(" "));
      }
      restore(payload.display_value || payload.value || placeholder, payload.raw_value || payload.value || "");
    } catch (error) {
      errorMessage.textContent = error.message || "Unable to save this change.";
      errorMessage.hidden = false;
      saveButton.disabled = false;
      cancelButton.disabled = false;
      field.focus();
    }
  }

  function cancel() {
    restore(element.dataset.originalValue || placeholder, element.dataset.originalInlineValue || "");
  }

  wrapper.addEventListener("click", (event) => {
    event.stopPropagation();
  });

  saveButton.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    void save();
  });
  cancelButton.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    cancel();
  });
  field.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
    if (!isMultiline && event.key === "Enter") {
      event.preventDefault();
      void save();
    }
    if (isMultiline && event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void save();
    }
  });
}

function initializeCourseDetail(root = document) {
  ensureBlockActionMenuDocumentBindings(document);
  closeBlockActionMenus(root);

  root.querySelectorAll("form[data-upload-form]").forEach((form) => {
    const fileInput = form.querySelector("[data-upload-input]");
    const fileName = form.querySelector("[data-upload-file-name]");
    const submitButton = form.querySelector("button[type='submit']");
    const submitText = form.querySelector("[data-upload-submit-text]");

    function syncFileName() {
      if (!fileName || !fileInput) {
        return;
      }
      const selectedFile = fileInput.files && fileInput.files.length ? fileInput.files[0].name : "";
      fileName.textContent = selectedFile || "No file selected";
    }

    syncFileName();
    fileInput?.addEventListener("change", syncFileName);

    form.addEventListener("submit", () => {
      form.classList.add("is-submitting");
      if (submitButton) {
        submitButton.disabled = true;
      }
      if (submitText) {
        submitText.textContent = "Uploading...";
      }
    });
  });

  root.querySelectorAll("[data-block-toggle]").forEach((toggle) => {
    const targetId = toggle.dataset.blockTarget;
    const target = targetId ? document.getElementById(targetId) : null;
    if (!target) {
      return;
    }

    function shouldIgnoreToggle(eventTarget) {
      if (eventTarget.closest("[data-block-menu]")) {
        return true;
      }
      const inlineEditable = eventTarget.closest("[data-inline-edit]");
      if (inlineEditable) {
        const blockCard = inlineEditable.closest("[data-block-card]");
        const isCollapsedTitle =
          inlineEditable.classList.contains("inline-editable-title") &&
          blockCard === toggle.closest("[data-block-card]") &&
          toggle.getAttribute("aria-expanded") !== "true";
        if (!isCollapsedTitle) {
          return true;
        }
      }

      return Boolean(
        eventTarget.closest("a") ||
        eventTarget.closest("button") ||
        eventTarget.closest("input") ||
        eventTarget.closest("textarea") ||
        eventTarget.closest("form"),
      );
    }

    toggle.addEventListener("click", (event) => {
      if (shouldIgnoreToggle(event.target)) {
        return;
      }
      setBlockExpanded(toggle, target, toggle.getAttribute("aria-expanded") !== "true");
    });

    toggle.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") {
        return;
      }
      if (shouldIgnoreToggle(event.target)) {
        return;
      }
      event.preventDefault();
      setBlockExpanded(toggle, target, toggle.getAttribute("aria-expanded") !== "true");
    });
  });

  root.querySelectorAll("[data-block-menu]").forEach((menu) => {
    const trigger = menu.querySelector("[data-block-menu-trigger]");
    const panel = menu.querySelector("[data-block-menu-panel]");
    if (!trigger || !panel) {
      return;
    }

    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const isOpen = trigger.getAttribute("aria-expanded") === "true";
      const blockCard = menu.closest("[data-block-card]");
      closeBlockActionMenus(document);
      trigger.setAttribute("aria-expanded", isOpen ? "false" : "true");
      panel.hidden = isOpen;
      blockCard?.classList.toggle("is-menu-open", !isOpen);
    });

    panel.addEventListener("click", (event) => {
      event.stopPropagation();
    });
  });

  root.querySelectorAll("[data-inline-edit]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (element.dataset.editing === "true" || event.target !== element) {
        return;
      }
      const blockCard = element.closest("[data-block-card]");
      if (blockCard && element.classList.contains("inline-editable-title") && !blockCard.classList.contains("is-expanded")) {
        return;
      }
      event.stopPropagation();
      createEditor(element);
    });
    element.addEventListener("keydown", (event) => {
      if (element.dataset.editing === "true" || event.target !== element) {
        return;
      }
      if (event.key === "Enter" || event.key === " ") {
        const blockCard = element.closest("[data-block-card]");
        if (blockCard && element.classList.contains("inline-editable-title") && !blockCard.classList.contains("is-expanded")) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        createEditor(element);
      }
    });
  });

  root.querySelectorAll("form[data-async-refresh]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      event.stopPropagation();
      void submitAsyncRefreshForm(form);
    });
  });

  root.querySelectorAll("[data-open-block-target]").forEach((link) => {
    link.addEventListener("click", (event) => {
      const targetId = link.dataset.openBlockTarget;
      if (!targetId) {
        return;
      }
      event.preventDefault();
      openBlockTarget(targetId);
      window.history.replaceState({}, "", `#${targetId}`);
    });
  });

  root.querySelectorAll("[data-course-config-input]").forEach((input) => {
    let saveHandle = null;
    const scheduleSave = () => {
      if (saveHandle) {
        window.clearTimeout(saveHandle);
      }
      saveHandle = window.setTimeout(() => {
        void saveCourseConfigInput(input);
      }, 500);
    };

    if (input.type === "checkbox" || input.tagName === "SELECT") {
      input.addEventListener("change", () => {
        if (input.name === "demo_enabled") {
          syncDemoSettingsVisibility(root);
        }
        void saveCourseConfigInput(input);
      });
      return;
    }

    input.addEventListener("input", () => {
      scheduleSave();
    });
    input.addEventListener("change", () => {
      if (saveHandle) {
        window.clearTimeout(saveHandle);
        saveHandle = null;
      }
      void saveCourseConfigInput(input);
    });
    input.addEventListener("blur", () => {
      if (saveHandle) {
        window.clearTimeout(saveHandle);
        saveHandle = null;
      }
      void saveCourseConfigInput(input);
    });
  });

  root.querySelectorAll("[data-block-config-input]").forEach((input) => {
    let saveHandle = null;
    const scheduleSave = () => {
      if (saveHandle) {
        window.clearTimeout(saveHandle);
      }
      saveHandle = window.setTimeout(() => {
        void saveBlockConfigInput(input);
      }, 500);
    };

    input.addEventListener("input", () => {
      scheduleSave();
    });
    input.addEventListener("change", () => {
      if (saveHandle) {
        window.clearTimeout(saveHandle);
        saveHandle = null;
      }
      void saveBlockConfigInput(input);
    });
    input.addEventListener("blur", () => {
      if (saveHandle) {
        window.clearTimeout(saveHandle);
        saveHandle = null;
      }
      void saveBlockConfigInput(input);
    });
  });

  schedulePendingAssetRefresh(root);
  syncDemoSettingsVisibility(root);
}

document.addEventListener("DOMContentLoaded", () => {
  initializeCourseDetail(document);
  expandBlockPathFromHash(document);
  ensureBackgroundJobStatusPolling();
  void refreshBackgroundJobStatusNow();
  if (hasPendingCourseDetailIndicators(document)) {
    void syncPendingCourseDetailStateNow();
  }
});

window.addEventListener("focus", () => {
  void refreshBackgroundJobStatusNow();
  if (hasPendingCourseDetailIndicators(document)) {
    void syncPendingCourseDetailStateNow();
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    void refreshBackgroundJobStatusNow();
  }
  if (!document.hidden && hasPendingCourseDetailIndicators(document)) {
    void syncPendingCourseDetailStateNow();
  }
});
