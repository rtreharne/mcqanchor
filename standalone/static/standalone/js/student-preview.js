function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

const previewRoot = document.querySelector("[data-student-preview]");
const previewDataNode = document.getElementById("student-preview-data");

if (previewRoot && previewDataNode) {
  const actionUrlTemplate = previewRoot.dataset.actionUrlTemplate || "";
  const blockSwitcher = previewRoot.querySelector(".preview-block-switcher");
  const transcript = previewRoot.querySelector(".preview-chat-transcript");
  const form = previewRoot.querySelector(".preview-chat-form");
  const input = previewRoot.querySelector("#preview-chat-input");
  const statusText = previewRoot.querySelector(".preview-chat-status");
  const quizControls = previewRoot.querySelector(".preview-quiz-controls");
  const sidebarToggle = previewRoot.querySelector("[data-preview-sidebar-toggle]");
  const sidebarScrim = previewRoot.querySelector("[data-preview-sidebar-scrim]");
  const previewSidebar = previewRoot.querySelector(".preview-sidebar");
  const courseMetricsPanel = previewRoot.querySelector("[data-preview-course-metrics]");
  const sidebarSummary = previewRoot.querySelector("[data-preview-sidebar-summary]");
  const sidebarSummaryText = previewRoot.querySelector("[data-preview-sidebar-summary-text]");
  const sidebarSummaryCopy = previewRoot.querySelector("[data-preview-sidebar-summary-copy]");
  const sidebarSummaryToggle = previewRoot.querySelector("[data-preview-sidebar-summary-toggle]");
  const launchLoader = previewRoot.querySelector("[data-preview-launch-loader]");
  const submitButton = form?.querySelector("button[type='submit']");
  const quizMenu = previewRoot.querySelector("[data-quiz-menu]");
  const quizMenuTrigger = previewRoot.querySelector("[data-quiz-menu-trigger]");
  const quizMenuPanel = previewRoot.querySelector("[data-quiz-menu-panel]");
  const waqAlignment = previewRoot.querySelector("[data-waq-alignment]");
  const waqAlignmentLabel = previewRoot.querySelector("[data-waq-alignment-label]");
  const waqAlignmentFill = previewRoot.querySelector("[data-waq-alignment-fill]");
  const waqAlignmentLoader = previewRoot.querySelector("[data-waq-alignment-loader]");
  const activeBlockTitle = previewRoot.querySelector(".preview-active-block-title");
  const projectSwitcher = previewRoot.querySelector("[data-preview-project-switcher]");
  const projectPanel = previewRoot.querySelector("[data-preview-project-panel]");
  const resourceButtons = Array.from(previewRoot.querySelectorAll("[data-preview-resource]"));
  const mobileSidebarMedia = window.matchMedia("(max-width: 980px)");
  const mobileChatMedia = window.matchMedia("(max-width: 640px)");
  const flagSheet = previewRoot.querySelector("[data-preview-flag-sheet]");
  const flagSheetScrim = previewRoot.querySelector("[data-preview-flag-sheet-scrim]");
  const flagSheetCloseButton = previewRoot.querySelector("[data-preview-flag-sheet-close]");
  const flagSheetQuestion = previewRoot.querySelector("[data-preview-flag-sheet-question]");
  const flagObjectiveField = previewRoot.querySelector("[data-preview-flag-objective-field]");
  const flagObjectiveSelect = previewRoot.querySelector("[data-preview-flag-objective-select]");
  const flagInstructionInput = previewRoot.querySelector("[data-preview-flag-instruction]");
  const flagSheetError = previewRoot.querySelector("[data-preview-flag-error]");
  const flagOnlyButton = previewRoot.querySelector("[data-preview-flag-only]");
  const flagSaveButton = previewRoot.querySelector("[data-preview-flag-save]");
  const objectiveSheet = previewRoot.querySelector("[data-preview-objective-sheet]");
  const objectiveSheetScrim = previewRoot.querySelector("[data-preview-objective-sheet-scrim]");
  const objectiveSheetCloseButton = previewRoot.querySelector("[data-preview-objective-sheet-close]");
  const objectiveSheetObjective = previewRoot.querySelector("[data-preview-objective-sheet-objective]");
  const objectiveSheetExistingWrap = previewRoot.querySelector("[data-preview-objective-sheet-existing-wrap]");
  const objectiveSheetExisting = previewRoot.querySelector("[data-preview-objective-sheet-existing]");
  const objectiveGuardrailInput = previewRoot.querySelector("[data-preview-objective-guardrail]");
  const objectiveSheetError = previewRoot.querySelector("[data-preview-objective-sheet-error]");
  const objectiveSheetSaveButton = previewRoot.querySelector("[data-preview-objective-sheet-save]");
  const isTeacherPreview = previewRoot.dataset.previewMode === "student-preview";
  const isDemoMode = previewRoot.dataset.demoMode === "true";
  const hideFlagActions = previewRoot.dataset.hideFlagActions === "true";

  let previewState = JSON.parse(previewDataNode.textContent || "{}");
  let activeBlockId = String(previewState.active_block_id || "");
  let requestInFlight = false;
  let sidebarOpen = true;
  let inlineMessageSequence = 0;
  let sidebarAutoCloseTimer = 0;
  let highlightedSidebarBlockId = "";
  let highlightedSidebarBlockUntil = 0;
  let waqDraftDebounceTimer = 0;
  let waqDraftRequestId = 0;
  let waqAlignmentLoadingRequestId = 0;
  let waqDraftAbortController = null;
  let sidebarSummaryExpanded = false;
  let sidebarSummaryFullText = "";
  let practiceValidationNavigationTimer = 0;
  let flagSheetState = null;
  let guardrailSheetState = null;
  const inlineMessagesByBlock = {};
  const loadingMessagesByBlock = {};
  const optimisticUserMessagesByBlock = {};
  const activeProjectIdsByBlock = {};
  const projectAnswerDraftsById = {};
  const maqSelectionsByQuestionId = {};
  const sidebarSelectionPreviewMs = 2000;
  const practiceValidationLaunchDelayMs = 5000;
  const practiceValidationMobileSidebarDelayMs = 500;
  const previewDateFormatter = new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  const reducedMotionMedia = window.matchMedia("(prefers-reduced-motion: reduce)");
  const richText = window.StandaloneRichText || {
    appendFormattedMessageContent(container, text) {
      if (!container) {
        return;
      }
      const paragraph = document.createElement("p");
      paragraph.textContent = String(text || "");
      container.appendChild(paragraph);
    },
    appendInlineText(target, text) {
      if (target) {
        target.textContent = String(text || "");
      }
    },
    buildTextPanel(headingText, bodyText, extraClass = "") {
      const panel = document.createElement("div");
      panel.className = `preview-written-answer-panel${extraClass ? ` ${extraClass}` : ""}`;
      const heading = document.createElement("span");
      heading.className = "preview-written-answer-heading";
      heading.textContent = headingText;
      const paragraph = document.createElement("p");
      paragraph.textContent = String(bodyText || "");
      panel.append(heading, paragraph);
      return panel;
    },
    renderMath() {},
  };

  function activeBlockStorageKey() {
    const courseId = String(previewState?.course?.id || previewRoot.dataset.courseId || "");
    const mode = previewRoot.dataset.previewMode || "preview";
    return courseId ? `quizanchor:${mode}:course:${courseId}:active-block` : "";
  }

  function persistActiveBlockId(blockId) {
    const storageKey = activeBlockStorageKey();
    if (!storageKey) {
      return;
    }
    try {
      if (blockId) {
        window.localStorage.setItem(storageKey, String(blockId));
      } else {
        window.localStorage.removeItem(storageKey);
      }
    } catch (error) {
      return;
    }
  }

  function restoreActiveBlockId() {
    const storageKey = activeBlockStorageKey();
    if (!storageKey) {
      return;
    }
    try {
      const storedBlockId = window.localStorage.getItem(storageKey);
      if (!storedBlockId) {
        return;
      }
      const matchingBlock = (previewState.blocks || []).find((block) => String(block.id) === String(storedBlockId));
      if (matchingBlock) {
        activeBlockId = String(matchingBlock.id);
        return;
      }
      window.localStorage.removeItem(storageKey);
    } catch (error) {
      return;
    }
  }

  function showLaunchLoader() {
    if (launchLoader) {
      launchLoader.hidden = false;
    }
  }

  function demoValidationVisitorStorageKey() {
    const courseKey = String(previewRoot.dataset.demoCourseKey || previewState?.course?.id || "");
    return courseKey ? `quizanchor:demo-validation-visitor:${courseKey}` : "";
  }

  function demoValidationVisitorKey() {
    const storageKey = demoValidationVisitorStorageKey();
    if (!storageKey) {
      return "";
    }
    try {
      let value = window.localStorage.getItem(storageKey) || "";
      if (!value) {
        value = window.crypto?.randomUUID ? window.crypto.randomUUID().replace(/-/g, "") : `${Date.now()}${Math.random().toString(16).slice(2)}`;
        window.localStorage.setItem(storageKey, value);
      }
      return value;
    } catch (_error) {
      return "";
    }
  }

  function demoValidationUrl(url) {
    if (!isDemoMode || !url) {
      return url;
    }
    try {
      const nextUrl = new URL(url, window.location.origin);
      if (!nextUrl.pathname.includes("/validation-practice/")) {
        return nextUrl.toString();
      }
      if (!nextUrl.searchParams.get("visitor")) {
        const visitorKey = demoValidationVisitorKey();
        if (visitorKey) {
          nextUrl.searchParams.set("visitor", visitorKey);
        }
      }
      return nextUrl.toString();
    } catch (_error) {
      return url;
    }
  }

  function beginPracticeValidationLaunch(link) {
    if (!link || practiceValidationNavigationTimer) {
      return;
    }
    showLaunchLoader();
    if (isMobileSidebar()) {
      window.setTimeout(() => {
        setSidebarOpen(false);
      }, practiceValidationMobileSidebarDelayMs);
    }
    practiceValidationNavigationTimer = window.setTimeout(() => {
      window.location.assign(demoValidationUrl(link.href));
    }, practiceValidationLaunchDelayMs);
  }

  function isPracticeValidationLaunchLink(link) {
    if (!link || !link.href) {
      return false;
    }
    try {
      const url = new URL(link.href, window.location.origin);
      const path = String(url.pathname || "");
      const isPracticeValidationPath = path.includes("/validation-practice/") || path.includes("/student-preview/validate/practice/");
      return isPracticeValidationPath && !url.searchParams.has("review");
    } catch (_error) {
      return false;
    }
  }

  function actionUrl(blockId, action) {
    return actionUrlTemplate.replace("/0/ACTION/", `/${blockId}/${action}/`);
  }

  function truncateSidebarSummary(text, limit = 100) {
    const normalized = String(text || "").trim();
    if (normalized.length <= limit) {
      return normalized;
    }
    const truncated = normalized.slice(0, limit);
    const lastSpace = truncated.lastIndexOf(" ");
    return (lastSpace > 72 ? truncated.slice(0, lastSpace) : truncated).trimEnd();
  }

  function renderSidebarSummary() {
    if (!sidebarSummaryText || !sidebarSummaryCopy || !sidebarSummaryToggle) {
      return;
    }
    const excerpt = truncateSidebarSummary(sidebarSummaryFullText);
    const isTruncated = excerpt.length < sidebarSummaryFullText.length;
    sidebarSummaryCopy.textContent = sidebarSummaryExpanded || !isTruncated
      ? sidebarSummaryFullText
      : excerpt;
    sidebarSummaryToggle.hidden = !isTruncated;
    sidebarSummaryToggle.textContent = sidebarSummaryExpanded ? "... less" : "... more";
    sidebarSummaryToggle.setAttribute("aria-expanded", sidebarSummaryExpanded ? "true" : "false");
    const isExpanded = sidebarSummaryExpanded && isTruncated;
    sidebarSummary?.classList.toggle("is-expanded", isExpanded);
    previewSidebar?.classList.toggle("has-expanded-summary", isExpanded);
  }

  function currentBlock() {
    return (previewState.blocks || []).find((block) => String(block.id) === String(activeBlockId)) || previewState.blocks?.[0] || null;
  }

  function findBlock(blockId) {
    return (previewState.blocks || []).find((block) => String(block.id) === String(blockId)) || null;
  }

  function currentProject(block = currentBlock()) {
    if (!block || !Array.isArray(block.projects)) {
      return null;
    }
    const activeProjectId = String(activeProjectIdsByBlock[String(block.id)] || "");
    return block.projects.find((project) => String(project.id) === activeProjectId) || null;
  }

  function setActiveProject(blockId, projectId = "") {
    const key = String(blockId || "");
    if (!key) {
      return;
    }
    if (projectId) {
      activeProjectIdsByBlock[key] = String(projectId);
      return;
    }
    delete activeProjectIdsByBlock[key];
  }

  function currentFlagSheetBlock() {
    return flagSheetState ? findBlock(flagSheetState.blockId) : null;
  }

  function currentGuardrailSheetBlock() {
    return guardrailSheetState ? findBlock(guardrailSheetState.blockId) : null;
  }

  function currentObjectiveForGuardrailSheet() {
    const block = currentGuardrailSheetBlock();
    if (!guardrailSheetState || !block || !Array.isArray(block.learning_objectives)) {
      return null;
    }
    return block.learning_objectives.find(
      (objective) => Number(objective.id || 0) === Number(guardrailSheetState.learningObjectiveId || 0),
    ) || null;
  }

  function setFlagSheetError(message = "") {
    if (!flagSheetError) {
      return;
    }
    flagSheetError.textContent = message;
    flagSheetError.hidden = !message;
  }

  function setGuardrailSheetError(message = "") {
    if (!objectiveSheetError) {
      return;
    }
    objectiveSheetError.textContent = message;
    objectiveSheetError.hidden = !message;
  }

  function closeFlagSheet() {
    flagSheetState = null;
    setFlagSheetError("");
    if (flagInstructionInput) {
      flagInstructionInput.value = "";
    }
    if (flagObjectiveSelect) {
      flagObjectiveSelect.innerHTML = "";
      flagObjectiveSelect.value = "";
    }
    syncFlagSheetState();
  }

  function closeGuardrailSheet() {
    guardrailSheetState = null;
    setGuardrailSheetError("");
    if (objectiveGuardrailInput) {
      objectiveGuardrailInput.value = "";
    }
    if (objectiveSheetExistingWrap) {
      objectiveSheetExistingWrap.hidden = true;
    }
    if (objectiveSheetExisting) {
      objectiveSheetExisting.textContent = "";
    }
    syncGuardrailSheetState();
  }

  function syncFlagSheetState() {
    if (!flagSheet || !isTeacherPreview) {
      return;
    }
    const isOpen = Boolean(flagSheetState);
    flagSheet.hidden = !isOpen;
    flagSheetScrim.hidden = !isOpen;
    flagSheet.classList.toggle("is-open", isOpen);
    if (!isOpen) {
      return;
    }
    if (flagOnlyButton) {
      flagOnlyButton.disabled = requestInFlight;
    }
    if (flagSaveButton) {
      flagSaveButton.disabled = requestInFlight;
    }
    if (flagInstructionInput) {
      flagInstructionInput.disabled = requestInFlight;
    }
    if (flagObjectiveSelect) {
      flagObjectiveSelect.disabled = requestInFlight;
    }
  }

  function syncGuardrailSheetState() {
    if (!objectiveSheet || !isTeacherPreview) {
      return;
    }
    const isOpen = Boolean(guardrailSheetState);
    objectiveSheet.hidden = !isOpen;
    objectiveSheetScrim.hidden = !isOpen;
    objectiveSheet.classList.toggle("is-open", isOpen);
    if (!isOpen) {
      return;
    }
    if (objectiveSheetSaveButton) {
      objectiveSheetSaveButton.disabled = requestInFlight;
    }
    if (objectiveGuardrailInput) {
      objectiveGuardrailInput.disabled = requestInFlight;
    }
  }

  function openFlagSheet(message) {
    if (!isTeacherPreview || !message) {
      return;
    }
    const block = currentBlock();
    if (!block) {
      return;
    }
    flagSheetState = {
      blockId: String(block.id),
      questionId: Number(message.question_id || 0),
      learningObjectiveId: Number(message.learning_objective_id || 0) || null,
    };
    if (flagSheetQuestion) {
      flagSheetQuestion.textContent = questionStemText(message);
    }
    if (flagInstructionInput) {
      flagInstructionInput.value = "";
    }
    setFlagSheetError("");
    if (flagObjectiveSelect) {
      flagObjectiveSelect.innerHTML = "";
      const objectives = Array.isArray(block.learning_objectives) ? block.learning_objectives : [];
      if (flagSheetState.learningObjectiveId) {
        if (flagObjectiveField) {
          flagObjectiveField.hidden = true;
        }
      } else {
        if (flagObjectiveField) {
          flagObjectiveField.hidden = false;
        }
        const placeholderOption = document.createElement("option");
        placeholderOption.value = "";
        placeholderOption.textContent = "Choose a learning objective";
        flagObjectiveSelect.appendChild(placeholderOption);
        objectives.forEach((objective) => {
          const option = document.createElement("option");
          option.value = String(objective.id);
          option.textContent = `${objective.code} ${objective.text}`;
          flagObjectiveSelect.appendChild(option);
        });
      }
    }
    syncFlagSheetState();
    flagInstructionInput?.focus();
  }

  function openGuardrailSheet(objective) {
    if (!isTeacherPreview || !objective) {
      return;
    }
    const block = currentBlock();
    if (!block) {
      return;
    }
    guardrailSheetState = {
      blockId: String(block.id),
      learningObjectiveId: Number(objective.id || 0),
    };
    if (objectiveSheetObjective) {
      objectiveSheetObjective.textContent = `${objective.code} ${objective.text}`;
    }
    const currentGuidance = String(objective.assistant_guidance || "").trim();
    if (objectiveSheetExistingWrap) {
      objectiveSheetExistingWrap.hidden = !currentGuidance;
    }
    if (objectiveSheetExisting) {
      objectiveSheetExisting.textContent = currentGuidance;
    }
    if (objectiveGuardrailInput) {
      objectiveGuardrailInput.value = "";
    }
    setGuardrailSheetError("");
    syncGuardrailSheetState();
    objectiveGuardrailInput?.focus();
  }

  async function submitFlagSheet({ saveCorrection }) {
    if (!flagSheetState || requestInFlight) {
      return;
    }
    const payload = { question_id: flagSheetState.questionId };
    if (saveCorrection) {
      const instruction = String(flagInstructionInput?.value || "").trim();
      const learningObjectiveId = flagSheetState.learningObjectiveId || Number(flagObjectiveSelect?.value || 0) || null;
      if (!instruction) {
        setFlagSheetError("Add a correction note before saving it.");
        flagInstructionInput?.focus();
        return;
      }
      if (!learningObjectiveId) {
        setFlagSheetError("Choose the learning objective this correction belongs to.");
        flagObjectiveSelect?.focus();
        return;
      }
      payload.instruction = instruction;
      payload.learning_objective_id = learningObjectiveId;
    }
    setFlagSheetError("");
    const succeeded = await postPreviewAction("flag", payload, {
      focusComposer: true,
      scrollMode: "preserve",
      onError: (error) => {
        setFlagSheetError(error?.message || "Unable to save this correction right now.");
      },
    });
    if (succeeded) {
      closeFlagSheet();
    }
  }

  async function submitGuardrailSheet() {
    if (!guardrailSheetState || requestInFlight) {
      return;
    }
    const instruction = String(objectiveGuardrailInput?.value || "").trim();
    if (!instruction) {
      setGuardrailSheetError("Add a guardrail before saving it.");
      objectiveGuardrailInput?.focus();
      return;
    }
    setGuardrailSheetError("");
    const succeeded = await postPreviewAction(
      "guardrail",
      {
        learning_objective_id: guardrailSheetState.learningObjectiveId,
        instruction,
      },
      {
        focusComposer: true,
        scrollMode: "preserve",
        onError: (error) => {
          setGuardrailSheetError(error?.message || "Unable to save this guardrail right now.");
        },
      },
    );
    if (succeeded) {
      closeGuardrailSheet();
    }
  }

  function pendingQuestion(block = currentBlock()) {
    if (currentProject(block)) {
      return null;
    }
    if (!block || !Array.isArray(block.transcript)) {
      return null;
    }
    return [...block.transcript].reverse().find(
      (message) => message.kind === "question" && !message.answered && !message.flagged,
    ) || null;
  }

  function pendingWrittenQuestion(block = currentBlock()) {
    const question = pendingQuestion(block);
    return question?.question_type === "waq" ? question : null;
  }

  function isAdvancedQuestionType(questionType) {
    return questionType === "maq" || questionType === "waq";
  }

  function advancedQuestionUnlockText(block = currentBlock()) {
    const metrics = block?.metrics || {};
    const threshold = Number(metrics.advanced_question_start_percent || 0);
    const completed = Number(metrics.completed_count || 0);
    const target = Number(metrics.target_question_count || 0);
    if (!threshold || !target) {
      return "Locked";
    }
    return `Unlocks at ${threshold}% target (${completed}/${target})`;
  }

  function syncQuizMenuItems() {
    if (!quizMenuPanel) {
      return;
    }
    const block = currentBlock();
    const unlocked = block?.metrics?.advanced_question_types_unlocked !== false;
    quizMenuPanel.querySelectorAll("[data-quiz-type]").forEach((button) => {
      const questionType = button.dataset.quizType || "";
      const locked = isAdvancedQuestionType(questionType) && !unlocked;
      const copy = button.querySelector(".preview-quiz-menu-item-copy");
      if (copy && !copy.dataset.defaultText) {
        copy.dataset.defaultText = copy.textContent || "";
      }
      button.disabled = requestInFlight || locked;
      button.setAttribute("aria-disabled", button.disabled ? "true" : "false");
      button.classList.toggle("is-locked", locked);
      if (copy) {
        copy.textContent = locked ? advancedQuestionUnlockText(block) : copy.dataset.defaultText;
      }
    });
  }

  function updateQuestionMessage(questionId, updater) {
    if (!questionId || typeof updater !== "function") {
      return null;
    }
    let updatedQuestion = null;
    (previewState.blocks || []).forEach((block) => {
      (block.transcript || []).forEach((message) => {
        if (message.kind === "question" && String(message.question_id) === String(questionId)) {
          updater(message, block);
          updatedQuestion = message;
        }
      });
    });
    return updatedQuestion;
  }

  function blockInlineMessages(blockId) {
    const key = String(blockId);
    if (!Array.isArray(inlineMessagesByBlock[key])) {
      inlineMessagesByBlock[key] = [];
    }
    return inlineMessagesByBlock[key];
  }

  function setQuizLoading(blockId, isLoading) {
    const key = String(blockId);
    if (isLoading) {
      loadingMessagesByBlock[key] = true;
      return;
    }
    delete loadingMessagesByBlock[key];
  }

  function setOptimisticUserMessage(blockId, text = "") {
    const key = String(blockId);
    if (!text) {
      delete optimisticUserMessagesByBlock[key];
      return;
    }
    optimisticUserMessagesByBlock[key] = {
      id: `optimistic-user-${key}`,
      kind: "text",
      role: "user",
      text,
    };
  }

  function maqSelection(questionId) {
    return Array.isArray(maqSelectionsByQuestionId[String(questionId)]) ? maqSelectionsByQuestionId[String(questionId)] : [];
  }

  function setMaqSelection(questionId, selections) {
    const key = String(questionId);
    const normalized = [];
    (selections || []).forEach((selection) => {
      const cleaned = String(selection).trim();
      if (cleaned && !normalized.includes(cleaned)) {
        normalized.push(cleaned);
      }
    });
    if (!normalized.length) {
      delete maqSelectionsByQuestionId[key];
      return;
    }
    maqSelectionsByQuestionId[key] = normalized;
  }

  function toggleMaqSelection(questionId, option) {
    const selections = maqSelection(questionId);
    if (selections.includes(option)) {
      setMaqSelection(
        questionId,
        selections.filter((selection) => selection !== option),
      );
      return;
    }
    setMaqSelection(questionId, [...selections, option]);
  }

  function syncRenderedMaqQuestion(questionId) {
    if (!transcript) {
      return;
    }
    const selections = maqSelection(questionId);
    transcript.querySelectorAll("[data-preview-question='true']").forEach((questionCard) => {
      if (String(questionCard.dataset.questionId || "") !== String(questionId)) {
        return;
      }
      questionCard.querySelectorAll("[data-maq-option-button='true']").forEach((optionButton) => {
        const option = optionButton.dataset.optionValue || "";
        const isSelected = selections.includes(option);
        optionButton.classList.toggle("is-selected", isSelected);
        optionButton.setAttribute("aria-pressed", isSelected ? "true" : "false");
        const checkbox = optionButton.querySelector(".preview-answer-chip-checkbox");
        if (checkbox) {
          checkbox.textContent = isSelected ? "✓" : "";
        }
      });
      questionCard.querySelectorAll("[data-maq-submit-button='true']").forEach((submitButton) => {
        submitButton.dataset.hasSelection = selections.length ? "true" : "false";
        submitButton.disabled = requestInFlight || !selections.length;
      });
    });
  }

  function clearAnsweredQuestionSelections() {
    (previewState.blocks || []).forEach((block) => {
      (block.transcript || []).forEach((message) => {
        if (message.kind === "question" && message.answered) {
          delete maqSelectionsByQuestionId[String(message.question_id)];
        }
      });
    });
  }

  function setStatus(message) {
    if (statusText) {
      statusText.textContent = message || "";
    }
  }

  function isMobileSidebar() {
    return mobileSidebarMedia.matches;
  }

  function clearSidebarSelectionPreview() {
    highlightedSidebarBlockId = "";
    highlightedSidebarBlockUntil = 0;
    blockSwitcher?.querySelectorAll(".preview-block-card.is-selection-preview").forEach((card) => {
      card.classList.remove("is-selection-preview");
    });
  }

  function clearSidebarAutoCloseTimer(clearHighlight = false) {
    if (sidebarAutoCloseTimer) {
      window.clearTimeout(sidebarAutoCloseTimer);
      sidebarAutoCloseTimer = 0;
    }
    if (clearHighlight) {
      clearSidebarSelectionPreview();
    }
  }

  function isSidebarSelectionPreview(blockId) {
    return (
      highlightedSidebarBlockId === String(blockId)
      && highlightedSidebarBlockUntil > Date.now()
    );
  }

  function scheduleSidebarAutoClose(blockId) {
    clearSidebarAutoCloseTimer();
    highlightedSidebarBlockId = String(blockId);
    highlightedSidebarBlockUntil = Date.now() + sidebarSelectionPreviewMs;
    sidebarAutoCloseTimer = window.setTimeout(() => {
      clearSidebarAutoCloseTimer(true);
      setSidebarOpen(false);
    }, sidebarSelectionPreviewMs);
  }

  function applySidebarState() {
    previewRoot.classList.toggle("is-sidebar-collapsed", !sidebarOpen);
    if (sidebarToggle) {
      sidebarToggle.setAttribute("aria-expanded", String(sidebarOpen));
      sidebarToggle.setAttribute("aria-label", sidebarOpen ? "Hide preview sidebar" : "Show preview sidebar");
    }
    if (sidebarScrim) {
      sidebarScrim.hidden = !isMobileSidebar() || !sidebarOpen;
    }
  }

  function setSidebarOpen(nextOpen) {
    if (!nextOpen) {
      clearSidebarAutoCloseTimer(true);
    }
    sidebarOpen = !!nextOpen;
    applySidebarState();
  }

  if (sidebarSummaryCopy) {
    sidebarSummaryFullText = sidebarSummaryCopy.textContent.trim();
    renderSidebarSummary();
  }

  sidebarSummaryToggle?.addEventListener("click", () => {
    sidebarSummaryExpanded = !sidebarSummaryExpanded;
    renderSidebarSummary();
  });

  previewRoot.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("a") : null;
    if (!link || !(link instanceof HTMLAnchorElement)) {
      return;
    }
    if (event.defaultPrevented || link.target === "_blank" || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    if (!isPracticeValidationLaunchLink(link)) {
      return;
    }
    event.preventDefault();
    beginPracticeValidationLaunch(link);
  });

  function toggleSidebar() {
    setSidebarOpen(!sidebarOpen);
  }

  function closeQuizMenu() {
    if (!quizMenu || !quizMenuTrigger || !quizMenuPanel) {
      return;
    }
    quizMenu.dataset.open = "false";
    quizMenuTrigger.setAttribute("aria-expanded", "false");
    quizMenuPanel.setAttribute("hidden", "hidden");
    quizMenuPanel.hidden = true;
  }

  function openQuizMenu() {
    if (!quizMenu || !quizMenuTrigger || !quizMenuPanel || requestInFlight) {
      return;
    }
    syncQuizMenuItems();
    quizMenu.dataset.open = "true";
    quizMenuTrigger.setAttribute("aria-expanded", "true");
    quizMenuPanel.removeAttribute("hidden");
    quizMenuPanel.hidden = false;
  }

  function isQuizMenuOpen() {
    return !!quizMenuPanel && !quizMenuPanel.hidden;
  }

  function closeObjectiveMenus(exceptMenu = null) {
    previewRoot.querySelectorAll("[data-preview-objective-menu]").forEach((menu) => {
      if (exceptMenu && menu === exceptMenu) {
        return;
      }
      const trigger = menu.querySelector("[data-preview-objective-menu-trigger]");
      const panel = menu.querySelector("[data-preview-objective-menu-panel]");
      menu.dataset.open = "false";
      trigger?.setAttribute("aria-expanded", "false");
      if (panel) {
        panel.hidden = true;
        panel.setAttribute("hidden", "hidden");
      }
    });
  }

  function toggleObjectiveMenu(menu) {
    if (!menu || requestInFlight) {
      return;
    }
    const trigger = menu.querySelector("[data-preview-objective-menu-trigger]");
    const panel = menu.querySelector("[data-preview-objective-menu-panel]");
    if (!trigger || !panel) {
      return;
    }
    const willOpen = panel.hidden;
    closeObjectiveMenus(willOpen ? menu : null);
    if (!willOpen) {
      menu.dataset.open = "false";
      trigger.setAttribute("aria-expanded", "false");
      panel.hidden = true;
      panel.setAttribute("hidden", "hidden");
      return;
    }
    menu.dataset.open = "true";
    trigger.setAttribute("aria-expanded", "true");
    panel.hidden = false;
    panel.removeAttribute("hidden");
  }

  function resizeComposerInput() {
    if (!input) {
      return;
    }
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 96)}px`;
  }

  function clearWaqDraftTimer() {
    if (waqDraftDebounceTimer) {
      window.clearTimeout(waqDraftDebounceTimer);
      waqDraftDebounceTimer = 0;
    }
  }

  function setWaqAlignmentLoading(requestId) {
    waqAlignmentLoadingRequestId = requestId || 0;
    renderWaqAlignment();
  }

  function clearWaqAlignmentLoading(requestId = 0) {
    if (requestId && requestId !== waqAlignmentLoadingRequestId) {
      return;
    }
    waqAlignmentLoadingRequestId = 0;
    renderWaqAlignment();
  }

  function abortWaqDraftRequest({ clearLoading = true } = {}) {
    if (waqDraftAbortController) {
      waqDraftAbortController.abort();
      waqDraftAbortController = null;
    }
    if (clearLoading) {
      clearWaqAlignmentLoading();
    }
  }

  function setWaqAlignmentFlash(isFlashing) {
    if (!waqAlignment) {
      return;
    }
    waqAlignment.classList.toggle("is-flashing", !!isFlashing);
  }

  function renderWaqAlignment(question = pendingWrittenQuestion(), { flash = false } = {}) {
    if (!waqAlignment || !waqAlignmentLabel || !waqAlignmentFill) {
      return;
    }
    if (!question || question.answered || question.flagged) {
      waqAlignmentLoadingRequestId = 0;
      waqAlignment.hidden = true;
      waqAlignment.dataset.state = "drafting";
      waqAlignment.dataset.loading = "false";
      waqAlignmentFill.style.width = "0%";
      waqAlignmentLabel.textContent = "Start typing";
      if (waqAlignmentLoader) {
        waqAlignmentLoader.hidden = true;
      }
      setWaqAlignmentFlash(false);
      return;
    }

    const score = Number(question.alignment_score || 0);
    const state = question.alignment_state || "drafting";
    const isLoading = !!waqAlignmentLoadingRequestId && !!String(question.draft_answer || "").trim();
    waqAlignment.hidden = false;
    waqAlignment.dataset.state = state;
    waqAlignment.dataset.loading = isLoading ? "true" : "false";
    waqAlignmentFill.style.width = `${Math.max(0, Math.min(score, 100))}%`;
    if (waqAlignmentLoader) {
      waqAlignmentLoader.hidden = !isLoading;
    }
    if (!question.draft_answer && !question.submitted_text) {
      waqAlignmentLabel.textContent = "Start typing";
    } else if (state === "aligned") {
      waqAlignmentLabel.textContent = `Aligned ${formatPercentage(score)}`;
    } else if (state === "close") {
      waqAlignmentLabel.textContent = `${formatPercentage(score)} close`;
    } else {
      waqAlignmentLabel.textContent = `${formatPercentage(score)} building`;
    }
    if (flash) {
      setWaqAlignmentFlash(false);
      window.requestAnimationFrame(() => {
        setWaqAlignmentFlash(true);
        window.setTimeout(() => setWaqAlignmentFlash(false), 700);
      });
      return;
    }
    setWaqAlignmentFlash(false);
  }

  function syncComposerInputFromState() {
    if (!input) {
      return;
    }
    if (currentProject()) {
      if (input.dataset.mode === "waq") {
        input.value = "";
      }
      input.dataset.mode = "project-chat";
      resizeComposerInput();
      return;
    }
    const question = pendingWrittenQuestion();
    if (question) {
      const nextValue = question.draft_answer || "";
      if (input.value !== nextValue) {
        input.value = nextValue;
      }
      input.dataset.mode = "waq";
      resizeComposerInput();
      return;
    }

    if (input.dataset.mode === "waq") {
      input.value = "";
      resizeComposerInput();
    }
    input.dataset.mode = "chat";
  }

  function syncComposerState() {
    if (!submitButton || !input) {
      return;
    }
    const activeProject = currentProject();
    const activeWaq = pendingWrittenQuestion();
    const hasText = !!input.value.trim();
    const isWaqMode = !!activeWaq;
    const isProjectMode = !!activeProject;

    previewRoot.classList.toggle("is-waq-mode", isWaqMode);
    previewRoot.classList.toggle("is-project-mode", isProjectMode);
    form?.classList.toggle("is-waq-mode", isWaqMode);
    quizControls?.classList.toggle("is-answer-mode", isWaqMode);
    input.placeholder = isWaqMode
      ? "Write your answer..."
      : (isProjectMode ? "Ask for a hint or nudge..." : "Ask a related question.");
    submitButton.textContent = isWaqMode ? "Submit answer" : (hasText ? "Send" : (isProjectMode ? "Hint" : "Quiz"));
    submitButton.disabled = requestInFlight || (isWaqMode && !hasText);
    if (quizMenu) {
      quizMenu.hidden = hasText || isWaqMode || isProjectMode;
    }
    if (quizMenuTrigger) {
      quizMenuTrigger.disabled = requestInFlight || !!input.value.trim() || isWaqMode || isProjectMode;
    }
    syncQuizMenuItems();
    renderWaqAlignment(activeWaq);
  }

  function setComposerDisabled(disabled) {
    requestInFlight = disabled;
    if (input) {
      input.disabled = disabled;
    }
    if (submitButton) {
      submitButton.disabled = disabled;
    }
    if (quizMenuTrigger) {
      quizMenuTrigger.disabled = disabled || !!input?.value.trim() || !!pendingWrittenQuestion();
    }
    previewRoot.querySelectorAll(".preview-answer-chip").forEach((button) => {
      button.disabled = disabled;
    });
    previewRoot.querySelectorAll(".preview-question-submit").forEach((button) => {
      button.disabled = disabled || button.dataset.hasSelection !== "true";
    });
    previewRoot.querySelectorAll(".preview-flag-button").forEach((button) => {
      button.disabled = disabled || button.textContent === "Flagged";
    });
    previewRoot.querySelectorAll(".preview-project-answer-input, .preview-project-answer-submit").forEach((field) => {
      field.disabled = disabled || field.dataset.completed === "true";
    });
    previewRoot.querySelectorAll(".preview-further-study-button, .preview-further-study-question").forEach((button) => {
      button.disabled = disabled;
    });
    resourceButtons.forEach((button) => {
      button.disabled = disabled;
    });
    previewRoot.querySelectorAll("[data-preview-metric-button='true']").forEach((button) => {
      button.disabled = disabled;
    });
    if (disabled) {
      closeQuizMenu();
      closeObjectiveMenus();
    }
    syncQuizMenuItems();
    syncFlagSheetState();
    syncGuardrailSheetState();
  }

  function updateComposerClearance() {
    if (!form) {
      return;
    }
    previewRoot.style.setProperty("--preview-composer-clearance", `${form.offsetHeight + 20}px`);
  }

  function formatPercentage(value) {
    return `${Number(value || 0).toFixed(1)}%`;
  }

  function formatMetricNumber(value) {
    return Number(value || 0).toFixed(1);
  }

  function formatCount(value, singular, plural = `${singular}s`) {
    const count = Number(value || 0);
    return `${count} ${count === 1 ? singular : plural}`;
  }

  function formatHalfLifeDays(days) {
    const count = Number(days || 0);
    if (!count) {
      return "";
    }
    return `${count} day${count === 1 ? "" : "s"}`;
  }

  function formatPreviewDate(isoDate) {
    if (!isoDate) {
      return "";
    }
    const parsed = new Date(`${isoDate}T00:00:00`);
    if (Number.isNaN(parsed.getTime())) {
      return isoDate;
    }
    return previewDateFormatter.format(parsed);
  }

  function metricLabel(metricKey) {
    return {
      overall: "Overall practice",
      mastery: "Mastery",
      coverage: "Coverage",
      engagement: "Engagement",
      target: "Target",
    }[metricKey] || "Metric";
  }

  function scrollTranscriptToBottom() {
    transcript?.scrollTo({ top: transcript.scrollHeight, behavior: reducedMotionMedia.matches ? "auto" : "smooth" });
  }

  function transcriptHeaderOffset() {
    if (!transcript || mobileChatMedia.matches) {
      return 0;
    }
    const header = previewRoot.querySelector(".preview-chat-header");
    if (!header || !header.offsetHeight) {
      return 0;
    }
    return header.offsetHeight;
  }

  function scrollTranscriptToMessageTop(messageElement) {
    if (!transcript || !messageElement) {
      return;
    }
    const targetTop = Math.max(messageElement.offsetTop - transcriptHeaderOffset(), 0);
    transcript.scrollTo({ top: targetTop, behavior: reducedMotionMedia.matches ? "auto" : "smooth" });
  }

  function latestTranscriptMessageCard() {
    if (!transcript) {
      return null;
    }
    return transcript.lastElementChild instanceof HTMLElement ? transcript.lastElementChild : null;
  }

  function latestPendingQuestionCard() {
    if (!transcript) {
      return null;
    }
    return Array.from(transcript.querySelectorAll("[data-preview-question='true']")).reverse().find(
      (element) => element.dataset.answered !== "true" && element.dataset.flagged !== "true",
    ) || null;
  }

  function updateQuestionOverflowState(activeQuestion) {
    if (!transcript || !activeQuestion || !activeQuestion.isConnected) {
      return;
    }
    const hint = activeQuestion.querySelector(".preview-question-overflow-hint");
    if (!hint) {
      return;
    }
    const answerRegion = activeQuestion.querySelector(".preview-message-options") || activeQuestion;
    const transcriptRect = transcript.getBoundingClientRect();
    const answerRect = answerRegion.getBoundingClientRect();
    const visibilityTolerance = 6;
    const isFullyVisible = answerRect.bottom <= transcriptRect.bottom + visibilityTolerance;
    activeQuestion.classList.toggle("is-overflowing-question", !isFullyVisible);
    hint.hidden = isFullyVisible;
  }

  function syncQuestionViewport(scrollMode = "bottom", previousScrollTop = 0) {
    if (!transcript) {
      return;
    }

    const questionCards = Array.from(transcript.querySelectorAll("[data-preview-question='true']"));
    questionCards.forEach((card) => {
      card.classList.remove("is-overflowing-question");
      const hint = card.querySelector(".preview-question-overflow-hint");
      if (hint) {
        hint.hidden = true;
      }
    });

    const activeQuestion = latestPendingQuestionCard();
    if (activeQuestion) {
      if (scrollMode === "question") {
        scrollTranscriptToMessageTop(activeQuestion);
        window.requestAnimationFrame(() => {
          if (latestPendingQuestionCard() === activeQuestion) {
            updateQuestionOverflowState(activeQuestion);
          }
        });
      } else {
        updateQuestionOverflowState(activeQuestion);
        window.requestAnimationFrame(() => {
          if (latestPendingQuestionCard() === activeQuestion) {
            updateQuestionOverflowState(activeQuestion);
          }
        });
      }

      if (scrollMode === "question") {
        return;
      }
    }

    if (scrollMode === "preserve") {
      transcript.scrollTop = Math.min(previousScrollTop, transcript.scrollHeight);
      return;
    }

    const latestMessage = latestTranscriptMessageCard();
    if (latestMessage) {
      scrollTranscriptToMessageTop(latestMessage);
      return;
    }

    scrollTranscriptToBottom();
  }

  function ensureActiveBlockCardVisible() {
    if (!blockSwitcher) {
      return;
    }

    const activeCard = blockSwitcher.querySelector(".preview-block-card.is-active");
    if (!activeCard) {
      return;
    }

    const containerRect = blockSwitcher.getBoundingClientRect();
    const cardRect = activeCard.getBoundingClientRect();
    const padding = 12;
    const visibleHeight = containerRect.height - padding * 2;

    if (cardRect.height > visibleHeight) {
      blockSwitcher.scrollTop = Math.max(activeCard.offsetTop - padding, 0);
      return;
    }

    if (cardRect.top < containerRect.top + padding) {
      blockSwitcher.scrollTop -= containerRect.top + padding - cardRect.top;
      return;
    }

    if (cardRect.bottom > containerRect.bottom - padding) {
      blockSwitcher.scrollTop += cardRect.bottom - (containerRect.bottom - padding);
    }
  }

  function metricButtonMarkup(metricKey, value, { scope = "block", blockId = "", metrics = null } = {}) {
    const blockAttribute = blockId ? ` data-block-id="${blockId}"` : "";
    const isFixedEngagement = metricKey === "engagement" && !!metrics?.engagement_is_fixed;
    return `
      <button
        type="button"
        class="preview-block-metric${metricKey === "overall" ? " preview-block-metric--overall" : ""}${isFixedEngagement ? " is-fixed-engagement" : ""}"
        data-preview-metric-button="true"
        data-metric-key="${metricKey}"
        data-metric-scope="${scope}"${blockAttribute}
      >
        <span>${metricLabel(metricKey)}</span>
        <strong>${formatPercentage(value)}</strong>
      </button>
    `;
  }

  function metricMarkup(metrics, { scope = "block", blockId = "" } = {}) {
    return `
      ${scope === "course" && typeof metrics.overall === "number"
        ? metricButtonMarkup("overall", metrics.overall, { scope, blockId, metrics })
        : ""}
      ${metricButtonMarkup("mastery", metrics.mastery, { scope, blockId, metrics })}
      ${metricButtonMarkup("coverage", metrics.coverage, { scope, blockId, metrics })}
      ${metricButtonMarkup("engagement", metrics.engagement, { scope, blockId, metrics })}
      ${metricButtonMarkup("target", metrics.target, { scope, blockId, metrics })}
    `;
  }

  function renderCourseMetrics() {
    if (!courseMetricsPanel) {
      return;
    }
    const metrics = previewState.course?.metrics;
    if (!metrics) {
      courseMetricsPanel.hidden = true;
      courseMetricsPanel.innerHTML = "";
      return;
    }
    courseMetricsPanel.hidden = false;
    courseMetricsPanel.innerHTML = `
      <p class="preview-sidebar-section-label">PRACTICE AVERAGES</p>
      <div class="preview-block-metrics">${metricMarkup(metrics, { scope: "course" })}</div>
    `;
  }

  function optionLabel(index) {
    return String.fromCharCode(65 + index);
  }

  function questionTypeLabel(message) {
    if (message?.question_type_label) {
      return String(message.question_type_label);
    }
    switch (String(message?.question_type || "")) {
      case "num":
        return "Numerical MCQ";
      case "maq":
        return "Multiple-answer MCQ";
      case "waq":
        return "Written answer";
      case "mcq":
      default:
        return "MCQ";
    }
  }

  function formatSelectedAnswers(options, selectedAnswers, flagged = false) {
    const normalizedAnswers = Array.isArray(selectedAnswers) ? selectedAnswers : [];
    const selectedText = normalizedAnswers.map((answer) => {
      const optionIndex = Array.isArray(options) ? options.indexOf(answer) : -1;
      return optionIndex >= 0 ? `${optionLabel(optionIndex)}. ${answer}` : answer;
    });
    if (!selectedText.length) {
      return flagged ? "Selected: flagged" : "";
    }
    return `Selected: ${selectedText.join(", ")}${flagged ? " • flagged" : ""}`;
  }

  function normalizeAnswerList(answers) {
    return Array.isArray(answers) ? answers.filter(Boolean) : [];
  }

  function reviewedOptionState(option, selectedAnswers, correctAnswers) {
    const isSelected = selectedAnswers.includes(option);
    const isCorrect = correctAnswers.includes(option);

    if (isSelected && isCorrect) {
      return { modifier: "is-correct", badge: "Correct", indicator: "✓" };
    }
    if (isSelected && !isCorrect) {
      return { modifier: "is-incorrect", badge: "Your choice", indicator: "×" };
    }
    if (!isSelected && isCorrect) {
      return { modifier: "is-missed", badge: "Missed", indicator: "!" };
    }
    return { modifier: "", badge: "", indicator: "" };
  }

  function renderAnsweredOptions(message) {
    const optionsWrapper = document.createElement("div");
    optionsWrapper.className = "preview-message-options preview-message-options--review";

    const selectedAnswers = normalizeAnswerList(message.selected_answers?.length ? message.selected_answers : [message.selected_answer]);
    const correctAnswers = normalizeAnswerList(message.correct_answers);

    message.options.forEach((option, index) => {
      const state = reviewedOptionState(option, selectedAnswers, correctAnswers);
      const optionRow = document.createElement("div");
      optionRow.className = `preview-answer-chip preview-answer-chip--review${state.modifier ? ` ${state.modifier}` : ""}`;
      const indicator = document.createElement("span");
      indicator.className = "preview-answer-chip-indicator";
      indicator.setAttribute("aria-hidden", "true");
      indicator.textContent = state.indicator;
      const label = document.createElement("span");
      label.className = "preview-answer-chip-label";
      label.textContent = optionLabel(index);
      const text = document.createElement("span");
      text.className = "preview-answer-chip-text";
      richText.appendInlineText(text, option);
      optionRow.append(indicator, label, text);
      if (state.badge) {
        const badge = document.createElement("span");
        badge.className = "preview-answer-chip-badge";
        badge.textContent = state.badge;
        optionRow.appendChild(badge);
      }
      optionsWrapper.appendChild(optionRow);
    });

    if (message.flagged) {
      const flaggedNote = document.createElement("p");
      flaggedNote.className = "preview-message-sources";
      flaggedNote.textContent = "Question flagged.";
      optionsWrapper.appendChild(flaggedNote);
    }

    return optionsWrapper;
  }

  function renderWrittenAnswerReview(message) {
    const review = document.createElement("div");
    review.className = "preview-written-answer-review";
    review.appendChild(richText.buildTextPanel("Your answer", message.submitted_text || "No answer submitted."));

    const meter = document.createElement("div");
    meter.className = `preview-written-answer-alignment is-${message.alignment_state || "drafting"}`;
    meter.innerHTML = `
      <div class="preview-written-answer-alignment-head">
        <span>Alignment</span>
        <strong>${formatPercentage(message.alignment_score)}</strong>
      </div>
      <div class="preview-written-answer-alignment-track" aria-hidden="true">
        <span style="width: ${Math.max(0, Math.min(Number(message.alignment_score || 0), 100))}%;"></span>
      </div>
    `;
    review.appendChild(meter);

    if (message.model_answer_revealed && message.model_answer) {
      review.appendChild(richText.buildTextPanel("Model answer", message.model_answer, "is-model-answer"));
    }

    return review;
  }

  function appendFormattedMessageContent(container, text) {
    richText.appendFormattedMessageContent(container, text);
  }

  function appendQuestionCodeSnippet(container, message) {
    if (!container || !message?.is_coding_question || !message.code_snippet) {
      return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "preview-question-code-snippet";
    const label = document.createElement("span");
    label.className = "preview-question-code-label";
    const kind = message.coding_question_kind === "debug" ? "Debug" : "Code";
    const language = String(message.coding_language || "").trim();
    label.textContent = language ? `${kind} · ${language}` : kind;
    const pre = document.createElement("pre");
    pre.className = "preview-message-code-block preview-question-code-block";
    if (language) {
      pre.dataset.language = language;
    }
    const code = document.createElement("code");
    code.className = "preview-message-code";
    code.textContent = String(message.code_snippet || "").replace(/^\n+|\n+$/g, "");
    pre.appendChild(code);
    wrapper.appendChild(label);
    wrapper.appendChild(pre);
    container.appendChild(wrapper);
  }

  function questionStemText(message) {
    let stem = String(message?.text || "").trim();
    if (!stem) {
      return "";
    }
    stem = stem
      .replace(/\s*\((?:validation\s+)?variant\s+\d+\)\??\s*$/i, "")
      .replace(/\s+/g, " ")
      .trim();
    if (message?.is_coding_question && message?.code_snippet) {
      const strippedStem = stem.replace(/```[\w+-]*\n?[\s\S]*?```/g, " ").replace(/\s+/g, " ").trim();
      if (strippedStem) {
        stem = strippedStem;
      }
    }
    return stem;
  }

  function appendFurtherStudyAction(actions, message) {
    if (
      !actions
      || !message
      || !Array.isArray(message.further_study_questions)
      || !message.further_study_questions.length
    ) {
      return;
    }
    const furtherStudyButton = document.createElement("button");
    furtherStudyButton.type = "button";
    furtherStudyButton.className = "preview-further-study-button";
    furtherStudyButton.textContent = "Further study";
    furtherStudyButton.disabled = requestInFlight;
    furtherStudyButton.addEventListener("click", () => {
      appendFurtherStudyMessage(message);
    });
    actions.appendChild(furtherStudyButton);
  }

  function renderBlockSwitcher() {
    if (!blockSwitcher) {
      return;
    }
    const previousScrollTop = blockSwitcher.scrollTop;
    const previousActiveBlockId = blockSwitcher.dataset.activeBlockId || "";
    const activeChanged = previousActiveBlockId !== String(activeBlockId);
    blockSwitcher.innerHTML = "";
    (previewState.blocks || []).forEach((block) => {
      const isActive = String(block.id) === String(activeBlockId);
      const isSelectionPreview = isActive && isSidebarSelectionPreview(block.id);
      const article = document.createElement("article");
      article.className = `preview-block-card${isActive ? " is-expanded is-active" : ""}${isSelectionPreview ? " is-selection-preview" : ""}`;

      const controlsId = `preview-block-panel-${block.id}`;
      const header = document.createElement("div");
      header.className = "preview-block-card-header";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "preview-block-card-toggle";
      button.setAttribute("aria-expanded", isActive ? "true" : "false");
      button.setAttribute("aria-controls", controlsId);
      const overallPractice = typeof block.metrics?.overall === "number"
        ? formatPercentage(block.metrics.overall)
        : "0.0%";
      button.innerHTML = `
        <div class="preview-block-title-row">
          <strong>${block.title}</strong>
          <span class="preview-block-card-icon" aria-hidden="true"></span>
        </div>
      `;
      button.addEventListener("click", () => {
        activeBlockId = String(block.id);
        if (isMobileSidebar()) {
          scheduleSidebarAutoClose(block.id);
        } else {
          clearSidebarAutoCloseTimer(true);
        }
        renderPreview();
      });
      const overallButton = document.createElement("button");
      overallButton.type = "button";
      overallButton.className = "preview-block-overall-button";
      overallButton.setAttribute("aria-label", `Show how overall practice is calculated for ${block.title}`);
      overallButton.innerHTML = `
        <span>Overall practice</span>
        <strong>${overallPractice}</strong>
      `;
      overallButton.addEventListener("click", () => {
        activeBlockId = String(block.id);
        renderPreview("preserve");
        appendMetricMessage("overall", "block", block.id);
      });
      header.append(button, overallButton);

      const content = document.createElement("div");
      content.id = controlsId;
      content.className = "preview-block-card-content";
      content.hidden = !isActive;
      content.innerHTML = `
        <div class="preview-block-metrics">${metricMarkup(block.metrics, { scope: "block", blockId: block.id })}</div>
      `;

      article.append(header, content);
      blockSwitcher.appendChild(article);
    });
    blockSwitcher.dataset.activeBlockId = String(activeBlockId);
    window.requestAnimationFrame(() => {
      if (!blockSwitcher) {
        return;
      }
      if (!activeChanged) {
        blockSwitcher.scrollTop = previousScrollTop;
      }
      ensureActiveBlockCardVisible();
    });
  }

  function renderMessage(message) {
    const article = document.createElement("article");
    const roleClass = message.role === "user" ? "preview-message--user" : "preview-message--assistant";
    const feedbackClass =
      message.kind === "feedback" ? (message.correct ? " preview-feedback--correct" : " preview-feedback--incorrect") : "";
    article.className = `preview-message ${roleClass}${feedbackClass}`;

    if (message.kind === "question") {
      article.dataset.previewQuestion = "true";
      article.dataset.questionId = String(message.question_id || "");
      article.dataset.answered = message.answered ? "true" : "false";
      article.dataset.flagged = message.flagged ? "true" : "false";
      const callout = document.createElement("div");
      callout.className = "preview-question-callout";
      callout.textContent = questionTypeLabel(message);
      article.appendChild(callout);
      appendFormattedMessageContent(article, questionStemText(message));
      appendQuestionCodeSnippet(article, message);

      if (message.question_type === "waq" && !message.answered && !message.flagged) {
        const helper = document.createElement("p");
        helper.className = "preview-message-sources";
        helper.textContent = "Type your answer in the fixed box below.";
        article.appendChild(helper);
      } else if (Array.isArray(message.options) && message.options.length && !message.answered && !message.flagged) {
        const overflowHint = document.createElement("div");
        overflowHint.className = "preview-question-overflow-hint";
        overflowHint.hidden = true;
        overflowHint.textContent = message.question_type === "waq"
          ? "Scroll to see the rest of this question."
          : "Scroll to see all answers.";
        article.appendChild(overflowHint);

        const optionsWrapper = document.createElement("div");
        optionsWrapper.className = "preview-message-options";
        if (message.question_type === "maq") {
          const selections = maqSelection(message.question_id);
          message.options.forEach((option, index) => {
            const optionButton = document.createElement("button");
            optionButton.type = "button";
            optionButton.className = `preview-answer-chip preview-answer-chip--maq${selections.includes(option) ? " is-selected" : ""}`;
            optionButton.dataset.maqOptionButton = "true";
            optionButton.dataset.optionValue = option;
            optionButton.setAttribute("aria-pressed", selections.includes(option) ? "true" : "false");
            optionButton.innerHTML = `
              <span class="preview-answer-chip-checkbox" aria-hidden="true">${selections.includes(option) ? "✓" : ""}</span>
              <span class="preview-answer-chip-label">${optionLabel(index)}</span>
              <span class="preview-answer-chip-text"></span>
            `;
            richText.appendInlineText(optionButton.querySelector(".preview-answer-chip-text"), option);
            optionButton.disabled = requestInFlight;
            optionButton.addEventListener("click", () => {
              toggleMaqSelection(message.question_id, option);
              syncRenderedMaqQuestion(message.question_id);
            });
            optionsWrapper.appendChild(optionButton);
          });

          const submitRow = document.createElement("div");
          submitRow.className = "preview-question-submit-row";
          const submitSelectionButton = document.createElement("button");
          submitSelectionButton.type = "button";
          submitSelectionButton.className = "button secondary preview-question-submit";
          submitSelectionButton.dataset.maqSubmitButton = "true";
          submitSelectionButton.textContent = "Submit";
          submitSelectionButton.dataset.hasSelection = selections.length ? "true" : "false";
          submitSelectionButton.disabled = requestInFlight || !selections.length;
          submitSelectionButton.addEventListener("click", () => {
            const currentSelections = maqSelection(message.question_id);
            void postPreviewAction("answer", {
              question_id: message.question_id,
              answers: currentSelections,
            });
          });
          submitRow.appendChild(submitSelectionButton);
          optionsWrapper.appendChild(submitRow);
        } else {
          message.options.forEach((option, index) => {
            const optionButton = document.createElement("button");
            optionButton.type = "button";
            optionButton.className = "preview-answer-chip";
            optionButton.innerHTML = `
              <span class="preview-answer-chip-label">${optionLabel(index)}</span>
              <span class="preview-answer-chip-text"></span>
            `;
            richText.appendInlineText(optionButton.querySelector(".preview-answer-chip-text"), option);
            optionButton.disabled = requestInFlight;
            optionButton.addEventListener("click", () => {
              void postPreviewAction("answer", {
                question_id: message.question_id,
                answer: option,
              });
            });
            optionsWrapper.appendChild(optionButton);
          });
        }
        article.appendChild(optionsWrapper);
      } else if (message.question_type === "waq" && (message.submitted_text || message.model_answer_revealed)) {
        article.appendChild(renderWrittenAnswerReview(message));
      } else if (
        (Array.isArray(message.selected_answers) && message.selected_answers.length) ||
        message.selected_answer
      ) {
        if (Array.isArray(message.correct_answers) && message.correct_answers.length) {
          article.appendChild(renderAnsweredOptions(message));
        } else {
          const selected = document.createElement("p");
          selected.className = "preview-message-sources";
          selected.textContent = formatSelectedAnswers(
            message.options,
            Array.isArray(message.selected_answers) && message.selected_answers.length
              ? message.selected_answers
              : [message.selected_answer],
            message.flagged,
          );
          article.appendChild(selected);
        }
      }

      const actions = document.createElement("div");
      actions.className = "preview-message-actions";
      if (
        message.answered
        && !message.flagged
        && Array.isArray(message.further_study_questions)
        && message.further_study_questions.length
      ) {
        appendFurtherStudyAction(actions, message);
      }
      if (!hideFlagActions) {
        const flagButton = document.createElement("button");
        flagButton.type = "button";
        flagButton.className = "preview-flag-button";
        flagButton.textContent = message.flagged ? "Flagged" : "Flag question";
        flagButton.disabled = requestInFlight || message.flagged;
        flagButton.addEventListener("click", () => {
          if (isTeacherPreview) {
            openFlagSheet(message);
            return;
          }
          void postPreviewAction("flag", { question_id: message.question_id });
        });
        actions.appendChild(flagButton);
      }
      article.appendChild(actions);
      richText.renderMath(article);
      return article;
    }

    if (message.kind === "loading") {
      article.classList.add("preview-message--loading");
      article.innerHTML = `
        <div class="preview-loading-dots" aria-label="Generating next quiz question">
          <span></span>
          <span></span>
          <span></span>
        </div>
      `;
      return article;
    }

    if (message.kind === "validation_reminder") {
      article.classList.add("preview-message--validation-reminder");
      appendFormattedMessageContent(article, message.text || "");

      if (message.cta_url) {
        const actions = document.createElement("div");
        actions.className = "preview-message-actions";
        const link = document.createElement("a");
        link.className = "button secondary preview-validation-reminder-link";
        link.href = message.cta_url;
        link.textContent = message.cta_label || "Book validation";
        actions.appendChild(link);
        article.appendChild(actions);
      }

      return article;
    }

    if (message.kind === "resource") {
      article.innerHTML = `
        <div class="preview-message-meta">
          <span class="preview-message-pill">${message.block_label}</span>
          <span class="preview-message-pill">${message.resource_label}</span>
        </div>
      `;
      if (message.resource_key === "metric") {
        if (message.text) {
          const summary = document.createElement("p");
          summary.textContent = message.text;
          article.appendChild(summary);
        }

        const metricRows = Array.isArray(message.metric_rows) ? message.metric_rows : [];
        if (metricRows.length) {
          const list = document.createElement("ul");
          list.className = "preview-metric-detail-list";
          metricRows.forEach((row) => {
            const item = document.createElement("li");
            item.textContent = row;
            list.appendChild(item);
          });
          article.appendChild(list);
        }

        if (message.metric_formula) {
          const formula = document.createElement("p");
          formula.className = "preview-metric-formula";
          formula.textContent = message.metric_formula;
          article.appendChild(formula);
        }

        return article;
      }
      if (message.resource_key === "further_study") {
        if (message.text) {
          const summary = document.createElement("p");
          summary.textContent = message.text;
          article.appendChild(summary);
        }

        const questions = Array.isArray(message.questions) ? message.questions : [];
        if (questions.length) {
          const list = document.createElement("div");
          list.className = "preview-further-study-list";
          questions.forEach((questionText) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "preview-further-study-question";
            button.textContent = questionText;
            button.disabled = requestInFlight;
            button.addEventListener("click", () => {
              void sendCourseChatQuestion(questionText, { focusComposer: true, closeSidebarOnMobile: true });
            });
            list.appendChild(button);
          });
          article.appendChild(list);
        }

        return article;
      }
      if (message.resource_key === "objectives") {
        const list = document.createElement("ul");
        list.className = "preview-objective-list";
        const resourceBlock = findBlock(message.block_id || currentBlock()?.id || 0);
        const objectives = Array.isArray(resourceBlock?.learning_objectives)
          ? resourceBlock.learning_objectives
          : (Array.isArray(message.objectives) ? message.objectives : []);

        if (!objectives.length) {
          const emptyItem = document.createElement("li");
          emptyItem.className = "preview-objective-item";
          emptyItem.textContent = "No learning objectives yet.";
          list.appendChild(emptyItem);
        } else {
          objectives.forEach((objective) => {
            const item = document.createElement("li");
            item.className = `preview-objective-item${objective.covered ? " is-covered" : ""}`;

            const tick = document.createElement("span");
            tick.className = "preview-objective-status";
            tick.setAttribute("aria-hidden", "true");
            tick.textContent = objective.covered ? "✓" : "";

            const code = document.createElement("span");
            code.className = "preview-objective-code";
            code.textContent = objective.code;

            const text = document.createElement("span");
            text.className = "preview-objective-text";
            text.textContent = objective.text;

            item.append(tick, code, text);

            if (isTeacherPreview) {
              const actions = document.createElement("div");
              actions.className = "preview-objective-actions";
              const menu = document.createElement("div");
              menu.className = "preview-objective-menu";
              menu.dataset.previewObjectiveMenu = "true";
              const trigger = document.createElement("button");
              trigger.type = "button";
              trigger.className = "preview-objective-menu-trigger";
              trigger.setAttribute("aria-label", `Actions for ${objective.code}`);
              trigger.setAttribute("aria-haspopup", "menu");
              trigger.setAttribute("aria-expanded", "false");
              trigger.dataset.previewObjectiveMenuTrigger = "true";
              trigger.disabled = requestInFlight;
              trigger.innerHTML = `
                <span class="preview-objective-menu-trigger-dots" aria-hidden="true">
                  <span></span>
                  <span></span>
                  <span></span>
                </span>
              `;
              const panel = document.createElement("div");
              panel.className = "preview-objective-menu-panel";
              panel.setAttribute("role", "menu");
              panel.hidden = true;
              panel.dataset.previewObjectiveMenuPanel = "true";
              panel.innerHTML = `
                <button type="button" role="menuitem" class="preview-objective-menu-item" data-preview-objective-question-type="mcq" data-objective-id="${objective.id}">
                  Re-generate MCQ
                </button>
                <button type="button" role="menuitem" class="preview-objective-menu-item" data-preview-objective-question-type="num" data-objective-id="${objective.id}">
                  Re-generate Numeric
                </button>
                <button type="button" role="menuitem" class="preview-objective-menu-item" data-preview-objective-question-type="maq" data-objective-id="${objective.id}">
                  Re-generate MAQ
                </button>
                <button type="button" role="menuitem" class="preview-objective-menu-item" data-preview-objective-question-type="waq" data-objective-id="${objective.id}">
                  Re-generate WAQ
                </button>
                <button type="button" role="menuitem" class="preview-objective-menu-item is-accent" data-preview-objective-guardrail="true" data-objective-id="${objective.id}">
                  Add guardrail
                </button>
              `;
              menu.append(trigger, panel);
              actions.appendChild(menu);
              item.appendChild(actions);
            }

            list.appendChild(item);
          });
        }

        article.appendChild(list);
        return article;
      }
    }

    appendFormattedMessageContent(article, message.text || "");

    if (message.role === "assistant" && message.kind === "text") {
      const actions = document.createElement("div");
      actions.className = "preview-message-actions";
      appendFurtherStudyAction(actions, message);
      if (actions.childElementCount) {
        article.appendChild(actions);
      }
    }

    richText.renderMath(article);

    return article;
  }

  function combinedTranscript(block) {
    const project = currentProject(block);
    if (project) {
      const messages = Array.isArray(project.transcript) ? [...project.transcript] : [];
      if (optimisticUserMessagesByBlock[String(block.id)]) {
        messages.push(optimisticUserMessagesByBlock[String(block.id)]);
      }
      if (loadingMessagesByBlock[String(block.id)]) {
        messages.push({
          id: `loading-project-${block.id}`,
          kind: "loading",
          role: "assistant",
        });
      }
      return messages;
    }
    const baseMessages = Array.isArray(block?.transcript) ? block.transcript : [];
    const inlineMessages = block ? blockInlineMessages(block.id) : [];
    const combined = [];

    inlineMessages
      .filter((message) => message.insert_after_count === 0)
      .sort((left, right) => left.sequence - right.sequence)
      .forEach((message) => combined.push(message));

    baseMessages.forEach((message, index) => {
      combined.push(message);
      inlineMessages
        .filter((inlineMessage) => inlineMessage.insert_after_count === index + 1)
        .sort((left, right) => left.sequence - right.sequence)
        .forEach((inlineMessage) => combined.push(inlineMessage));
    });

    inlineMessages
      .filter((message) => message.insert_after_count > baseMessages.length)
      .sort((left, right) => left.sequence - right.sequence)
      .forEach((message) => combined.push(message));

    if (block && optimisticUserMessagesByBlock[String(block.id)]) {
      combined.push(optimisticUserMessagesByBlock[String(block.id)]);
    }

    if (block && loadingMessagesByBlock[String(block.id)]) {
      combined.push({
        id: `loading-${block.id}`,
        kind: "loading",
        role: "assistant",
      });
    }

    return combined;
  }

  function renderTranscript(scrollMode = "bottom") {
    if (!transcript) {
      return;
    }
    const block = currentBlock();
    const previousScrollTop = transcript.scrollTop;
    transcript.innerHTML = "";
    if (!block) {
      return;
    }
    combinedTranscript(block).forEach((message) => {
      transcript.appendChild(renderMessage(message));
    });
    syncQuestionViewport(scrollMode, previousScrollTop);
  }

  function resourceMessagePayload(block, resource) {
    if (resource === "description") {
      return {
        block_id: block.id,
        block_label: block.title,
        kind: "resource",
        resource_key: "description",
        resource_label: "Description",
        role: "assistant",
        text: block.summary || "No description yet.",
      };
    }

    const objectives = Array.isArray(block.learning_objectives) ? block.learning_objectives : [];
    return {
      block_id: block.id,
      block_label: block.title,
      kind: "resource",
      resource_key: "objectives",
      resource_label: "Learning objectives",
      role: "assistant",
      text: "",
      objectives,
    };
  }

  function furtherStudyMessagePayload(block, sourceMessage) {
    const questions = Array.isArray(sourceMessage?.further_study_questions)
      ? sourceMessage.further_study_questions.filter(Boolean)
      : [];
    if (!block || !sourceMessage || !questions.length) {
      return null;
    }
    return {
      block_label: block.title,
      kind: "resource",
      resource_key: "further_study",
      resource_label: "Further study",
      role: "assistant",
      text: sourceMessage.kind === "question"
        ? "Try one of these follow-up questions."
        : "Take this a step further.",
      questions,
    };
  }

  function metricMessagePayload(metricKey, { scope = "course", block = currentBlock() } = {}) {
    const courseMetrics = previewState.course?.metrics;
    const metrics = scope === "course" ? courseMetrics : block?.metrics;
    if (!metrics) {
      return null;
    }

    if (scope === "block" && metricKey === "coverage" && block) {
      return resourceMessagePayload(block, "objectives");
    }

    const liveBlockCount = Number(courseMetrics?.block_count || 0);
    const liveBlockLabel = formatCount(liveBlockCount, "live block");
    const metricTitle = metricLabel(metricKey);
    const payload = {
      block_label: scope === "course" ? "Practice averages" : (block?.title || "Practice"),
      kind: "resource",
      resource_key: "metric",
      resource_label: metricTitle,
      role: "assistant",
      text: "",
      metric_rows: [],
      metric_formula: "",
    };

    if (metricKey === "overall") {
      const weights = courseMetrics?.weights || {};
      payload.text = scope === "course"
        ? `Overall practice is the weighted practice score built from the average mastery, coverage, engagement, and target values across ${liveBlockLabel}.`
        : "Overall practice for this block is the weighted score built from mastery, coverage, engagement, and target.";
      payload.metric_rows = [
        `${scope === "course" ? "Average mastery" : "Mastery"}: ${formatPercentage(metrics.mastery)} x ${weights.mastery || 0}`,
        `${scope === "course" ? "Average coverage" : "Coverage"}: ${formatPercentage(metrics.coverage)} x ${weights.coverage || 0}`,
        `${scope === "course" ? "Average engagement" : "Engagement"}: ${formatPercentage(metrics.engagement)} x ${weights.engagement || 0}`,
        `${scope === "course" ? "Average target" : "Target"}: ${formatPercentage(metrics.target)} x ${weights.target || 0}`,
      ];
      payload.metric_formula = weights.total
        ? `(${formatMetricNumber(metrics.mastery)} x ${weights.mastery || 0} + ${formatMetricNumber(metrics.coverage)} x ${weights.coverage || 0} + ${formatMetricNumber(metrics.engagement)} x ${weights.engagement || 0} + ${formatMetricNumber(metrics.target)} x ${weights.target || 0}) / ${weights.total} = ${formatPercentage(metrics.overall)}`
        : "No practice-score weightings have been set yet.";
      return payload;
    }

    if (metricKey === "mastery") {
      payload.text = scope === "course"
        ? `Average mastery is the mean mastery score across ${liveBlockLabel}.`
        : "Mastery for this block is correct answers divided by completed answers.";
      payload.metric_rows = [
        `Displayed score: ${formatPercentage(metrics.mastery)}`,
        `Correct answers: ${metrics.correct_count || 0}`,
        `Incorrect answers: ${metrics.incorrect_count || 0}`,
        `Completed questions: ${metrics.completed_count || 0}`,
      ];
      return payload;
    }

    if (metricKey === "coverage") {
      payload.text = `Average coverage is the mean block coverage across ${liveBlockLabel}.`;
      payload.metric_rows = [
        `Displayed score: ${formatPercentage(metrics.coverage)}`,
        `Learning objectives covered at least once: ${metrics.covered_objective_count || 0} of ${metrics.total_objective_count || 0} across the whole course`,
      ];
      return payload;
    }

    if (metricKey === "engagement") {
      const halfLifeLabel = formatHalfLifeDays(metrics.engagement_half_life_days || 0);
      if (metrics.engagement_is_fixed) {
        payload.text = scope === "course"
          ? `Average engagement is currently fixed at 100% across ${liveBlockLabel} because no engagement half-life is configured for this course. Engagement decay will start only after a half-life is set.`
          : "Engagement is currently fixed at 100% for this block because no engagement half-life is configured for this course. Once a half-life is set, engagement will decay exponentially from the block release date.";
        payload.metric_rows = scope === "course"
          ? [
            `Displayed score: ${formatPercentage(metrics.engagement)}`,
            "Half-life: not set",
            "Decay status: inactive",
          ]
          : [
            `Displayed score: ${formatPercentage(metrics.engagement)}`,
            `Release date: ${block?.available_from_label || formatPreviewDate(block?.available_from || "") || "Not set"}`,
            "Half-life: not set",
            "Decay status: inactive",
          ];
        return payload;
      }
      if (scope === "course") {
        payload.text = `Average engagement is the mean engagement score across ${liveBlockLabel}. Each answered question contributes a decayed weight from its block release date using an exponential half-life of ${halfLifeLabel}. A question counts as 100% on release day, 50% after one half-life, 25% after two, and so on.`;
        payload.metric_rows = [
          `Displayed score: ${formatPercentage(metrics.engagement)}`,
          `Half-life: ${halfLifeLabel}`,
          `Weighted activity: ${formatMetricNumber(metrics.engagement_weighted_count || 0)}`,
          `Combined live-block target: ${metrics.combined_target_question_count || 0}`,
        ];
        return payload;
      }

      const availableFromLabel = block?.available_from_label || formatPreviewDate(block?.available_from || "");
      payload.text = `Engagement for this block decays exponentially from its release date using a half-life of ${halfLifeLabel}. Each answered question contributes a weight of 0.5^(days since release / half-life). The displayed score is weighted activity divided by the block target, capped at 100%.`;
      payload.metric_rows = [
        `Displayed score: ${formatPercentage(metrics.engagement)}`,
        `Release date: ${availableFromLabel || "Not set"}`,
        `Half-life: ${halfLifeLabel}`,
        `Weighted activity: ${formatMetricNumber(metrics.engagement_weighted_count || 0)} of ${metrics.target_question_count || 0}`,
      ];
      return payload;
    }

    if (metricKey === "target") {
      payload.text = scope === "course"
        ? `Average target is the mean target-completion score across ${liveBlockLabel}.`
        : "Target shows how much of this block's practice target has been completed.";
      payload.metric_rows = scope === "course"
        ? [
          `Displayed score: ${formatPercentage(metrics.target)}`,
          `Completed questions: ${metrics.completed_count || 0}`,
          `Combined live-block target: ${metrics.combined_target_question_count || 0}`,
        ]
        : [
          `Displayed score: ${formatPercentage(metrics.target)}`,
          `Completed questions: ${metrics.completed_count || 0} of ${metrics.target_question_count || 0}`,
        ];
      return payload;
    }

    return null;
  }

  function appendInlineMessage(messagePayload, { block = currentBlock(), dedupeKey = "", closeSidebarOnMobile = false } = {}) {
    if (!block || !messagePayload) {
      return;
    }

    const inlineMessages = blockInlineMessages(block.id);
    const lastMessage = inlineMessages[inlineMessages.length - 1];
    if (dedupeKey && lastMessage?._dedupe_key === dedupeKey) {
      scrollTranscriptToBottom();
      if (closeSidebarOnMobile && isMobileSidebar()) {
        setSidebarOpen(false);
      }
      return;
    }

    inlineMessageSequence += 1;
    inlineMessages.push({
      ...messagePayload,
      _dedupe_key: dedupeKey,
      id: `inline-resource-${block.id}-${inlineMessageSequence}`,
      insert_after_count: Array.isArray(block.transcript) ? block.transcript.length : 0,
      sequence: inlineMessageSequence,
    });
    renderTranscript();
    if (closeSidebarOnMobile && isMobileSidebar()) {
      setSidebarOpen(false);
    }
  }

  function appendResourceMessage(resource) {
    const block = currentBlock();
    if (!block || !resource) {
      return;
    }
    appendInlineMessage(resourceMessagePayload(block, resource), {
      block,
      dedupeKey: `resource:${block.id}:${resource}`,
    });
  }

  function appendFurtherStudyMessage(sourceMessage) {
    const block = currentBlock();
    const payload = furtherStudyMessagePayload(block, sourceMessage);
    if (!block || !payload) {
      return;
    }
    const sourceKey = sourceMessage.question_id || sourceMessage.id || payload.questions.join("|");
    appendInlineMessage(payload, {
      block,
      dedupeKey: `further-study:${block.id}:${sourceKey}`,
      closeSidebarOnMobile: true,
    });
  }

  function renderProjectSwitcher() {
    if (!projectSwitcher) {
      return;
    }
    const block = currentBlock();
    projectSwitcher.innerHTML = "";
    if (!block) {
      projectSwitcher.hidden = true;
      return;
    }
    const projects = Array.isArray(block.projects) ? block.projects : [];
    if (!projects.length) {
      projectSwitcher.hidden = true;
      return;
    }
    projectSwitcher.hidden = false;

    const practiceButton = document.createElement("button");
    practiceButton.type = "button";
    practiceButton.className = `preview-header-action${currentProject(block) ? "" : " is-active"}`;
    practiceButton.textContent = "Practice";
    practiceButton.addEventListener("click", () => {
      setActiveProject(block.id, "");
      renderPreview("preserve");
    });
    projectSwitcher.appendChild(practiceButton);

    projects.forEach((project) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `preview-header-action${String(currentProject(block)?.id || "") === String(project.id) ? " is-active" : ""}`;
      button.textContent = project.title;
      button.addEventListener("click", async () => {
        setActiveProject(block.id, project.id);
        if (!project.materialized) {
          await postPreviewAction("project_open", { project_id: project.id }, { scrollMode: "preserve" });
        } else {
          renderPreview("preserve");
        }
      });
      projectSwitcher.appendChild(button);
    });
  }

  function renderProjectPanel() {
    if (!projectPanel) {
      return;
    }
    const block = currentBlock();
    const project = currentProject(block);
    projectPanel.innerHTML = "";
    projectPanel.hidden = !project;
    if (!project) {
      return;
    }

    const statusLabel = project.assignment_status === "complete"
      ? "Complete"
      : (project.assignment_status === "in_progress" ? "In progress" : "Ready");

    const downloads = Array.isArray(project.downloads) ? project.downloads : [];
    const wrapper = document.createElement("div");
    wrapper.className = "preview-written-answer-panel";
    wrapper.innerHTML = `
      <div class="preview-message-meta">
        <span class="preview-message-pill">Project</span>
        <span class="preview-message-pill">${statusLabel}</span>
        ${project.seed ? `<span class="preview-message-pill">Seed ${project.seed}</span>` : ""}
      </div>
    `;

    const instructions = document.createElement("div");
    appendFormattedMessageContent(instructions, project.student_instructions || "Project instructions will appear here once published.");
    wrapper.appendChild(instructions);

    if (downloads.length) {
      const actions = document.createElement("div");
      actions.className = "preview-message-actions";
      downloads.forEach((download) => {
        const link = document.createElement("a");
        link.className = "button secondary";
        link.href = download.url;
        link.textContent = download.label;
        actions.appendChild(link);
      });
      wrapper.appendChild(actions);
    }

    const answerRow = document.createElement("div");
    answerRow.className = "preview-chat-composer";
    const answerInput = document.createElement("input");
    answerInput.type = "text";
    answerInput.className = "preview-project-answer-input";
    answerInput.placeholder = project.answer_unit
      ? `${project.answer_label} (${project.answer_unit})`
      : project.answer_label || "Answer";
    answerInput.value = projectAnswerDraftsById[String(project.id)] || "";
    answerInput.disabled = requestInFlight || project.assignment_status === "complete";
    answerInput.dataset.completed = project.assignment_status === "complete" ? "true" : "false";
    answerInput.addEventListener("input", () => {
      projectAnswerDraftsById[String(project.id)] = answerInput.value;
      submitAnswerButton.disabled = requestInFlight || project.assignment_status === "complete" || !answerInput.value.trim();
    });
    answerInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitAnswerButton.click();
      }
    });
    const submitAnswerButton = document.createElement("button");
    submitAnswerButton.type = "button";
    submitAnswerButton.className = "button preview-project-answer-submit";
    submitAnswerButton.textContent = project.assignment_status === "complete" ? "Completed" : "Submit project answer";
    submitAnswerButton.disabled = requestInFlight || project.assignment_status === "complete" || !answerInput.value.trim();
    submitAnswerButton.dataset.completed = project.assignment_status === "complete" ? "true" : "false";
    submitAnswerButton.addEventListener("click", async () => {
      const answer = String(answerInput.value || "").trim();
      if (!answer) {
        return;
      }
      projectAnswerDraftsById[String(project.id)] = "";
      await postPreviewAction(
        "project_submit",
        { project_id: project.id, answer },
        { focusComposer: true, minDurationMs: 600, scrollMode: "bottom" },
      );
    });
    answerRow.append(answerInput, submitAnswerButton);
    wrapper.appendChild(answerRow);

    const helper = document.createElement("p");
    helper.className = "preview-message-sources";
    helper.textContent = project.assignment_status === "complete"
      ? "This project is complete. You can still revisit the transcript and downloads."
      : (project.hints_remaining > 0
        ? `${project.hints_remaining} guided hint${project.hints_remaining === 1 ? "" : "s"} remaining.`
        : "You can still ask for a nudge in the chat box below.");
    wrapper.appendChild(helper);

    projectPanel.appendChild(wrapper);
  }

  function appendMetricMessage(metricKey, scope, blockId = "") {
    const block = scope === "block" ? findBlock(blockId) : currentBlock();
    if (!block || !metricKey) {
      return;
    }

    const payload = metricMessagePayload(metricKey, { scope, block });
    if (!payload) {
      return;
    }

    const dedupeKey = payload.resource_key === "objectives"
      ? `resource:${block.id}:objectives`
      : `metric:${scope}:${metricKey}:${block.id}`;

    appendInlineMessage(payload, {
      block,
      dedupeKey,
      closeSidebarOnMobile: true,
    });
  }

  function renderPreview(scrollMode = "bottom") {
    const block = currentBlock();
    if (!block) {
      return;
    }
    closeObjectiveMenus();
    activeBlockId = String(block.id);
    persistActiveBlockId(activeBlockId);
    const project = currentProject(block);
    if (activeBlockTitle) {
      activeBlockTitle.textContent = project ? `${block.title} · ${project.title}` : block.title;
    }
    renderCourseMetrics();
    renderBlockSwitcher();
    renderProjectSwitcher();
    renderProjectPanel();
    renderTranscript(scrollMode);
    if (flagSheetState) {
      const sheetBlock = currentFlagSheetBlock();
      const stillAvailable = Array.isArray(sheetBlock?.transcript)
        && sheetBlock.transcript.some(
          (message) => Number(message.question_id || 0) === Number(flagSheetState.questionId)
            && !message.flagged,
        );
      if (!stillAvailable) {
        closeFlagSheet();
      } else {
        syncFlagSheetState();
      }
    }
    if (guardrailSheetState) {
      const objective = currentObjectiveForGuardrailSheet();
      if (!objective) {
        closeGuardrailSheet();
      } else {
        if (objectiveSheetObjective) {
          objectiveSheetObjective.textContent = `${objective.code} ${objective.text}`;
        }
        const currentGuidance = String(objective.assistant_guidance || "").trim();
        if (objectiveSheetExistingWrap) {
          objectiveSheetExistingWrap.hidden = !currentGuidance;
        }
        if (objectiveSheetExisting) {
          objectiveSheetExisting.textContent = currentGuidance;
        }
        syncGuardrailSheetState();
      }
    }
    syncComposerInputFromState();
    syncComposerState();
    updateComposerClearance();
  }

  async function postDraftAnswer(questionId, answerText, requestId) {
    const block = currentBlock();
    if (!block) {
      return;
    }

    const controller = new AbortController();
    waqDraftAbortController = controller;
    const response = await fetch(actionUrl(block.id, "draft_answer"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
      },
      body: JSON.stringify({
        question_id: questionId,
        answer_text: answerText,
      }),
      credentials: "same-origin",
      signal: controller.signal,
    });
    if (waqDraftAbortController === controller) {
      waqDraftAbortController = null;
    }
    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || "Unable to update alignment right now.");
    }
    if (requestId !== waqDraftRequestId) {
      return;
    }
    let previousState = "drafting";
    const updatedQuestion = updateQuestionMessage(data.alignment.question_id, (message) => {
      previousState = message.alignment_state || "drafting";
      message.draft_answer = data.alignment.answer_text || "";
      message.alignment_score = data.alignment.alignment_score || 0;
      message.alignment_state = data.alignment.alignment_state || "drafting";
    });
    clearWaqAlignmentLoading(requestId);
    const shouldFlash = previousState !== "aligned" && updatedQuestion?.alignment_state === "aligned";
    if (updatedQuestion && String(updatedQuestion.question_id) === String(pendingWrittenQuestion()?.question_id)) {
      renderWaqAlignment(updatedQuestion, { flash: shouldFlash });
    }
  }

  async function postPreviewAction(action, payload = null, options = {}) {
    const block = currentBlock();
    if (!block) {
      return false;
    }
    setStatus("");
    clearWaqDraftTimer();
    waqDraftRequestId += 1;
    abortWaqDraftRequest();
    setComposerDisabled(true);
    let succeeded = false;
    try {
      const responsePromise = fetch(actionUrl(block.id, action), {
        method: "POST",
        headers: {
          "Content-Type": payload ? "application/json" : "text/plain;charset=UTF-8",
          "X-CSRFToken": getCsrfToken(),
          "X-Requested-With": "XMLHttpRequest",
        },
        body: payload ? JSON.stringify(payload) : "",
        credentials: "same-origin",
      });
      const minimumDelayPromise = options.minDurationMs
        ? new Promise((resolve) => window.setTimeout(resolve, options.minDurationMs))
        : Promise.resolve();
      const [response] = await Promise.all([responsePromise, minimumDelayPromise]);
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || "Unable to update right now.");
      }
      previewState = data.preview;
      activeBlockId = String(data.preview.active_block_id || block.id);
      clearAnsweredQuestionSelections();
      renderPreview(options.scrollMode || "bottom");
      succeeded = true;
    } catch (error) {
      setStatus(error.message || "Unable to update right now.");
      if (typeof options.onError === "function") {
        options.onError(error);
      }
    } finally {
      setComposerDisabled(false);
      if (options.focusComposer) {
        input?.focus();
      }
      syncComposerState();
    }
    return succeeded;
  }

  async function sendCourseChatQuestion(questionText, { clearComposer = false, focusComposer = true, closeSidebarOnMobile = false } = {}) {
    const trimmed = String(questionText || "").trim();
    if (!trimmed || requestInFlight) {
      return;
    }

    const block = currentBlock();
    if (!block) {
      return;
    }

    if (clearComposer && input) {
      input.value = "";
      resizeComposerInput();
      syncComposerState();
      updateComposerClearance();
    }

    if (closeSidebarOnMobile && isMobileSidebar()) {
      setSidebarOpen(false);
    }

    setOptimisticUserMessage(block.id, trimmed);
    setQuizLoading(block.id, true);
    renderTranscript();
    try {
      await postPreviewAction("chat", { question: trimmed }, { focusComposer, minDurationMs: 900 });
    } finally {
      setOptimisticUserMessage(block.id, "");
      setQuizLoading(block.id, false);
      renderTranscript();
    }
  }

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!input || requestInFlight) {
      return;
    }
    const trimmed = input.value.trim();
    const activeProject = currentProject();
    if (activeProject) {
      if (trimmed) {
        input.value = "";
        resizeComposerInput();
        syncComposerState();
        updateComposerClearance();
      }
      const block = currentBlock();
      if (block) {
        setOptimisticUserMessage(block.id, trimmed || "Hint");
        setQuizLoading(block.id, true);
        renderTranscript();
      }
      try {
        await postPreviewAction(
          "project_chat",
          { project_id: activeProject.id, message: trimmed },
          { focusComposer: true, minDurationMs: 600, scrollMode: "bottom" },
        );
      } finally {
        if (block) {
          setOptimisticUserMessage(block.id, "");
          setQuizLoading(block.id, false);
          renderTranscript();
        }
      }
      return;
    }
    const activeWaq = pendingWrittenQuestion();
    if (activeWaq) {
      if (!trimmed) {
        syncComposerState();
        return;
      }
      clearWaqDraftTimer();
      input.value = "";
      resizeComposerInput();
      syncComposerState();
      updateComposerClearance();
      const block = currentBlock();
      updateQuestionMessage(activeWaq.question_id, (message) => {
        message.draft_answer = "";
      });
      if (block) {
        setOptimisticUserMessage(block.id, trimmed);
        setQuizLoading(block.id, true);
        renderTranscript();
      }
      try {
        await postPreviewAction(
          "answer",
          { question_id: activeWaq.question_id, answer_text: trimmed },
          { focusComposer: true, minDurationMs: 900, scrollMode: "bottom" },
        );
      } finally {
        if (block) {
          setOptimisticUserMessage(block.id, "");
          setQuizLoading(block.id, false);
          renderTranscript();
        }
      }
      return;
    }

    if (trimmed) {
      await sendCourseChatQuestion(trimmed, { clearComposer: true, focusComposer: true });
      return;
    }
    input.blur();
    const block = currentBlock();
    if (block) {
      setQuizLoading(block.id, true);
      renderTranscript();
    }
    let quizRequestSucceeded = false;
    try {
        quizRequestSucceeded = await postPreviewAction("quiz", null, { minDurationMs: 2000, scrollMode: "question" });
    } finally {
      if (block) {
        setQuizLoading(block.id, false);
        renderTranscript(quizRequestSucceeded ? "question" : "preserve");
      }
    }
  });

  quizMenuTrigger?.addEventListener("click", () => {
    if (requestInFlight || input?.value.trim()) {
      return;
    }
    if (isQuizMenuOpen()) {
      closeQuizMenu();
      return;
    }
    openQuizMenu();
  });

  quizMenuPanel?.querySelectorAll("[data-quiz-type]").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (requestInFlight || button.disabled) {
        return;
      }
      const block = currentBlock();
      const questionType = button.dataset.quizType || "";
      closeQuizMenu();
      renderPreview("preserve");
      input?.blur();
      if (block) {
        setQuizLoading(block.id, true);
        renderTranscript();
      }
      let quizRequestSucceeded = false;
      try {
        quizRequestSucceeded = await postPreviewAction("quiz", { question_type: questionType }, { minDurationMs: 2000, scrollMode: "question" });
      } finally {
        if (block) {
          setQuizLoading(block.id, false);
          renderTranscript(quizRequestSucceeded ? "question" : "preserve");
        }
      }
    });
  });

  input?.addEventListener("keydown", async (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form?.requestSubmit();
    }
  });

  input?.addEventListener("input", () => {
    resizeComposerInput();
    syncComposerState();
    updateComposerClearance();
    const activeWaq = pendingWrittenQuestion();
    if (!activeWaq) {
      return;
    }
    updateQuestionMessage(activeWaq.question_id, (message) => {
      message.draft_answer = input.value;
      if (!input.value.trim()) {
        message.alignment_score = 0;
        message.alignment_state = "drafting";
      }
    });
    renderWaqAlignment(activeWaq);
    clearWaqDraftTimer();
    if (!input.value.trim()) {
      waqDraftRequestId += 1;
      abortWaqDraftRequest();
      return;
    }
    abortWaqDraftRequest({ clearLoading: false });
    const requestId = waqDraftRequestId + 1;
    waqDraftRequestId = requestId;
    setWaqAlignmentLoading(requestId);
    waqDraftDebounceTimer = window.setTimeout(() => {
      void postDraftAnswer(activeWaq.question_id, input.value, requestId).catch((error) => {
        if (error?.name === "AbortError") {
          return;
        }
        if (requestId === waqDraftRequestId) {
          clearWaqAlignmentLoading(requestId);
          setStatus("Unable to update alignment right now.");
        }
      });
    }, 120);
  });

  resourceButtons.forEach((button) => {
    button.addEventListener("click", () => {
      appendResourceMessage(button.dataset.previewResource || "");
    });
  });

  previewRoot.addEventListener("click", (event) => {
    const objectiveMenuTrigger = event.target.closest("[data-preview-objective-menu-trigger='true']");
    if (objectiveMenuTrigger && previewRoot.contains(objectiveMenuTrigger)) {
      event.preventDefault();
      event.stopPropagation();
      toggleObjectiveMenu(objectiveMenuTrigger.closest("[data-preview-objective-menu]"));
      return;
    }

    const objectiveGenerateButton = event.target.closest("[data-preview-objective-question-type]");
    if (objectiveGenerateButton && previewRoot.contains(objectiveGenerateButton)) {
      event.preventDefault();
      event.stopPropagation();
      if (requestInFlight || objectiveGenerateButton.disabled) {
        return;
      }
      const block = currentBlock();
      const objectiveId = Number(objectiveGenerateButton.dataset.objectiveId || 0);
      const questionType = objectiveGenerateButton.dataset.previewObjectiveQuestionType || "";
      closeObjectiveMenus();
      input?.blur();
      if (block) {
        setQuizLoading(block.id, true);
        renderTranscript();
      }
      void (async () => {
        let quizRequestSucceeded = false;
        try {
          quizRequestSucceeded = await postPreviewAction(
            "quiz",
            {
              question_type: questionType,
              learning_objective_id: objectiveId,
              force_new: true,
            },
            { minDurationMs: 2000, scrollMode: "question" },
          );
        } finally {
          if (block) {
            setQuizLoading(block.id, false);
            renderTranscript(quizRequestSucceeded ? "question" : "preserve");
          }
        }
      })();
      return;
    }

    const objectiveGuardrailButton = event.target.closest("[data-preview-objective-guardrail='true']");
    if (objectiveGuardrailButton && previewRoot.contains(objectiveGuardrailButton)) {
      event.preventDefault();
      event.stopPropagation();
      const block = currentBlock();
      const objectiveId = Number(objectiveGuardrailButton.dataset.objectiveId || 0);
      const objective = Array.isArray(block?.learning_objectives)
        ? block.learning_objectives.find((item) => Number(item.id || 0) === objectiveId)
        : null;
      closeObjectiveMenus();
      if (objective) {
        openGuardrailSheet(objective);
      }
      return;
    }

    const metricButton = event.target.closest("[data-preview-metric-button='true']");
    if (!metricButton || !previewRoot.contains(metricButton) || metricButton.disabled) {
      return;
    }
    event.preventDefault();
    appendMetricMessage(
      metricButton.dataset.metricKey || "",
      metricButton.dataset.metricScope || "block",
      metricButton.dataset.blockId || "",
    );
  });

  flagSheetScrim?.addEventListener("click", () => {
    closeFlagSheet();
  });

  flagSheetCloseButton?.addEventListener("click", () => {
    closeFlagSheet();
  });

  flagOnlyButton?.addEventListener("click", () => {
    void submitFlagSheet({ saveCorrection: false });
  });

  flagSaveButton?.addEventListener("click", () => {
    void submitFlagSheet({ saveCorrection: true });
  });

  flagInstructionInput?.addEventListener("input", () => {
    if (!flagSheetError?.hidden) {
      setFlagSheetError("");
    }
  });

  objectiveSheetScrim?.addEventListener("click", () => {
    closeGuardrailSheet();
  });

  objectiveSheetCloseButton?.addEventListener("click", () => {
    closeGuardrailSheet();
  });

  objectiveSheetSaveButton?.addEventListener("click", () => {
    void submitGuardrailSheet();
  });

  objectiveGuardrailInput?.addEventListener("input", () => {
    if (!objectiveSheetError?.hidden) {
      setGuardrailSheetError("");
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && flagSheetState) {
      closeFlagSheet();
    }
    if (event.key === "Escape" && guardrailSheetState) {
      closeGuardrailSheet();
    }
    if (event.key === "Escape") {
      closeObjectiveMenus();
    }
  });

  sidebarToggle?.addEventListener("click", () => {
    toggleSidebar();
  });

  sidebarScrim?.addEventListener("click", () => {
    setSidebarOpen(false);
  });

  document.addEventListener("click", (event) => {
    if (isTeacherPreview) {
      const objectiveMenuTarget = event.target instanceof Element ? event.target.closest("[data-preview-objective-menu]") : null;
      if (!objectiveMenuTarget) {
        closeObjectiveMenus();
      }
    }
    if (!quizMenu || !isQuizMenuOpen()) {
      return;
    }
    if (!quizMenu.contains(event.target)) {
      closeQuizMenu();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeQuizMenu();
      if (sidebarOpen) {
        setSidebarOpen(false);
      }
    }
  });

  window.addEventListener("resize", () => {
    updateComposerClearance();
    if (!isMobileSidebar()) {
      clearSidebarAutoCloseTimer(true);
    }
    applySidebarState();
  });
  restoreActiveBlockId();
  applySidebarState();
  renderPreview();
}
