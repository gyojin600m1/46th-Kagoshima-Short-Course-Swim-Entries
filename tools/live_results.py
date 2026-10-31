#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第46回 鹿児島県短水路水泳記録会（2026-11-01）の速報を取り込む。

  日水連 Live Results（https://live-results.swim.or.jp/）から「組・レーン」と「結果」を取り、
  index.html の DATA に書き込んで live.json を更新する。push は .github/workflows/live-results.yml がする。
  9/13 小学生学年別の watch/update.py と同じ取り方（API・欄の名前）。

  ・大会は当日にならないと Live Results に出ないので、毎回「大会一覧」から探す（鹿児島・11/1・短水路）。
    環境変数 GAME で大会コードを決め打ちもできる。
  ・Live Results の大会お知らせに転載の掲示が出たら止める（終了コード 4）。
  ・申込データと突き合わない行が多いときは書き込まない（別の大会を拾ったときなどに間違ったものを出さない）。
  ・順位は、競技No.の全組が終わってから、全区分まとめたタイム順で付ける（失格・棄権には付けない）。

終了コード: 0=変更あり 3=変更なし 4=掲示で停止 5=まだ大会が出ていない 2=エラー
  --dry  書き込まない（中身を表示するだけ）
"""
import json, os, re, sys, time, urllib.request, unicodedata, datetime
from concurrent.futures import ThreadPoolExecutor

ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML  = os.path.join(ROOT, 'index.html')
LIVE  = os.path.join(ROOT, 'live.json')
STATE = os.path.join(ROOT, 'tools', '.live_state.json')
DATE  = '2026-11-01'
API   = 'https://live-results.swim.or.jp/api/'
UA    = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
         '(KHTML, like Gecko) Chrome/128.0 Safari/537.36')
NOTICE_WORDS = ['転載', '再配布', '再利用', '大会運営を目的']
MARK, END = 'const DATA = /*__DATA__*/', '/*__DATA_END__*/'
JST = datetime.timezone(datetime.timedelta(hours=9))
DRY = '--dry' in sys.argv


def get(path, timeout=30):
    req = urllib.request.Request(API + path, headers={
        'User-Agent': UA, 'Accept': 'application/json', 'Referer': 'https://live-results.swim.or.jp/'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def load(p, d):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return d


IT = str.maketrans({'髙': '高', '﨑': '崎', '栁': '柳', '𠮷': '吉', '濵': '浜', '濱': '浜',
                    '邉': '辺', '邊': '辺', '齋': '斎', '齊': '斉', '冨': '富',
                    '德': '徳', '曻': '昇', '澤': '沢', '瀨': '瀬'})
nm = lambda s: unicodedata.normalize('NFKC', re.sub(r'[\s　]', '', str(s or ''))).translate(IT).upper()


def sec(t):
    t = re.sub(r'[^\d:.]', '', str(t or ''))
    m = re.match(r'^(?:(\d+):)?(\d+(?:\.\d+)?)$', t)
    return None if not m else int(m.group(1) or 0) * 60 + float(m.group(2))


def find_game(st):
    """鹿児島（46）・11/1・短水路の大会を探す。見つけたら覚えておく。"""
    if os.environ.get('GAME'):
        return os.environ['GAME']
    if st.get('game'):
        return st['game']
    games = []
    for path in ('games/member_group/46', 'games/'):
        try:
            games += get(path) or []
        except Exception as ex:
            print(f'大会一覧が取れない（{path}）: {ex}')
    for g in games:
        name = str(g.get('game_name') or '')
        if (str(g.get('member_group_code')) == '46' or str(g.get('game_code', '')).startswith('46')) \
           and str(g.get('date_start') or '') <= DATE <= str(g.get('date_end') or g.get('date_start') or '') \
           and '短水路' in name:
            st['game'] = str(g['game_code'])
            print(f'大会を見つけた: {g["game_code"]} {name}')
            return st['game']
    return None


def main():
    st = load(STATE, {})
    game = find_game(st)
    if not game:
        print('Live Results にこの大会がまだ出ていない')
        save_state(st)
        return 5

    # 発信源の大会お知らせに転載の掲示が出たら止める
    try:
        body = json.dumps(get(f'race_heats/messages/{game}'), ensure_ascii=False)
        if any(w in body for w in NOTICE_WORDS):
            print('HALT: Live Results の大会お知らせに掲示: ' + re.sub(r'\s+', ' ', body)[:160])
            return 4
    except Exception as ex:
        print(f'お知らせの確認に失敗（続行）: {ex}')

    html = open(HTML, encoding='utf-8').read()
    s = html.index(MARK) + len(MARK)
    d, e_ = json.JSONDecoder().raw_decode(html, s)
    assert html[e_:e_ + len(END)] == END
    ent = [e for e in d['entries'] if e.get('entryType') != 'リレーのみ']
    relays = d.get('relays', [])
    by_id, by_name = {}, {}
    for e in ent:
        no = (e.get('programNos') or [None])[0]
        if e.get('id'):
            by_id[(str(e['id']), no)] = e
        by_name.setdefault((nm(e['name']), no), []).append(e)

    rl = get(f'race_heats/race_list/{game}?game_date={DATE}')
    if not rl:
        print('組の一覧がまだ空')
        save_state(st)
        return 3

    done = set(tuple(x) for x in st.get('done', []))
    st['runs'] = int(st.get('runs', 0)) + 1
    full = (st['runs'] % 10 == 1)                 # 10回に1回は全部の組を取り直す（訂正を拾う）
    todo = [r for r in rl if full or (str(r['program_id']), str(r['heat'])) not in done]

    def fetch(r):
        pid, ht = str(r['program_id']), str(r['heat'])
        try:
            return r, get(f'result/race?game_code={game}&program_id={pid}&heat={ht}&raceStatus=9'), None
        except Exception as ex:
            return r, None, ex
    with ThreadPoolExecutor(3) as ex_:
        fetched = list(ex_.map(fetch, todo))

    def result_of(row):
        rt = (row.get('result_time') or '').strip()
        why = (row.get('reason_code_display_name') or '').strip()
        if not rt and not why:
            return None
        res = {}
        if rt:
            res['time'] = rt
        if why:
            res['note'] = why
        if row.get('new_personal_record') in (True, 'true', 1, '1'):
            res['rec'] = '自己ベスト'
        return res

    changed, warns, rows_seen, rows_hit, lane_diff = 0, [], 0, 0, []
    for r, rows, err in fetched:
        pid, ht = str(r['program_id']), str(r['heat'])
        if err is not None:
            warns.append(f'No.{r.get("display_program_id")} {ht}組が取れない: {err}')
            continue
        if not rows:
            continue
        if str(r.get('is_finished')) == 'true':
            done.add((pid, ht))
        no = int(r.get('display_program_id') or pid)
        is_relay = str(r.get('is_relay')) == 'true'
        for row in rows:
            rows_seen += 1
            if is_relay:
                team = row.get('swimmer_name') or row.get('entry_group_name1') or ''
                same = [x for x in relays if no in (x.get('programNos') or [])]
                cand = [x for x in same if nm(x['team']) == nm(team)] or \
                       [x for x in same if nm(x['team']) in nm(team) or nm(team) in nm(x['team'])]
                if len(cand) != 1:
                    warns.append(f'リレー No.{no} {ht}組 "{team}" が申込と突き合わない')
                    continue
                e = cand[0]
                members = [re.sub(r'[\s　]+', ' ', row.get(f'swimmer{i}_name') or '').strip() for i in range(1, 5)]
                members = [m for m in members if m]
                if members and e.get('members') != members:
                    e['members'] = members; changed += 1
            else:
                e = by_id.get((str(row.get('swimmer_code') or ''), no))
                if e is None:
                    c = by_name.get((nm(row.get('swimmer_name')), no)) or []
                    e = c[0] if len(c) == 1 else None
                if e is None:
                    warns.append(f'No.{no} {ht}組 {row.get("swimmer_name")}({row.get("swimmer_code")}) が申込に無い')
                    continue
            rows_hit += 1
            # 組・レーンは公式プログラムのものを使う（Live Results のレーン番号はプールによって lane / lane_adjusted がずれる）。
            # 食い違いは数えて知らせるだけ
            lanes = {str(row.get('lane') or '').strip(), str(row.get('lane_adjusted') or '').strip()}
            if e.get('heat') != int(ht) or str(e.get('lane')) not in lanes:
                lane_diff.append(f"No.{no} {e.get('name') or e.get('team')} プログラム {e.get('heat')}組{e.get('lane')} ／ 速報 {ht}組{row.get('lane')}")
            res = result_of(row)
            old = e.get('result') or {}
            if res and {k: v for k, v in old.items() if k != 'rank'} != res:
                if 'rank' in old: res['rank'] = old['rank']
                e['result'] = res; changed += 1

    st['done'] = sorted([list(x) for x in done])
    for w in warns[:8]:
        print('  △ ' + w)
    if lane_diff:
        print(f'  △ 組・レーンがプログラムと違う {len(lane_diff)}件: ' + ' / '.join(lane_diff[:3]))
    if rows_seen and rows_hit < rows_seen * 0.8:
        print(f'  ⚠ 申込と突き合わない行が多い（{rows_hit}/{rows_seen}）。別の大会かもしれないので書き込まない')
        save_state(st)
        return 2

    # 順位：競技No.の全組が終わってから。全区分まとめたタイム順。失格・棄権には付けない
    fin = {}
    for r in rl:
        n = int(r.get('display_program_id') or r['program_id'])
        fin.setdefault(n, []).append(str(r.get('is_finished')) == 'true')
    for no, flags in fin.items():
        rows = [e for e in ent + relays if no in (e.get('programNos') or [])]
        if not all(flags):
            for e in rows:
                if (e.get('result') or {}).pop('rank', None) is not None:
                    changed += 1
            continue
        timed = sorted([(sec(e['result']['time']), e) for e in rows
                        if (e.get('result') or {}).get('time') and not e['result'].get('note') and sec(e['result']['time'])],
                       key=lambda x: x[0])
        prev_t, prev_r = None, 0
        for i, (t, e) in enumerate(timed, 1):
            rk = prev_r if (prev_t is not None and abs(t - prev_t) < 1e-9) else i
            if e['result'].get('rank') != rk:
                e['result']['rank'] = rk; changed += 1
            prev_t, prev_r = t, rk
        for e in rows:
            if (e.get('result') or {}).get('note') and e['result'].pop('rank', None) is not None:
                changed += 1

    # お知らせ（速報のまとめ）と、公式速報へのボタン
    meta = d['meta']
    url = f'https://live-results.swim.or.jp/game-program/{game}'
    if meta.get('resultsUrl') != url:
        meta['resultsUrl'] = url; meta['resultsLabel'] = '公式の速報（Live Results）'; changed += 1
    pg = {p['no']: p for p in d['program']}
    nos = sorted(n for n in fin)
    fin_nos = [n for n in nos if all(fin[n])]
    n_time = sum(1 for e in ent + relays if (e.get('result') or {}).get('time'))
    n_note = sum(1 for e in ent + relays if (e.get('result') or {}).get('note'))
    base = ('組・レーンは公式プログラムどおり。順位は全区分（年齢区分）をまとめたタイム順で、このアプリで計算した速報値です。'
            '正式な結果は大会の公式発表をご確認ください。')
    if fin_nos:
        last = fin_nos[-1]; p = pg.get(last, {})
        head = f"{p.get('gender', '')} {p.get('distance', '')} {p.get('stroke', '')}".strip()
        top = sorted([e for e in ent + relays if last in (e.get('programNos') or []) and (e.get('result') or {}).get('rank') == 1],
                     key=lambda e: e.get('name') or e.get('team') or '')
        line = '　'.join(f"🥇 {e.get('name') or e.get('team')} {e['result']['time']}" for e in top)
        if len(fin_nos) == len(nos) and meta.get('liveWatch'):
            meta['liveWatch'] = False; changed += 1     # 全部終わったら、見ている画面の見回りも止める
        if len(fin_nos) == len(nos):
            note = (f"🏁 **全{len(nos)}競技 終了**　記録 {n_time}件" + (f"・失格/棄権 {n_note}件" if n_note else '')
                    + '\n' + base)
        else:
            note = (f"🔴 **速報中**　終了 {len(fin_nos)}/{len(nos)}競技・記録 {n_time}件"
                    + (f"・失格/棄権 {n_note}件" if n_note else '')
                    + f"\n**直近 No.{last} {head}**" + (('\n' + line) if line else '') + '\n' + base)
        if meta.get('notice') != note:
            meta['notice'] = note; changed += 1

    n_res = sum(1 for e in ent + relays if e.get('result'))
    print(f'大会 {game}・組 {len(rl)}（取得 {len(todo)}）・突き合わせ {rows_hit}/{rows_seen}・結果 {n_res}件・終了 {len(fin_nos)}/{len(nos)}競技・変更 {changed}')
    save_state(st)
    if not changed:
        return 3
    if DRY:
        print('（--dry なので書き込まない）')
        return 0
    html = html[:s] + json.dumps(d, ensure_ascii=False, separators=(',', ':')) + html[e_:]
    with open(HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    with open(LIVE, 'w', encoding='utf-8') as f:
        json.dump({'n': n_res, 't': datetime.datetime.now(JST).strftime('%H:%M')}, f)
    return 0


def save_state(st):
    try:
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(st, f)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as ex:
        print('想定外のエラー: ' + repr(ex)[:300])
        sys.exit(2)
