# ruff: noqa
"""阶段 C：页内 fetch 翻到底（或上限），并对热评拉回复；统计去重、耗时、异常。"""
import json, sys, time, random
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.sync_api import sync_playwright
aweme_id = sys.argv[1]; max_pages = int(sys.argv[2]); top_reply = int(sys.argv[3]) if len(sys.argv) > 3 else 3
JS = """async (url) => { const ac = new AbortController(); const tm = setTimeout(() => ac.abort(), 20000);
  let r; try { r = await fetch(url, {credentials:'include', signal: ac.signal}); } catch(e) { return {http: -1, len: 0, raw: 'fetch error: ' + e, comments: []} }
  const t = await r.text(); clearTimeout(tm);
  let j = null; try { j = JSON.parse(t) } catch(e) {}
  return {http: r.status, len: t.length, raw: j ? null : t.slice(0, 200), sc: j && j.status_code, has_more: j && j.has_more,
    cursor: j && j.cursor, total: j && j.total,
    comments: j && j.comments ? j.comments.map(c => ({cid: c.cid, text: c.text, digg: c.digg_count, rt: c.reply_comment_total,
      ct: c.create_time, ip: c.ip_label, nick: c.user && c.user.nickname})) : []} }"""
signed = []
with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path="/usr/bin/chromium", headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"]); print("chromium", b.version, flush=True)
    c = b.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width":1280,"height":800},
        user_agent="Mozilla/5.0 (X11; Linux " + __import__("platform").machine() + ") AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + b.version + " Safari/537.36")
    pg = c.new_page()
    pg.route("**/*", lambda rt: rt.abort() if rt.request.resource_type in ("media","font") else rt.continue_())
    first = {}
    pg.on("request", lambda req: first.setdefault("url", req.url) if "/comment/list/" in req.url and "/reply/" not in req.url else None)
    pg.on("request", lambda req: signed.append("a_bogus=" in req.url) if "/comment/list/" in req.url else None)
    t0 = time.time(); print('start', flush=True)
    pg.goto(f"https://www.douyin.com/video/{aweme_id}", wait_until="domcontentloaded", timeout=45000)
    for _ in range(40):
        if "url" in first: break
        pg.wait_for_timeout(250)
    print('first url captured' if 'url' in first else 'NO first url', round(time.time()-t0,1), flush=True)
    u = urlparse(first["url"]); base = {k: v[0] for k, v in parse_qs(u.query).items()}
    for k in ("a_bogus", "msToken", "X-Bogus"): base.pop(k, None)
    cmts, pages, cursor, anomalies, stop = {}, 0, 0, [], "max_pages"
    while pages < max_pages:
        q = dict(base, cursor=str(cursor), count="20")
        r = pg.evaluate(JS, urlunparse(u._replace(query=urlencode(q))))
        pages += 1
        print('page', pages, 'cursor', cursor, 'http', r['http'], 'sc', r.get('sc'), 'n', len(r['comments']), 'more', r.get('has_more'), round(time.time()-t0,1), flush=True)
        if r["http"] != 200 or r["sc"] not in (0, None) or r["raw"]:
            anomalies.append({k: r[k] for k in ("http","sc","raw","len")} | {"page": pages}); stop = "anomaly"; break
        for x in r["comments"]: cmts[x["cid"]] = x
        if not r["has_more"]: stop = f"has_more=0 at page {pages}"; break
        if not r["comments"]: anomalies.append({"empty_page": pages})
        cursor = r["cursor"]; total = r["total"]
        pg.wait_for_timeout(random.randint(800, 1600))
    elapsed_top = time.time() - t0
    # 回复
    reply_res = []
    rbase = {k: base[k] for k in base if k not in ("aweme_id", "cursor", "count", "item_type")}
    for x in sorted(cmts.values(), key=lambda x: -(x["rt"] or 0))[:top_reply]:
        got, rc, more, rp = 0, 0, 1, 0
        while more and rp < 5:
            q = dict(rbase, item_id=aweme_id, comment_id=x["cid"], cursor=str(rc), count="20")
            ru = urlunparse(u._replace(path="/aweme/v1/web/comment/list/reply/", query=urlencode(q)))
            r = pg.evaluate(JS, ru); rp += 1
            print('reply', x['cid'], rp, r['http'], r.get('sc'), len(r['comments']), flush=True)
            if r["http"] != 200 or r["sc"] not in (0, None) or r["raw"]:
                reply_res.append({"cid": x["cid"], "anomaly": {k: r[k] for k in ("http","sc","raw")}}); break
            got += len(r["comments"]); more = r["has_more"]; rc = r["cursor"]
            pg.wait_for_timeout(random.randint(800, 1600))
        reply_res.append({"cid": x["cid"], "reply_total": x["rt"], "got": got, "pages": rp})
    b.close()
out = {"aweme_id": aweme_id, "pages": pages, "unique": len(cmts), "total_field": total if pages else None, "stop": stop,
       "anomalies": anomalies, "elapsed_top_s": round(elapsed_top, 1), "all_signed": all(signed), "n_requests": len(signed),
       "replies": reply_res, "with_ip": sum(1 for x in cmts.values() if x["ip"]),
       "top3": [(x["digg"], x["text"][:24]) for x in sorted(cmts.values(), key=lambda x: -(x["digg"] or 0))[:3]]}
json.dump({"summary": out, "comments": list(cmts.values())}, open(f"/tmp/{aweme_id}_c.json", "w"), ensure_ascii=False)
print(json.dumps(out, ensure_ascii=False))
