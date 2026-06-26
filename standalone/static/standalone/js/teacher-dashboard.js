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
