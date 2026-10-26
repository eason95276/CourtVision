const state = {
  data: null,
  selectedDate: null,
  favoriteTeamId: null,
  activeGame: null,
  homeScroll: 0,
  previousFocus: null,
  aiFailures: new Set(),
  aiPending: false,
  currentAnalyses: new Map(),
  editorialOverlays: new Map(),
  predictionStates: new Map(),
  factViews: new Map(),
  marketCache: new Map(),
  marketPending: new Map(),
  marketFetchedAt: new Map(),
  gameLoads: new Map(),
  refreshTimer: null,
  currentChinaDay: null,
};

// Temporary opening-day presentation only. No report activation or generation.
const HOME_OPENING_PREVIEW = {
  enabled: false,
  date: "2026-10-21",
  gameIds: new Set(["espn:401909088", "espn:401909089", "espn:401909090"]),
};

const LOCAL_LOGO_TEAMS = new Set([
  "ATL",
  "BKN",
  "BOS",
  "CHA",
  "CHI",
  "DET",
  "MIA",
  "MIL",
  "MIN",
  "NY",
  "OKC",
  "ORL",
  "PHI",
  "SA",
  "TOR",
  "WSH",
]);

const PLAYER_DISPLAY_NAMES = {
  "LeBron James": "勒布朗·詹姆斯",
  "Kentavious Caldwell-Pope": "肯塔维奥斯·考德威尔-波普",
  "Anfernee Simons": "安芬尼·西蒙斯",
  "VJ Edgecombe": "VJ·埃奇库姆",
  "Andre Drummond": "安德烈·德拉蒙德",
  "Jordan Clarkson": "乔丹·克拉克森",
  "Miles McBride": "迈尔斯·麦克布赖德",
  "Jose Alvarado": "何塞·阿尔瓦拉多",
  "Kevin McCullar Jr.": "凯文·麦克卡勒",
  "Tyler Kolek": "泰勒·科勒克",
  "Landry Shamet": "兰德里·沙梅特",
  "Mohamed Diawara": "穆罕默德·迪亚瓦拉",
  "Jack Kayil": "杰克·凯伊尔",
  "Tyler Nickel": "泰勒·尼克尔",
};

const byId = (id) => document.getElementById(id);
const text = (tag, value, className) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = playerDisplayCopy(value);
  return node;
};
const numberFormats = new Map();
const dateFormats = new Map();
const number = (value, digits = 1) => {
  if (!numberFormats.has(digits)) numberFormats.set(digits, new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }));
  return numberFormats.get(digits).format(Number(value));
};
function dateTimeFormat(locale, options) {
  const key = locale + JSON.stringify(options);
  if (!dateFormats.has(key)) dateFormats.set(key, new Intl.DateTimeFormat(locale, options));
  return dateFormats.get(key);
}
const percent = (value, digits = 1) =>
  `${number(Number(value) * 100, digits)}%`;

function teamById(game, id) {
  return game.home.id === Number(id) ? game.home : game.away;
}

function activePrediction(game) {
  const selection = state.predictionStates.get(game.id);
  if (selection?.status === "locked_unavailable" || selection?.status === "schedule_changed") {
    return null;
  }
  const payload = state.currentAnalyses.get(game.id);
  const final = payload?.judgment?.final_prediction;
  if (!final || !["home", "away"].includes(final.winner_side)) {
    if (game.schedule_catalog_fact) return null;
    if (game.registered_game || !game.prediction || game.prediction.available === false) return null;
    return { ...game.prediction, updated_at: null, composite: false };
  }
  const winner = game[final.winner_side];
  return {
    winner_team_id: winner.id,
    winner_probability: Number(final.win_probability),
    expected_home_margin:
      final.winner_side === "home"
        ? Number(final.expected_margin)
        : -Number(final.expected_margin),
    expected_total: Number(final.expected_total),
    updated_at: payload.published_at || payload.generated_at || null,
    composite: true,
    version_id: payload.version_id,
  };
}

function acceptPredictionSelection(gameId, selection) {
  if (!selection || selection.game_id !== gameId) return false;
  const game = state.data?.games.find((item) => item.id === gameId);
  if (selection.version) {
    if (
      !game ||
      selection.version_id !== selection.version.version_id ||
      !analysisCompatible(game, selection.version)
    ) return false;
  }
  const factView = selection.fact_view;
  const version = selection.version;
  let compatibleFactView = Boolean(
    factView && version &&
    factView.schema_version === "pregame-fact-view-v1" &&
    factView.game_id === gameId &&
    factView.version_id === selection.version_id &&
    factView.source_payload_sha256 === version.payload_sha256 &&
    factView.context?.schema_version === "pregame-fact-context-v1" &&
    factView.context?.game_id === gameId
  );
  if (compatibleFactView && factView.context.feature_cutoff) {
    const factCutoff = Date.parse(factView.context.feature_cutoff);
    const inputCutoff = Date.parse(version.input_cutoff_at || "");
    const tipoff = Date.parse(game?.tipoff || "");
    compatibleFactView = Number.isFinite(factCutoff) && Number.isFinite(inputCutoff) &&
      Number.isFinite(tipoff) && factCutoff <= inputCutoff && factCutoff < tipoff;
  }
  const previous = state.predictionStates.get(gameId);
  const revision = Number(selection.state_revision ?? 0);
  const previousRevision = Number(previous?.state_revision ?? -1);
  if (revision < previousRevision) return false;
  if (
    revision === previousRevision &&
    previous?.version_id &&
    selection.version_id &&
    previous.version_id !== selection.version_id
  ) return false;
  state.predictionStates.set(gameId, selection);
  const editorial = selection.display_projection?.portfolio_editorial;
  if (version && editorial?.schema_version === "portfolio-editorial-v1" &&
      editorial.base_prediction_version_id === version.version_id &&
      editorial.base_prediction_payload_sha256 === version.payload_sha256 &&
      editorial.editorial && typeof editorial.editorial.analysis_article === "string") {
    state.editorialOverlays.set(gameId, {
      basePredictionVersionId: editorial.base_prediction_version_id,
      editorial: editorial.editorial,
    });
  } else {
    state.editorialOverlays.delete(gameId);
  }
  if (compatibleFactView) state.factViews.set(gameId, factView);
  else state.factViews.delete(gameId);
  if (previous?.version_id && previous.version_id !== selection.version_id) {
    state.marketCache.delete(gameId); state.marketFetchedAt.delete(gameId);
  }
  if (selection.version?.judgment?.final_prediction) {
    state.currentAnalyses.set(gameId, selection.version);
    state.aiFailures.delete(gameId);
  } else {
    state.currentAnalyses.delete(gameId);
  }
  if (state.activeGame?.id === gameId && typeof renderDetail === "function") {
    renderDetail(state.activeGame);
  }
  return true;
}

function analysisCompatible(game, payload) {
  if (!payload?.judgment?.final_prediction) return false;
  if (typeof payload.version_id !== "string" || !payload.version_id) return false;
  if (!payload.input_snapshot || typeof payload.input_snapshot !== "object") return false;
  if (![
    "matchup-history-v1",
    "two-stage-forecast-v1",
  ].includes(payload.analysis_contract_version)) return false;
  if (payload.game_id !== game.id) return false;
  if (Number(payload.schedule_revision ?? 1) !== Number(game.schedule_revision ?? 1)) return false;
  const identity = payload.game_identity;
  if (!identity) return false;
  if (identity.game_id !== game.id) return false;
  if (Number(identity.home_team_id) !== Number(game.home.id)) return false;
  if (Number(identity.away_team_id) !== Number(game.away.id)) return false;
  const cachedTipoff = Date.parse(identity.scheduled_tipoff || "");
  const currentTipoff = Date.parse(game.tipoff || "");
  const storedCutoff = Date.parse(payload.input_cutoff_at || "");
  const legacyCutoff = Date.parse(payload.cutoff || "");
  if (payload.input_cutoff_at !== undefined && !Number.isFinite(storedCutoff)) return false;
  if (payload.cutoff !== undefined && !Number.isFinite(legacyCutoff)) return false;
  if (
    Number.isFinite(storedCutoff) &&
    Number.isFinite(legacyCutoff) &&
    storedCutoff !== legacyCutoff
  ) return false;
  if (payload.analysis_contract_version === "two-stage-forecast-v1" && !Number.isFinite(storedCutoff)) return false;
  const cutoff = Number.isFinite(storedCutoff) ? storedCutoff : legacyCutoff;
  return (
    Number.isFinite(cachedTipoff) &&
    cachedTipoff === currentTipoff &&
    Number.isFinite(cutoff) &&
    cutoff < currentTipoff &&
    typeof payload.base_input_hash === "string" &&
    payload.base_input_hash.length > 0 &&
    Number.isFinite(Date.parse(payload.published_at || "")) &&
    typeof payload.payload_sha256 === "string" &&
    payload.payload_sha256.length === 64
  );
}

function chinaDateKey(value) {
  const parts = dateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(value));
  const record = Object.fromEntries(
    parts.map((part) => [part.type, part.value]),
  );
  return `${record.year}-${record.month}-${record.day}`;
}

function displayDate(value, includeTime = true) {
  return dateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "long",
    day: "numeric",
    weekday: "short",
    ...(includeTime
      ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : {}),
  }).format(new Date(value));
}

function displayClock(value) {
  return dateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function statusLabel(game) {
  if (game.status === "scheduled") {
    const tipoff = Date.parse(game.tipoff || "");
    return Number.isFinite(tipoff) && tipoff <= Date.now() ? "赛况待更新" : "未开始";
  }
  if (["in", "in_progress", "live"].includes(game.status)) return "进行中";
  if (["post", "final", "completed", "finished"].includes(game.status)) return "已结束";
  if (["cancelled", "canceled"].includes(game.status)) return "已取消";
  if (["postponed", "suspended"].includes(game.status)) return "已改期";
  return game.status || "状态未知";
}

function hasVisibleScore(game) {
  return ["in", "in_progress", "live", "post", "final", "completed", "finished"].includes(game.status) &&
    game.home?.score !== null && game.home?.score !== undefined &&
    game.away?.score !== null && game.away?.score !== undefined &&
    Number.isFinite(Number(game.home.score)) && Number.isFinite(Number(game.away.score));
}

function scoreText(value) {
  const score = Number(value);
  return Number.isInteger(score) ? String(score) : number(score);
}

function safeFavorite() {
  try {
    const stored = localStorage.getItem("25th-second.favoriteTeam");
    return stored ? Number(stored) : null;
  } catch (_) {
    return null;
  }
}

function saveFavorite(value) {
  try {
    if (value) localStorage.setItem("25th-second.favoriteTeam", String(value));
    else localStorage.removeItem("25th-second.favoriteTeam");
  } catch (_) {
    // Preference storage can be unavailable in a hardened browser.
  }
}

function applyFavoriteTheme() {
  const selected = state.data?.teams.find(
    (team) => team.id === state.favoriteTeamId,
  );
  document.documentElement.style.setProperty(
    "--team-accent",
    selected?.accent || "#8b95a8",
  );
  const home = byId("home-view");
  if (home) {
    home.dataset.favoriteAbbr = selected?.abbreviation || "25";
    home.classList.toggle("has-favorite-theme", Boolean(selected));
    home.style.setProperty(
      "--favorite-logo-image",
      selected ? `url("${logoSource(selected)}")` : "none",
    );
  }
  document.body.dataset.favoriteTheme = selected ? "team" : "brand";
  byId("favorite-team").value = selected ? String(selected.id) : "";
}

function logoSource(team) {
  return LOCAL_LOGO_TEAMS.has(team.abbreviation)
    ? `logos/${team.abbreviation.toLowerCase()}.png`
    : team.logo_url;
}

function logo(team, size = "card") {
  const wrap = document.createElement("span");
  wrap.className = `team-logo-wrap ${size}`;
  const image = document.createElement("img");
  image.className = "team-logo";
  const source = logoSource(team);
  image.src = source;
  image.decoding = "async";
  image.alt = `${team.name_zh}队徽`;
  image.width = size === "stage" ? 140 : 52;
  image.height = size === "stage" ? 140 : 52;
  image.loading = size === "stage" ? "eager" : "lazy";
  image.addEventListener("load", () => wrap.classList.add("is-loaded"), {
    once: true,
  });
  image.addEventListener("error", () => wrap.classList.add("is-fallback"), {
    once: true,
  });
  const fallback = text("span", team.abbreviation, "logo-fallback");
  fallback.setAttribute("aria-hidden", "true");
  wrap.append(image, fallback);
  return wrap;
}

function setupFavoritePicker() {
  const select = byId("favorite-team");
  const fragment = document.createDocumentFragment();
  [...state.data.teams]
    .sort((a, b) => a.name_zh.localeCompare(b.name_zh, "zh-CN"))
    .forEach((team) => {
      const option = document.createElement("option");
      option.value = String(team.id);
      option.textContent = team.name_zh;
      fragment.append(option);
    });
  select.append(fragment);
  state.favoriteTeamId = safeFavorite();
  applyFavoriteTheme();
  select.addEventListener("change", () => {
    state.favoriteTeamId = select.value ? Number(select.value) : null;
    saveFavorite(state.favoriteTeamId);
    applyFavoriteTheme();
    renderHome();
    if (state.activeGame) renderDetail(state.activeGame);
  });
}

function homeCollectionOptions() {
  if (HOME_OPENING_PREVIEW.enabled) {
    return [
      { key: "opening", label: "揭幕战", subtitle: "10月21日" },
      { key: "published", label: "已发布", subtitle: "赛前 / 赛后" },
    ];
  }
  return [
    { key: state.todayDate, label: "今天", subtitle: displayDate(`${state.todayDate}T12:00:00+08:00`, false) },
    { key: state.tomorrowDate, label: "明天", subtitle: displayDate(`${state.tomorrowDate}T12:00:00+08:00`, false) },
    { key: "published", label: "已发布", subtitle: "赛前 / 赛后" },
  ];
}

function gameInHomeCollection(game, collection) {
  const date = chinaDateKey(game.tipoff);
  if (collection === "opening") {
    return HOME_OPENING_PREVIEW.gameIds.has(game.id) && date === HOME_OPENING_PREVIEW.date;
  }
  if (collection === "published") {
    // Only accepted published versions; no static prediction fallback or future day-two promotion.
    const published = Boolean(state.currentAnalyses.get(game.id)?.version_id || game.postgame?.version_id);
    return published && (!HOME_OPENING_PREVIEW.enabled || date < HOME_OPENING_PREVIEW.date);
  }
  return game.homepage_visible !== false && date === collection;
}

function setupDateFilters() {
  const today = state.data.schedule_window?.today || chinaDateKey(new Date());
  const tomorrow = state.data.schedule_window?.tomorrow || chinaDateKey(new Date(Date.parse(`${today}T12:00:00+08:00`) + 86400000));
  state.todayDate = today;
  state.tomorrowDate = tomorrow;
  const options = homeCollectionOptions();
  const dates = options.map((option) => option.key);
  state.selectedDate =
    state.selectedDate && dates.includes(state.selectedDate)
      ? state.selectedDate
      : dates[0];
  const container = byId("date-filters");
  container.replaceChildren();
  options.forEach(({ key: date, label, subtitle }) => {
    const button = document.createElement("button");
    button.append(text("span", label));
    button.append(text("small", subtitle));
    button.className = "date-filter-button";
    button.type = "button";
    button.dataset.date = date;
    button.setAttribute("aria-pressed", String(date === state.selectedDate));
    button.addEventListener("click", () => {
      state.selectedDate = date;
      setupDateFilters();
      renderHome();
      [...container.children]
        .find((item) => item.dataset.date === date)
        ?.focus({ preventScroll: true });
    });
    container.append(button);
  });
}

function cardTeam(team) {
  const node = document.createElement("div");
  node.className = "card-team";
  node.append(
    logo(team),
    text("strong", team.name_zh),
    text("small", team.abbreviation),
  );
  return node;
}

function playerName(player) {
  if (typeof player === "string") return analystCopy(player);
  const canonical = player?.name || "";
  return (
    (player?.display_name && player.display_name !== canonical
      ? player.display_name
      : null) ||
    PLAYER_DISPLAY_NAMES[canonical] ||
    state.activeGame?.player_display_names?.find((p) => p.canonical_name_en === canonical)?.display_name_zh ||
    canonical ||
    "球员"
  );
}

function rosterFor(game, side) {
  return game.roster_context?.teams?.[side]?.official_team_roster || null;
}

function gameCard(game, index) {
  const active = activePrediction(game);
  const predicted = active ? teamById(game, active.winner_team_id) : null;
  const favorite =
    state.favoriteTeamId &&
    [game.home.id, game.away.id].includes(state.favoriteTeamId);
  const card = document.createElement("a");
  card.className = `game-card${favorite ? " favorite" : ""}`;
  card.href = `#game/${encodeURIComponent(game.id)}`;
  card.style.setProperty(
    "--card-accent",
    favorite
      ? teamById(game, state.favoriteTeamId).accent
      : "var(--team-accent)",
  );
  card.style.setProperty("--index", String(index));
  card.setAttribute(
    "aria-label",
    `查看${game.away.name_zh}对${game.home.name_zh}比赛详情`,
  );

  const inner = document.createElement("div");
  inner.className = "card-inner";
  const top = document.createElement("div");
  top.className = "card-top";
  top.append(
    text("span", displayDate(game.tipoff)),
    text("span", statusLabel(game)),
  );
  if (favorite) top.append(text("span", "主队比赛", "favorite-label"));

  const matchup = document.createElement("div");
  matchup.className = "card-matchup";
  matchup.append(
    cardTeam(game.away),
    text("span", "VS", "versus"),
    cardTeam(game.home),
  );

  const prediction = document.createElement("div");
  prediction.className = "card-prediction";
  if (hasVisibleScore(game)) {
    const final = ["post", "final", "completed", "finished"].includes(game.status);
    prediction.classList.add("score-prediction");
    prediction.append(
      text("p", final ? "最终比分" : "当前比分"),
      text("strong", `${scoreText(game.away.score)} — ${scoreText(game.home.score)}`, "card-score"),
    );
  } else if (active && predicted) {
    const copy = document.createElement("p");
    copy.append("预测：", text("strong", predicted.name_zh));
    prediction.append(copy, text("span", percent(active.winner_probability), "card-probability"));
  } else {
    prediction.append(text("p", game.registered_game ? "尚无正式赛前分析" : "尚无正式预测", "prediction-unavailable"));
  }

  const insights = document.createElement("ul");
  insights.className = "card-insights";
  insights.append(
    text("li", active ? marginLabel(game) : hasVisibleScore(game) ? `比赛状态：${statusLabel(game)}` : "赛前判断尚未发布"),
    text(
      "li",
      active?.updated_at
        ? `更新于 ${displayClock(active.updated_at)}`
        : state.predictionStates.get(game.id)?.status === "locked_unavailable"
          ? "比赛已开始，未保存赛前判断"
          : game.roster_notice
          ? "近期数据包含上赛季比赛"
          : game.registered_game ? "真实赛程" : "官方赛程",
    ),
  );
  inner.append(top, matchup, prediction, insights);
  card.append(inner);
  return card;
}

function renderGames() {
  const grid = byId("games-grid");
  grid.replaceChildren();
  const games = state.data.games
    .filter((game) => gameInHomeCollection(game, state.selectedDate))
    .sort((a, b) => {
      const aFavorite =
        state.favoriteTeamId &&
        [a.home.id, a.away.id].includes(state.favoriteTeamId);
      const bFavorite =
        state.favoriteTeamId &&
        [b.home.id, b.away.id].includes(state.favoriteTeamId);
      return (
        Number(bFavorite) - Number(aFavorite) ||
        new Date(a.tipoff) - new Date(b.tipoff)
      );
    });
  games.forEach((game, index) => grid.append(gameCard(game, index)));
  byId("games-empty").hidden = games.length > 0;
}

function renderHome() {
  if (!state.data) return;
  setupDateFilters();
  renderGames();
  const title = byId("home-games-title");
  const emptyTitle = byId("games-empty").querySelector("strong");
  const emptyCopy = byId("games-empty").querySelector("p");
  if (state.selectedDate === "opening") {
    if (title) title.textContent = "揭幕战首日";
    byId("coverage-note").textContent = "2026年10月21日 · 北京时间";
    byId("product-notice").textContent = "仅展示揭幕战第一天三场比赛；后续比赛日暂不展示。";
    emptyTitle.textContent = "揭幕战资料暂不可用";
    if (emptyCopy) emptyCopy.textContent = "稍后刷新查看已发布内容，不会现场生成分析。";
    return;
  }
  if (state.selectedDate === "published") {
    if (title) title.textContent = "已发布比赛";
    byId("coverage-note").textContent = "已发布的赛前分析与赛后复盘 · 北京时间";
    byId("product-notice").textContent = "只读取已保存的正式版本，不现场生成分析。";
    emptyTitle.textContent = "暂无已发布比赛";
    if (emptyCopy) emptyCopy.textContent = "正式发布的分析会保留在这里。";
    return;
  }
  if (title) title.textContent = "接下来的比赛";
  if (emptyCopy) emptyCopy.textContent = "今天和明天的赛程会分开显示；预测仅显示已正式发布的版本。";
  const window = state.data.schedule_window || {};
  const statusCopy = {
    ready: `${window.today_count || 0} 场今天 · ${window.tomorrow_count || 0} 场明天 · 北京时间`,
    empty: "赛程已同步 · 今天与明天暂无比赛 · 北京时间",
    stale: "赛程同步未完成或暂时失败，先显示本地已有赛程 · 北京时间",
    unavailable: "赛程暂不可用，等待本地同步 · 北京时间",
  };
  byId("coverage-note").textContent = statusCopy[window.status] || "赛程状态暂不可用 · 北京时间";
  byId("product-notice").textContent = "比分与赛程独立更新；预测只显示已正式发布的第25秒判断。";
  if (window.status === "empty") emptyTitle.textContent = "今天与明天暂无比赛";
  else if (window.status === "stale") emptyTitle.textContent = "暂时无法确认今天或明天的赛程";
  else if (window.status === "unavailable") emptyTitle.textContent = "赛程尚未同步";
  else emptyTitle.textContent = "这个比赛日暂无比赛";
}

function stageTeam(team) {
  const node = document.createDocumentFragment();
  node.append(
    logo(team, "stage"),
    text("strong", team.name_zh),
    text("small", team.abbreviation),
  );
  return node;
}

function marginLabel(game) {
  const prediction = activePrediction(game);
  if (!prediction) return "赛前分差暂不可用";
  const margin = Number(prediction.expected_home_margin);
  const team = teamById(game, prediction.winner_team_id);
  const value = Math.max(0.5, Math.round(Math.abs(margin) * 2) / 2);
  return `预计${team.name_zh}赢 ${number(value)} 分`;
}

function compactWinnerMarginLabel(game) {
  const prediction = activePrediction(game);
  if (!prediction) return "暂不可用";
  const margin = Number(prediction.expected_home_margin);
  const team = teamById(game, prediction.winner_team_id);
  const value = Math.max(0.5, Math.round(Math.abs(margin) * 2) / 2);
  return `${team.name_zh} +${number(value)}`;
}

function displayedStoryTitle(payload) {
  return displayedJudgment(payload)?.pregame_story?.title;
}

function displayedJudgment(payload) {
  const judgment = payload?.judgment || {};
  const overlay = state.editorialOverlays.get(payload?.game_id);
  if (!overlay || overlay.basePredictionVersionId !== payload?.version_id) return judgment;
  const editorial = overlay.editorial || {};
  return {
    ...judgment,
    pregame_story: editorial.pregame_story || judgment.pregame_story,
    analysis_article: editorial.analysis_article ?? judgment.analysis_article,
    watch_themes: editorial.watch_themes ?? judgment.watch_themes,
    player_analysis: editorial.player_analysis ?? judgment.player_analysis,
    central_thesis: editorial.central_thesis ?? judgment.central_thesis,
    core_question: editorial.core_question ?? judgment.core_question,
    game_script: editorial.game_script ?? judgment.game_script,
    model_risk: editorial.model_risk ?? judgment.model_risk,
  };
}

function sourceCalendarDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[1]}/${match[2]}/${match[3]}` : "日期待核";
}

function matchupScore(value) {
  if (value === null || value === undefined || value === "") return null;
  const score = Number(value);
  // A full NBA game cannot finish at zero points; legacy imports that used
  // zero as a missing-value default are therefore rendered as unavailable.
  return Number.isFinite(score) && score > 0 ? score : null;
}

function matchupCompetition(meeting) {
  const explicit = String(meeting?.game_type || meeting?.season_type || meeting?.competition || "").toLowerCase();
  if (/play.?in|附加赛/.test(explicit)) return "附加赛";
  if (/playoff|季后赛/.test(explicit) || meeting?.is_playoffs === true || meeting?.is_playoffs === 1) return "季后赛";
  if (/regular[_ -]?season|常规赛/.test(explicit)) return "常规赛";
  if (/preseason|季前赛/.test(explicit)) return "季前赛";
  if (/all.?star|全明星/.test(explicit)) return "全明星赛";

  // NBA's official 10-digit game ID encodes competition type in its prefix.
  const officialGameId = String(meeting?.game_id || "");
  if (/^001\d{7}$/.test(officialGameId)) return "季前赛";
  if (/^002\d{7}$/.test(officialGameId)) return "常规赛";
  if (/^003\d{7}$/.test(officialGameId)) return "全明星赛";
  if (/^004\d{7}$/.test(officialGameId)) return "季后赛";
  if (/^005\d{7}$/.test(officialGameId)) return "附加赛";

  const explicitCompetition = String(meeting?.competition || "").trim();
  return explicitCompetition && !/unknown|待核|未知/i.test(explicitCompetition)
    ? explicitCompetition
    : null;
}

function frozenMeetings(game) {
  const homeId = Number(game.home.id);
  const awayId = Number(game.away.id);
  return [...(game.matchup_history?.meetings || [])]
    .filter((meeting) => {
      const historicalHome = Number(meeting.home_team_id);
      const historicalAway = Number(meeting.away_team_id);
      return historicalHome !== historicalAway &&
        ((historicalHome === homeId && historicalAway === awayId) ||
         (historicalHome === awayId && historicalAway === homeId));
    })
    .sort((left, right) => String(left.played_at || "").localeCompare(String(right.played_at || "")))
    .map((meeting) => {
      const homeScore = matchupScore(meeting.home_score);
      const awayScore = matchupScore(meeting.away_score);
      const currentHomeScore = Number(meeting.home_team_id) === homeId ? homeScore : awayScore;
      const currentAwayScore = Number(meeting.away_team_id) === awayId ? awayScore : homeScore;
      const winnerTeamId = homeScore === null || awayScore === null
        ? null
        : Number(meeting.home_team_id) === homeId
          ? (homeScore > awayScore ? homeId : awayId)
          : (homeScore > awayScore ? awayId : homeId);
      return {
        ...meeting,
        home_team_id: Number(meeting.home_team_id),
        away_team_id: Number(meeting.away_team_id),
        home_score: homeScore,
        away_score: awayScore,
        current_home_score: currentHomeScore,
        current_away_score: currentAwayScore,
        winner_team_id: winnerTeamId,
        score_complete: homeScore !== null && awayScore !== null,
        competition_label: matchupCompetition(meeting),
      };
    });
}

function renderMatchupWindow(game, rows, requestedGames) {
  const complete = rows.filter((meeting) => meeting.score_complete);
  const window = document.createElement("article");
  window.className = "matchup-window";
  window.dataset.sampleGames = String(rows.length);
  window.append(text("h3", `近${requestedGames}场`));
  if (complete.length < requestedGames) {
    window.append(text("small", `现有${complete.length}场完整比分`, "matchup-sample-note"));
  }
  const teams = document.createElement("div");
  teams.className = "matchup-window-teams";
  for (const team of [game.away, game.home]) {
    const wins = complete.filter((meeting) => meeting.winner_team_id === Number(team.id)).length;
    const losses = complete.length - wins;
    const points = complete.reduce((sum, meeting) => sum +
      (Number(team.id) === Number(game.home.id) ? meeting.current_home_score : meeting.current_away_score), 0);
    const row = document.createElement("div");
    row.className = "matchup-window-team";
    row.append(
      text("strong", team.name_zh),
      text("span", complete.length ? `${wins}胜${losses}负 · 场均得分 ${number(points / complete.length)}` : "胜负与场均得分暂不可用"),
    );
    teams.append(row);
  }
  window.append(teams);
  return window;
}

function renderMatchupHistory(game) {
  const section = byId("matchup-history-section");
  const container = byId("matchup-history");
  const meetings = frozenMeetings(game);
  container.replaceChildren();
  section.hidden = false;
  section.classList.toggle("is-empty", meetings.length === 0);
  if (!meetings.length) {
    container.append(text("p", "暂无可用的历史交锋记录。", "matchup-history-empty"));
    return;
  }

  const summaries = document.createElement("div");
  summaries.className = "matchup-history-summaries";
  [5, 10].forEach((requestedGames) => {
    summaries.append(renderMatchupWindow(game, meetings.slice(-requestedGames), requestedGames));
  });
  container.append(summaries);

  const table = document.createElement("table");
  table.className = "matchup-history-table";
  const hasCompetition = meetings.some((meeting) => Boolean(meeting.competition_label));
  table.classList.toggle("has-competition", hasCompetition);
  const header = document.createElement("thead");
  const headerRow = document.createElement("tr");
  const columns = ["日期", ...(hasCompetition ? ["赛事"] : []), "客队", "比分", "主队"];
  columns.forEach((label) => {
    const cell = text("th", label);
    cell.scope = "col";
    headerRow.append(cell);
  });
  header.append(headerRow);
  const body = document.createElement("tbody");
  meetings.slice().reverse().forEach((meeting) => {
    const row = document.createElement("tr");
    row.append(text("td", sourceCalendarDate(meeting.played_at)));
    if (hasCompetition) {
      row.append(text("td", meeting.competition_label || "", "matchup-competition"));
    }
    const awayTeamCell = text("td", teamById(game, meeting.away_team_id).name_zh, "matchup-team");
    if (meeting.winner_team_id === meeting.away_team_id) awayTeamCell.classList.add("matchup-winner-team");
    row.append(awayTeamCell);

    const scoreCell = text("td", "", "matchup-score");
    if (meeting.score_complete) {
      const awayScore = text("span", String(meeting.away_score));
      const homeScore = text("span", String(meeting.home_score));
      if (meeting.winner_team_id === meeting.away_team_id) awayScore.classList.add("matchup-winner-score");
      if (meeting.winner_team_id === meeting.home_team_id) homeScore.classList.add("matchup-winner-score");
      scoreCell.append(awayScore, document.createTextNode("–"), homeScore);
    } else {
      scoreCell.textContent = "比分暂缺";
    }
    row.append(scoreCell);

    const homeTeamCell = text("td", teamById(game, meeting.home_team_id).name_zh, "matchup-team");
    if (meeting.winner_team_id === meeting.home_team_id) homeTeamCell.classList.add("matchup-winner-team");
    row.append(homeTeamCell);
    body.append(row);
  });
  table.append(header, body);
  const wrapper = document.createElement("div");
  wrapper.className = "matchup-history-table-wrap";
  wrapper.append(table);
  container.append(wrapper);
}

function renderFactors(game, themes = null, status = "idle") {
  const section = byId("factors-section");
  const container = byId("key-factors");
  container.replaceChildren();
  const items = themes || [];
  section.hidden = items.length === 0;
  if (!items.length) return;
  container.style.setProperty(
    "--factor-count",
    items.length,
  );
  items.forEach((factor) => {
    const card = document.createElement("article");
    card.className = "factor-card";
    card.append(
      text("h3", analystCopy(factor.title)),
      text("p", analystCopy(factor.statement)),
    );
    container.append(card);
  });
}

function renderPregameStory(story = null, status = "idle") {
  const note = byId("baseline-note");
  const game = state.activeGame;
  const title = byId("detail-title");
  const dek = byId("cover-dek");
  if (dek && game) {
    title.textContent = story?.title ? analystCopy(story.title) : `${game.away.name_zh}对阵${game.home.name_zh}`;
    dek.textContent = story?.statement ? analystCopy(story.statement) : "本场尚无已发布的完整分析，先查看已确认的比赛资料。";
    byId("cover-eyebrow").textContent = hasVisibleScore(game) ? "赛前焦点 · 回看当时的判断" : story ? "本场焦点" : "比赛资料";
    if (typeof updateCoverReading === "function") updateCoverReading();
    note.hidden = true;
    note.replaceChildren();
    return;
  }
  if (story) {
    note.hidden = false;
    note.replaceChildren(
      text("strong", analystCopy(story.title)),
      text("p", analystCopy(story.statement)),
    );
    return;
  }
  if (status === "unavailable") {
    note.hidden = false;
    note.replaceChildren(
      text("strong", "第25秒分析暂不可用"),
      text("p", "当前预测和结构化赛前事实仍可正常查看。"),
    );
    return;
  }
  note.hidden = true;
  note.replaceChildren();
}

function renderProductAnalysis(payload) {
  if (!state.activeGame || !payload?.judgment) return;
  const judgment = displayedJudgment(payload);
  const story = judgment.pregame_story
    ? { ...judgment.pregame_story, title: displayedStoryTitle(payload) }
    : null;
  renderPregameStory(story, "success");
  renderFactors(state.activeGame, judgment.watch_themes || [], "success");
  renderKeyPlayers(state.activeGame, judgment.player_analysis || [], "success");
}

function renderProductAnalysisState(game, status = "idle") {
  renderPregameStory(null, status);
  renderFactors(game, null, status);
  renderKeyPlayers(game, null, status);
}

function contextStat(label, value) {
  const row = document.createElement("div");
  row.className = "context-stat";
  row.append(text("span", label), text("strong", value));
  return row;
}

function available(value, formatter = (item) => String(item)) {
  return value === null || value === undefined ? "暂不可用" : formatter(value);
}

function spreadLabel(game, homeSpread) {
  if (homeSpread === null || homeSpread === undefined) return "—";
  const spread = Number(homeSpread);
  if (!Number.isFinite(spread)) return "—";
  if (Math.abs(spread) < 0.05) return "平手";
  const favored = spread < 0 ? game.home : game.away;
  return `${favored.name_zh} -${number(Math.abs(spread))}`;
}

function renderMarket(game) {
  const version = state.currentAnalyses.get(game.id);
  const market = game.background?.market;
  const card = byId("market-context");
  if (card) {
    card.replaceChildren(text("h3", "市场参考"));
    if (!market) {
      card.append(text("p", "暂无赛前市场数据", "context-missing"));
    } else {
      card.append(
        contextStat("当前让分", spreadLabel(game, market.home_spread)),
        contextStat(
          "当前总分",
          market.total == null ? "—" : number(market.total),
        ),
      );
    }
  }
  const hero = byId("hero-market");
  const marketSource = byId("hero-market-source");
  marketSource.replaceChildren(); marketSource.hidden = true;
  hero.replaceChildren(
    text("span", "市场参考"),
    text(
      "strong",
      market
        ? `让分 ${spreadLabel(game, market.home_spread)} · 总分 ${market.total == null ? "—" : number(market.total)}`
        : "暂无赛前市场数据",
    ),
  );
  if (!market) {
    if (version?.public_market_reference) {
      renderPublicMarketReference(version.public_market_reference);
      return;
    }
    if (state.marketCache.has(game.id)) {
      const reference = state.marketCache.get(game.id);
      if (reference) renderPublicMarketReference(reference);
      const age = Date.now() - (state.marketFetchedAt.get(game.id) || 0);
      if (age > (reference ? 1200000 : 600000)) requestPublicMarketReference(game);
    } else {
      requestPublicMarketReference(game);
    }
  }
  appendMarketJudgment(game);
}

function appendMarketJudgment(game) {
  const host = byId("hero-market-source");
  host.querySelector(".market-judgment")?.remove();
  const selected = state.predictionStates.get(game.id);
  const version = state.currentAnalyses.get(game.id);
  const projection = selected?.display_projection;
  if (!version || projection?.version_id !== version.version_id) return;
  const interpretation = projection.market_interpretation;
  if (!interpretation) return;
  const block = document.createElement("div");
  block.className = "market-judgment";
  const spread = interpretation.spread;
  if (spread?.status === "available" && ["home", "away"].includes(spread.ats_lean) &&
      Number.isFinite(spread.ats_lean_reference?.line)) {
    block.append(contextStat("第25秒让分判断",
      game[spread.ats_lean].name_zh + " " + signedNumber(spread.ats_lean_reference.line)));
  }
  const total = interpretation.total;
  if (total?.status === "available" && ["over", "under", "aligned"].includes(total.total_lean)) {
    block.append(contextStat("第25秒大小分判断", {over:"大分", under:"小分", aligned:"与市场一致"}[total.total_lean]));
  }
  if (block.childElementCount) {
    host.hidden = false;
    host.prepend(block);
  }
}

function renderPublicMarketReference(reference) {
  if (!reference || state.activeGame?.background?.market) return;
  reference = state.currentAnalyses.get(state.activeGame.id)?.public_market_reference || reference;
  const historical = ["historical_backfill", "historical_archive"].includes(reference.reference_kind) || state.activeGame.status === "final";
  const heading = historical ? "赛前市场参考" : reference.market_stage === "early" ? "公开早盘参考" : "公开市场参考";
  const card = byId("market-context");
  if (card) {
    card.replaceChildren(
      text("h3", heading),
      contextStat(
        "让分",
        reference.home_spread == null
          ? "暂未可靠提取"
          : spreadLabel(state.activeGame, reference.home_spread),
      ),
      contextStat(
        "总分",
        reference.total == null ? "暂未可靠提取" : number(reference.total),
      ),
    );
  }
  const hero = byId("hero-market");
  const parts = [];
  if (reference.home_spread != null) {
    parts.push(`让分 ${spreadLabel(state.activeGame, reference.home_spread)}`);
  }
  if (reference.total != null) parts.push(`总分 ${number(reference.total)}`);
  hero.replaceChildren(
    text("span", heading),
    text("strong", parts.join(" · ") || "来源已找到，具体数字暂未可靠提取"),
  );
  const sourceHost = byId("hero-market-source");
  sourceHost.replaceChildren(); sourceHost.hidden = false;
  if (historical && reference.used_by_published_prediction === false) {
    sourceHost.append(text("div", "赛后整理 · 未用于原预测"));
  }
  const details = document.createElement("details"); details.append(text("summary", "来源与时间"));
  const link = text("a", reference.source_title || "查看盘口来源");
  link.href = reference.source_url; link.target = "_blank"; link.rel = "noopener noreferrer"; details.append(link);
  const showTime = (value) => !value ? "未知" : value.length === 10 ? value : new Date(value).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"});
  details.append(text("p", `抓取于 ${showTime(reference.captured_at)}${reference.published_at ? ` · 原文发布 ${showTime(reference.published_at)}` : ""}`));
  if (reference.provider_archive_label) details.append(text("p", "供应商历史盘口字段，具体报价时点未核验"));
  const prices = [];
  for (const side of ["away", "home"]) {
    if (reference[`${side}_moneyline`] != null) prices.push(`${state.activeGame[side].name_zh} ${reference[`${side}_moneyline`] > 0 ? "+" : ""}${reference[`${side}_moneyline`]}`);
  }
  if (prices.length) details.append(text("p", `Moneyline：${prices.join(" · ")}`));
  sourceHost.append(details);
  appendMarketJudgment(state.activeGame);
}

async function requestPublicMarketReference(game) {
  if (game.background?.market || state.marketPending.has(game.id)) return;
  const card = byId("market-context");
  if (card && state.activeGame?.id === game.id) {
    card.replaceChildren(
      text("h3", "市场参考"),
      text("p", "正在获取市场参考…", "context-missing"),
    );
  }
  const request = (async () => {
      for (let attempt=0;attempt<32;attempt++) {
        const response = await fetch(`/api/market-reference?game_id=${encodeURIComponent(game.id)}`, { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || "市场参考暂不可用");
        const reference = payload.public_market_reference || null;
        state.marketFetchedAt.set(game.id, Date.now());
        if (reference) {
          state.marketCache.set(game.id, reference);
          if (state.activeGame?.id === game.id) renderPublicMarketReference(reference);
        }
        if (payload.status !== "fetching" && !payload.refreshing) {
          state.marketCache.set(game.id, reference);
          if (!reference && state.activeGame?.id === game.id) renderMarket(game);
          return;
        }
        await new Promise(resolve => setTimeout(resolve,1500));
      }
      if (!state.marketCache.has(game.id)) state.marketCache.set(game.id,null);
      if (state.activeGame?.id === game.id) renderMarket(game);
    })()
    .catch(() => {
      state.marketFetchedAt.set(game.id, Date.now());
      state.marketCache.set(game.id, null);
      if (state.activeGame?.id === game.id) {
        renderMarket(game);
      }
    })
    .finally(() => state.marketPending.delete(game.id));
  state.marketPending.set(game.id, request);
}

function lineupNames(value) {
  const players = Array.isArray(value) ? value : Array.isArray(value?.players) ? value.players : [];
  return players.map((player) => typeof player === "string"
    ? player.trim()
    : String(player?.name || player?.player_name || "").trim());
}

function normalizedLineup(lineup) {
  const basis = lineup?.basis;
  if (!["confirmed", "source_expected", "agent_expected", "unavailable"].includes(basis)) return null;
  const players = lineupNames(lineup);
  const evidence_ids = Array.isArray(lineup?.evidence_ids) ? lineup.evidence_ids : [];
  if (basis === "unavailable") return players.length === 0 ? { basis, players: [], evidence_ids } : null;
  if (players.length !== 5 || players.some((name) => !name) || new Set(players).size !== 5) return null;
  return { basis, players, evidence_ids, candidates: lineupNames(lineup.candidates || []) };
}

function versionPregamePlan(version) {
  const plans = (Array.isArray(version?.editorial_material) ? version.editorial_material : [])
    .map((item) => item?.pregame_plan)
    .filter((plan) => plan && typeof plan === "object");
  if (!plans.length) return { status: "missing", plan: null };
  const signatures = plans.map((plan) => JSON.stringify((plan.lineups || [])
    .map((row) => ({ side: row.side, basis: row.basis, players: lineupNames(row).sort(), candidates: lineupNames(row.candidates || []) }))
    .sort((left, right) => String(left.side).localeCompare(String(right.side)))));
  if (new Set(signatures).size > 1) return { status: "conflict", plan: null };
  const plan = plans[0];
  const sides = new Map();
  for (const row of plan.lineups || []) {
    if (!["home", "away"].includes(row.side) || sides.has(row.side)) return { status: "conflict", plan: null };
    const normalized = normalizedLineup(row);
    if (!normalized) return { status: "invalid", plan: null };
    sides.set(row.side, normalized);
  }
  if (sides.size !== 2 || !sides.has("home") || !sides.has("away")) return { status: "invalid", plan: null };
  return { status: "available", plan, sides };
}

function legacySnapshotLineup(context) {
  const candidates = [];
  const confirmed = context?.confirmed_starters;
  if (lineupNames(confirmed).length) {
    candidates.push({ basis: "legacy_confirmed", players: lineupNames(confirmed),
      evidence_ids: confirmed?.evidence_ids || context?.confirmed_starters_evidence_ids || [], source: "snapshot" });
  }
  const projected = lineupNames(context?.projected_starters).length
    ? context.projected_starters
    : context?.current_rotation?.projected_starters;
  if (lineupNames(projected).length) {
    const basisValue = Array.isArray(projected) ? context?.projected_starters_basis : projected.basis;
    const basis = ["source_expected", "agent_expected"].includes(basisValue) ? basisValue : "legacy_projected";
    const evidence_ids = Array.isArray(projected?.evidence_ids) ? projected.evidence_ids :
      Array.isArray(context?.projected_starters_evidence_ids) ? context.projected_starters_evidence_ids : [];
    candidates.push({ basis, players: lineupNames(projected), evidence_ids, source: "snapshot" });
  }
  const unique = new Map(candidates.map((candidate) => [
    JSON.stringify({ basis: candidate.basis, players: candidate.players.slice().sort() }), candidate,
  ]));
  if (unique.size > 1) return { status: "conflict", lineup: null };
  const lineup = [...unique.values()][0];
  if (!lineup) return { status: "missing", lineup: null };
  if (lineup.players.length !== 5 || lineup.players.some((name) => !name) || new Set(lineup.players).size !== 5) {
    return { status: "invalid", lineup: null };
  }
  return { status: "available", lineup };
}

function lineupEvidence(version, lineup) {
  const evidence = version?.analyst_context?.objective_evidence || [];
  const byId = new Map(evidence.filter((item) => item?.id).map((item) => [item.id, item]));
  return (lineup.evidence_ids || []).map((id) => byId.get(id)).filter(Boolean);
}

function lineupIdentityVerified(game, version, side, name, refs) {
  const teamContext = version?.input_snapshot?.roster_context?.teams?.[side] || {};
  const roster = teamContext.official_team_roster || teamContext.current_team_roster;
  const teamId = Number(game[side].id);
  if (roster?.status === "available" &&
      Number(teamContext.team_id ?? roster.team_id) === teamId &&
      (roster.players || []).some((player) =>
        String(player.name || player.player_name || "").trim() === name &&
        (player.team_id == null || Number(player.team_id) === teamId))) return true;
  return refs.some((item) => {
    if (item.section !== "web_context") return false;
    const value = item.value || {};
    if (value.eligible_for_fact !== true || Number(value.team_id) !== teamId) return false;
    const claim = String(value.claim_type || "");
    if (["current_roster", "projected_starters", "confirmed_starter"].includes(claim)) {
      return String(value.quote || "").includes(name);
    }
    if (value.temporal_scope !== "current_season_background" ||
        !["primary", "professional"].includes(value.source_role) ||
        /traded away|departed|left the|former|上赛季|离队/i.test(String(value.quote || ""))) return false;
    return String(value.quote || "").includes(name) &&
      (claim === "personnel_change" || claim === "role_background");
  });
}

function officialStatusForPlayer(report, teamId, name, playerId = null) {
  return (report?.records || []).find((record) => {
    if (Number(record.teamId ?? record.team_id) !== Number(teamId)) return false;
    if (playerId != null && record.player_id != null) return String(record.player_id) === String(playerId);
    return playerNameAliases(record.player?.name || record.player?.player_name || record.player_name)
      .some((candidate) => playerNameAliases(name).some((expected) => candidate.toLowerCase() === expected.toLowerCase()));
  }) || null;
}

function lineupPlayerConfirmedOut(game, version, side, name, refs = [], officialReport = null) {
  const report = officialReport || officialInjuryReportProjection(game, version);
  return officialStatusForPlayer(report, game[side].id, name)?.status === "out";
}

function validPlanLineup(game, version, side, lineup, officialReport = null) {
  if (!lineup || lineup.basis === "unavailable") return true;
  const refs = lineupEvidence(version, lineup);
  const sourceKind = lineup.basis === "confirmed" ? "confirmed_starter"
    : lineup.basis === "source_expected" ? "projected_starters" : null;
  return lineup.players.every((name) => {
    if (!lineupIdentityVerified(game, version, side, name, refs) ||
        lineupPlayerConfirmedOut(game, version, side, name, refs, officialReport)) return false;
    if (!sourceKind) return true;
    return refs.some((item) => item.section === "web_context" &&
      item.value?.eligible_for_fact === true && Number(item.value?.team_id) === Number(game[side].id) &&
      item.value?.claim_type === sourceKind && String(item.value?.quote || "").includes(name));
  });
}

function confirmedLineupBasis(basis) {
  return ["confirmed", "legacy_confirmed"].includes(basis);
}

function snapshotLineupForDisplay(context, game, version, side, officialReport) {
  for (const [field,basis,refField] of [["confirmed_starters","confirmed","confirmed_starters_evidence_ids"],
      ["projected_starters","source_expected","projected_starters_evidence_ids"]]) {
    const value=context?.[field];
    const lineup={basis,players:lineupNames(value),evidence_ids:value?.evidence_ids || context?.[refField] || []};
    if (lineup.players.length===5 && new Set(lineup.players).size===5 &&
        validPlanLineup(game,version,side,lineup,officialReport)) return {status:"available",lineup};
  }
  return legacySnapshotLineup(context);
}

function lineupReserveCandidates(game, version, side, lineup) {
  const plan = versionPregamePlan(version).plan || {};
  const rotation = version?.input_snapshot?.roster_context?.teams?.[side]?.current_rotation;
  const usage = (plan.player_usage || []).filter(row => row.side === side && row.rationale &&
    row.minutes_max > 0).map(row => ({name:row.player_name,evidence_ids:row.evidence_ids || []}));
  const rotations = rotation?.status === "available" ? (rotation.lineup_candidates || []) : [];
  return [
    ...(lineup.candidates || []).map(name => ({name,evidence_ids:lineup.evidence_ids || []})),
    ...usage,
    ...rotations.filter(row => row.rationale || row.role).map(row => ({
      name:row.player_name || row.name,evidence_ids:row.evidence_ids || rotation.evidence_ids || []})),
  ].filter(row => row.name && lineupIdentityVerified(game,version,side,row.name,lineupEvidence(version,row)));
}

function renderLineups(game, version = null, selectedOfficialReport = null) {
  const card = byId("lineup-context");
  card.replaceChildren();
  const title = text("h3", "预计首发");
  card.append(title);
  let lineups = null;
  const planResult = versionPregamePlan(version);
  if (["conflict", "invalid"].includes(planResult.status)) {
    card.append(text("p", planResult.status === "conflict"
      ? "同一版本的首发资料存在冲突，暂不展示。"
      : "首发资料未通过完整性核对，暂不展示。", "context-missing"));
    return;
  }
  if (planResult.status === "available") {
    lineups = planResult.sides;
    const snapshot = version?.input_snapshot?.roster_context?.teams || {};
    const legacy = Object.fromEntries(["home", "away"].map((side) =>
      [side, snapshotLineupForDisplay(snapshot[side],game,version,side,selectedOfficialReport)]));
    for (const side of ["home", "away"]) {
      const legacyLineup = legacy[side];
      const planned = lineups.get(side);
      if (legacyLineup.status === "conflict") {
        card.append(text("p", "同一版本的首发资料存在冲突，暂不展示。", "context-missing"));
        return;
      }
      if (legacyLineup.status === "available" && !confirmedLineupBasis(planned.basis)) {
        const sourceBasis = confirmedLineupBasis(legacyLineup.lineup.basis) ? "confirmed"
          : ["source_expected","agent_expected"].includes(legacyLineup.lineup.basis) ? legacyLineup.lineup.basis : null;
        const sourced = sourceBasis ? {...legacyLineup.lineup,basis:sourceBasis} : null;
        const priority={confirmed:3,source_expected:2,agent_expected:1,unavailable:0};
        if (sourced && priority[sourceBasis]>priority[planned.basis] &&
            validPlanLineup(game,version,side,sourced,selectedOfficialReport)) {
          lineups.set(side,{...sourced,candidates:planned.candidates || []});
          continue;
        }
      }
    if (legacyLineup.status === "available" &&
          (legacyLineup.lineup.players.slice().sort().join("\u0000") !== planned.players.slice().sort().join("\u0000") ||
           (planned.basis !== "unavailable" && legacyLineup.lineup.basis !== "legacy_projected" &&
            (legacyLineup.lineup.basis === "legacy_confirmed" ? "confirmed" : legacyLineup.lineup.basis) !== planned.basis))) {
        card.append(text("p", "同一版本的首发资料存在冲突，暂不展示。", "context-missing"));
        return;
      }
    }
  } else if (version?.version_id) {
    const teams = version.input_snapshot?.roster_context?.teams || {};
    const legacy = Object.fromEntries(["home", "away"].map((side) =>
      [side, snapshotLineupForDisplay(teams[side],game,version,side,selectedOfficialReport)]));
    if (Object.values(legacy).some((item) => item.status === "conflict")) {
      card.append(text("p", "同一版本的首发资料存在冲突，暂不展示。", "context-missing"));
      return;
    }
    if (Object.values(legacy).every((item) => item.status === "missing")) {
      card.append(text("p", "该版本未保存首发资料。", "context-missing"));
      return;
    }
    lineups = new Map(Object.entries(legacy).map(([side, item]) => [side,
      item.status === "available" ? item.lineup :
        { basis: item.status === "missing" ? "missing" : "invalid", players: [] }]));
  }

  if (!version?.version_id || !lineups) {
    card.append(text("p", "尚无已发布版本的首发资料。", "context-missing"));
    return;
  }
  const officialReport = selectedOfficialReport || officialInjuryReportProjection(game, version);
  lineups = new Map([...lineups].map(([side, lineup]) => {
    const removedOut = (lineup.players || []).filter((name) =>
      lineupPlayerConfirmedOut(game, version, side, name, [], officialReport));
    const players = (lineup.players || []).filter((name) =>
      !lineupPlayerConfirmedOut(game, version, side, name, [], officialReport));
    const replacements = [];
    if (removedOut.length) {
      for (const candidate of lineupReserveCandidates(game,version,side,lineup)) {
        if (players.includes(candidate.name) || lineupPlayerConfirmedOut(game,version,side,candidate.name,[],officialReport)) continue;
        players.push(candidate.name);replacements.push(candidate.name);
        if (players.length===5) break;
      }
    }
    return [side, {
      ...lineup,
      basis: removedOut.length ? "agent_expected" : lineup.basis,
      players,
      replacements,
      officialOutRemoved: removedOut,
    }];
  }));
  const confirmedSides = ["away", "home"].filter((side) => {
    const lineup = lineups.get(side);
    return confirmedLineupBasis(lineup?.basis) && lineup.players?.length === 5 &&
      validPlanLineup(game, version, side, lineup, officialReport);
  });
  const hasConfirmedLineup = confirmedSides.length > 0;
  title.textContent = hasConfirmedLineup ? "首发阵容" : "预计首发";

  for (const side of ["away", "home"]) {
    const lineup = lineups.get(side);
    if (lineup.basis === "invalid") {
      card.append(text("p", `${game[side].name_zh}首发资料不完整，暂不展示。`, "context-missing"));
      continue;
    }
    if (lineup.basis !== "unavailable" && lineup.basis !== "missing" && lineup.players.length > 0 &&
        !validPlanLineup(game, version, side, lineup, officialReport)) {
      card.append(text("p", "首发资料未通过身份或状态核对，暂不展示。", "context-missing"));
      continue;
    }
    const block = document.createElement("div");
    block.className = "lineup-team";
    if (lineup.basis === "missing") {
      block.append(text("strong", `${game[side].name_zh} · 首发资料待补`),
        text("p", "该版本未保存首发资料。", "context-missing"));
      card.append(block);
      continue;
    }
    if (lineup.basis === "unavailable") {
      block.append(text("strong", game[side].name_zh),
        text("p", "暂无可用的预计首发信息。", "context-missing"));
      card.append(block);
      continue;
    }
    const isConfirmed = confirmedLineupBasis(lineup.basis) && lineup.players.length === 5 &&
      !(lineup.officialOutRemoved || []).length;
    const needsSideQualifier = hasConfirmedLineup && !isConfirmed &&
      (["confirmed", "legacy_confirmed", "source_expected", "agent_expected", "legacy_projected"].includes(lineup.basis));
    block.append(text("strong", needsSideQualifier
      ? `${game[side].name_zh} · 预计首发`
      : game[side].name_zh));
    if (!isConfirmed && ["source_expected","agent_expected"].includes(lineup.basis)) {
      block.append(text("small", lineup.basis === "source_expected" ? "来源预计" : "第25秒预计", "lineup-basis"));
    }
    if (lineup.players.length) {
      const names = document.createElement("p");
      names.className = "lineup-player-list";
      lineup.players.forEach((name, index) => {
        if (index) names.append(document.createTextNode("、"));
        const identity = frozenRosterPlayer(game, version, game[side].id, name);
        const player = identity || { name };
        const item = document.createElement("span");
        item.className = "lineup-player";
        item.append(text("span", playerName(player)));
        const availability = officialStatusForPlayer(officialReport, game[side].id, name,
          player.player_id || player.id);
        if (availability?.status && !["available", "active"].includes(availability.status)) {
          item.append(text("small", availability.label, `lineup-player-status status-${availability.status}`));
        }
        names.append(item);
      });
      block.append(names);
    }
    if ((lineup.officialOutRemoved || []).length) {
      block.append(text("p", lineup.players.length===5 ? "确认缺阵者已移除，按本版候选补位。" : "确认缺阵者已移除，预计首发信息不足。", "context-missing"));
    } else if (!lineup.players.length) {
      block.append(text("p", "暂无可用的预计首发信息。", "context-missing"));
    }
    card.append(block);
  }
}

function frozenRoster(game, version, side) {
  const team = version?.input_snapshot?.roster_context?.teams?.[side];
  const roster = team?.official_team_roster || team?.current_team_roster;
  return roster?.status === "available" && Number(team.team_id ?? roster.team_id) === Number(game[side].id)
    ? (roster.players || []) : [];
}

function renderKeyPlayers(game, claims = null, status = "idle", selectedOfficialReport = null) {
  const container = byId("key-players");
  const section = container.closest(".players-section");
  container.classList.remove("is-single-side");
  container.replaceChildren();
  const version = state.currentAnalyses.get(game.id);
  const entries = claims?.length ? claims : (displayedJudgment(version)?.player_analysis || []);
  const officialReport = selectedOfficialReport || officialInjuryReportProjection(game, version);
  if (!entries.length) {
    section?.classList.add("is-empty");
    container.append(text(
      "p",
      status === "success" ? "关键球员解读暂缺" : "尚无已发布分析",
      "factor-empty",
    ));
    return;
  }
  const selected = new Map();
  for (const side of ["away", "home"]) {
    const roster = version ? frozenRoster(game, version, side) : (rosterFor(game, side)?.players || []);
    const players = [];
    const seen = new Set();
    for (const claim of entries) {
      const name = String(claim.player_name || "").trim();
      const player = roster.find(candidate =>
        String(candidate.name || candidate.player_name || "").trim() === name &&
        (candidate.team_id == null || Number(candidate.team_id) === Number(game[side].id)));
      if (!player) continue;
      const key = availabilityPlayerKey(game[side].id, player);
      if (seen.has(key)) continue;
      seen.add(key);
      const availability = officialStatusForPlayer(officialReport, game[side].id, name,
        player.player_id || player.id);
      if (availability?.status === "out") continue;
      players.push({ player, claim, availability });
      if (players.length === 3) break;
    }
    selected.set(side, players);
  }
  container.dataset.qualityWarnings = JSON.stringify([...selected].filter(([,players])=>players.length<3)
    .map(([side,players])=>({code:"key_player_candidates_insufficient",version_id:version?.version_id,side,available:players.length})));
  if (![...selected.values()].some((players) => players.length)) {
    section?.classList.add("is-empty");
    container.append(text("p", "关键球员解读暂缺", "factor-empty"));
    return;
  }
  section?.classList.remove("is-empty");
  container.classList.toggle("is-single-side", [...selected.values()].filter(players => players.length).length === 1);
  for (const side of ["away", "home"]) {
    if (!(selected.get(side) || []).length) continue;
    const group = document.createElement("article");
    group.className = `player-side player-side-${side}`;
    group.style.setProperty("--player-accent", game[side].accent);
    const header = document.createElement("header");
    header.append(
      logo(game[side]),
      text("span", side === "away" ? "客队变量" : "主队变量"),
      text("h3", game[side].name_zh),
    );
    group.append(header);
    const list = document.createElement("div");
    list.className = "player-side-list";
    (selected.get(side) || []).forEach(({ player, claim, availability }) => {
      const entry = document.createElement("section");
      entry.className = "player-entry";
      const heading = text("h4", playerName(player));
      if (availability && !["available", "active"].includes(availability.status)) {
        heading.append(text("span", availability.label, `player-availability-status status-${availability.status}`));
      }
      const statement = analystCopy(claim.statement);
      const conditional = ["questionable", "doubtful", "game_time_decision", "day_to_day"].includes(availability?.status) &&
        !/若|如果/.test(statement);
      const conditionalMinutes = /\d+(?:\.\d+)?\s*(?:(?:到|[–—~-])\s*\d+(?:\.\d+)?\s*)?分钟/.test(statement) &&
        !["available","active"].includes(availability?.status) && !/若|如果/.test(statement);
      entry.append(
        heading,
        text("p", conditional || conditionalMinutes ? `若最终出场，${statement}` : statement),
      );
      list.append(entry);
    });
    group.append(list);
    container.append(group);
    if (side === "away" && (selected.get("home") || []).length) {
      const axis = document.createElement("div");
      axis.className = "matchup-axis";
      axis.setAttribute("aria-hidden", "true");
      axis.append(text("strong", "VS"));
      container.append(axis);
    }
  }
}

const TEAM_SEASON_METRICS = [
  { key: "off_rating", label: "进攻效率", preference: "higher", description: "进攻效率数值较高" },
  { key: "def_rating", label: "防守效率", preference: "lower", description: "防守效率数值较低" },
  { key: "net_rating", label: "净效率", preference: "higher", description: "净效率数值较高" },
  { key: "pace", label: "比赛节奏", preference: null },
  { key: "three_pct", label: "三分命中率", preference: "higher", description: "三分命中率数值较高" },
];

function teamSeasonMetricDisplay(metric, value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const rounded = (Math.round(Number(value) * 10) / 10).toFixed(1);
  return metric.key === "three_pct" ? `${rounded}%` : rounded;
}

function teamSeasonCompare(metric, awayValue, homeValue) {
  if (!metric.preference || awayValue == null || homeValue == null ||
      !Number.isFinite(Number(awayValue)) || !Number.isFinite(Number(homeValue)) ||
      teamSeasonMetricDisplay(metric, awayValue) === teamSeasonMetricDisplay(metric, homeValue)) {
    return { away: false, home: false };
  }
  const awayPreferred = metric.preference === "higher"
    ? Number(awayValue) > Number(homeValue) : Number(awayValue) < Number(homeValue);
  return { away: awayPreferred, home: !awayPreferred };
}

function comparisonValue(metric, value, emphasized) {
  const cell = document.createElement("div");
  cell.className = `comparison-value${emphasized ? " is-compared" : ""}`;
  cell.append(text("strong", teamSeasonMetricDisplay(metric, value)));
  return cell;
}

function renderTeamSeasonSummary(game, stats) {
  const summary = document.createElement("p");
  summary.className = "team-season-summary";
  if (stats?.status === "unavailable") {
    summary.textContent = "上赛季球队数据暂不可用。";
    return summary;
  }
  const away = stats?.teams?.away?.metrics || {};
  const home = stats?.teams?.home?.metrics || {};
  const differences = [];
  for (const metric of TEAM_SEASON_METRICS.filter(item => item.preference)) {
    const awayValue = away[metric.key];
    const homeValue = home[metric.key];
    if (awayValue == null || homeValue == null ||
        teamSeasonMetricDisplay(metric, awayValue) === teamSeasonMetricDisplay(metric, homeValue)) continue;
    const winnerSide = teamSeasonCompare(metric, awayValue, homeValue).away ? "away" : "home";
    differences.push(`${game[winnerSide].name_zh}${metric.description}`);
  }
  const paceAway = away.pace;
  const paceHome = home.pace;
  const paceLine = paceAway == null || paceHome == null
    ? "Pace 暂缺。"
    : Math.abs(Number(paceAway) - Number(paceHome)) < 0.05
      ? "两队 Pace 数据接近。"
      : `两队 Pace 相差 ${Math.abs(Number(paceAway) - Number(paceHome)).toFixed(1)} 回合/场，${game[Number(paceAway) > Number(paceHome) ? "away" : "home"].name_zh}的数据更快。`;
  summary.textContent = `${differences.length ? `上赛季，${differences.join("；")}。` : "上赛季这几项核心数据数值接近。"} ${paceLine}`;
  return summary;
}

function renderTeamData(game) {
  const container = byId("team-data");
  const stats = game.previous_season_team_stats;
  const teams = stats?.teams || {};
  const awayData = teams.away?.metrics || {};
  const homeData = teams.home?.metrics || {};
  container.replaceChildren();
  const statsSources = [...new Set(["away", "home"].flatMap(side =>
    (teams[side]?.sources || []).map(source => source.source).filter(Boolean)))];
  const seasonLabel = stats?.season ? `上赛季（${stats.season}）` : "上赛季数据";
  container.append(text("p", seasonLabel + (statsSources.length ? ` · 来源：${statsSources.join("、")}` : ""), "team-season-label"));
  const head = document.createElement("header");
  for (const side of ["away", "home"]) {
    const team = document.createElement("div");
    team.className = `comparison-team comparison-team-${side}`;
    team.style.setProperty("--data-accent", game[side].accent);
    team.append(logo(game[side]), text("strong", game[side].name_zh));
    head.append(team);
  }
  container.append(head);
  const rows = document.createElement("div");
  rows.className = "comparison-rows";
  TEAM_SEASON_METRICS.forEach(metric => {
    const row = document.createElement("div");
    row.className = "comparison-row";
    const signals = teamSeasonCompare(metric, awayData[metric.key], homeData[metric.key]);
    row.append(
      comparisonValue(metric, awayData[metric.key], signals.away),
      text("span", metric.label),
      comparisonValue(metric, homeData[metric.key], signals.home),
    );
    if (typeof metricComparisonGraphic === "function") {
      row.children[1].append(metricComparisonGraphic(metric, awayData[metric.key], homeData[metric.key], game));
    }
    rows.append(row);
  });
  container.append(rows, renderTeamSeasonSummary(game, stats));
}

const PUBLIC_AVAILABILITY_LABELS = {
  out: "确认缺阵",
  inactive: "确认缺阵",
  suspended: "停赛",
  questionable: "出战成疑",
  doubtful: "出战可能性较低",
  probable: "大概率出战",
  game_time_decision: "赛前决定",
  day_to_day: "每日观察 · 本场待确认",
  practice_absence: "缺席训练",
  minutes_restriction: "出场时间受限",
  available: "可出战",
};

function normalizeAvailabilityStatus(value) {
  const key = String(value || "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  const aliases = {
    gtd: "game_time_decision", game_time: "game_time_decision", game_time_call: "game_time_decision",
    ruled_out: "out", confirmed_out: "out", full_go: "available", active: "available",
    healthy: "available", restricted: "minutes_restriction", limited: "minutes_restriction",
    missed: "practice_absence", missed_practice: "practice_absence", did_not_practice: "practice_absence",
  };
  return PUBLIC_AVAILABILITY_LABELS[key] ? key : aliases[key] || null;
}

function playerNameAliases(value) {
  const name = String(value || "").trim();
  return [...new Set([name, name.replace(/\s+(?:III|IV|II|Jr\.?|Sr\.?)$/i, "").trim()])].filter(Boolean);
}

function frozenRosterPlayer(game, version, teamId, name, playerId = null) {
  const side = ["away", "home"].find((item) => Number(game[item].id) === Number(teamId));
  if (!side) return null;
  const roster = frozenRoster(game, version, side);
  if (playerId != null) {
    const byId = roster.find((player) => String(player.player_id || player.id || "") === String(playerId));
    if (byId) return byId;
  }
  return roster.find((player) => playerNameAliases(player.name || player.player_name)
    .some((alias) => playerNameAliases(name).some((candidate) => alias.toLowerCase() === candidate.toLowerCase()))) || null;
}

function injuryReasonZh(reason) {
  const value = String(reason || "").trim();
  if (!value) return "";
  if (/[\u3400-\u9fff]/.test(value) && !/[A-Za-z]{2,}/.test(value)) return value;
  const normalized = value.toLowerCase();
  const injury = /sprain|扭伤/.test(normalized) ? "扭伤" :
    /strain|拉伤/.test(normalized) ? "拉伤" : /soreness|pain|injur|伤/.test(normalized) ? "伤势" : "";
  const part = /knee|patella|meniscus|acl|mcl/.test(normalized) ? "膝部" :
    /ankle/.test(normalized) ? "脚踝" : /hamstring/.test(normalized) ? "腿筋" :
    /calf/.test(normalized) ? "小腿" : /hip|groin/.test(normalized) ? "髋部/腹股沟" :
    /back|spine/.test(normalized) ? "背部" : /shoulder/.test(normalized) ? "肩部" :
    /wrist|hand|finger/.test(normalized) ? "手部" : /elbow/.test(normalized) ? "肘部" :
    /foot|toe/.test(normalized) ? "足部" : "";
  if (part) return `${part}${injury && injury !== "伤势" ? injury : "伤势"}`;
  if (/illness|sick/.test(normalized)) return "生病";
  return injury;
}

function strictReportTimestamp(value) {
  const raw = String(value || "").trim();
  if (!/[T ]\d{2}:\d{2}/.test(raw) || !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)) return null;
  const timestamp = Date.parse(raw);
  return Number.isFinite(timestamp) ? timestamp : null;
}

function availabilityPlayerKey(teamId, player) {
  return `${teamId}:${String(player.player_id || player.id || player.name || player.player_name || "").toLowerCase()}`;
}

function officialReportStatus(record) {
  const raw = String(record?.availability_status || record?.status || record?.injury_status || "").trim();
  if (!raw) return null;
  const status = normalizeAvailabilityStatus(raw);
  const labels = {
    out: "确认缺阵", questionable: "出战成疑", doubtful: "大概率缺阵", probable: "大概率出战",
    game_time_decision: "赛前决定", day_to_day: "每日观察", inactive: "未激活", suspended: "停赛",
  };
  if (status === "available") return { status, label: "已确认正常出战" };
  return { status: status || "other", label: labels[status] || `官方状态 · ${raw}` };
}

function officialReportSourceUrl(record, report) {
  const candidates = [
    record?.source_url, record?.report_url, record?.url, record?.source?.url,
    report?.source_url, report?.report_url, report?.url, report?.source?.url,
  ];
  return candidates.map((value) => String(value || "").trim()).find((url) =>
    /^https:\/\/ak-static\.cms\.nba\.com\/referee\/injury\/Injury-Report_\d{4}-\d{2}-\d{2}_[A-Za-z0-9_-]+\.pdf(?:[?#].*)?$/i.test(url)
  ) || "";
}

function officialReportGameId(record, report) {
  return record?.game_id || record?.nba_game_id || record?.target_game_id ||
    report?.game_id || report?.nba_game_id || report?.target_game_id ||
    record?.game_identity?.game_id || report?.game_identity?.game_id;
}

function officialInjuryReportProjection(game, version) {
  const tipoff = Date.parse(game.tipoff || "");
  if (!Number.isFinite(tipoff)) return { status: "unavailable", records: [], items: [] };
  const roots = [
    game.official_injury_report,
    game.background?.official_injury_report,
    game.background?.availability,
    version?.official_injury_report,
    version?.input_snapshot?.official_injury_report,
    version?.input_snapshot?.background?.official_injury_report,
    version?.input_snapshot?.background?.availability,
  ].filter(Boolean);
  const groups = new Map();
  const addRecord = (report, record) => {
    const sourceUrl = officialReportSourceUrl(record, report);
    const gameId = officialReportGameId(record, report);
    const gameIds = [game.id, game.nba_game_id].filter(Boolean).map(String);
    const rawTime = record?.source_published_at || record?.report_published_at || record?.report_timestamp ||
      record?.published_at || report?.source_published_at || report?.report_published_at ||
      report?.report_timestamp || report?.published_at;
    const timestamp = strictReportTimestamp(rawTime);
    if (!sourceUrl || gameId == null || !gameIds.includes(String(gameId)) || !timestamp ||
        timestamp > tipoff || timestamp > Date.now()) return;
    const name = String(record?.player_name || record?.player || "").trim();
    const teamId = Number(record?.team_id);
    const status = officialReportStatus(record);
    const hasPlayerStatus = Boolean(name && Number.isFinite(teamId) && status);
    const reportPublished = ["available", "published", "success", "complete"].includes(
      String(report?.status || report?.report_status || "").toLowerCase()
    ) || report?.published === true;
    if (!hasPlayerStatus && !reportPublished) return;
    const key = `${sourceUrl}\u0000${timestamp}`;
    if (!groups.has(key)) groups.set(key, { sourceUrl, timestamp, coverage: report.coverage || "complete", records: [] });
    if (!hasPlayerStatus) return;
    if (!name || !Number.isFinite(teamId) ||
        ![Number(game.home.id), Number(game.away.id)].includes(teamId) || !status) return;
    const rosterPlayer = frozenRosterPlayer(game, version, teamId, name,
      record?.player_id || record?.person_id);
    const player = rosterPlayer || {
      name,
      player_id: record?.player_id || record?.person_id || null,
      display_name: (game.player_display_names || []).find((item) => item.canonical_name_en === name)?.display_name_zh,
    };
    groups.get(key).records.push({
      key: availabilityPlayerKey(teamId, player), teamId, player, status: status.status, label: status.label,
      reason: injuryReasonZh(record?.reason || record?.injury_reason), url: sourceUrl, timestamp,
    });
  };

  for (const root of roots) {
    const reports = Array.isArray(root) ? root : Array.isArray(root.reports) ? root.reports : [root];
    for (const report of reports) {
      const records = Array.isArray(report?.records) ? report.records : [];
      if (records.length) records.forEach((record) => addRecord(report, record));
      else addRecord(report, report);
    }
  }
  if (!groups.size) return { status: "unavailable", records: [], items: [] };

  const latestTimestamp = Math.max(...[...groups.values()].map((group) => group.timestamp));
  const latestGroups = [...groups.values()].filter((group) => group.timestamp === latestTimestamp);
  const latestRecords = new Map();
  for (const group of latestGroups) {
    for (const candidate of group.records) {
      const existing = latestRecords.get(candidate.key);
      if (!existing) latestRecords.set(candidate.key, candidate);
      else if (existing.status !== candidate.status || existing.reason !== candidate.reason) {
        latestRecords.set(candidate.key, { ...existing, conflict: true });
      }
    }
  }
  const records = [...latestRecords.values()].filter((record) => !record.conflict);
  const items = records.filter((record) => !["available", "active"].includes(record.status))
    .sort((left, right) => {
      const priority = { out: 0, doubtful: 1, questionable: 2, game_time_decision: 3, probable: 4 };
      return (priority[left.status] ?? 5) - (priority[right.status] ?? 5) ||
        ((left.teamId === Number(game.away.id) ? 0 : 1) - (right.teamId === Number(game.away.id) ? 0 : 1)) ||
        left.player.name.localeCompare(right.player.name);
    });
  return { status: "published", coverage: latestGroups.every(group=>group.coverage==="complete") ? "complete" : "partial",
    timestamp: latestTimestamp, sourceUrl: latestGroups[0]?.sourceUrl || "", records, items };
}

function renderAvailabilityItem(container, item) {
  const detail = document.createElement("details");
  detail.className = `availability-entry availability-${item.status}${item.status === "probable" ? " is-low-priority" : ""}`;
  detail.append(text("summary", `${playerName(item.player)} · ${item.label}${item.reason ? ` · ${item.reason}` : ""}`));
  if (String(item.url || "").startsWith("https://")) {
    const link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "查看官方报告";
    detail.append(link);
  }
  container.append(detail);
}

function seasonFormWindowRecord(window) {
  const games = Number(window?.games);
  const explicitWins = Number(window?.wins);
  const explicitLosses = Number(window?.losses);
  if (window?.status === "available" && Number.isInteger(games) && games > 0 &&
      Number.isInteger(explicitWins) && Number.isInteger(explicitLosses) && explicitWins >= 0 &&
      explicitLosses >= 0 && explicitWins + explicitLosses === games) {
    return {games, wins: explicitWins, losses: explicitLosses};
  }
  const winRate = Number(window?.win_rate);
  if (window?.status !== "available" || !Number.isInteger(games) || games < 1 ||
      !Number.isFinite(winRate) || winRate < 0 || winRate > 1) return null;
  const wins = Math.round(winRate * games);
  if (Math.abs(wins / games - winRate) > 0.00001) return null;
  return {games, wins, losses: games - wins};
}

function formWindowContainsTarget(window, game) {
  const targetIds = [game.id, game.nba_game_id].filter(Boolean).map(String);
  const gameIds = (window?.game_ids || []).map(String);
  return targetIds.some((id) => gameIds.includes(id));
}

function recentStatusLines(game, version, side) {
  const form = game.season_form;
  const cutoff = Date.parse(form?.cutoff || version?.input_cutoff_at || game.technical?.cutoff || "");
  const versionCutoff = Date.parse(version?.input_cutoff_at || game.technical?.cutoff || "");
  const tipoff = Date.parse(game.tipoff || "");
  if (!form || form.status !== "available" || !Number.isFinite(cutoff) || !Number.isFinite(tipoff) ||
      cutoff > tipoff || (Number.isFinite(versionCutoff) && cutoff > versionCutoff) ||
      (form.game_id && form.game_id !== game.id)) return ["赛前近期记录暂不可用"];
  if (game.game_type === "regular_season" && form.competition_scope !== "regular_season") {
    return ["常规赛近期记录暂不可用"];
  }

  const team = form.teams?.[side];
  if (!team) return ["赛前近期记录暂不可用"];
  const currentGames = Number(team.current_season_games || 0);
  const currentWindow = team.current_last5;
  const includesTarget = formWindowContainsTarget(currentWindow, game) ||
    formWindowContainsTarget({game_ids: team.current_season_game_ids}, game);
  const currentRecord = includesTarget ? null : seasonFormWindowRecord(currentWindow);
  if (currentGames > 0) {
    const seasonRecord = includesTarget ? null : seasonFormWindowRecord({
      status: "available", games: currentGames,
      wins: team.current_season_record?.wins, losses: team.current_season_record?.losses,
    });
    const lines = [seasonRecord
      ? `${form.competition_scope === "regular_season" ? "本季常规赛" : "本季"}已赛${seasonRecord.games}场：${seasonRecord.wins}胜${seasonRecord.losses}负`
      : currentRecord
        ? `${form.competition_scope === "regular_season" ? "本季常规赛" : "本季"}近${currentRecord.games}场：${currentRecord.wins}胜${currentRecord.losses}负`
        : "本季近期战绩暂不可用"];
    const last = team.last_result;
    const lastTime = Date.parse(last?.played_at || "");
    if (last && last.game_id !== game.id && Number.isFinite(lastTime) && lastTime < cutoff &&
        Number.isFinite(Number(last.team_score)) && Number.isFinite(Number(last.opponent_score)) &&
        last.opponent_name_zh) {
      lines.push(`上一场：${number(last.team_score, 0)}–${number(last.opponent_score, 0)} ${last.opponent_name_zh}`);
    }
    const streakGames = Number(team.streak?.games);
    if (!includesTarget && Number.isInteger(streakGames) && streakGames >= 2 && ["win", "loss"].includes(team.streak?.result)) {
      lines.push(`当前${team.streak.result === "win" ? "连胜" : "连败"}${streakGames}场`);
    }
    return lines;
  }
  return [form.competition_scope === "regular_season" ? "本季常规赛暂无比赛记录" : "本季暂无比赛记录"];
}

function renderRecentStatus(game, version, container) {
  for (const side of ["away", "home"]) {
    const team = document.createElement("div");
    team.className = "recent-form-team";
    team.append(text("strong", game[side].name_zh));
    recentStatusLines(game, version, side).forEach((line) => team.append(text("p", line)));
    container.append(team);
  }
}

function renderContext(game, selectedVersion = null) {
  const injury = byId("injury-context");
  injury.replaceChildren(text("h3", "伤病与出战"));
  const version = selectedVersion || state.currentAnalyses.get(game.id);
  const officialReport = officialInjuryReportProjection(game, version);
  officialReport.items.forEach((item) => renderAvailabilityItem(injury, item));
  if (officialReport.status !== "published") {
    injury.append(text("p", "本场官方伤病报告暂未获取", "context-missing official-injury-empty"));
  } else if (officialReport.coverage === "partial") {
    injury.append(text("p", "部分球队状态尚未提交，当前只展示已公布信息。", "context-missing official-injury-empty"));
  } else if (!officialReport.items.length) {
    injury.append(text("p", "本场官方伤病报告中暂无异常状态", "context-missing official-injury-empty"));
  }

  const recent = byId("schedule-context");
  recent.replaceChildren(text("h3", "近期状态"));
  renderRecentStatus(game, version, recent);
  renderMarket(game);
  renderLineups(game, selectedVersion, officialReport);
  renderKeyPlayers(game, null, "idle", officialReport);
  renderTeamData(game);
}

function formatCutoffForDisplay(value) {
  const timestamp = Date.parse(value || "");
  if (!Number.isFinite(timestamp)) return "未记录";
  const formatted = dateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
  return `${formatted} 北京时间`;
}

function renderTechnical(game, selectedVersion = state.currentAnalyses.get(game.id), selection = state.predictionStates.get(game.id)) {
  const container = byId("technical-content");
  container.replaceChildren();
  const displayProjection = selection?.display_projection;
  const frozenCutoffs = displayProjection && selectedVersion?.version_id &&
    displayProjection.version_id === selectedVersion.version_id
    ? displayProjection.cutoffs || {}
    : {};
  const rows = [
    ["数据身份", game.id],
    ["预测状态", game.technical.source_label],
    ["Prediction Version 输入截止", formatCutoffForDisplay(frozenCutoffs.input_cutoff_at)],
    ["球队历史数据特征截止", formatCutoffForDisplay(frozenCutoffs.feature_cutoff_at)],
    ["阵容与赛前情报上下文截止", formatCutoffForDisplay(frozenCutoffs.roster_context_cutoff_at)],
    ["赛程来源时间", game.source.schedule_captured_at || "未记录"],
  ];
  rows.forEach(([label, content]) => {
    const row = document.createElement("div");
    row.className = "technical-row";
    row.append(text("span", label), text("code", content));
    container.append(row);
  });
  if (game.missing.length) {
    const list = document.createElement("ul");
    list.className = "missing-list";
    const reasons = {
      availability: "暂无符合要求的伤病数据",
      market: "暂无符合来源与时间要求的市场数据",
      key_players: "暂无符合要求的核心球员数据",
      team_form: "近期球队状态数据不完整",
      roster: "暂无符合时间要求的当前名单",
      roster_continuity: "名单或上赛季分钟缺失，阵容连续性暂不可计算",
      matchup: "比赛背景数据不完整",
      residual: "预测修正暂不可用，具体降级方式见预测记录",
      residual_prediction: "预测修正暂不可用，具体降级方式见预测记录",
    };
    [
      ...new Set(
        game.missing.map(
          (item) => reasons[item.section] || "部分补充数据暂不可用",
        ),
      ),
    ].forEach((reason) => list.append(text("li", reason)));
    container.append(text("p", "当前缺失"), list);
  }
  const advanced = document.createElement("details");
  advanced.append(
    text("summary", "高级原始信息"),
    text(
      "pre",
      JSON.stringify(
        {
          prediction_method: game.technical.prediction_method,
          model_version: game.technical.model_version,
          missing_data: game.missing,
          source: game.source,
          roster_context: game.roster_context,
        },
        null,
        2,
      ),
    ),
  );
  container.append(advanced);
}

function renderPredictionDisplay(game) {
  const current = activePrediction(game);
  const focusTitle = byId("prediction-focus")?.querySelector("p") || document.querySelector(".prediction-focus > p");
  const judgmentTitle = byId("primary-judgment-title");
  const probabilityLens = byId("probability-lens");
  const probabilityLabel = document.querySelector(".prediction-focus > small");
  const marginTitle = document.querySelector(".primary-numbers > div:first-child > span");
  const totalTitle = document.querySelector(".primary-numbers > div:last-child > span");
  const marginNode = byId("expected-margin");
  const totalNode = byId("expected-total");
  if (!current) {
    if (hasVisibleScore(game)) {
      const homeScore = Number(game.home.score);
      const awayScore = Number(game.away.score);
      const final = ["post", "final", "completed", "finished"].includes(game.status);
      const label = final ? "最终比分" : "当前比分";
      if (focusTitle) focusTitle.textContent = label;
      if (judgmentTitle) judgmentTitle.textContent = "比赛结果";
      byId("predicted-winner").textContent = `${game.away.name_zh} ${scoreText(awayScore)} : ${scoreText(homeScore)} ${game.home.name_zh}`;
      probabilityLens.hidden = true;
      if (probabilityLabel) probabilityLabel.hidden = true;
      byId("predicted-probability").textContent = "";
      if (marginTitle) marginTitle.textContent = final ? "实际分差" : "当前分差";
      if (totalTitle) totalTitle.textContent = final ? "实际总分" : "当前总分";
      const winner = homeScore >= awayScore ? game.home : game.away;
      marginNode.textContent = `${winner.name_zh} ${final ? "赢" : "领先"} ${scoreText(Math.abs(homeScore - awayScore))} 分`;
      totalNode.textContent = scoreText(homeScore + awayScore);
      byId("prediction-updated").hidden = true;
      return;
    }
    if (focusTitle) focusTitle.textContent = "赛前预测";
    if (judgmentTitle) judgmentTitle.textContent = "第25秒判断";
    byId("predicted-winner").textContent = "尚无正式预测";
    byId("predicted-probability").textContent = "—";
    probabilityLens.hidden = false;
    probabilityLens.style.setProperty("--probability", "0%");
    if (probabilityLabel) probabilityLabel.hidden = false;
    if (marginTitle) marginTitle.textContent = "分差";
    if (totalTitle) totalTitle.textContent = "总分";
    marginNode.textContent = "暂不可用";
    totalNode.textContent = "暂不可用";
    byId("prediction-updated").hidden = true;
    return;
  }
  if (focusTitle) focusTitle.textContent = "赛前预测";
  if (judgmentTitle) judgmentTitle.textContent = "第25秒判断";
  probabilityLens.hidden = false;
  if (probabilityLabel) probabilityLabel.hidden = false;
  if (marginTitle) marginTitle.textContent = "分差";
  if (totalTitle) totalTitle.textContent = "总分";
  const predicted = teamById(game, current.winner_team_id);
  byId("predicted-winner").textContent = predicted.name_zh;
  byId("predicted-probability").textContent = percent(current.winner_probability);
  byId("probability-lens").style.setProperty(
    "--probability",
    `${current.winner_probability * 100}%`,
  );
  byId("expected-margin").textContent = marginLabel(game).replace(/^预计/, "");
  byId("expected-total").textContent = number(current.expected_total);
  const updated = byId("prediction-updated");
  updated.hidden = !current.updated_at;
  updated.textContent = current.updated_at
    ? `更新于 ${displayClock(current.updated_at)}`
    : "";
}

function hasSafeCurrentScheduleForm(game, version) {
  const form = game?.season_form;
  if (form?.source !== "schedule_catalog_final_games" || form.game_id !== game?.id) return false;
  const cutoff = Date.parse(form.cutoff || "");
  const tipoff = Date.parse(game.tipoff || "");
  const versionCutoff = Date.parse(version?.input_cutoff_at || "");
  return Number.isFinite(cutoff) && Number.isFinite(tipoff) && cutoff < tipoff &&
    (!version?.input_cutoff_at || (Number.isFinite(versionCutoff) && cutoff <= versionCutoff));
}

function renderDetail(game) {
  const selectedVersion = state.currentAnalyses.get(game.id);
  const frozenInput = selectedVersion?.version_id ? selectedVersion.input_snapshot : null;
  if (frozenInput && typeof frozenInput === "object") {
    game = {
      ...game,
      background: frozenInput.background || {
        schedule: {}, recent_form: {home: {}, away: {}},
        availability: {status: "unavailable", records: []},
      },
      roster_context: Object.prototype.hasOwnProperty.call(frozenInput, "roster_context")
        ? frozenInput.roster_context : null,
      matchup_history: Object.prototype.hasOwnProperty.call(frozenInput, "matchup_history")
        ? frozenInput.matchup_history : null,
      season_form: hasSafeCurrentScheduleForm(game, selectedVersion)
        ? game.season_form
        : Object.prototype.hasOwnProperty.call(frozenInput, "season_form")
          ? frozenInput.season_form : null,
      fact_context: state.factViews.get(game.id)?.context || null,
      technical: {...game.technical, ...(frozenInput.technical || {})},
      source: frozenInput.source || game.source,
    };
  }
  state.activeGame = game;
  const currentPrediction = activePrediction(game);
  const detail = byId("detail-view");
  detail.dataset.awayAbbr = game.away.abbreviation;
  detail.dataset.homeAbbr = game.home.abbreviation;
  detail.dataset.matchLabel = `${game.away.abbreviation}  /  ${game.home.abbreviation}`;
  detail.style.setProperty("--away-logo-image", `url("${logoSource(game.away)}")`);
  detail.style.setProperty("--home-logo-image", `url("${logoSource(game.home)}")`);
  for (const side of ["away", "home"]) {
    const field = detail.querySelector(`.match-field-${side}`);
    field.dataset.abbr = game[side].abbreviation;
    field.style.setProperty("--field-accent", game[side].accent);
    field.style.setProperty("--field-logo", `url("${logoSource(game[side])}")`);
  }
  const favorite =
    state.favoriteTeamId &&
    [game.home.id, game.away.id].includes(state.favoriteTeamId);
  const accentTeam = favorite
    ? teamById(game, state.favoriteTeamId)
    : currentPrediction
      ? teamById(game, currentPrediction.winner_team_id)
      : game.home;
  document.documentElement.style.setProperty(
    "--team-accent",
    accentTeam.accent,
  );
  document.documentElement.style.setProperty("--away-accent", game.away.accent);
  document.documentElement.style.setProperty("--home-accent", game.home.accent);
  document.body.dataset.gameTheme = "active";
  byId("detail-time").textContent = displayDate(game.tipoff);
  const scoreStatus = hasVisibleScore(game)
    ? `${["post", "final", "completed", "finished"].includes(game.status) ? " · 终场" : " · 当前比分"} ${scoreText(game.away.score)}-${scoreText(game.home.score)}`
    : "";
  const phaseLabel = selectedVersion?.stage === "FINAL_PREGAME" ? " · 最终赛前分析"
    : selectedVersion?.stage === "EARLY_PREVIEW" ? " · 赛前预览"
      : selectedVersion?.stage === "UPDATED_PREVIEW" ? " · 重要更新" : "";
  const scheduleChanged = state.predictionStates.get(game.id)?.status === "schedule_changed";
  byId("detail-status").textContent = scheduleChanged
    ? `${statusLabel(game)}${scoreStatus} · 赛程改期，暂无适用的新版预测`
    : `${game.game_type === "preseason" ? "季前赛 · " : ""}${statusLabel(game)}${scoreStatus}${phaseLabel}`;
  byId("away-team").replaceChildren(stageTeam(game.away));
  byId("home-team").replaceChildren(stageTeam(game.home));
  renderPredictionDisplay(game);
  renderContext(game, selectedVersion);
  renderMatchupHistory(game);
  renderTechnical(game, selectedVersion, state.predictionStates.get(game.id));
  const cachedAnalysis = state.currentAnalyses.get(game.id);
  if (cachedAnalysis) renderProductAnalysis(cachedAnalysis);
  else
    renderProductAnalysisState(
      game,
      state.aiFailures.has(game.id) ? "unavailable" : "idle",
    );
  const locked = ["locked", "locked_unavailable", "schedule_changed"].includes(
    state.predictionStates.get(game.id)?.status,
  );
  byId("ai-analysis").disabled = (locked || Boolean(game.generation_hold_reason)) && !cachedAnalysis;
  byId("ai-analysis").querySelector("strong").textContent =
    cachedAnalysis
    ? "继续阅读第25秒分析"
    : locked ? "比赛已开始" : game.generation_hold_reason ? "尚无正式赛前分析" : "阅读第25秒分析";
  const postgame = byId("postgame-entry");
  const oldPostgame = postgame.querySelector(".postgame-content");
  if (oldPostgame && (oldPostgame.dataset.gameId !== game.id || oldPostgame.dataset.expectedVersionId !== game.postgame?.version_id)) oldPostgame.remove();
  const postButton = postgame.querySelector("button");
  const availablePostgame = game.postgame?.status === "available";
  const basicPostgame = availablePostgame && game.postgame?.review_level === "basic";
  const pendingPostgame = game.postgame?.status === "pending";
  const unavailableEditorial = game.postgame?.status === "editorial_unavailable";
  postButton.disabled = !availablePostgame;
  postButton.textContent = availablePostgame ? (basicPostgame ? "查看赛后基础复核" : "查看赛后复盘")
    : pendingPostgame ? "赛后复盘准备中"
      : unavailableEditorial ? "复盘文章暂不可用"
        : ["post", "final", "completed", "finished"].includes(game.status) ? "暂无可用复盘" : "等待比赛结束";
  const postgameScore = byId("postgame-score");
  postgameScore.hidden = !availablePostgame && !unavailableEditorial;
  postgameScore.textContent = game.postgame?.score_text || "";
  const postgamePreview = byId("postgame-preview");
  postgamePreview.hidden = !availablePostgame;
  postgamePreview.textContent = availablePostgame
    ? [game.postgame?.prediction_summary, game.postgame?.market_summary].filter(Boolean).join("  ·  ")
    : "";
  byId("postgame-copy").textContent = availablePostgame
    ? (basicPostgame ? "已核对赛果与赛前判断；完整复盘文章尚未生成。" : "回看赛前判断与真实赛果，再进入完整篮球复盘。")
    : pendingPostgame
      ? "比赛已经结束，赛果正在同步。"
      : unavailableEditorial
        ? "赛果已同步，完整复盘文章暂不可用。"
      : ["post", "final", "completed", "finished"].includes(game.status)
        ? "本场暂无已发布的赛后复盘。"
      : "比赛结束后，这里将回看赛前判断对了什么、错了什么。";
  document.querySelector(".technical-details").open = false;
  document.title = `${game.away.name_zh} vs ${game.home.name_zh} · 第25秒`;
}

function showView(name) {
  const home = byId("home-view");
  const detail = byId("detail-view");
  const homeActive = name === "home";
  home.hidden = !homeActive;
  detail.hidden = homeActive;
  home.classList.toggle("is-active", homeActive);
  detail.classList.toggle("is-active", !homeActive);
}

function route() {
  if (!state.data) return;
  if (!byId("ai-sheet").hidden) closeSheet();
  const raw = location.hash || "#home";
  if (raw.startsWith("#game/")) {
    const gameId = decodeURIComponent(raw.slice(6));
    const game = state.data.games.find((item) => item.id === gameId);
    if (game && !game.public_summary) {
      if (!state.activeGame) state.homeScroll = window.scrollY;
      showView("detail");
      renderDetail(game);
      window.scrollTo({ top: 0, behavior: "auto" });
      byId("back-home").focus({ preventScroll: true });
      return;
    }
    state.activeGame = null;
    showView("home");
    loadGameById(gameId).catch(() => {
      const error = byId("app-error");
      error.querySelector("strong").textContent = "没有找到这场比赛";
      error.querySelector("p").textContent = "比赛详情暂时无法读取，请返回今天或明天的赛程。";
      error.hidden = false;
    });
    return;
  }
  state.activeGame = null;
  delete document.body.dataset.gameTheme;
  document.documentElement.style.removeProperty("--away-accent");
  document.documentElement.style.removeProperty("--home-accent");
  applyFavoriteTheme();
  showView("home");
  document.title = "第25秒 · 看见比分之外";
  requestAnimationFrame(() =>
    window.scrollTo({ top: state.homeScroll, behavior: "auto" }),
  );
}

async function loadGameById(gameId) {
  if (state.gameLoads.has(gameId)) return state.gameLoads.get(gameId);
  const loading = (async () => {
    const response = await fetch(`/api/game?game_id=${encodeURIComponent(gameId)}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || payload.status !== "success" || payload.game?.id !== gameId) {
      throw new Error("game_detail_unavailable");
    }
    const existing = state.data.games.findIndex((item) => item.id === gameId);
    payload.game.homepage_visible = existing >= 0 ? state.data.games[existing].homepage_visible : false;
    if (existing >= 0) state.data.games[existing] = payload.game;
    else state.data.games.push(payload.game);
    if (payload.prediction) acceptPredictionSelection(gameId, payload.prediction);
    if (decodeURIComponent((location.hash || "").slice(6)) === gameId) route();
    return payload.game;
  })();
  state.gameLoads.set(gameId, loading);
  try { return await loading; }
  finally { state.gameLoads.delete(gameId); }
}

function sheetState(title, message, loading = false) {
  const node = document.createElement("div");
  node.className = "sheet-state";
  if (loading) node.append(text("span", "", "loader"));
  node.append(text("div", ""));
  node.lastElementChild.append(text("h3", title), text("p", message));
  return node;
}

function appendAnalysisRefreshNote(container, message) {
  const note = text("p", message, "analysis-refresh-note");
  const rail = container.querySelector(".analyst-verdict");
  if (rail) rail.append(note);
  else container.prepend(note);
}

function openSheet() {
  if (byId("ai-sheet").hidden) state.previousFocus = document.activeElement;
  const layer = byId("ai-sheet");
  layer.hidden = false;
  byId("main").inert = true;
  document.querySelector(".topbar").inert = true;
  document.body.style.overflow = "hidden";
  const sheet = layer.querySelector(".analysis-sheet");
  byId("sheet-matchup").textContent =
    `${state.activeGame.away.name_zh} vs ${state.activeGame.home.name_zh} · ${displayDate(state.activeGame.tipoff)}`;
  sheet.scrollTop = 0;
  requestAnimationFrame(() => sheet.focus());
}

function closeSheet() {
  byId("ai-sheet").hidden = true;
  byId("main").inert = false;
  document.querySelector(".topbar").inert = false;
  document.body.style.overflow = "";
  state.previousFocus?.focus();
}

// Presentation-only glossary. Preserve the unmodified source in expandable text.
function playerDisplayCopy(value) {
  let result = String(value ?? "");
  const catalog = state.currentAnalyses.get(state.activeGame?.id)?.input_snapshot?.player_display_names || state.activeGame?.player_display_names || [];
  const aliases = new Map(catalog.map((row) => [row.canonical_name_en, row.display_name_zh]));
  const counts = new Map();
  catalog.forEach((row) => row.canonical_name_en.split(/\s+/).forEach((part) => {
    if (part.length >= 5) counts.set(part, (counts.get(part) || 0) + 1);
  }));
  catalog.forEach((row) => row.canonical_name_en.split(/\s+/).forEach((part) => {
    if (part.length >= 5 && counts.get(part) === 1) aliases.set(part, row.display_name_zh);
  }));
  [...aliases.entries()].sort(([a], [b]) => b.length - a.length).forEach(([raw, zh]) => {
    result = result.replace(new RegExp(`(?<![A-Za-z])${raw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![A-Za-z])`, "g"), zh);
  });
  return result;
}

function analystCopy(value) {
  const terms = {
    "prediction_method=pure_basketball_fallback": "当前沿用纯篮球预测",
    "market_usage=unavailable": "市场数据暂不可用",
    "opening_night_roster_confirmed 为 false": "揭幕战最终名单尚未确认",
    "previous_season_is_majority=true": "上赛季比赛占多数",
    "previous_season_is_majority 为 true": "上赛季比赛占多数",
    "key_player_status 为 null": "关键球员状态尚未确认",
    "cold_start=true": "近期数据仍含旧赛季比赛",
    "cold_start 显示当前赛季观测场次为0": "当前赛季还没有可用比赛样本",
    cold_start: "新赛季冷启动状态",
    "continuity_status=unavailable": "阵容连续性暂不可计算",
    "confirmed_starters 为空": "确认首发尚未公布",
    "current_rotation 未验证": "当前轮换尚未验证",
    confirmed_starters: "确认首发",
    current_rotation: "当前轮换",
    "def_rating_15": "近十五场防守效率",
    "no legal future market data available": "暂无符合要求的赛前市场数据",
    official_team_roster: "球队官方名单",
    league_assignment_cross_check: "联盟名单交叉核对",
    assignment_only: "仅在联盟名单出现的",
    retained_minutes_ratio: "上赛季分钟保留比例",
    top8_retained_count: "上赛季主要八名球员保留人数",
    top8: "主要八人",
    continuity: "阵容连续性",
    unavailable: "暂不可用",
    rolling_15: "近十五场",
    rolling_5: "近五场",
    rolling: "近期窗口",
    Pure: "纯篮球预测",
    Residual: "预测修正",
    Market: "市场参考",
    matchup: "比赛匹配",
    Embiid: "恩比德",
    Maxey: "马克西",
    Brunson: "布伦森",
    Towns: "唐斯",
    Anunoby: "阿奴诺比",
    Bridges: "布里奇斯",
    Hart: "哈特",
    McBride: "麦克布莱德",
    "Caldwell-Pope": "考德威尔-波普",
    Simons: "西蒙斯",
    Edgecombe: "埃奇库姆",
    Drummond: "德拉蒙德",
    Clarkson: "克拉克森",
    "McCullar": "麦克卡勒",
    Kolek: "科勒克",
    Shamet: "沙梅特",
    LeBron: "勒布朗·詹姆斯",
    KCP: "考德威尔-波普",
  };
  let result = playerDisplayCopy(value);
  [...Object.entries(PLAYER_DISPLAY_NAMES), ...Object.entries(terms)]
    .sort(([left], [right]) => right.length - left.length)
    .forEach(([raw, readable]) => {
      result = result.split(raw).join(readable);
    });
  if (state.activeGame) {
    ["away", "home"].forEach((side) => {
      (rosterFor(state.activeGame, side)?.players || []).forEach((player) => {
        if (player.name && player.display_name) {
          result = result.split(player.name).join(player.display_name);
        }
      });
    });
  }
  return result;
}

function renderLegacyJudgment(payload) {
  const judgment = payload.judgment;
  renderProductAnalysis(payload);
  const container = byId("sheet-content");
  const readable = {
    home: state.activeGame.home.name_zh,
    away: state.activeGame.away.name_zh,
    no_lean: "暂不判断",
    insufficient_evidence: "证据不足",
    over: "偏高",
    under: "偏低",
    low: "低",
    medium: "中",
    high: "高",
  };
  const verdict = document.createElement("section");
  verdict.className = "analyst-verdict";
  const heading = text(
    "h3",
    ["home", "away"].includes(judgment.winner_lean)
      ? `倾向${readable[judgment.winner_lean]}`
      : readable[judgment.winner_lean] || "暂不判断",
  );
  verdict.append(
    text("p", "先给观点", "verdict-label"),
    heading,
    text("p", analystCopy(judgment.subjective_rationale)),
  );
  container.replaceChildren(verdict);
  const question = document.createElement("section");
  question.className = "analysis-thesis";
  question.append(
    text("p", "这场真正要看什么", "verdict-label"),
    text(
      "h3",
      analystCopy(
        judgment.core_question ||
          judgment.key_matchup_variables?.[0]?.statement ||
          "哪一方能让比赛进入自己更熟悉的节奏？",
      ),
    ),
  );
  container.append(question);
  const tabs = document.createElement("div");
  tabs.className = "analysis-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "分析阅读主题");
  container.append(tabs);
  const panels = [];
  const sources = {
    roster_context: "官方名单与赛季窗口",
    prediction: "预测记录",
    team_form: "历史球队表现",
    matchup: "比赛背景",
    market: "市场来源",
    availability: "球员可用性",
    provenance: "数据来源说明",
  };
  function claimSection(panel, title, claims, empty = null) {
    if (!claims?.length && !empty) return;
    const section = document.createElement("section");
    section.className = "analysis-block";
    section.append(text("h3", title));
    if (!claims?.length) section.append(text("p", empty, "analysis-empty"));
    else {
      const list = document.createElement("ul");
      claims.forEach((claim) => {
        const item = document.createElement("li");
        if (claim.player_name) item.append(text("strong", analystCopy(claim.player_name)));
        item.append(text("p", analystCopy(claim.statement)));
        const meta = document.createElement("div");
        meta.className = "analysis-meta";
        if (
          claim.evidence_ids?.length ||
          analystCopy(claim.statement) !== claim.statement
        ) {
          const refs = document.createElement("details");
          refs.append(text("summary", "查看依据"));
          (claim.evidence_ids || []).forEach((id) => {
            const evidence = payload.evidence_index?.[id];
            const linked = (payload.sources || []).find((source) => source.id === id);
            if (linked?.url) {
              const anchor = document.createElement("a");
              anchor.href = linked.url;
              anchor.target = "_blank";
              anchor.rel = "noreferrer noopener";
              anchor.textContent = linked.title || linked.url;
              refs.append(anchor);
            } else {
              refs.append(text("p", sources[evidence?.section] || "报告中的原始证据"));
            }
          });
          meta.append(refs);
        }
        if (meta.childElementCount) item.append(meta);
        list.append(item);
      });
      section.append(list);
    }
    panel.append(section);
  }
  ["为什么这么看", "关键球员与对位", "风险与未知"].forEach((title, index) => {
    const button = text("button", title);
    button.id = `analysis-tab-${index}`;
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(index === 0));
    button.setAttribute("aria-controls", `analysis-panel-${index}`);
    button.tabIndex = index === 0 ? 0 : -1;
    const panel = document.createElement("div");
    panel.id = `analysis-panel-${index}`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", button.id);
    panel.tabIndex = 0;
    panel.hidden = index !== 0;
    const activate = () => {
      [...tabs.children].forEach((tab, i) => {
        tab.setAttribute("aria-selected", String(i === index));
        tab.tabIndex = i === index ? 0 : -1;
      });
      panels.forEach((item, i) => (item.hidden = i !== index));
    };
    button.addEventListener("click", activate);
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      const next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? 2
            : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
      tabs.children[next].click();
      tabs.children[next].focus();
    });
    tabs.append(button);
    panels.push(panel);
    container.append(panel);
  });
  claimSection(
    panels[0],
    "为什么这么看",
    judgment.supporting_evidence,
    "分析师未单独列出支持证据。",
  );
  claimSection(
    panels[0],
    "冲突信息",
    judgment.conflicting_evidence,
    "本次未列出明确的冲突信息。",
  );
  claimSection(panels[0], "本场关键变量", judgment.key_matchup_variables);
  claimSection(
    panels[1],
    "球队与阵容",
    judgment.roster_analysis,
    "当前没有足够的阵容证据形成结论。",
  );
  claimSection(
    panels[1],
    "关键球员与对位",
    judgment.player_analysis,
    "当前没有足够的球员信息形成结论。",
  );
  claimSection(panels[1], "新赛季，如何看待历史表现", [
    ...(judgment.cold_start_analysis || []),
    ...(judgment.historical_data_relevance || []),
  ]);
  const script = document.createElement("section");
  script.className = "analysis-block analysis-narrative";
  script.append(
    text("h3", "比赛可能怎么打"),
    text("p", analystCopy(judgment.game_script || "当前证据还不足以形成清晰的比赛路径。")),
  );
  panels[2].append(script);
  const risk = document.createElement("section");
  risk.className = "analysis-block analysis-risk";
  risk.append(
    text("h3", "模型最可能错在哪里"),
    text("p", analystCopy(judgment.model_risk || "当前未单独生成模型风险判断。")),
  );
  panels[2].append(risk);
  claimSection(
    panels[2],
    "仍未解决的不确定性",
    judgment.uncertainty?.length
      ? judgment.uncertainty
      : (judgment.unresolved_uncertainty || []).map((statement) => ({
          statement,
          classification: "source_fact",
          evidence_ids: [],
        })),
    "本次没有额外列出的不确定性。",
  );
  const linkedSources = (payload.sources || []).filter(
    (source) => typeof source.url === "string" && /^https?:\/\//.test(source.url),
  );
  if (linkedSources.length) {
    const sourceBlock = document.createElement("details");
    sourceBlock.className = "analysis-block analysis-sources";
    sourceBlock.append(text("summary", `查看来源（${linkedSources.length}）`));
    linkedSources.slice(0, 12).forEach((source) => {
      const link = document.createElement("a");
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = source.title || source.url;
      sourceBlock.append(link);
    });
    panels[2].append(sourceBlock);
  }
  renderPublicMarketReference(payload.public_market_reference);
  renderKeyPlayers(state.activeGame, judgment.player_analysis, "success");
}

function renderJudgment(payload) {
  const judgment = displayedJudgment(payload);
  renderProductAnalysis(payload);
  renderPredictionDisplay(state.activeGame);
  renderGames();
  const current = activePrediction(state.activeGame);
  const winner = teamById(state.activeGame, current.winner_team_id);
  const container = byId("sheet-content");
  const verdict = document.createElement("section");
  verdict.className = "analyst-verdict";
  verdict.append(
    text("p", payload.stage === "EARLY_PREVIEW" ? "第25秒 · 赛前预览" : payload.stage === "FINAL_PREGAME" ? "第25秒 · 最终赛前分析" : payload.stage === "UPDATED_PREVIEW" ? "第25秒 · 重要更新" : "第25秒 · 赛前判断", "verdict-label"),
    text("h3", `${winner.name_zh}胜`),
    text(
      "p",
      `${marginLabel(state.activeGame)} · 胜率 ${percent(current.winner_probability)} · 预计总分 ${number(current.expected_total)}`,
    ),
  );
  if (current.updated_at) {
    verdict.append(
      text("p", `更新于 ${displayClock(current.updated_at)}`, "analyst-disclaimer"),
    );
  }
  const locked = ["locked", "locked_unavailable", "schedule_changed"].includes(
    state.predictionStates.get(state.activeGame.id)?.status,
  );
  const refresh = text(
    "button",
    locked ? "赛前判断已锁定" : "刷新已发布分析",
    "analysis-refresh-button",
  );
  refresh.type = "button";
  refresh.disabled = locked;
  if (!locked) refresh.addEventListener("click", () => requestAiAnalysis(true));
  verdict.append(refresh);
  const shortReviewTitle =
    displayedStoryTitle(payload) ||
    judgment.core_question ||
    `${state.activeGame.away.name_zh}对${state.activeGame.home.name_zh}`;
  const shortReview = document.createElement("header");
  shortReview.className = "reader-cover";
  shortReview.append(
    text("p", "第25秒 · 赛前分析", "reader-kicker"),
    text("h2", analystCopy(shortReviewTitle), "reader-title"),
  );
  container.replaceChildren(shortReview, verdict);
  const article = document.createElement("article");
  article.className = "analysis-block analysis-article";
  analystCopy(judgment.analysis_article || "")
    .split(/\n\s*\n/)
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((paragraph) => article.append(text("p", paragraph)));
  container.append(article);
  const labels = {
    roster_context: "官方名单与赛季窗口",
    prediction: "预测输入记录",
    team_form: "球队状态数据",
    matchup: "赛程与比赛背景",
    market: "盘口数据",
    availability: "伤病与出战数据",
    web_context: "赛前新闻与公开来源",
  };
  const evidenceIds = new Set([
    ...(judgment.analysis_article_evidence_ids || []),
    ...(judgment.final_prediction?.evidence_ids || []),
  ]);
  const sourceBlock = document.createElement("details");
  sourceBlock.className = "analysis-block analysis-sources";
  sourceBlock.append(text("summary", "来源与数据"));
  new Set(
    [...evidenceIds]
      .map((id) => payload.evidence_index?.[id]?.section)
      .filter(Boolean),
  ).forEach((section) => {
    sourceBlock.append(text("p", labels[section] || "赛前结构化数据"));
  });
  (payload.sources || [])
    .filter((source) => typeof source.url === "string" && /^https?:\/\//.test(source.url))
    .slice(0, 12)
    .forEach((source) => {
      const link = document.createElement("a");
      link.href = source.url;
      link.target = "_blank";
      link.rel = "noreferrer noopener";
      link.textContent = source.title || source.url;
      sourceBlock.append(link);
    });
  container.append(sourceBlock);
  renderPublicMarketReference(payload.public_market_reference);
  renderKeyPlayers(state.activeGame, judgment.player_analysis || [], "success");
}

function renderOfflineAnalysis(game, message) {
  const container = byId("sheet-content");
  const normalizedMessage = String(message || "本次分析未能完成。").trim();
  container.replaceChildren(
    sheetState(
      "第25秒分析暂不可用",
      `${normalizedMessage} 你仍可查看当前预测和结构化赛前事实。`,
      false,
    ),
  );
  renderProductAnalysisState(game, "unavailable");
}

async function requestAiAnalysis(forceRefresh = false) {
  if (!state.activeGame || state.aiPending) return;
  const gameId = state.activeGame.id;
  openSheet();
  const content = byId("sheet-content");
  const cached = state.currentAnalyses.get(gameId);
  if (cached) {
    renderJudgment(cached);
    appendAnalysisRefreshNote(content, "正在读取已发布分析…");
  } else {
    content.replaceChildren(
      sheetState(
        "正在读取赛前分析",
        "正在读取这场比赛当前已发布的报告。",
        true,
      ),
    );
  }
  content.setAttribute("aria-busy", "true");
  const button = byId("ai-analysis");
  state.aiPending = true;
  state.aiFailures.delete(gameId);
  button.disabled = true;
  try {
    const facts = await fetchScheduleWindow();
    let freshGame = facts.games?.find((game) => game.id === gameId);
    let directSelection = null;
    if (!freshGame || freshGame.public_summary) {
      const detailResponse = await fetch(`/api/game?game_id=${encodeURIComponent(gameId)}`, { cache: "no-store" });
      const detailPayload = await detailResponse.json();
      if (!detailResponse.ok || detailPayload.status !== "success" || detailPayload.game?.id !== gameId) {
        throw new Error("没有找到当前比赛");
      }
      freshGame = detailPayload.game;
      directSelection = detailPayload.prediction;
      const existingSlot = facts.games.findIndex(game => game.id === gameId);
      if (existingSlot >= 0) facts.games[existingSlot] = freshGame;
      else {
        freshGame.homepage_visible = false;
        facts.games.push(freshGame);
      }
    }
    if (state.activeGame?.id !== gameId) return;
    if (Number(freshGame.schedule_revision ?? 1) !== Number(state.activeGame.schedule_revision ?? 1)) {
      state.currentAnalyses.delete(gameId);
      state.factViews.delete(gameId);
    }
    state.data = facts;
    state.activeGame = freshGame;
    if (directSelection) acceptPredictionSelection(gameId, directSelection);
    renderDetail(freshGame);
    if (typeof readingHeader === "function") readingHeader(freshGame);
    const response = await fetch("/api/current-analyses", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error("报告读取失败");
    if (payload.predictions?.[gameId]) {
      acceptPredictionSelection(gameId, payload.predictions[gameId]);
    }
    const selected = state.currentAnalyses.get(gameId);
    if (state.activeGame?.id !== gameId) return;
    if (!selected) {
      renderOfflineAnalysis(state.activeGame, "当前没有已发布的赛前分析，请稍后查看。");
      return;
    }
    state.aiFailures.delete(gameId);
    if (state.activeGame?.id === gameId) {
      renderPredictionDisplay(state.activeGame);
      renderGames();
      renderJudgment(selected);
      button.querySelector("strong").textContent = "继续阅读第25秒分析";
    }
  } catch (error) {
    if (state.activeGame?.id === gameId) {
      const latest = state.currentAnalyses.get(gameId);
      const lockedNow = ["locked", "locked_unavailable", "schedule_changed"].includes(
        state.predictionStates.get(gameId)?.status,
      );
      if (latest) {
        renderJudgment(latest);
        if (!lockedNow) appendAnalysisRefreshNote(content, "读取未完成，仍显示上一份已发布分析。");
        return;
      }
      state.aiFailures.add(gameId);
      if (lockedNow) {
        renderOfflineAnalysis(state.activeGame, "比赛已开始，当前没有可读取的赛前判断。");
        return;
      }
      renderOfflineAnalysis(state.activeGame, "暂时无法读取已发布分析，请稍后重试。");
      const retry = text("button", "重新尝试", "retry-button");
      retry.type = "button";
      retry.addEventListener("click", () => requestAiAnalysis(false));
      content.append(retry);
    }
  } finally {
    state.aiPending = false;
    content.setAttribute("aria-busy", "false");
    button.disabled = ["locked", "locked_unavailable", "schedule_changed"].includes(
      state.predictionStates.get(gameId)?.status,
    ) && !state.currentAnalyses.has(gameId);
  }
}

function signedNumber(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return (numeric > 0 ? "+" : "") + number(numeric);
}

function postgameNumber(value) {
  const formatted = number(value);
  return formatted.endsWith(".0") ? formatted.slice(0, -2) : formatted;
}

function settlementLabel(value) {
  return value === "win" ? "命中" : value === "push" ? "走盘" : value === "loss" ? "未命中" : "未结算";
}

function renderPostgameReader(postgame, game) {
  const content = byId("sheet-content");
  content.replaceChildren();
  content.className = "sheet-content has-postgame";

  const rail = document.createElement("aside");
  rail.className = "postgame-reader-rail";
  const basicReview = postgame.review_level === "basic";
  rail.append(text("p", basicReview ? "赛后基础复核" : "第25秒赛后复盘", "verdict-label"));
  rail.append(text("h3", postgame.score?.text || postgame.title, "postgame-score-title"));

  const prediction = postgame.prediction || {};
  const forecastBlock = document.createElement("section");
  forecastBlock.className = "postgame-rail-block";
  forecastBlock.append(text("p", "赛前判断", "postgame-rail-label"));
  forecastBlock.append(text(
    "strong",
    (prediction.winner_name || "赛前方向") + "胜" +
      (prediction.winner_probability != null ? " · 胜率 " + percent(prediction.winner_probability) : ""),
    "postgame-rail-winner",
  ));
  if (prediction.expected_winner_margin != null && prediction.actual_winner_margin != null) {
    forecastBlock.append(text(
      "p",
      (prediction.winner_name || "预测方") + "预计赢 " +
        postgameNumber(Math.abs(prediction.expected_winner_margin)) + " 分 · " +
        (prediction.actual_winner_name || "实际胜方") + "实际赢 " +
        postgameNumber(Math.abs(prediction.actual_winner_margin)) + " 分",
    ));
  }
  if (prediction.expected_total != null && prediction.actual_total != null) {
    forecastBlock.append(text(
      "p",
      "总分预计 " + postgameNumber(prediction.expected_total) + " · 实际 " + postgameNumber(prediction.actual_total),
    ));
  }
  rail.append(forecastBlock);

  const market = postgame.market_reference || {};
  const marketBlock = document.createElement("section");
  marketBlock.className = "postgame-rail-block postgame-market-block";
  marketBlock.append(text("p", "公开早盘参考", "postgame-rail-label"));
  if (market.status === "available") {
    const homeName = market.home_team_name || game.home.name_zh;
    const awayName = market.away_team_name || game.away.name_zh;
    const lineText = market.home_spread == null ? "让分暂无"
      : homeName + " " + signedNumber(market.home_spread);
    const totalText = market.total == null ? "总分暂无" : "总分 " + postgameNumber(market.total);
    marketBlock.append(text("p", lineText + " · " + totalText));
    const spread = market.spread_review;
    if (spread) {
      const sideName = spread.lean_side === "home" ? homeName
        : spread.lean_side === "away" ? awayName : "接近盘口";
      const sideLine = spread.lean_line == null ? "" : " " + signedNumber(spread.lean_line);
      marketBlock.append(text(
        "p",
        "让分方向 " + sideName + sideLine + " · " + settlementLabel(spread.settlement),
        "postgame-settlement",
      ));
    }
    if (market.total_review) {
      const totalDirection = market.total_review.lean === "over" ? "大分"
        : market.total_review.lean === "under" ? "小分" : "走盘";
      marketBlock.append(text(
        "p",
        "总分方向 " + totalDirection + " · " + settlementLabel(market.total_review.settlement),
        "postgame-settlement",
      ));
    }
  } else {
    marketBlock.append(text("p", "这场没有可用的赛前公开盘口参考。"));
  }
  rail.append(marketBlock);
  const railReview = postgame.settlement_summary || postgame.short_review || "";
  rail.append(text("p", railReview, "postgame-short-review"));

  const article = document.createElement("article");
  article.className = "postgame-reader-article analysis-block";
  article.append(text("p", basicReview ? "基础复核 · 暂无完整分析文章" : "终场复盘", "postgame-article-kicker"));
  article.append(text("h1", postgame.title || "这场比赛怎么打成这样"));
  if (basicReview) article.append(text("p", "这里只核对赛果和赛前判断；球员表现与胜负原因等待正式复盘。"));
  String(postgame.article || "")
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean)
    .forEach((paragraph) => article.append(text("p", paragraph)));

  const sources = document.createElement("details");
  sources.className = "postgame-reader-sources analysis-block";
  sources.append(text("summary", "来源与数据"));
  (postgame.sources || []).forEach((source) => {
    if (typeof source.url !== "string" || !/^https:\/\//.test(source.url)) return;
    const row = document.createElement("div");
    row.className = "postgame-source-row";
    row.append(text("span", source.label || "来源"));
    const link = text("a", source.title || source.url);
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    row.append(link);
    if (source.captured_at) row.append(text("small", "抓取于 " + displayClock(source.captured_at)));
    sources.append(row);
  });
  if (!sources.querySelector(".postgame-source-row")) {
    sources.append(text("p", "当前没有可展开的来源链接。"));
  }

  content.append(rail, article, sources);
  content.setAttribute("aria-busy", "false");
  if (typeof readingHeader === "function") readingHeader(game, { mode: "postgame", prediction });
  byId("sheet-title").textContent = basicReview ? "赛后基础复核" : "第25秒赛后复盘";
}

function showPostgameReaderState(title, message, loading = false) {
  const content = byId("sheet-content");
  content.className = "sheet-content";
  content.replaceChildren(sheetState(title, message, loading));
  content.setAttribute("aria-busy", String(loading));
}

function setupInteractions() {
  window.addEventListener("hashchange", route);
  byId("ai-analysis").addEventListener("click", () => requestAiAnalysis(false));
  byId("postgame-entry").querySelector("button").addEventListener("click", async () => {
    const game = state.activeGame;
    if (!game?.postgame || game.postgame.status !== "available") return;
    const gameId = game.id;
    const expectedPostgameVersion = game.postgame.version_id;
    openSheet({ mode: "postgame" });
    byId("sheet-title").textContent = "第25秒赛后复盘";
    showPostgameReaderState("正在读取赛后复盘", "正在载入已保存的比赛复盘。", true);
    try {
      const response = await fetch("/api/postgame?game_id=" + encodeURIComponent(gameId), { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.status !== "available") throw new Error("postgame_unavailable");
      if (state.activeGame?.id !== gameId ||
          payload.postgame.game_id !== gameId ||
          payload.postgame.version_id !== expectedPostgameVersion) {
        throw new Error("postgame_identity_or_version_changed");
      }
      renderPostgameReader(payload.postgame, state.activeGame);
    } catch {
      if (state.activeGame?.id === gameId && !byId("ai-sheet").hidden) {
        showPostgameReaderState("赛后复盘暂不可用", "这篇复盘暂时无法读取，请稍后重试。");
        byId("sheet-title").textContent = "第25秒赛后复盘";
      }
    }
  });
  byId("close-sheet").addEventListener("click", closeSheet);
  document
    .querySelector(".sheet-backdrop")
    .addEventListener("click", closeSheet);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !byId("ai-sheet").hidden) closeSheet();
    if (event.key === "Tab" && !byId("ai-sheet").hidden) {
      const focusable = [
        ...byId("ai-sheet").querySelectorAll(
          ".analysis-sheet button:not([tabindex='-1']), .analysis-sheet summary, .analysis-sheet [href], .analysis-sheet [tabindex]:not([tabindex='-1'])",
        ),
      ].filter((node) => !node.disabled && node.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  document.addEventListener("pointerdown", (event) => {
    const card = event.target.closest(".game-card");
    if (card) card.classList.add("is-pressed");
  });
  ["pointerup", "pointercancel"].forEach((type) =>
    document.addEventListener(type, () => {
      document
        .querySelectorAll(".game-card.is-pressed")
        .forEach((card) => card.classList.remove("is-pressed"));
    }),
  );
}

async function loadCurrentAnalyses() {
  try {
    const response = await fetch("/api/current-analyses", { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    await Promise.all(Object.entries(payload.predictions || {}).map(async ([gameId, selection]) => {
      let game = state.data?.games.find((item) => item.id === gameId);
      if (!game && selection?.version && selection.version_id === selection.version.version_id) {
        // A saved report can be outside today's schedule. Reuse the read-only detail endpoint.
        try {
          game = await loadGameById(gameId);
          for (const team of [game.home, game.away]) {
            if (!state.data.teams.some((item) => item.id === team.id)) state.data.teams.push(team);
          }
        } catch (_) { return; }
      }
      if (!game) return;
      acceptPredictionSelection(gameId, selection);
    }));
  } catch (_) {
  }
}

async function fetchScheduleWindow() {
  let response = await fetch("/api/schedule-window", { cache: "no-store" });
  if (!response.ok) response = await fetch("/app_data.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`schedule_window_http_${response.status}`);
  return response.json();
}

async function refreshScheduleWindow() {
  if (!state.data || document.visibilityState === "hidden") return;
  const activeId = state.activeGame?.id || null;
  const priorActive = activeId ? state.data.games.find((game) => game.id === activeId) : null;
  try {
    const next = await fetchScheduleWindow();
    if (!Array.isArray(next.games) || !Array.isArray(next.teams) || !next.schedule_window) return;
    if (next.public_revision && next.public_revision === state.data.public_revision) return;
    if (next.public_archive && activeId) {
      const response = await fetch(`/api/game?game_id=${encodeURIComponent(activeId)}`, { cache: "no-store" });
      const detail = await response.json();
      const index = next.games.findIndex(item => item.id === activeId);
      if (response.ok && detail.game?.id === activeId && index >= 0) next.games[index] = detail.game;
    }
    for (const oldGame of state.data.games) {
      const replacement = next.games.find((game) => game.id === oldGame.id);
      if (replacement && (replacement.tipoff !== oldGame.tipoff ||
          replacement.home.id !== oldGame.home.id || replacement.away.id !== oldGame.away.id)) {
        state.currentAnalyses.delete(oldGame.id);
        state.editorialOverlays.delete(oldGame.id);
        state.predictionStates.delete(oldGame.id);
        state.factViews.delete(oldGame.id);
      }
    }
    state.data = next;
    if (activeId && !state.data.games.some((game) => game.id === activeId) && priorActive) {
      priorActive.homepage_visible = false;
      state.data.games.push(priorActive);
    }
    await loadCurrentAnalyses();
    renderHome();
    if (activeId) {
      state.activeGame = state.data.games.find((game) => game.id === activeId) || priorActive;
      if (state.activeGame) renderDetail(state.activeGame);
    } else if ((location.hash || "#home") === "#home") {
      route();
    }
  } catch (_) {
    // A failed read leaves the last successful schedule in place; no generation or market fetch is started.
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  setupInteractions();
  try {
    state.data = await fetchScheduleWindow();
    await loadCurrentAnalyses();
    setupFavoritePicker();
    renderHome();
    route();
    state.refreshTimer = window.setInterval(refreshScheduleWindow, 60 * 1000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshScheduleWindow();
    });
  } catch (error) {
    console.error(error);
    byId("home-view").hidden = true;
    byId("detail-view").hidden = true;
    byId("app-error").hidden = false;
  }
});
