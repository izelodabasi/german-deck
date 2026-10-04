const STORAGE_KEY = "german-deck-progress-v1";
const MIGRATION_KEY = "german-deck-progress-db-migrated-v1";

const elements = {
  totalCards: document.querySelector("#totalCards"),
  seenCards: document.querySelector("#seenCards"),
  accuracy: document.querySelector("#accuracy"),
  front: document.querySelector("#front"),
  answerPanel: document.querySelector("#answerPanel"),
  back: document.querySelector("#back"),
  showAnswer: document.querySelector("#showAnswer"),
  wrong: document.querySelector("#wrong"),
  correct: document.querySelector("#correct"),
  cardShown: document.querySelector("#cardShown"),
  cardTrue: document.querySelector("#cardTrue"),
  cardFalse: document.querySelector("#cardFalse"),
  lastShown: document.querySelector("#lastShown"),
  lastCorrect: document.querySelector("#lastCorrect"),
  resetProgress: document.querySelector("#resetProgress"),
};

let cards = [];
let progress = {};
let currentCard = null;
let showingAnswer = false;

function defaultStats() {
  return {
    shownCount: 0,
    trueCount: 0,
    falseCount: 0,
    lastShownAt: null,
    lastCorrectAt: null,
  };
}

function loadProgress() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
    return stored && typeof stored === "object" ? stored : {};
  } catch {
    return {};
  }
}

async function api(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload ?? {}),
  });

  if (!response.ok) throw new Error(`API ${path} failed with HTTP ${response.status}`);
  return response.json();
}

async function loadDbProgress() {
  const response = await fetch("/api/progress");
  if (!response.ok) throw new Error(`Could not load DB progress: HTTP ${response.status}`);
  const data = await response.json();
  return data.progress ?? {};
}

async function migrateLocalStorageProgress() {
  if (localStorage.getItem(MIGRATION_KEY) === "yes") return;

  const localProgress = loadProgress();
  if (Object.keys(localProgress).length > 0) {
    await api("/api/import-progress", { progress: localProgress });
  }

  localStorage.setItem(MIGRATION_KEY, "yes");
}

function statsFor(cardId) {
  progress[cardId] = { ...defaultStats(), ...progress[cardId] };
  return progress[cardId];
}

function formatTime(timestamp) {
  if (!timestamp) return "Never";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp));
}

function formatFrontText(text) {
  return text.replace(/\n+/g, "\n\n");
}

function sanitizeHtml(html) {
  const template = document.createElement("template");
  template.innerHTML = html.replace(/(?:<br>\s*){3,}/gi, "<br><br>");
  const allowedTags = new Set(["BR", "SPAN", "B", "I", "U", "STRONG", "EM"]);

  [...template.content.querySelectorAll("*")].forEach((node) => {
    if (!allowedTags.has(node.tagName)) {
      node.replaceWith(...node.childNodes);
      return;
    }

    [...node.attributes].forEach((attribute) => {
      if (attribute.name !== "style") {
        node.removeAttribute(attribute.name);
      }
    });

    if (node.hasAttribute("style")) {
      const color = node.style.color;
      node.removeAttribute("style");
      if (color) node.style.color = color;
    }
  });

  return template.innerHTML;
}

function cardWeight(card) {
  const stats = statsFor(card.id);
  const now = Date.now();
  const lastCorrectAt = stats.lastCorrectAt ?? 0;
  const daysSinceCorrect = Math.max(1, (now - lastCorrectAt) / 86400000);
  const mistakeBoost = 1 + stats.falseCount * 0.35;
  const newCardBoost = stats.shownCount === 0 ? 12 : 1;
  return Math.log2(daysSinceCorrect + 1) * mistakeBoost + newCardBoost;
}

function chooseCard() {
  const previousId = currentCard?.id;
  const candidates = cards.length > 1 ? cards.filter((card) => card.id !== previousId) : cards;
  const weights = candidates.map(cardWeight);
  const total = weights.reduce((sum, weight) => sum + weight, 0);
  let marker = Math.random() * total;

  for (let index = 0; index < candidates.length; index += 1) {
    marker -= weights[index];
    if (marker <= 0) return candidates[index];
  }

  return candidates[candidates.length - 1];
}

function updateSummary() {
  const allStats = cards.map((card) => statsFor(card.id));
  const seen = allStats.filter((stats) => stats.shownCount > 0).length;
  const trueCount = allStats.reduce((sum, stats) => sum + stats.trueCount, 0);
  const shownCount = allStats.reduce((sum, stats) => sum + stats.trueCount + stats.falseCount, 0);
  const accuracy = shownCount ? Math.round((trueCount / shownCount) * 100) : 0;

  elements.totalCards.textContent = String(cards.length);
  elements.seenCards.textContent = String(seen);
  elements.accuracy.textContent = `${accuracy}%`;
}

function updateCurrentStats() {
  if (!currentCard) return;
  const stats = statsFor(currentCard.id);
  elements.cardShown.textContent = String(stats.shownCount);
  elements.cardTrue.textContent = String(stats.trueCount);
  elements.cardFalse.textContent = String(stats.falseCount);
  elements.lastShown.textContent = formatTime(stats.lastShownAt);
  elements.lastCorrect.textContent = formatTime(stats.lastCorrectAt);
}

function renderCard() {
  if (!currentCard) return;

  showingAnswer = false;
  elements.front.textContent = formatFrontText(currentCard.front);
  elements.back.innerHTML = "";
  elements.answerPanel.classList.add("hidden");
  elements.showAnswer.classList.remove("hidden");
  elements.wrong.classList.add("hidden");
  elements.correct.classList.add("hidden");
  updateSummary();
  updateCurrentStats();
}

async function nextCard() {
  currentCard = chooseCard();
  renderCard();
}

function showAnswer() {
  if (!currentCard || showingAnswer) return;
  showingAnswer = true;
  elements.back.innerHTML = sanitizeHtml(currentCard.back);
  elements.answerPanel.classList.remove("hidden");
  elements.showAnswer.classList.add("hidden");
  elements.wrong.classList.remove("hidden");
  elements.correct.classList.remove("hidden");
}

async function gradeCard(wasCorrect) {
  if (!currentCard || !showingAnswer) return;
  elements.wrong.disabled = true;
  elements.correct.disabled = true;

  try {
    const { stats } = await api("/api/grade", {
      cardId: currentCard.id,
      wasCorrect,
      now: Date.now(),
    });
    progress[currentCard.id] = stats;
    await nextCard();
  } finally {
    elements.wrong.disabled = false;
    elements.correct.disabled = false;
  }
}

async function boot() {
  try {
    await migrateLocalStorageProgress();
    progress = await loadDbProgress();

    const response = await fetch("data/cards.json");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    cards = data.cards;
  } catch (error) {
    elements.front.textContent = "Could not load data/cards.json. Start this with a local web server.";
    console.error(error);
    return;
  }

  if (!cards.length) {
    elements.front.textContent = "No cards found in the deck.";
    return;
  }

  await nextCard();
}

elements.showAnswer.addEventListener("click", showAnswer);
elements.wrong.addEventListener("click", () => gradeCard(false));
elements.correct.addEventListener("click", () => gradeCard(true));
elements.resetProgress.addEventListener("click", async () => {
  if (!confirm("Reset all saved progress for this deck?")) return;
  progress = {};
  await api("/api/reset");
  await nextCard();
});

window.addEventListener("keydown", (event) => {
  if (event.key === " ") {
    event.preventDefault();
    showAnswer();
  }
  if (event.key === "1") gradeCard(false);
  if (event.key === "2") gradeCard(true);
});

boot();
