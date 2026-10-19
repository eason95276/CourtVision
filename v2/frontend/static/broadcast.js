/* Visual edition only. Consume the selected saved version; never generate a report. */
function savedStory(game) {
  const version = state.currentAnalyses.get(game.id);
  return version?.version_id ? displayedJudgment(version)?.pregame_story : null;
}

function decorateBroadcastCard(card, game) {
  const story = savedStory(game);
  const inner = card.querySelector(".card-inner");
  const heading = text("h3", story?.title ? analystCopy(story.title) : `${game.away.name_zh}对阵${game.home.name_zh}`, "card-story");
  inner.insertBefore(heading, inner.querySelector(".card-prediction"));
  if (story?.statement) {
    const summary = text("p", analystCopy(story.statement), "card-story-dek");
    inner.insertBefore(summary, inner.querySelector(".card-prediction"));
  }
  const read = card.querySelector(".card-read");
  if (read) read.textContent = hasVisibleScore(game) ? "查看比赛与复盘 ↗" : story ? "阅读本场分析 ↗" : "查看比赛资料 ↗";
  card.dataset.cover = story ? "story" : "facts";
}

function renderHomeEdition() {
  const grid = byId("games-grid");
  const cards = [...grid.children];
  cards.forEach(card => card.classList.remove("expanded-game"));
  // The existing game-day/favorite ordering selects the cover, with no hard-coded game.
  cards[0]?.classList.add("expanded-game");
  grid.classList.toggle("has-cover", cards.length > 0);
  grid.classList.toggle("single-card", cards.length === 1);
}

function updateTipoffClock(now = Date.now()) {
  const node = byId("tipoff-countdown");
  const game = state.activeGame;
  if (!node || !game) return;
  const remaining = Date.parse(game.tipoff) - now;
  if (!Number.isFinite(remaining) || hasVisibleScore(game) || ["post", "final", "completed", "finished", "cancelled", "canceled", "postponed", "delayed"].includes(game.status)) {
    node.hidden = true;
    return;
  }
  node.hidden = false;
  if (remaining <= 0) {
    node.textContent = "已到开赛时间 · 状态待同步";
    return;
  }
  // Whole minutes are rounded up so the clock never says 00:00 before tipoff.
  const minutes = Math.ceil(remaining / 60000);
  const hours = Math.floor(minutes / 60);
  const time = hours >= 24 ? `${Math.floor(hours / 24)}天 ${hours % 24}时` : `${String(hours).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
  node.replaceChildren(text("span", "距开赛"), text("strong", time));
}

function renderBroadcastDetail(game) {
  updateTipoffClock();
  const hasReport = Boolean(state.currentAnalyses.get(game.id)?.version_id);
  byId("ai-analysis").querySelector("strong").textContent = hasReport ? "阅读本场完整分析" : "查看现有比赛资料";
  byId("ai-analysis").querySelector("small").textContent = hasReport ? "观点、对位与判断依据" : "完整分析尚未发布";
  const prediction = activePrediction(game);
  const result = byId("cover-scoreline");
  const scored = hasVisibleScore(game);
  result.hidden = !scored;
  result.replaceChildren();
  if (scored) {
    const ended = ["post", "final", "completed", "finished"].includes(game.status);
    result.append(text("small", ended ? "最终比分" : "已同步比分"), text("strong", `${scoreText(game.away.score)} — ${scoreText(game.home.score)}`));
  }
  const strip = byId("hero-probability-bar");
  strip.hidden = !prediction;
  strip.replaceChildren();
  if (prediction && Number.isFinite(prediction.winner_probability)) {
    const awayProbability = prediction.winner_team_id === game.away.id ? prediction.winner_probability : 1 - prediction.winner_probability;
    const captions = document.createElement("div");
    captions.className = "probability-captions";
    captions.append(text("span", game.away.name_zh), text("span", game.home.name_zh));
    const track = document.createElement("div");
    track.className = "probability-track";
    track.style.setProperty("--away-probability", `${Math.min(100, Math.max(0, awayProbability * 100))}%`);
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", `赛前预测胜率：${game.away.name_zh}${percent(awayProbability)}，${game.home.name_zh}${percent(1 - awayProbability)}`);
    strip.append(captions, track);
  }
  if (prediction && scored) {
    byId("prediction-focus").querySelector("p").textContent = "赛前预测 · 已归档";
    byId("primary-judgment-title").textContent = "赛前判断";
  }
  updateIntelligenceLayout();
  updateCoverReading();
}

function updateIntelligenceLayout() {
  const grid = document.querySelector(".intelligence-grid");
  const injury = byId("injury-context");
  const hasAvailability = Boolean(injury.querySelector(".availability-entry"));
  injury.classList.toggle("is-light", !hasAvailability);
  grid.classList.toggle("has-light-injury", !hasAvailability);
}

function updateCoverReading() {
  const dek = byId("cover-dek");
  const toggle = byId("cover-dek-toggle");
  const gameId = state.activeGame?.id || "";
  if (!dek || !toggle) return;
  if (toggle.dataset.gameId !== gameId) {
    dek.classList.remove("is-expanded");
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "展开导读";
    toggle.dataset.gameId = gameId;
  }
  requestAnimationFrame(() => {
    const mobile = matchMedia("(max-width: 700px)").matches;
    toggle.hidden = !mobile || (!dek.classList.contains("is-expanded") && dek.scrollHeight <= dek.clientHeight + 1);
  });
}

function metricComparisonGraphic(metric, awayValue, homeValue, game) {
  const graphic = document.createElement("span");
  graphic.className = "metric-graphic";
  if (awayValue == null || homeValue == null || awayValue === "" || homeValue === "" ||
      !Number.isFinite(Number(awayValue)) || !Number.isFinite(Number(homeValue))) return graphic;
  const away = Number(awayValue), home = Number(homeValue);
  const delta = Math.abs(away - home);
  const same = teamSeasonMetricDisplay(metric, away) === teamSeasonMetricDisplay(metric, home);
  const lower = metric.preference === "lower";
  const side = (lower ? away < home : away > home) ? "away" : "home";
  const caption = same ? "数值接近" : `${game[side].name_zh}${lower ? "低" : "高"} ${number(delta)}${metric.key === "three_pct" ? " 个百分点" : ""}`;
  graphic.setAttribute("aria-label", caption);
  // Visible axis limits prevent a small difference from being presented as a large lead.
  const domains = {off_rating:[90,130],def_rating:[90,130],net_rating:[-20,20],pace:[90,110],three_pct:[25,45]};
  const baseDomain = domains[metric.key] || [Math.min(away,home,0),Math.max(away,home,1)];
  const lo = Math.min(baseDomain[0], Math.floor(Math.min(away,home)/5)*5);
  const hi = Math.max(baseDomain[1], Math.ceil(Math.max(away,home)/5)*5);
  const span = Math.max(hi-lo,1);
  const awayPosition=(away-lo)/span*100, homePosition=(home-lo)/span*100;
  const track = text("span", "", "metric-track");
  track.setAttribute("aria-hidden", "true");
  const fill = text("i", "", "metric-fill");
  fill.style.left = `${Math.min(awayPosition,homePosition)}%`;
  fill.style.width = `${Math.abs(awayPosition-homePosition)}%`;
  const awayDot=text("i","","metric-dot metric-dot-away");
  const homeDot=text("i","","metric-dot metric-dot-home");
  awayDot.style.left=`${awayPosition}%`;homeDot.style.left=`${homePosition}%`;
  track.append(fill,awayDot,homeDot);
  const axis=text("span","","metric-axis-labels");
  axis.append(text("span",String(lo)),text("span",caption,"metric-delta"),text("span",String(hi)));
  const units = ["away","home"].map(s => game.previous_season_team_stats?.teams?.[s]?.sources?.find(source=>source.metric===metric.key)?.unit);
  const unitLabels={per_100_possessions:"每百回合",possessions_per_game:"回合 / 场",percent:"%"};
  const unit = units[0] && units[0]===units[1] ? unitLabels[units[0]] : null;
  graphic.append(track,axis);
  if(unit) graphic.append(text("small",unit,"metric-unit"));
  graphic.setAttribute("aria-label", `${caption}，图示刻度${lo}至${hi}${unit ? `，单位${unit}` : ""}`);
  return graphic;
}

document.addEventListener("DOMContentLoaded", () => {
  // No polling of providers or reports; this timer only redraws local time.
  window.setInterval(() => { if (!document.hidden) updateTipoffClock(); }, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) updateTipoffClock(); });
  byId("cover-dek-toggle").addEventListener("click", () => {
    const expanded = byId("cover-dek").classList.toggle("is-expanded");
    const button = byId("cover-dek-toggle");
    button.setAttribute("aria-expanded", String(expanded));
    button.textContent = expanded ? "收起导读" : "展开导读";
  });
  let resizeFrame = null;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(updateCoverReading);
  });
});
