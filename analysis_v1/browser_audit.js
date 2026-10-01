const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const baseUrl = process.env.COURTVISION_APP_URL || "http://127.0.0.1:8501";
const outputDir = path.join(__dirname, "output", "playwright");
fs.mkdirSync(outputDir, { recursive: true });

async function inspectPage(page, name, hash, screenshotName) {
  const response = await page.goto(`${baseUrl}/#${hash}`, { waitUntil: "networkidle" });
  await page.waitForSelector(`[data-page="${hash}"].active`);
  await page.waitForTimeout(500);
  const visiblePage = await page.locator(`[data-page="${hash}"].active`).count();
  const bodyText = await page.locator(`[data-page="${hash}"].active`).innerText();
  await page.screenshot({ path: path.join(outputDir, screenshotName), fullPage: true });
  return {
    name,
    hash,
    http_status: response ? response.status() : null,
    visible_page_count: visiblePage,
    content_characters: bodyText.length,
  };
}

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const consoleErrors = [];
  const pageErrors = [];
  const failedRequests = [];
  const desktop = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  desktop.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  desktop.on("pageerror", (error) => pageErrors.push(error.message));
  desktop.on("requestfailed", (request) => failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText || ""}`));

  const pages = [];
  pages.push(await inspectPage(desktop, "首页", "home", "01_home.png"));
  pages.push(await inspectPage(desktop, "NBA 数据总览", "overview", "02_overview.png"));
  await desktop.selectOption("#team-select", { label: "Houston Rockets" });
  const selectedTeam = await desktop.locator("#team-select").inputValue();
  const teamProfile = await desktop.locator("#team-profile").innerText();
  await desktop.screenshot({ path: path.join(outputDir, "02_overview_filtered_houston.png"), fullPage: true });
  pages.push(await inspectPage(desktop, "预测与误差分析", "predictions", "03_predictions.png"));
  pages.push(await inspectPage(desktop, "Bad Case", "bad-cases", "04_bad_cases.png"));
  await desktop.selectOption("#badcase-sort", "total");
  const sortedBadCaseText = await desktop.locator("#badcase-grid .case-card").first().innerText();
  pages.push(await inspectPage(desktop, "旗舰案例", "series", "05_series.png"));
  pages.push(await inspectPage(desktop, "方法与可信度", "methodology", "06_methodology.png"));

  const navTargets = await desktop.locator("#main-nav a").evaluateAll((links) => links.map((link) => link.getAttribute("href")));
  const limitVisible = await desktop.locator("#limit-list").isVisible();
  const backendNote = await desktop.locator("#backend-note").innerText();

  const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
  await mobile.goto(`${baseUrl}/#home`, { waitUntil: "networkidle" });
  await mobile.locator(".nav-toggle").click();
  const mobileNavVisible = await mobile.locator("#main-nav").isVisible();
  const dimensions = await mobile.evaluate(() => ({
    viewport: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    height: document.documentElement.scrollHeight,
  }));
  await mobile.screenshot({ path: path.join(outputDir, "07_mobile_home_menu.png"), fullPage: true });
  await mobile.goto(`${baseUrl}/#predictions`, { waitUntil: "networkidle" });
  const mobilePredictionsVisible = await mobile.locator('[data-page="predictions"].active').isVisible();
  await mobile.screenshot({ path: path.join(outputDir, "08_mobile_predictions.png"), fullPage: true });

  const report = {
    generated_at_utc: new Date().toISOString(),
    base_url: baseUrl,
    browser: "Microsoft Edge via bundled Playwright library",
    desktop_viewport: "1440x1000",
    mobile_viewport: "390x844",
    pages,
    interactions: {
      team_filter_value: selectedTeam,
      team_filter_changed_content: teamProfile.includes("场均得分"),
      bad_case_sort_first_card_contains_total_error: sortedBadCaseText.includes("total_absolute_error"),
      navigation_targets: navTargets,
    },
    visibility: {
      limits_visible: limitVisible,
      backend_fallback_disclosed: backendNote.includes("SQLite fallback"),
      mobile_navigation_visible_after_toggle: mobileNavVisible,
      mobile_predictions_visible: mobilePredictionsVisible,
    },
    layout: {
      mobile_document_width: dimensions.scrollWidth,
      mobile_viewport_width: dimensions.viewport,
      horizontal_overflow: dimensions.scrollWidth > dimensions.viewport,
    },
    errors: {
      console_errors: consoleErrors,
      page_errors: pageErrors,
      failed_requests: failedRequests,
    },
    result: "pending_assertions",
  };

  const passed = pages.every((item, index) => (index === 0 ? item.http_status === 200 : item.http_status === null) && item.visible_page_count === 1 && item.content_characters > 30)
    && selectedTeam === "Houston Rockets"
    && report.interactions.team_filter_changed_content
    && navTargets.length === 6
    && limitVisible
    && report.visibility.backend_fallback_disclosed
    && mobileNavVisible
    && mobilePredictionsVisible
    && !report.layout.horizontal_overflow
    && consoleErrors.length === 0
    && pageErrors.length === 0
    && failedRequests.length === 0;
  report.result = passed ? "passed" : "failed";
  fs.writeFileSync(path.join(outputDir, "browser_audit.json"), JSON.stringify(report, null, 2), "utf8");
  await browser.close();
  if (!passed) {
    console.error(JSON.stringify(report, null, 2));
    process.exit(1);
  }
  console.log(JSON.stringify(report, null, 2));
})();
