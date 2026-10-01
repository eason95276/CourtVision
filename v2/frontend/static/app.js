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
  predictionStates: new Map(),
  marketCache: new Map(),
  marketPending: new Map(),
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
  node.textContent = value;
  return node;
};
const number = (value, digits = 1) =>
  Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
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
  if (selection.version) {
    const game = state.data?.games.find((item) => item.id === gameId);
    if (
      !game ||
      selection.version_id !== selection.version.version_id ||
      !analysisCompatible(game, selection.version)
    ) return false;
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
  if (selection.version?.judgment?.final_prediction) {
    state.currentAnalyses.set(gameId, selection.version);
    state.aiFailures.delete(gameId);
  } else {
    state.currentAnalyses.delete(gameId);
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
  const parts = new Intl.DateTimeFormat("en-CA", {
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
  return new Intl.DateTimeFormat("zh-CN", {
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
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function statusLabel(game) {
  if (game.status === "scheduled") return "未开始";
  if (game.status === "in") return "进行中";
  if (game.status === "post") return "已结束";
  return game.status || "状态未知";
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
  image.src = logoSource(team);
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

function setupDateFilters() {
  const dates = [
    ...new Set(state.data.games.map((game) => chinaDateKey(game.tipoff))),
  ];
  state.selectedDate =
    state.selectedDate && dates.includes(state.selectedDate)
      ? state.selectedDate
      : dates[0];
  const container = byId("date-filters");
  container.replaceChildren();
  dates.forEach((date) => {
    const sample = state.data.games.find(
      (game) => chinaDateKey(game.tipoff) === date,
    );
    const button = text("button", displayDate(sample.tipoff, false));
    button.type = "button";
    button.dataset.date = date;
    button.setAttribute("aria-pressed", String(date === state.selectedDate));
    button.addEventListener("click", () => {
      state.selectedDate = date;
      setupDateFilters();
      renderGames();
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
  if (typeof player === "string") return player;
  const canonical = player?.name || "";
  return (
    (player?.display_name && player.display_name !== canonical
      ? player.display_name
      : null) ||
    PLAYER_DISPLAY_NAMES[canonical] ||
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
  if (active && predicted) {
    const copy = document.createElement("p");
    copy.append("预测：", text("strong", predicted.name_zh));
    prediction.append(copy, text("span", percent(active.winner_probability), "card-probability"));
  } else {
    prediction.append(text("p", "第25秒判断暂不可用", "prediction-unavailable"));
  }

  const insights = document.createElement("ul");
  insights.className = "card-insights";
  insights.append(
    text("li", active ? marginLabel(game) : "赛前判断尚未生成"),
    text(
      "li",
      active?.updated_at
        ? `更新于 ${displayClock(active.updated_at)}`
        : state.predictionStates.get(game.id)?.status === "locked_unavailable"
          ? "比赛已开始，未保存赛前判断"
          : game.roster_notice
          ? "近期数据包含上赛季比赛"
          : "当前最新预测",
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
    .filter((game) => chinaDateKey(game.tipoff) === state.selectedDate)
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
  const coverage = state.data.coverage;
  byId("coverage-note").textContent =
    `${coverage.games_with_saved_prediction} 场比赛已准备 · 北京时间`;
  byId("product-notice").textContent =
    "当前展示开发期赛前预测快照，预测以页面标注的数据截止时间为准。";
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

function seasonPhaseCopy(game) {
  const phase = game.season_phase?.phase;
  const labels = {
    opening_night: "下方近 15 场来自上赛季末段，可能包含季后赛",
    very_early: "下方近 15 场混合了本赛季与上赛季比赛，本季样本仍少",
    early_sample: "下方近 15 场跨越两个赛季，本季近 5 场见上方对照",
    comparison_ready: "下方近 15 场跨越两个赛季，本季近 10 场见上方对照",
    current_season: "下方为本赛季近 15 场表现",
  };
  return labels[phase] || "近期球队数据的赛季构成暂未核实";
}

function renderSeasonFormComparison(game, container) {
  const phase = game.season_phase?.phase;
  const window = phase === "early_sample" ? "current_last5" :
    phase === "comparison_ready" ? "current_last10" : null;
  const required = phase === "early_sample" ? 5 : 10;
  if (!window || game.season_form?.status !== "available") return;
  const teams = game.season_form.teams || {};
  if (["away", "home"].some((side) =>
    (teams[side]?.[window]?.games || 0) < required ||
    (teams[side]?.previous_last10?.games || 0) < 10
  )) return;
  const compare = document.createElement("div");
  compare.className = "season-form-comparison";
  compare.append(text("h3", `本赛季近 ${required} 场 · 上赛季末 10 场`));
  ["away", "home"].forEach((side) => {
    const current = teams[side][window];
    const previous = teams[side].previous_last10;
    const row = document.createElement("div");
    row.append(
      text("strong", game[side].name_zh),
      text(
        "span",
        `本季胜率 ${percent(current.win_rate)}、场均净胜 ${number(current.point_margin_per_game)} 分`,
      ),
      text(
        "span",
        `上季胜率 ${percent(previous.win_rate)}、场均净胜 ${number(previous.point_margin_per_game)} 分`,
      ),
    );
    compare.append(row);
  });
  container.append(compare);
}

function matchupMetricValue(metric, value) {
  if (value == null) return "暂不可用";
  return metric === "three_pct" ? percent(value) : number(value);
}

function renderMatchupHistory(game) {
  const section = byId("matchup-history-section");
  const container = byId("matchup-history");
  const history = game.matchup_history || {};
  const relevant = ["high", "medium"].includes(history.matchup_relevance);
  section.hidden = !relevant || history.status !== "available";
  container.replaceChildren();
  if (section.hidden) return;
  const series = history.series_summary;
  const recent = history.most_recent_meeting;
  const lead = document.createElement("div");
  lead.className = "matchup-history-lead";
  if (series) {
    const winner = teamById(game, series.winner_team_id);
    const loser = winner.id === game.home.id ? game.away : game.home;
    const averageMargin = series.team_summaries?.[String(winner.id)]?.average_margin;
    lead.append(
      text("span", "上赛季季后赛"),
      text(
        "h3",
        `${winner.name_zh} ${series.wins[String(winner.id)]} 比 ${series.wins[String(loser.id)]} ${loser.name_zh}`,
      ),
      text(
        "p",
        averageMargin == null
          ? `系列赛共 ${series.games} 场。`
          : `系列赛共 ${series.games} 场，${winner.name_zh}场均净胜 ${number(Math.abs(averageMargin))} 分。`,
      ),
    );
  } else if (recent) {
    const winner = teamById(game, recent.winner_team_id);
    const currentAwayScore =
      recent.away_team_id === game.away.id ? recent.away_score : recent.home_score;
    const currentHomeScore =
      recent.home_team_id === game.home.id ? recent.home_score : recent.away_score;
    lead.append(
      text("span", "最近一次交锋"),
      text("h3", `${winner.name_zh}取胜`),
      text("p", `${game.away.name_zh} ${currentAwayScore} · ${game.home.name_zh} ${currentHomeScore}`),
    );
  }
  container.append(lead);
  const factors = document.createElement("div");
  factors.className = "matchup-history-factors";
  (series?.deterministic_factors || []).slice(0, 3).forEach((factor) => {
    const row = document.createElement("div");
    row.append(
      text("span", factor.label),
      text(
        "strong",
        `${game.away.name_zh} ${matchupMetricValue(factor.metric, factor.away_value)} · ${game.home.name_zh} ${matchupMetricValue(factor.metric, factor.home_value)}`,
      ),
    );
    factors.append(row);
  });
  if (factors.childElementCount) container.append(factors);
}

function renderFactors(game, themes = null, status = "idle") {
  const container = byId("key-factors");
  container.replaceChildren();
  const items = themes || [];
  container.style.setProperty(
    "--factor-count",
    Math.max(1, items.length),
  );
  if (!items.length) {
    container.append(
      text(
        "p",
        status === "unavailable"
          ? "第25秒分析暂不可用"
          : "生成第25秒分析后查看本场赛前看点",
        "factor-empty",
      ),
    );
    return;
  }
  items.forEach((factor, index) => {
    const card = document.createElement("article");
    card.className = "factor-card";
    card.append(
      text("span", `0${index + 1}`),
      text("h3", analystCopy(factor.title)),
      text("p", analystCopy(factor.statement)),
    );
    container.append(card);
  });
}

function renderPregameStory(story = null, status = "idle") {
  const note = byId("baseline-note");
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
  const judgment = payload.judgment;
  renderPregameStory(judgment.pregame_story || null, "success");
  renderFactors(state.activeGame, judgment.watch_themes || [], "success");
  renderRelationship(
    state.activeGame,
    judgment.prediction_perspective || null,
    "success",
  );
  renderKeyPlayers(state.activeGame, judgment.player_analysis || [], "success");
}

function renderProductAnalysisState(game, status = "idle") {
  renderPregameStory(null, status);
  renderFactors(game, null, status);
  renderRelationship(game, null, status);
  renderKeyPlayers(game, null, status);
}

function relationshipStep(label, title, strong, description, missing = false) {
  const node = document.createElement("article");
  node.className = `relationship-step${missing ? " missing" : ""}`;
  node.append(
    text("span", label),
    text("h3", title),
    text("strong", strong),
    text("p", description),
  );
  return node;
}

function winnerForProbability(game, homeProbability) {
  const home = Number(homeProbability) >= 0.5;
  return {
    team: home ? game.home : game.away,
    probability: home ? Number(homeProbability) : 1 - Number(homeProbability),
  };
}

function renderRelationship(game, perspective = null, status = "idle") {
  const current = activePrediction(game);
  const container = byId("prediction-relationship");
  if (!current) {
    container.replaceChildren(
      relationshipStep("当前状态", "赛前判断暂不可用", "等待有效版本", "比赛已开始，且没有可锁定的赛前版本。", true),
    );
    return;
  }
  const winner = teamById(game, current.winner_team_id);
  const steps = [
    relationshipStep(
      "当前最新预测",
      `${winner.name_zh}胜`,
      `${winner.name_zh} ${percent(current.winner_probability)}`,
      `${marginLabel(game)} · 预计总分 ${number(current.expected_total)}${current.updated_at ? ` · 更新于 ${displayClock(current.updated_at)}` : ""}`,
    ),
  ];
  container.style.setProperty("--step-count", steps.length);
  container.replaceChildren(...steps);
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
    if (state.marketCache.has(game.id)) {
      const reference = state.marketCache.get(game.id);
      if (reference) renderPublicMarketReference(reference);
    } else {
      requestPublicMarketReference(game);
    }
  }
}

function renderPublicMarketReference(reference) {
  if (!reference || state.activeGame?.background?.market) return;
  const heading = reference.market_stage === "early" ? "公开早盘参考" : "公开市场参考";
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
  const current = state.currentAnalyses.get(state.activeGame.id)?.judgment;
  renderRelationship(
    state.activeGame,
    current?.prediction_perspective || null,
    state.aiFailures.has(state.activeGame.id) ? "unavailable" : "idle",
  );
}

async function requestPublicMarketReference(game) {
  if (game.background?.market || state.marketPending.has(game.id)) return;
  const card = byId("market-context");
  if (card && state.activeGame?.id === game.id) {
    card.replaceChildren(
      text("h3", "市场参考"),
      text("p", "正在查找可核验的公开早盘…", "context-missing"),
    );
  }
  const request = fetch("/api/market-reference", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ game_id: game.id }),
  })
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || "公开市场参考暂不可用");
      const reference = payload.public_market_reference || null;
      state.marketCache.set(game.id, reference);
      if (state.activeGame?.id !== game.id) return;
      if (reference) {
        renderPublicMarketReference(reference);
      } else {
        renderMarket(game);
      }
    })
    .catch(() => {
      state.marketCache.set(game.id, null);
      if (state.activeGame?.id === game.id) {
        renderMarket(game);
        const current = state.currentAnalyses.get(game.id)?.judgment;
        renderRelationship(
          game,
          current?.prediction_perspective || null,
          state.aiFailures.has(game.id) ? "unavailable" : "idle",
        );
      }
    })
    .finally(() => state.marketPending.delete(game.id));
  state.marketPending.set(game.id, request);
}

function starterRows(context) {
  if (context?.confirmed_starters?.length) {
    return { label: "确认首发", note: "", players: context.confirmed_starters };
  }
  if (context?.projected_starters?.length) {
    return {
      label: "预计首发",
      note: "非官方确认",
      players: context.projected_starters,
    };
  }
  return null;
}

function renderLineups(game) {
  const card = byId("lineup-context");
  card.replaceChildren(text("h3", "首发阵容"));
  let any = false;
  for (const side of ["away", "home"]) {
    const lineup = starterRows(game.roster_context?.teams?.[side] || {});
    if (!lineup) continue;
    any = true;
    const block = document.createElement("div");
    block.className = "lineup-team";
    block.append(
      text("strong", `${game[side].name_zh} · ${lineup.label}`),
      lineup.note ? text("small", lineup.note) : document.createTextNode(""),
      text("p", lineup.players.slice(0, 5).map(playerName).join("、")),
    );
    card.append(block);
  }
  if (!any) card.append(text("p", "暂未公布", "context-missing"));
}

const KEY_PLAYER_PRIORITY = new Map([
  ["LeBron James", 100], ["Tyrese Maxey", 95], ["Joel Embiid", 94],
  ["Jalen Brunson", 100], ["Karl-Anthony Towns", 96],
  ["Cade Cunningham", 100], ["Jalen Duren", 90],
  ["Jayson Tatum", 100], ["Jaylen Brown", 96],
  ["Shai Gilgeous-Alexander", 100], ["Jalen Williams", 94], ["Chet Holmgren", 92],
  ["Victor Wembanyama", 100], ["Giannis Antetokounmpo", 100],
  ["Trae Young", 100], ["Paolo Banchero", 100], ["Franz Wagner", 95],
  ["Anthony Edwards", 100], ["Rudy Gobert", 90],
  ["LaMelo Ball", 100], ["Brandon Miller", 92],
  ["Scottie Barnes", 100], ["Immanuel Quickley", 92],
  ["Coby White", 100], ["Bam Adebayo", 100], ["Tyler Herro", 95],
  ["De'Aaron Fox", 100], ["Mikal Bridges", 96], ["OG Anunoby", 91],
  ["Josh Hart", 88], ["Khris Middleton", 94], ["Jalen Johnson", 96],
  ["Nic Claxton", 90],
]);

function renderKeyPlayers(game, claims = null, status = "idle") {
  const container = byId("key-players");
  container.replaceChildren();
  const claimByName = new Map((claims || []).map((claim) => [claim.player_name, claim]));
  if (!claimByName.size) {
    container.append(
      text(
        "p",
        status === "unavailable"
          ? "第25秒分析暂不可用"
          : "生成第25秒分析后查看关键球员与对位",
        "factor-empty",
      ),
    );
    return;
  }
  const selected = new Map();
  for (const side of ["away", "home"]) {
    const players = [...(rosterFor(game, side)?.players || [])]
      .filter((player) => claimByName.has(player.name))
      .sort(
        (a, b) =>
          Number(claimByName.has(b.name)) - Number(claimByName.has(a.name)),
      )
      .slice(0, 3);
    selected.set(side, players);
  }
  if (![...selected.values()].some((players) => players.length)) {
    container.append(
      text("p", "当前名单已接入，关键球员将在第25秒分析中结合本场对位确定。", "factor-empty"),
    );
    return;
  }
  for (const side of ["away", "home"]) {
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
    (selected.get(side) || []).forEach((player) => {
      const claim = claimByName.get(player.name);
      const entry = document.createElement("section");
      entry.className = "player-entry";
      entry.append(
        text("h4", playerName(player)),
        text(
          "p",
          claim
            ? analystCopy(claim.statement)
            : "当前官方名单中的核心球员，具体角色仍需结合首发与本场对位判断。",
        ),
      );
      list.append(entry);
    });
    if (!list.childElementCount) {
      list.append(text("p", "当前没有形成这一侧的球员对位判断。", "factor-empty"));
    }
    group.append(list);
    container.append(group);
    if (side === "away") {
      const axis = document.createElement("div");
      axis.className = "matchup-axis";
      axis.setAttribute("aria-hidden", "true");
      axis.append(text("span", "对位"), text("strong", "VS"));
      container.append(axis);
    }
  }
}

function dynamicTeamMetric(game) {
  const home = game.background.recent_form.home;
  const away = game.background.recent_form.away;
  const candidates = [
    ["three_pct_15", "三分命中率", 0.02, percent],
    ["rebound_share_15", "篮板占比", 0.03, percent],
    ["turnover_rate_15", "失误率", 0.02, percent],
  ];
  return candidates
    .map(([key, label, threshold, formatter]) => ({
      key,
      label,
      formatter,
      strength:
        home[key] == null || away[key] == null
          ? 0
          : Math.abs(Number(home[key]) - Number(away[key])) / threshold,
    }))
    .filter((item) => item.strength >= 1)
    .sort((a, b) => b.strength - a.strength)[0];
}

function renderTeamData(game) {
  const container = byId("team-data");
  container.replaceChildren();
  renderSeasonFormComparison(game, container);
  container.append(text("p", seasonPhaseCopy(game), "season-phase-note"));
  const dynamic = dynamicTeamMetric(game);
  const awayData = game.background.recent_form.away;
  const homeData = game.background.recent_form.home;
  const head = document.createElement("header");
  for (const side of ["away", "home"]) {
    const team = document.createElement("div");
    team.className = `comparison-team comparison-team-${side}`;
    team.style.setProperty("--data-accent", game[side].accent);
    team.append(logo(game[side]), text("strong", game[side].name_zh));
    head.append(team);
  }
  container.append(head);
  const metrics = [
    ["进攻效率", "off_rating_15", number],
    ["防守效率", "def_rating_15", number],
    ["净效率", "net_rating_15", number],
    ["比赛节奏", "pace_15", number],
  ];
  if (dynamic) metrics.push([dynamic.label, dynamic.key, dynamic.formatter]);
  const rows = document.createElement("div");
  rows.className = "comparison-rows";
  metrics.forEach(([label, key, formatter]) => {
    const row = document.createElement("div");
    row.className = "comparison-row";
    row.append(
      text("strong", available(awayData[key], formatter)),
      text("span", label),
      text("strong", available(homeData[key], formatter)),
    );
    rows.append(row);
  });
  container.append(rows);
}

function pregameStatusSummary(game) {
  const schedule = game.background.schedule || {};
  const notes = [`${game.away.name_zh}客场挑战${game.home.name_zh}`];
  if (game.season_phase?.phase === "opening_night") {
    notes.push("双方都处于新赛季开局阶段");
  }
  if (schedule.away_back_to_back) notes.push(`${game.away.name_zh}背靠背作战`);
  if (schedule.home_back_to_back) notes.push(`${game.home.name_zh}背靠背作战`);
  if (
    !schedule.away_back_to_back &&
    !schedule.home_back_to_back &&
    schedule.away_rest_days != null &&
    schedule.home_rest_days != null
  ) {
    const gap = Number(schedule.away_rest_days) - Number(schedule.home_rest_days);
    if (Math.abs(gap) >= 2) {
      const rested = gap > 0 ? game.away : game.home;
      notes.push(`${rested.name_zh}拥有更长休息时间`);
    } else if (Math.abs(gap) < 0.5) {
      notes.push("双方休息时间相同");
    }
  }
  return notes.join("，");
}

function renderContext(game) {
  const injury = byId("injury-context");
  injury.replaceChildren(text("h3", "伤病与出战"));
  if (game.background.availability.status === "available") {
    injury.append(text("p", game.background.availability.message));
    game.background.availability.records.slice(0, 3).forEach((row) => {
      injury.append(
        contextStat(row.player || "球员", row.status || "状态未知"),
      );
    });
  } else {
    injury.append(text("div", "暂无可靠信息", "context-missing"));
  }

  const schedule = byId("schedule-context");
  schedule.replaceChildren(text("h3", "赛前状态"));
  const scheduleData = game.background.schedule || {};
  const statusNodes = [
    text("p", pregameStatusSummary(game), "pregame-status-summary"),
  ];
  if (scheduleData.home_back_to_back || scheduleData.away_back_to_back) {
    statusNodes.push(
      contextStat(
        `${game.home.name_zh} 背靠背`,
        scheduleData.home_back_to_back ? "是" : "否",
      ),
      contextStat(
        `${game.away.name_zh} 背靠背`,
        scheduleData.away_back_to_back ? "是" : "否",
      ),
    );
  } else if (
    scheduleData.home_rest_days != null &&
    scheduleData.away_rest_days != null &&
    Math.abs(
      Number(scheduleData.home_rest_days) - Number(scheduleData.away_rest_days),
    ) >= 2
  ) {
    statusNodes.push(
      contextStat(
        `${game.home.name_zh} 休息`,
        `${number(scheduleData.home_rest_days, 0)} 天`,
      ),
      contextStat(
        `${game.away.name_zh} 休息`,
        `${number(scheduleData.away_rest_days, 0)} 天`,
      ),
    );
  }
  schedule.append(...statusNodes);
  renderMarket(game);
  renderLineups(game);
  renderKeyPlayers(game);
  renderTeamData(game);
}

function renderTechnical(game) {
  const container = byId("technical-content");
  container.replaceChildren();
  const rows = [
    ["数据身份", game.id],
    ["预测状态", game.technical.source_label],
    ["数据截止", game.technical.cutoff || "未记录"],
    ["阵容与窗口说明截止", game.technical.roster_context_cutoff || "未记录"],
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

function renderRoster(game) {
  const container = byId("roster-context");
  container.replaceChildren();
  byId("roster-notice").textContent =
    "当前名单来自球队官方页面，完整名单默认折叠。";
  for (const side of ["away", "home"]) {
    const team = game[side],
      context = game.roster_context?.teams?.[side] || {};
    const roster = context.official_team_roster,
      continuity = context.continuity || {},
      cold = context.cold_start || {};
    const card = document.createElement("article");
    card.className = "context-card";
    const head = document.createElement("div");
    head.className = "roster-head";
    head.append(
      logo(team),
      text("h3", team.name_zh),
      text(
        "small",
        roster?.players ? `${roster.players.length} 名成员` : "名单待补充",
      ),
    );
    card.append(head);
    if (roster?.status === "available") {
      const details = document.createElement("details");
      const list = document.createElement("ul");
      list.className = "roster-list";
      const positions = {
        Guard: "后卫",
        Forward: "前锋",
        Center: "中锋",
        "Guard-Forward": "后卫 / 前锋",
        "Forward-Guard": "前锋 / 后卫",
        "Forward-Center": "前锋 / 中锋",
        "Center-Forward": "中锋 / 前锋",
        G: "后卫",
        F: "前锋",
        C: "中锋",
      };
      roster.players.forEach((player) => {
        const row = text("li", playerName(player));
        if (positions[player.position])
          row.append(text("small", positions[player.position]));
        list.append(row);
      });
      details.append(text("summary", "查看官方当前名单"), list);
      card.append(details);
      if (roster.source?.captured_at)
        card.append(
          text(
            "p",
            `名单来源：NBA 官方 · ${displayDate(roster.source.captured_at, false)}抓取`,
            "roster-source",
          ),
        );
    } else card.append(text("p", "暂无符合时间要求的官方名单。"));
    if (cold.status === "available") {
      for (const window of [5, 15]) {
        const current = cold[`rolling_${window}_current_season_games`];
        const previous = cold[`rolling_${window}_previous_season_games`];
        const older = cold[`rolling_${window}_older_season_games`] || 0;
        const row = document.createElement("div");
        row.className = "window-row";
        const labels = document.createElement("div");
        labels.append(
          text("span", `近 ${window} 场数据`),
          text(
            "span",
            `本季 ${current ?? "未知"} · 上季 ${previous ?? "未知"}${older ? ` · 更早 ${older}` : ""}`,
          ),
        );
        const track = document.createElement("div");
        track.className = "window-track";
        track.setAttribute("aria-hidden", "true");
        const fill = document.createElement("i");
        fill.style.width = `${(Number(previous || 0) / window) * 100}%`;
        track.append(fill);
        row.append(labels, track);
        card.append(row);
      }
    }
    if (continuity.status === "available") {
      card.append(
        contextStat(
          "上赛季出场分钟保留",
          available(continuity.retained_minutes_ratio, percent),
        ),
      );
      const details = document.createElement("details");
      details.append(
        text("summary", "阵容连续性与名单差异"),
        contextStat(
          "上赛季分钟前八名保留",
          `${continuity.top8_retained_count} / ${continuity.top8_count}`,
        ),
        contextStat(
          "前八名保留分钟比例",
          available(continuity.top8_retained_minutes_ratio, percent),
        ),
        text(
          "p",
          `当前列入、上赛季未在该队出场：${(continuity.newly_listed_players || []).map((p) => p.name).join("、") || "无"}`,
        ),
        text(
          "p",
          `上赛季出场、当前未列入：${(continuity.previous_participants_not_listed || []).map((p) => p.name).join("、") || "无"}`,
        ),
      );
      card.append(details);
    } else
      card.append(
        text(
          "p",
          "精确的上赛季球员分钟仍不完整，暂时无法量化阵容保留程度。",
          "roster-source",
        ),
      );
    container.append(card);
  }
}
function renderPredictionDisplay(game) {
  const current = activePrediction(game);
  if (!current) {
    byId("predicted-winner").textContent = "暂不可用";
    byId("predicted-probability").textContent = "—";
    byId("probability-lens").style.setProperty("--probability", "0%");
    byId("expected-margin").textContent = "暂不可用";
    byId("expected-total").textContent = "暂不可用";
    byId("prediction-updated").hidden = true;
    renderRelationship(game);
    return;
  }
  const predicted = teamById(game, current.winner_team_id);
  byId("predicted-winner").textContent = predicted.name_zh;
  byId("predicted-probability").textContent = percent(current.winner_probability);
  byId("probability-lens").style.setProperty(
    "--probability",
    `${current.winner_probability * 100}%`,
  );
  byId("expected-margin").textContent = marginLabel(game);
  byId("expected-total").textContent = number(current.expected_total);
  const updated = byId("prediction-updated");
  updated.hidden = !current.updated_at;
  updated.textContent = current.updated_at
    ? `更新于 ${displayClock(current.updated_at)}`
    : "";
  renderRelationship(game);
}

function renderDetail(game) {
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
  byId("detail-status").textContent = statusLabel(game);
  byId("away-team").replaceChildren(stageTeam(game.away));
  byId("home-team").replaceChildren(stageTeam(game.home));
  renderPredictionDisplay(game);
  renderContext(game);
  renderMatchupHistory(game);
  renderRoster(game);
  renderTechnical(game);
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
  byId("ai-analysis").disabled = locked && !cachedAnalysis;
  byId("ai-analysis").querySelector("strong").textContent =
    cachedAnalysis
    ? "继续阅读第25秒分析"
    : locked ? "比赛已开始" : "阅读第25秒分析";
  const postgame = byId("postgame-entry");
  const postButton = postgame.querySelector("button");
  const availablePostgame = game.postgame?.status === "available";
  postButton.disabled = !availablePostgame;
  postButton.textContent = availablePostgame ? "查看赛后复盘" : "等待比赛结束";
  byId("postgame-copy").textContent = availablePostgame
    ? "回看赛前核心判断，并与真实比赛结果逐项验证。"
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
    if (game) {
      if (!state.activeGame) state.homeScroll = window.scrollY;
      showView("detail");
      renderDetail(game);
      window.scrollTo({ top: 0, behavior: "auto" });
      byId("back-home").focus({ preventScroll: true });
      return;
    }
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

function sheetState(title, message, loading = false) {
  const node = document.createElement("div");
  node.className = "sheet-state";
  if (loading) node.append(text("span", "", "loader"));
  node.append(text("div", ""));
  node.lastElementChild.append(text("h3", title), text("p", message));
  return node;
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
  let result = String(value || "");
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
  renderKeyPlayers(state.activeGame, judgment.player_analysis);
}

function renderJudgment(payload) {
  const judgment = payload.judgment;
  renderProductAnalysis(payload);
  renderPredictionDisplay(state.activeGame);
  renderGames();
  const current = activePrediction(state.activeGame);
  const winner = teamById(state.activeGame, current.winner_team_id);
  const container = byId("sheet-content");
  const verdict = document.createElement("section");
  verdict.className = "analyst-verdict";
  verdict.append(
    text("p", "第25秒最终预测", "verdict-label"),
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
    locked ? "赛前判断已锁定" : "更新当前预测",
    "analysis-refresh-button",
  );
  refresh.type = "button";
  refresh.disabled = locked;
  if (!locked) refresh.addEventListener("click", () => requestAiAnalysis(true));
  verdict.append(refresh);
  container.replaceChildren(verdict);
  const thesis = document.createElement("section");
  thesis.className = "analysis-thesis";
  thesis.append(
    text("p", "赛前短评", "verdict-label"),
    text(
      "h3",
      analystCopy(
        judgment.pregame_story?.title ||
          judgment.core_question ||
          `${state.activeGame.away.name_zh}对${state.activeGame.home.name_zh}`,
      ),
    ),
  );
  container.append(thesis);
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
  renderKeyPlayers(state.activeGame, judgment.player_analysis || []);
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
  const selection = state.predictionStates.get(gameId);
  const locked = ["locked", "locked_unavailable", "schedule_changed"].includes(selection?.status);
  if (locked) {
    if (cached) renderJudgment(cached);
    else renderOfflineAnalysis(state.activeGame, "比赛已开始，当前没有可读取的赛前判断。");
    return;
  }
  if (cached && !forceRefresh) {
    renderJudgment(cached);
    return;
  }
  if (cached) {
    renderJudgment(cached);
    content.prepend(text("p", "正在更新赛前信息…", "analysis-refresh-note"));
  } else {
    content.replaceChildren(
      sheetState(
        "正在整理证据",
        "分析师正在结合球队表现、当前名单与预测形成判断。你可以先关闭面板，继续看比赛。",
        true,
      ),
    );
  }
  content.setAttribute("aria-busy", "true");
  const button = byId("ai-analysis");
  state.aiPending = true;
  state.aiFailures.delete(gameId);
  button.disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 90000);
  try {
    const response = await fetch("/api/ai-analysis", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game_id: gameId, request_id: crypto.randomUUID() }),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (payload.game_id === gameId && Number.isFinite(Number(payload.state_revision))) {
      acceptPredictionSelection(gameId, payload);
    }
    const selected = state.currentAnalyses.get(gameId);
    if (payload.update_status === "failed") {
      if (selected) {
        renderJudgment(selected);
        content.prepend(text("p", "本次更新未完成，仍显示上一份成功判断。", "analysis-refresh-note"));
        return;
      }
      throw new Error(payload.message || "AI 分析暂不可用");
    }
    if (!response.ok || !selected)
      throw new Error(payload.message || "AI 分析暂不可用");
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
        if (!lockedNow) content.prepend(text("p", "本次更新未完成，仍显示上一份成功判断。", "analysis-refresh-note"));
        return;
      }
      state.aiFailures.add(gameId);
      if (lockedNow) {
        renderOfflineAnalysis(state.activeGame, "比赛已开始，当前没有可读取的赛前判断。");
        return;
      }
      const message =
        error.name === "AbortError"
          ? "这次生成用时较长，请稍后重试。"
          : /Error|Exception|failed|fetch|JSON/i.test(error.message)
            ? "本次分析未能完成，请稍后重试。"
            : error.message;
      renderOfflineAnalysis(state.activeGame, message);
      const retry = text("button", "重新尝试", "retry-button");
      retry.type = "button";
      retry.addEventListener("click", () => requestAiAnalysis(false));
      content.append(retry);
    }
  } finally {
    clearTimeout(timeout);
    state.aiPending = false;
    content.setAttribute("aria-busy", "false");
    button.disabled = ["locked", "locked_unavailable", "schedule_changed"].includes(
      state.predictionStates.get(gameId)?.status,
    ) && !state.currentAnalyses.has(gameId);
  }
}
function setupInteractions() {
  window.addEventListener("hashchange", route);
  byId("ai-analysis").addEventListener("click", () => requestAiAnalysis(false));
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
    Object.entries(payload.predictions || {}).forEach(([gameId, selection]) => {
      const game = state.data?.games.find((item) => item.id === gameId);
      if (!game) return;
      acceptPredictionSelection(gameId, selection);
    });
  } catch (_) {
    // The static demo remains usable without the local API server.
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  setupInteractions();
  try {
    const response = await fetch("app_data.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    await loadCurrentAnalyses();
    setupFavoritePicker();
    renderHome();
    route();
  } catch (error) {
    console.error(error);
    byId("home-view").hidden = true;
    byId("detail-view").hidden = true;
    byId("app-error").hidden = false;
  }
});
