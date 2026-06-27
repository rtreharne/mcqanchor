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
    } catch (_error) {
      label.textContent = originalLabel;
    } finally {
      button.classList.remove("is-pending");
      button.disabled = false;
    }
  });
});
