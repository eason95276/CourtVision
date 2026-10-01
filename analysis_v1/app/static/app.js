const state = { data: null, page: "home", selectedSeason: null, selectedTeam: null };

const formatNumber = (value, digits = 0) => Number(value).toLocaleString("zh-CN", {
  minimumFractionDigits: digits,
  maximumFractionDigits: digits,
});
const pct = (value, digits = 1) => `${formatNumber(Number(value) * 100, digits)}%`;
const byId = (id) => document.getElementById(id);

function route() {
  const requested = location.hash.replace("#", "") || "home";
  const target = document.querySelector(`[data-page="${requested}"]`) ? requested : "home";
  state.page = target;
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("active", page.dataset.page === target));
  document.querySelectorAll("nav a").forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${target}`));
  byId("main-nav").classList.remove("open");
  byId("main-nav").previousElementSibling?.setAttribute("aria-expanded", "false");
  window.scrollTo({ top: 0, behavior: "instant" });
}

function metric(label, value, note = "") {
  return `<div class="metric"><span>${label}</span><strong>${value}</strong>${note ? `<small>${note}</small>` : ""}</div>`;
}

function renderHeader() {
  const db = state.data.database;
  byId("hero-games").textContent = formatNumber(db.table_rows.dim_game);
  byId("hero-seasons").textContent = formatNumber(db.table_rows.dim_season);
  byId("hero-predictions").textContent = formatNumber(db.table_rows.fact_prediction);
}

function setupOverviewFilters() {
  const seasonSelect = byId("season-select");
  seasonSelect.innerHTML = state.data.seasons.map((row) => `<option value="${row.season_label}">${row.season_label}</option>`).join("");
  state.selectedSeason = state.data.seasons.at(-1).season_label;
  seasonSelect.value = state.selectedSeason;
  seasonSelect.addEventListener("change", () => {
    state.selectedSeason = seasonSelect.value;
    renderOverview();
  });

  const teamSelect = byId("team-select");
  const teams = [...state.data.team_profiles].sort((a, b) => a.team_name.localeCompare(b.team_name));
  teamSelect.innerHTML = teams.map((row) => `<option value="${row.team_name}">${row.team_name}</option>`).join("");
  state.selectedTeam = teams.find((row) => row.team_name === "Los Angeles Lakers")?.team_name || teams[0].team_name;
  teamSelect.value = state.selectedTeam;
  teamSelect.addEventListener("change", () => {
    state.selectedTeam = teamSelect.value;
    renderOverview();
  });
}

function renderOverview() {
  const season = state.data.seasons.find((row) => row.season_label === state.selectedSeason);
  const team = state.data.team_profiles.find((row) => row.team_name === state.selectedTeam);
  const homeAway = state.data.home_away.find((row) => row.team_name === state.selectedTeam);
  const recent = state.data.recent_form.find((row) => row.team_name === state.selectedTeam);
  byId("overview-metrics").innerHTML = [
    metric("赛季比赛", formatNumber(season.games), state.selectedSeason),
    metric("季后赛或附加赛", formatNumber(season.playoff_or_play_in_games), pct(season.playoff_share)),
    metric("球队历史样本", formatNumber(team.games), state.selectedTeam),
    metric("历史胜率", pct(team.win_rate), "描述性聚合"),
  ].join("");

  const maxGames = Math.max(...state.data.seasons.map((row) => row.games));
  byId("season-chart").innerHTML = state.data.seasons.map((row) => `
    <div class="bar-item ${row.season_label === state.selectedSeason ? "" : "dim"}" title="${row.season_label}: ${row.games} 场">
      <div class="bar" style="height:${Math.max(5, row.games / maxGames * 100)}%"><span>${formatNumber(row.games)}</span></div>
      <label>${row.season_label}</label>
    </div>`).join("");

  byId("team-context").textContent = `${team.games} 场历史样本`;
  byId("team-profile").innerHTML = `
    <div class="profile-score">
      <div><span>场均得分</span><strong>${formatNumber(team.avg_points_for, 1)}</strong></div>
      <div><span>场均失分</span><strong>${formatNumber(team.avg_points_against, 1)}</strong></div>
    </div>
    <div class="stat-line"><span>场均净胜分</span><strong>${formatNumber(team.avg_point_margin, 2)}</strong></div>
    <div class="stat-line"><span>主场胜率</span><strong>${pct(homeAway.home_win_rate)}</strong></div>
    <div class="stat-line"><span>客场胜率</span><strong>${pct(homeAway.away_win_rate)}</strong></div>
    <div class="stat-line"><span>主客场差</span><strong>${pct(homeAway.home_away_gap)}</strong></div>`;

  byId("recent-form").innerHTML = recent ? `
    <p class="big-stat">${pct(recent.win_rate_last10, 0)} <small>最近 10 场胜率</small></p>
    <div class="stat-line"><span>最近 5 场胜率</span><strong>${pct(recent.win_rate_last5, 0)}</strong></div>
    <div class="stat-line"><span>最近 5 场净胜分</span><strong>${formatNumber(recent.margin_last5, 1)}</strong></div>
    <div class="stat-line"><span>最近 10 场净胜分</span><strong>${formatNumber(recent.margin_last10, 1)}</strong></div>
    <div class="stat-line"><span>窗口结束时间</span><strong>${recent.game_date.slice(0, 10)}</strong></div>` : "<p>没有成熟滚动窗口。</p>";
}

function renderPredictions() {
  const model = state.data.model_summary[0];
  const market = state.data.market_summary[0];
  const marginQ4 = state.data.margin_error.at(-1);
  const totalQ4 = state.data.total_error.at(-1);
  byId("model-metrics").innerHTML = [
    metric("测试样本", formatNumber(model.games), "2025 赛季"),
    metric("胜负准确率", pct(model.accuracy), `Log Loss ${formatNumber(model.log_loss, 4)}`),
    metric("分差 MAE", formatNumber(market.model_margin_mae, 2), "分"),
    metric("总分 MAE", formatNumber(market.model_total_mae, 2), "分"),
  ].join("");

  byId("confidence-chart").innerHTML = state.data.confidence.map((row) => {
    const predictedHeight = Math.max(8, row.mean_confidence * 100);
    const actualHeight = Math.max(8, row.actual_accuracy * 100);
    return `<div class="confidence-item">
      <i style="height:${predictedHeight}%"><span>${pct(row.mean_confidence, 0)}</span></i>
      <i class="actual" style="height:${actualHeight}%"><span>${pct(row.actual_accuracy, 0)}</span></i>
      <label>${row.confidence_band}<br>${row.games} 场</label>
    </div>`;
  }).join("");

  byId("margin-error").innerHTML = `
    <p class="big-stat">${formatNumber(market.model_margin_mae, 2)} <small>平均绝对误差</small></p>
    <div class="stat-line"><span>最高四分位均值</span><strong>${formatNumber(marginQ4.mean_error, 2)}</strong></div>
    <div class="stat-line"><span>最高观察误差</span><strong>${formatNumber(marginQ4.max_error, 2)}</strong></div>
    <div class="stat-line"><span>20 分以上占比</span><strong>${pct(marginQ4.share_error_20_plus)}</strong></div>`;
  byId("total-error").innerHTML = `
    <p class="big-stat">${formatNumber(market.model_total_mae, 2)} <small>平均绝对误差</small></p>
    <div class="stat-line"><span>最高四分位均值</span><strong>${formatNumber(totalQ4.mean_error, 2)}</strong></div>
    <div class="stat-line"><span>最高观察误差</span><strong>${formatNumber(totalQ4.max_error, 2)}</strong></div>
    <div class="stat-line"><span>25 分以上占比</span><strong>${pct(totalQ4.share_error_25_plus)}</strong></div>`;

  const stabilityRows = state.data.stability.map((row) => `<tr><td>${row.evaluation_season}</td><td>${row.task}</td><td>${row.n_eval}</td><td>${row.log_loss ?? ""}</td><td>${row.accuracy ?? ""}</td><td>${row.mae ?? ""}</td></tr>`).join("");
  byId("stability-table").innerHTML = `<table><thead><tr><th>赛季</th><th>任务</th><th>样本</th><th>Log Loss</th><th>Accuracy</th><th>MAE</th></tr></thead><tbody>${stabilityRows}</tbody></table>`;
  byId("market-card").innerHTML = `
    <div class="comparison"><div><span>模型分差 MAE</span><strong>${formatNumber(market.model_margin_mae, 2)}</strong></div><div><span>市场分差 MAE</span><strong>${formatNumber(market.market_margin_mae, 2)}</strong></div></div>
    <div class="comparison" style="margin-top:.75rem"><div><span>模型总分 MAE</span><strong>${formatNumber(market.model_total_mae, 2)}</strong></div><div><span>市场总分 MAE</span><strong>${formatNumber(market.market_total_mae, 2)}</strong></div></div>
    <p style="color:var(--muted);font-size:.78rem">模型在该同样本中分差多 ${formatNumber(market.model_minus_market_margin_mae, 2)} 分、总分多 ${formatNumber(market.model_minus_market_total_mae, 2)} 分。市场线时间身份未验证。</p>`;
}

function renderBadCases() {
  const sortKey = byId("badcase-sort").value;
  const mapping = { margin: "margin_absolute_error", total: "total_absolute_error", win: "win_log_loss" };
  const key = mapping[sortKey];
  const rows = [...state.data.worst_games].sort((a, b) => Number(b[key]) - Number(a[key])).slice(0, 12);
  byId("badcase-grid").innerHTML = rows.map((row, index) => `
    <article class="case-card">
      <header><span>REVIEW ${String(index + 1).padStart(2, "0")}</span><span>${row.game_date.slice(0, 10)}</span></header>
      <h3>${row.away_team}<br>@ ${row.home_team}</h3>
      <span class="error-tag">${key} ${formatNumber(row[key], 2)}</span>
      <div class="case-score">
        <div><span>预测分差</span><strong>${formatNumber(row.predicted_margin, 1)}</strong></div>
        <div><span>实际分差</span><strong>${formatNumber(row.point_margin, 0)}</strong></div>
        <div><span>预测总分</span><strong>${formatNumber(row.predicted_total, 1)}</strong></div>
        <div><span>实际总分</span><strong>${formatNumber(row.total_points, 0)}</strong></div>
      </div>
      <div class="stat-line"><span>主队胜率预测</span><strong>${pct(row.win_probability)}</strong></div>
    </article>`).join("");
}

function renderSeries() {
  const series = state.data.flagship_series;
  if (!series || series.status === "research_pending") {
    byId("series-content").innerHTML = `<div class="series-status warning"><h3>${series?.title || "证据包待核验"}</h3><p>本地页面不会把任务书中的未核验叙事直接展示成事实。</p></div>`;
    return;
  }
  const games = (series.games || []).map((game) => `<article><h3>${game.label}: ${game.away_team} ${game.away_score} / ${game.home_team} ${game.home_score}</h3><p>${game.date} · ${game.venue_side || "主客场见比赛记录"} · 系列赛 ${game.series_state_after}</p><p>${game.verified_highlights || ""}</p></article>`).join("");
  byId("series-content").innerHTML = `
    <div class="series-summary"><div><p class="eyebrow">${series.fact_status || "VERIFIED LOCAL DATA"}</p><h3>${series.title}</h3><p>${series.scope_note || ""}</p></div><strong>${series.final_series_score || ""}</strong></div>
    <div class="timeline">${games}</div>
    <article class="panel warning-panel"><div class="panel-head"><h3>仍未确认</h3><span>不得写进成片事实</span></div><ul>${(series.unverified || []).map((item) => `<li>${item}</li>`).join("")}</ul></article>`;
}

function renderMethodology() {
  byId("quality-list").innerHTML = state.data.quality_warnings.map((row) => `<div class="quality-item"><strong>${row.check_name}</strong><p>${row.detail}</p></div>`).join("");
  byId("limit-list").innerHTML = state.data.limits.map((item) => `<li>${item}</li>`).join("");
  byId("backend-note").textContent = `${state.data.product.database_backend}。${state.data.product.duckdb_status}`;
}

function renderAll() {
  renderHeader();
  setupOverviewFilters();
  renderOverview();
  renderPredictions();
  renderBadCases();
  renderSeries();
  renderMethodology();
}

document.addEventListener("DOMContentLoaded", async () => {
  window.addEventListener("hashchange", route);
  byId("badcase-sort").addEventListener("change", renderBadCases);
  document.querySelector(".nav-toggle").addEventListener("click", (event) => {
    const nav = byId("main-nav");
    nav.classList.toggle("open");
    event.currentTarget.setAttribute("aria-expanded", String(nav.classList.contains("open")));
  });
  try {
    const response = await fetch("app_data.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    renderAll();
  } catch (error) {
    const banner = byId("error-banner");
    banner.hidden = false;
    banner.textContent = `应用数据未加载：${error.message}`;
  }
  route();
});
