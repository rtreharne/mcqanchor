function getCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

const VALIDATION_ROOM_CODE_ADJECTIVES = [
  "amber", "brisk", "calm", "daring", "eager", "fizzy", "gentle", "hidden", "icy", "jolly",
  "keen", "lively", "mellow", "nimble", "opal", "plucky", "quiet", "rapid", "silver", "tidy",
  "upbeat", "vivid", "witty", "young", "zesty",
];
const VALIDATION_ROOM_CODE_ANIMALS = [
  "ant", "badger", "crane", "dolphin", "egret", "fox", "gecko", "heron", "ibis", "jackal",
  "koala", "lemur", "newt", "otter", "panda", "quail", "rabbit", "seal", "tiger", "urchin",
  "viper", "walrus", "yak", "zebra",
];

const validationRoot = document.querySelector("[data-student-validate]");
const validationDataNode = document.getElementById("validation-session-data");
const validationSidebarDataNode = document.getElementById("validation-sidebar-data");

if (validationRoot && validationDataNode) {
  const actionUrlTemplate = validationRoot.dataset.sessionActionUrl || "";
  const isDemoMode = validationRoot.dataset.demoMode === "true";
  const demoVisitorKeyFromPage = String(validationRoot.dataset.demoVisitorKey || "").trim();
  const transcriptNode = validationRoot.querySelector(".validation-chat-transcript");
  const form = validationRoot.querySelector(".validation-chat-form");
  const input = validationRoot.querySelector("#validation-chat-input");
  const submitButton = validationRoot.querySelector("[data-validation-submit]");
  const nextButton = validationRoot.querySelector("[data-validation-next]");
  const answerRow = validationRoot.querySelector("[data-validation-answer-row]");
  const nextRow = validationRoot.querySelector("[data-validation-next-row]");
  const topCtaNode = validationRoot.querySelector("[data-validation-top-cta]");
  const topCtaCopyNode = validationRoot.querySelector("[data-validation-top-cta-copy]");
  const topCtaLinkNode = validationRoot.querySelector("[data-validation-top-cta-link]");
  const statusNode = validationRoot.querySelector(".preview-chat-status");
  const timerNode = validationRoot.querySelector("[data-validation-timer]");
  const progressNode = validationRoot.querySelector("[data-validation-progress-label]");
  const waqAlignment = validationRoot.querySelector("[data-waq-alignment]");
  const waqAlignmentLabel = validationRoot.querySelector("[data-waq-alignment-label]");
  const waqAlignmentFill = validationRoot.querySelector("[data-waq-alignment-fill]");
  const waqAlignmentLoader = validationRoot.querySelector("[data-waq-alignment-loader]");
  const sidebarToggle = validationRoot.querySelector("[data-preview-sidebar-toggle]");
  const sidebarScrim = validationRoot.querySelector("[data-preview-sidebar-scrim]");
  const previewSidebar = validationRoot.querySelector(".preview-sidebar");
  const sidebarSummary = validationRoot.querySelector("[data-preview-sidebar-summary]");
  const sidebarSummaryCopy = validationRoot.querySelector("[data-preview-sidebar-summary-copy]");
  const sidebarSummaryToggle = validationRoot.querySelector("[data-preview-sidebar-summary-toggle]");
  const launchLoader = validationRoot.querySelector("[data-validation-launch-loader]");
  const bookingOptionsTrigger = validationRoot.querySelector("[data-preview-booking-options-trigger]");
  const mobileSidebarMedia = window.matchMedia("(max-width: 980px)");
  const mobileChatMedia = window.matchMedia("(max-width: 640px)");

  let sessionState = JSON.parse(validationDataNode.textContent || "{}");
  const sidebarState = validationSidebarDataNode ? JSON.parse(validationSidebarDataNode.textContent || "{}") : {};
  let requestInFlight = false;
  let draftDebounceTimer = 0;
  let draftRequestId = 0;
  let alignmentLoadingRequestId = 0;
  let awayTimestamp = 0;
  let sidebarOpen = true;
  let sidebarSummaryExpanded = false;
  let sidebarSummaryFullText = "";
  let roomCodeClockOffsetMs = 0;
  let lastAuditOptionKey = "";
  let pendingSkipConfirmationQuestionId = 0;
  let activeAction = "";
  let practiceValidationNavigationTimer = 0;
  const roomCodeKeyCache = new Map();
  const practiceValidationLaunchDelayMs = 5000;
  const practiceValidationMobileSidebarDelayMs = 500;
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

  function isOfficialValidation() {
    return sessionState.mode === "digital_invigilation";
  }

  function hasActionEndpoint() {
    return !!actionUrlTemplate && !!sessionState.attempt_id;
  }

  function actionUrl(action) {
    return actionUrlTemplate.replace("ACTION", action);
  }

  function activeAttemptStorageKey() {
    return sessionState.attempt_id ? `quizanchor:validation-away:${sessionState.attempt_id}` : "";
  }

  function setStatus(message) {
    if (statusNode) {
      statusNode.textContent = message || "";
    }
  }

  function showLaunchLoader() {
    if (launchLoader) {
      launchLoader.hidden = false;
    }
  }

  function demoValidationVisitorStorageKey() {
    const courseKey = String(validationRoot.dataset.demoCourseKey || "");
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
        value = demoVisitorKeyFromPage || (window.crypto?.randomUUID ? window.crypto.randomUUID().replace(/-/g, "") : `${Date.now()}${Math.random().toString(16).slice(2)}`);
        window.localStorage.setItem(storageKey, value);
      }
      return value;
    } catch (_error) {
      return demoVisitorKeyFromPage;
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
    if (mobileSidebarMedia.matches) {
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

  function setAwayTimestamp(timestamp) {
    awayTimestamp = Number(timestamp || 0);
    const key = activeAttemptStorageKey();
    if (!key) {
      return;
    }
    try {
      if (awayTimestamp) {
        window.localStorage.setItem(key, String(awayTimestamp));
      } else {
        window.localStorage.removeItem(key);
      }
    } catch (_error) {
      return;
    }
  }

  function storedAwayTimestamp() {
    const key = activeAttemptStorageKey();
    if (!key) {
      return 0;
    }
    try {
      return Number(window.localStorage.getItem(key) || 0);
    } catch (_error) {
      return 0;
    }
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
    if (!sidebarSummaryCopy || !sidebarSummaryToggle) {
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

  function applySidebarState() {
    validationRoot.classList.toggle("is-sidebar-collapsed", !sidebarOpen);
    if (sidebarToggle) {
      sidebarToggle.setAttribute("aria-expanded", String(sidebarOpen));
      sidebarToggle.setAttribute("aria-label", sidebarOpen ? "Hide preview sidebar" : "Show preview sidebar");
    }
    if (sidebarScrim) {
      sidebarScrim.hidden = !mobileSidebarMedia.matches || !sidebarOpen;
    }
  }

  function setSidebarOpen(nextOpen) {
    sidebarOpen = !!nextOpen;
    applySidebarState();
  }

  function resizeInput() {
    if (!input) {
      return;
    }
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  }

  function updateComposerClearance() {
    if (!form || !validationRoot) {
      return;
    }
    validationRoot.style.setProperty("--preview-composer-clearance", `${form.offsetHeight + 20}px`);
  }

  function formatPercentage(value) {
    return `${Number(value || 0).toFixed(1)}%`;
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

  function normalizeAnswers(value) {
    return Array.isArray(value) ? value.filter(Boolean).map((item) => String(item).trim()).filter(Boolean) : [];
  }

  function currentPendingQuestion() {
    return sessionState.pending_question || null;
  }

  function currentPendingAudit() {
    return sessionState.pending_audit || null;
  }

  function currentInputMode() {
    const question = currentPendingQuestion();
    if (question && question.question_type === "waq" && !sessionState.completed) {
      return "waq";
    }
    return "none";
  }

  function shouldShowNextButton() {
    return (!!sessionState.next_available || !!currentPendingQuestion()) && !sessionState.completed;
  }

  function isFinalValidationPracticeStep() {
    const progress = sessionState.progress || {};
    return (
      String(sessionState.mode || "") === "validation_practice"
      && !!currentPendingQuestion()
      && !sessionState.completed
      && Number(progress.remaining_count || 0) <= 1
    );
  }

  function isSelectionQuestion(question) {
    return question && (question.question_type === "mcq" || question.question_type === "num" || question.question_type === "maq");
  }

  function pendingSelectionAnswers(question) {
    if (!isSelectionQuestion(question)) {
      return [];
    }
    return normalizeAnswers(question.selected_answers);
  }

  function scrollTranscriptToBottom() {
    transcriptNode?.scrollTo({ top: transcriptNode.scrollHeight, behavior: "auto" });
  }

  function scrollTranscriptToTop() {
    transcriptNode?.scrollTo({ top: 0, behavior: "auto" });
  }

  function latestPendingQuestionCard() {
    if (!transcriptNode) {
      return null;
    }
    return Array.from(transcriptNode.querySelectorAll("[data-preview-question='true']")).reverse().find(
      (element) => element.dataset.answered !== "true",
    ) || null;
  }

  function updateQuestionOverflowState(activeQuestion) {
    if (!transcriptNode || !activeQuestion || !activeQuestion.isConnected) {
      return;
    }
    const hint = activeQuestion.querySelector(".preview-question-overflow-hint");
    if (!hint) {
      return;
    }
    const answerRegion = activeQuestion.querySelector(".preview-message-options") || activeQuestion;
    const transcriptRect = transcriptNode.getBoundingClientRect();
    const answerRect = answerRegion.getBoundingClientRect();
    const visibilityTolerance = 6;
    const isFullyVisible = answerRect.bottom <= transcriptRect.bottom + visibilityTolerance;
    activeQuestion.classList.toggle("is-overflowing-question", !isFullyVisible);
    hint.hidden = isFullyVisible;
  }

  function syncQuestionViewport() {
    if (!transcriptNode) {
      return;
    }
    if (sessionState.completed) {
      scrollTranscriptToTop();
      return;
    }
    const questionCards = Array.from(transcriptNode.querySelectorAll("[data-preview-question='true']"));
    questionCards.forEach((card) => {
      card.classList.remove("is-overflowing-question");
      const hint = card.querySelector(".preview-question-overflow-hint");
      if (hint) {
        hint.hidden = true;
      }
    });

    const activeQuestion = latestPendingQuestionCard();
    if (activeQuestion) {
      const isTallerThanViewport = activeQuestion.offsetHeight > transcriptNode.clientHeight - 16;
      if (mobileChatMedia.matches || isTallerThanViewport) {
        transcriptNode.scrollTo({ top: Math.max(activeQuestion.offsetTop, 0), behavior: "auto" });
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
      return;
    }

    scrollTranscriptToBottom();
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
    const wrapper = document.createElement("div");
    wrapper.className = "preview-message-options preview-message-options--review";
    const selectedAnswers = normalizeAnswers(message.selected_answers?.length ? message.selected_answers : [message.selected_answer]);
    const correctAnswers = normalizeAnswers(message.correct_answers);
    (message.options || []).forEach((option, index) => {
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
      wrapper.appendChild(optionRow);
    });
    return wrapper;
  }

  function renderWrittenAnswerReview(message) {
    const review = document.createElement("div");
    review.className = "preview-written-answer-review";
    review.appendChild(richText.buildTextPanel("Your answer", message.submitted_text || "No answer submitted."));
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
    const code = document.createElement("code");
    code.className = "preview-message-code";
    code.textContent = String(message.code_snippet || "").replace(/^\n+|\n+$/g, "");
    pre.appendChild(code);
    wrapper.append(label, pre);
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

  function updateProgress() {
    if (!progressNode) {
      return;
    }
    const progress = sessionState.progress || {};
    progressNode.textContent = `${Math.min(progress.answered_count || 0, progress.total_questions || 0)} of ${progress.total_questions || 0} complete`;
  }

  function updateTimer() {
    if (!timerNode) {
      return;
    }
    const secondsRemaining = Math.max(0, Number(sessionState.time_remaining_seconds || 0));
    const minutes = Math.floor(secondsRemaining / 60);
    const seconds = secondsRemaining % 60;
    timerNode.textContent = `${minutes}:${String(seconds).padStart(2, "0")}`;
    timerNode.closest(".validation-meta-pill")?.classList.toggle(
      "is-urgent",
      !!sessionState.timer_running && secondsRemaining <= 60,
    );
  }

  function renderTopCta() {
    if (!topCtaNode || !topCtaLinkNode) {
      return;
    }
    const returnUrl = String(sessionState.practice_return_url || "").trim();
    const showReturnAction = !!returnUrl;
    topCtaNode.hidden = !showReturnAction;
    if (!showReturnAction) {
      topCtaLinkNode.href = "#";
      return;
    }
    if (topCtaCopyNode) {
      topCtaCopyNode.textContent = "Practice validation complete. Return to practice mode when you're ready to continue.";
    }
    topCtaLinkNode.href = returnUrl;
  }

  function currentServerTimeMs() {
    return Date.now() + roomCodeClockOffsetMs;
  }

  async function roomCodeKey(seed) {
    if (!roomCodeKeyCache.has(seed)) {
      const keyPromise = window.crypto.subtle.importKey(
        "raw",
        new TextEncoder().encode(seed),
        { name: "HMAC", hash: "SHA-256" },
        false,
        ["sign"],
      );
      roomCodeKeyCache.set(seed, keyPromise);
    }
    return roomCodeKeyCache.get(seed);
  }

  function bytesToHex(bytes) {
    return Array.from(new Uint8Array(bytes))
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
  }

  function roomCodeFromDigest(digest) {
    const adjective = VALIDATION_ROOM_CODE_ADJECTIVES[Number.parseInt(digest.slice(0, 8), 16) % VALIDATION_ROOM_CODE_ADJECTIVES.length];
    const animal = VALIDATION_ROOM_CODE_ANIMALS[Number.parseInt(digest.slice(8, 16), 16) % VALIDATION_ROOM_CODE_ANIMALS.length];
    return `${adjective}-${animal}`;
  }

  async function deriveRoomCode(seed, bucket, salt = "") {
    const key = await roomCodeKey(seed);
    const message = salt ? `${bucket}:${salt}` : `${bucket}`;
    const signature = await window.crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
    return roomCodeFromDigest(bytesToHex(signature));
  }

  function currentRoomCodeBucket(pendingAudit) {
    if (!pendingAudit) {
      return 0;
    }
    if (pendingAudit.code_bucket) {
      return Number(pendingAudit.code_bucket || 0);
    }
    return Math.floor(currentServerTimeMs() / 60000);
  }

  async function buildRoomCodeOptions(pendingAudit) {
    const config = sessionState.room_code_client || {};
    const seed = String(config.seed || "");
    const optionCount = Math.max(2, Number(config.option_count || 4));
    const bucket = currentRoomCodeBucket(pendingAudit);
    if (!seed || !bucket) {
      return [];
    }

    const correct = await deriveRoomCode(seed, bucket);
    const options = [correct];
    let distractorIndex = 1;
    while (options.length < optionCount) {
      const nextCode = await deriveRoomCode(seed, bucket, `option-${distractorIndex}`);
      distractorIndex += 1;
      if (!options.includes(nextCode)) {
        options.push(nextCode);
      }
    }

    const sortEntries = await Promise.all(
      options.map(async (code) => {
        const key = await deriveRoomCode(seed, bucket, `sort-${code}`);
        return [key, code];
      }),
    );
    sortEntries.sort((left, right) => String(left[0]).localeCompare(String(right[0])) || String(left[1]).localeCompare(String(right[1])));
    return sortEntries.map((entry) => entry[1]);
  }

  async function refreshPendingAuditOptions(force = false) {
    const pendingAudit = currentPendingAudit();
    const config = sessionState.room_code_client || {};
    if (!pendingAudit || !config.seed) {
      return;
    }
    const bucket = currentRoomCodeBucket(pendingAudit);
    const optionKey = `${pendingAudit.id}:${bucket}`;
    if (!force && lastAuditOptionKey === optionKey && Array.isArray(pendingAudit.options) && pendingAudit.options.length) {
      return;
    }
    const options = await buildRoomCodeOptions(pendingAudit);
    if (!currentPendingAudit() || Number(currentPendingAudit().id) !== Number(pendingAudit.id)) {
      return;
    }
    sessionState.pending_audit = {
      ...pendingAudit,
      options,
    };
    lastAuditOptionKey = optionKey;
    renderTranscript();
  }

  function setComposerDisabled(disabled) {
    requestInFlight = !!disabled;
    if (input) {
      input.disabled = disabled;
    }
    if (submitButton) {
      submitButton.disabled = disabled;
    }
    if (nextButton) {
      nextButton.disabled = disabled;
    }
    transcriptNode?.querySelectorAll(
      ".preview-answer-chip, .preview-question-submit, .preview-confirm-button, .preview-cta-action",
    ).forEach((button) => {
      button.disabled = disabled || button.dataset.locked === "true";
    });
  }

  function renderAlignment({ flash = false } = {}) {
    if (!waqAlignment || !waqAlignmentLabel || !waqAlignmentFill) {
      return;
    }
    waqAlignment.hidden = true;
    waqAlignment.dataset.loading = "false";
    if (waqAlignmentLoader) {
      waqAlignmentLoader.hidden = true;
    }
  }

  function syncControls() {
    const mode = currentInputMode();
    const pendingQuestion = currentPendingQuestion();
    const unansweredQuestionId = pendingQuestion && !pendingQuestion.answered ? Number(pendingQuestion.question_id || 0) : 0;
    const showWaqComposer = mode === "waq";
    if (!unansweredQuestionId || pendingSkipConfirmationQuestionId !== unansweredQuestionId) {
      pendingSkipConfirmationQuestionId = 0;
    }
    if (answerRow) {
      answerRow.hidden = !showWaqComposer;
      answerRow.setAttribute("aria-hidden", showWaqComposer ? "false" : "true");
    }
    if (nextRow) {
      nextRow.hidden = !shouldShowNextButton();
    }
    if (submitButton) {
      submitButton.hidden = !showWaqComposer;
      submitButton.textContent = requestInFlight && activeAction === "submit" ? "Sending..." : "Send";
      submitButton.disabled = requestInFlight || !showWaqComposer || !input?.value.trim();
    }
    if (nextButton) {
      nextButton.disabled = requestInFlight;
      nextButton.textContent = pendingSkipConfirmationQuestionId
        ? "Skip question"
        : (isFinalValidationPracticeStep() ? "Submit" : "Next");
    }
    if (input) {
      input.hidden = !showWaqComposer;
      if (showWaqComposer) {
        input.placeholder = pendingQuestion?.answered ? "Write more..." : "Write your answer...";
        const question = currentPendingQuestion();
        const draftValue = question?.draft_answer || "";
        if (document.activeElement !== input && input.value !== draftValue) {
          input.value = draftValue;
        }
      } else {
        input.value = "";
      }
    }
    resizeInput();
    renderAlignment();
    updateComposerClearance();
  }

  function mergedTranscript() {
    const isValidationFlow = (
      ["validation_practice", "digital_invigilation", "preview_validate"].includes(String(sessionState.mode || ""))
    );
    const shouldHideAnsweredHistory = isValidationFlow && !sessionState.completed;
    const transcriptMessages = Array.isArray(sessionState.transcript) ? sessionState.transcript : [];
    const answeredQuestionIds = transcriptMessages
      .filter((message) => message.kind === "question" && message.answered && message.question_id)
      .map((message) => Number(message.question_id))
      .filter((value) => Number.isFinite(value));
    const latestAnsweredQuestionId = answeredQuestionIds.length ? answeredQuestionIds[answeredQuestionIds.length - 1] : 0;
    const pendingQuestionId = Number(currentPendingQuestion()?.question_id || 0);
    const visibleQuestionId = Number(
      sessionState.next_available && latestAnsweredQuestionId
        ? latestAnsweredQuestionId
        : pendingQuestionId,
    );
    const transcript = (
      transcriptMessages
        ? transcriptMessages
          .filter((message) => {
            if (
              isValidationFlow
              && message.role === "user"
              && Number(message.question_id || 0)
              && String(message.question_type || "") !== "waq"
            ) {
              return false;
            }
            if (!shouldHideAnsweredHistory) {
              return true;
            }
            const messageQuestionId = Number(message.question_id || 0);
            if (Number.isFinite(messageQuestionId) && messageQuestionId) {
              return messageQuestionId === visibleQuestionId;
            }
            return true;
          })
          .map((message) => ({ ...message }))
        : []
    );
    const pendingQuestion = currentPendingQuestion();
    if (pendingQuestion) {
      const existingIndex = transcript.findIndex((message) => message.kind === "question" && Number(message.question_id) === Number(pendingQuestion.question_id));
      if (existingIndex >= 0) {
        transcript[existingIndex] = { ...transcript[existingIndex], ...pendingQuestion };
      } else {
        transcript.push({
          id: `pending-question-${pendingQuestion.question_id}`,
          role: "assistant",
          kind: "question",
          ...pendingQuestion,
        });
      }
    }
    const pendingAudit = currentPendingAudit();
    if (pendingAudit) {
      transcript.push({
        id: `pending-audit-${pendingAudit.id}`,
        role: "assistant",
        kind: "audit",
        audit_prompt_id: pendingAudit.id,
        text: pendingAudit.text,
        options: Array.isArray(pendingAudit.options) ? pendingAudit.options : [],
      });
    }
    return transcript;
  }

  function renderMessage(message) {
    const article = document.createElement("article");
    const roleClass = message.role === "user" ? "preview-message--user" : "preview-message--assistant";
    const feedbackClass = message.kind === "feedback" ? (message.correct ? " preview-feedback--correct" : " preview-feedback--incorrect") : "";
    const summaryClass = message.kind === "summary" ? " preview-message--summary" : "";
    article.className = `preview-message ${roleClass}${feedbackClass}${summaryClass}`;

    if (message.kind === "question") {
      article.dataset.previewQuestion = "true";
      article.dataset.questionId = String(message.question_id || "");
      article.dataset.answered = message.answered ? "true" : "false";
      const callout = document.createElement("div");
      callout.className = "preview-question-callout";
      callout.textContent = questionTypeLabel(message);
      article.appendChild(callout);

      if (message.block_label) {
        const blockLabel = document.createElement("div");
        blockLabel.className = "preview-question-block-label";
        blockLabel.textContent = String(message.block_label);
        article.appendChild(blockLabel);
      }

      if (message.review_visible && message.answered && message.is_correct !== null && message.is_correct !== undefined) {
        const grade = document.createElement("div");
        grade.className = `preview-question-grade${message.is_correct ? " is-correct" : " is-incorrect"}`;
        grade.textContent = message.is_correct ? "Correct" : "Incorrect";
        article.appendChild(grade);
      }

      appendFormattedMessageContent(article, questionStemText(message));
      appendQuestionCodeSnippet(article, message);

      if (message.question_type === "waq" && !message.answered) {
        const helper = document.createElement("p");
        helper.className = "preview-message-sources";
        helper.textContent = "Type your answer in the fixed box below.";
        article.appendChild(helper);
      } else if (Array.isArray(message.options) && message.options.length && !message.answered) {
        const overflowHint = document.createElement("div");
        overflowHint.className = "preview-question-overflow-hint";
        overflowHint.hidden = true;
        overflowHint.textContent = "Scroll to see all answers.";
        article.appendChild(overflowHint);

        const optionsWrapper = document.createElement("div");
        optionsWrapper.className = "preview-message-options";
        const selectedAnswers = normalizeAnswers(message.selected_answers);
        if (message.question_type === "maq") {
          (message.options || []).forEach((option, index) => {
            const optionButton = document.createElement("button");
            optionButton.type = "button";
            optionButton.className = `preview-answer-chip preview-answer-chip--maq${selectedAnswers.includes(option) ? " is-selected" : ""}`;
            optionButton.innerHTML = `
              <span class="preview-answer-chip-checkbox" aria-hidden="true">${selectedAnswers.includes(option) ? "✓" : ""}</span>
              <span class="preview-answer-chip-label">${optionLabel(index)}</span>
              <span class="preview-answer-chip-text"></span>
            `;
            richText.appendInlineText(optionButton.querySelector(".preview-answer-chip-text"), option);
            optionButton.addEventListener("click", () => {
              setStatus("");
              pendingSkipConfirmationQuestionId = 0;
              const nextSelected = normalizeAnswers(message.selected_answers);
              if (nextSelected.includes(option)) {
                message.selected_answers = nextSelected.filter((item) => item !== option);
              } else {
                message.selected_answers = [...nextSelected, option];
              }
              sessionState.pending_question = { ...(currentPendingQuestion() || {}), selected_answers: message.selected_answers };
              renderSession();
            });
            optionsWrapper.appendChild(optionButton);
          });
        } else {
          (message.options || []).forEach((option, index) => {
            const optionButton = document.createElement("button");
            optionButton.type = "button";
            const isSelected = selectedAnswers.includes(option);
            optionButton.className = `preview-answer-chip preview-answer-chip--maq${isSelected ? " is-selected" : ""}`;
            optionButton.innerHTML = `
              <span class="preview-answer-chip-checkbox" aria-hidden="true">${isSelected ? "•" : ""}</span>
              <span class="preview-answer-chip-label">${optionLabel(index)}</span>
              <span class="preview-answer-chip-text"></span>
            `;
            richText.appendInlineText(optionButton.querySelector(".preview-answer-chip-text"), option);
            optionButton.addEventListener("click", () => {
              setStatus("");
              pendingSkipConfirmationQuestionId = 0;
              message.selected_answers = [option];
              sessionState.pending_question = { ...(currentPendingQuestion() || {}), selected_answers: message.selected_answers };
              renderSession();
            });
            optionsWrapper.appendChild(optionButton);
          });
        }
        article.appendChild(optionsWrapper);
      } else if (message.question_type === "waq" && message.review_visible && (message.answered || message.submitted_text)) {
        article.appendChild(renderWrittenAnswerReview(message));
      } else if (message.answered && message.review_visible && Array.isArray(message.correct_answers) && message.correct_answers.length) {
        article.appendChild(renderAnsweredOptions(message));
      }
      richText.renderMath(article);
      return article;
    }

    if (message.kind === "audit") {
      const callout = document.createElement("div");
      callout.className = "preview-question-callout";
      callout.textContent = "Session code";
      article.appendChild(callout);
      appendFormattedMessageContent(article, message.text || "");
      if (Array.isArray(message.options) && message.options.length) {
        const optionsWrapper = document.createElement("div");
        optionsWrapper.className = "preview-message-options";
        message.options.forEach((option, index) => {
          const optionButton = document.createElement("button");
          optionButton.type = "button";
          optionButton.className = "preview-answer-chip";
          optionButton.innerHTML = `
            <span class="preview-answer-chip-label">${optionLabel(index)}</span>
            <span class="preview-answer-chip-text"></span>
          `;
          richText.appendInlineText(optionButton.querySelector(".preview-answer-chip-text"), option);
          optionButton.addEventListener("click", () => {
            void postAction("submit", { audit_prompt_id: message.audit_prompt_id, answer_text: option });
          });
          optionsWrapper.appendChild(optionButton);
        });
        article.appendChild(optionsWrapper);
      }
      richText.renderMath(article);
      return article;
    }

    if (message.kind === "confirm") {
      appendFormattedMessageContent(article, message.text || "");
      const actions = document.createElement("div");
      actions.className = "preview-message-actions";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button secondary preview-confirm-button";
      button.textContent = message.button_label || "Confirm";
      button.addEventListener("click", () => {
        void postAction("confirm", {});
      });
      actions.appendChild(button);
      article.appendChild(actions);
      return article;
    }

    if (message.kind === "cta") {
      appendFormattedMessageContent(article, message.text || "");
      const actions = Array.isArray(message.actions) ? message.actions : [];
      if (actions.length) {
        const actionRow = document.createElement("div");
        actionRow.className = "preview-message-actions";
        actions.forEach((action) => {
          const link = document.createElement("a");
          link.className = `button${action.style === "secondary" ? " secondary" : ""} preview-cta-action`;
          link.href = action.url;
          link.textContent = action.label;
          actionRow.appendChild(link);
        });
        article.appendChild(actionRow);
      }
      richText.renderMath(article);
      return article;
    }

    if (message.kind === "booking_options") {
      appendFormattedMessageContent(article, message.text || "");
      const sessions = Array.isArray(message.sessions) ? message.sessions : [];
      if (sessions.length) {
        const list = document.createElement("div");
        list.className = "preview-booking-option-list";
        sessions.forEach((session) => {
          const card = document.createElement("article");
          card.className = "preview-booking-option-card";

          const header = document.createElement("div");
          header.className = "preview-booking-option-head";
          const heading = document.createElement("strong");
          heading.textContent = session.title || "Validation session";
          const datetime = document.createElement("span");
          datetime.textContent = session.datetime || "";
          header.appendChild(heading);
          header.appendChild(datetime);
          card.appendChild(header);

          const meta = document.createElement("div");
          meta.className = "preview-booking-option-meta";
          [
            session.location,
            `${Number(session.spaces_left || 0)} spaces left`,
            `${Number(session.recent_booking_count || 0)} booked in last 24h`,
            `${Number(session.question_count || 0)} questions`,
          ].filter(Boolean).forEach((value) => {
            const pill = document.createElement("span");
            pill.textContent = value;
            meta.appendChild(pill);
          });
          card.appendChild(meta);

          const actions = document.createElement("div");
          actions.className = "preview-message-actions";
          const link = document.createElement("a");
          link.className = "button secondary preview-cta-action";
          link.href = session.url || "#";
          link.textContent = "Book this session";
          actions.appendChild(link);
          card.appendChild(actions);
          list.appendChild(card);
        });
        article.appendChild(list);
      }
      richText.renderMath(article);
      return article;
    }

    if (message.kind === "feedback" && typeof message.correct === "boolean") {
      const grade = document.createElement("div");
      grade.className = `preview-question-grade${message.correct ? " is-correct" : " is-incorrect"}`;
      grade.textContent = message.correct ? "Correct" : "Incorrect";
      article.appendChild(grade);
    }

    appendFormattedMessageContent(article, message.text || "");
    richText.renderMath(article);
    return article;
  }

  function showBookingOptionsInChat() {
    const sessions = Array.isArray(sidebarState.booking_sessions) ? sidebarState.booking_sessions : [];
    if (!sessions.length) {
      return;
    }
    const message = {
      id: "preview-booking-options",
      role: "assistant",
      kind: "booking_options",
      text: sessions.length === 1
        ? "This validation session is open for booking."
        : "Choose one of the currently bookable validation sessions.",
      sessions,
    };
    sessionState.transcript = Array.isArray(sessionState.transcript)
      ? sessionState.transcript.filter((entry) => String(entry.id || "") !== "preview-booking-options")
      : [];
    sessionState.transcript.push(message);
    renderTranscript();
    scrollTranscriptToBottom();
    if (mobileSidebarMedia.matches) {
      setSidebarOpen(false);
    }
  }

  function renderTranscript() {
    if (!transcriptNode) {
      return;
    }
    transcriptNode.innerHTML = "";
    mergedTranscript().forEach((message) => {
      transcriptNode.appendChild(renderMessage(message));
    });
    syncQuestionViewport();
  }

  function renderSession() {
    if (sessionState.completed) {
      setAwayTimestamp(0);
    }
    const roomCodeClient = sessionState.room_code_client || {};
    roomCodeClockOffsetMs = roomCodeClient.server_now_ms ? roomCodeClient.server_now_ms - Date.now() : 0;
    renderTopCta();
    renderTranscript();
    updateTimer();
    updateProgress();
    syncControls();
    void refreshPendingAuditOptions();
  }

  function setAlignmentLoading(requestId) {
    alignmentLoadingRequestId = requestId;
    renderAlignment();
  }

  function clearAlignmentLoading(requestId = 0) {
    if (requestId && alignmentLoadingRequestId !== requestId) {
      return;
    }
    alignmentLoadingRequestId = 0;
    renderAlignment();
  }

  function queueDraftCheck() {
    return;
  }

  function postAction(action, payload) {
    if (!hasActionEndpoint()) {
      return Promise.resolve();
    }
    activeAction = String(action || "");
    setStatus("");
    setComposerDisabled(true);
    return fetch(actionUrl(action), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCsrfToken(),
        "X-Requested-With": "XMLHttpRequest",
        ...(isDemoMode && demoValidationVisitorKey() ? { "X-Demo-Visitor-Key": demoValidationVisitorKey() } : {}),
      },
      body: JSON.stringify(payload || {}),
      credentials: "same-origin",
    })
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || "Something went wrong.");
        }
        if (data.session) {
          sessionState = data.session;
          pendingSkipConfirmationQuestionId = 0;
          lastAuditOptionKey = "";
          renderSession();
          return;
        }
      })
      .catch((error) => {
        setStatus(error.message || "Something went wrong.");
      })
      .finally(() => {
        activeAction = "";
        setComposerDisabled(false);
        syncControls();
      });
  }

  if (isDemoMode) {
    demoValidationVisitorKey();
  }

  function reportAwayDuration(millisecondsAway) {
    if (!isOfficialValidation() || sessionState.completed || !hasActionEndpoint()) {
      return Promise.resolve();
    }
    const awaySeconds = Math.max(0, Math.round(Number(millisecondsAway || 0) / 1000));
    if (!awaySeconds) {
      return Promise.resolve();
    }
    return postAction("presence", { away_seconds: awaySeconds });
  }

  input?.addEventListener("input", () => {
    setStatus("");
    pendingSkipConfirmationQuestionId = 0;
    resizeInput();
    syncControls();
    const question = currentPendingQuestion();
    if (question && question.question_type === "waq") {
      sessionState.pending_question = { ...question, draft_answer: input.value };
    }
  });

  input?.addEventListener("paste", (event) => {
    if (currentInputMode() !== "waq") {
      return;
    }
    event.preventDefault();
    setStatus("Pasting is disabled for written validation answers.");
  });

  nextButton?.addEventListener("click", () => {
    if (requestInFlight || !shouldShowNextButton()) {
      return;
    }
    const question = currentPendingQuestion();
    const unansweredQuestionId = question && !question.answered ? Number(question.question_id || 0) : 0;
    if (unansweredQuestionId) {
      const selectedAnswers = pendingSelectionAnswers(question);
      if (selectedAnswers.length) {
        if (question.question_type === "mcq" || question.question_type === "num") {
          void postAction("submit", { question_id: unansweredQuestionId, answer: selectedAnswers[0] });
        } else {
          void postAction("submit", { question_id: unansweredQuestionId, answers: selectedAnswers });
        }
        return;
      }
      if (pendingSkipConfirmationQuestionId !== unansweredQuestionId) {
        pendingSkipConfirmationQuestionId = unansweredQuestionId;
        setStatus("You have not answered this question. If you continue, you will not be able to return to it.");
        syncControls();
        return;
      }
      void postAction("skip", { question_id: unansweredQuestionId });
      return;
    }
    void postAction("next", {});
  });

  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (requestInFlight || currentInputMode() !== "waq") {
      return;
    }
    const question = currentPendingQuestion();
    if (!question || !input?.value.trim()) {
      return;
    }
    void postAction("submit", {
      question_id: question.question_id,
      answer_text: input.value,
    }).then(() => {
      if (input) {
        input.value = "";
        resizeInput();
        input.focus();
      }
    });
  });

  transcriptNode?.addEventListener("copy", (event) => {
    if (!isOfficialValidation()) {
      return;
    }
    event.preventDefault();
    setStatus("Copying validation questions is not allowed.");
  });

  transcriptNode?.addEventListener("cut", (event) => {
    if (!isOfficialValidation()) {
      return;
    }
    event.preventDefault();
    setStatus("Copying validation questions is not allowed.");
  });

  document.addEventListener("visibilitychange", () => {
    if (!isOfficialValidation() || sessionState.completed) {
      return;
    }
    if (document.hidden) {
      setAwayTimestamp(Date.now());
      return;
    }
    const leftAt = awayTimestamp || storedAwayTimestamp();
    setAwayTimestamp(0);
    if (leftAt) {
      void reportAwayDuration(Date.now() - leftAt);
    }
  });

  window.addEventListener("pagehide", () => {
    if (!isOfficialValidation() || sessionState.completed) {
      return;
    }
    setAwayTimestamp(Date.now());
  });

  window.addEventListener("pageshow", () => {
    if (!isOfficialValidation() || sessionState.completed) {
      return;
    }
    const leftAt = storedAwayTimestamp();
    if (leftAt) {
      setAwayTimestamp(0);
      void reportAwayDuration(Date.now() - leftAt);
    }
  });

  sidebarSummaryToggle?.addEventListener("click", () => {
    sidebarSummaryExpanded = !sidebarSummaryExpanded;
    renderSidebarSummary();
  });

  bookingOptionsTrigger?.addEventListener("click", () => {
    showBookingOptionsInChat();
  });

  validationRoot.addEventListener("click", (event) => {
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

  sidebarToggle?.addEventListener("click", () => {
    setSidebarOpen(!sidebarOpen);
  });
  sidebarScrim?.addEventListener("click", () => {
    setSidebarOpen(false);
  });
  window.addEventListener("resize", () => {
    if (!mobileSidebarMedia.matches) {
      setSidebarOpen(true);
    } else {
      applySidebarState();
    }
    syncQuestionViewport();
    updateComposerClearance();
  });

  function tick() {
    if (sessionState.show_timer && sessionState.timer_running) {
      sessionState.time_remaining_seconds = Math.max(0, Number(sessionState.time_remaining_seconds || 0) - 1);
      updateTimer();
    }
    if (currentPendingAudit()) {
      void refreshPendingAuditOptions();
    }
  }

  if (sidebarSummaryCopy) {
    sidebarSummaryFullText = sidebarSummaryCopy.textContent.trim();
    renderSidebarSummary();
  }
  applySidebarState();
  renderSession();
  window.setInterval(tick, 1000);
}
