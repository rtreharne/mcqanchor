(function () {
  function hasKatexRenderer() {
    return typeof window.renderMathInElement === "function";
  }

  function renderMath(container) {
    if (!container || !hasKatexRenderer()) {
      return;
    }
    window.renderMathInElement(container, {
      delimiters: [
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      throwOnError: false,
      strict: "ignore",
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    });
  }

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
        code.className = "preview-message-inline-code";
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

  function appendTextBlock(container, blockText) {
    const unorderedListPattern = /^\s*[-*]\s+/;
    const orderedListPattern = /^\s*\d+\.\s+/;
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
      list.className = "preview-message-list";
      listItems.forEach((itemText) => {
        const item = document.createElement("li");
        item.className = "preview-message-list-item";
        appendInlineMarkdown(item, itemText);
        list.appendChild(item);
      });
      container.appendChild(list);
      return;
    }

    const paragraph = document.createElement("p");
    paragraph.className = "preview-message-paragraph";
    appendInlineMarkdown(paragraph, blockText);
    container.appendChild(paragraph);
  }

  function appendFormattedMessageContent(container, text) {
    if (!container) {
      return;
    }

    const source = String(text || "");
    const fencePattern = /```([\w+-]+)?\n?([\s\S]*?)```/g;
    let lastIndex = 0;
    let hasContent = false;

    function appendTextSegment(segment) {
      const normalized = String(segment || "").replace(/^\n+|\n+$/g, "");
      if (!normalized) {
        return;
      }
      normalized.split(/\n{2,}/).forEach((blockText) => {
        appendTextBlock(container, blockText);
        hasContent = true;
      });
    }

    source.replace(fencePattern, (match, language, code, offset) => {
      appendTextSegment(source.slice(lastIndex, offset));

      const pre = document.createElement("pre");
      pre.className = "preview-message-code-block";
      if (language) {
        pre.dataset.language = String(language).trim().toLowerCase();
      }
      const codeElement = document.createElement("code");
      codeElement.className = "preview-message-code";
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
      paragraph.className = "preview-message-paragraph";
      appendInlineMarkdown(paragraph, source);
      container.appendChild(paragraph);
    }

    renderMath(container);
  }

  function appendInlineText(target, text) {
    if (!target) {
      return;
    }
    target.textContent = "";
    appendInlineMarkdown(target, text);
    renderMath(target);
  }

  function buildTextPanel(headingText, bodyText, extraClass = "") {
    const panel = document.createElement("div");
    panel.className = `preview-written-answer-panel${extraClass ? ` ${extraClass}` : ""}`;
    const heading = document.createElement("span");
    heading.className = "preview-written-answer-heading";
    heading.textContent = headingText;
    panel.appendChild(heading);
    appendFormattedMessageContent(panel, bodyText || "No answer submitted.");
    return panel;
  }

  window.StandaloneRichText = {
    appendFormattedMessageContent,
    appendInlineText,
    buildTextPanel,
    renderMath,
  };
}());
