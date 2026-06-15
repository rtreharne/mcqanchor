function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function setBlockExpanded(toggle, target, expanded) {
  toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  target.hidden = !expanded;
  toggle.closest("[data-block-card]")?.classList.toggle("is-expanded", expanded);
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
  const placeholder = element.dataset.inlinePlaceholder || "";
  const isMultiline = element.dataset.inlineMultiline === "true";
  const label = element.dataset.inlineLabel || "Value";

  element.dataset.editing = "true";
  element.dataset.originalValue = originalValue;
  element.classList.add("is-editing");

  const wrapper = document.createElement("div");
  wrapper.className = "inline-editor";

  const field = isMultiline ? document.createElement("textarea") : document.createElement("input");
  field.className = "inline-editor-field";
  field.value = originalValue === placeholder ? "" : originalValue;
  field.setAttribute("aria-label", label);
  if (!isMultiline) {
    field.type = "text";
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
  field.select();

  function restore(value) {
    element.textContent = value || placeholder;
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
      restore(payload.display_value || payload.value || placeholder);
    } catch (error) {
      errorMessage.textContent = error.message || "Unable to save this change.";
      errorMessage.hidden = false;
      saveButton.disabled = false;
      cancelButton.disabled = false;
      field.focus();
    }
  }

  function cancel() {
    restore(element.dataset.originalValue || placeholder);
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
      return Boolean(
        eventTarget.closest("[data-inline-edit]") ||
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

  schedulePendingAssetRefresh(root);
}

document.addEventListener("DOMContentLoaded", () => {
  initializeCourseDetail(document);
  expandBlockPathFromHash(document);
  if (hasPendingCourseDetailIndicators(document)) {
    void syncPendingCourseDetailStateNow();
  }
});

window.addEventListener("focus", () => {
  if (hasPendingCourseDetailIndicators(document)) {
    void syncPendingCourseDetailStateNow();
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && hasPendingCourseDetailIndicators(document)) {
    void syncPendingCourseDetailStateNow();
  }
});
