"""Build the existing native product renderer; no local backend or models."""
import argparse,json,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def build(output):
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    for name in ('index.html','product.css','editorial.css','final.css','app.js','editorial.js'):
        source=ROOT/'v2/frontend/static'/name;text=source.read_text(encoding='utf-8')
        if name=='index.html':text=text.replace('<script src="app.js"','<script src="static-data.js" defer></script>\n    <script src="app.js"')
        if name=='app.js':
            start=text.index('const LOCAL_LOGO_TEAMS = new Set(');end=text.index('\n\nconst PLAYER_DISPLAY_NAMES',start)
            text=text[:start]+'const LOCAL_LOGO_TEAMS = new Set();'+text[end:]
            text=text.replace('const dates = [today, tomorrow];','const dates = state.data.public_archive ? ["published"] : [today, tomorrow];')
            text=text.replace('      : today;', '      : dates[0];')
            text=text.replace('displayDate(`${date}T12:00:00+08:00`, false)', 'state.data.public_archive ? "赛前 / 赛后" : displayDate(`${date}T12:00:00+08:00`, false)')
            text=text.replace('chinaDateKey(game.tipoff) === state.selectedDate','state.data.public_archive || chinaDateKey(game.tipoff) === state.selectedDate')
            text=text.replace('index === 0 ? "今天" : "明天"','state.data.public_archive ? "已发布比赛" : (index === 0 ? "今天" : "明天")')
            text=text.replace('byId("coverage-note").textContent = statusCopy[window.status] || "赛程状态暂不可用 · 北京时间";','byId("coverage-note").textContent = state.data.public_archive ? "已发布的真实赛前分析与赛后复盘 · 北京时间" : statusCopy[window.status];')
            text=text.replace('byId("product-notice").textContent = "比分与赛程独立更新；预测只显示已正式发布的第25秒判断。";','byId("product-notice").textContent = state.data.public_archive ? "公开阅读站 · 内容在本地生成后发布，不现场调用AI。" : "预测只显示正式发布判断。";')
            text=text.replace('verdict.append(refresh);','if (!window.COURTVISION_PUBLIC) verdict.append(refresh);')
        if name=='final.css':text+='\n/* Public read-only release, no internal diagnostics. */\n.technical-details{display:none}\n'
        (out/name).write_text(text,encoding='utf-8')
    shutil.copy2(ROOT/'web/static-data.js',out/'static-data.js')
    shutil.copytree(ROOT/'public_data',out/'public_data',dirs_exist_ok=True)
    (out/'.nojekyll').touch()
    print(json.dumps({'built':str(out),'games':len(json.loads((out/'public_data/index.json').read_text(encoding='utf-8'))['games'])}))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default=str(ROOT/'_site'));a=p.parse_args();build(a.output)
