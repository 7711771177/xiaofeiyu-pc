# -*- coding: utf-8 -*-
"""
小肥鱼帮你做作业 · 电脑版
学习通作业截止提醒 —— 桌面版（无第三方依赖，Python 标准库即可运行）

启动：双击「启动小肥鱼.bat」
停止：双击「停止小肥鱼.bat」

说明：本程序完全在你本机运行，数据只存在同目录的 data.json 里。
      cookie 只用于向学习通请求你自己的课程与作业列表。
"""
import base64
import hashlib
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))

# pythonw.exe 没有控制台，sys.stdout/stderr 会是 None，print 会直接抛异常
if sys.stdout is None or sys.stderr is None:
    _f = open(os.path.join(ROOT, "app.log"), "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _f
    if sys.stderr is None:
        sys.stderr = _f

DATA_FILE = os.path.join(ROOT, "data.json")
UI_FILE = os.path.join(ROOT, "ui.html")
LOG_FILE = os.path.join(ROOT, "app.log")
VERSION = "1.5.0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

COURSE_LIST = ("https://mooc1-1.chaoxing.com/visit/courselistdata?"
               "courseType=1&courseFolderId=0&baseEducation=0&superstarClass=0")
WORK_ENC = ("https://mooc1-1.chaoxing.com/mooc-ans/visit/stucoursemiddle?"
            "courseid={cid}&clazzid={clazz}&vc=1&cpi={cpi}&ismooc2=1&v=2")
WORK_LIST = ("https://mooc1.chaoxing.com/mooc2/work/list?courseId={cid}"
             "&classId={clazz}&cpi={cpi}&ut=s&t={t}&stuenc=&enc={enc}")
TASK_PAGE = ("https://mooc1.chaoxing.com/mooc2/work/task?workId={wid}"
             "&courseId={cid}&classId={clazz}&cpi={cpi}")
LOGIN_URL = "https://i.chaoxing.com"

REMAIN_RE = re.compile(r"剩余\s*(?:(\d+)\s*天)?\s*(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分钟)?")
DONE_WORDS = ("已交", "已提交", "已完成", "已批阅", "待批阅")

FREE_ENDPOINT = "https://text.pollinations.ai/openai"
FREE_MODEL = "openai"

SYS_ANSWER = ("你是学习通的作业解题助手。用户会给你一份作业的题目，"
              "请逐题给出可以直接提交的答案。只输出 JSON 数组，不要任何解释文字。"
              '格式：[{"i":1,"answer":"B"}]')
SYS_EXPLAIN = ("你是学习辅导助手。用户给你一道题，请讲清考点、解题思路、每个选项对错的原因，"
               "并给出你认为的正确答案。用中文，条理清晰，不要输出 JSON。")

# ---------------------------------------------------------------- 基础设施

_lock = threading.RLock()
_data_cache = None
_remind_seen = set()


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def default_data():
    return {
        "settings": {
            "agreed": False,
            "interval": 30,
            "lead24": True,
            "lead2": True,
            "lead0": False,
            "showDone": False,
            "aiMode": "custom",
            "aiBase": "https://api.deepseek.com/v1",
            "aiModel": "deepseek-chat",
            "aiKey": "",
            "autoSync": True,
            "layout": "mica",      # mica=云母悬浮卡片 / official=原版平铺玻璃
            "glass": "std",        # clear=清透 / std=标准 / frost=磨砂
            "glow": True,          # 地面光斑
        },
        "cookie": "",
        "account": "",
        "courses": [],
        "tasks": [],
        "drafts": {},              # workId -> 是否已暂存（有作答草稿）
        "lastSync": 0,
        "lastError": "",
        "lastAccount": "",
    }


def load_data():
    global _data_cache
    if _data_cache is not None:
        return _data_cache
    data = default_data()
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k == "settings" and isinstance(v, dict):
                        data["settings"].update(v)
                    else:
                        data[k] = v
        except Exception as e:
            log("读取 data.json 失败：%r" % (e,))
    _data_cache = data
    return data


def save_data():
    global _data_cache
    with _lock:
        data = _data_cache or load_data()
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------- 网络请求

def http_get(url, cookie="", referer="", timeout=25):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    req.add_header("Accept", "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8")
    if cookie:
        req.add_header("Cookie", cookie)
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "charset" in ctype.lower():
                m = re.search(r"charset=([\w-]+)", ctype, re.I)
                enc = m.group(1) if m else "utf-8"
            else:
                enc = "utf-8"
            try:
                return resp.geturl(), raw.decode(enc, "replace")
            except LookupError:
                return resp.geturl(), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return url, body or ("HTTP %s" % e.code)
    except Exception as e:
        raise RuntimeError("网络请求失败：%s" % e)


def looks_html(raw, ctype=""):
    """判断上游返回的到底是不是 HTML 页面。
    学习通的 ajax 接口经常用 text/html 的 Content-Type 返回 JSON
    （例如 {"msg":"无效的课程！","status":false}），这种绝不能注入 base/shim，
    否则前端的 dataType:"json" 直接解析失败，界面只显示"保存失败"。"""
    if isinstance(raw, str):
        head = raw[:400].lstrip().lower()
    else:
        head = (raw or b"")[:400].lstrip().lower()
    if isinstance(head, bytes):
        for sig in (b"<!doctype html", b"<html", b"<head", b"<body", b"<!--"):
            if head.startswith(sig):
                return True
        return False
    for sig in ("<!doctype html", "<html", "<head", "<body", "<!--"):
        if head.startswith(sig):
            return True
    return False


def maybe_inject_proxy_body(code, out_ct, raw_out, final, origin, cookie=""):
    """代理直通接口用：只有真 HTML 才注入，JSON/图片/其它原样返回。"""
    if not (out_ct or "").lower().startswith("text/html"):
        return out_ct, raw_out
    if not looks_html(raw_out):
        return out_ct, raw_out
    try:
        html = raw_out.decode("utf-8", "replace")
    except Exception:
        html = str(raw_out)
    return "text/html; charset=utf-8", inject_html(html, final, origin, cookie).encode("utf-8")


def inject_html(html, final, origin, cookie=""):
    """把 base + 代理 shim 注入学习通返回的 HTML（/api/page 与 /api/x 共用）"""
    html = rewrite_imgs(html, final or "", cookie, origin)
    shim = PROXY_SHIM % (final or "").replace('"', "&quot;")
    if re.search(r"<head[^>]*>", html[:8000], re.I):
        html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + shim, html,
                      count=1, flags=re.I)
    else:
        html = shim + html
    return html


def http_proxy(url, cookie="", method="GET", body=None, ctype=None,
               referer="https://i.chaoxing.com", timeout=40, origin_hdr=None):
    """把页面里的 ajax / 表单提交原样转发到学习通，再把结果原样返回给浏览器。"""
    req = urllib.request.Request(url, data=body, method=method.upper())
    req.add_header("User-Agent", UA)
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    req.add_header("Accept", "application/json, text/javascript, text/html;q=0.9, */*;q=0.8")
    if cookie:
        req.add_header("Cookie", cookie)
    if referer:
        req.add_header("Referer", referer)
    if origin_hdr:
        req.add_header("Origin", origin_hdr)
        req.add_header("X-Requested-With", "XMLHttpRequest")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.geturl(), resp.status,
                    resp.headers.get("Content-Type") or "text/html; charset=utf-8",
                    resp.read())
    except urllib.error.HTTPError as e:
        return (url, e.code,
                e.headers.get("Content-Type") if e.headers else "text/plain; charset=utf-8",
                e.read() or b"")
    except Exception as e:
        return (url, 502, "text/plain; charset=utf-8",
                ("代理请求失败：%s" % e).encode("utf-8"))


PROXY_SHIM = """
<base href="%s">
<script>
(function(){
  var ORIGIN = location.origin;
  function abs(u){ try { return new URL(u, document.baseURI).href; } catch(e){ return u; } }
  function needProxy(u){
    if (!u) return null;
    var a = abs(u);
    if (!/^https?:\\/\\//i.test(a)) return null;
    if (a.indexOf(ORIGIN) === 0) return null;
    return /chaoxing\\.com|chaoxing\\.net/i.test(a) ? a : null;
  }
  function wrap(a){ return ORIGIN + "/api/x?url=" + encodeURIComponent(a)
      + "&ref=" + encodeURIComponent(document.baseURI); }

  var ox = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u){
    var p = needProxy(u);
    if (p) u = wrap(p);
    return ox.call(this, m, u);
  };

  if (window.fetch){
    var of = window.fetch;
    window.fetch = function(input, init){
      try {
        var u = (typeof input === "string") ? input : (input && input.url) || "";
        var p = needProxy(u);
        if (p) return of.call(window, wrap(p), init);
      } catch(e){}
      return of.apply(window, arguments);
    };
  }

  function fixForms(){
    var fs = document.getElementsByTagName("form");
    for (var i = 0; i < fs.length; i++){
      var a = fs[i].getAttribute("action");
      var p = needProxy(a);
      if (p) fs[i].setAttribute("action", wrap(p));
    }
  }
  function formBody(f){
    try { return new URLSearchParams(new FormData(f)).toString(); } catch(e){ return ""; }
  }
  function tell(kind, text){
    try { parent.postMessage({src: "xiaofeiyu", kind: kind, text: text || ""}, "*"); } catch(e){}
  }
  function sendForm(f, url){
    tell("submitting", "");
    var xhr = new XMLHttpRequest();
    xhr.open("POST", ORIGIN + "/api/x?url=" + encodeURIComponent(url), true);
    try { xhr.setRequestHeader("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8"); } catch(e){}
    xhr.onload = function(){
      var t = "";
      try {
        t = String(xhr.responseText || "").replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").slice(0, 120);
      } catch(e){}
      tell("submitted", t);
      setTimeout(function(){
        try {
          location.href = ORIGIN + "/api/page?url=" + encodeURIComponent(document.baseURI);
        } catch(e){
          try { location.reload(); } catch(e2){}
        }
      }, 1500);
    };
    xhr.onerror = function(){ tell("submitfail", "网络请求失败"); };
    try { xhr.send(formBody(f)); } catch(e){ tell("submitfail", String(e)); }
  }
  var os = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function(){
    var a = this.getAttribute("action") || "";
    var tgt = abs(a);
    if (tgt.indexOf("http") !== 0 || tgt.indexOf(ORIGIN) === 0 || tgt.indexOf("chaoxing") < 0){
      return os.call(this);
    }
    try { sendForm(this, tgt); }
    catch(e){ try { os.call(this); } catch(e2){} }
  };
  try { window.open = function(){ return null; }; } catch(e){}
  document.addEventListener("DOMContentLoaded", fixForms);
  setTimeout(fixForms, 800);
  setTimeout(fixForms, 2500);

  // 图片：学习通 CDN 有防盗链，浏览器直连会裂图；统一改走本机 /api/img
  function imgProxy(u){
    var a = abs(u);
    if (!/^https?:\\/\\//i.test(a)) return null;
    if (a.indexOf(ORIGIN) === 0) return null;
    if (!/chaoxing|ananas/i.test(a)) return null;
    return ORIGIN + "/api/img?url=" + encodeURIComponent(a) + "&ref=" + encodeURIComponent(document.baseURI);
  }
  function fixImg(el){
    if (!el || el.tagName !== "IMG") return;
    var cur = el.getAttribute("src") || "";
    // 已经换成代理地址就直接返回（必须用 data-px 比对：只看当前 src 会在
    // MutationObserver 里自我触发，形成死循环把渲染线程卡死）
    if (el.getAttribute("data-px") && cur === el.getAttribute("data-px")) return;
    var raw = el.getAttribute("data-orig") || cur || el.getAttribute("data-original");
    if (!raw || raw.indexOf("/api/img") === 0 || raw.indexOf("data:") === 0) return;
    var p = imgProxy(raw);
    if (!p) return;
    if (!el.getAttribute("data-orig")) el.setAttribute("data-orig", raw);
    if (cur !== p){
      el.setAttribute("data-px", p);
      el.setAttribute("src", p);
    }
  }
  function fixImgs(){
    var list = document.images;
    for (var i = 0; i < list.length; i++) fixImg(list[i]);
  }
  var fixTimer = null;
  function scheduleFix(){
    if (fixTimer) return;
    fixTimer = setTimeout(function(){ fixTimer = null; fixImgs(); }, 80);
  }
  document.addEventListener("DOMContentLoaded", fixImgs);
  setTimeout(fixImgs, 300);
  setTimeout(fixImgs, 1200);
  setTimeout(fixImgs, 3000);
  try {
    new MutationObserver(function(){
      // 只做「攒一下再统一扫」，不在回调里逐条改 DOM（避免自触发 + 长任务卡死）
      scheduleFix();
    }).observe(document.documentElement, {childList:true, subtree:true,
                                          attributes:true, attributeFilter:["src","data-src"]});
  } catch(e){}
  window.addEventListener("load", function(){ setTimeout(fixImgs, 200); setTimeout(fixImgs, 1500); });
})();
</script>
"""


IMG_HOST_RE = re.compile(r"(^|\.)(ananas\.chaoxing\.com|chaoxing\.com|chaoxing\.net|chaoxing\.com\.cn)$", re.I)
IMG_DIR = os.path.join(ROOT, "imgcache")
IMG_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
                "image/svg+xml": ".svg"}
# 1x1 透明 GIF：抓不到时回这个，避免页面上一堆裂图
BLANK_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
             b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
             b"\x00\x02\x02D\x01\x00;")


def http_bin(url, cookie="", referer="", timeout=30):
    """抓二进制（图片），返回 (final, code, ctype, bytes)。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
    if referer:
        req.add_header("Referer", referer)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.geturl(), r.status, (r.headers.get("Content-Type") or ""), r.read()
    except urllib.error.HTTPError as e:
        return url, e.code, "", b""
    except Exception:
        return url, 0, "", b""


def is_img_host(url):
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return bool(IMG_HOST_RE.search(host))


def img_proxy_url(url, ref="", origin=""):
    path = ("/api/img?url=" + urllib.parse.quote(url, safe="")
            + "&ref=" + urllib.parse.quote(ref, safe=""))
    return (origin.rstrip("/") + path) if origin else path


IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)", re.I)


def looks_like_image(url):
    """只有真图片才代理。学习通 CDN 上还挂着 js/css，别把它们也塞进 /api/img。"""
    if not url.startswith("http"):
        return False
    if not is_img_host(url):
        return False
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    if "ananas" in host:          # 图床，基本都是图片
        return True
    path = urllib.parse.urlsplit(url).path or ""
    if IMG_EXT_RE.search(path):
        return True
    if path.lower().endswith((".js", ".css", ".json", ".html", ".htm")):
        return False
    return False


def proxy_one(val, base, origin=""):
    v = (val or "").strip()
    if not v or v.startswith("data:") or v.startswith("/api/img"):
        return None
    try:
        absu = urllib.parse.urljoin(base, v)
    except Exception:
        return None
    if not looks_like_image(absu):
        return None
    return img_proxy_url(absu, base, origin)


ATTR_RE = re.compile(r'\b(src|data-original|data-src)(\s*=\s*)(["\'])([^"\']+)\3', re.I)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)


def rewrite_imgs(html, base, cookie="", origin=""):
    """页面里的外链图片统一改走本机 /api/img（带 Cookie/Referer 才能过防盗链）。

    只处理 <img> 标签内的 src/data-original/data-src，外加内联样式里的
    background-image:url(...)，避免误伤 script/link 之类的资源。
    """
    def fix_tag(mt):
        tag = mt.group(0)

        def repl(m):
            new = proxy_one(m.group(4), base, origin)
            if not new:
                return m.group(0)
            return "%s%s%s%s%s" % (m.group(1), m.group(2), m.group(3), new, m.group(3))
        return ATTR_RE.sub(repl, tag)

    out = IMG_TAG_RE.sub(fix_tag, html)

    def repl_css(m):
        new = proxy_one(m.group(2), base, origin)
        if not new:
            return m.group(0)
        return "url(%s%s%s)" % (m.group(1), new, m.group(1))

    out = re.sub(r'url\(\s*(["\']?)([^)"\']+)\1\s*\)', repl_css, out)
    return out


def serve_img_bytes(url, ref=""):
    """取图（带磁盘缓存），返回 (ctype, bytes)。"""
    try:
        os.makedirs(IMG_DIR, exist_ok=True)
    except Exception:
        pass
    # Referer 必须是学习通的页面，否则图床直接 403；本机地址一律替换掉
    try:
        rhost = (urllib.parse.urlsplit(ref or "").hostname or "").lower()
    except Exception:
        rhost = ""
    if (not ref) or rhost in ("127.0.0.1", "localhost", "") or rhost.endswith(".local"):
        ref = LOGIN_URL
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    ctype = ""
    for ext in (".png", ".jpg", ".gif", ".webp", ".bmp", ".svg"):
        p = os.path.join(IMG_DIR, key + ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            d = load_data()
            return {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp", ".bmp": "image/bmp",
                    ".svg": "image/svg+xml"}[ext], open(p, "rb").read()
    d = load_data()
    final, code, ctype, data = http_bin(url, d.get("cookie", ""), ref or LOGIN_URL, timeout=30)
    if not data:
        return "image/gif", BLANK_GIF
    base_mime = (ctype or "").split(";")[0].strip().lower()
    if not base_mime.startswith("image/"):
        # 防盗链/登录页返回的是 HTML，别当图片塞回去
        return "image/gif", BLANK_GIF
    ext = IMG_MIME_EXT.get(base_mime, ".img")
    try:
        with open(os.path.join(IMG_DIR, key + ext), "wb") as f:
            f.write(data)
    except Exception:
        pass
    return base_mime, data


VISION_PROMPT = (
    "这是学习通作业里的一张图片。请把图片内容完整转写出来：\n"
    "1) 如果是表格，用 markdown 表格原样转写（表头、每一行数据都要，不要省略）；\n"
    "2) 如果是代码/公式/图表，按原样的文字与结构转写；\n"
    "3) 最后用一两句话说明这张图在这道题里起什么作用（例如「提供了 student/course/SC 三张表的结构与数据，"
    "SQL 题要据此作答」）。\n"
    "只输出转写内容和一句用途说明，不要编造图里没有的信息，看不清的地方写（看不清）。"
)


def img_data_uri(url, ref=""):
    ctype, data = serve_img_bytes(url, ref)
    if not data or data == BLANK_GIF:
        return None
    if len(data) > 6 * 1024 * 1024:
        return None
    return "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode())


def extract_content(raw):
    """从 OpenAI 兼容响应里取文本（普通 JSON 或 SSE 流都吃）。"""
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        return (obj["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        pass
    if "data:" in raw:
        parts = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk in ("", "[DONE]"):
                continue
            try:
                j = json.loads(chunk)
                parts.append(j["choices"][0]["delta"].get("content") or "")
            except Exception:
                continue
        return "".join(parts).strip()
    return raw.strip()


DOUBAO_EXE = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Doubao", "Application", "Doubao.exe")
DOUBAO_PS1 = os.path.join(ROOT, "doubao.ps1")
DOUBAO_READ_PROMPT = (
    "这是豆包聊天窗口下半部分的截图。只提取最下面那一条机器人回复的正文，"
    "忽略上面残留的历史消息、搜索卡片、按钮、输入框里的提示文字。"
    "原样保留数学公式、选项、答案和解析，不要自己改写、不要补全。"
    "如果最后一条回复被截断了，就只输出看得见的部分。"
)


SHOT_PORT = 9333
SHOT_PROFILE = os.path.join(os.environ.get("LOCALAPPDATA") or ROOT, "XiaofeiyuPC-shot")
_SHOT = {"ws": None, "proc": None}


def _shot_browser():
    """懒启动一个无头 Edge/Chrome，专门用来给「整题」截图。"""
    st = _SHOT
    if st["ws"] is not None:
        try:
            st["ws"].call("Runtime.evaluate", {"expression": "1"}, timeout=5)
            return st["ws"]
        except Exception:
            try:
                st["ws"].close()
            except Exception:
                pass
            st["ws"] = None
    br = find_browser()
    if not br:
        raise RuntimeError("没找到 Edge 或 Chrome，无法给题目截图")
    os.makedirs(SHOT_PROFILE, exist_ok=True)
    proc = subprocess.Popen(
        [br, "--headless=new", "--remote-debugging-port=%d" % SHOT_PORT,
         "--user-data-dir=" + SHOT_PROFILE, "--no-first-run", "--no-default-browser-check",
         "--disable-gpu", "--hide-scrollbars", "--window-size=1400,1100", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    page = None
    for _ in range(80):
        time.sleep(0.3)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % SHOT_PORT, timeout=1) as r:
                lst = json.loads(r.read().decode("utf-8", "replace"))
            for t in lst:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    page = t
                    break
            if page:
                break
        except Exception:
            continue
    if not page:
        raise RuntimeError("截图用的无头浏览器没起来（端口 %d 被占用？）" % SHOT_PORT)
    ws = _WS(page["webSocketDebuggerUrl"])
    ws.call("Page.enable")
    ws.call("Runtime.enable")
    st["ws"], st["proc"] = ws, proc
    return ws


def _ev(ws, expr, timeout=30):
    r = ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, timeout=timeout)
    return (r.get("result") or {}).get("value")


def capture_question(page_url, idx, scale=2.0, max_h=2600):
    """把作业页里第 idx 道题（题干 + 选项）整块截成一张 PNG，返回 bytes。
    这是用户要的效果：不是零散的图片，而是「这一题的完整样子」。"""
    ws = _shot_browser()
    origin = "http://127.0.0.1:%d" % PORT_NOW[0]
    target = origin + "/api/page?url=" + urllib.parse.quote(page_url, safe="")
    ws.call("Emulation.setDeviceMetricsOverride",
            {"width": 1400, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
    ws.call("Page.navigate", {"url": target})
    ready = False
    for _ in range(60):
        time.sleep(0.3)
        try:
            n = _ev(ws, "document.querySelectorAll('.questionLi').length") or 0
            if n > idx:
                ready = True
                break
        except Exception:
            pass
    if not ready:
        raise RuntimeError("作业页里的题目没加载出来（第 %d 题），点「刷新页面」再试" % (idx + 1))
    for _ in range(25):
        try:
            if _ev(ws, "Array.prototype.every.call(document.images, function(i){return i.complete})"):
                break
        except Exception:
            pass
        time.sleep(0.3)
    # 把 fixed/sticky 的悬浮层藏掉（顶部「作业/暂时保存/提交」条、右侧答题卡），截图才干净
    try:
        _ev(ws, """(function(){
            var all=document.body.getElementsByTagName('*');
            for (var i=0;i<all.length;i++){
                var cs=getComputedStyle(all[i]);
                if (cs.position==='fixed' || cs.position==='sticky'){ all[i].style.visibility='hidden'; }
            }
            window.scrollTo(0,0);
            return 1;
        })()""")
    except Exception:
        pass
    time.sleep(0.4)
    info = _ev(ws, """(function(){
        var qs=document.querySelectorAll('.questionLi');
        var q=qs[%d]; if(!q) return null;
        q.scrollIntoView({block:'start'});
        var r=q.getBoundingClientRect();
        return {x:Math.round(r.left+window.scrollX), y:Math.round(r.top+window.scrollY),
                w:Math.round(r.width), h:Math.round(r.height)};
    })()""" % idx)
    if not info or info.get("w", 0) < 50 or info.get("h", 0) < 30:
        raise RuntimeError("这一题在页面上没有可截取的区域")
    pad_x, pad_top = 8, 6
    info["x"] = max(0, info["x"] - pad_x)
    info["y"] = max(0, info["y"] - pad_top)
    info["w"] = info["w"] + pad_x * 2
    info["h"] = info["h"] + pad_top
    w = max(200, min(info["w"], 1400))
    h = max(60, min(info["h"], max_h))
    if h > 1200:
        scale = min(scale, 1.5)
    shot = ws.call("Page.captureScreenshot",
                   {"format": "png",
                    "clip": {"x": info["x"], "y": info["y"], "width": w, "height": h, "scale": scale},
                    "captureBeyondViewport": True},
                   timeout=90)
    data = base64.b64decode(shot.get("data") or "")
    if len(data) < 3000:
        raise RuntimeError("截出来的题目图太小，可能页面还没渲染好")
    return data


def crop_bottom(raw, keep=0.62):
    """豆包窗口截图只留下面一部分：最新回答在最下面，上面是历史消息和按钮。"""
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        top = int(h * (1.0 - keep))
        im = im.crop((0, top, w, h))
        if im.width > 1280:
            im = im.resize((1280, int(im.height * 1280.0 / im.width)), Image.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return raw, "image/png"


def doubao_ask(images, ref="", wait=16, text="", max_images=3):
    """把整题（题干+选项）的图逐张粘进豆包，再补一段文字要求，发出去，
    等它答完后截它的窗口，用视觉模型把回答转成文字。"""
    images = [u for u in (images or []) if isinstance(u, str) and u.startswith("http")][:max_images]
    text = (text or "").strip()
    if not images and not text:
        raise RuntimeError("这一题既没有图片也没有文字，发不了")
    tmp_files = []
    out_json = os.path.join(ROOT, "_doubao_out.json")
    try:
        for i, u in enumerate(images):
            ctype, data = serve_img_bytes(u, ref)
            if not data or data == BLANK_GIF:
                continue
            p = os.path.join(ROOT, "_doubao_in%d.png" % i)
            with open(p, "wb") as f:
                f.write(data)
            tmp_files.append(p)
        if images and not tmp_files:
            raise RuntimeError("题目图片没取到（可能被图床拦了），点「刷新页面」再试")
        if os.path.exists(out_json):
            os.remove(out_json)
        shot = os.path.join(ROOT, "_doubao_shot.png")
        cmd = ["powershell.exe", "-NoProfile", "-Sta", "-ExecutionPolicy", "Bypass",
               "-File", DOUBAO_PS1, "-Images", "|".join(tmp_files), "-Text", text,
               "-Wait", str(int(wait)), "-Exe", DOUBAO_EXE, "-Out", out_json, "-Shot", shot]
        subprocess.run(cmd, timeout=int(wait) + 120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not os.path.exists(out_json):
            raise RuntimeError("豆包自动化没有返回结果（脚本可能被安全软件拦了）")
        with open(out_json, "r", encoding="utf-8") as f:
            res = json.load(f)
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "豆包没有返回内容")
        shot = res.get("shot") or shot
        if not (shot and os.path.exists(shot)):
            raise RuntimeError("没截到豆包的回答（窗口可能被切走了）")
        with open(shot, "rb") as f:
            raw = f.read()
        if len(raw) < 4000:
            raise RuntimeError("截到的图太小，可能没显示出回答")
        raw = crop_bottom(raw)
        cfg = ai_config(load_data())
        body_img, mime = raw if isinstance(raw, tuple) else (raw, "image/png")
        uri = "data:" + mime + ";base64," + base64.b64encode(body_img).decode()
        out = ai_vision(cfg, [uri], DOUBAO_READ_PROMPT, timeout=180)
        if not out.strip():
            raise RuntimeError("视觉模型没读出内容")
        return out.strip()
    finally:
        for p in tmp_files + [out_json, os.path.join(ROOT, "_doubao_shot.png")]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


def pick_answer(text):
    """从豆包的回答里挖出「答案」那一句。
    兼容：答案：C / 答案是C / 答案为 C / **答案：C** / 正确答案：C；
    挖不到就取最后一行（短的才算）。"""
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"[*_`]+", "", t)          # 去掉 markdown 记号
    t = re.sub(r"[ 	]+", " ", t)
    cands = []
    for pat in (r"答案\s*(?:是|为)?\s*[:：]\s*([^\n\r]{1,120})",
                r"答案\s*(?:是|为)\s*([^\n\r]{1,120})",
                r"正确答案\s*[:：]\s*([^\n\r]{1,120})"):
        for m in re.finditer(pat, t):
            v = m.group(1).strip()
            if v:
                cands.append(v)
        if cands:
            break
    if not cands:
        lines = [x.strip() for x in t.splitlines() if x.strip()]
        if lines:
            last = lines[-1]
            if len(last) <= 120:
                cands.append(last)
    if not cands:
        return ""
    a = cands[-1] if len(cands) > 1 else cands[0]
    a = a.strip().strip("。．.、,，;；:：!！\"'“”")
    a = re.sub(r"^[（(]\s*", "", a)
    a = re.sub(r"^选项\s*", "", a)
    a = re.sub(r"^(?:答案|选择)\s*[:：]?\s*", "", a)
    # “选 C”“选择C” 这种开头，直接取字母
    mm0 = re.match(r"^选(?:择)?\s*([A-Ha-h])", a)
    if mm0:
        return mm0.group(1).upper()
    # 只取第一句，避免把解释也带进来
    a = re.split(r"[。；;\n]", a)[0].strip()
    # “C项”“B 选项” -> 字母
    mm = re.match(r"^([A-Ha-h])\s*(?:项|选项)?[）)]?$", a)
    if mm:
        a = mm.group(1).upper()
    else:
        mm2 = re.match(r"^([A-Ha-h])\s*(?:项|选项)", a)
        if mm2:
            a = mm2.group(1).upper()
    return a[:200]


def doubao_item_images(it):
    """整题截图：item 带 url+idx 时优先用「这一题整块」的截图，替代零散图片。"""
    url = (it.get("url") or "").strip()
    idx = it.get("idx")
    if url and isinstance(idx, int) and idx >= 0:
        png = capture_question(url, idx)
        fn = os.path.join(ROOT, "_doubao_shot_in.png")
        with open(fn, "wb") as f:
            f.write(png)
        return [fn]
    files = []
    for u in (it.get("images") or [])[:3]:
        try:
            ctype, data = serve_img_bytes(u, it.get("ref") or "")
            if not data or data[:4] == b"GIF8":
                continue
            fn = os.path.join(ROOT, "_doubao_in%d.png" % len(files))
            with open(fn, "wb") as f:
                f.write(data)
            files.append(fn)
        except Exception:
            continue
    return files


def doubao_ask_files(files, text="", wait=16):
    """把现成的 PNG 文件列表粘给豆包 -> 等回答 -> 截图 -> 视觉模型转写。"""
    if not files:
        raise RuntimeError("这一题没有可以发给豆包的截图")
    out_json = os.path.join(ROOT, "_doubao_out.json")
    shot = os.path.join(ROOT, "_doubao_shot.png")
    for p in (out_json, shot):
        try:
            os.remove(p)
        except OSError:
            pass
    args = ["powershell.exe", "-NoProfile", "-Sta", "-ExecutionPolicy", "Bypass",
            "-File", DOUBAO_PS1, "-Images", "|".join(files), "-Text", text,
            "-Wait", str(int(wait)), "-Exe", DOUBAO_EXE, "-Out", out_json, "-Shot", shot]
    subprocess.run(args, timeout=int(wait) + 120, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not os.path.exists(out_json):
        raise RuntimeError("豆包自动化没有返回结果（可能被安全软件拦了）")
    res = json.loads((open(out_json, encoding="utf-8-sig").read() or "{}"))
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "豆包自动化失败")
    if not os.path.exists(shot):
        raise RuntimeError("没拿到豆包窗口的截图")
    raw = open(shot, "rb").read()
    if len(raw) < 4000:
        raise RuntimeError("截到的图太小（%d 字节），豆包窗口可能没展开" % len(raw))
    data, ctype = crop_bottom(raw)   # 注意顺序：crop_bottom 返回 (bytes, mime)
    b64 = base64.b64encode(data).decode("ascii")
    return ai_vision(ai_config(load_data()), ["data:%s;base64,%s" % (ctype, b64)],
                     DOUBAO_READ_PROMPT, timeout=180)


def doubao_batch(items, ref="", wait=16, summarize=True):
    """逐题问豆包：每题的答案先在本地抽出来，最后拼成一份汇总清单。
    只有当某题没抽出答案时，才额外让豆包自己汇总一次（省时间）。"""
    results = []
    for it in items:
        try:
            files = doubao_item_images(it)
            text = doubao_ask_files(files, it.get("text") or "", wait)
        except Exception as e:
            log("第 %s 题问豆包失败：%s" % (it.get("i"), e))
            results.append({"i": it.get("i"), "text": "", "answer": "", "error": str(e)})
            continue
        results.append({"i": it.get("i"), "text": text, "answer": pick_answer(text)})

    lines = []
    missing = []
    for r in results:
        if r["answer"]:
            lines.append("%s. %s" % (r["i"], r["answer"]))
        else:
            missing.append(r)
    summary = "\n".join(lines)

    if summarize and missing:
        src = []
        for r in results:
            src.append("第%s题的回答：%s" % (r["i"], (r["text"] or "")[:600]))
        prompt = ("下面是我逐题问你得到的回答。请只输出一个答案清单，每行格式严格为："
                  "题号. 答案（例如 1. B 或 2. ACD 或 3. 对 或 4. 填空答案，多个空用 ||| 分隔）。"
                  "不要解释，不要多余的话。\n\n" + "\n".join(src))
        try:
            extra = doubao_ask([], ref, wait, prompt)
            if extra.strip():
                summary = (summary + "\n" + extra.strip()).strip()
        except Exception as e:
            log("豆包汇总失败（不影响已抽到的答案）：%s" % e)
    return {"answers": results, "summary": summary}


def ai_vision(cfg, imgs, prompt=VISION_PROMPT, timeout=180):
    """把图片交给支持视觉的模型转写。imgs: [data_uri, ...]"""
    url = ai_endpoint(cfg)
    if not url:
        raise RuntimeError("接口地址没填")
    bad, _ = ai_problem(cfg, url)
    if bad:
        raise RuntimeError(bad)
    content = [{"type": "text", "text": prompt}]
    for u in imgs:
        content.append({"type": "image_url", "image_url": {"url": u}})
    payload = {
        "model": (FREE_MODEL if cfg["free"] else (cfg["model"] or "deepseek-chat")),
        "messages": [
            {"role": "system", "content": "你是学习辅导助手，负责把作业图片里的内容准确转写成文字供解题使用。"},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    try:
        raw = http_post_json(url, payload, cfg["key"], timeout)
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 400" in msg and ("image" in msg.lower() or "vision" in msg.lower() or "content" in msg.lower()):
            raise RuntimeError("这个接口不接受图片（可能不是视觉模型）。读图要换成支持视觉的模型，"
                               "例如 qwen-vl-max、glm-4v、gpt-4o-mini，改到「设置 → 解题助手」的模型名即可。")
        if "HTTP 404" in msg:
            raise RuntimeError("接口地址或模型名不对，读图请求被 404 了。检查「设置 → 解题助手」里的接口地址与模型名。")
        raise
    if not raw:
        raise RuntimeError("接口没有返回内容")
    return extract_content(raw)


def http_post_json(url, payload, key="", timeout=120):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError("接口返回 HTTP %s %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("接口请求失败：%s" % e)


def looks_logged_out(body, final_url=""):
    """判断是不是被踢回登录页。宽松判定：只在有明确登录页特征时才认为失效。"""
    low = (body or "").lower()
    if "passport" in (final_url or "").lower():
        return True
    if "passport2.chaoxing.com/login" in low or "passport.chaoxing.com/login" in low:
        return True
    if "请先登录" in body or "你还没有登录" in body:
        return True
    return False


# ---------------------------------------------------------------- 解析

def _attr(attrs, name):
    m = re.search(r'\b%s\s*=\s*"([^"]*)"' % re.escape(name), attrs, re.I)
    if not m:
        m = re.search(r"\b%s\s*=\s*'([^']*)'" % re.escape(name), attrs, re.I)
    return m.group(1).strip() if m else ""


def _text(fragment):
    txt = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", txt).strip()


def parse_courses(html):
    out, seen = [], set()
    # 用「每个 <li 开头为界」切片：课程 li 内部还有 ul.hanlde-list>li（“移动到/退课”），
    # 若用 <li>(.*?)</li> 非贪婪匹配，会在内部 li 的 </li> 处提前收尾，导致课程名变成“移动到”。
    starts = [m for m in re.finditer(r"<li\b([^>]*)>", html, re.I)
              if (_attr(m.group(1), "courseid") or _attr(m.group(1), "courseId"))]
    for i, m in enumerate(starts):
        attrs = m.group(1)
        cid = _attr(attrs, "courseid") or _attr(attrs, "courseId")
        clazz = _attr(attrs, "clazzid") or _attr(attrs, "classId")
        cpi = _attr(attrs, "personid") or _attr(attrs, "cpi") or _attr(attrs, "personId")
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        body = html[m.end():end]
        name = ""
        nm = re.search(r'<span[^>]*class="[^"]*course-name[^"]*"([^>]*)>', body, re.I)
        if nm:
            name = _attr(nm.group(1), "title")
            if not name:
                seg = body[nm.end():nm.end() + 300]
                name = _text(seg.split("</span>")[0])
        if not name:
            name = _attr(attrs, "title") or _text(body)[:40]
        name = re.sub(r"\s+", " ", name).strip()[:60]
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"name": name or ("课程 " + cid), "courseId": cid,
                    "classId": clazz, "cpi": cpi})
    return out


def parse_remaining_minutes(text):
    m = REMAIN_RE.search(text or "")
    if not m:
        return 0
    d = int(m.group(1) or 0)
    h = int(m.group(2) or 0)
    mi = int(m.group(3) or 0)
    total = d * 1440 + h * 60 + mi
    return total * 60 * 1000


def parse_work(html, course):
    items = []
    starts = [m for m in re.finditer(r"<li\b([^>]*onclick\s*=\s*\"[^\"]*goTask[^\"]*\"[^>]*)>", html, re.I)]
    for i, m in enumerate(starts):
        attrs = m.group(1)
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        block = html[m.end():end]
        data = _attr(attrs, "data")
        # 学习通在 li 的 data 属性里给了完整作业地址（带 answerId 和每份作业的 enc），
        # 自己拼的 URL 会缺参数，服务器会直接回「您没有权限访问这个页面」
        task_url = data.strip() if data else ""
        if task_url.startswith("//"):
            task_url = "https:" + task_url
        elif task_url.startswith("/"):
            task_url = "https://mooc1.chaoxing.com" + task_url
        elif task_url and not task_url.startswith("http"):
            task_url = ""
        wid = ""
        wm = re.search(r"workId=(\d+)", data or attrs)
        if wm:
            wid = wm.group(1)
        if not wid:
            wm = re.search(r"workId=(\d+)", block)
            wid = wm.group(1) if wm else ""
        if not wid:
            continue
        title = ""
        for pm in re.finditer(r"<p[^>]*>(.*?)</p>", block, re.S | re.I):
            t = _text(pm.group(1))
            if t and "作" in t:
                title = t
                break
        if not title:
            for pm in re.finditer(r"<p[^>]*>(.*?)</p>", block, re.S | re.I):
                t = _text(pm.group(1))
                if t:
                    title = t
                    break
        status = ""
        sm = re.search(r'class="[^"]*\bstatus\b[^"]*"[^>]*>(.*?)</', block, re.S | re.I)
        if sm:
            status = _text(sm.group(1))
        tm = re.search(r'class="[^"]*\btime\b[^"]*"[^>]*>(.*?)</', block, re.S | re.I)
        time_text = _text(tm.group(1)) if tm else ""
        if not time_text:
            time_text = block
        ms = parse_remaining_minutes(time_text)
        end_at = int(time.time() * 1000) + ms if ms else 0
        done = any(w in status for w in DONE_WORDS)
        url = task_url or TASK_PAGE.format(wid=wid, cid=course["courseId"],
                                           clazz=course["classId"], cpi=course["cpi"])
        items.append({
            "workId": wid,
            "answerId": answer_id_of(url),
            "courseId": course["courseId"],
            "classId": course["classId"],
            "courseName": course["name"],
            "title": title or ("作业 " + wid),
            "status": status,
            "url": url,
            "endAt": end_at,
            "done": done,
            "firstSeen": int(time.time() * 1000),
        })
    return items


def fetch_work_enc(cookie, course):
    url = WORK_ENC.format(cid=course["courseId"], clazz=course["classId"],
                          cpi=course["cpi"])
    final, body = http_get(url, cookie, LOGIN_URL)
    enc, t = "", ""
    m = re.search(r'id="workEnc"[^>]*value="([^"]*)"', body, re.I)
    if m:
        enc = m.group(1)
    m = re.search(r'id="workEnc"[^>]*\bt\s*=\s*"([^"]*)"', body, re.I)
    if m:
        t = m.group(1)
    if not t:
        m = re.search(r'name="t"[^>]*value="([^"]*)"', body, re.I)
        if m:
            t = m.group(1)
    return enc, t


def answer_id_of(url):
    """从作业地址里取 answerId：0 = 从没作答过，非 0 = 已经有一条作答记录（点过「暂时保存」）。"""
    m = re.search(r"[?&]answerId=(\d+)", url or "")
    return m.group(1) if m else ""


def page_draft(html):
    """作业页里是否已有作答内容（用户点过「暂时保存」）。
    返回 (state, filled, total)，state 为 draft / empty / unknown：
    unknown 表示这一页根本没有作答控件（学习通对「只有查看权限」的作业只回看答案页），不能据此判空。"""
    total = len(re.findall(r'class="[^"]*\bquestionLi\b', html))
    filled = 0
    seen = set()
    # 选择题/判断题的已选答案存在隐藏域 <input id="answer{qid}" value="A">
    for m in re.finditer(r'<input[^>]*\bid="answer(\d+)"[^>]*>', html, re.I):
        tag = m.group(0)
        v = re.search(r'value="([^"]*)"', tag)
        if v and v.group(1).strip():
            seen.add(m.group(1))
    # 简答题答案框 <textarea id="answer{qid}">
    for m in re.finditer(r'<textarea[^>]*\bid="answer(\d+)"[^>]*>(.*?)</textarea>', html, re.I | re.S):
        if re.sub(r'<[^>]+>|&nbsp;|\s', "", m.group(2) or ""):
            seen.add(m.group(1))
    # 填空题每空一个 <textarea id="answerEditor{qid}{n}">
    for m in re.finditer(r'<textarea[^>]*\bid="answerEditor(\d+)[_\d]*"[^>]*>(.*?)</textarea>',
                         html, re.I | re.S):
        if re.sub(r'<[^>]+>|&nbsp;|\s', "", m.group(2) or ""):
            seen.add(m.group(1))
    # 作答页（answerId=0 的作业）：有作答控件，值非空就是存过草稿
    has_fields = bool(re.search(r'id="answer(Editor)?\d+', html)) or \
                 bool(re.search(r'name="tiankongsize\d+"', html))
    if has_fields:
        filled = len(seen)
        return ("draft" if filled > 0 else "empty"), filled, total

    # 回看页（已经有作答记录的作业）：答案显示在 span.stuAnswerContent / div.mark_answer 里
    shown = 0
    for m in re.finditer(r'<span[^>]*class="[^"]*stuAnswerContent[^"]*"[^>]*>(.*?)</span>',
                         html, re.I | re.S):
        if re.sub(r'<[^>]+>|&nbsp;|\s', "", m.group(1) or ""):
            shown += 1
    if shown:
        return "draft", shown, total
    if re.search(r'stuAnswerContent', html, re.I) or re.search(r'class="[^"]*\bmark_answer\b', html):
        return "empty", 0, total
    return "unknown", 0, total


def check_draft(cookie, task):
    """抓一份作业页判断是否暂存过"""
    url = task.get("url") or ""
    if not url:
        return None
    try:
        final, body = http_get(url, cookie, LOGIN_URL, timeout=30)
    except Exception:
        return None
    if "questionLi" not in body:
        return None
    state, filled, total = page_draft(body)
    return state


def sync_all(progress=None):
    data = load_data()
    cookie = data.get("cookie", "")
    if not cookie:
        raise RuntimeError("还没有登录，请先完成登录")

    def step(txt):
        if progress is not None:
            try:
                progress(txt)
            except Exception:
                pass

    step("正在获取课程…")
    final, body = http_get(COURSE_LIST, cookie, LOGIN_URL)
    if looks_logged_out(body, final):
        raise PermissionError("登录已失效，请重新登录")
    courses = parse_courses(body)
    if not courses:
        if "登录" in body and "超星" in body and "课程" not in body:
            raise PermissionError("登录已失效，请重新登录")
        raise PermissionError("没能读到课程列表，可能是登录已失效，或账号下没有课程")

    tasks = []
    for i, c in enumerate(courses):
        step("正在读取第 %d/%d 门课程…" % (i + 1, len(courses)))
        try:
            enc, t = fetch_work_enc(cookie, c)
            url = WORK_LIST.format(cid=c["courseId"], clazz=c["classId"],
                                   cpi=c["cpi"], t=t, enc=enc)
            _, wk = http_get(url, cookie, WORK_ENC.split("?")[0])
            if looks_logged_out(wk):
                raise PermissionError("登录已失效，请重新登录")
            tasks.extend(parse_work(wk, c))
        except PermissionError:
            raise
        except Exception as e:
            log("课程 %s 读取失败：%r" % (c.get("name"), e))
        time.sleep(0.3)

    now = int(time.time() * 1000)
    old = {t["workId"]: t for t in data.get("tasks", [])}
    for t in tasks:
        o = old.get(t["workId"])
        if o:
            t["firstSeen"] = o.get("firstSeen", t["firstSeen"])
    tasks.sort(key=lambda t: (bool(t["done"]), t["endAt"] == 0, t["endAt"]))
    data["courses"] = courses
    data["tasks"] = tasks
    data["lastSync"] = now
    data["lastError"] = ""
    save_data()

    open_n = sum(1 for t in tasks if not t["done"])
    return {"courses": courses, "tasks": tasks, "open": open_n, "total": len(tasks)}


# ---------------------------------------------------------------- AI

def ai_config(data=None):
    data = data or load_data()
    s = data.get("settings", {})
    return {
        "free": (s.get("aiMode") == "free"),
        "base": (s.get("aiBase") or "").strip(),
        "key": (s.get("aiKey") or "").strip(),
        "model": (s.get("aiModel") or "deepseek-chat").strip(),
    }


def ai_endpoint(cfg):
    if cfg["free"]:
        return FREE_ENDPOINT
    base = (cfg["base"] or "").strip()
    if not base:
        return ""
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if "/v1" in base:
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def ai_problem(cfg, url=""):
    """返回 (错误文案, 是否建议切免费通道)。自备接口配置不全时给出人话提示，别甩 401。"""
    if cfg["free"]:
        return "", False
    if not (cfg["base"] or "").strip():
        return ("自备接口的「接口地址」还空着。可以点下面的按钮改用免费通道，"
                "或到「设置 → 解题助手」填地址（例如 https://api.deepseek.com/v1）。"), True
    if not (cfg["key"] or "").strip():
        host = ""
        try:
            host = urllib.parse.urlparse(url or cfg["base"]).netloc
        except Exception:
            pass
        return ("自备接口（%s）还没填 Key，所以服务器返回 401 鉴权失败。"
                "点下面的按钮改用免费通道就能直接生成；"
                "想更快更准就到「设置 → 解题助手」填上 Key。" % (host or cfg["base"])), True
    return "", False


def ai_call(system, user, cfg, timeout=120):
    url = ai_endpoint(cfg)
    if not url:
        raise RuntimeError("接口地址没填")
    bad, _ = ai_problem(cfg, url)
    if bad:
        raise RuntimeError(bad)
    payload = {
        # 免费通道走 pollinations 的 Classic API，只认它自己的模型名（openai 等）；
        # 把 deepseek-chat 这类自备模型名传过去会直接 404。
        "model": (FREE_MODEL if cfg["free"] else (cfg["model"] or "deepseek-chat")),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    raw = ""
    for attempt in range(2 if cfg["free"] else 1):
        try:
            raw = http_post_json(url, payload, cfg["key"], timeout)
            break
        except RuntimeError as e:
            msg = str(e)
            if "HTTP 402" in msg or "FREE_LIMIT" in msg:
                # pollinations 免费通道限制：每个 IP 每 30 秒 1 次，等一会儿自动重试一次
                if attempt == 0:
                    wait = 6
                    mm = re.search(r"[Rr]etry after (\d+)", msg)
                    if mm:
                        wait = min(int(mm.group(1)) + 2, 35)
                    log("免费通道限流，%d 秒后自动重试一次" % wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError("免费通道被限流了（每个 IP 每 30 秒只能请求 1 次）。"
                                   "等半分钟再点一次即可；想要不限速、更快更准，"
                                   "就到「设置 → 解题助手」填自己的接口 Key。")
            if "HTTP 404" in msg and "isn't on the Classic API" in msg:
                raise RuntimeError("免费通道不支持当前模型名，请在设置里把模型留空或切到「免费通道」。")
            raise
    if not raw:
        raise RuntimeError("接口没有返回内容")
    try:
        obj = json.loads(raw)
        return obj["choices"][0]["message"]["content"]
    except Exception:
        pass
    if "data:" in raw:
        parts = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk in ("", "[DONE]"):
                    continue
                try:
                    j = json.loads(chunk)
                    parts.append(j["choices"][0]["delta"].get("content") or "")
                except Exception:
                    continue
        if parts:
            return "".join(parts)
    return raw[:4000]


def extract_answers(text):
    s = text or ""
    a, b = s.find("["), s.rfind("]")
    if a >= 0 and b > a:
        try:
            arr = json.loads(s[a:b + 1])
            out = []
            for it in arr:
                if isinstance(it, dict):
                    out.append({"i": int(it.get("i") or it.get("index") or 0),
                                "answer": str(it.get("answer") or it.get("a") or "")})
            if out:
                return out
        except Exception:
            pass
    out = []
    for m in re.finditer(r'"i"\s*:\s*"?(\d+)"?[^}]*?"answer"\s*:\s*"([^"]*)"', s):
        out.append({"i": int(m.group(1)), "answer": m.group(2)})
    return out


def ping_ai(cfg):
    content = ai_call("你只回复一个词。", "回复 ok", cfg, timeout=45)
    return (content or "").strip()[:40]


# ---------------------------------------------------------------- 通知

def notify_win(title, message):
    esc = lambda s: (s or "").replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(8000,'%s','%s',[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 7;$n.Dispose();"
    ) % (esc(title), esc(message))
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                          "-Command", ps], creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log("通知失败：%r" % (e,))


def remind_loop():
    while True:
        try:
            data = load_data()
            s = data.get("settings", {})
            now = int(time.time() * 1000)
            for t in data.get("tasks", []):
                if t.get("done") or not t.get("endAt"):
                    continue
                for lead, on in ((24, s.get("lead24")), (2, s.get("lead2")), (0, s.get("lead0"))):
                    if not on:
                        continue
                    fire_at = t["endAt"] - lead * 3600 * 1000
                    key = (t["workId"], lead)
                    if key in _remind_seen:
                        continue
                    if now >= fire_at and now < t["endAt"] + 3600 * 1000:
                        _remind_seen.add(key)
                        if lead == 0:
                            title = "已到截止时间"
                        elif lead >= 24:
                            title = "提前 24 小时提醒"
                        else:
                            title = "提前 %d 小时提醒" % lead
                        notify_win("小肥鱼帮你做作业 · " + title, "%s\n%s" % (t.get("title", ""), t.get("courseName", "")))
        except Exception as e:
            log("提醒循环异常：%r" % (e,))
        time.sleep(30)


def autosync_loop():
    last = 0
    while True:
        time.sleep(20)
        try:
            data = load_data()
            s = data.get("settings", {})
            if not s.get("autoSync", True):
                continue
            if not data.get("cookie"):
                continue
            minutes = max(int(s.get("interval") or 30), 5)
            now = time.time()
            if now - last < minutes * 60:
                continue
            last = now
            log("自动同步开始")
            try:
                sync_all()
                log("自动同步完成")
            except PermissionError as e:
                data["lastError"] = str(e)
                save_data()
                notify_win("小肥鱼帮你做作业 · 登录失效", str(e))
            except Exception as e:
                data["lastError"] = "同步失败：%s" % e
                save_data()
                log("自动同步失败：%r" % (e,))
        except Exception as e:
            log("自动同步循环异常：%r" % (e,))


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "XiaofeiyuPC/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            try:
                with open(UI_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "ui.html 读取失败：%s" % e, "text/plain; charset=utf-8")
            return
        if path.startswith("/img/"):
            name = os.path.basename(path)
            fp = os.path.join(ROOT, "img", name)
            if os.path.isfile(fp):
                ctype = ("image/png" if name.lower().endswith(".png")
                         else "image/jpeg" if name.lower().endswith((".jpg", ".jpeg"))
                         else "application/octet-stream")
                try:
                    with open(fp, "rb") as f:
                        self._send(200, f.read(), ctype)
                except Exception as e:
                    self._send(500, "读取失败：%s" % e, "text/plain; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
            return
        if path == "/api/ping":
            self._json({"ok": True, "version": VERSION})
            return
        if path == "/api/state":
            data = load_data()
            s = dict(data.get("settings", {}))
            s["aiKey"] = "***" if s.get("aiKey") else ""
            self._json({
                "settings": s,
                "logged": bool(data.get("cookie")),
                "account": data.get("account", ""),
                "courses": data.get("courses", []),
                "tasks": data.get("tasks", []),
                "drafts": data.get("drafts", {}),
                "lastSync": data.get("lastSync", 0),
                "lastError": data.get("lastError", ""),
                "version": VERSION,
                "loginUrl": LOGIN_URL,
            })
            return
        if path == "/api/draft":
            qs = urllib.parse.parse_qs(parsed.query)
            work_id = (qs.get("workId") or [""])[0]
            url = (qs.get("url") or [""])[0]
            data = load_data()
            task = {"url": url}
            if not task["url"] and work_id:
                for t in data.get("tasks", []):
                    if str(t.get("workId")) == str(work_id):
                        task = t
                        break
            state = check_draft(data.get("cookie", ""), task)
            known = state in ("draft", "empty")
            if known and work_id:
                data.setdefault("drafts", {})[str(work_id)] = (state == "draft")
                save_data()
            self._json({"ok": True, "known": known, "workId": work_id,
                        "hasDraft": state == "draft"})
            return
        if path == "/api/x":
            qs = urllib.parse.parse_qs(parsed.query)
            target = (qs.get("url") or [""])[0]
            if not target.startswith("http"):
                self._send(400, "bad url", "text/plain; charset=utf-8")
                return
            data = load_data()
            ref = (qs.get("ref") or [""])[0]
            final, code, out_ct, raw_out = http_proxy(
                target, data.get("cookie", ""), "GET", None, None,
                referer=(ref if ref.startswith("http") else "https://i.chaoxing.com"))
            origin = "http://" + (self.headers.get("Host") or "127.0.0.1:17800")
            out_ct, raw_out = maybe_inject_proxy_body(code, out_ct, raw_out, final, origin,
                                                      data.get("cookie", ""))
            log("代理 %s %s %s %d字节" % ("GET", code, (target or "")[:70], len(raw_out or b"")))
            self._send(code, raw_out, out_ct)
            return
        if path == "/api/img":
            qs = urllib.parse.parse_qs(parsed.query)
            src = (qs.get("url") or [""])[0]
            ref = (qs.get("ref") or [""])[0]
            if not src.startswith("http"):
                self._send(400, "bad url", "text/plain; charset=utf-8")
                return
            try:
                ctype, data = serve_img_bytes(src, ref)
            except Exception:
                ctype, data = "image/gif", BLANK_GIF
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            return
        if path == "/api/page":
            qs = urllib.parse.parse_qs(parsed.query)
            target = (qs.get("url") or [""])[0]
            if not target.startswith("http"):
                self._send(400, "bad url", "text/plain; charset=utf-8")
                return
            try:
                data = load_data()
                final, html = http_get(target, data.get("cookie", ""), LOGIN_URL, timeout=30)
                origin = "http://" + (self.headers.get("Host") or "127.0.0.1:17800")
                html = inject_html(html, final, origin, data.get("cookie", ""))
                self._send(200, html, "text/html; charset=utf-8")
            except Exception as e:
                self._send(502, "<meta charset='utf-8'><p>页面加载失败：%s</p>" % e,
                           "text/html; charset=utf-8")
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        path = parsed.path
        if path == "/api/x":
            qs = urllib.parse.parse_qs(parsed.query)
            target = (qs.get("url") or [""])[0]
            if not target.startswith("http"):
                self._send(400, "bad url", "text/plain; charset=utf-8")
                return
            data = load_data()
            ctype = self.headers.get("Content-Type")
            ref = (qs.get("ref") or [""])[0]
            ref = ref if ref.startswith("http") else "https://mooc1.chaoxing.com/"
            org_hdr = "https://mooc1.chaoxing.com"
            try:
                org_hdr = "https://" + urllib.parse.urlparse(target).netloc
            except Exception:
                pass
            final, code, out_ct, raw_out = http_proxy(
                target, data.get("cookie", ""), "POST", raw, ctype,
                referer=ref, origin_hdr=org_hdr)
            origin = "http://" + (self.headers.get("Host") or "127.0.0.1:17800")
            out_ct, raw_out = maybe_inject_proxy_body(code, out_ct, raw_out, final, origin,
                                                      data.get("cookie", ""))
            log("代理 %s %s %s %d字节" % ("POST", code, (target or "")[:70], len(raw_out or b"")))
            if not (out_ct or "").lower().startswith("text/html"):
                log("代理体 %s %s %r" % (code, out_ct, (raw_out or b"")[:160]))
            self._send(code, raw_out, out_ct)
            return
        data = load_data()

        try:
            if path == "/api/doubao":
                ref = (body.get("ref") or "").strip()
                wait = 16
                try:
                    wait = max(6, min(90, int(body.get("wait") or 16)))
                except Exception:
                    wait = 16
                items = body.get("items")
                if isinstance(items, list) and items:
                    clean = []
                    for it in items[:12]:
                        if not isinstance(it, dict):
                            continue
                        clean.append({
                            "i": it.get("i"),
                            "images": [u for u in (it.get("images") or [])
                                       if isinstance(u, str) and u.startswith("http")],
                            "url": (it.get("url") or "").strip(),
                            "idx": it.get("idx"),
                            "text": (it.get("text") or "").strip(),
                        })
                    if not clean:
                        self._json({"ok": False, "error": "没有可发的题目"})
                        return
                    try:
                        r = doubao_batch(clean, ref, wait, bool(body.get("summary", True)))
                        log("豆包批量 %d 题，汇总 %d 字" % (len(r["answers"]), len(r["summary"])))
                        self._json({"ok": True, "answers": r["answers"], "summary": r["summary"]})
                    except Exception as e:
                        log("豆包批量失败 %s" % e)
                        self._json({"ok": False, "error": str(e)})
                    return
                urls = [u for u in (body.get("images") or [])
                        if isinstance(u, str) and u.startswith("http")]
                text_in = (body.get("text") or "").strip()
                try:
                    text = doubao_ask(urls, ref, wait, text_in)
                    log("豆包 %d 字" % len(text))
                    self._json({"ok": True, "text": text})
                except Exception as e:
                    log("豆包失败 %s" % e)
                    self._json({"ok": False, "error": str(e)})
                return

            if path == "/api/agree":
                data["settings"]["agreed"] = True
                save_data()
                self._json({"ok": True})
                return

            if path == "/api/cookie":
                cookie = (body.get("cookie") or "").strip()
                data["cookie"] = cookie
                if not cookie:
                    data["tasks"], data["courses"], data["account"] = [], [], ""
                save_data()
                self._json({"ok": True})
                return

            if path == "/api/vision":
                try:
                    vbody = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    vbody = {}
                urls = [u for u in (vbody.get("images") or [])
                        if isinstance(u, str) and u.startswith("http")]
                ref = (vbody.get("ref") or "").strip()
                if not urls:
                    self._json({"ok": False, "error": "这一页没有可读的图片"})
                    return
                urls = urls[:6]
                vdata = load_data()
                cfg = ai_config(vdata)
                uris, skipped = [], 0
                for u in urls:
                    du = img_data_uri(u, ref)
                    if du:
                        uris.append(du)
                    else:
                        skipped += 1
                if not uris:
                    self._json({"ok": False,
                                "error": "图片都下载失败或被拦了（%d 张），点「刷新页面」重开一次再试。" % skipped})
                    return
                try:
                    vtext = ai_vision(cfg, uris)
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})
                    return
                self._json({"ok": True, "text": vtext, "count": len(uris), "skipped": skipped})
                return

            if path == "/api/setdrafts":
                incoming = body.get("drafts") or {}
                if not isinstance(incoming, dict):
                    self._json({"ok": False, "error": "bad drafts"})
                    return
                cur = data.setdefault("drafts", {})
                for k, v in incoming.items():
                    cur[str(k)] = bool(v)
                save_data()
                self._json({"ok": True, "count": len(cur)})
                return

            if path == "/api/settings":
                s = data.setdefault("settings", {})
                for k in ("interval", "lead24", "lead2", "lead0", "showDone",
                          "aiMode", "aiBase", "aiModel", "aiKey", "autoSync",
                          "layout", "glass", "glow"):
                    if k in body:
                        s[k] = body[k]
                save_data()
                self._json({"ok": True})
                return

            if path == "/api/login/open":
                try:
                    br = open_login_window()
                    self._json({"ok": True, "browser": os.path.basename(br)})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})
                return

            if path == "/api/login/capture":
                try:
                    pairs = capture_login_cookies()
                except Exception as e:
                    self._json({"ok": False, "error": str(e)})
                    return
                if not pairs:
                    self._json({"ok": False, "error":
                                "还没检测到登录状态：请在打开的窗口里完成学习通登录，再点这个按钮"})
                    return
                header = cookie_header(pairs)
                uid = has_uid(pairs)
                data["cookie"] = header
                save_data()
                self._json({"ok": True, "count": len(pairs), "hasUid": uid,
                            "names": [k for k, _ in pairs][:20]})
                return

            if path == "/api/sync":
                try:
                    res = sync_all()
                    self._json({"ok": True, "open": res["open"], "total": res["total"],
                                "courses": len(res["courses"]), "tasks": res["tasks"]})
                except PermissionError as e:
                    data["lastError"] = str(e)
                    save_data()
                    self._json({"ok": False, "auth": True, "error": str(e)})
                except Exception as e:
                    data["lastError"] = str(e)
                    save_data()
                    self._json({"ok": False, "error": str(e)})
                return

            if path == "/api/checkdrafts":
                ids = body.get("workIds") or []
                data = load_data()
                cookie = data.get("cookie", "")
                if not cookie:
                    self._json({"ok": False, "error": "还没有登录，请先完成登录"})
                    return
                if not ids:
                    # 默认检查所有未交作业（最多 40 个，按截止时间从近到远）
                    opens = [t for t in data.get("tasks", []) if not t.get("done")]
                    opens.sort(key=lambda t: t.get("endAt") or 9e18)
                    ids = [str(t.get("workId")) for t in opens[:40] if t.get("url")]
                by_id = {str(t.get("workId")): t for t in data.get("tasks", [])}
                drafts = data.setdefault("drafts", {})
                checked = 0
                for wid in ids:
                    t = by_id.get(str(wid))
                    if not t or not t.get("url"):
                        continue
                    state = check_draft(cookie, t)
                    if state not in ("draft", "empty"):
                        continue
                    drafts[str(wid)] = (state == "draft")
                    checked += 1
                    time.sleep(0.25)
                save_data()
                self._json({"ok": True, "checked": checked,
                            "drafts": drafts,
                            "hasDraft": sum(1 for v in drafts.values() if v)})
                return

            if path == "/api/testai":
                cfg = ai_config(data)
                bad, suggest_free = ai_problem(cfg, ai_endpoint(cfg))
                if bad:
                    self._json({"ok": False, "error": bad, "fix": "free"})
                    return
                try:
                    txt = ping_ai(cfg)
                    self._json({"ok": True, "reply": txt})
                except Exception as e:
                    self._json({"ok": False, "error": str(e), "fix": "free"})
                return

            if path == "/api/solve":
                cfg = ai_config(data)
                prompt = (body.get("prompt") or "").strip()
                mode = body.get("mode") or "answer"
                if not prompt:
                    self._json({"ok": False, "error": "没有题目内容"})
                    return
                bad, suggest_free = ai_problem(cfg, ai_endpoint(cfg))
                if bad:
                    self._json({"ok": False, "error": bad, "fix": "free"})
                    return
                try:
                    if mode == "explain":
                        txt = ai_call(SYS_EXPLAIN, prompt[:6000], cfg)
                        self._json({"ok": True, "text": txt})
                    else:
                        txt = ai_call(SYS_ANSWER, prompt[:8000], cfg)
                        self._json({"ok": True, "text": txt,
                                    "answers": extract_answers(txt)})
                except Exception as e:
                    msg = str(e)
                    fix = ""
                    if "HTTP 401" in msg or "HTTP 403" in msg:
                        host = ""
                        try:
                            host = urllib.parse.urlparse(ai_endpoint(cfg)).netloc
                        except Exception:
                            pass
                        msg = ("接口鉴权失败（401/403）：%s 认为 Key 不对或已过期。端点：%s。"
                               "可以点下面的按钮改用免费通道。" % (host or "服务商", ai_endpoint(cfg)))
                        fix = "free"
                    self._json({"ok": False, "error": msg, "fix": fix})
                return

            if path == "/api/clear":
                data["cookie"] = ""
                data["tasks"], data["courses"], data["account"] = [], [], ""
                data["lastSync"], data["lastError"] = 0, ""
                data["settings"]["agreed"] = True
                save_data()
                self._json({"ok": True})
                return

            if path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
                return

            self._json({"ok": False, "error": "unknown api"}, 404)
        except Exception as e:
            log("接口 %s 异常：%r" % (path, e))
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass


# ---------------------------------------------------------------- 登录助手
# 思路：起一个受控的 Edge/Chrome 窗口（独立配置目录 + 调试端口），
#      用户在里面正常登录学习通，我们通过 CDP 直接读它拿到的 Cookie。
#      全程不需要用户按 F12、也不需要复制粘贴。

DEBUG_PORT = 9223
PROFILE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or ROOT, "XiaofeiyuPC-login")

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

UID_KEYS = ("_uid", "uf", "fid", "_d")


def find_browser():
    for p in BROWSERS:
        if os.path.exists(p):
            return p
    return ""


def _cdp_http(path, method="GET", timeout=3):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (DEBUG_PORT, path),
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def debug_ready():
    try:
        _cdp_http("/json/version", timeout=1.5)
        return True
    except Exception:
        return False


def open_login_window():
    br = find_browser()
    if not br:
        raise RuntimeError("没找到 Edge 或 Chrome 浏览器，无法自动登录")
    if debug_ready():
        try:
            _cdp_http("/json/new?" + urllib.parse.quote(LOGIN_URL, safe=""),
                      method="PUT", timeout=3)
            return br
        except Exception:
            pass
        # 老版本 Edge 不支持 /json/new 的 PUT，改用 CDP 开新标签
        try:
            ver = _cdp_http("/json/version", timeout=2)
            ws = _WS(ver["webSocketDebuggerUrl"])
            try:
                ws.call("Target.createTarget", {"url": LOGIN_URL}, timeout=8)
            finally:
                ws.close()
        except Exception:
            pass
        return br
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [br,
            "--remote-debugging-port=%d" % DEBUG_PORT,
            "--user-data-dir=" + PROFILE_DIR,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,msEdgeSplitScreen",
            LOGIN_URL]
    subprocess.Popen(args, close_fds=True,
                     creationflags=0x00000008 | 0x08000000)
    for _ in range(40):
        time.sleep(0.5)
        if debug_ready():
            return br
    raise RuntimeError("登录窗口启动超时，请重试一次")


class _WS(object):
    """够用的 WebSocket 客户端（只为了和本机 CDP 说话）。"""

    def __init__(self, url, timeout=20):
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, u.port or 80
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(8192)
            if not chunk:
                raise RuntimeError("调试端口连接被关闭")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0]:
            raise RuntimeError("调试端口握手失败")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("调试连接已断开")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, op, payload):
        head = bytearray([0x80 | op])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        return bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

    def send(self, obj):
        self.sock.sendall(self._frame(0x1, json.dumps(obj).encode("utf-8")))

    def recv(self):
        data = b""
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(ln)
            if op == 0x9:
                self.sock.sendall(self._frame(0xA, payload))
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise RuntimeError("调试连接被浏览器关闭")
            data += payload
            if fin:
                return data.decode("utf-8", "replace")

    def call(self, method, params=None, timeout=20):
        self._id += 1
        mid = self._id
        self.send({"id": mid, "method": method, "params": params or {}})
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.recv())
            if msg.get("id") != mid:
                continue
            if msg.get("error"):
                raise RuntimeError("调试接口报错：%s" % msg["error"].get("message"))
            return msg.get("result", {})
        raise RuntimeError("调试接口超时：%s" % method)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def capture_login_cookies():
    """从受控浏览器里读出 chaoxing 相关的 Cookie。"""
    if not debug_ready():
        raise RuntimeError("登录窗口还没打开，请先点「① 打开登录窗口」")
    targets = []
    try:
        targets = _cdp_http("/json/list", timeout=3)
    except Exception:
        targets = []
    page = None
    for t in targets:
        if t.get("type") == "page" and "chaoxing" in (t.get("url") or ""):
            page = t
            break
    if page is None:
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                page = t
                break
    if page is None:
        raise RuntimeError("登录窗口里还没有打开的页面，请先在窗口里打开学习通")

    ws = _WS(page["webSocketDebuggerUrl"])
    try:
        try:
            ws.call("Network.enable", {}, timeout=15)
        except Exception:
            pass
        res = ws.call("Network.getAllCookies", {}, timeout=20)
    finally:
        ws.close()

    now = int(time.time())
    pairs, seen = [], set()
    for c in res.get("cookies") or []:
        domain = (c.get("domain") or "").lstrip(".").lower()
        if not (domain == "chaoxing.com" or domain.endswith(".chaoxing.com")):
            continue
        name = c.get("name") or ""
        if not name or name in seen:
            continue
        exp = c.get("expires") or 0
        try:
            exp = int(float(exp))
        except Exception:
            exp = 0
        if exp and exp > 0 and exp < now:
            continue
        seen.add(name)
        pairs.append((name, c.get("value") or ""))
    return pairs


def cookie_header(pairs):
    return "; ".join("%s=%s" % (k, v) for k, v in pairs)


def has_uid(pairs):
    names = {k for k, _ in pairs}
    return any(k in names for k in UID_KEYS)


# ---------------------------------------------------------------- 启动

PORTS = [17800, 17801, 17802, 17803, 17808, 17809, 17810]
PORT_NOW = [17800]


def find_port():
    for p in PORTS:
        try:
            s = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            return p, s
        except OSError:
            continue
    return None, None


def already_running():
    for p in PORTS:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % p, timeout=1) as r:
                if b"ok" in r.read():
                    return p
        except Exception:
            continue
    return 0


APP_WINDOW_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "XiaofeiyuPC-window")


def open_app_window(url, fullscreen=True):
    """用 Edge/Chrome 的「应用窗口」模式打开界面：没有标签页和地址栏，
    开机动画铺满整屏。没装这两个浏览器时退回默认浏览器。"""
    br = find_browser()
    if br:
        args = [br,
                "--app=" + url,
                "--user-data-dir=" + APP_WINDOW_DIR,
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate"]
        if fullscreen:
            args.append("--start-fullscreen")
        else:
            args.append("--window-size=1280,820")
        try:
            subprocess.Popen(args, close_fds=True,
                             creationflags=0x08000000 if os.name == "nt" else 0)
            return True
        except Exception as e:
            log("应用窗口启动失败，退回默认浏览器：%s" % e)
    webbrowser.open(url)
    return False


def main():
    op = already_running()
    if op:
        open_app_window("http://127.0.0.1:%d/" % op)
        print("已在运行：http://127.0.0.1:%d/" % op)
        return
    port, srv = find_port()
    PORT_NOW[0] = port
    if srv is None:
        print("端口全被占用，启动失败")
        return
    threading.Thread(target=remind_loop, daemon=True).start()
    threading.Thread(target=autosync_loop, daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    log("启动服务 %s" % url)
    if "--open" in sys.argv:
        threading.Timer(0.6, lambda: open_app_window(url)).start()
    print("小肥鱼帮你做作业 · 电脑版 %s" % VERSION)
    print("服务地址：%s" % url)
    print("关闭窗口不会停止服务；要退出请运行「停止小肥鱼.bat」")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        log("启动失败：\n" + traceback.format_exc())
        raise
