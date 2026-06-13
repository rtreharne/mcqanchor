function getCookie(name) {
  const cookieValue = document.cookie
    .split(";")
    .map((cookie) => cookie.trim())
    .find((cookie) => cookie.startsWith(`${name}=`));
  return cookieValue ? decodeURIComponent(cookieValue.split("=")[1]) : "";
}

const launcher = document.querySelector(".chat-launcher");
const chatPanel = document.querySelector(".chat-panel");
const closeButton = document.querySelector(".chat-close");
const discoveryPanel = document.querySelector(".chat-discovery");
const transcript = document.querySelector(".chat-transcript");
const chatForm = document.querySelector(".chat-form");
const questionInput = document.querySelector("#chat-question");
const statusText = document.querySelector(".chat-status");
const sendButton = chatForm?.querySelector('button[type="submit"]');
const starterList = document.querySelector(".starter-list");
const starterButtons = document.querySelectorAll(".starter-question");
const scenarioButtons = document.querySelectorAll(".scenario-button");
const walkthroughPanel = document.querySelector(".walkthrough-panel");
const walkthroughTitle = document.querySelector(".walkthrough-title");
const walkthroughProgress = document.querySelector(".walkthrough-progress");
const walkthroughStage = document.querySelector(".walkthrough-stage");
const walkthroughStepTitle = document.querySelector(".walkthrough-step-title");
const walkthroughBody = document.querySelector(".walkthrough-body");
const walkthroughNextButton = document.querySelector(".walkthrough-next");
const walkthroughExitButton = document.querySelector(".walkthrough-exit");
const chatOpenLinks = document.querySelectorAll(".chat-open-link");

const history = [];
let thinkingMessage = null;
let hasShownContactPrompt = false;
let resetAction = null;
let activeScenarioId = null;
let activeScenarioStepIndex = 0;
let walkthroughTransitionTimer = null;
const MAX_HISTORY_ITEMS = 6;
const MAX_HISTORY_MESSAGE_LENGTH = 280;
const WALKTHROUGH_TRANSITION_MS = 220;
const WALKTHROUGH_SCENARIOS = {
  student: {
    title: "Student perspective",
    steps: [
      {
        stage: "Stage 1",
        title: "Joining the module",
        body: [
          "At the start of the semester, the student sees MCQ Anchor set up inside their course area or accesses it directly as a standalone platform.",
          "They can immediately see the module topics, the practice expectations for the term, and how their learning will connect to a short controlled validation later on.",
        ],
      },
      {
        stage: "Stage 2",
        title: "Practising little and often",
        body: [
          "Each week, the student completes short online MCQ sets generated dynamically from the course materials they have just studied.",
          "This is practice for learning, so they can work in their own time, use notes if appropriate, and build confidence through regular low-stakes activity.",
        ],
      },
      {
        stage: "Stage 3",
        title: "Using feedback to improve",
        body: [
          "Because the practice MCQs are built directly from approved course materials, the feedback stays closely tied to the actual content of the module.",
          "After each practice set, the student receives immediate feedback, explanations, and a clearer sense of which topics still need attention.",
          "Over the semester, they build coverage across the curriculum instead of relying on one last-minute revision push.",
        ],
      },
      {
        stage: "Stage 4",
        title: "Building a learning record",
        body: [
          "As the term progresses, MCQ Anchor tracks a fuller picture than a single raw score alone, such as accuracy, coverage, sustained engagement, and completion against targets.",
          "That means steady effort throughout the semester still counts, not just performance on one day.",
        ],
      },
      {
        stage: "Stage 5",
        title: "Taking the short validation",
        body: [
          "At the planned point in the course, the student books a short controlled validation session, typically around 15 to 20 minutes.",
          "They complete an unseen paper-based MCQ check without notes, phones, or generative AI, giving the course team a credible snapshot of independent knowledge.",
        ],
      },
      {
        stage: "Stage 6",
        title: "Receiving an anchored result",
        body: [
          "At the end of the process, the student is rewarded for regular practice, but the final outcome is anchored by what they can do independently in the validation step.",
          "The result is a fairer middle ground: students keep the benefits of continuous learning, while staff gain more confidence in what the score means.",
        ],
      },
    ],
  },
  teacher: {
    title: "Teacher walkthrough",
    steps: [
      {
        stage: "Stage 1",
        title: "Setting up the module",
        body: [
          "At the start of the semester, the teacher uploads or confirms the approved course materials that will be used for learning and assessment.",
          "They define the relevant topics, learning outcomes, and the broad structure of practice across the term.",
        ],
      },
      {
        stage: "Stage 2",
        title: "Generating practice from the course content",
        body: [
          "MCQ Anchor generates practice MCQs dynamically from the module materials, rather than relying on a detached generic bank.",
          "That means the weekly practice is closely aligned with the actual content, language, and teaching sequence of the course.",
        ],
      },
      {
        stage: "Stage 3",
        title: "Publishing regular practice",
        body: [
          "The teacher releases short online practice opportunities throughout the semester so students can engage little and often.",
          "Because the practice is for learning, the emphasis is on repetition, feedback, and curriculum coverage rather than on trying to police every interaction.",
        ],
      },
      {
        stage: "Stage 4",
        title: "Monitoring progress across the cohort",
        body: [
          "As students work through the term, the teacher can review patterns in accuracy, topic coverage, engagement, and completion.",
          "This gives a richer picture of learning progress than a single online percentage score viewed in isolation.",
        ],
      },
      {
        stage: "Stage 5",
        title: "Running the controlled validation",
        body: [
          "When the course reaches the validation point, the teacher arranges a short controlled paper-based MCQ session, typically around 15 to 20 minutes.",
          "Ahead of the booked sessions, the teacher prints the personalised student papers and answer sheets so each validation is ready to run smoothly.",
          "Students complete unseen questions drawn from a secure validation pool that matches the same outcomes and difficulty blueprint as the practice layer.",
        ],
      },
      {
        stage: "Stage 6",
        title: "Reviewing anchored results",
        body: [
          "At the end of the process, the teacher can combine practice and validation using the chosen scoring model and any calibration rules the institution wants to apply.",
          "The result is a more defensible mark: students still benefit from continuous practice, while the teacher has stronger evidence of independent understanding.",
        ],
      },
    ],
  },
};

function normalizeHistoryText(text) {
  const trimmed = (text || "").trim();
  if (trimmed.length <= MAX_HISTORY_MESSAGE_LENGTH) {
    return trimmed;
  }
  return `${trimmed.slice(0, MAX_HISTORY_MESSAGE_LENGTH - 1).trimEnd()}…`;
}

function buildHistoryPayload() {
  return history.slice(-MAX_HISTORY_ITEMS).map((item) => ({
    role: item.role,
    content: normalizeHistoryText(item.content),
  }));
}

function updateChatLayout() {
  if (!chatPanel || !chatForm) {
    return;
  }

  const composerClearance = chatForm.offsetHeight + 24;
  chatPanel.style.setProperty("--chat-form-clearance", `${composerClearance}px`);
}

function updateChatMode() {
  if (!chatPanel || !discoveryPanel || !transcript || !walkthroughPanel || !chatForm) {
    return;
  }

  const hasTranscriptContent = transcript.children.length > 0;
  const isWalkthroughMode = Boolean(activeScenarioId);
  const nextMode = isWalkthroughMode ? "walkthrough" : hasTranscriptContent ? "conversation" : "discovery";

  chatPanel.classList.toggle("is-discovery-mode", nextMode === "discovery");
  chatPanel.classList.toggle("is-conversation-mode", nextMode === "conversation");
  chatPanel.classList.toggle("is-walkthrough-mode", nextMode === "walkthrough");
  discoveryPanel.hidden = nextMode !== "discovery";
  transcript.hidden = nextMode !== "conversation";
  walkthroughPanel.hidden = nextMode !== "walkthrough";
  chatForm.hidden = nextMode === "walkthrough";
}

function scrollTranscriptToBottom() {
  if (!transcript) {
    return;
  }

  requestAnimationFrame(() => {
    updateChatLayout();
    transcript.scrollTop = transcript.scrollHeight;
  });
}

function removeResetAction() {
  if (!resetAction) {
    return;
  }

  resetAction.remove();
  resetAction = null;
}

function appendResetAction() {
  if (!transcript) {
    return;
  }

  removeResetAction();
  resetAction = document.createElement("article");
  resetAction.className = "chat-reset";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "chat-reset-button";
  button.textContent = "Back to FAQs";
  button.addEventListener("click", resetChat);

  resetAction.appendChild(button);
  transcript.appendChild(resetAction);
  updateChatMode();
  scrollTranscriptToBottom();
}

function renderWalkthroughStep() {
  if (!activeScenarioId || !walkthroughTitle || !walkthroughProgress || !walkthroughStage || !walkthroughStepTitle || !walkthroughBody || !walkthroughNextButton) {
    return;
  }

  const scenario = WALKTHROUGH_SCENARIOS[activeScenarioId];
  if (!scenario) {
    return;
  }

  const step = scenario.steps[activeScenarioStepIndex];
  walkthroughTitle.textContent = scenario.title;
  walkthroughProgress.textContent = `Step ${activeScenarioStepIndex + 1} of ${scenario.steps.length}`;
  walkthroughStage.textContent = step.stage;
  walkthroughStepTitle.textContent = step.title;
  walkthroughBody.innerHTML = step.body.map((paragraph) => `<p>${paragraph}</p>`).join("");
  walkthroughNextButton.textContent = activeScenarioStepIndex === scenario.steps.length - 1 ? "Finish" : "Next";
}

function clearWalkthroughTransition() {
  if (walkthroughTransitionTimer) {
    clearTimeout(walkthroughTransitionTimer);
    walkthroughTransitionTimer = null;
  }

  walkthroughPanel?.classList.remove("is-step-exiting", "is-step-entering");
  walkthroughNextButton?.removeAttribute("disabled");
}

function transitionWalkthroughStep(updateStep) {
  if (!walkthroughPanel) {
    updateStep();
    return;
  }

  clearWalkthroughTransition();
  walkthroughNextButton?.setAttribute("disabled", "disabled");
  walkthroughPanel.classList.add("is-step-exiting");

  walkthroughTransitionTimer = window.setTimeout(() => {
    updateStep();
    walkthroughPanel.classList.remove("is-step-exiting");
    walkthroughPanel.classList.add("is-step-entering");

    walkthroughTransitionTimer = window.setTimeout(() => {
      walkthroughPanel?.classList.remove("is-step-entering");
      walkthroughNextButton?.removeAttribute("disabled");
      walkthroughTransitionTimer = null;
    }, WALKTHROUGH_TRANSITION_MS);
  }, 140);
}

function openWalkthrough(scenarioId) {
  if (!WALKTHROUGH_SCENARIOS[scenarioId]) {
    return;
  }

  removeThinkingMessage();
  removeResetAction();
  activeScenarioId = scenarioId;
  activeScenarioStepIndex = 0;
  clearWalkthroughTransition();
  renderWalkthroughStep();
  updateChatMode();
}

function advanceWalkthrough() {
  if (!activeScenarioId) {
    return;
  }

  const scenario = WALKTHROUGH_SCENARIOS[activeScenarioId];
  if (!scenario) {
    return;
  }

  if (activeScenarioStepIndex >= scenario.steps.length - 1) {
    exitWalkthrough();
    return;
  }

  transitionWalkthroughStep(() => {
    activeScenarioStepIndex += 1;
    renderWalkthroughStep();
  });
}

function exitWalkthrough() {
  clearWalkthroughTransition();
  activeScenarioId = null;
  activeScenarioStepIndex = 0;
  updateChatMode();
  updateChatLayout();
  questionInput?.focus();
}

function resetChat() {
  removeThinkingMessage();
  removeResetAction();
  history.length = 0;
  hasShownContactPrompt = false;
  activeScenarioId = null;
  activeScenarioStepIndex = 0;

  if (transcript) {
    transcript.innerHTML = "";
    transcript.scrollTop = 0;
  }

  if (questionInput) {
    questionInput.value = "";
    questionInput.style.height = "auto";
  }

  if (statusText) {
    statusText.textContent = "";
  }

  setComposerDisabled(false);
  updateChatMode();
  updateChatLayout();
  questionInput?.focus();
}

function appendMessage(role, text) {
  if (!transcript) {
    return null;
  }
  const message = document.createElement("article");
  message.className = `chat-message chat-message-${role}`;
  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  message.appendChild(paragraph);
  transcript.appendChild(message);
  updateChatMode();
  scrollTranscriptToBottom();
  return message;
}

function appendContactPrompt() {
  if (!transcript || hasShownContactPrompt) {
    return;
  }

  const message = document.createElement("article");
  message.className = "chat-message chat-message-assistant chat-cta";
  message.innerHTML =
    '<p>If you want module-specific advice or a pilot conversation, the quickest next step is the contact form.</p><a href="#contact">Start a conversation</a>';
  transcript.appendChild(message);
  updateChatMode();
  scrollTranscriptToBottom();
  hasShownContactPrompt = true;
}

function showThinkingMessage() {
  if (!transcript || thinkingMessage) {
    return;
  }

  thinkingMessage = document.createElement("article");
  thinkingMessage.className = "chat-message chat-message-assistant chat-message-thinking";
  thinkingMessage.setAttribute("aria-label", "Assistant is thinking");
  thinkingMessage.innerHTML =
    '<p class="thinking-ellipsis" aria-hidden="true"><span>.</span><span>.</span><span>.</span></p>';
  transcript.appendChild(thinkingMessage);
  updateChatMode();
  scrollTranscriptToBottom();
}

function removeThinkingMessage() {
  if (!thinkingMessage) {
    return;
  }

  thinkingMessage.remove();
  thinkingMessage = null;
  updateChatMode();
}

function setComposerDisabled(disabled) {
  if (questionInput) {
    questionInput.disabled = disabled;
  }
  if (sendButton) {
    sendButton.disabled = disabled;
  }
}

function hideStarterQuestions() {
  updateChatMode();
}

function openChat() {
  if (!launcher || !chatPanel) {
    return;
  }
  launcher.setAttribute("aria-expanded", "true");
  document.body.classList.add("chat-open");
  chatPanel.hidden = false;
  updateChatLayout();
  questionInput?.focus();
}

function closeChat() {
  if (!launcher || !chatPanel) {
    return;
  }
  launcher.setAttribute("aria-expanded", "false");
  document.body.classList.remove("chat-open");
  chatPanel.hidden = true;
  launcher.focus();
}

async function sendQuestion(question) {
  if (!questionInput || !statusText || !sendButton) {
    return;
  }

  const trimmed = question.trim();
  if (!trimmed) {
    statusText.textContent = "Please enter a question first.";
    return;
  }

  hideStarterQuestions();
  removeResetAction();
  appendMessage("user", trimmed);
  history.push({ role: "user", content: trimmed });
  questionInput.value = "";
  statusText.textContent = "";
  setComposerDisabled(true);
  showThinkingMessage();

  try {
    const response = await fetch("/api/product-chat/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCookie("csrftoken"),
      },
      body: JSON.stringify({
        question: trimmed,
        history: buildHistoryPayload(),
      }),
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Something went wrong.");
    }

    removeThinkingMessage();
    appendMessage("assistant", payload.answer);
    history.push({ role: "assistant", content: payload.answer });
    statusText.textContent = "";
    if (history.filter((item) => item.role === "user").length >= 3) {
      appendContactPrompt();
    }
    appendResetAction();
  } catch (error) {
    removeThinkingMessage();
    appendMessage("assistant", error.message || "The chat assistant is unavailable right now.");
    history.push({
      role: "assistant",
      content: error.message || "The chat assistant is unavailable right now.",
    });
    statusText.textContent = "";
    appendResetAction();
  } finally {
    setComposerDisabled(false);
    questionInput.focus();
  }
}

launcher?.addEventListener("click", () => {
  if (chatPanel?.hidden) {
    openChat();
  } else {
    closeChat();
  }
});

closeButton?.addEventListener("click", closeChat);

chatForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  await sendQuestion(questionInput?.value || "");
});

questionInput?.addEventListener("keydown", async (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    if (questionInput.disabled) {
      return;
    }
    await sendQuestion(questionInput.value || "");
  }
});

questionInput?.addEventListener("input", () => {
  questionInput.style.height = "auto";
  questionInput.style.height = `${Math.min(questionInput.scrollHeight, 83)}px`;
  updateChatLayout();
  scrollTranscriptToBottom();
});

starterButtons.forEach((button) => {
  button.addEventListener("click", async () => {
    openChat();
    await sendQuestion(button.textContent || "");
  });
});

scenarioButtons.forEach((button) => {
  button.addEventListener("click", () => {
    openChat();
    openWalkthrough(button.dataset.scenario || "");
  });
});

walkthroughNextButton?.addEventListener("click", advanceWalkthrough);
walkthroughExitButton?.addEventListener("click", exitWalkthrough);

chatOpenLinks.forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    openChat();
  });
});

window.addEventListener("resize", () => {
  updateChatLayout();
  scrollTranscriptToBottom();
});

updateChatMode();
updateChatLayout();
