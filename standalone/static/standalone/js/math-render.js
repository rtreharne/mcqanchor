(function () {
  const literalUnicodeEscapePattern = /\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})/g;
  const mathDelimitedPattern = /(?:\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\))/g;
  const inlineEquationPatternSource = String.raw`\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)?(?:\s+[A-Za-z][A-Za-z0-9]*){0,5}\s*=\s*.+?(?=(?:[.?!;:](?:\s|$))|$)`;
  const greekTexMap = {
    α: "\\alpha",
    β: "\\beta",
    γ: "\\gamma",
    δ: "\\delta",
    Δ: "\\Delta",
    ε: "\\epsilon",
    θ: "\\theta",
    λ: "\\lambda",
    μ: "\\mu",
    π: "\\pi",
    σ: "\\sigma",
    φ: "\\phi",
    ω: "\\omega",
  };
  const superscriptDigits = {
    "-": "⁻",
    "+": "⁺",
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
  };
  const superscriptDigitLookup = Object.fromEntries(
    Object.entries(superscriptDigits).map(([plainText, superscriptText]) => [superscriptText, plainText]),
  );
  const mathFunctionNames = [
    "radians",
    "degrees",
    "log10",
    "sqrt",
    "asin",
    "acos",
    "atan",
    "sin",
    "cos",
    "tan",
    "log",
    "ln",
    "exp",
    "abs",
  ];
  const mathFunctionTex = {
    radians: "\\operatorname{radians}",
    degrees: "\\operatorname{degrees}",
    log10: "\\log_{10}",
    asin: "\\arcsin",
    acos: "\\arccos",
    atan: "\\arctan",
    sin: "\\sin",
    cos: "\\cos",
    tan: "\\tan",
    log: "\\log",
    ln: "\\ln",
    exp: "\\exp",
    abs: "\\operatorname{abs}",
  };

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

  function decodeLiteralUnicodeEscapes(text) {
    return String(text || "").replace(literalUnicodeEscapePattern, (match, shortCodepoint, longCodepoint) => {
      const codepoint = shortCodepoint || longCodepoint;
      const parsed = Number.parseInt(codepoint, 16);
      return Number.isFinite(parsed) ? String.fromCodePoint(parsed) : match;
    });
  }

  function formatScientificNotationExponent(exponentText) {
    return normalizeScientificNotationExponentText(exponentText).replace(/^\+/, "").replace(/^-/, "−");
  }

  function normalizeScientificNotationExponentText(exponentText) {
    return String(exponentText || "")
      .replace(/\s+/g, "")
      .replace(/^−/, "-")
      .replace(/^⁻/, "-")
      .replace(/^＋/, "+")
      .replace(/^⁺/, "+");
  }

  function scientificNotationNumericValue(mantissa, exponent) {
    const normalizedExponent = normalizeScientificNotationExponentText(exponent);
    const value = Number(`${mantissa}e${normalizedExponent}`);
    return Number.isFinite(value) ? value : null;
  }

  function shouldUseScientificNotation(mantissa, exponent) {
    const numericValue = scientificNotationNumericValue(mantissa, exponent);
    if (numericValue === null || numericValue === 0) {
      return false;
    }
    const absoluteValue = Math.abs(numericValue);
    return absoluteValue < 1e-3 || absoluteValue >= 1e4;
  }

  function decimalTextFromScientificNotation(mantissa, exponent) {
    const numericValue = scientificNotationNumericValue(mantissa, exponent);
    return numericValue === null ? `${mantissa}e${exponent}` : `${numericValue}`;
  }

  function appendScientificNotation(target, mantissa, exponent) {
    const wrapper = document.createElement("span");
    wrapper.className = "preview-scientific-notation";

    const base = document.createElement("span");
    base.className = "preview-scientific-notation-base";
    base.textContent = `${mantissa} × 10`;

    const superscript = document.createElement("sup");
    superscript.className = "preview-scientific-notation-exponent";
    superscript.textContent = formatScientificNotationExponent(exponent);

    wrapper.append(base, superscript);
    target.appendChild(wrapper);
  }

  function decodeSuperscriptExponent(text) {
    return Array.from(String(text || "")).map((character) => superscriptDigitLookup[character] || character).join("");
  }

  function appendTextWithUnitPowers(target, text) {
    const sourceText = String(text || "");
    const powerPattern = /([A-Za-zµμΩ%])\^([+\-−]?\d+)/g;
    let lastIndex = 0;

    sourceText.replace(powerPattern, (match, base, exponent, offset) => {
      const plainPrefix = sourceText.slice(lastIndex, offset);
      if (plainPrefix) {
        target.appendChild(document.createTextNode(plainPrefix));
      }

      target.appendChild(document.createTextNode(base));
      const superscript = document.createElement("sup");
      superscript.className = "preview-inline-superscript";
      superscript.textContent = formatScientificNotationExponent(exponent);
      target.appendChild(superscript);

      lastIndex = offset + match.length;
      return match;
    });

    const trailing = sourceText.slice(lastIndex);
    if (trailing) {
      target.appendChild(document.createTextNode(trailing));
    }
  }

  function appendTextWithScientificNotation(target, text) {
    const sourceText = String(text || "");
    const scientificPattern = /(^|[^A-Za-z0-9_\\])([-+]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:[eE]\s*([+\-−]?\d+)|×\s*10(?:\^([+\-−]?\d+)|([⁻⁺⁰¹²³⁴⁵⁶⁷⁸⁹]+)))(?=($|[^A-Za-z0-9_]))/g;
    let lastIndex = 0;

    sourceText.replace(scientificPattern, (match, prefix, mantissa, rawExponent, plainPowerExponent, superscriptExponent, _suffix, offset) => {
      const plainPrefix = sourceText.slice(lastIndex, offset);
      if (plainPrefix) {
        appendTextWithUnitPowers(target, plainPrefix);
      }
      if (prefix) {
        appendTextWithUnitPowers(target, prefix);
      }
      const exponent = normalizeScientificNotationExponentText(
        rawExponent || plainPowerExponent || decodeSuperscriptExponent(superscriptExponent),
      );
      if (shouldUseScientificNotation(mantissa, exponent)) {
        appendScientificNotation(target, mantissa, exponent);
      } else {
        appendTextWithUnitPowers(target, decimalTextFromScientificNotation(mantissa, exponent));
      }
      lastIndex = offset + match.length;
      return match;
    });

    const trailing = sourceText.slice(lastIndex);
    if (trailing) {
      appendTextWithUnitPowers(target, trailing);
    }
  }

  function normalizeScientificNotationMath(text) {
    return String(text || "").replace(
      /(^|[^A-Za-z0-9_\\])([-+]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:[eE]\s*([+\-−]?\d+)|×\s*10\^([+\-−]?\d+))(?=($|[^A-Za-z0-9_]))/g,
      (match, prefix, mantissa, rawExponent, powerExponent) => {
        const exponent = normalizeScientificNotationExponentText(rawExponent || powerExponent);
        return (
          shouldUseScientificNotation(mantissa, exponent)
            ? `${prefix}${mantissa} \\times 10^{${Number.parseInt(exponent, 10)}}`
            : `${prefix}${decimalTextFromScientificNotation(mantissa, exponent)}`
        );
      },
    );
  }

  function replaceGreekCharacters(text) {
    return Array.from(String(text || "")).map((character) => greekTexMap[character] || character).join("");
  }

  function isIdentifierBoundary(character) {
    return !character || !/[A-Za-z0-9_\\]/.test(character);
  }

  function findMatchingParen(text, openIndex) {
    let depth = 0;
    for (let index = openIndex; index < text.length; index += 1) {
      if (text[index] === "(") {
        depth += 1;
      } else if (text[index] === ")") {
        depth -= 1;
        if (depth === 0) {
          return index;
        }
      }
    }
    return -1;
  }

  function mathFunctionLabel(name) {
    return mathFunctionTex[name] || `\\operatorname{${name}}`;
  }

  function formatAngleTex(argumentTex) {
    const normalized = String(argumentTex || "").trim();
    if (!normalized) {
      return "";
    }
    if (/^[A-Za-z0-9\\{}_^.-]+$/.test(normalized)) {
      return `${normalized}^{\\circ}`;
    }
    return `\\left(${normalized}\\right)^{\\circ}`;
  }

  function wrapRomanMathPhrase(phrase) {
    const normalized = String(phrase || "").trim();
    if (!normalized) {
      return "";
    }
    if (normalized.includes("\\")) {
      return normalized;
    }
    if (/^[A-Za-z]$/.test(normalized)) {
      return normalized;
    }
    if (/^(?:sin|cos|tan|asin|acos|atan|ln|log10|log|exp|abs|sqrt|radians|degrees|pi|e)$/i.test(normalized)) {
      return normalized;
    }
    return `\\mathrm{${normalized.replace(/\s+/g, "\\ ")}}`;
  }

  function romanizeWordPhrases(text) {
    return String(text || "").replace(
      /(^|[=+\-*/×÷−(){}\[\],])\s*([A-Za-z][A-Za-z]*(?:\s+[A-Za-z][A-Za-z]*)*)(?=\s*(?:[=+\-*/×÷−(){}\[\],]|$))/g,
      (match, prefix, phrase, offset, source) => {
        if (prefix === "{" && /\\[A-Za-z]+$/.test(source.slice(Math.max(0, offset - 24), offset))) {
          return match;
        }
        return `${prefix}${wrapRomanMathPhrase(phrase)}`;
      },
    );
  }

  function normalizeEquationExpression(expression) {
    const source = String(expression || "");
    const equalsIndex = source.indexOf("=");
    if (equalsIndex < 0) {
      return source.trim();
    }

    let left = source.slice(0, equalsIndex).trim();
    const right = source.slice(equalsIndex + 1).trim();

    left = left
      .replace(/^(?:the\s+)?(?:formula|equation|relationship|expression|law)\s*[:\-]?\s*/i, "")
      .replace(/^(?:the\s+)?(?:formula|equation|relationship|expression|law)\s+(?:is|for|becomes)\s+/i, "")
      .replace(/^(?:this|that|it)\s+(?:is|gives|becomes)\s+/i, "")
      .trim();

    const leftWords = left.split(/\s+/).filter(Boolean);
    if (leftWords.length > 4) {
      left = leftWords.slice(-2).join(" ");
    }

    return `${left} = ${right}`.trim();
  }

  function splitEquationExpression(expression) {
    const source = String(expression || "");
    const proseTailPattern = /,\s*(?=(?:calculate|determine|estimate|find|compute|work out|show|state|identify|explain|what|which|how|when|where|why)\b)/i;
    const match = proseTailPattern.exec(source);
    if (!match || match.index < 0) {
      return {
        equationText: source,
        trailingText: "",
      };
    }
    return {
      equationText: source.slice(0, match.index).trimEnd(),
      trailingText: source.slice(match.index),
    };
  }

  function replaceBarePiInMath(text) {
    return String(text || "").replace(/(^|[^\\A-Za-z])pi(?=([^A-Za-z]|$))/g, (match, prefix) => `${prefix}\\pi`);
  }

  function normalizeStoredMathBody(body) {
    let normalized = decodeLiteralUnicodeEscapes(body);

    for (let iteration = 0; iteration < 4; iteration += 1) {
      const next = normalized
        .replace(/\\operatorname\{radians\}\\left\(([^()]*)\\right\)/g, (match, argument) => formatAngleTex(argument))
        .replace(/\bradians\(([^()]*)\)/g, (match, argument) => formatAngleTex(convertInlineExpressionToTex(argument)));
      if (next === normalized) {
        break;
      }
      normalized = next;
    }

    normalized = replaceBarePiInMath(normalized);
    normalized = normalizeScientificNotationMath(normalized);
    return normalized;
  }

  function normalizeDelimitedMath(text) {
    const decoded = decodeLiteralUnicodeEscapes(text);
    return decoded.replace(/(\\\[|\\\()([\s\S]*?)(\\\]|\\\))/g, (match, left, body, right) => {
      return `${left}${normalizeStoredMathBody(body)}${right}`;
    });
  }

  function replaceMathFunctionCalls(text) {
    let output = "";
    let cursor = 0;

    while (cursor < text.length) {
      let replaced = false;

      for (const name of mathFunctionNames) {
        if (text.slice(cursor, cursor + name.length).toLowerCase() !== name) {
          continue;
        }
        if (!isIdentifierBoundary(text[cursor - 1])) {
          continue;
        }

        let openIndex = cursor + name.length;
        while (/\s/.test(text[openIndex] || "")) {
          openIndex += 1;
        }
        if (text[openIndex] !== "(") {
          continue;
        }

        const closeIndex = findMatchingParen(text, openIndex);
        if (closeIndex < 0) {
          continue;
        }

        const innerExpression = text.slice(openIndex + 1, closeIndex);
        const innerTex = convertInlineExpressionToTex(innerExpression);
        if (name === "sqrt") {
          output += `\\sqrt{${innerTex}}`;
        } else if (name === "abs") {
          output += `\\left|${innerTex}\\right|`;
        } else if (name === "radians") {
          output += formatAngleTex(innerTex);
        } else {
          output += `${mathFunctionLabel(name)}\\left(${innerTex}\\right)`;
        }
        cursor = closeIndex + 1;
        replaced = true;
        break;
      }

      if (!replaced) {
        output += text[cursor];
        cursor += 1;
      }
    }

    return output;
  }

  function replaceBareMathFunctions(text) {
    return String(text || "").replace(
      /\b(?:sin|cos|tan|asin|acos|atan|ln|log10|log|exp|abs|radians|degrees)\b/gi,
      (match, offset, source) => {
        if (!isIdentifierBoundary(source[offset - 1])) {
          return match;
        }
        return mathFunctionLabel(match.toLowerCase());
      },
    );
  }

  function replacePiConstant(text) {
    return String(text || "").replace(/\bpi\b/gi, (match, offset, source) => {
      if (!isIdentifierBoundary(source[offset - 1])) {
        return match;
      }
      return "\\pi";
    });
  }

  function replaceIdentifierSubscripts(text) {
    return String(text || "")
      .replace(
        /\b([A-Za-z][A-Za-z0-9]*)_([A-Za-z0-9]+)\b/g,
        (match, base, subscript) => {
          const baseTex = base.length === 1 ? base : `\\mathrm{${base}}`;
          return `${baseTex}_{\\mathrm{${subscript}}}`;
        },
      )
      .replace(
        /\b([A-Za-z]+)(\d+)\b/g,
        (match, base, subscript) => {
          const baseTex = base.length === 1 ? base : `\\mathrm{${base}}`;
          return `${baseTex}_{${subscript}}`;
        },
      );
  }

  function normalizeEquationTrailingText(text) {
    const normalized = String(text || "").trim();
    if (!normalized) {
      return "";
    }

    const leadingVerbPattern = /^,\s*(calculate|determine|estimate|find|compute|work out|show|state|identify|explain|what|which|how|when|where|why)\b/i;
    const match = leadingVerbPattern.exec(normalized);
    if (!match) {
      return ` ${normalized}`;
    }

    const verb = match[1];
    const sentenceTail = normalized
      .slice(match[0].length)
      .replace(/^\s+/, "");

    return `. ${verb.charAt(0).toUpperCase()}${verb.slice(1)}${sentenceTail ? ` ${sentenceTail}` : ""}`;
  }

  function convertInlineExpressionToTex(expression) {
    let normalized = decodeLiteralUnicodeEscapes(expression).replace(/\s+/g, " ").trim();
    if (!normalized) {
      return "";
    }
    normalized = normalized.replace(/\\(alpha|beta|gamma|delta|epsilon|theta|lambda|mu|pi|sigma|phi|omega)(?=[A-Za-z])/gi, "\\$1 ");
    normalized = normalizeScientificNotationMath(normalized);
    normalized = replaceMathFunctionCalls(normalized);
    normalized = replaceGreekCharacters(normalized);
    normalized = replaceBareMathFunctions(normalized);
    normalized = replacePiConstant(normalized);
    normalized = replaceIdentifierSubscripts(normalized);
    normalized = romanizeWordPhrases(normalized);
    normalized = normalized
      .replace(/×/g, " \\times ")
      .replace(/÷/g, " \\div ")
      .replace(/−/g, " - ")
      .replace(/\*/g, " \\times ")
      .replace(/\s*=\s*/g, " = ")
      .replace(/\s*\+\s*/g, " + ")
      .replace(/\s*-\s*/g, " - ")
      .replace(/\s*\/\s*/g, " / ")
      .replace(/\s+/g, " ")
      .trim();
    return normalized;
  }

  function isLikelyInlineEquation(text) {
    const normalized = decodeLiteralUnicodeEscapes(text).replace(/\s+/g, " ").trim();
    if (!normalized || !normalized.includes("=") || normalized.length > 160) {
      return false;
    }
    return (
      /[0-9]/.test(normalized)
      || /[_+\-*/^()]/.test(normalized)
      || /\b(?:sqrt|sin|cos|tan|asin|acos|atan|ln|log10|log|exp|abs|radians|degrees)\b/i.test(normalized)
      || /[α-ωΑ-Ω]/.test(normalized)
    );
  }

  function appendMathAwarePlainText(target, text) {
    const sourceText = decodeLiteralUnicodeEscapes(text);
    const equationPattern = new RegExp(inlineEquationPatternSource, "g");
    let lastIndex = 0;
    let match;

    while ((match = equationPattern.exec(sourceText))) {
      const plainPrefix = sourceText.slice(lastIndex, match.index);
      if (plainPrefix) {
        appendTextWithScientificNotation(target, plainPrefix);
      }

      const rawExpression = match[0];
      const splitExpression = splitEquationExpression(rawExpression);
      const expression = normalizeEquationExpression(splitExpression.equationText);
      if (isLikelyInlineEquation(expression)) {
        const inlineMath = document.createElement("span");
        inlineMath.className = "preview-inline-math";
        inlineMath.textContent = `\\(${convertInlineExpressionToTex(expression)}\\)`;
        target.appendChild(inlineMath);
        if (splitExpression.trailingText) {
          appendTextWithScientificNotation(target, normalizeEquationTrailingText(splitExpression.trailingText));
        }
      } else {
        appendTextWithScientificNotation(target, rawExpression);
      }
      lastIndex = match.index + rawExpression.length;
    }

    const trailing = sourceText.slice(lastIndex);
    if (trailing) {
      appendTextWithScientificNotation(target, trailing);
    }
  }

  function appendPlainTextContent(target, text) {
    const sourceText = String(text || "");
    let lastIndex = 0;

    sourceText.replace(mathDelimitedPattern, (match, offset) => {
      appendMathAwarePlainText(target, sourceText.slice(lastIndex, offset));
      target.appendChild(document.createTextNode(normalizeDelimitedMath(match)));
      lastIndex = offset + match.length;
      return match;
    });

    appendMathAwarePlainText(target, sourceText.slice(lastIndex));
  }

  function appendInlineMarkdown(target, inlineText) {
    const sourceText = String(inlineText || "");
    const tokenPattern = /`[^`\n]+`|\*\*(?=\S)[\s\S]*?\S\*\*|__(?=\S)[\s\S]*?\S__|\*(?=\S)[\s\S]*?\S\*|_(?=\S)[\s\S]*?\S_/g;
    let inlineLastIndex = 0;

    function looksLikeMathCollision(tokenText) {
      const innerText = String(tokenText || "").slice(1, -1);
      return (
        innerText.includes("=")
        || /[0-9]/.test(innerText)
        || /[+\-*/^]/.test(innerText)
        || /\b(?:sqrt|sin|cos|tan|asin|acos|atan|ln|log10|log|exp|abs|radians|degrees)\b/i.test(innerText)
      );
    }

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
        if (looksLikeMathCollision(tokenText)) {
          appendPlainTextContent(targetNode, tokenText);
          return;
        }
        const emphasis = document.createElement("em");
        appendInlineMarkdown(emphasis, tokenText.slice(1, -1));
        targetNode.appendChild(emphasis);
        return;
      }
      appendPlainTextContent(targetNode, tokenText);
    }

    sourceText.replace(tokenPattern, (match, offset) => {
      const plainText = sourceText.slice(inlineLastIndex, offset);
      if (plainText) {
        appendPlainTextContent(target, plainText);
      }
      appendToken(target, match);
      inlineLastIndex = offset + match.length;
      return match;
    });

    const trailingText = sourceText.slice(inlineLastIndex);
    if (trailingText) {
      appendPlainTextContent(target, trailingText);
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
