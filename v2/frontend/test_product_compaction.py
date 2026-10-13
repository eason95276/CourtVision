from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from .export_data import STATIC_ROOT


BROWSER_CHECK = r"""
const fs = require('node:fs');
const { chromium } = require(process.argv[1]);
const config = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const results = [];
  try {
    for (const viewport of [{ width: 1366, height: 768 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport, timezoneId: 'America/Los_Angeles', reducedMotion: 'reduce' });
      let aiPosts = 0, marketReads = 0, externalAssets = 0, currentAnalysisReads = 0;
      await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {
          externalAssets++;
          if (request.resourceType() === 'image') {
            return route.fulfill({ status: 200, contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg"/>' });
          }
          if (request.resourceType() === 'stylesheet' || request.resourceType() === 'font') {
            return route.fulfill({ status: 200, contentType: 'text/css', body: '' });
          }
          return route.abort();
        }
        if (url.pathname === '/app_data.json') {
          return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(config.data) });
        }
        if (url.pathname === '/api/schedule-window') {
          return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(config.data) });
        }
        if (url.pathname === '/api/current-analyses') {
          currentAnalysisReads++;
          if (currentAnalysisReads > 1) await new Promise(resolve => setTimeout(resolve, 700));
          return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(config.response) });
        }
        if (url.pathname === '/api/market-reference') {
          marketReads++;
          return route.fulfill({ status: 200, contentType: 'application/json', body: '{"status":"available","public_market_reference":null}' });
        }
        if (url.pathname === '/api/ai-analysis' && request.method() === 'POST') {
          aiPosts++;
          return route.fulfill({ status: 503, contentType: 'application/json', body: '{"status":"unexpected_generation"}' });
        }
        return route.continue();
      });
      const page = await context.newPage(), pageErrors = [], consoleErrors = [];
      page.on('pageerror', error => pageErrors.push(error.message));
      page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
      await page.goto(config.url + '/#game/' + encodeURIComponent(config.gameId), { waitUntil: 'domcontentloaded' });
      try {
        await page.waitForFunction(id => state.activeGame?.id === id && state.currentAnalyses.has(id), config.gameId, { timeout: 7000 });
      } catch (error) {
        console.error(JSON.stringify(await page.evaluate(({ id, version }) => {
          const game = state.data?.games.find(item => item.id === id);
          return { loaded: Boolean(state.data), gameId: game?.id, activeGameId: state.activeGame?.id,
            apiVersion: version?.version_id, compatible: game ? analysisCompatible(game, version) : null,
            appError: document.querySelector('#app-error')?.textContent };
        }, { id: config.gameId, version: config.response.predictions[config.gameId].version })));
        throw error;
      }
      const initial = await page.evaluate(id => {
        const selected = state.currentAnalyses.get(id);
        const rows = [...document.querySelectorAll('.comparison-row')].map(row => ({
          label: row.children[1]?.textContent,
          awaySignal: row.children[0]?.querySelector('small')?.textContent || '',
          homeSignal: row.children[2]?.querySelector('small')?.textContent || '',
          awayValue: row.children[0]?.querySelector('strong')?.textContent || '',
          homeValue: row.children[2]?.querySelector('strong')?.textContent || '',
        }));
        return {
          versionId: selected.version_id,
          article: selected.judgment.analysis_article,
          activePrediction: activePrediction(state.activeGame),
          predictionLabel: document.querySelector('.prediction-focus > p').textContent,
          probability: document.querySelector('#predicted-probability').textContent,
          margin: document.querySelector('#expected-margin').textContent,
          marginTitle: document.querySelector('.primary-numbers > div:first-child > span').textContent,
          total: document.querySelector('#expected-total').textContent,
          totalTitle: document.querySelector('.primary-numbers > div:last-child > span').textContent,
          judgmentTitle: document.querySelector('#primary-judgment-title').textContent,
          awayWinnerMargin: (() => {
            const awayVersion = { ...selected, judgment: { ...selected.judgment,
              final_prediction: { ...selected.judgment.final_prediction, winner_side: 'away', expected_margin: 3.5 } } };
            state.currentAnalyses.set(id, awayVersion);
            renderPredictionDisplay(state.activeGame);
            const label = document.querySelector('#expected-margin').textContent;
            state.currentAnalyses.set(id, selected);
            renderDetail(state.data.games.find(game => game.id === id));
            return label;
          })(),
          displayTitleOverride: (() => {
            const titlePayload = { ...selected, game_id: 'espn:401898390',
              version_id: 'ffc2947f-ae38-424c-894f-c2f05d71c3d6',
              judgment: { ...selected.judgment, pregame_story: {
                title: '勇士主场迎战湖人，真正的变量是库里们能打多久', statement: 'fixture' } } };
            const title = displayedStoryTitle(titlePayload);
            renderPregameStory({ ...titlePayload.judgment.pregame_story, title }, 'success');
            const rendered = document.querySelector('#baseline-note strong').textContent;
            const frozenTitle = titlePayload.judgment.pregame_story.title;
            renderProductAnalysis(selected);
            return { rendered, frozenTitle };
          })(),
          lineupText: document.querySelector('#lineup-context').textContent,
          lineupTitle: document.querySelector('#lineup-context h3').textContent,
          injuryTitle: document.querySelector('#injury-context h3').textContent,
          injuryText: document.querySelector('#injury-context').textContent,
          recentStatusTitle: document.querySelector('#schedule-context h3').textContent,
          recentStatusText: document.querySelector('#schedule-context').textContent,
          intelligenceEyebrow: document.querySelector('#detail-view .intelligence-section .section-title p')?.textContent || null,
          payloadHashBeforeContextRender: selected.payload_sha256,
          payloadHashAfterContextRender: state.currentAnalyses.get(id)?.payload_sha256,
          frozenPlayerCopy: [
            displayPlayerAnalysisStatement({version_id:'ffc2947f-ae38-424c-894f-c2f05d71c3d6'},
              {name:'Stephen Curry'},{statement:'勇士进攻的绝对核心，只要在场就能撑起传切和三分牵制。按分析性预计出场14到22分钟，他的实际时间直接决定主队进攻质量，也是这场最大的变量。'}),
            displayPlayerAnalysisStatement({version_id:'ffc2947f-ae38-424c-894f-c2f05d71c3d6'},
              {name:'Luka Doncic'},{statement:'湖人进攻核心，持球挡拆对勇士换防是直接考验。来源称湖人首发已有节奏，分析性预计出场16到24分钟，他的状态决定湖人能否把比赛带走。'}),
            displayPlayerAnalysisStatement({version_id:'ffc2947f-ae38-424c-894f-c2f05d71c3d6'},
              {name:'Draymond Green'},{statement:'勇士防守沟通和组织的枢纽，老将负荷管理下预计有限出场。他在场时勇士的换防和协防体系更完整，缺阵时段主队防守容易掉档。'}),
          ],
          tableRows: [...document.querySelectorAll('.matchup-history-table tbody tr')].map(row => [...row.children].map(cell => cell.textContent)),
          matchupHeaders: [...document.querySelectorAll('.matchup-history-table thead th')].map(cell => cell.textContent),
          matchupWinnerTeams: [...document.querySelectorAll('.matchup-history-table tbody tr')].map(row => row.querySelector('.matchup-winner-team')?.textContent || ''),
          matchupWinnerScores: [...document.querySelectorAll('.matchup-history-table tbody tr')].map(row => [...row.querySelectorAll('.matchup-winner-score')].map(cell => cell.textContent)),
          windowTitles: [...document.querySelectorAll('.matchup-window h3')].map(node => node.textContent),
          windowSummaries: [...document.querySelectorAll('.matchup-window')].map(node => node.textContent),
          overflowWithoutBodyClip: (() => {
            const original = document.body.style.overflowX;
            document.body.style.overflowX = 'visible';
            const result = { document: document.documentElement.scrollWidth,
              body: document.body.scrollWidth, viewport: innerWidth };
            document.body.style.overflowX = original;
            return result;
          })(),
          coreRows: rows,
          comparisonSemantics: {
            roundedTie: teamSeasonCompare({key:'off_rating',preference:'higher'},100.01,100.04),
            pace: teamSeasonCompare({key:'pace',preference:null},101.7,100.7),
            lowerDefense: teamSeasonCompare({key:'def_rating',preference:'lower'},115.0,112.0),
          },
          sampleMethodVisible: document.querySelector('#technical-content').textContent.includes('指标有效样本'),
          emptyPlayerSection: document.querySelector('.players-section').classList.contains('is-empty'),
          rosterDomExists: Boolean(document.querySelector('#roster-context')),
          relationshipDomExists: Boolean(document.querySelector('#prediction-relationship')),
          documentWidth: document.documentElement.scrollWidth,
          viewportWidth: innerWidth,
        };
      }, config.gameId);

      const recentStatusBoundaries = await page.evaluate(id => {
        const originalGame = state.activeGame;
        const originalVersion = state.currentAnalyses.get(id);
        const finalGame = { ...originalGame, status: 'final',
          away: { ...originalGame.away, score: 98 }, home: { ...originalGame.home, score: 124 },
          result: { away_score: 98, home_score: 124 } };
        renderContext(finalGame, originalVersion);
        const afterFinal = { text: document.querySelector('#schedule-context').textContent,
          lineup: document.querySelector('#lineup-context').textContent };

        const lateVersion = structuredClone(originalVersion);
        lateVersion.input_snapshot.season_form.cutoff = '2027-01-01T00:00:00+00:00';
        const lateGame = { ...originalGame, season_form: {
          ...originalGame.season_form, cutoff: '2027-01-01T00:00:00+00:00' } };
        renderContext(lateGame, lateVersion);
        const afterLateCutoff = document.querySelector('#schedule-context').textContent;

        const includesTargetGame = structuredClone(originalGame);
        includesTargetGame.season_form.teams.home.current_last5.game_ids = [originalGame.id];
        renderContext(includesTargetGame, originalVersion);
        const includesTarget = document.querySelector('#schedule-context').textContent;

        state.currentAnalyses.set(id, originalVersion);
        renderDetail(originalGame);
        return { afterFinal, afterLateCutoff, includesTarget,
          versionId: state.currentAnalyses.get(id).version_id,
          payloadHash: state.currentAnalyses.get(id).payload_sha256 };
      }, config.gameId);

      const emptyMatchup = await page.evaluate(id => {
        const current = state.currentAnalyses.get(id);
        const emptyVersion = { ...current, input_snapshot: { ...current.input_snapshot,
          matchup_history: { status: 'unavailable', meetings: [], matchup_relevance: 'low' } } };
        state.currentAnalyses.set(id, emptyVersion);
        renderDetail(state.data.games.find(game => game.id === id));
        const result = { text: document.querySelector('#matchup-history').textContent,
          compact: document.querySelector('#matchup-history-section').classList.contains('is-empty'),
          height: document.querySelector('#matchup-history-section').getBoundingClientRect().height };
        state.currentAnalyses.set(id, current);
        renderDetail(state.data.games.find(game => game.id === id));
        return result;
      }, config.gameId);

      const shortHistory = await page.evaluate(id => {
        const current = state.currentAnalyses.get(id);
        const shortVersion = { ...current, input_snapshot: { ...current.input_snapshot,
          matchup_history: { ...current.input_snapshot.matchup_history,
            meetings: current.input_snapshot.matchup_history.meetings.slice(-3), matchup_relevance: 'low' } } };
        state.currentAnalyses.set(id, shortVersion);
        renderDetail(state.data.games.find(game => game.id === id));
        const result = { count: document.querySelectorAll('.matchup-history-table tbody tr').length,
          windows: [...document.querySelectorAll('.matchup-window h3')].map(node => node.textContent) };
        state.currentAnalyses.set(id, current);
        renderDetail(state.data.games.find(game => game.id === id));
        return result;
      }, config.gameId);

      const untypedHistory = await page.evaluate(id => {
        const current = state.currentAnalyses.get(id);
        const version = { ...current, input_snapshot: { ...current.input_snapshot,
          matchup_history: { ...current.input_snapshot.matchup_history,
            meetings: current.input_snapshot.matchup_history.meetings.map((meeting, index) => ({
              ...meeting, game_id: `untyped-${index}`, game_type: null, season_type: null,
              competition: null, is_playoffs: false,
            })) } } };
        state.currentAnalyses.set(id, version);
        renderDetail(state.data.games.find(game => game.id === id));
        const table = document.querySelector('.matchup-history-table');
        const result = { headers: [...table.querySelectorAll('thead th')].map(cell => cell.textContent),
          rowCellCounts: [...table.querySelectorAll('tbody tr')].map(row => row.children.length) };
        state.currentAnalyses.set(id, current);
        renderDetail(state.data.games.find(game => game.id === id));
        return result;
      }, config.gameId);

      const connectedPanels = await page.evaluate(id => {
        const original = state.currentAnalyses.get(id), game = state.activeGame;
        const name = original.input_snapshot.roster_context.teams.home.official_team_roster.players[0].name;
        const keyVersion = structuredClone(original);
        keyVersion.judgment.player_analysis = [{player_name:name,statement:'本场重点看他的持球与空间分工。',classification:'subjective_analysis',evidence_ids:[]}];
        keyVersion.analyst_context.objective_evidence.push({id:'injury-background',section:'web_context',value:{
          team_id:game.home.id,eligible_for_fact:true,claim_type:'long_term_injury',captured_at:'2026-10-19T11:30:00+00:00',
          quote:name+' missed practice with a sprain.',url:'https://example.com/verified-background'}});
        keyVersion.analyst_context.objective_evidence.push({id:'late-background',section:'web_context',value:{
          team_id:game.home.id,eligible_for_fact:true,claim_type:'long_term_injury',captured_at:'2026-10-21T11:30:00+00:00',
          quote:'FUTURE_CAPTURE_MUST_NOT_SHOW',url:'https://example.com/late'}});
        keyVersion.analyst_context.objective_evidence.push({id:'live-availability',section:'web_context',value:{
          team_id:game.home.id,eligible_for_fact:true,claim_type:'availability',temporal_class:'LIVE',status:'questionable',
          captured_at:'2026-10-19T11:30:00+00:00',quote:name+' is questionable for this game.',url:'https://example.com/game-status'}});
        state.currentAnalyses.set(id,keyVersion);
        renderProductAnalysis(keyVersion); renderContext(game,keyVersion);
        const structured={cards:document.querySelectorAll('#key-players .player-entry').length,
          keyText:document.querySelector('#key-players').textContent,injuryText:document.querySelector('#injury-context').textContent};
        const fallbackVersion=structuredClone(keyVersion);fallbackVersion.judgment.player_analysis=[];
        fallbackVersion.editorial_material[0].pregame_plan.player_usage=[{side:'home',player_name:name,
          basis:'agent_estimate',minutes_min:12,minutes_max:20,evidence_ids:['roster-home'],rationale:'UNREVIEWED_RATIONALE_MUST_NOT_SHOW'}];
        state.currentAnalyses.set(id,fallbackVersion);renderProductAnalysis(fallbackVersion);renderContext(game,fallbackVersion);
        const fallback={cards:document.querySelectorAll('#key-players .player-entry').length,
          keyText:document.querySelector('#key-players').textContent,injuryText:document.querySelector('#injury-context').textContent};
        const balancedVersion=structuredClone(original);
        const balancedClaims=[];
        for(const side of ['away','home']){
          const team=balancedVersion.input_snapshot.roster_context.teams[side];
          team.team_id=game[side].id;
          team.official_team_roster={...(team.official_team_roster||{}),status:'available',team_id:game[side].id,
            players:Array.from({length:3},(_,index)=>({name:`Synthetic ${side} player ${index+1}`,team_id:game[side].id,
              player_id:`synthetic-${side}-${index+1}`}))};
          for(const player of team.official_team_roster.players.slice(0,5))
            balancedClaims.push({player_name:player.name,statement:'synthetic role and matchup text',classification:'subjective_analysis',evidence_ids:[]});
        }
        balancedVersion.judgment.player_analysis=balancedClaims;
        state.currentAnalyses.set(id,balancedVersion);renderProductAnalysis(balancedVersion);
        const balanced={awayCards:document.querySelectorAll('#key-players .player-side-away .player-entry').length,
          homeCards:document.querySelectorAll('#key-players .player-side-home .player-entry').length,
          vs:document.querySelector('.matchup-axis strong')?.textContent||null,
          axisLabel:document.querySelector('.matchup-axis span')?.textContent||null};
        state.currentAnalyses.set(id,original);renderProductAnalysis(original);renderContext(game,original);
        return {structured,fallback,balanced};
      },config.gameId);
      const teamSeasonStats = await page.evaluate(id => {
        const game = state.activeGame;
        const original = game.previous_season_team_stats;
        game.previous_season_team_stats = {status:'available',season:'2025-26',teams:{
          away:{status:'available',metrics:{off_rating:112,def_rating:112.6,net_rating:-0.6,pace:101.7,three_pct:35.6}},
          home:{status:'available',metrics:{off_rating:115.7,def_rating:113.3,net_rating:2.4,pace:100.7,three_pct:35.9}},
        }};
        renderDetail(game);
        const section=document.querySelector('#team-data');
        const result={season:section.querySelector('.team-season-label')?.textContent,
          labels:[...section.querySelectorAll('.comparison-row > span')].map(node=>node.textContent),
          values:[...section.querySelectorAll('.comparison-row .comparison-value strong')].map(node=>node.textContent),
          compared:[...section.querySelectorAll('.comparison-row .comparison-value')].map(node=>node.classList.contains('is-compared')),
          summary:section.querySelector('.team-season-summary')?.textContent,
          staleWindowText:document.querySelector('#technical-content')?.textContent.includes('窗口请求')||false,
          sampleValueText:section.textContent.includes('样本值较高')||section.textContent.includes('样本值较低')};
        if(original===undefined) delete game.previous_season_team_stats; else game.previous_season_team_stats=original;
        renderDetail(game);
        return result;
      },config.gameId);
      const availabilityTransitions = await page.evaluate(id => {
        const original = state.currentAnalyses.get(id), game = state.activeGame;
        const player = original.input_snapshot.roster_context.teams.home.official_team_roster.players[0];
        const reportBase=Date.now()-60*60*1000;
        const reportTime=offset=>new Date(reportBase+offset*60*1000).toISOString();
        const reportUrlFor=timestamp=>{
          const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',year:'numeric',
            month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:true})
            .formatToParts(new Date(timestamp)).map(part=>[part.type,part.value]));
          return `https://ak-static.cms.nba.com/referee/injury/Injury-Report_${parts.year}-${parts.month}-${parts.day}_${parts.hour}_${parts.minute}${parts.dayPeriod}.pdf`;
        };
        const record = (status, captured_at, reason='right knee soreness') => ({
          game_id:game.id,player_id: player.player_id, player_name: player.name, team_id: game.home.id,
          status, reason, source_published_at:captured_at,captured_at,source_url:reportUrlFor(captured_at),
        });
        const renderRecords = records => {
          const version = structuredClone(original);
          version.analyst_context.objective_evidence = [];
          version.input_snapshot.background = { ...(version.input_snapshot.background || {}),
            availability: { status: 'available',game_id:game.id,source_url:records.at(-1)?.source_url,
              source_published_at:records.at(-1)?.source_published_at,captured_at:records.at(-1)?.captured_at,
              time_validation: { accepted_records: records.length, cutoff: '2026-10-19T12:00:00+00:00' },
              records } };
          state.currentAnalyses.set(id, version);
          renderContext(game, version);
          return document.querySelector('#injury-context').textContent;
        };
        const questionableToAvailable = renderRecords([
          record('questionable', reportTime(0)),
          record('available', reportTime(10)),
        ]);
        const questionableToOut = renderRecords([
          record('questionable', reportTime(0)),
          record('out', reportTime(10), 'left knee strain'),
        ]);
        const normalOnly = renderRecords([record('available', reportTime(10))]);
        const quoteOnly = structuredClone(original);
        quoteOnly.analyst_context.objective_evidence = [{id:'quote-only-status',section:'web_context',value:{
          eligible_for_fact:true,team_id:game.home.id,player_name:player.name,player_id:player.player_id,
          claim_type:'availability',temporal_class:'LIVE',quote:player.name+' is questionable for this game.',
          captured_at:'2026-10-19T11:30:00+00:00',url:'https://example.com/quote-only'}}];
        quoteOnly.input_snapshot.background = { ...(quoteOnly.input_snapshot.background || {}),
          availability: { status:'unavailable', records:[] } };
        state.currentAnalyses.set(id, quoteOnly);
        renderContext(game, quoteOnly);
        const quoteOnlyText = document.querySelector('#injury-context').textContent;
        const restriction = structuredClone(original);
        restriction.analyst_context.objective_evidence = [{id:'confirmed-limit',section:'web_context',value:{
          eligible_for_fact:true,team_id:game.home.id,player_name:player.name,player_id:player.player_id,
          claim_type:'minutes_restriction',temporal_class:'LIVE',status:'limited',reason:'knee',
          captured_at:'2026-10-19T11:30:00+00:00',url:'https://example.com/restriction'}}];
        restriction.input_snapshot.background = { ...(restriction.input_snapshot.background || {}),
          availability: { status:'unavailable', records:[] } };
        state.currentAnalyses.set(id, restriction);
        renderContext(game, restriction);
        const limited = document.querySelector('#injury-context').textContent;
        state.currentAnalyses.set(id, original);
        renderContext(game, original);
        return {questionableToAvailable,questionableToOut,normalOnly,quoteOnlyText,limited};
      },config.gameId);
      const themeCounts = await page.evaluate(id => {
        const original = state.currentAnalyses.get(id);
        const counts = [];
        for (const count of [0, 1, 2, 4]) {
          const themes = Array.from({ length: count }, (_, index) => ({
            title: `测试看点${index + 1}`, statement: `用于验证${index + 1}条看点。`, evidence_ids: [], classification: 'subjective_analysis',
          }));
          const version = { ...original, judgment: { ...original.judgment, watch_themes: themes, player_analysis: [] } };
          state.currentAnalyses.set(id, version);
          renderProductAnalysis(version);
          const section = document.querySelector('#factors-section');
          counts.push({
            requested: count,
            actual: document.querySelectorAll('#key-factors .factor-card').length,
            hidden: section.hidden,
            hasNumberMarker: Boolean(document.querySelector('#key-factors .factor-card > span')),
            keyPlayerText: document.querySelector('#key-players').textContent,
          });
        }
        renderProductAnalysisState(state.activeGame, 'idle');
        const emptyKeyPlayerText = document.querySelector('#key-players').textContent;
        const zeroThemes = state.currentAnalyses.get(id);
        renderProductAnalysis(zeroThemes);
        const unavailableKeyPlayerText = document.querySelector('#key-players').textContent;
        return { counts, emptyKeyPlayerText, unavailableKeyPlayerText };
      }, config.gameId);

      const lineups = await page.evaluate(({ id, scenarios }) => {
        const game = state.data.games.find(item => item.id === id);
        const initialVersion = state.currentAnalyses.get(id);
        const outputs = {};
        for (const scenario of scenarios) {
          const version = {
            ...initialVersion,
            editorial_material: scenario.editorial_material,
            input_snapshot: { ...initialVersion.input_snapshot,
              roster_context: scenario.roster_context || initialVersion.input_snapshot.roster_context,
              background: scenario.background || initialVersion.input_snapshot.background },
          };
          state.currentAnalyses.set(id, version);
          renderDetail(game);
          const lineupCard = document.querySelector('#lineup-context');
          outputs[scenario.name] = { heading: lineupCard.querySelector('h3')?.textContent || '',
            text: lineupCard.textContent };
        }
        return outputs;
      }, { id: config.gameId, scenarios: config.lineupScenarios });

      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForFunction(id => state.activeGame?.id === id && state.currentAnalyses.has(id), config.gameId);
      if (config.screenshotDirectory) {
        await page.screenshot({ path: `${config.screenshotDirectory}/detail-viewport-${viewport.width}x${viewport.height}.png` });
        await page.screenshot({ path: `${config.screenshotDirectory}/detail-${viewport.width}x${viewport.height}.png`, fullPage: true });
        await page.locator('#team-data-title').scrollIntoViewIfNeeded();
        await page.screenshot({ path: `${config.screenshotDirectory}/team-data-${viewport.width}x${viewport.height}.png` });
      }
      await page.locator('#ai-analysis').click();
      await page.waitForFunction(() => document.querySelector('#sheet-content .analyst-short-review h4'));
      const loadingReview = await page.evaluate(() => {
        const review = document.querySelector('#sheet-content .analyst-short-review');
        const rect = review.getBoundingClientRect();
        return { text: review.textContent, top: rect.top, left: rect.left, width: rect.width,
          railTop: review.closest('.analyst-verdict').getBoundingClientRect().top,
          sheetScrollTop: document.querySelector('.analysis-sheet').scrollTop,
          noteVisible: Boolean(document.querySelector('#sheet-content .analysis-refresh-note')),
          count: document.querySelectorAll('#sheet-content .analyst-short-review').length };
      });
      await page.waitForFunction(() => !document.querySelector('#sheet-content .analysis-refresh-note'));
      await page.waitForFunction(() => document.querySelector('#sheet-content').textContent.includes('冻结版本赛前短评'));
      const analysis = await page.evaluate(async id => {
        const review = document.querySelector('#sheet-content .analyst-short-review');
        const reviewRect = review.getBoundingClientRect();
        const article = document.querySelector('#sheet-content .analysis-article');
        const articleRect = article.getBoundingClientRect();
        const sheet = document.querySelector('.analysis-sheet');
        const originalScrollTop = sheet.scrollTop;
        sheet.scrollTop = 520;
        await new Promise(requestAnimationFrame);
        const scrolledRail = { top: review.getBoundingClientRect().top,
          position: getComputedStyle(document.querySelector('.analyst-verdict')).position,
          scrollTop: sheet.scrollTop };
        sheet.scrollTop = originalScrollTop;
        return {
          versionId: state.currentAnalyses.get(id).version_id,
          articleVisible: document.querySelector('#sheet-content').textContent.includes('冻结版本专属正文'),
          probability: document.querySelector('#predicted-probability').textContent,
          margin: document.querySelector('#expected-margin').textContent,
          total: document.querySelector('#expected-total').textContent,
          shortReviewText: review.textContent,
          shortReviewCount: document.querySelectorAll('#sheet-content .analyst-short-review').length,
          shortReviewInPredictionRail: Boolean(review.closest('.analyst-verdict')),
          rightHeroCount: document.querySelectorAll('#sheet-content .analysis-thesis').length,
          shortReviewRect: { top: reviewRect.top, left: reviewRect.left, width: reviewRect.width },
          railTop: review.closest('.analyst-verdict').getBoundingClientRect().top,
          sheetScrollTop: sheet.scrollTop,
          articleLeft: articleRect.left,
          scrolledRail,
          overflow: document.documentElement.scrollWidth > innerWidth + 1,
        };
      }, config.gameId);
      await page.locator('#close-sheet').click();
      await page.waitForFunction(() => document.querySelector('#ai-sheet').hidden);
      const returned = await page.evaluate(id => ({
        versionId: state.currentAnalyses.get(id).version_id,
        keyPlayerText: document.querySelector('#key-players').textContent,
        probability: document.querySelector('#predicted-probability').textContent,
      }), config.gameId);
      results.push({ viewport, initial, recentStatusBoundaries, emptyMatchup, shortHistory, untypedHistory, connectedPanels, teamSeasonStats, availabilityTransitions, themeCounts, lineups, loadingReview, analysis, returned, aiPosts, marketReads, externalAssets, pageErrors, consoleErrors,
        overflow: analysis.overflow });
      await context.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def _fixture(game: dict):
    game["game_type"] = "preseason"
    game["nba_game_id"] = "0022600001"
    game_id = game["id"]
    side_players = {
        side: [f"25th-{side}-player-{index}" for index in range(1, 6)]
        for side in ("away", "home")
    }
    roster_teams = {}
    display_names = []
    objective_evidence = []
    source_claim_ids = {"confirmed": {"home": [], "away": []}, "source_expected": {"home": [], "away": []}}
    for side in ("away", "home"):
        team_id = game[side]["id"]
        roster_teams[side] = {
            "team_id": team_id,
            "official_team_roster": {
                "status": "available", "team_id": team_id,
                "players": [{"player_id": f"fixture-{side}-{i}", "name": name, "team_id": team_id}
                            for i, name in enumerate(side_players[side], 1)],
            },
            "confirmed_starters": [], "projected_starters": [],
        }
        objective_evidence.append({
            "id": f"roster-{side}", "section": "roster_context", "value": {
                "teams": {side: {"team_id": team_id, "official_team_roster": roster_teams[side]["official_team_roster"]}},
            },
        })
        for name in side_players[side]:
            display_names.append({"player_id": f"fixture-{side}", "canonical_name_en": name,
                                 "display_name_zh": f"{game[side]['name_zh']}测试球员{len(display_names) + 1}",
                                 "translation_source": "known_mapping"})
            for basis, claim_type in (("confirmed", "confirmed_starter"), ("source_expected", "projected_starters")):
                evidence_id = f"{basis}-{side}-{len(source_claim_ids[basis][side]) + 1}"
                source_claim_ids[basis][side].append(evidence_id)
                objective_evidence.append({
                    "id": evidence_id, "section": "web_context", "value": {
                        "eligible_for_fact": True, "team_id": team_id, "claim_type": claim_type,
                        "quote": f"{name} 被列为本场相关首发资料样例。",
                    },
                })

    meetings = []
    home_id, away_id = game["home"]["id"], game["away"]["id"]
    for index in range(1, 11):
        historical_home = home_id if index % 2 else away_id
        historical_away = away_id if historical_home == home_id else home_id
        home_wins = index % 3 != 0
        current_home_score = 120 + index if home_wins else 100 + index
        current_away_score = 90 + index if home_wins else 115 + index
        historical_home_score = current_home_score if historical_home == home_id else current_away_score
        historical_away_score = current_away_score if historical_away == away_id else current_home_score
        if index == 8:
            historical_home_score = 0  # Legacy missing-score sentinel must not display as a real zero.
        meetings.append({
            "game_id": f"{'004' if index == 7 else '002'}25{index:05d}", "played_at": f"2026-01-{index:02d}T02:30:00",
            "season_start": 2025, "is_playoffs": index == 7,
            "home_team_id": historical_home, "away_team_id": historical_away,
            "home_score": historical_home_score, "away_score": historical_away_score,
        })

    matchup_history = {
        "status": "available", "game_id": game_id, "home_team_id": home_id, "away_team_id": away_id,
        "meetings": meetings, "matchup_relevance": "low", "numeric_model_usage": False,
    }
    roster_context = {
        "game_id": game_id, "context_cutoff": "2026-10-19T12:00:00+00:00",
        "numeric_model_usage": False, "teams": roster_teams,
    }
    snapshot = copy.deepcopy(game)
    snapshot["roster_context"] = roster_context
    snapshot["matchup_history"] = matchup_history
    snapshot["player_display_names"] = display_names
    snapshot["season_form"] = {
        "status": "available", "game_id": game_id, "season_start": 2026,
        "cutoff": "2026-10-19T11:00:00+00:00",
        "competition_scope": "all competitive games in each season",
        "teams": {
            "away": {
                "team_id": game["away"]["id"], "current_season_games": 0,
                "current_last5": {"status": "no_games", "games": 0, "game_ids": []},
                "previous_last10": {"status": "available", "games": 10, "win_rate": 0.4,
                                    "game_ids": [f"previous-away-{i}" for i in range(10)]},
            },
            "home": {
                "team_id": game["home"]["id"], "current_season_games": 5,
                "current_last5": {"status": "available", "games": 5, "win_rate": 0.6,
                                   "game_ids": [f"00226000{i:02d}" for i in range(2, 7)]},
                "previous_last10": {"status": "available", "games": 10, "win_rate": 0.5,
                                    "game_ids": [f"previous-home-{i}" for i in range(10)]},
            },
        },
    }
    snapshot.setdefault("technical", {})["cutoff"] = "2026-10-19T12:00:00+00:00"

    model_evidence_ids = {side: [f"roster-{side}"] for side in ("away", "home")}
    plan = {
        "lineups": [
            {"side": "away", "basis": "source_expected", "players": side_players["away"],
             "evidence_ids": model_evidence_ids["away"] + source_claim_ids["source_expected"]["away"],
             "rationale": "browser fixture"},
            {"side": "home", "basis": "agent_expected", "players": side_players["home"],
             "evidence_ids": model_evidence_ids["home"], "rationale": "browser fixture"},
        ],
        "player_usage": [], "key_matchup": "offline fixture", "uncertainty": [],
    }
    metrics = {
        "off_rating_15": {"away": 116.0, "home": 110.0},
        "def_rating_15": {"away": 111.0, "home": 107.0},
        "net_rating_15": {"away": 8.0, "home": 3.0},
        "pace_15": {"away": 102.0, "home": 96.0},
        "three_pct_15": {"away": 0.39, "home": 0.35},
        "rebound_share_15": {"away": 0.53, "home": 0.51},
        "turnover_rate_15": {"away": 0.15001, "home": 0.15004},
    }
    team_form = {side: {"metrics": {
        key: {"value": values[side], "valid_games": 13, "missing_games": 2,
              "validation": "matched", "aggregation": "mean_of_per_game_values", "method": "frozen-fixture"}
        for key, values in metrics.items()
    }} for side in ("away", "home")}
    version_id = "fixture-version-package-a"
    payload_sha = "a" * 64
    version = {
        "version_id": version_id, "game_id": game_id,
        "analysis_contract_version": "two-stage-forecast-v1",
        "schedule_revision": game.get("schedule_revision", 1),
        "game_identity": {"game_id": game_id, "home_team_id": home_id, "away_team_id": away_id,
                          "scheduled_tipoff": game["tipoff"]},
        "input_cutoff_at": "2026-10-19T12:00:00+00:00",
        "published_at": "2026-10-20T22:00:00+00:00",
        "payload_sha256": payload_sha, "base_input_hash": "fixture-input-hash",
        "prediction": {"winner_side": "home", "win_probability": 0.65, "expected_margin": 5.5, "expected_total": 225.5},
        "judgment": {
            "winner_lean": "home", "side_lean": "home", "total_lean": "over", "confidence": "medium",
            "baseline_agreement": {"pure": "agree", "market": "not_applicable", "residual": "not_applicable"},
            "subjective_rationale": "fixture rationale",
            "pregame_story": {"title": "冻结版本赛前短评", "statement": "本页展示所选正式版本。"},
            "watch_themes": [
                {"title": "样本窗口", "statement": "第一条赛前看点。"},
                {"title": "对位变量", "statement": "第二条赛前看点。"},
                {"title": "轮换变化", "statement": "第三条赛前看点。"},
                {"title": "末节处理", "statement": "第四条赛前看点。"},
            ],
            "player_analysis": [],
            "final_prediction": {"winner_side": "home", "win_probability": 0.65,
                                 "expected_margin": 5.5, "expected_total": 225.5, "evidence_ids": []},
            "analysis_article": "\n\n".join(
                ["冻结版本专属正文：这是与数字预测绑定的同一份离线文章。"] * 12
            ),
            "analysis_article_evidence_ids": [], "supporting_evidence": [], "conflicting_evidence": [],
            "key_matchup_variables": [], "roster_analysis": [], "uncertainty": [],
        },
        "editorial_material": [{"summary": "本场预计首发与核心球员负荷", "pregame_plan": plan}],
        "input_snapshot": snapshot, "analyst_context": {"objective_evidence": objective_evidence},
        "evidence_index": {item["id"]: {"section": item["section"]} for item in objective_evidence},
        "sources": [], "public_market_reference": None,
    }
    selection = {"status": "current", "game_id": game_id, "version_id": version_id,
                 "state_revision": 3, "version": version}
    fact_context = {
        "schema_version": "pregame-fact-context-v1", "game_id": game_id,
        "feature_cutoff": "2026-10-19T11:00:00+00:00", "team_form": team_form,
    }
    fact_view = {"schema_version": "pregame-fact-view-v1", "game_id": game_id,
                 "version_id": version_id, "source_payload_sha256": payload_sha,
                 "context": fact_context}
    response = {"status": "success", "predictions": {game_id: {**selection, "fact_view": fact_view}}}
    display_names = {row.get("canonical_name_en"): row.get("display_name_zh")
                     for row in (game.get("player_display_names") or snapshot.get("player_display_names") or [])}

    def plan_for(basis, side):
        if basis == "unavailable":
            return {"side": side, "basis": basis, "players": [], "evidence_ids": [], "rationale": "unavailable"}
        refs = [f"roster-{side}"]
        if basis in {"confirmed", "source_expected"}:
            refs += source_claim_ids[basis][side]
        return {"side": side, "basis": basis, "players": side_players[side], "evidence_ids": refs,
                "rationale": "browser fixture"}

    scenarios = []
    for name, basis in (("confirmed", "confirmed"), ("source_expected", "source_expected"),
                        ("agent_expected", "agent_expected"), ("unavailable", "unavailable")):
        scenario_plan = {"lineups": [plan_for(basis, "away"), plan_for(basis, "home")],
                         "player_usage": [], "key_matchup": "fixture", "uncertainty": []}
        scenarios.append({"name": name, "editorial_material": [{"pregame_plan": scenario_plan}]})
    conflict_plan = copy.deepcopy(plan)
    second_conflict = copy.deepcopy(plan)
    second_conflict["lineups"][1]["players"] = [*side_players["home"][:-1], "Unverified Other Player"]
    scenarios.append({"name": "conflict", "editorial_material": [
        {"pregame_plan": conflict_plan}, {"pregame_plan": second_conflict},
    ]})
    incomplete_plan = copy.deepcopy(plan)
    incomplete_plan["lineups"][1]["players"] = incomplete_plan["lineups"][1]["players"][:4]
    scenarios.append({"name": "incomplete", "editorial_material": [{"pregame_plan": incomplete_plan}]})
    wrong_roster = copy.deepcopy(roster_context)
    wrong_roster["teams"]["home"]["team_id"] = away_id
    scenarios.append({"name": "identity_mismatch", "editorial_material": [{"pregame_plan": plan}],
                      "roster_context": wrong_roster})
    out_background = copy.deepcopy(snapshot.get("background") or {})
    tipoff = datetime.fromisoformat(str(game["tipoff"]).replace("Z", "+00:00"))
    report_time = min(datetime.now(timezone.utc), tipoff) - timedelta(minutes=5)
    local_report_time = report_time.astimezone(ZoneInfo("America/New_York"))
    report_url = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_" + local_report_time.strftime("%Y-%m-%d_%I_%M%p") + ".pdf"
    report_stamp = report_time.isoformat(timespec="seconds").replace("+00:00", "Z")
    out_background["availability"] = {"status": "available", "game_id": game_id,
        "source_url": report_url, "source_published_at": report_stamp, "captured_at": report_stamp,
        "records": [{"game_id": game_id, "team_id": home_id, "player_name": side_players["home"][0],
                     "status": "out", "source_url": report_url,
                     "source_published_at": report_stamp, "captured_at": report_stamp}]}
    scenarios.append({"name": "confirmed_out", "editorial_material": [{"pregame_plan": plan}],
                      "background": out_background})
    legacy_rosters = copy.deepcopy(roster_context)
    legacy_rosters["teams"]["away"]["confirmed_starters"] = list(side_players["away"])
    legacy_rosters["teams"]["home"]["projected_starters"] = list(side_players["home"])
    scenarios.append({"name": "legacy_snapshot", "editorial_material": [], "roster_context": legacy_rosters})
    return {"game_id": game_id, "data": {**json.loads(Path(STATIC_ROOT, "app_data.json").read_text(encoding="utf-8")),
                                            "games": [game]},
            "response": response, "version": version, "lineup_scenarios": scenarios,
            "side_players": side_players, "display_names": display_names}


def test_selected_version_compaction_and_starters_in_real_browser(tmp_path):
    node = shutil.which("node")
    playwright = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
    if not node or not playwright.is_dir():
        pytest.skip("Local Node and Playwright bundle required; no downloads are performed.")
    data = json.loads((STATIC_ROOT / "app_data.json").read_text(encoding="utf-8"))
    game = copy.deepcopy(next(item for item in data["games"] if item["id"] == "espn:401909089"))
    fixture = _fixture(game)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(STATIC_ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    screenshot_directory = Path(os.environ.get("COURTVISION_PACKAGE_A_SCREENSHOTS", str(tmp_path / "screenshots")))
    screenshot_directory.mkdir(parents=True, exist_ok=True)
    try:
        probe = subprocess.run(
            [node, "-e", BROWSER_CHECK, str(playwright)],
            input=json.dumps({"url": f"http://127.0.0.1:{server.server_port}", **fixture,
                              "gameId": fixture["game_id"],
                              "lineupScenarios": fixture["lineup_scenarios"],
                              "screenshotDirectory": str(screenshot_directory)}, ensure_ascii=False),
            text=True, encoding="utf-8", capture_output=True, timeout=90, check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    assert probe.returncode == 0, probe.stderr
    results = json.loads(probe.stdout)
    assert len(results) == 2
    receipt = []
    for row in results:
        initial = row["initial"]
        assert initial["versionId"] == "fixture-version-package-a"
        assert all("分析性预计" not in item and "来源称" not in item and "负荷管理" not in item and "有限出场" not in item
                   for item in initial["frozenPlayerCopy"])
        connected = row["connectedPanels"]
        assert connected["structured"]["cards"] == 1
        assert "持球与空间分工" in connected["structured"]["keyText"]
        assert "本场官方伤病报告暂未获取" in connected["structured"]["injuryText"]
        assert "出战成疑" not in connected["structured"]["injuryText"]
        assert "查看官方报告" not in connected["structured"]["injuryText"]
        assert "questionable" not in connected["structured"]["injuryText"].lower()
        assert "missed practice" not in connected["structured"]["injuryText"].lower()
        assert "FUTURE_CAPTURE_MUST_NOT_SHOW" not in connected["structured"]["injuryText"]
        assert connected["fallback"]["cards"] == 0
        assert "12–20" not in connected["fallback"]["keyText"]
        assert "预计负荷" not in connected["fallback"]["keyText"]
        assert "UNREVIEWED_RATIONALE_MUST_NOT_SHOW" not in connected["fallback"]["keyText"]
        assert connected["balanced"] == {"awayCards": 3, "homeCards": 3, "vs": "VS", "axisLabel": None}
        stats = row["teamSeasonStats"]
        assert stats["season"] == "上赛季（2025-26）"
        assert stats["labels"] == ["进攻效率", "防守效率", "净效率", "比赛节奏", "三分命中率"]
        assert stats["values"] == ["112.0", "115.7", "112.6", "113.3", "-0.6", "2.4", "101.7", "100.7", "35.6%", "35.9%"]
        assert len(stats["compared"]) == 10 and not stats["compared"][6] and not stats["compared"][7]
        assert "样本值较高" not in stats["summary"] and "样本值较低" not in stats["summary"]
        assert stats["staleWindowText"] is False and stats["sampleValueText"] is False
        assert "Agent预计" not in connected["fallback"]["injuryText"]
        assert "12–20" not in connected["fallback"]["injuryText"]
        assert "关键球员分钟计划" not in connected["fallback"]["injuryText"]
        assert "本场官方伤病报告中暂无异常状态" in row["availabilityTransitions"]["questionableToAvailable"]
        assert "确认缺阵" in row["availabilityTransitions"]["questionableToOut"]
        assert "膝部拉伤" in row["availabilityTransitions"]["questionableToOut"]
        assert "本场官方伤病报告中暂无异常状态" in row["availabilityTransitions"]["normalOnly"]
        assert "本场官方伤病报告暂未获取" in row["availabilityTransitions"]["quoteOnlyText"]
        assert "本场官方伤病报告暂未获取" in row["availabilityTransitions"]["limited"]
        assert "Agent预计" not in row["availabilityTransitions"]["limited"]
        assert initial["article"] == fixture["version"]["judgment"]["analysis_article"]
        assert initial["activePrediction"]["expected_total"] == 225.5
        assert initial["probability"] == "65.0%" and initial["total"] == "225.5"
        assert initial["predictionLabel"] == "赛前预测"
        assert initial["judgmentTitle"] == "第25秒判断"
        assert initial["marginTitle"] == "分差" and initial["margin"] == f"{game['home']['name_zh']} +5.5"
        assert initial["totalTitle"] == "总分"
        assert initial["awayWinnerMargin"] == f"{game['away']['name_zh']} +3.5"
        assert initial["displayTitleOverride"]["rendered"] == "巴特勒、穆迪不在，勇士先看看库里身边怎么搭"
        assert initial["displayTitleOverride"]["frozenTitle"] == "勇士主场迎战湖人，真正的变量是库里们能打多久"
        assert initial["lineupTitle"] == "预计首发"
        assert "来源预计" not in initial["lineupText"] and "第25秒预计" not in initial["lineupText"]
        assert "测试球员" in initial["lineupText"]
        assert initial["injuryTitle"] == "伤病与出战"
        assert "本场官方伤病报告暂未获取" in initial["injuryText"]
        assert initial["recentStatusTitle"] == "近期状态"
        assert initial["intelligenceEyebrow"] is None
        assert "本季暂无比赛记录" in initial["recentStatusText"]
        assert "上季末10场" not in initial["recentStatusText"]
        assert "本季近5场：3胜2负" in initial["recentStatusText"]
        assert "上一场" not in initial["recentStatusText"]
        assert "连胜" not in initial["recentStatusText"] and "连败" not in initial["recentStatusText"]
        assert initial["payloadHashBeforeContextRender"] == initial["payloadHashAfterContextRender"]
        assert row["recentStatusBoundaries"]["afterFinal"]["text"] == initial["recentStatusText"]
        assert "124" not in row["recentStatusBoundaries"]["afterFinal"]["text"]
        assert "98" not in row["recentStatusBoundaries"]["afterFinal"]["text"]
        assert "上季末10场" not in row["recentStatusBoundaries"]["afterFinal"]["text"]
        assert row["recentStatusBoundaries"]["versionId"] == initial["versionId"]
        assert row["recentStatusBoundaries"]["payloadHash"] == initial["payloadHashBeforeContextRender"]
        assert row["recentStatusBoundaries"]["afterLateCutoff"].count("赛前近期记录暂不可用") == 2
        assert "本季近5场：3胜2负" not in row["recentStatusBoundaries"]["includesTarget"]
        assert "本季近期战绩暂不可用" in row["recentStatusBoundaries"]["includesTarget"]
        assert len(initial["tableRows"]) == 10
        assert initial["tableRows"][0][0] == "2026/01/10"
        assert initial["matchupHeaders"] == ["日期", "赛事", "客队", "比分", "主队"]
        assert initial["tableRows"][0][1] == "常规赛"
        assert initial["tableRows"][0][2] == game["home"]["name_zh"]
        assert initial["tableRows"][0][3] == "130–100"
        assert initial["tableRows"][0][4] == game["away"]["name_zh"]
        assert "季后赛" in " ".join(" ".join(cells) for cells in initial["tableRows"])
        assert "比分暂缺" in " ".join(" ".join(cells) for cells in initial["tableRows"])
        assert initial["matchupWinnerTeams"][0] == game["home"]["name_zh"]
        assert initial["matchupWinnerScores"][0] == ["130"]
        assert initial["windowTitles"] == ["近5场", "近10场"]
        assert all("本版本" not in item and "比分完整" not in item and "实际" not in item
                   for item in initial["windowSummaries"])
        assert "2胜2负 · 场均得分 110.5" in initial["windowSummaries"][0]
        assert "2胜2负 · 场均得分 118.0" in initial["windowSummaries"][0]
        assert row["untypedHistory"]["headers"] == ["日期", "客队", "比分", "主队"]
        assert set(row["untypedHistory"]["rowCellCounts"]) == {4}
        assert initial["rosterDomExists"] is False and initial["relationshipDomExists"] is False
        assert initial["sampleMethodVisible"] is False
        assert initial["emptyPlayerSection"] is True
        assert row["emptyMatchup"]["compact"] is True
        assert "暂无可用的历史交锋记录" in row["emptyMatchup"]["text"]
        assert row["emptyMatchup"]["height"] < 150
        assert row["shortHistory"]["count"] == 3
        assert row["shortHistory"]["windows"] == ["近5场", "近10场"]
        assert initial["documentWidth"] <= initial["viewportWidth"] + 1
        assert initial["overflowWithoutBodyClip"]["document"] <= initial["overflowWithoutBodyClip"]["viewport"] + 1
        assert initial["overflowWithoutBodyClip"]["body"] <= initial["overflowWithoutBodyClip"]["viewport"] + 1
        rows = {item["label"]: item for item in initial["coreRows"]}
        assert list(rows) == ["进攻效率", "防守效率", "净效率", "比赛节奏", "三分命中率"]
        assert all(item["awaySignal"] == item["homeSignal"] == "" for item in rows.values())
        assert initial["comparisonSemantics"]["roundedTie"] == {"away": False, "home": False}
        assert initial["comparisonSemantics"]["pace"] == {"away": False, "home": False}
        assert initial["comparisonSemantics"]["lowerDefense"] == {"away": False, "home": True}
        counts = row["themeCounts"]["counts"]
        assert [item["actual"] for item in counts] == [0, 1, 2, 4]
        assert counts[0]["hidden"] is True and counts[0]["actual"] == 0
        assert all(item["actual"] == item["requested"] for item in counts[1:])
        assert all(item["hasNumberMarker"] is False for item in counts)
        assert counts[1]["hidden"] is False
        assert counts[0]["keyPlayerText"] == "关键球员解读暂缺"
        assert row["themeCounts"]["emptyKeyPlayerText"] == "尚无已发布分析"
        assert row["themeCounts"]["unavailableKeyPlayerText"] == "关键球员解读暂缺"
        assert row["lineups"]["confirmed"]["heading"] == "首发阵容"
        assert "确认首发" not in row["lineups"]["confirmed"]["text"]
        assert row["lineups"]["source_expected"]["heading"] == "预计首发"
        assert "来源预计" not in row["lineups"]["source_expected"]["text"]
        assert row["lineups"]["agent_expected"]["heading"] == "预计首发"
        assert "第25秒预计" not in row["lineups"]["agent_expected"]["text"]
        assert row["lineups"]["unavailable"]["heading"] == "预计首发"
        assert row["lineups"]["unavailable"]["text"].count("暂无可用的预计首发信息") == 2
        assert "冲突" in row["lineups"]["conflict"]["text"] and "Unverified Other Player" not in row["lineups"]["conflict"]["text"]
        assert "完整性核对" in row["lineups"]["incomplete"]["text"]
        assert "身份或状态核对" in row["lineups"]["identity_mismatch"]["text"]
        out_display_name = fixture["display_names"].get(fixture["side_players"]["home"][0],
                                                        fixture["side_players"]["home"][0])
        assert out_display_name not in row["lineups"]["confirmed_out"]["text"]
        assert row["lineups"]["legacy_snapshot"]["heading"] == "首发阵容"
        assert "来源性质" not in row["lineups"]["legacy_snapshot"]["text"]
        assert "预计首发" in row["lineups"]["legacy_snapshot"]["text"]
        assert row["analysis"]["versionId"] == initial["versionId"]
        assert row["analysis"]["articleVisible"] is True
        assert row["loadingReview"]["noteVisible"] is True
        assert row["loadingReview"]["count"] == 1
        assert row["loadingReview"]["text"] == "赛前短评冻结版本赛前短评"
        assert row["analysis"]["shortReviewCount"] == 1
        assert row["analysis"]["shortReviewInPredictionRail"] is True
        assert row["analysis"]["rightHeroCount"] == 0
        assert row["analysis"]["shortReviewText"] == "赛前短评冻结版本赛前短评"
        assert abs(row["loadingReview"]["top"] - row["analysis"]["shortReviewRect"]["top"]) < 1, (
            f"review top {row['loadingReview']['top']} -> {row['analysis']['shortReviewRect']['top']}; "
            f"rail top {row['loadingReview']['railTop']} -> {row['analysis']['railTop']}; "
            f"sheet scroll {row['loadingReview']['sheetScrollTop']} -> {row['analysis']['sheetScrollTop']}"
        )
        assert abs(row["loadingReview"]["left"] - row["analysis"]["shortReviewRect"]["left"]) < 1
        if row["viewport"]["width"] > 900:
            assert row["analysis"]["shortReviewRect"]["left"] < row["analysis"]["articleLeft"]
            assert row["analysis"]["scrolledRail"]["position"] == "sticky"
            assert row["analysis"]["scrolledRail"]["scrollTop"] > 0
            assert row["analysis"]["scrolledRail"]["top"] >= 139
        else:
            assert row["analysis"]["scrolledRail"]["position"] == "static"
        assert row["analysis"]["total"] == initial["total"]
        assert row["returned"]["versionId"] == initial["versionId"]
        assert row["returned"]["keyPlayerText"] == "关键球员解读暂缺"
        assert row["returned"]["probability"] == initial["probability"]
        assert row["aiPosts"] == 0
        assert row["overflow"] is False
        assert row["pageErrors"] == []
        assert row["consoleErrors"] == []
        receipt.append({"viewport": row["viewport"], "versionId": initial["versionId"],
                        "historyRows": len(initial["tableRows"]),
                        "themeCounts": [item["actual"] for item in row["themeCounts"]["counts"]],
                        "aiPosts": row["aiPosts"], "overflow": row["overflow"],
                        "pageErrors": row["pageErrors"], "consoleErrors": row["consoleErrors"]})
    (screenshot_directory / "browser-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
