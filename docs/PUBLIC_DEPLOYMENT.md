# 发布

网站入口是 <https://nba.weitao.si/>，源码在 [eason95276/CourtVision](https://github.com/eason95276/CourtVision)。

报告在本地生成，公开站通过 GitHub Pages 展示导出的结果。Pages 不运行 Agent、本地 API、数据库、Scheduler 或评测工作台。

## 明日预测名单

README 顶部的名单由 `config/daily_picks.json` 中人工选定的场次生成，日期统一为北京时间。每天计划于 12:10 运行，也会在选场文件或公开比赛记录变化后运行；GitHub 的定时任务可能延迟，不能作为赛前最终版的定时器。

每天选好比赛后，在对应北京时间日期下记录比赛ID和本轮更新状态。未配置的日期显示尚未选定，不自动沿用昨天的名单。板块同时标注现有正式报告的资料截止时间；刷新名单不代表刷新伤病、盘口或文章。

`scripts/update_daily_board.py` 只改 README 的标记区，不调用外部数据源或模型，不发布预测。`daily-board.yml` 只提交这部分 README 更新，不能替代本地 Research、生成与人工验收。它没有访问本地数据库或生成密钥的权限。

## 预览页面

在公开仓库根目录执行：

```sh
python scripts/build_public_site.py
python -m http.server 8000 --directory _site
```

打开 `http://localhost:8000`。比赛详情使用 `#game/...` 路由，刷新时仍由同一静态入口加载。

## 导出一场比赛

先在本地完成正式版本发布，再从公开仓库运行：

```sh
python publish_game.py espn:GAME_ID --api-base http://127.0.0.1:8625
```

默认写入当前仓库的 `public_data/`，也可用 `--destination` 指定目录。本地 API 只在导出时需要；导出读取已有版本，不调用生成接口，也不回写原 Prediction／Postgame Version。

提交前检查本次数据和页面：

```sh
python -m unittest discover -s tests/public -v
python scripts/check_public_release.py --history
python scripts/build_public_site.py
```

公开站测试中的 JavaScript 回归需要 Node.js。另检查电脑和手机页面、文章与复盘入口，以及浏览器控制台。

核对差异后，只提交这次导出的数据：

```sh
git diff -- public_data
git add public_data
git commit -m "Publish saved game analysis"
git push origin main
```

新增比赛不需要重新打包整个私有工程。代码改动单独审查，不把私有目录、数据库、密钥或原始响应一起提交。

## 部署流程

`main` 上的提交触发 [Pages 工作流](../.github/workflows/pages.yml)：

```text
公开文件与历史检查 → 测试 → 构建 _site → 上传 → 部署
```

检查失败时不会部署新页面，线上保留上一版。推送成功和网站更新是两个步骤：等 Actions 完成，再打开正式域名核对新页面、比赛详情与字体资源。

## 域名

产品使用子域名 `nba.weitao.si`，旧文档中的根域名 `weitao.si` 配置不作为当前产品配置。

如需调整域名，在仓库的 Settings → Pages 中核对 Custom domain 与 HTTPS 状态，再按 [GitHub 官方说明](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site)处理 DNS。域名账户、验证值和登录信息不放进仓库。

完整检查项见[发布清单](PUBLIC_RELEASE_CHECKLIST.md)。
