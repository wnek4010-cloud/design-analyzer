#!/usr/bin/env python3
"""
설계 자동 분석 도구 - Windows GUI 버전
더블클릭으로 실행, 추가 설치 없음 (Python 내장 tkinter 사용)
"""
import os, re, sys, json, threading, urllib.request, urllib.error, urllib.parse
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# ══════════════════════════════════════════════
#  분석 엔진 (analyze.py 와 동일)
# ══════════════════════════════════════════════
ALLOWED_EXT = {'.java','.properties','.xml','.yml','.yaml','.json',
               '.gradle','.py','.js','.ts','.kt','.sql','.conf','.cfg','.ini'}
SKIP_DIRS   = {'target','build','.git','node_modules','__pycache__','.idea','out','dist','.gradle'}
URL_PATTERN = re.compile(r'(https?://[^\s\'"<>{}|\\\^`\[\]]+)', re.I)

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(); self.text=[]; self._skip=False
    def handle_starttag(self,tag,attrs):
        if tag in ('style','script'): self._skip=True
    def handle_endtag(self,tag):
        if tag in ('style','script'): self._skip=False
    def handle_data(self,data):
        if not self._skip:
            d=data.strip()
            if d: self.text.append(d)

def extract_html_text(path):
    try:
        p=TextExtractor(); p.feed(Path(path).read_text(encoding='utf-8',errors='ignore'))
        return '\n'.join(p.text)
    except: return ''

def collect_files(src_dir):
    files=[]
    for root,dirs,fnames in os.walk(src_dir):
        dirs[:]=[d for d in dirs if d not in SKIP_DIRS]
        for f in fnames:
            if Path(f).suffix.lower() in ALLOWED_EXT:
                files.append(Path(root)/f)
    return sorted(files)

def extract_urls(files, src_dir):
    seen=set(); results=[]
    for fp in files:
        try:
            rel=fp.relative_to(src_dir)
            for i,line in enumerate(fp.read_text(encoding='utf-8',errors='ignore').splitlines(),1):
                for m in URL_PATTERN.finditer(line):
                    url=m.group(1).rstrip('.,;)')
                    key=(url,str(rel))
                    if len(url)>10 and key not in seen:
                        seen.add(key)
                        results.append({'file':str(rel),'line':i,'url':url,'context':line.strip()[:120]})
        except: pass
    return results

def detect_hardcoded(files, src_dir):
    patterns=[
        (re.compile(r'import\s+(kr\.go\.[a-zA-Z.]+)\s*;'),'SDK import'),
        (re.compile(r'ldc\s+#\d+.*String\s+(https?://\S+)',re.I),'bytecode URL'),
        (re.compile(r'(password|passwd|secret|api_?key|auth_?key)\s*=\s*["\']([^"\']{4,})["\']',re.I),'민감정보'),
        (re.compile(r'["\'](https?://[^"\']{10,})["\']'),'URL 하드코딩'),
    ]
    results=[]
    for fp in files:
        try:
            rel=fp.relative_to(src_dir)
            for i,line in enumerate(fp.read_text(encoding='utf-8',errors='ignore').splitlines(),1):
                for pat,label in patterns:
                    m=pat.search(line)
                    if m:
                        val=m.group(1) if label!='민감정보' else f"{m.group(1)}=****"
                        results.append({'file':str(rel),'line':i,'type':label,'value':val[:100]})
                        break
        except: pass
    return results

def parse_properties(files, src_dir):
    configs=[]
    for fp in files:
        if fp.suffix.lower()!='.properties': continue
        try:
            rel=fp.relative_to(src_dir)
            for i,line in enumerate(fp.read_text(encoding='utf-8',errors='ignore').splitlines(),1):
                line=line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k,_,v=line.partition('=')
                    configs.append({'file':str(rel),'line':i,'key':k.strip(),'value':v.strip()})
        except: pass
    return configs

def parse_pom(files, src_dir):
    deps=[]
    for fp in files:
        if fp.name.lower()!='pom.xml': continue
        try:
            content=fp.read_text(encoding='utf-8',errors='ignore')
            rel=fp.relative_to(src_dir)
            for block in re.findall(r'<dependency>(.*?)</dependency>',content,re.DOTALL):
                def gv(tag): m=re.search(f'<{tag}>(.*?)</{tag}>',block); return m.group(1) if m else ''
                deps.append({'file':str(rel),'groupId':gv('groupId'),'artifactId':gv('artifactId'),
                             'version':gv('version'),'scope':gv('scope') or 'compile',
                             'systemPath':gv('systemPath')})
        except: pass
    return deps

def detect_sdk_imports(files, src_dir):
    pat=re.compile(r'import\s+(kr\.go\.[a-zA-Z.]+)\s*;')
    results=[]
    for fp in files:
        if fp.suffix.lower()!='.java': continue
        try:
            rel=fp.relative_to(src_dir)
            for i,line in enumerate(fp.read_text(encoding='utf-8',errors='ignore').splitlines(),1):
                m=pat.search(line)
                if m: results.append({'file':str(rel),'line':i,'package':m.group(1)})
        except: pass
    return results

def check_url(url, timeout=5):
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'},method='HEAD')
        with urllib.request.urlopen(req,timeout=timeout) as r: return r.status,'OK'
    except urllib.error.HTTPError as e: return e.code,e.reason
    except urllib.error.URLError as e: return 0,str(e.reason)[:40]
    except Exception as e: return 0,str(e)[:40]

def generate_report(data, output_path):
    now=datetime.now().strftime('%Y-%m-%d %H:%M')
    urls=data['urls']; hc=data['hardcoded']; cfgs=data['configs']
    deps=data['deps']; sdks=data['sdks']; chks=data['url_checks']
    alive=sum(1 for r in chks if 200<=r.get('status',0)<400)
    dead =sum(1 for r in chks if r.get('status',0) and not(200<=r.get('status',0)<400))
    chk_map={r['url']:r for r in chks}

    def badge(t,c):
        cols={'green':('#d1fae5','#065f46'),'red':('#fee2e2','#991b1b'),
              'amber':('#fef3c7','#92400e'),'blue':('#dbeafe','#1e40af'),
              'purple':('#ede9fe','#5b21b6'),'gray':('#f3f4f6','#374151')}
        bg,fg=cols.get(c,cols['gray'])
        return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600">{t}</span>'

    def tr(*cells,hdr=False):
        tag='th' if hdr else 'td'
        style='padding:8px 10px;border-bottom:1px solid #e5e7eb;font-size:12px;vertical-align:top'
        return '<tr>'+''.join(f'<{tag} style="{style}">{c}</{tag}>' for c in cells)+'</tr>'

    url_rows=''
    for u in urls[:200]:
        chk=chk_map.get(u['url'],{})
        sc=chk.get('status',0)
        if sc and 200<=sc<400: sb=badge(f'{sc} OK','green')
        elif sc: sb=badge(f'{sc} FAIL','red')
        else: sb=badge('미확인','gray')
        url_rows+=tr(f'<code style="font-size:11px">{u["file"]}:{u["line"]}</code>',
                     f'<code style="font-size:11px;word-break:break-all">{u["url"][:90]}</code>',sb)

    hc_rows=''
    for h in hc[:100]:
        tc={'URL 하드코딩':'amber','SDK import':'purple','민감정보':'red','bytecode URL':'red'}.get(h['type'],'gray')
        hc_rows+=tr(f'<code style="font-size:11px">{h["file"]}:{h["line"]}</code>',
                    badge(h['type'],tc),
                    f'<code style="font-size:11px;word-break:break-all">{h["value"][:90]}</code>')

    cfg_rows=''.join(tr(f'<code style="font-size:11px">{c["file"]}</code>',
                        f'<code style="font-size:11px">{c["key"]}</code>',
                        f'<code style="font-size:11px">{c["value"][:80]}</code>') for c in cfgs[:100])

    dep_rows=''
    for d in deps:
        sc_cls={'system':'red','test':'gray','provided':'blue'}.get(d['scope'],'green')
        sp=f'<br><small style="color:#6b7280">{d["systemPath"][:60]}</small>' if d['systemPath'] else ''
        dep_rows+=tr(f'<code style="font-size:11px">{d["groupId"]}</code>',
                     f'<code style="font-size:11px">{d["artifactId"]}</code>',
                     d['version'], badge(d['scope'],sc_cls)+sp)

    sdk_rows=''.join(tr(f'<code style="font-size:11px">{s["file"]}:{s["line"]}</code>',
                        f'<code style="font-size:11px">{s["package"]}</code>') for s in sdks[:50])

    html=f'''<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>설계 분석 리포트 — {now}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Noto Sans KR",system-ui,sans-serif;background:#f8fafc;color:#1e293b;font-size:14px}}
.hdr{{background:linear-gradient(135deg,#1e3a5f,#2563eb);color:white;padding:24px 40px}}
.hdr h1{{font-size:22px;font-weight:700}}.hdr p{{font-size:13px;opacity:.75;margin-top:4px}}
.body{{max-width:1200px;margin:0 auto;padding:28px 20px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:24px}}
.stat{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:14px;text-align:center}}
.num{{font-size:26px;font-weight:700;color:#2563eb;line-height:1;margin:4px 0}}
.lbl{{font-size:11px;color:#64748b}}
.sec{{background:white;border:1px solid #e2e8f0;border-radius:12px;margin-bottom:20px;overflow:hidden}}
.sec-hd{{background:#f1f5f9;padding:13px 20px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #e2e8f0}}
.sec-title{{font-size:14px;font-weight:700;color:#1e3a5f}}
.sec-cnt{{font-size:11px;color:#64748b}}
table{{width:100%;border-collapse:collapse}}
th{{background:#f8fafc;color:#475569;font-size:11px;font-weight:600;padding:9px 10px;text-align:left;border-bottom:1px solid #e2e8f0}}
.alert{{margin:14px 20px;padding:11px 15px;border-radius:8px;font-size:12px;line-height:1.6}}
.alert-red{{background:#fee2e2;border-left:4px solid #ef4444;color:#991b1b}}
.alert-amber{{background:#fef3c7;border-left:4px solid #f59e0b;color:#92400e}}
.footer{{text-align:center;color:#94a3b8;font-size:12px;padding:24px}}
</style></head><body>
<div class="hdr"><h1>⚡ 설계 자동 분석 리포트</h1>
<p>소스: {data["src_dir"]} {'| 기획서: '+data["plan_file"] if data["plan_file"] else ''} | 생성: {now}</p></div>
<div class="body">
<div class="stats">
  <div class="stat"><div class="lbl">분석 파일</div><div class="num">{data["file_count"]}</div><div class="lbl">개</div></div>
  <div class="stat"><div class="lbl">추출 URL</div><div class="num">{len(urls)}</div><div class="lbl">개</div></div>
  <div class="stat"><div class="lbl">하드코딩</div><div class="num" style="color:#f59e0b">{len(hc)}</div><div class="lbl">건 탐지</div></div>
  <div class="stat"><div class="lbl">URL 생존</div><div class="num" style="color:#10b981">{alive}</div><div class="lbl">/ {len(chks)}개</div></div>
  <div class="stat"><div class="lbl">URL 불가</div><div class="num" style="color:#ef4444">{dead}</div><div class="lbl">건</div></div>
  <div class="stat"><div class="lbl">SDK import</div><div class="num" style="color:#8b5cf6">{len(sdks)}</div><div class="lbl">건</div></div>
</div>

<div class="sec"><div class="sec-hd"><span class="sec-title">🔗 추출 URL</span><span class="sec-cnt">{len(urls)}개</span></div>
<table><tr><th>위치</th><th>URL</th><th>상태</th></tr>{url_rows}</table></div>

{'<div class="sec"><div class="sec-hd"><span class="sec-title">⚠️ 하드코딩 탐지</span><span class="sec-cnt">'+str(len(hc))+'건</span></div><div class="alert alert-amber">하드코딩된 항목이 발견되었습니다. 설정 파일 분리를 검토하세요.</div><table><tr><th>위치</th><th>유형</th><th>값</th></tr>'+hc_rows+'</table></div>' if hc else ''}

{'<div class="sec"><div class="sec-hd"><span class="sec-title">📦 SDK Import</span><span class="sec-cnt">'+str(len(sdks))+'건</span></div><div class="alert alert-red">외부 SDK JAR 의존성이 있습니다. API 전환 시 교체 필요 여부를 확인하세요.</div><table><tr><th>파일</th><th>패키지</th></tr>'+sdk_rows+'</table></div>' if sdks else ''}

<div class="sec"><div class="sec-hd"><span class="sec-title">⚙️ Properties 설정</span><span class="sec-cnt">{len(cfgs)}항목</span></div>
<table><tr><th>파일</th><th>키</th><th>값</th></tr>{cfg_rows}</table></div>

{'<div class="sec"><div class="sec-hd"><span class="sec-title">📌 pom.xml 의존성</span><span class="sec-cnt">'+str(len(deps))+'개</span></div><table><tr><th>groupId</th><th>artifactId</th><th>version</th><th>scope</th></tr>'+dep_rows+'</table></div>' if deps else ''}

</div><div class="footer">설계 자동 분석 도구 | {now}</div></body></html>'''
    Path(output_path).write_text(html, encoding='utf-8')

# ══════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('설계 자동 분석 도구')
        self.geometry('680x620')
        self.resizable(True, True)
        self.configure(bg='#f8fafc')
        self._build()
        self.running = False

    def _build(self):
        # ── 헤더
        hdr = tk.Frame(self, bg='#1e3a5f', height=56)
        hdr.pack(fill='x')
        tk.Label(hdr, text='⚡  설계 자동 분석 도구', bg='#1e3a5f', fg='white',
                 font=('맑은 고딕', 14, 'bold')).pack(side='left', padx=20, pady=14)
        tk.Label(hdr, text='소스 폴더 → HTML 리포트 자동 생성', bg='#1e3a5f', fg='#93c5fd',
                 font=('맑은 고딕', 10)).pack(side='left', pady=14)

        body = tk.Frame(self, bg='#f8fafc', padx=24, pady=20)
        body.pack(fill='both', expand=True)

        # ── 소스 폴더
        self._label(body, '① 소스 폴더 선택')
        row1 = tk.Frame(body, bg='#f8fafc')
        row1.pack(fill='x', pady=(4,12))
        self.src_var = tk.StringVar()
        src_entry = tk.Entry(row1, textvariable=self.src_var, font=('맑은 고딕',10),
                             relief='solid', bd=1, bg='white')
        src_entry.pack(side='left', fill='x', expand=True, ipady=6, padx=(0,8))
        tk.Button(row1, text='찾아보기', command=self._browse_src,
                  bg='#2563eb', fg='white', font=('맑은 고딕',10,'bold'),
                  relief='flat', padx=12, cursor='hand2').pack(side='left')

        # ── 기획서
        self._label(body, '② 기획서 파일 선택 (선택)')
        row2 = tk.Frame(body, bg='#f8fafc')
        row2.pack(fill='x', pady=(4,12))
        self.plan_var = tk.StringVar()
        tk.Entry(row2, textvariable=self.plan_var, font=('맑은 고딕',10),
                 relief='solid', bd=1, bg='white').pack(side='left', fill='x', expand=True, ipady=6, padx=(0,8))
        tk.Button(row2, text='찾아보기', command=self._browse_plan,
                  bg='#6366f1', fg='white', font=('맑은 고딕',10,'bold'),
                  relief='flat', padx=12, cursor='hand2').pack(side='left')

        # ── 출력 경로
        self._label(body, '③ 리포트 저장 위치')
        row3 = tk.Frame(body, bg='#f8fafc')
        row3.pack(fill='x', pady=(4,12))
        self.out_var = tk.StringVar(value=str(Path.home()/'Desktop'))
        tk.Entry(row3, textvariable=self.out_var, font=('맑은 고딕',10),
                 relief='solid', bd=1, bg='white').pack(side='left', fill='x', expand=True, ipady=6, padx=(0,8))
        tk.Button(row3, text='찾아보기', command=self._browse_out,
                  bg='#64748b', fg='white', font=('맑은 고딕',10,'bold'),
                  relief='flat', padx=12, cursor='hand2').pack(side='left')

        # ── 옵션
        opt = tk.Frame(body, bg='#f8fafc')
        opt.pack(fill='x', pady=(0,16))
        self.check_url_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opt, text='URL 생존 확인 (시간 더 걸림)', variable=self.check_url_var,
                       bg='#f8fafc', font=('맑은 고딕',10), cursor='hand2').pack(side='left')

        # ── 실행 버튼
        self.run_btn = tk.Button(body, text='▶  분석 시작', command=self._run,
                                 bg='#2563eb', fg='white', font=('맑은 고딕',13,'bold'),
                                 relief='flat', padx=20, pady=10, cursor='hand2')
        self.run_btn.pack(fill='x', pady=(0,14))

        # ── 진행 바
        self.progress = ttk.Progressbar(body, mode='indeterminate')
        self.progress.pack(fill='x', pady=(0,10))

        # ── 로그창
        self._label(body, '실행 로그')
        self.log = scrolledtext.ScrolledText(body, height=12, font=('Consolas',9),
                                             bg='#0f172a', fg='#94a3b8',
                                             relief='flat', bd=0, state='disabled')
        self.log.pack(fill='both', expand=True, pady=(4,0))
        self.log.tag_config('ok',  foreground='#34d399')
        self.log.tag_config('err', foreground='#f87171')
        self.log.tag_config('warn',foreground='#fbbf24')
        self.log.tag_config('info',foreground='#60a5fa')

    def _label(self, parent, text):
        tk.Label(parent, text=text, bg='#f8fafc', fg='#475569',
                 font=('맑은 고딕',10,'bold')).pack(anchor='w', pady=(0,2))

    def _browse_src(self):
        d = filedialog.askdirectory(title='소스 폴더 선택')
        if d: self.src_var.set(d)

    def _browse_plan(self):
        f = filedialog.askopenfilename(title='기획서 파일 선택',
                                       filetypes=[('HTML/텍스트','*.html *.htm *.txt *.md'),('전체','*.*')])
        if f: self.plan_var.set(f)

    def _browse_out(self):
        d = filedialog.askdirectory(title='리포트 저장 폴더 선택')
        if d: self.out_var.set(d)

    def _log(self, msg, tag='info'):
        self.log.config(state='normal')
        ts = datetime.now().strftime('%H:%M:%S')
        self.log.insert('end', f'[{ts}] {msg}\n', tag)
        self.log.see('end')
        self.log.config(state='disabled')

    def _run(self):
        if self.running: return
        src = self.src_var.get().strip()
        if not src:
            messagebox.showwarning('경고', '소스 폴더를 선택해주세요')
            return
        if not Path(src).exists():
            messagebox.showerror('오류', f'폴더가 없습니다:\n{src}')
            return
        self.running = True
        self.run_btn.config(state='disabled', text='분석 중...')
        self.progress.start(10)
        threading.Thread(target=self._analyze, daemon=True).start()

    def _analyze(self):
        try:
            src = Path(self.src_var.get())
            plan = self.plan_var.get().strip()
            out_dir = Path(self.out_var.get())
            now_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            out_file = out_dir / f'report_{now_str}.html'

            self._log(f'소스 폴더: {src}', 'info')
            if plan: self._log(f'기획서: {plan}', 'info')

            self._log('[1/6] 파일 수집 중...', 'info')
            files = collect_files(src)
            self._log(f'      → {len(files)}개 파일 발견', 'ok')

            self._log('[2/6] URL 추출 중...', 'info')
            urls = extract_urls(files, src)
            self._log(f'      → {len(urls)}개 URL 추출', 'ok')

            self._log('[3/6] 하드코딩 탐지 중...', 'info')
            hc = detect_hardcoded(files, src)
            self._log(f'      → {len(hc)}건 탐지', 'warn' if hc else 'ok')

            self._log('[4/6] Properties 파싱 중...', 'info')
            cfgs = parse_properties(files, src)
            self._log(f'      → {len(cfgs)}개 설정 항목', 'ok')

            self._log('[5/6] 의존성 · SDK 분석 중...', 'info')
            deps = parse_pom(files, src)
            sdks = detect_sdk_imports(files, src)
            self._log(f'      → 의존성 {len(deps)}개, SDK import {len(sdks)}건',
                      'warn' if sdks else 'ok')

            url_checks = []
            if self.check_url_var.get():
                self._log('[6/6] URL 생존 확인 중...', 'info')
                unique = list({u['url'] for u in urls})[:30]
                for i, url in enumerate(unique, 1):
                    self._log(f'      [{i}/{len(unique)}] {url[:55]}...', 'info')
                    code, msg = check_url(url)
                    alive = 200 <= code < 400
                    url_checks.append({'url': url, 'status': code, 'message': msg})
                    self._log(f'      → {code} {msg}', 'ok' if alive else 'err')
            else:
                self._log('[6/6] URL 생존 확인 생략', 'info')

            plan_text = extract_html_text(plan) if plan else ''
            self._log('리포트 생성 중...', 'info')
            generate_report({
                'urls': urls, 'hardcoded': hc, 'configs': cfgs,
                'deps': deps, 'sdks': sdks, 'url_checks': url_checks,
                'file_count': len(files), 'src_dir': str(src),
                'plan_file': plan, 'plan_channels': []
            }, out_file)

            self._log(f'✅ 완료!  →  {out_file}', 'ok')
            # 자동으로 브라우저에서 열기
            import webbrowser
            webbrowser.open(out_file.as_uri())
            messagebox.showinfo('완료', f'분석 완료!\n\n리포트가 브라우저에서 열렸습니다.\n\n저장 위치:\n{out_file}')

        except Exception as e:
            self._log(f'오류: {e}', 'err')
            messagebox.showerror('오류', str(e))
        finally:
            self.running = False
            self.run_btn.config(state='normal', text='▶  분석 시작')
            self.progress.stop()

if __name__ == '__main__':
    app = App()
    app.mainloop()
