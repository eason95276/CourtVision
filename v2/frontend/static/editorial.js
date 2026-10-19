const motionPreference = matchMedia("(prefers-reduced-motion: reduce)");
let visualTransition = null;
let openedGameId = null;

function continueScene(update, shared = null) {
  visualTransition?.skipTransition();
  clearSharedElements();
  if (!document.startViewTransition || motionPreference.matches) {
    update();
    return;
  }
  shared?.before?.();
  const transition = document.startViewTransition(() => {
    clearSharedElements();
    update();
    shared?.after?.();
  });
  transition.ready.catch(() => {});
  transition.updateCallbackDone.catch(() => {});
  visualTransition = transition;
  transition.finished
    .catch(() => {})
    .finally(() => {
      clearSharedElements();
      if (visualTransition === transition) visualTransition = null;
    });
}

function clearSharedElements() {
  document.querySelectorAll("[data-scene-element]").forEach((el) => {
    el.style.viewTransitionName = "";
    delete el.dataset.sceneElement;
  });
}

function share(element, name) {
  if (!element) return;
  element.dataset.sceneElement = name;
  element.style.viewTransitionName = name;
}

function shareMatch(scope) {
  if (!scope) return;
  const teams = scope.querySelectorAll(".card-team,.team-stage,.reading-team");
  ["away", "home"].forEach((side, i) => {
    share(teams[i]?.querySelector(".team-logo-wrap"), `${side}-crest`);
    share(teams[i]?.querySelector("strong"), `${side}-name`);
  });
  share(
    scope.querySelector(
      ".card-probability,#predicted-probability,.reading-probability",
    ),
    "forecast-number",
  );
}

const originalGameCard = gameCard;
gameCard = function (game, index) {
  const card = originalGameCard(game, index);
  card.dataset.gameId = game.id;
  card.style.setProperty("--away-color", game.away.accent);
  card.style.setProperty("--home-color", game.home.accent);
  const teams = card.querySelectorAll(".card-team");
  teams[0].append(text("span", "客场", "venue-label"));
  teams[1].append(text("span", "主场", "venue-label"));
  card.append(text("span", "比赛前瞻 ↗", "card-read"));
  if (typeof decorateBroadcastCard === "function") decorateBroadcastCard(card, game);
  return card;
};

const originalRenderGames = renderGames;
renderGames = function () {
  originalRenderGames();
  const cards = [...byId("games-grid").children];
  const focused =
    cards.find((card) => card.dataset.gameId === openedGameId) || cards[0];
  focused?.classList.add("expanded-game");
  const sample = state.data.games.find(
    (game) => chinaDateKey(game.tipoff) === state.selectedDate,
  );
  const title = document.querySelector(".section-toolbar h2");
  if (sample) title.textContent = displayDate(sample.tipoff, false);
  document.querySelector(".section-kicker").textContent =
    `${cards.length} 场对阵 · 北京时间`;
  if (typeof renderHomeEdition === "function") renderHomeEdition();
};

const originalDateFilters = setupDateFilters;
setupDateFilters = function () {
  originalDateFilters();
  [...byId("date-filters").children].forEach((old) => {
    const button = old.cloneNode(true);
    if (button.getAttribute("aria-pressed") === "true")
      button.style.viewTransitionName = "matchday-selection";
    button.addEventListener("click", () => {
      if (state.selectedDate === button.dataset.date) return;
      continueScene(() => {
        state.selectedDate = button.dataset.date;
        setupDateFilters();
        renderHome();
        [...byId("date-filters").children]
          .find((item) => item.dataset.date === state.selectedDate)
          ?.focus({ preventScroll: true });
      });
    });
    old.replaceWith(button);
  });
};

const originalRenderDetail = renderDetail;
renderDetail = function (game) {
  originalRenderDetail(game);
  const hero = document.querySelector(".match-hero");
  hero.style.setProperty("--away-color", game.away.accent);
  hero.style.setProperty("--home-color", game.home.accent);
  const current = activePrediction(game);
  hero.dataset.winner = current
    ? current.winner_team_id === game.home.id ? "home" : "away"
    : "unavailable";
  for (const side of ["away", "home"]) {
    const team = byId(`${side}-team`);
    team.dataset.abbr = game[side].abbreviation;
    team.append(text("span", side === "home" ? "主场" : "客场", "venue-label"));
  }
  byId("ai-analysis").querySelector("strong").textContent =
    state.currentAnalyses.has(game.id)
    ? "继续阅读第25秒分析"
    : "阅读第25秒分析";
  byId("ai-analysis").querySelector("small").textContent =
    "从数据进入这场比赛的判断";
  byId("ai-analysis").querySelector(".analyst-symbol").textContent = "24 → 25";
  document.querySelector(".factors-section .section-title p").textContent =
    "赛前数据线索";
  if (typeof renderBroadcastDetail === "function") renderBroadcastDetail(game);
};

const originalRoute = route;
route = function () {
  if (!state.data) return;
  const target = location.hash.startsWith("#game/")
    ? decodeURIComponent(location.hash.slice(6))
    : null;
  const previous = state.activeGame?.id;
  if (!target && previous) openedGameId = previous;
  const sourceScope = previous
    ? document.querySelector(".match-hero")
    : [...document.querySelectorAll(".game-card")].find(
        (card) => card.dataset.gameId === target,
      );
  const update = () => {
    originalRoute();
    document.title = document.title.replace('看见比分之外。', '看见比分之外');
    if (!state.activeGame) {
      renderGames();
      const card = [...document.querySelectorAll(".game-card")].find(
        (card) => card.dataset.gameId === openedGameId,
      );
      window.scrollTo({ top: state.homeScroll, behavior: "instant" });
      if (previous) card?.focus({ preventScroll: true });
    }
  };
  const targetScope = () =>
    state.activeGame
      ? document.querySelector(".match-hero")
      : [...document.querySelectorAll(".game-card")].find(
          (card) => card.dataset.gameId === openedGameId,
        );
  if (previous || target)
    continueScene(update, {
      before: () => shareMatch(sourceScope),
      after: () => shareMatch(targetScope()),
    });
  else update();
};

function readingHeader(game, options = {}) {
  const header = document.querySelector(".analysis-sheet > header");
  const sheet = document.querySelector(".analysis-sheet");
  const mode = options.mode === "postgame" ? "postgame" : "pregame";
  const frozen = options.prediction || activePrediction(game);
  sheet.dataset.readerMode = mode;
  sheet.dataset.awayAbbr = game.away.abbreviation;
  sheet.dataset.homeAbbr = game.home.abbreviation;
  sheet.dataset.matchLabel = `${game.away.abbreviation}  /  ${game.home.abbreviation}`;
  sheet.style.setProperty("--away-logo-image", `url("${logoSource(game.away)}")`);
  sheet.style.setProperty("--home-logo-image", `url("${logoSource(game.home)}")`);
  header.querySelector(".reading-match")?.remove();
  const summary = document.createElement("div");
  summary.className = "reading-match";
  for (const side of ["away", "home"]) {
    const team = document.createElement("div");
    team.className = "reading-team";
    team.append(logo(game[side]), text("strong", game[side].name_zh));
    summary.append(team);
  }
  const forecast = document.createElement("div");
  forecast.className = "reading-forecast";
  const winnerName = mode === "postgame"
    ? options.prediction?.winner_name || (frozen?.winner_team_id ? teamById(game, frozen.winner_team_id).name_zh : null)
    : frozen ? teamById(game, frozen.winner_team_id).name_zh : null;
  forecast.append(
    text(
      "span",
      frozen
        ? (mode === "postgame" ? "赛前预测 · " : "当前预测 · ") + (winnerName || "")
        : "尚无已发布预测",
    ),
    text(
      "strong",
      frozen ? percent(frozen.winner_probability) : "—",
      "reading-probability",
    ),
  );
  summary.append(forecast);
  header.insertBefore(summary, byId("close-sheet"));
  header.querySelector("h2").textContent = mode === "postgame" ? "第25秒赛后复盘" : "第25秒分析";
}

const originalOpenSheet = openSheet;
openSheet = function (options = {}) {
  if (!byId("ai-sheet").hidden) return;
  const trigger = document.activeElement;
  clearSharedElements();
  readingHeader(state.activeGame, options);
  continueScene(() => {
    clearSharedElements();
    originalOpenSheet();
    state.previousFocus = trigger;
    document.body.classList.add("reading-mode");
  });
};

const originalCloseSheet = closeSheet;
closeSheet = function () {
  if (byId("ai-sheet").hidden) return;
  clearSharedElements();
  continueScene(() => {
    clearSharedElements();
    document.body.classList.remove("reading-mode");
    originalCloseSheet();
  });
};

const originalRenderJudgment = renderJudgment;
renderJudgment = function (payload) {
  originalRenderJudgment(payload);
  const tabs = document.querySelector(".analysis-tabs");
  if (!tabs) {
    byId("sheet-content").classList.add("has-analysis");
    return;
  }
  tabs.setAttribute("role", "navigation");
  tabs.setAttribute("aria-label", "分析章节");
  [...tabs.children].forEach((button, index) => {
    const link = text("a", ["为什么这么看", "关键球员与对位", "风险与未知"][index]);
    link.href = `#analysis-panel-${index}`;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const panel = byId(`analysis-panel-${index}`);
      panel.scrollIntoView({
        behavior: motionPreference.matches ? "instant" : "smooth",
        block: "start",
      });
      panel.focus({ preventScroll: true });
    });
    button.replaceWith(link);
    const panel = byId(`analysis-panel-${index}`);
    panel.hidden = false;
    panel.setAttribute("role", "region");
    panel.removeAttribute("aria-labelledby");
    panel.setAttribute(
      "aria-label",
      ["为什么这么看", "关键球员与对位", "风险与未知"][index],
    );
  });
  byId("sheet-content").classList.add("has-analysis");
};

const originalRequestAi = requestAiAnalysis;
requestAiAnalysis = async function () {
  byId("sheet-content").classList.remove("has-analysis");
  await originalRequestAi();
};

document.addEventListener("DOMContentLoaded", () => {
  document.querySelector(".brand-mark").textContent = "25";
  document.querySelector(".section-toolbar h2").textContent = "比赛日";
  document.querySelector(".analysis-sheet header h2").textContent = "第25秒分析";
  byId("close-sheet").setAttribute("aria-label", "返回比赛");
  const intro = byId("brand-intro");
  let seen = false;
  try {
    seen = sessionStorage.getItem("25th-second.introSeen") === "1";
  } catch (_) {
    seen = true;
  }
  if (motionPreference.matches || seen) {
    intro.hidden = true;
    document.body.classList.add("intro-complete");
    return;
  }
  document.body.classList.add("intro-playing");
  window.setTimeout(() => {
    document.body.classList.add("intro-complete");
    document.body.classList.remove("intro-playing");
    try {
      sessionStorage.setItem("25th-second.introSeen", "1");
    } catch (_) {}
    window.setTimeout(() => {
      intro.hidden = true;
    }, 450);
  }, 1700);
});
