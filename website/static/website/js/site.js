function getCookie(name) {
  const cookieValue = document.cookie
    .split(";")
    .map((cookie) => cookie.trim())
    .find((cookie) => cookie.startsWith(`${name}=`));
  return cookieValue ? decodeURIComponent(cookieValue.split("=")[1]) : "";
}

const launcher = document.querySelector(".chat-launcher");
const launcherBubbleStream = document.querySelector(".chat-launcher-bubbles");
const heroSection = document.querySelector(".hero");
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
const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
const desktopAutoOpenQuery = window.matchMedia("(min-width: 1024px)");

const history = [];
let thinkingMessage = null;
let hasShownContactPrompt = false;
let resetAction = null;
let activeScenarioId = null;
let activeScenarioStepIndex = 0;
let walkthroughTransitionTimer = null;
let bubbleSpawnTimer = null;
let chatPanelHideTimer = null;
let chatPanelShowFrame = null;
let hasAutoOpenedLauncher = false;
let lastScrollY = window.scrollY;
const MAX_HISTORY_ITEMS = 6;
const MAX_HISTORY_MESSAGE_LENGTH = 280;
const WALKTHROUGH_TRANSITION_MS = 220;
const CHAT_PANEL_TRANSITION_MS = 260;
const WALKTHROUGH_SCENARIOS = {
  student: {
    title: "Student perspective",
    steps: [
      {
        stage: "Stage 1",
        title: "Joining the course",
        body: [
          "At the start of the semester, the student sees MCQ Anchor set up inside their course area or accesses it directly as a standalone platform.",
          "They can immediately see the course topics, the practice expectations for the term, and how their learning will connect to a short controlled validation later on.",
        ],
      },
      {
        stage: "Stage 2",
        title: "Practising little and often",
        body: [
          "Each week, the student completes short online MCQ sets generated dynamically from the course materials they have just studied.",
          "This is practice for learning, so they can work in their own time, use notes if appropriate, and build confidence through regular low-stakes activity.",
          "They can also open practice validation whenever they want, as often as they like, to rehearse the validation experience before any booked session.",
        ],
      },
      {
        stage: "Stage 3",
        title: "Using feedback to improve",
        body: [
          "Because the practice MCQs are built directly from approved course materials, the feedback stays closely tied to the actual content of the course.",
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
          "They complete the validation digitally on a single device, such as their phone, with the session designed to flag use of external resources or generative AI tools.",
          "That gives the course team a credible snapshot of what the student can do independently under controlled conditions.",
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
        title: "Setting up the course",
        body: [
          "At the start of the semester, the teacher uploads or confirms the approved course materials that will be used for learning and assessment.",
          "They define the relevant topics, learning outcomes, and the broad structure of practice across the term.",
        ],
      },
      {
        stage: "Stage 2",
        title: "Generating practice from the course content",
        body: [
          "MCQ Anchor generates practice MCQs dynamically from the course materials, rather than relying on a detached generic bank.",
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
          "When the course reaches the validation point, the teacher arranges a short controlled digital session, typically around 15 to 20 minutes.",
          "Students complete unseen questions drawn from a secure validation pool that matches the same outcomes and difficulty blueprint as the practice layer.",
          "Validation runs on a single device per student, such as a phone, with the session designed to flag use of external resources or generative AI tools.",
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

function randomBetween(min, max) {
  return Math.random() * (max - min) + min;
}

function scheduleNextBubble() {
  if (!launcherBubbleStream || reducedMotionQuery.matches) {
    bubbleSpawnTimer = null;
    return;
  }

  const nextDelay = randomBetween(180, 640);
  bubbleSpawnTimer = window.setTimeout(() => {
    spawnLauncherBubble();

    if (Math.random() < 0.3) {
      window.setTimeout(spawnLauncherBubble, randomBetween(90, 180));
    }

    scheduleNextBubble();
  }, nextDelay);
}

function spawnLauncherBubble() {
  if (!launcherBubbleStream || reducedMotionQuery.matches) {
    return;
  }

  const bubble = document.createElement("span");
  const bubbleSize = randomBetween(0.36, 1.02);
  const bubbleRise = randomBetween(3.8, 5.8);
  const bubbleDuration = randomBetween(2.2, 4.1);
  const bubbleXStart = randomBetween(-0.45, 0.45);
  const bubbleXEnd = bubbleXStart + randomBetween(-0.75, 0.75);

  bubble.className = "chat-launcher-bubble";
  bubble.style.setProperty("--bubble-size", `${bubbleSize.toFixed(2)}rem`);
  bubble.style.setProperty("--bubble-rise", `${bubbleRise.toFixed(2)}rem`);
  bubble.style.setProperty("--bubble-duration", `${bubbleDuration.toFixed(2)}s`);
  bubble.style.setProperty("--bubble-x-start", `${bubbleXStart.toFixed(2)}rem`);
  bubble.style.setProperty("--bubble-x-end", `${bubbleXEnd.toFixed(2)}rem`);
  launcherBubbleStream.appendChild(bubble);

  bubble.addEventListener("animationend", () => {
    bubble.remove();
  }, { once: true });
}

function startBubbleStream() {
  if (!launcherBubbleStream || reducedMotionQuery.matches || bubbleSpawnTimer !== null) {
    return;
  }

  spawnLauncherBubble();
  scheduleNextBubble();
}

function stopBubbleStream() {
  if (bubbleSpawnTimer !== null) {
    window.clearTimeout(bubbleSpawnTimer);
    bubbleSpawnTimer = null;
  }

  launcherBubbleStream?.replaceChildren();
}

function scheduleLauncherWiggle() {
  if (!launcher || reducedMotionQuery.matches) {
    return;
  }

  window.setTimeout(() => {
    launcher.classList.add("is-wiggle");
  }, 5000);
}

function maybeAutoOpenLauncherOnScroll() {
  if (
    hasAutoOpenedLauncher ||
    !launcher ||
    !heroSection ||
    !desktopAutoOpenQuery.matches ||
    !chatPanel?.hidden
  ) {
    lastScrollY = window.scrollY;
    return;
  }

  const currentScrollY = window.scrollY;
  const isScrollingDown = currentScrollY > lastScrollY;
  const heroRect = heroSection.getBoundingClientRect();
  const heroMidpoint = heroRect.top + heroRect.height / 2;

  lastScrollY = currentScrollY;

  if (!isScrollingDown || heroMidpoint > 0) {
    return;
  }

  hasAutoOpenedLauncher = true;
  launcher.click();
  window.removeEventListener("scroll", maybeAutoOpenLauncherOnScroll);
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

function appendFormattedMessageContent(container, text) {
  if (!container) {
    return;
  }

  const source = String(text || "");
  const fencePattern = /```([\w+-]+)?\n?([\s\S]*?)```/g;
  const unorderedListPattern = /^\s*[-*]\s+/;
  const orderedListPattern = /^\s*\d+\.\s+/;
  let lastIndex = 0;
  let hasContent = false;

  function appendInlineMarkdown(target, inlineText) {
    const sourceText = String(inlineText || "");
    const tokenPattern = /`[^`\n]+`|\*\*[^*][\s\S]*?\*\*|__[^_][\s\S]*?__|\*[^*\n][\s\S]*?\*|_[^_\n][\s\S]*?_/g;
    let inlineLastIndex = 0;

    function appendToken(targetNode, tokenText) {
      if (!tokenText) {
        return;
      }
      if (tokenText.startsWith("`") && tokenText.endsWith("`")) {
        const code = document.createElement("code");
        code.className = "chat-message-inline-code";
        code.textContent = tokenText.slice(1, -1);
        targetNode.appendChild(code);
        return;
      }
      if (
        (tokenText.startsWith("**") && tokenText.endsWith("**"))
        || (tokenText.startsWith("__") && tokenText.endsWith("__"))
      ) {
        const strong = document.createElement("strong");
        appendInlineMarkdown(strong, tokenText.slice(2, -2));
        targetNode.appendChild(strong);
        return;
      }
      if (
        (tokenText.startsWith("*") && tokenText.endsWith("*"))
        || (tokenText.startsWith("_") && tokenText.endsWith("_"))
      ) {
        const emphasis = document.createElement("em");
        appendInlineMarkdown(emphasis, tokenText.slice(1, -1));
        targetNode.appendChild(emphasis);
        return;
      }
      targetNode.appendChild(document.createTextNode(tokenText));
    }

    sourceText.replace(tokenPattern, (match, offset) => {
      const plainText = sourceText.slice(inlineLastIndex, offset);
      if (plainText) {
        target.appendChild(document.createTextNode(plainText));
      }
      appendToken(target, match);
      inlineLastIndex = offset + match.length;
      return match;
    });

    const trailingText = sourceText.slice(inlineLastIndex);
    if (trailingText) {
      target.appendChild(document.createTextNode(trailingText));
    }
  }

  function parseListItems(lines, markerPattern) {
    const items = [];
    let currentItem = "";
    for (const line of lines) {
      if (!line.trim()) {
        if (currentItem) {
          currentItem += "\n";
        }
        continue;
      }
      if (markerPattern.test(line)) {
        if (currentItem.trim()) {
          items.push(currentItem.trim());
        }
        currentItem = line.replace(markerPattern, "").trim();
        continue;
      }
      if (/^\s+/.test(line) && currentItem) {
        currentItem = `${currentItem}\n${line.trim()}`;
        continue;
      }
      return [];
    }
    if (currentItem.trim()) {
      items.push(currentItem.trim());
    }
    return items;
  }

  function appendTextBlock(blockText) {
    const lines = blockText.split("\n");
    const nonEmptyLines = lines.filter((line) => line.trim());
    if (!nonEmptyLines.length) {
      return;
    }

    const firstLine = nonEmptyLines[0];
    const isUnorderedList = unorderedListPattern.test(firstLine);
    const isOrderedList = !isUnorderedList && orderedListPattern.test(firstLine);
    const listItems = isUnorderedList
      ? parseListItems(lines, unorderedListPattern)
      : (isOrderedList ? parseListItems(lines, orderedListPattern) : []);

    if (listItems.length) {
      const list = document.createElement(isOrderedList ? "ol" : "ul");
      list.className = "chat-message-list";
      listItems.forEach((itemText) => {
        const item = document.createElement("li");
        item.className = "chat-message-list-item";
        appendInlineMarkdown(item, itemText);
        list.appendChild(item);
      });
      container.appendChild(list);
      hasContent = true;
      return;
    }

    const paragraph = document.createElement("p");
    paragraph.className = "chat-message-paragraph";
    appendInlineMarkdown(paragraph, blockText);
    container.appendChild(paragraph);
    hasContent = true;
  }

  function appendTextSegment(segment) {
    const normalized = String(segment || "").replace(/^\n+|\n+$/g, "");
    if (!normalized) {
      return;
    }
    normalized.split(/\n{2,}/).forEach((blockText) => {
      appendTextBlock(blockText);
    });
  }

  source.replace(fencePattern, (match, language, code, offset) => {
    appendTextSegment(source.slice(lastIndex, offset));

    const pre = document.createElement("pre");
    pre.className = "chat-message-code-block";
    if (language) {
      pre.dataset.language = String(language).trim().toLowerCase();
    }
    const codeElement = document.createElement("code");
    codeElement.className = "chat-message-code";
    codeElement.textContent = String(code || "").replace(/^\n+|\n+$/g, "");
    pre.appendChild(codeElement);
    container.appendChild(pre);
    hasContent = true;
    lastIndex = offset + match.length;
    return match;
  });

  appendTextSegment(source.slice(lastIndex));

  if (!hasContent) {
    const paragraph = document.createElement("p");
    paragraph.className = "chat-message-paragraph";
    appendInlineMarkdown(paragraph, source);
    container.appendChild(paragraph);
  }
}

function appendMessage(role, text) {
  if (!transcript) {
    return null;
  }
  const message = document.createElement("article");
  message.className = `chat-message chat-message-${role}`;
  appendFormattedMessageContent(message, text);
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
    '<p>If you want course-specific advice or a pilot conversation, the quickest next step is the contact form.</p><a href="#contact">Start a conversation</a>';
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

  if (chatPanelHideTimer) {
    window.clearTimeout(chatPanelHideTimer);
    chatPanelHideTimer = null;
  }

  if (chatPanelShowFrame) {
    window.cancelAnimationFrame(chatPanelShowFrame);
    chatPanelShowFrame = null;
  }

  launcher.setAttribute("aria-expanded", "true");
  document.body.classList.add("chat-open");
  chatPanel.hidden = false;
  chatPanelShowFrame = window.requestAnimationFrame(() => {
    chatPanel.classList.add("is-visible");
    chatPanelShowFrame = null;
  });
  updateChatLayout();
  questionInput?.focus();
}

function closeChat() {
  if (!launcher || !chatPanel) {
    return;
  }

  if (chatPanelHideTimer) {
    window.clearTimeout(chatPanelHideTimer);
    chatPanelHideTimer = null;
  }

  if (chatPanelShowFrame) {
    window.cancelAnimationFrame(chatPanelShowFrame);
    chatPanelShowFrame = null;
  }

  launcher.setAttribute("aria-expanded", "false");
  document.body.classList.remove("chat-open");
  chatPanel.classList.remove("is-visible");

  if (reducedMotionQuery.matches) {
    chatPanel.hidden = true;
    launcher.focus();
    return;
  }

  chatPanelHideTimer = window.setTimeout(() => {
    if (!chatPanel.classList.contains("is-visible")) {
      chatPanel.hidden = true;
    }
    chatPanelHideTimer = null;
  }, CHAT_PANEL_TRANSITION_MS);

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

    let payload;
    try {
      payload = await response.json();
    } catch (parseError) {
      throw new Error("The chat returned an unexpected server response. Please refresh the page and try again.");
    }

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

launcher?.addEventListener("animationend", (event) => {
  if (event.animationName === "chat-launcher-wiggle") {
    launcher.classList.remove("is-wiggle");
  }
});

if (typeof reducedMotionQuery.addEventListener === "function") {
  reducedMotionQuery.addEventListener("change", (event) => {
    if (event.matches) {
      stopBubbleStream();
      return;
    }

    startBubbleStream();
  });
}

window.addEventListener("resize", () => {
  updateChatLayout();
  scrollTranscriptToBottom();
});

window.addEventListener("scroll", maybeAutoOpenLauncherOnScroll, { passive: true });

updateChatMode();
updateChatLayout();
startBubbleStream();
if (document.readyState === "complete") {
  scheduleLauncherWiggle();
} else {
  window.addEventListener("load", scheduleLauncherWiggle, { once: true });
}
