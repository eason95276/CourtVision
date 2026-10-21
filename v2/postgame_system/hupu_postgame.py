"""Game-bound Hupu discussion, kept separate from verified basketball facts."""
from __future__ import annotations

import json
import os
import re
import time
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from v2.pregame_system.storage import content_hash, save

SCHEMA = 'postgame-fan-discussion-v2'
CHINA = ZoneInfo('Asia/Shanghai')
MAX_THREADS = 2
MAX_COMMENTS = 8


def instant(value):
    at = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('discussion_timestamp_requires_timezone')
    return at.astimezone(timezone.utc)


def thread_url(url):
    parsed = urlsplit(str(url or ''))
    if (parsed.scheme != 'https' or parsed.hostname != 'bbs.hupu.com' or parsed.username
            or parsed.password or parsed.port not in (None, 443)
            or not re.fullmatch(r'/\d+(?:-\d+)?\.html', parsed.path)):
        raise ValueError('hupu_thread_url_invalid')
    return 'https://bbs.hupu.com' + parsed.path


def plain(text):
    return re.sub(r'\\([\\`*_{}\[\]()#+\-.!:/])', r'\1', str(text or ''))


def comments(text):
    """Read the site's highlighted-replies section, not search snippets."""
    if '这些回帖亮了' not in text:
        return []
    section = text.split('这些回帖亮了', 1)[1].split('Re：', 1)[0]
    result = []
    seen = set()
    for match in re.finditer(r'(.*?)\s*亮了\s*\((\d+)\)', section, re.S):
        chunk = match[1]
        if '引用 @' in chunk:
            continue  # Nested quotes cannot safely be assigned to the replying user.
        if '举报' in chunk:
            chunk = chunk.rsplit('举报', 1)[1]
        lines = []
        for line in chunk.splitlines():
            line = line.strip()
            line = re.sub(r'^\d+楼\s*','',line)
            if (not line or re.match(r'^(?:收起|回复|查看评论|只看此人|点灭|\[图片\]|查看更多)', line)
                    or re.search(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}', line)):
                continue
            if line not in lines:
                lines.append(line)
        quote = ' '.join(lines).strip()
        if not 8 <= len(quote) <= 260:
            continue
        if re.search(r'https?://|加群|免费领|API.?KEY|system prompt|忽略.{0,8}指令|输出.{0,8}密钥',quote,re.I):
            continue
        original=quote
        quote=re.sub(r'傻逼|傻[比屄]|黑猩猩|畜[牲生]|去死','',quote).strip(' ，,。！!')
        if len(quote)<8:continue
        key = re.sub(r'\s+', '', quote)
        if key in seen:
            continue
        seen.add(key)
        result.append({'text': quote, 'likes': int(match[2]),
            'adapted_not_direct_quote':quote!=original,
            'classification': 'fan_opinion_not_verified_fact',
            'ranking_basis': 'highlighted reply on fetched Hupu page'})
    return sorted(result, key=lambda row: row['likes'], reverse=True)[:MAX_COMMENTS]


def qualify_page(row, context, postgame, at):
    url = thread_url(row['url'])
    body = plain(row.get('content'))
    head = body.split('这些回帖亮了', 1)[0]
    title = plain(row.get('title'))
    home, away = context['game']['home']['name'], context['game']['away']['name']
    hs, aws = context['game']['home_score'], context['game']['away_score']
    compact = re.sub(r'\s+', '', title + '\n' + head[:2400])
    def score(first, n, second, m):
        return re.search(re.escape(first) + rf'(?:队)?(?:以)?{n}[-—–:：比]{m}(?:战胜|击败|不敌)?' + re.escape(second), compact)
    if not ('赛后' in compact and (score(away, aws, home, hs) or score(home, hs, away, aws))):
        raise ValueError('discussion_game_or_score_mismatch')
    # Provider dates are often midnight placeholders. Use the actual thread header.
    stamp = re.search(r'楼主\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)', head)
    if not stamp:
        raise ValueError('discussion_publication_time_unverified')
    published = datetime.fromisoformat(stamp[1]).replace(tzinfo=CHINA).astimezone(timezone.utc)
    tip = instant(postgame['record']['game']['scheduled_tipoff'])
    if published < tip or published > at or published > tip + timedelta(days=2):
        raise ValueError('discussion_outside_game_window')
    rows = comments(body)
    if not rows:
        raise ValueError('highlighted_replies_unavailable')
    return {'source_id': 'hupu-' + content_hash(url)[:18], 'url': url,
        'title': title or head.splitlines()[0].lstrip('# ').strip(),
        'published_at': published.isoformat(), 'captured_at': at.isoformat(),
        'body_sha256': content_hash(body), 'game_binding': 'teams_and_final_score_and_thread_timestamp',
        'comments': rows, 'classification': 'community_discussion',
        'not_a_fact_source': True}


def qualify_coach_report(row,context,postgame,at):
    url=thread_url(row['url']);body=plain(row.get('content'))
    head=body.split('这些回帖亮了',1)[0]
    game=context['game'];home,away=game['home']['name'],game['away']['name']
    compact=re.sub(r'\s+','',head[:4500])
    def matched(a,n,b,m):
        return re.search(re.escape(a)+rf'.{{0,12}}?{n}[-—–:：比]{m}.{{0,12}}?'+re.escape(b),compact)
    if not ('虎扑篮球资讯'in head and re.search(r'主帅|主教练|教练',head)and'赛后'in head
            and(matched(home,game['home_score'],away,game['away_score'])or
                matched(away,game['away_score'],home,game['home_score']))):
        raise ValueError('coach_report_game_binding_unverified')
    stamp=re.search(r'楼主\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)',head)
    if not stamp:raise ValueError('coach_report_time_unverified')
    published=datetime.fromisoformat(stamp[1]).replace(tzinfo=CHINA).astimezone(timezone.utc)
    tip=instant(postgame['record']['game']['scheduled_tipoff'])
    if not tip<=published<=min(at,tip+timedelta(days=2)):
        raise ValueError('coach_report_outside_game_window')
    article=re.search(r'虎扑\d+月\d+日讯',head)
    if not article:raise ValueError('coach_report_body_missing')
    statement=head[article.start():].split('来源：',1)[0].strip()[:1100]
    return {'source_id':'coach-'+content_hash(url)[:18],'url':url,'title':row.get('title')or'',
        'published_at':published.isoformat(),'captured_at':at.isoformat(),'statement':statement,
        'classification':'source_attributed_coach_statement','not_a_numeric_fact_source':True}


class HupuPostgameCollector:
    def __init__(self, root, *, transport=None, fetcher=None, clock=None):
        self.root = Path(root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.transport = transport
        self.fetcher = fetcher

    def collect(self, context, postgame, *, force=False):
        at = instant(self.clock())
        binding = {k: postgame[k] for k in ('game_id', 'version_id', 'base_prediction_version_id')}
        folder = self.root / postgame['version_id']
        pointer = folder / 'latest.json'
        if pointer.is_file() and not force:
            try:
                cached = json.loads(pointer.read_text(encoding='utf-8'))
                minutes = 90 if cached['status'] == 'available' else 15
                if cached.get('schema_version')==SCHEMA and cached['binding'] == binding and timedelta(0) <= at - instant(cached['captured_at']) < timedelta(minutes=minutes):
                    if content_hash({k:v for k,v in cached.items()if k!='snapshot_sha256'}) == cached['snapshot_sha256']:
                        return cached
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if self.transport is None:
            from v2.pregame_system.deepseek_web import DeepSeekWebTransport
            self.transport = DeepSeekWebTransport(root=folder/'acquisition', timeout=30)
        def fetch(url):
            if self.fetcher is not None:
                return self.fetcher(url)
            from v2.pregame_system.source_body_fetch import direct_fetch
            try:
                result = direct_fetch(url, 10)
                if thread_url(result.get('url')) == url and '这些回帖亮了' in result.get('content', ''):
                    return result
            except Exception:
                pass
            return self.transport.fetch_many([url], budget_seconds=35)[0]
        home, away = context['game']['home']['name'], context['game']['away']['name']
        hs, aws = context['game']['home_score'], context['game']['away_score']
        day = instant(postgame['record']['game']['scheduled_tipoff']).astimezone(CHINA).date().isoformat()
        queries = [f'site:bbs.hupu.com [赛后] {away}{aws}-{hs}{home} {day}',
                   f'site:bbs.hupu.com {away} {home} {aws} {hs} {day} 赛后 热评']
        report = {'schema_version': SCHEMA, 'binding': binding, 'captured_at': at.isoformat(),
            'status': 'missing', 'threads': [], 'postgame_reports':[], 'diagnostics': [],
            'usage': 'audience reactions and optional short attributed jokes; never basketball fact evidence',
            'model_calls': 0}
        seen = set()
        started = time.monotonic()
        for query in queries:
            if report['threads'] or time.monotonic()-started > 65:
                break
            try:
                found = self.transport.search({'query':query, 'max_results':6})
            except Exception as exc:
                report['diagnostics'].append({'operation':'search', 'error_type':type(exc).__name__})
                continue
            for candidate in found.get('results', []):
                if len(seen) >= 4 or len(report['threads']) >= MAX_THREADS or time.monotonic()-started > 85:
                    break
                try:
                    url = thread_url(candidate.get('url'))
                    if url in seen:
                        continue
                    seen.add(url)
                    row = fetch(url)
                    if thread_url(row.get('url') or url) != url:
                        raise ValueError('discussion_redirect_mismatch')
                    if not row.get('content'):
                        raise ValueError('discussion_body_unavailable')
                    row = {**row, 'url':url, 'title':row.get('title') or candidate.get('title')}
                    page=folder/'pages'/(content_hash(row)+'.json')
                    if not page.exists():
                        save(page,{'captured_at':at.isoformat(),'page':row})
                    accepted = qualify_page(row, context, postgame, at)
                    report['threads'].append(accepted)
                    # Exact main match thread is sufficient; do not scrape many pages for filler.
                    break
                except Exception as exc:
                    report['diagnostics'].append({'url':candidate.get('url'), 'error_type':type(exc).__name__,
                        'reason':str(exc)[:120] if isinstance(exc,ValueError)else'acquisition_failed'})
        report['status'] = 'available' if report['threads'] else 'missing'
        # One bounded search for an attributable coach account, kept out of fan opinions.
        if time.monotonic()-started<75:
            try:
                found=self.transport.search({'query':f'site:bbs.hupu.com {away} {home} {day} 赛后 主帅 转换 防守','max_results':4})
                for candidate in found.get('results',[])[:2]:
                    if time.monotonic()-started>100:break
                    try:
                        url=thread_url(candidate.get('url'));row=fetch(url)
                        if thread_url(row.get('url')or url)!=url:raise ValueError('coach_report_redirect_mismatch')
                        row={**row,'url':url,'title':row.get('title')or candidate.get('title')}
                        page=folder/'pages'/(content_hash(row)+'.json')
                        if not page.exists():save(page,{'captured_at':at.isoformat(),'page':row})
                        report['postgame_reports'].append(qualify_coach_report(row,context,postgame,at))
                        break
                    except Exception as exc:
                        report['diagnostics'].append({'operation':'coach_report','error_type':type(exc).__name__})
            except Exception as exc:
                report['diagnostics'].append({'operation':'coach_report_search','error_type':type(exc).__name__})
        report['snapshot_sha256'] = content_hash(report)
        save(folder/(at.strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]+'.json'), report)
        # Immutable acquisition snapshots stay intact; only this cache pointer advances.
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=folder,suffix='.tmp',delete=False)as stream:
            json.dump(report,stream,ensure_ascii=False,indent=2,allow_nan=False)
            temporary=stream.name
        os.replace(temporary,pointer)
        return report


def writer_view(report):
    """Only bounded discussion excerpts reach the Writer, never an entire forum page."""
    return {k: report[k] for k in ('status', 'captured_at', 'threads', 'usage', 'snapshot_sha256')}
