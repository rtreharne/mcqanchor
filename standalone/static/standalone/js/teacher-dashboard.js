function setDashboardSectionExpanded(toggle, expanded) {
  const targetId = toggle.dataset.blockTarget;
  const target = targetId ? document.getElementById(targetId) : null;
  if (!target) {
    return;
  }
  toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  target.hidden = !expanded;
  toggle.closest("[data-block-card]")?.classList.toggle("is-expanded", expanded);
}

function toggleDashboardSection(toggle) {
  const expanded = toggle.getAttribute("aria-expanded") === "true";
  setDashboardSectionExpanded(toggle, !expanded);
}

document.querySelectorAll("[data-block-toggle]").forEach((toggle) => {
  setDashboardSectionExpanded(toggle, false);

  toggle.addEventListener("click", () => {
    toggleDashboardSection(toggle);
  });

  toggle.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    toggleDashboardSection(toggle);
  });
});

const BACKGROUND_JOB_STATUS_CLASSES = ["is-ready", "is-completed", "is-paused", "is-uploaded", "is-analyzing", "is-creating", "is-queued", "is-running", "is-failed"];
let backgroundJobStatusPollHandle = null;
let backgroundJobStatusSyncInFlight = false;

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

document.querySelectorAll("[data-builder-toggle-form]").forEach((form) => {
  const button = form.querySelector("[data-builder-toggle-button]");
  const label = form.querySelector("[data-builder-toggle-label]");
  if (!(button instanceof HTMLButtonElement) || !(label instanceof HTMLElement)) {
    return;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.classList.contains("is-pending")) {
      return;
    }

    const originalLabel = label.textContent || "";
    button.classList.add("is-pending");
    button.disabled = true;

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: {
          Accept: "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        credentials: "same-origin",
      });

      if (!response.ok) {
        throw new Error("Request failed.");
      }

      const payload = await response.json();
      if (!payload.ok) {
        throw new Error("Request failed.");
      }

      form.action = String(payload.action_url || form.action);
      button.setAttribute("aria-checked", payload.enabled ? "true" : "false");
      button.title = String(payload.title || "");
      label.textContent = String(payload.label || originalLabel);
      void refreshBackgroundJobStatusNow();
    } catch (_error) {
      label.textContent = originalLabel;
    } finally {
      button.classList.remove("is-pending");
      button.disabled = false;
    }
  });
});

document.addEventListener("DOMContentLoaded", () => {
  ensureBackgroundJobStatusPolling();
  void refreshBackgroundJobStatusNow();
});

window.addEventListener("focus", () => {
  void refreshBackgroundJobStatusNow();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    void refreshBackgroundJobStatusNow();
  }
});
