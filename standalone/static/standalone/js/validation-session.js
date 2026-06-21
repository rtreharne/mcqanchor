function validationCsrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

const validationRoot = document.querySelector("[data-validation-session]");
const validationDataNode = document.getElementById("validation-session-data");

if (validationRoot && validationDataNode) {
  const actionUrlTemplate = validationRoot.dataset.actionUrlTemplate || "";
  const transcriptNode = validationRoot.querySelector(".validation-chat-transcript");
  const form = validationRoot.querySelector(".validation-chat-form");
  const input = validationRoot.querySelector("#validation-chat-input");
  const submitButton = validationRoot.querySelector("[data-validation-submit]");
  const statusNode = validationRoot.querySelector(".preview-chat-status");
  const timerNode = validationRoot.querySelector("[data-validation-timer]");
  const progressNode = validationRoot.querySelector("[data-validation-progress-label]");
  const roomCodeNode = validationRoot.querySelector("[data-validation-room-code]");
  const roomCodeValue = validationRoot.querySelector("[data-validation-room-code-value]");
  const roomCodeCountdown = validationRoot.querySelector("[data-validation-room-code-countdown]");
  const waqAlignment = validationRoot.querySelector("[data-waq-alignment]");
  const waqAlignmentLabel = validationRoot.querySelector("[data-waq-alignment-label]");
  const waqAlignmentFill = validationRoot.querySelector("[data-waq-alignment-fill]");
  const waqAlignmentLoader = validationRoot.querySelector("[data-waq-alignment-loader]");

  let sessionState = JSON.parse(validationDataNode.textContent || "{}");
  let requestInFlight = false;
  let draftDebounceTimer = 0;
  let draftRequestId = 0;
  let alignmentLoadingRequestId = 0;
  let awayTimestamp = 0;
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

  function awayStorageKey() {
    return sessionState.attempt_id ? `quizanchor:validation-away:${sessionState.attempt_id}` : "";
  }

  function actionUrl(action) {
    return actionUrlTemplate.replace("ACTION", action);
  }

  function setStatus(message) {
    if (statusNode) {
      statusNode.textContent = message || "";
    }
  }

  function setAwayTimestamp(timestamp) {
    awayTimestamp = Number(timestamp || 0);
    const key = awayStorageKey();
    if (!key) {
      return;
    }
    try {
      if (awayTimestamp) {
        window.localStorage.setItem(key, String(awayTimestamp));
      } else {
        window.localStorage.removeItem(key);
      }
    } catch (error) {
      return;
    }
  }

  function storedAwayTimestamp() {
    const key = awayStorageKey();
    if (!key) {
      return 0;
    }
    try {
      return Number(window.localStorage.getItem(key) || 0);
    } catch (error) {
      return 0;
    }
  }

  function resizeInput() {
    if (!input) {
      return;
    }
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
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
        return "Standard MCQ";
    }
  }

  function currentPendingQuestion() {
    return sessionState.pending_question || null;
  }

  function currentPendingAudit() {
    return sessionState.pending_audit || null;
  }

  function currentInputMode() {
    if (currentPendingAudit()) {
      return "audit";
    }
    if (currentPendingQuestion()?.question_type === "waq") {
      return "waq";
    }
    return "none";
  }

  function updateProgress() {
    const progress = sessionState.progress || {};
    if (progressNode) {
      progressNode.textContent = `${Math.min(progress.answered_count || 0, progress.total_questions || 0)}/${progress.total_questions || 0}`;
    }
  }

  function updateRoomCode() {
    const roomCode = sessionState.room_code;
    const show = !!roomCode && roomCodeNode && roomCodeValue && roomCodeCountdown;
    if (!show) {
      if (roomCodeNode) {
        roomCodeNode.hidden = true;
      }
      return;
    }
    roomCodeNode.hidden = false;
    roomCodeValue.textContent = roomCode.code || "";
    roomCodeCountdown.textContent = `Refreshes in ${Number(roomCode.seconds_remaining || 0)}s`;
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

  function tick() {
    if (sessionState.timer_running) {
      sessionState.time_remaining_seconds = Math.max(0, Number(sessionState.time_remaining_seconds || 0) - 1);
    }
    if (sessionState.room_code) {
      sessionState.room_code.seconds_remaining = Math.max(0, Number(sessionState.room_code.seconds_remaining || 0) - 1);
    }
    updateTimer();
    updateRoomCode();
  }

  function mergedTranscript() {
    const transcript = Array.isArray(sessionState.transcript) ? sessionState.transcript.map((message) => ({ ...message })) : [];
    const pendingQuestion = currentPendingQuestion();
    const pendingQuestionIndex = pendingQuestion
      ? transcript.findIndex((message) => message.kind === "question" && Number(message.question_id) === Number(pendingQuestion.question_id) && !message.answered)
      : -1;
    if (pendingQuestion && pendingQuestionIndex >= 0) {
      transcript[pendingQuestionIndex] = {
        ...transcript[pendingQuestionIndex],
        ...pendingQuestion,
      };
    } else if (pendingQuestion) {
      transcript.push({
        id: `pending-question-${pendingQuestion.question_id}`,
        role: "assistant",
        kind: "question",
        ...pendingQuestion,
      });
    }
    const pendingAudit = currentPendingAudit();
    const pendingAuditIndex = pendingAudit
      ? transcript.findIndex((message) => message.kind === "audit" && Number(message.audit_prompt_id) === Number(pendingAudit.id))
      : -1;
    if (pendingAudit && pendingAuditIndex >= 0) {
      transcript[pendingAuditIndex] = {
        ...transcript[pendingAuditIndex],
        ...pendingAudit,
      };
    } else if (pendingAudit) {
      transcript.push({
        id: `pending-audit-${pendingAudit.id}`,
        role: "assistant",
        kind: "audit",
        audit_prompt_id: pendingAudit.id,
        text: pendingAudit.text,
        placeholder: pendingAudit.placeholder,
      });
    }
    return transcript;
  }

  function normalizeAnswers(value) {
    if (Array.isArray(value)) {
      return value.filter(Boolean).map((item) => String(item).trim()).filter(Boolean);
    }
    return value ? [String(value).trim()] : [];
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
    const selectedAnswers = normalizeAnswers(message.selected_answers);
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
    const code = document.createElement("code");
    code.className = "preview-message-code";
    code.textContent = String(message.code_snippet || "").replace(/^\n+|\n+$/g, "");
    pre.appendChild(code);
    wrapper.append(label, pre);
    container.appendChild(wrapper);
  }

  function postAction(action, payload) {
    setStatus("");
    setComposerDisabled(true);
    return fetch(actionUrl(action), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": validationCsrfToken(),
      },
      body: JSON.stringify(payload || {}),
    })
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || "Something went wrong.");
        }
        if (data.session) {
          sessionState = data.session;
          renderSession();
        }
        if (data.alignment) {
          const question = currentPendingQuestion();
          if (question && Number(question.question_id) === Number(data.alignment.question_id)) {
            sessionState.pending_question = {
              ...question,
              draft_answer: data.alignment.answer_text,
              alignment_score: data.alignment.alignment_score,
              alignment_state: data.alignment.alignment_state,
            };
            renderAlignment();
          }
        }
      })
      .catch((error) => {
        setStatus(error.message || "Something went wrong.");
      })
      .finally(() => {
        setComposerDisabled(false);
      });
  }

  function reportAwayDuration(millisecondsAway) {
    if (!isOfficialValidation() || sessionState.completed) {
      return Promise.resolve();
    }
    const awaySeconds = Math.max(0, Math.round(Number(millisecondsAway || 0) / 1000));
    if (!awaySeconds) {
      return Promise.resolve();
    }
    return postAction("presence", { away_seconds: awaySeconds });
  }

  function setComposerDisabled(disabled) {
    requestInFlight = !!disabled;
    if (input) {
      input.disabled = disabled;
    }
    if (submitButton) {
      submitButton.disabled = disabled;
    }
    transcriptNode?.querySelectorAll(".preview-answer-chip, .preview-question-submit").forEach((button) => {
      button.disabled = disabled || button.dataset.locked === "true";
    });
  }

  function renderAlignment({ flash = false } = {}) {
    const question = currentPendingQuestion();
    const visible = question && question.question_type === "waq" && !sessionState.completed;
    if (!waqAlignment || !waqAlignmentLabel || !waqAlignmentFill) {
      return;
    }
    waqAlignment.hidden = !visible;
    if (!visible) {
      if (waqAlignmentLoader) {
        waqAlignmentLoader.hidden = true;
      }
      return;
    }
    const score = Number(question.alignment_score || 0);
    const state = question.alignment_state || "drafting";
    waqAlignment.dataset.state = state;
    waqAlignmentFill.style.width = `${Math.max(0, Math.min(score, 100))}%`;
    waqAlignmentLabel.textContent = !question.draft_answer
      ? "Start typing"
      : (state === "aligned" ? `Aligned ${formatPercentage(score)}` : `${formatPercentage(score)} building`);
    if (waqAlignmentLoader) {
      waqAlignmentLoader.hidden = alignmentLoadingRequestId === 0;
    }
    if (flash) {
      waqAlignment.classList.remove("is-flashing");
      window.requestAnimationFrame(() => {
        waqAlignment.classList.add("is-flashing");
        window.setTimeout(() => waqAlignment.classList.remove("is-flashing"), 700);
      });
    }
  }

  function syncComposer() {
    const mode = currentInputMode();
    validationRoot.classList.toggle("is-waq-mode", mode === "waq");
    validationRoot.classList.toggle("is-audit-mode", mode === "audit");
    if (!input || !submitButton) {
      return;
    }
    if (mode === "audit") {
      input.placeholder = currentPendingAudit()?.placeholder || "Enter the room code...";
      input.hidden = false;
      submitButton.hidden = false;
      submitButton.textContent = "Submit code";
      submitButton.disabled = requestInFlight || !input.value.trim();
    } else if (mode === "waq") {
      input.placeholder = "Write your answer...";
      input.hidden = false;
      submitButton.hidden = false;
      submitButton.textContent = "Submit answer";
      const question = currentPendingQuestion();
      const draftValue = question?.draft_answer || "";
      if (document.activeElement !== input) {
        input.value = draftValue;
      }
      submitButton.disabled = requestInFlight || !input.value.trim();
    } else {
      input.value = "";
      input.hidden = true;
      submitButton.hidden = true;
    }
    resizeInput();
    renderAlignment();
  }

  function renderMessage(message) {
    const article = document.createElement("article");
    const roleClass = message.role === "user" ? "preview-message--user" : "preview-message--assistant";
    const feedbackClass =
      message.kind === "feedback" ? (message.correct ? " preview-feedback--correct" : " preview-feedback--incorrect") : "";
    article.className = `preview-message ${roleClass}${feedbackClass}`;

    if (message.kind === "question") {
      article.dataset.previewQuestion = "true";
      const callout = document.createElement("div");
      callout.className = "preview-question-callout";
      callout.textContent = questionTypeLabel(message);
      article.appendChild(callout);
      appendFormattedMessageContent(article, questionStemText(message));
      appendQuestionCodeSnippet(article, message);
      if (message.question_type === "waq" && !message.answered) {
        const helper = document.createElement("p");
        helper.className = "preview-message-sources";
        helper.textContent = "Type your answer below.";
        article.appendChild(helper);
      }
      if (Array.isArray(message.options) && message.options.length && !message.answered) {
        const optionsWrapper = document.createElement("div");
        optionsWrapper.className = "preview-message-options";
        if (message.question_type === "maq") {
          const selectedAnswers = normalizeAnswers(message.selected_answers);
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
          const submitRow = document.createElement("div");
          submitRow.className = "preview-question-submit-row";
          const submitSelection = document.createElement("button");
          submitSelection.type = "button";
          submitSelection.className = "button secondary preview-question-submit";
          submitSelection.textContent = "Submit";
          submitSelection.dataset.locked = normalizeAnswers(message.selected_answers).length ? "false" : "true";
          submitSelection.disabled = requestInFlight || !normalizeAnswers(message.selected_answers).length;
          submitSelection.addEventListener("click", () => {
            void postAction("submit", {
              question_id: message.question_id,
              answers: normalizeAnswers(message.selected_answers),
            });
          });
          submitRow.appendChild(submitSelection);
          optionsWrapper.appendChild(submitRow);
        } else {
          (message.options || []).forEach((option, index) => {
            const optionButton = document.createElement("button");
            optionButton.type = "button";
            optionButton.className = "preview-answer-chip";
            optionButton.innerHTML = `
              <span class="preview-answer-chip-label">${optionLabel(index)}</span>
              <span class="preview-answer-chip-text"></span>
            `;
            richText.appendInlineText(optionButton.querySelector(".preview-answer-chip-text"), option);
            optionButton.addEventListener("click", () => {
              void postAction("submit", { question_id: message.question_id, answer: option });
            });
            optionsWrapper.appendChild(optionButton);
          });
        }
        article.appendChild(optionsWrapper);
      } else if (message.question_type === "waq" && (message.answered || message.submitted_text)) {
        article.appendChild(renderWrittenAnswerReview(message));
      } else if (message.answered && Array.isArray(message.correct_answers) && message.correct_answers.length) {
        article.appendChild(renderAnsweredOptions(message));
      }
      richText.renderMath(article);
      return article;
    }

    if (message.kind === "audit") {
      const callout = document.createElement("div");
      callout.className = "preview-question-callout";
      callout.textContent = "Room-code audit";
      article.appendChild(callout);
    }

    appendFormattedMessageContent(article, message.text);
    richText.renderMath(article);
    return article;
  }

  function renderTranscript() {
    if (!transcriptNode) {
      return;
    }
    transcriptNode.innerHTML = "";
    mergedTranscript().forEach((message) => {
      transcriptNode.appendChild(renderMessage(message));
    });
    transcriptNode.scrollTop = transcriptNode.scrollHeight;
  }

  function renderSession() {
    if (sessionState.completed) {
      setAwayTimestamp(0);
    }
    renderTranscript();
    updateTimer();
    updateProgress();
    updateRoomCode();
    syncComposer();
  }

  function queueDraftCheck() {
    const question = currentPendingQuestion();
    if (!question || question.question_type !== "waq" || !input) {
      return;
    }
    window.clearTimeout(draftDebounceTimer);
    const requestId = ++draftRequestId;
    alignmentLoadingRequestId = requestId;
    renderAlignment();
    draftDebounceTimer = window.setTimeout(() => {
      void postAction("draft_answer", {
        question_id: question.question_id,
        answer_text: input.value,
      }).finally(() => {
        if (alignmentLoadingRequestId === requestId) {
          alignmentLoadingRequestId = 0;
          renderAlignment({ flash: currentPendingQuestion()?.alignment_state === "aligned" });
        }
      });
    }, 260);
  }

  input?.addEventListener("input", () => {
    resizeInput();
    syncComposer();
    const question = currentPendingQuestion();
    if (question && question.question_type === "waq") {
      sessionState.pending_question = { ...question, draft_answer: input.value };
      queueDraftCheck();
      return;
    }
  });

  input?.addEventListener("paste", (event) => {
    if (currentInputMode() !== "waq") {
      return;
    }
    event.preventDefault();
    setStatus("Pasting is disabled for written validation answers.");
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

  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (requestInFlight) {
      return;
    }
    if (currentPendingAudit()) {
      void postAction("submit", {
        audit_prompt_id: currentPendingAudit().id,
        answer_text: input?.value || "",
      }).then(() => {
        if (input) {
          input.value = "";
          resizeInput();
        }
      });
      return;
    }
    const question = currentPendingQuestion();
    if (question?.question_type === "waq") {
      void postAction("submit", {
        question_id: question.question_id,
        answer_text: input?.value || "",
      }).then(() => {
        if (input) {
          input.value = "";
          resizeInput();
        }
      });
    }
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

  renderSession();
  updateTimer();
  window.setInterval(tick, 1000);
}
