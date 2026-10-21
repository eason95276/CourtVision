"""Render tomorrow's human-selected slate in README. No providers or generation."""
import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
START = '<!-- daily-picks:start -->'
END = '<!-- daily-picks:end -->'
TZ = ZoneInfo('Asia/Shanghai')
STATUSES = {
    'awaiting_preview': '报告待生成',
    'awaiting_review': '单场试稿待人工确认',
    'preview_failed': '新试稿未通过校验，保留原报告',
    'approved_draft': '文风稿已确认，正式版尚未替换',
    'awaiting_approval': '等待首篇试稿确认后再更新',
    'published': '本轮报告已发布',
    'failed': '本轮更新失败，保留原报告',
}


def instant(text):
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('timezone_required')
    return value


def load_selection(root):
    data = json.loads((root / 'config/daily_picks.json').read_text(encoding='utf-8'))
    if data.get('timezone') != 'Asia/Shanghai':
        raise ValueError('selection_timezone_mismatch')
    for day, picks in data['slates'].items():
        if date.fromisoformat(day).isoformat() != day or not isinstance(picks, list):
            raise ValueError('invalid_selection_date')
        ids = []
        for item in picks:
            gid = item['game_id']
            if not re.fullmatch(r'(espn|nba):\d+', gid) or item['update_status'] not in STATUSES:
                raise ValueError('invalid_selected_game')
            ids.append(gid)
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate_selected_game')
    return data


def render_board(selection, records, as_of):
    if as_of.tzinfo is None:
        raise ValueError('timezone_required')
    tomorrow = as_of.astimezone(TZ).date() + timedelta(days=1)
    lines = [f'## 明日预测名单 · {tomorrow.isoformat()}', '',
             '北京时间。这里只列人工选定的比赛；名单更新不会触发 Research 或模型生成。', '']
    picks = selection['slates'].get(tomorrow.isoformat(), [])
    rows, moved = [], []
    for pick in picks:
        gid = pick['game_id']
        record = records.get(gid)
        if record is None:
            raise ValueError('selected_game_not_in_public_records:' + gid)
        game = record['game']
        tipoff = instant(game['tipoff']).astimezone(TZ)
        name = f"{game['away']['name_zh']} @ {game['home']['name_zh']}"
        name = name.replace('|', '\\|').replace('\n', ' ')
        if tipoff.date() != tomorrow:
            moved.append(f'{name}：当前开赛日期为 {tipoff:%Y-%m-%d %H:%M}，不计入明日名单。')
            continue
        version = (record.get('prediction') or {}).get('version')
        if version:
            cutoff = instant(version['input_cutoff_at']).astimezone(TZ)
            report = f'已有正式报告；资料截止 {cutoff:%m-%d %H:%M}'
        else:
            report = '事实页，尚无正式报告'
        url = 'https://nba.weitao.si/#game/' + quote(gid, safe='')
        update_label = STATUSES[pick['update_status']]
        if not version and pick['update_status'] in {'failed', 'preview_failed'}:
            update_label = '本轮生成未通过，尚无正式报告'
        rows.append((tipoff, f'| {tipoff:%H:%M} | [{name}]({url}) | {report} | {update_label} |'))
    if rows:
        lines += ['| 开赛 | 比赛 | 当前公开内容 | 本轮更新 |',
                  '|---|---|---|---|', *[row[1] for row in sorted(rows)]]
    else:
        lines += ['明日尚无已选定且日期匹配的比赛。不沿用昨日名单，也不自动挑选其他比赛。']
    if moved:
        lines += ['', *moved]
    lines += ['', '每日计划于北京时间 12:10 更新，也会随选场或公开报告变动更新。',
              '选场记录：[daily_picks.json](config/daily_picks.json)。正式报告的资料时间单独列出，名单刷新不代表情报已刷新。']
    return '\n'.join(lines)


def replace_board(readme, board):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError('readme_board_markers_not_unique')
    before, rest = readme.split(START)
    _, after = rest.split(END)
    return before + START + '\n' + board + '\n' + END + after


def update(root=ROOT, as_of=None):
    selection = load_selection(root)
    index = json.loads((root / 'public_data/index.json').read_text(encoding='utf-8'))
    records = {}
    schedule=root/'public_data/schedule.json'
    if schedule.is_file():
        for game in json.loads(schedule.read_text(encoding='utf-8')).get('games',[]):
            records[game['id']]={'game':game,'prediction':{'version':None}}
    public = (root / 'public_data').resolve()
    for item in index['games']:
        path = (public / item['file']).resolve()
        if not path.is_relative_to(public / 'games'):
            raise ValueError('public_record_path_outside_games')
        record = json.loads(path.read_text(encoding='utf-8'))
        if record['game']['id'] != item['game_id']:
            raise ValueError('public_record_identity_mismatch')
        records[item['game_id']] = record
    board = render_board(selection, records, as_of or datetime.now(timezone.utc))
    path = root / 'README.md'
    original = path.read_text(encoding='utf-8')
    revised = replace_board(original, board)
    if revised != original:
        path.write_text(revised, encoding='utf-8')
    print(json.dumps({'changed': revised != original, 'generation_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of', type=instant, help='Timezone-qualified time for deterministic verification.')
    args = parser.parse_args()
    update(as_of=args.as_of)
